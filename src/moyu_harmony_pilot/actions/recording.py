"""Local recording controls delegated to the persistent device session manager."""

from __future__ import annotations

from typing import Any

from moyu_harmony_pilot.actions.models import (
    ActionContext,
    ActionOutcome,
    ActionPlugin,
    ActionSpec,
    empty_schema,
)
from moyu_harmony_pilot.device.recording import (
    DEFAULT_RECORD_FPS,
    DEFAULT_RECORD_MAX_DURATION,
    DEFAULT_RECORD_MAX_WIDTH,
    MAX_RECORD_DURATION,
    MAX_RECORD_FPS,
    MAX_RECORD_WIDTH,
    MIN_RECORD_DURATION,
    MIN_RECORD_FPS,
    RecordingSessionManager,
)

RECORDING_START_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["out"],
    "properties": {
        "out": {
            "type": "string",
            "description": "Destination MP4 filename on the host.",
        },
        "fps": {
            "type": "number", "minimum": MIN_RECORD_FPS, "maximum": MAX_RECORD_FPS,
            "description": f"Target capture frame rate. Defaults to {DEFAULT_RECORD_FPS}.",
        },
        "max_duration": {
            "type": "number", "minimum": MIN_RECORD_DURATION, "maximum": MAX_RECORD_DURATION,
            "description": (
                f"Maximum capture duration in seconds. Defaults to {DEFAULT_RECORD_MAX_DURATION}."
            ),
        },
        "max_width": {
            "type": "integer", "minimum": 1, "maximum": MAX_RECORD_WIDTH,
            "description": (
                f"Maximum video width in pixels. Defaults to {DEFAULT_RECORD_MAX_WIDTH}."
            ),
        },
        "display_id": {"type": "integer", "minimum": 0},
    },
}

RECORDING_START_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "session_id", "alias", "pid", "path", "started_at", "max_duration", "fps",
        "max_width", "display_id", "active",
    ],
    "properties": {
        "session_id": {"type": "string"},
        "alias": {"type": "string"},
        "pid": {"type": "integer", "minimum": 1},
        "path": {"type": "string"},
        "started_at": {"type": "string"},
        "max_duration": {
            "type": "number", "minimum": MIN_RECORD_DURATION, "maximum": MAX_RECORD_DURATION,
        },
        "fps": {"type": "number", "minimum": MIN_RECORD_FPS, "maximum": MAX_RECORD_FPS},
        "max_width": {
            "type": ["integer", "null"], "minimum": 1, "maximum": MAX_RECORD_WIDTH,
        },
        "display_id": {"type": ["integer", "null"], "minimum": 0},
        "active": {"type": "boolean"},
    },
}

RECORDING_STATUS_PARAMS_SCHEMA = empty_schema()
RECORDING_STATUS_RESULT_SCHEMA: dict[str, Any] = {
    **RECORDING_START_RESULT_SCHEMA,
    "required": ["alias", "active"],
}

RECORDING_STOP_PARAMS_SCHEMA = empty_schema()
RECORDING_STOP_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "alias", "path", "format", "frame_count", "duration_seconds", "effective_fps",
        "width", "height", "size_bytes", "stopped_reason",
    ],
    "properties": {
        "alias": {"type": "string"},
        "path": {"type": "string"},
        "format": {"type": "string", "enum": ["mp4"]},
        "frame_count": {"type": "integer", "minimum": 1},
        "duration_seconds": {"type": "number", "minimum": 0},
        "effective_fps": {"type": "number", "minimum": 0},
        "width": {"type": ["integer", "null"], "minimum": 1},
        "height": {"type": ["integer", "null"], "minimum": 1},
        "size_bytes": {"type": "integer", "minimum": 0},
        "stopped_reason": {"type": "string"},
    },
}


class RecordingStart(ActionPlugin):
    spec = ActionSpec(
        "record.start",
        "1.0",
        "Start a detached background screen recording session writing a host MP4.",
        side_effect="readonly",
        local_only=True,
        private_content=True,
        lock_device=False,
        diagnostic=True,
        params_schema=RECORDING_START_PARAMS_SCHEMA,
        result_schema=RECORDING_START_RESULT_SCHEMA,
        min_pilot_version="0.2.1.dev0",
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        manager = RecordingSessionManager(ctx.state_dir)
        info = manager.start(
            ctx.device, params["out"],
            fps=params.get("fps", DEFAULT_RECORD_FPS),
            max_duration=params.get("max_duration", DEFAULT_RECORD_MAX_DURATION),
            max_width=params.get("max_width", DEFAULT_RECORD_MAX_WIDTH),
            display_id=params.get("display_id"),
            startup_timeout=min(15.0, ctx.remaining_timeout),
        )
        return ActionOutcome(summary="Recording session started.", data=info.to_dict())


class RecordingStatus(ActionPlugin):
    spec = ActionSpec(
        "record.status",
        "1.0",
        "Inspect the current or recently finished local recording session.",
        side_effect="readonly",
        local_only=True,
        private_content=True,
        lock_device=False,
        diagnostic=True,
        params_schema=RECORDING_STATUS_PARAMS_SCHEMA,
        result_schema=RECORDING_STATUS_RESULT_SCHEMA,
        min_pilot_version="0.2.1.dev0",
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        info = RecordingSessionManager(ctx.state_dir).status(ctx.device)
        data = info.to_dict() if info is not None else {"alias": ctx.device.alias, "active": False}
        return ActionOutcome(summary="Recording session status inspected.", data=data)


class RecordingStop(ActionPlugin):
    spec = ActionSpec(
        "record.stop",
        "1.0",
        "Stop a recording session and finalize its host MP4 artifact.",
        side_effect="readonly",
        local_only=True,
        private_content=True,
        lock_device=False,
        diagnostic=True,
        timeout_seconds=45.0,
        params_schema=RECORDING_STOP_PARAMS_SCHEMA,
        result_schema=RECORDING_STOP_RESULT_SCHEMA,
        min_pilot_version="0.2.1.dev0",
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        result = RecordingSessionManager(ctx.state_dir).stop(
            ctx.device, timeout=ctx.remaining_timeout,
        )
        return ActionOutcome(summary="Recording saved to the host.", data=result.to_dict())
