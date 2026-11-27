"""HarmonyOS NEXT screen recording via official `snapshot_display` and host `ffmpeg`."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Any

from moyu_harmony_pilot.device.diagnostics import (
    enforce_file_mode_0600,
    read_image_dimensions,
    write_secure_text,
)
from moyu_harmony_pilot.device.errors import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
)
from moyu_harmony_pilot.device.lock import ensure_secure_dir
from moyu_harmony_pilot.hdc import SubprocessHdcTransport

if TYPE_CHECKING:
    from moyu_harmony_pilot.device.controller import HarmonyDevice

MIN_RECORD_FPS = 0.5
MAX_RECORD_FPS = 5.0
DEFAULT_RECORD_FPS = 2.0
MIN_RECORD_DURATION = 1.0
MAX_RECORD_DURATION = 600.0
DEFAULT_RECORD_MAX_DURATION = 120.0
DEFAULT_RECORD_MAX_WIDTH = 720
MAX_RECORD_WIDTH = 8192
DEFAULT_OUTPUT_FPS = 10

FrameEncoder = Callable[
    [Sequence[tuple[Path, float]], Path, float | None, int | None, Path | None],
    tuple[int | None, int | None],
]


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Structured metadata for a completed local MP4 screen recording."""

    alias: str
    path: Path
    video_format: str
    frame_count: int
    duration_seconds: float
    effective_fps: float
    width: int | None
    height: int | None
    size_bytes: int
    stopped_reason: str = "stopped"

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "path": str(self.path),
            "format": self.video_format,
            "frame_count": self.frame_count,
            "duration_seconds": self.duration_seconds,
            "effective_fps": self.effective_fps,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "stopped_reason": self.stopped_reason,
        }


@dataclass(frozen=True, slots=True)
class RecordingSessionInfo:
    """Metadata for a background cross-process recording session (never exposes raw serial)."""

    session_id: str
    alias: str
    pid: int
    output_path: Path
    started_at: str
    max_duration: float
    fps: float
    max_width: int | None
    display_id: int | None
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "alias": self.alias,
            "pid": self.pid,
            "path": str(self.output_path),
            "started_at": self.started_at,
            "max_duration": self.max_duration,
            "fps": self.fps,
            "max_width": self.max_width,
            "display_id": self.display_id,
            "active": self.active,
        }


def _device_session_key(serial: str) -> str:
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]


def validate_recording_options(
    *,
    output_path: str | Path | None = None,
    fps: float = DEFAULT_RECORD_FPS,
    max_duration: float = DEFAULT_RECORD_MAX_DURATION,
    max_width: int | None = DEFAULT_RECORD_MAX_WIDTH,
    display_id: int | None = None,
) -> Path | None:
    """Validate recording parameters fail-fast before any device I/O."""
    if (
        type(fps) not in (int, float)
        or not math.isfinite(fps)
        or not MIN_RECORD_FPS <= float(fps) <= MAX_RECORD_FPS
    ):
        raise DeviceConfigurationError(
            f"fps must be between {MIN_RECORD_FPS} and {MAX_RECORD_FPS}, got {fps!r}."
        )
    if (
        type(max_duration) not in (int, float)
        or not math.isfinite(max_duration)
        or not MIN_RECORD_DURATION <= float(max_duration) <= MAX_RECORD_DURATION
    ):
        raise DeviceConfigurationError(
            f"duration must be between {MIN_RECORD_DURATION} and {MAX_RECORD_DURATION} seconds, "
            f"got {max_duration!r}."
        )
    if max_width is not None and (
        type(max_width) is not int or not 1 <= max_width <= MAX_RECORD_WIDTH
    ):
        raise DeviceConfigurationError(
            f"max_width must be an integer between 1 and {MAX_RECORD_WIDTH}, got {max_width!r}."
        )
    if display_id is not None and (type(display_id) is not int or display_id < 0):
        raise DeviceConfigurationError(
            f"display_id must be a non-negative integer, got {display_id!r}."
        )
    if output_path is None:
        return None
    resolved = Path(output_path).expanduser()
    if resolved.suffix and resolved.suffix.lower() != ".mp4":
        raise DeviceConfigurationError(
            f"Unsupported recording format {resolved.suffix!r}; expected '.mp4'."
        )
    if not resolved.suffix:
        resolved = resolved.with_suffix(".mp4")
    return resolved


def find_ffmpeg(ffmpeg_path: str | Path | None = None) -> Path | None:
    """Locate an executable `ffmpeg` binary on the host, or return None."""
    if ffmpeg_path is not None:
        candidate = Path(ffmpeg_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def require_ffmpeg(ffmpeg_path: str | Path | None = None) -> Path:
    """Return the host `ffmpeg` binary path or raise `DeviceConfigurationError`."""
    found = find_ffmpeg(ffmpeg_path)
    if found is None:
        raise DeviceConfigurationError(
            "Screen recording to MP4 requires `ffmpeg` installed on the host.",
            hint="Install ffmpeg (e.g. `brew install ffmpeg` on macOS or `apt install ffmpeg`).",
        )
    return found


def _compute_encoded_dimensions(
    first_frame: Path,
    *,
    max_width: int | None,
) -> tuple[int | None, int | None]:
    width, height = read_image_dimensions(first_frame)
    if width is None or height is None or width <= 0 or height <= 0:
        return (None, None)
    target_w = min(width, max_width) if max_width is not None else width
    even_w = max(2, (target_w // 2) * 2)
    scaled_h = round(height * (even_w / width))
    even_h = max(2, (scaled_h // 2) * 2)
    return (even_w, even_h)


def _escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def encode_frames_mp4(
    frames: Sequence[tuple[Path, float]],
    output_path: Path,
    end_monotonic: float | None = None,
    max_width: int | None = None,
    ffmpeg_path: Path | None = None,
    *,
    output_fps: int = DEFAULT_OUTPUT_FPS,
    timeout: float = 60.0,
) -> tuple[int | None, int | None]:
    """Encode timestamped JPEG frames into a real-time 1:1 H.264 MP4 via host `ffmpeg`."""
    if not frames:
        raise DeviceError("No frames were captured for screen recording.", code="recording_empty")
    binary = require_ffmpeg(ffmpeg_path)
    dest = Path(output_path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    durations: list[float] = []
    for idx, (_path, ts) in enumerate(frames):
        if idx + 1 < len(frames):
            delta = frames[idx + 1][1] - ts
        elif end_monotonic is not None and end_monotonic > ts:
            delta = end_monotonic - ts
        elif durations:
            delta = sum(durations) / len(durations)
        else:
            delta = 0.5
        durations.append(max(0.05, min(30.0, float(delta))))

    lines: list[str] = []
    for (frame_path, _ts), duration in zip(frames, durations, strict=True):
        escaped = _escape_concat_path(frame_path)
        lines.append(f"file '{escaped}'")
        lines.append(f"duration {duration:.4f}")
    # Repeat the final frame so ffmpeg's concat demuxer honors its duration.
    lines.append(f"file '{_escape_concat_path(frames[-1][0])}'")

    if max_width is not None:
        vf = f"scale='trunc(min(iw,{max_width})/2)*2':-2"
    else:
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    concat_file = frames[0][0].parent / "frames.concat.txt"
    write_secure_text(concat_file, "\n".join(lines) + "\n")

    fd, tmp_name = tempfile.mkstemp(prefix=".moyu_rec_", suffix=".mp4", dir=dest.parent)
    os.close(fd)
    tmp_mp4 = Path(tmp_name)
    try:
        cmd = [
            str(binary),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-vf",
            vf,
            "-r",
            str(output_fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(tmp_mp4),
        ]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(
                f"ffmpeg timed out after {timeout:.1f}s while encoding screen recording.",
                code="recording_encode_failed",
            ) from exc
        if proc.returncode != 0 or not tmp_mp4.is_file() or tmp_mp4.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout).strip()
            msg = "ffmpeg failed to encode screen recording."
            if detail:
                msg = f"{msg} ({detail[:240]})"
            raise DeviceError(msg, code="recording_encode_failed")
        os.replace(tmp_mp4, dest)
        enforce_file_mode_0600(dest)
    except BaseException:
        tmp_mp4.unlink(missing_ok=True)
        raise

    return _compute_encoded_dimensions(frames[0][0], max_width=max_width)


class ScreenRecorder:
    """In-process background frame-loop recorder using official `snapshot_display`.

    Does NOT hold `DeviceLock`, because `snapshot_display` safely executes concurrently
    with `uitest` operations on HarmonyOS NEXT.
    """

    def __init__(
        self,
        device: HarmonyDevice,
        output_path: str | Path,
        *,
        fps: float = DEFAULT_RECORD_FPS,
        max_duration: float = DEFAULT_RECORD_MAX_DURATION,
        max_width: int | None = DEFAULT_RECORD_MAX_WIDTH,
        display_id: int | None = None,
        frame_timeout: float = 10.0,
        ffmpeg_path: str | Path | None = None,
        encoder: FrameEncoder | None = None,
        clock: Callable[[], float] = time.monotonic,
        work_dir: Path | None = None,
    ) -> None:
        resolved = validate_recording_options(
            output_path=output_path,
            fps=fps,
            max_duration=max_duration,
            max_width=max_width,
            display_id=display_id,
        )
        assert resolved is not None
        self._device = device
        self._output_path = resolved
        self._fps = float(fps)
        self._max_duration = float(max_duration)
        self._max_width = max_width
        self._display_id = display_id
        self._frame_timeout = max(1.0, float(frame_timeout))
        self._encoder: FrameEncoder = encoder or encode_frames_mp4
        self._ffmpeg_binary = require_ffmpeg(ffmpeg_path) if encoder is None else None
        self._clock = clock
        self._custom_work_dir = work_dir

        self._work_dir: Path | None = None
        self._frames: list[tuple[Path, float]] = []
        self._frames_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopped_reason = "stopped"
        self._capture_error: Exception | None = None
        self._result: RecordingResult | None = None
        self._started = False
        self._stopped = False

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def result(self) -> RecordingResult | None:
        return self._result

    @property
    def frame_count(self) -> int:
        with self._frames_lock:
            return len(self._frames)

    def _capture_one_frame(self) -> bool:
        if self._work_dir is None:
            return False
        with self._frames_lock:
            seq = len(self._frames) + 1
        frame_path = self._work_dir / f"frame_{seq:06d}.jpeg"
        captured_at = self._clock()
        self._device.screenshot(
            frame_path,
            image_format="jpeg",
            display_id=self._display_id,
            max_width=None,
            timeout=self._frame_timeout,
        )
        with self._frames_lock:
            self._frames.append((frame_path, captured_at))
        return True

    def start(self, *, capture_initial_frame: bool = True) -> ScreenRecorder:
        """Start background screen capture; optionally capture one initial frame immediately."""
        if self._started:
            raise DeviceBusyError("ScreenRecorder has already been started.")
        if self._custom_work_dir is not None:
            self._work_dir = ensure_secure_dir(self._custom_work_dir)
        else:
            rec_root = ensure_secure_dir(self._device.lock_dir.parent / "recordings")
            self._work_dir = ensure_secure_dir(rec_root / f"work_{uuid.uuid4().hex[:12]}")

        self._started_at = self._clock()
        try:
            if capture_initial_frame:
                self._capture_one_frame()
        except BaseException:
            self._cleanup_work_dir()
            raise

        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"harmony-record-{self._device.alias}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _run_loop(self) -> None:
        assert self._started_at is not None
        interval = 1.0 / self._fps
        deadline = self._started_at + self._max_duration
        consecutive_failures = 0

        while not self._stop_event.is_set():
            now = self._clock()
            if now >= deadline:
                self._stopped_reason = "duration_limit"
                self._stop_event.set()
                break

            with self._frames_lock:
                last_ts = self._frames[-1][1] if self._frames else None
            if last_ts is not None:
                wait_for = min(interval - (now - last_ts), deadline - now)
                if wait_for > 0 and self._stop_event.wait(timeout=wait_for):
                    break
                if self._clock() >= deadline:
                    self._stopped_reason = "duration_limit"
                    self._stop_event.set()
                    break

            try:
                self._capture_one_frame()
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self._capture_error = exc
                    self._stopped_reason = "capture_error"
                    self._stop_event.set()
                    break
                if self._stop_event.wait(timeout=min(0.25, max(0.0, deadline - self._clock()))):
                    break

    def stop(
        self,
        *,
        capture_final_frame: bool = False,
        stopped_reason: str | None = None,
    ) -> RecordingResult:
        """Stop capturing, encode collected frames to MP4, and remove temporary frames."""
        if self._stopped and self._result is not None:
            return self._result
        if not self._started:
            raise DeviceConfigurationError("ScreenRecorder was not started.")

        if stopped_reason is not None and self._stopped_reason == "stopped":
            self._stopped_reason = stopped_reason
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._frame_timeout + 5.0)

        try:
            if capture_final_frame and self._capture_error is None:
                with suppress(Exception):
                    self._capture_one_frame()

            end_ts = self._clock()
            with self._frames_lock:
                captured = list(self._frames)
            if not captured:
                if self._capture_error is not None:
                    raise DeviceError(
                        f"Screen recording stopped before capturing frames: {self._capture_error}",
                        code="recording_failed",
                    ) from self._capture_error
                raise DeviceError(
                    "No frames were captured for screen recording.", code="recording_empty"
                )

            width, height = self._encoder(
                captured,
                self._output_path,
                end_ts,
                self._max_width,
                self._ffmpeg_binary,
            )
            enforce_file_mode_0600(self._output_path)
            started_at = self._started_at if self._started_at is not None else captured[0][1]
            duration = round(max(0.0, end_ts - started_at), 3)
            eff_fps = round(len(captured) / duration, 2) if duration > 0 else float(len(captured))
            size_bytes = self._output_path.stat().st_size
            self._result = RecordingResult(
                alias=self._device.alias,
                path=self._output_path,
                video_format="mp4",
                frame_count=len(captured),
                duration_seconds=duration,
                effective_fps=eff_fps,
                width=width,
                height=height,
                size_bytes=size_bytes,
                stopped_reason=self._stopped_reason,
            )
            self._stopped = True
            return self._result
        finally:
            self._cleanup_work_dir()

    def _cleanup_work_dir(self) -> None:
        if self._work_dir is not None and self._work_dir.exists():
            shutil.rmtree(self._work_dir, ignore_errors=True)

    def __enter__(self) -> ScreenRecorder:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._started and not self._stopped:
            try:
                self.stop()
            except Exception:
                self._cleanup_work_dir()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "posix":
        # Reap workers started by this process before checking PID liveness.
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        else:
            if reaped_pid == pid:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # An inconclusive probe must preserve the session for recovery.
        return True
    return True


def _session_file_path(state_dir: Path, serial: str) -> Path:
    rec_dir = ensure_secure_dir(state_dir / "recordings")
    return rec_dir / f"session_{_device_session_key(serial)}.json"


def _read_session_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _session_info_from_record(record: dict[str, Any], *, active: bool) -> RecordingSessionInfo:
    return RecordingSessionInfo(
        session_id=str(record["session_id"]),
        alias=str(record["alias"]),
        pid=int(record["pid"]),
        output_path=Path(str(record["output_path"])),
        started_at=str(record["started_at"]),
        max_duration=float(record["max_duration"]),
        fps=float(record["fps"]),
        max_width=int(record["max_width"]) if record.get("max_width") is not None else None,
        display_id=int(record["display_id"]) if record.get("display_id") is not None else None,
        active=active,
    )


class RecordingSessionManager:
    """Manages detached cross-process recording sessions scoped per device.

    Raw device serials are passed to the detached worker via stdin only and are never
    written to session metadata files or command-line arguments.
    """

    def __init__(self, state_dir: str | Path = ".state") -> None:
        self._state_dir = Path(state_dir)

    def _session_path(self, serial: str) -> Path:
        return _session_file_path(self._state_dir, serial)

    def status(self, device: HarmonyDevice) -> RecordingSessionInfo | None:
        """Inspect active or recently finished recording session metadata for `device`."""
        path = self._session_path(device.profile.serial)
        record = _read_session_record(path)
        if record is None:
            return None
        pid = int(record.get("pid", 0))
        active = _is_pid_alive(pid)
        return _session_info_from_record(record, active=active)

    def start(
        self,
        device: HarmonyDevice,
        output_path: str | Path,
        *,
        fps: float = DEFAULT_RECORD_FPS,
        max_duration: float = DEFAULT_RECORD_MAX_DURATION,
        max_width: int | None = DEFAULT_RECORD_MAX_WIDTH,
        display_id: int | None = None,
        hdc_path: str | None = None,
        ffmpeg_path: str | Path | None = None,
        startup_timeout: float = 15.0,
    ) -> RecordingSessionInfo:
        """Launch a detached background recording session for `device`."""
        if (
            type(startup_timeout) not in (int, float)
            or not math.isfinite(startup_timeout)
            or startup_timeout <= 0
        ):
            raise DeviceConfigurationError(
                "startup_timeout must be a finite positive number."
            )
        resolved_out = validate_recording_options(
            output_path=output_path,
            fps=fps,
            max_duration=max_duration,
            max_width=max_width,
            display_id=display_id,
        )
        assert resolved_out is not None
        ffmpeg_bin = require_ffmpeg(ffmpeg_path)
        resolved_out.parent.mkdir(parents=True, exist_ok=True)

        session_path = self._session_path(device.profile.serial)
        existing = _read_session_record(session_path)
        if existing is not None:
            existing_pid = int(existing.get("pid", 0))
            if _is_pid_alive(existing_pid):
                raise DeviceBusyError(
                    f"Device alias {device.alias!r} already has an active recording session "
                    f"(pid={existing_pid}).",
                    hint="Run `harmony-pilot record stop` before starting a new recording.",
                )
            # Clean up stale session metadata left by an already exited worker.
            old_dir = existing.get("session_dir")
            if isinstance(old_dir, str) and old_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
            session_path.unlink(missing_ok=True)

        device.assert_thermal_safe(timeout=10.0)

        session_id = uuid.uuid4().hex[:12]
        rec_dir = ensure_secure_dir(self._state_dir / "recordings")
        session_dir = rec_dir / f"session_{session_id}"
        ready_file = session_dir / "ready.json"
        stop_file = session_dir / "stop"
        result_file = session_dir / "result.json"
        error_file = session_dir / "error.json"
        started_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        worker_payload: dict[str, Any] = {
            "session_id": session_id,
            "alias": device.alias,
            "serial": device.profile.serial,
            "max_temperature_c": device.profile.max_temperature_c,
            "state_dir": str(self._state_dir.resolve()),
            "output_path": str(resolved_out.resolve()),
            "fps": float(fps),
            "max_duration": float(max_duration),
            "max_width": max_width,
            "display_id": display_id,
            "hdc_path": hdc_path or (str(device._hdc.hdc_path) if device._hdc.hdc_path else None),
            "hdc_default_timeout": device._hdc._default_timeout,
            "ffmpeg_path": str(ffmpeg_bin),
            "session_dir": str(session_dir.resolve()),
            "ready_file": str(ready_file.resolve()),
            "stop_file": str(stop_file.resolve()),
            "result_file": str(result_file.resolve()),
            "error_file": str(error_file.resolve()),
        }
        transport = device._hdc.transport
        if isinstance(transport, SubprocessHdcTransport):
            worker_payload["hdc_env"] = dict(transport._env)
            worker_payload["hdc_transport_timeout"] = transport._default_timeout

        proc: subprocess.Popen[bytes] | None = None
        try:
            ensure_secure_dir(session_dir)
            deadline = time.monotonic() + float(startup_timeout)
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "moyu_harmony_pilot.device.recording"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            assert proc.stdin is not None
            try:
                proc.stdin.write(json.dumps(worker_payload).encode("utf-8"))
                proc.stdin.close()
            except OSError as exc:
                raise DeviceError(
                    "Failed to hand off recording configuration to worker.",
                    code="recording_start_failed",
                ) from exc

            while time.monotonic() < deadline:
                if ready_file.is_file():
                    break
                if error_file.is_file():
                    err_data = _read_session_record(error_file) or {}
                    raise DeviceError(
                        str(err_data.get("message") or "Recording worker failed during startup."),
                        code=str(err_data.get("code") or "recording_start_failed"),
                    )
                if proc.poll() is not None:
                    raise DeviceError(
                        f"Recording worker exited prematurely (code={proc.returncode}).",
                        code="recording_start_failed",
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            else:
                raise DeviceError(
                    "Timed out waiting for recording worker to capture the initial frame.",
                    code="recording_start_failed",
                )

            # Persist only session metadata. Device credentials and transport stay on stdin.
            metadata = {
                "session_id": session_id,
                "alias": device.alias,
                "pid": proc.pid,
                "output_path": str(resolved_out.resolve()),
                "started_at": started_at,
                "max_duration": float(max_duration),
                "fps": float(fps),
                "max_width": max_width,
                "display_id": display_id,
                "session_dir": str(session_dir.resolve()),
                "stop_file": str(stop_file.resolve()),
                "result_file": str(result_file.resolve()),
                "error_file": str(error_file.resolve()),
            }
            metadata_file = session_dir / "session.json"
            write_secure_text(metadata_file, json.dumps(metadata, ensure_ascii=False, indent=2))
            os.replace(metadata_file, session_path)
            return _session_info_from_record(metadata, active=True)
        except BaseException:
            try:
                if proc is not None:
                    try:
                        if proc.poll() is None:
                            with suppress(ProcessLookupError):
                                proc.terminate()
                        with suppress(subprocess.TimeoutExpired):
                            proc.wait(timeout=2.0)
                    finally:
                        # A budget interruption during graceful shutdown still reaps the worker.
                        if proc.poll() is None:
                            with suppress(ProcessLookupError):
                                proc.kill()
                        proc.wait()
            finally:
                if proc is not None and proc.stdin is not None:
                    with suppress(OSError):
                        proc.stdin.close()
                # Reap the worker before removing paths it may still be using.
                if proc is None or proc.poll() is not None:
                    published = _read_session_record(session_path)
                    if published is not None and published.get("session_id") == session_id:
                        session_path.unlink(missing_ok=True)
                    shutil.rmtree(session_dir, ignore_errors=True)
            raise

    def stop(
        self,
        device: HarmonyDevice,
        *,
        timeout: float = 45.0,
    ) -> RecordingResult:
        """Stop an active recording session, wait for MP4 encoding, and return its result."""
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise DeviceConfigurationError("timeout must be a finite positive number.")
        deadline = time.monotonic() + float(timeout)
        session_path = self._session_path(device.profile.serial)
        record = _read_session_record(session_path)
        if record is None:
            raise DeviceConfigurationError(
                f"No active recording session found for device alias {device.alias!r}.",
                hint="Start a recording first with `harmony-pilot record start --out demo.mp4`.",
            )

        pid = int(record.get("pid", 0))
        stop_file = Path(str(record["stop_file"]))
        result_file = Path(str(record["result_file"]))
        error_file = Path(str(record["error_file"]))
        session_dir = Path(str(record["session_dir"]))

        try:
            if not result_file.is_file() and not error_file.is_file():
                if stop_file.parent.is_dir():
                    write_secure_text(stop_file, "stop\n")
            signalled = False
            while _is_pid_alive(pid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError(
                        f"Timed out waiting for recording session {record['session_id']!r} "
                        "to finish encoding.",
                        code="recording_stop_timeout",
                        hint="Retry `harmony-pilot record stop` to collect the recording result.",
                    )
                if not signalled and remaining < timeout * 0.75:
                    with suppress(OSError):
                        os.kill(pid, signal.SIGTERM)
                    signalled = True
                time.sleep(min(0.05, remaining))

            if error_file.is_file():
                err_data = _read_session_record(error_file) or {}
                raise DeviceError(
                    str(err_data.get("message") or "Recording session failed."),
                    code=str(err_data.get("code") or "recording_failed"),
                )

            result_data = _read_session_record(result_file)
            if result_data is None:
                raise DeviceError(
                    "Recording worker exited without writing an encoded MP4 result.",
                    code="recording_failed",
                )

            return RecordingResult(
                alias=str(result_data["alias"]),
                path=Path(str(result_data["path"])),
                video_format=str(result_data.get("format", "mp4")),
                frame_count=int(result_data["frame_count"]),
                duration_seconds=float(result_data["duration_seconds"]),
                effective_fps=float(result_data["effective_fps"]),
                width=(
                    int(result_data["width"]) if result_data.get("width") is not None else None
                ),
                height=(
                    int(result_data["height"]) if result_data.get("height") is not None else None
                ),
                size_bytes=int(result_data["size_bytes"]),
                stopped_reason=str(result_data.get("stopped_reason", "stopped")),
            )
        finally:
            # An interrupted wait leaves a live session available to status/stop retries.
            if not _is_pid_alive(pid):
                current = _read_session_record(session_path)
                if current is not None and current.get("session_id") == record["session_id"]:
                    session_path.unlink(missing_ok=True)
                shutil.rmtree(session_dir, ignore_errors=True)


def _run_worker_main() -> int:
    """Detached worker entrypoint invoked by `RecordingSessionManager.start`."""
    from moyu_harmony_pilot.device.controller import HarmonyDevice
    from moyu_harmony_pilot.device.models import DeviceProfile
    from moyu_harmony_pilot.hdc import HdcClient

    raw = sys.stdin.read()
    payload = json.loads(raw)
    ready_file = Path(payload["ready_file"])
    stop_file = Path(payload["stop_file"])
    result_file = Path(payload["result_file"])
    error_file = Path(payload["error_file"])
    session_dir = Path(payload["session_dir"])

    stop_requested = threading.Event()
    stop_reason = ["stopped"]

    def _handle_signal(_signum: int, _frame: FrameType | None) -> None:
        stop_reason[0] = "stopped"
        stop_requested.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        default_timeout = float(payload.get("hdc_default_timeout", 15.0))
        transport = SubprocessHdcTransport(
            hdc_path=payload.get("hdc_path"),
            env=payload.get("hdc_env"),
            default_timeout=float(payload.get("hdc_transport_timeout", default_timeout)),
        )
        hdc = HdcClient(transport=transport, default_timeout=default_timeout)
        profile = DeviceProfile(
            alias=str(payload["alias"]),
            serial=str(payload["serial"]),
            max_temperature_c=float(payload.get("max_temperature_c", 42.0)),
        )
        device = HarmonyDevice(hdc, profile, state_dir=payload["state_dir"])
        recorder = ScreenRecorder(
            device,
            payload["output_path"],
            fps=float(payload["fps"]),
            max_duration=float(payload["max_duration"]),
            max_width=payload.get("max_width"),
            display_id=payload.get("display_id"),
            ffmpeg_path=payload.get("ffmpeg_path"),
            work_dir=session_dir / "frames",
        )
        recorder.start(capture_initial_frame=True)
        write_secure_text(ready_file, json.dumps({"pid": os.getpid(), "ready": True}))

        while not stop_requested.is_set():
            if stop_file.exists():
                stop_reason[0] = "stopped"
                break
            if recorder._stop_event.is_set():
                stop_reason[0] = recorder._stopped_reason
                break
            time.sleep(0.05)

        result = recorder.stop(
            capture_final_frame=(stop_reason[0] == "stopped"),
            stopped_reason=stop_reason[0],
        )
        write_secure_text(result_file, json.dumps(result.to_dict(), ensure_ascii=False))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, DeviceError) else "recording_failed"
        try:
            write_secure_text(
                error_file,
                json.dumps({"code": code, "message": str(exc)}, ensure_ascii=False),
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_worker_main())
