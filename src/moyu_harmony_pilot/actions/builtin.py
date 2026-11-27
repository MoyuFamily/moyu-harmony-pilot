"""Generic read-only system actions with explicitly declared receipt fields."""

from __future__ import annotations

import json
from typing import Any

from moyu_harmony_pilot.actions.apps import AppInstall, AppUninstall
from moyu_harmony_pilot.actions.files import FilesExport, FilesQuery
from moyu_harmony_pilot.actions.models import ActionContext, ActionOutcome, ActionPlugin, ActionSpec
from moyu_harmony_pilot.actions.recording import RecordingStart, RecordingStatus, RecordingStop
from moyu_harmony_pilot.actions.sms import SmsOpen, SmsRead


def metric_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


class DevicePing(ActionPlugin):
    spec = ActionSpec(
        "device.ping",
        "1.0",
        "Verify the official uitest service responds.",
        diagnostic=True,
        metrics_schema=metric_schema({"responsive": {"type": "boolean"}}),
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        responsive = bool(ctx.device.ui.uitest_version(timeout=ctx.remaining_timeout))
        return ActionOutcome(
            status="succeeded" if responsive else "failed",
            summary="Device response checked.",
            metrics={"responsive": responsive},
        )


class DeviceHealth(ActionPlugin):
    spec = ActionSpec(
        "device.health",
        "1.0",
        "Read battery, thermal, charging and screen telemetry.",
        diagnostic=True,
        metrics_schema=metric_schema(
            {
                "battery_percent": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                "temperature_c": {"type": ["number", "null"]},
                "charging": {"type": ["boolean", "null"]},
                "screen_state": {"type": "string", "enum": ["on", "off", "unknown"]},
                "overheated": {"type": "boolean"},
            }
        ),
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        health = ctx.device.get_health(timeout=ctx.remaining_timeout)
        return ActionOutcome(
            summary="Health telemetry collected.",
            metrics={
                "battery_percent": health.battery.battery_percent,
                "temperature_c": health.battery.temperature_c,
                "charging": health.battery.charging,
                "screen_state": health.power.screen_state.value,
                "overheated": health.overheated,
            },
        )


class UiInspect(ActionPlugin):
    spec = ActionSpec(
        "ui.inspect",
        "1.0",
        "Save a compact UI snapshot to local diagnostics.",
        local_only=True,
        diagnostic=True,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"max_elements": {"type": "integer", "minimum": 1, "maximum": 120}},
        },
        metrics_schema=metric_schema(
            {
                "layout_hash": {"type": "string", "maxLength": 128},
                "kept_elements": {"type": "integer"},
                "sensitive": {"type": "boolean"},
            }
        ),
        examples=({}, {"max_elements": 40}),
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        _, snapshot = ctx.device.dump_ui_compact(
            max_elements=params.get("max_elements", 120),
            timeout=ctx.remaining_timeout,
        )
        ctx.device.diagnostics.save_text(
            "ui-inspect",
            json.dumps(snapshot.to_dict(), ensure_ascii=False),
        )
        return ActionOutcome(
            summary="Compact snapshot saved locally.",
            metrics={
                "layout_hash": snapshot.layout_hash,
                "kept_elements": snapshot.kept_elements,
                "sensitive": bool(snapshot.sensitive_signals),
            },
            has_local_diagnostics=True,
        )


def builtin_plugins() -> tuple[ActionPlugin, ...]:
    return (
        DevicePing(), DeviceHealth(), UiInspect(), AppInstall(), AppUninstall(), SmsOpen(),
        SmsRead(), FilesQuery(), FilesExport(),
        RecordingStart(), RecordingStatus(), RecordingStop(),
    )
