"""Export individual device files and gallery assets through official HarmonyOS tools."""

from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from moyu_harmony_pilot.device.diagnostics import enforce_file_mode_0600
from moyu_harmony_pilot.device.errors import DeviceConfigurationError, DeviceError
from moyu_harmony_pilot.hdc import HdcClient, HdcError, HdcTimeoutError, make_remote_tmp_path

_MEDIA_URI_RE = re.compile(r"file://media/Photo/[1-9][0-9]*(?:/[^\r\n]+)?")
_MEDIA_PATH_RE = re.compile(r"/storage/media/(?:[0-9]+/)?local/files/Photo/.+")
_QUERY_COUNT_RE = re.compile(r"^find ([0-9]+) results?:?\s*$", re.MULTILINE)
_MEDIA_HINT = (
    "Check the gallery filename and device mediatool support. "
    "Use a locally available asset accessible to the current device user."
)


@dataclass(frozen=True)
class MediaQueryResult:
    """Bounded, deterministic gallery URI discovery for one display name."""

    name: str
    uris: tuple[str, ...]
    total: int

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "uris": list(self.uris),
            "count": len(self.uris),
            "total": self.total,
            "truncated": self.total > len(self.uris),
        }


@dataclass(frozen=True)
class FileExportResult:
    """Local artifact metadata with any outstanding remote temporary-file cleanup."""

    source: str
    path: Path
    size_bytes: int
    sha256: str
    transport: Literal["hdc", "mediatool"]
    cleanup_pending: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "transport": self.transport,
            "cleanup_pending": list(self.cleanup_pending),
        }


def _validate_text(value: str, *, field: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DeviceConfigurationError(
            f"{field} must contain 1..{maximum} characters without control characters."
        )


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise DeviceConfigurationError("timeout must be finite and within (0, 3600] seconds.")


def validate_media_query(name: str, *, limit: int = 100, timeout: float = 30.0) -> None:
    """Validate discovery options before accessing a device."""
    _validate_text(name, field="name", maximum=255)
    if name.startswith("-") or "/" in name or name in {".", ".."}:
        raise DeviceConfigurationError("name must be a gallery filename including its extension.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise DeviceConfigurationError("limit must be an integer within 1..1000.")
    _validate_timeout(timeout)


def validate_file_export(
    source: str, output_path: str | Path, *, overwrite: bool = False, timeout: float = 300.0
) -> tuple[Path, Literal["hdc", "mediatool"]]:
    """Validate one source and an explicit local filename without creating files."""
    _validate_text(source, field="source", maximum=4096)
    _validate_text(str(output_path), field="output_path", maximum=4096)
    _validate_timeout(timeout)
    if not isinstance(overwrite, bool):
        raise DeviceConfigurationError("overwrite must be a boolean.")
    mode: Literal["hdc", "mediatool"] = "hdc"
    if _MEDIA_URI_RE.fullmatch(source):
        mode = "mediatool"
    elif not source.startswith("/") or source.startswith("//"):
        raise DeviceConfigurationError(
            "source must be an absolute device file path or file://media/Photo/<id> URI."
        )
    elif _MEDIA_PATH_RE.fullmatch(source):
        mode = "mediatool"
    if source.endswith("/") or ".." in PurePosixPath(source).parts:
        raise DeviceConfigurationError("source must identify one file without parent traversal.")
    dest = Path(output_path).expanduser().absolute()
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        raise DeviceConfigurationError("output_path must identify a regular local file.")
    if dest.exists() and not overwrite:
        raise DeviceConfigurationError(
            "The output file already exists.",
            hint="Choose a new output filename or explicitly pass --overwrite.",
        )
    return dest, mode


def query_media(
    hdc: HdcClient, target: str, name: str, *, limit: int = 100, timeout: float = 30.0
) -> MediaQueryResult:
    """Resolve an exact gallery display name using `mediatool query <name> -u`."""
    validate_media_query(name, limit=limit, timeout=timeout)
    try:
        result = hdc.shell(target, ["mediatool", "query", name, "-u"], timeout=timeout)
    except HdcError as exc:
        raise type(exc)(
            "Gallery query stopped.", code=exc.code, retryable=exc.retryable, hint=exc.hint
        ) from None
    count_match = _QUERY_COUNT_RE.search(result.stdout)
    if count_match is None or "displayName format is not correct" in result.combined_output:
        raise DeviceError(
            "mediatool did not return a valid gallery query result.",
            code="media_query_failed", hint=_MEDIA_HINT,
        )
    uris = set()
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith('"') and candidate.endswith('"'):
            candidate = candidate[1:-1]
        if _MEDIA_URI_RE.fullmatch(candidate):
            uris.add(candidate)
    total = int(count_match[1])
    if total != len(uris):
        raise DeviceError(
            "mediatool returned an incomplete or unsupported gallery URI list.",
            code="media_query_failed", hint=_MEDIA_HINT,
        )
    return MediaQueryResult(name, tuple(sorted(uris)[:limit]), total)


def _remote_file_size(hdc: HdcClient, target: str, source: str, *, timeout: float) -> int:
    result = hdc.shell(target, ["stat", "-c", "%f %s", source], timeout=timeout)
    metadata = re.fullmatch(r"([0-9a-fA-F]+) ([0-9]+)", result.stdout.strip())
    if metadata is None:
        raise DeviceError(
            "Could not read the source file metadata.", code="file_stat_failed",
            hint="Check that the device file exists and is accessible through hdc.",
        )
    if not stat.S_ISREG(int(metadata[1], 16)):
        raise DeviceConfigurationError("source must identify a regular device file.")
    return int(metadata[2])


def _publish_file(
    staged: Path, dest: Path, *, overwrite: bool, remaining: Callable[[], float],
    cleanup_pending: list[dict[str, str]],
) -> None:
    if overwrite:
        os.replace(staged, dest)
        return
    try:
        # Link publication refuses even a concurrent creation of the destination.
        os.link(staged, dest)
        return
    except OSError as exc:
        unsupported = {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}
        if exc.errno not in unsupported and getattr(exc, "winerror", None) not in {1, 50}:
            raise
    # Removable volumes may lack hard links. Exclusive creation still protects existing files.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as output, staged.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                remaining()
                output.write(chunk)
        remaining()
    except BaseException:
        try:
            if os.path.samestat(created, dest.stat(follow_symlinks=False)):
                dest.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_pending.append({"location": "host", "path": str(dest)})
        raise


def export_file(
    hdc: HdcClient,
    target: str,
    source: str,
    output_path: str | Path,
    *,
    overwrite: bool = False,
    timeout: float = 300.0,
) -> FileExportResult:
    """Stage and verify one file, then publish with explicit overwrite semantics."""
    dest, mode = validate_file_export(source, output_path, overwrite=overwrite, timeout=timeout)
    deadline = time.monotonic() + timeout
    cleanup_pending: list[dict[str, str]] = []
    remote_tmp: str | None = None

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise HdcTimeoutError("File export exceeded its timeout.")
        return budget

    try:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        work_dir = tempfile.TemporaryDirectory(prefix=".moyu-export-", dir=dest.parent)
        try:
            staged = Path(work_dir.name) / "payload"
            remote_source = source
            try:
                if mode == "mediatool":
                    remote_tmp = make_remote_tmp_path("export", ".bin")
                    result = hdc.shell(
                        target, ["mediatool", "recv", source, remote_tmp], timeout=remaining()
                    )
                    if remote_tmp not in {line.strip() for line in result.stdout.splitlines()}:
                        raise DeviceError(
                            "mediatool did not confirm the exported file.",
                            code="media_export_failed", hint=_MEDIA_HINT,
                        )
                    remote_source = remote_tmp
                expected_size = _remote_file_size(
                    hdc, target, remote_source, timeout=remaining()
                )
                hdc.recv_file(target, remote_source, staged, timeout=remaining())
                if staged.is_symlink():
                    raise DeviceError(
                        "Expected a regular received file.", code="file_export_failed"
                    )
                enforce_file_mode_0600(staged)
                size_bytes = staged.stat().st_size
                if size_bytes != expected_size:
                    raise DeviceError(
                        "The received file size differs from the observed device file size.",
                        code="file_export_incomplete",
                        hint="Wait until the source file stops changing, then export again.",
                    )
                digest = hashlib.sha256()
                with staged.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        remaining()
                        digest.update(chunk)
                remaining()
                _publish_file(
                    staged, dest, overwrite=overwrite, remaining=remaining,
                    cleanup_pending=cleanup_pending,
                )
            finally:
                # Cleanup receives a separate bounded grace period even after transfer timeout.
                if remote_tmp is not None:
                    try:
                        cleaned = hdc.cleanup_remote_tmp(
                            target, remote_tmp, timeout=10.0, ignore_errors=True
                        )
                    except (HdcError, OSError):
                        cleaned = False
                    if not cleaned:
                        cleanup_pending.append({"location": "device", "path": remote_tmp})
        finally:
            try:
                work_dir.cleanup()
            except OSError:
                cleanup_pending.append({"location": "host", "path": work_dir.name})
        return FileExportResult(
            source, dest, size_bytes, digest.hexdigest(), mode, tuple(cleanup_pending)
        )
    except (DeviceError, HdcError, OSError) as exc:
        error: DeviceError | HdcError
        if isinstance(exc, FileExistsError):
            error = DeviceConfigurationError(
                "The output file already exists.",
                hint="Choose a new output filename or explicitly pass --overwrite.",
            )
        elif isinstance(exc, OSError):
            error = DeviceError(
                "Could not write the exported file.", code="file_export_failed",
                hint="Check the local output path, write permissions and available disk space.",
            )
        elif isinstance(exc, HdcError):
            error = type(exc)(
                "File export stopped.", code=exc.code, retryable=exc.retryable, hint=exc.hint
            )
        else:
            error = exc
        if cleanup_pending:
            error.hint = (error.hint or "Retry after checking the device connection.") + (
                " Clean up the remaining managed paths: "
                + ", ".join(f"{item['location']} {item['path']}" for item in cleanup_pending)
            )
        raise error from None
