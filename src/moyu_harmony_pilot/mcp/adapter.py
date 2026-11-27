"""Thin local tool facade with explicit action and interaction authorization."""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner, InteractionSession
from moyu_harmony_pilot.actions.budget import bounded_call, ensure_deadline_available
from moyu_harmony_pilot.actions.models import ActionBudgetExceeded, ActionContext
from moyu_harmony_pilot.actions.observation import capture_ocr, capture_screenshot
from moyu_harmony_pilot.actions.sms import SMS_ERROR_HINTS
from moyu_harmony_pilot.device import (
    DEFAULT_RECORD_FPS,
    DEFAULT_RECORD_MAX_DURATION,
    DEFAULT_RECORD_MAX_WIDTH,
    DeviceConfigurationError,
    DeviceError,
    DeviceRegistry,
    HarmonyDevice,
    RecordingSessionManager,
    ScreenRecorder,
    require_ffmpeg,
    validate_recording_options,
)
from moyu_harmony_pilot.hdc import ConnectionState, HdcClient, HdcError
from moyu_harmony_pilot.ui import UiError

DEFAULT_MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024


class PilotTools:
    def __init__(
        self,
        hdc: HdcClient,
        devices: DeviceRegistry,
        actions: ActionRegistry,
        *,
        state_dir: str | Path = ".state",
        local_lab: bool = False,
        max_steps: int = 20,
        timeout: float = 60,
        inline_images: bool = False,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        enable_ocr: bool = False,
        enable_sms: bool = False,
        enable_recording: bool = False,
    ) -> None:
        InteractionSession.validate_budget(max_steps, timeout)
        if any(
            type(value) is not bool
            for value in (inline_images, enable_ocr, enable_sms, enable_recording)
        ):
            raise DeviceConfigurationError("Observation startup switches must be boolean.")
        if type(max_image_bytes) is not int or not 1 <= max_image_bytes <= MAX_IMAGE_BYTES:
            raise DeviceConfigurationError(
                f"max_image_bytes must be an integer between 1 and {MAX_IMAGE_BYTES}."
            )
        self.hdc = hdc
        self.devices = devices
        self.actions = actions
        self.state_dir = Path(state_dir)
        self.local_lab = local_lab
        self.max_steps = max_steps
        self.timeout = timeout
        self.inline_images = inline_images
        self.max_image_bytes = max_image_bytes
        self.enable_ocr = enable_ocr
        self.enable_sms = enable_sms
        self.enable_recording = enable_recording
        self.runner = ActionRunner(actions, state_dir=state_dir)
        self._sessions: dict[str, InteractionSession] = {}
        for action_id in actions.allowed_actions:
            actions.get(action_id, remote=True)

    def _device(self, alias: str) -> HarmonyDevice:
        profile = self.devices.get_by_alias(alias)
        if profile is None:
            raise DeviceConfigurationError("Choose an alias from the configured device list.")
        self.devices.resolve(alias, self.hdc.list_targets(), require_online=True)
        return HarmonyDevice(self.hdc, profile, state_dir=self.state_dir)

    def list_devices(self) -> dict[str, Any]:
        targets = {target.serial: target for target in self.hdc.list_targets()}
        rows = []
        for profile in self.devices.profiles:
            target = targets.get(profile.serial)
            state = target.connection_state if target else ConnectionState.OFFLINE
            device = HarmonyDevice(self.hdc, profile, state_dir=self.state_dir)
            rows.append(
                {"alias": profile.alias, "connection_state": state.value, "busy": device.is_busy()}
            )
        return {"devices": rows}

    def health(self, alias: str) -> dict[str, Any]:
        device = self._device(alias)
        with device.acquire_lock():
            return device.get_health().to_dict()

    def dump_ui(self, alias: str) -> dict[str, Any]:
        device = self._device(alias)
        with device.acquire_lock():
            _, snapshot = device.dump_ui_compact()
        return {"alias": alias, "snapshot": snapshot.to_dict()}

    def screenshot(
        self, alias: str, *, max_width: int = 1280, som: bool = False, inline: bool = False,
    ) -> dict[str, Any]:
        if type(max_width) is not int or not 1 <= max_width <= 8192:
            raise DeviceConfigurationError("max_width must be an integer between 1 and 8192.")
        if type(inline) is not bool or type(som) is not bool:
            raise DeviceConfigurationError("inline and som must be boolean.")
        if inline and not self.inline_images:
            raise DeviceConfigurationError(
                "Inline screenshots require explicit --inline-images startup."
            )
        ensure_deadline_available()
        device = self._device(alias)
        context = ActionContext(device, time.monotonic() + self.timeout)
        directory = device.diagnostics.ensure_ready()
        fd, filename = tempfile.mkstemp(prefix="mcp-screen-", suffix=".png", dir=directory)
        os.close(fd)
        try:
            result, snapshot = bounded_call(
                context,
                lambda: capture_screenshot(
                    device, filename, image_format="png", max_width=max_width, som=som,
                    timeout=context.remaining_timeout, guard_sensitive=inline,
                ),
            )
            if inline and not 0 < result.size_bytes <= self.max_image_bytes:
                raise DeviceError(
                    "Screenshot exceeds the inline image byte limit. Reduce max_width.",
                    code="image_too_large",
                )
            device.diagnostics.prune()
            payload = result.to_dict()
            if snapshot is not None:
                payload["snapshot"] = snapshot.to_dict()
            return payload
        except BaseException as exc:
            Path(filename).unlink(missing_ok=True)
            if isinstance(exc, ActionBudgetExceeded):
                raise DeviceError(
                    "Screenshot deadline exhausted. Observe again with a sufficient timeout.",
                    code="action_budget_exceeded",
                ) from exc
            raise

    def ocr(
        self, alias: str, *, backend: str = "auto", max_width: int = 1280, timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not self.enable_ocr:
            raise DeviceConfigurationError("Local OCR requires explicit --enable-ocr startup.")
        return capture_ocr(
            self._device(alias), backend=backend, max_width=max_width, timeout=timeout,
        )

    def list_actions(self) -> dict[str, Any]:
        return {
            "actions": [
                self.actions.describe(action_id)
                for action_id in sorted(self.actions.allowed_actions)
            ]
        }

    def sms_read(
        self, alias: str, *, limit: int = 10, sender: str | None = None,
        wait_seconds: float = 0, timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not self.enable_sms:
            raise DeviceConfigurationError("SMS reading requires explicit --enable-sms startup.")
        from moyu_harmony_pilot.actions.sms import read_sms, validate_sms_options

        validate_sms_options(limit=limit, sender=sender, wait_seconds=wait_seconds, timeout=timeout)

        try:
            # Private message text belongs only in the requested result.
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                return read_sms(
                    self._device(alias), limit=limit, sender=sender, wait_seconds=wait_seconds,
                    timeout=timeout,
                )
        except (DeviceError, HdcError, UiError) as exc:
            raise type(exc)(
                "SMS observation stopped.", code=exc.code, retryable=exc.retryable,
                hint=SMS_ERROR_HINTS.get(exc.code, type(exc).default_hint),
            ) from None

    def record_start(
        self,
        alias: str,
        *,
        fps: float = DEFAULT_RECORD_FPS,
        max_duration: float = DEFAULT_RECORD_MAX_DURATION,
        max_width: int = DEFAULT_RECORD_MAX_WIDTH,
        display_id: int | None = None,
    ) -> dict[str, Any]:
        if not self.enable_recording:
            raise DeviceConfigurationError(
                "Screen recording requires explicit --enable-recording startup."
            )
        validate_recording_options(
            fps=fps, max_duration=max_duration, max_width=max_width, display_id=display_id
        )
        require_ffmpeg()
        device = self._device(alias)
        output_path = device.diagnostics.allocate_path(f"mcp_record_{alias}", suffix=".mp4")
        manager = RecordingSessionManager(self.state_dir)
        return manager.start(
            device,
            output_path,
            fps=fps,
            max_duration=max_duration,
            max_width=max_width,
            display_id=display_id,
        ).to_dict()

    def record_status(self, alias: str) -> dict[str, Any]:
        if not self.enable_recording:
            raise DeviceConfigurationError(
                "Screen recording requires explicit --enable-recording startup."
            )
        device = self._device(alias)
        manager = RecordingSessionManager(self.state_dir)
        info = manager.status(device)
        if info is None:
            return {"alias": alias, "active": False}
        return info.to_dict()

    def record_stop(self, alias: str, *, timeout: float = 45.0) -> dict[str, Any]:
        if not self.enable_recording:
            raise DeviceConfigurationError(
                "Screen recording requires explicit --enable-recording startup."
            )
        device = self._device(alias)
        manager = RecordingSessionManager(self.state_dir)
        result = manager.stop(device, timeout=timeout)
        device.diagnostics.prune()
        return result.to_dict()

    def run_action(
        self,
        alias: str,
        action_id: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
        record: bool = False,
        record_on_failure: bool = False,
    ) -> dict[str, Any]:
        if type(record) is not bool or type(record_on_failure) is not bool:
            raise DeviceConfigurationError("record and record_on_failure must be boolean.")
        want_record = record or record_on_failure
        if want_record and not self.enable_recording:
            raise DeviceConfigurationError(
                "Screen recording requires explicit --enable-recording startup."
            )
        _, _, _, prepared_seconds = self.runner.prepare(
            action_id, params, remote=True, idempotency_key=idempotency_key, timeout=timeout
        )
        if want_record:
            require_ffmpeg()
        device = self._device(alias)

        recorder: ScreenRecorder | None = None
        recording_info: dict[str, Any] | None = None
        if want_record:
            record_dest = device.diagnostics.allocate_path(f"mcp_action_{action_id}", suffix=".mp4")
            recorder = ScreenRecorder(
                device,
                record_dest,
                fps=DEFAULT_RECORD_FPS,
                max_duration=min(max(prepared_seconds + 30.0, 10.0), 600.0),
                max_width=DEFAULT_RECORD_MAX_WIDTH,
            )
            try:
                recorder.start(capture_initial_frame=True)
            except Exception as exc:
                recorder = None
                recording_info = {
                    "saved": False,
                    "error_code": exc.code if isinstance(exc, DeviceError) else "recording_failed",
                    "message": str(exc),
                }

        try:
            outcome = self.runner.run(
                device, action_id, params, remote=True, idempotency_key=idempotency_key,
                timeout=timeout,
            )
        finally:
            if recorder is not None:
                try:
                    rec_result = recorder.stop(
                        capture_final_frame=True, stopped_reason="action_completed"
                    )
                    recording_info = {"saved": True, **rec_result.to_dict()}
                except Exception as exc:
                    recording_info = {
                        "saved": False,
                        "error_code": (
                            exc.code if isinstance(exc, DeviceError) else "recording_failed"
                        ),
                        "message": str(exc),
                    }

        if (
            recording_info is not None
            and recording_info.get("saved") is True
            and record_on_failure
            and outcome.status in {"succeeded", "already_done"}
        ):
            Path(str(recording_info["path"])).unlink(missing_ok=True)
            recording_info = {
                "saved": False,
                "discarded_on_success": True,
                "path": None,
                "frame_count": recording_info["frame_count"],
                "duration_seconds": recording_info["duration_seconds"],
            }
        elif recording_info is not None and recording_info.get("saved") is True:
            device.diagnostics.prune()

        payload: dict[str, Any] = {"alias": alias, "action": action_id, **outcome.to_dict()}
        if recording_info is not None:
            payload["recording"] = recording_info
        return payload

    def _session(self, alias: str) -> InteractionSession:
        if not self.local_lab:
            raise DeviceConfigurationError(
                "Interactive tools require explicit --local-lab startup."
            )
        if alias not in self._sessions:
            self._sessions[alias] = InteractionSession(
                self._device(alias),
                max_steps=self.max_steps,
                timeout=self.timeout,
            )
        return self._sessions[alias]

    def lab_observe(self, alias: str) -> dict[str, Any]:
        return {"alias": alias, "snapshot": self._session(alias).observe().to_dict()}

    def click_ref(self, alias: str, ref: str, layout_hash: str) -> dict[str, Any]:
        return self._session(alias).click_ref(ref, layout_hash=layout_hash)

    def swipe(self, alias: str, direction: str, layout_hash: str) -> dict[str, Any]:
        return self._session(alias).swipe(direction, layout_hash=layout_hash)

    def back(self, alias: str, layout_hash: str) -> dict[str, Any]:
        return self._session(alias).back(layout_hash=layout_hash)

    def home(self, alias: str, layout_hash: str) -> dict[str, Any]:
        return self._session(alias).home(layout_hash=layout_hash)

    def long_click_ref(self, alias: str, ref: str, layout_hash: str) -> dict[str, Any]:
        return self._session(alias).long_click_ref(ref, layout_hash=layout_hash)

    def input_text_ref(
        self, alias: str, ref: str, text: str, layout_hash: str,
    ) -> dict[str, Any]:
        return self._session(alias).input_text_ref(ref, text, layout_hash=layout_hash)

    def start_app(
        self, alias: str, bundle_name: str, layout_hash: str, ability_name: str | None = None
    ) -> dict[str, Any]:
        return self._session(alias).start_app(
            bundle_name, ability_name=ability_name, layout_hash=layout_hash
        )

    def wait_for(self, alias: str, selector: dict[str, Any]) -> dict[str, Any]:
        from moyu_harmony_pilot.ui.selector import selector_from_dict

        node = self._session(alias).wait_for(selector_from_dict(selector))
        return {
            "id": node.id, "type": node.type, "text": node.text, "bounds": node.bounds.to_list(),
        }

    def scroll_until(
        self, alias: str, selector: dict[str, Any], direction: str = "up", max_swipes: int = 10
    ) -> dict[str, Any]:
        from moyu_harmony_pilot.ui.selector import selector_from_dict

        node = self._session(alias).scroll_until(
            selector_from_dict(selector), direction=direction, max_swipes=max_swipes
        )
        return {
            "id": node.id, "type": node.type, "text": node.text, "bounds": node.bounds.to_list(),
        }
