"""Idempotent toggle for a synthetic demo UI using context-bound interactions."""

from typing import Any

from moyu_harmony_pilot.actions import (
    ActionContext,
    ActionOutcome,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.device import DeviceConfigurationError, HumanRequiredError
from moyu_harmony_pilot.ui import CompactElement, CompactSnapshot

DEMO_BUNDLE = "com.example.pilotdemo"
TOGGLE_ID = "demo_toggle"


class DemoToggle(IdempotentAction):
    spec = ActionSpec(
        action_id="example.demo_toggle",
        version="1.0",
        api_version=1,
        min_pilot_version="0.2.0rc1",
        description="Set and verify the checked state of the synthetic demo toggle.",
        side_effect="mutating",
        timeout_seconds=30,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled"],
            "properties": {"enabled": {"type": "boolean"}},
        },
        inspection_metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["current_enabled"],
            "properties": {"current_enabled": {"type": "boolean"}},
        },
        metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled"],
            "properties": {"enabled": {"type": "boolean"}},
        },
        examples=({"enabled": True}, {"enabled": False}),
    )

    @staticmethod
    def _observe(ctx: ActionContext) -> tuple[CompactSnapshot, CompactElement]:
        snapshot = ctx.interact().observe()
        if snapshot.sensitive_signals:
            raise HumanRequiredError("Sensitive screen requires human intervention.")
        matches = [
            element for element in snapshot.elements
            if element.bundle_name == DEMO_BUNDLE and element.id == TOGGLE_ID
            and element.type == "Toggle"
        ]
        if snapshot.truncated or len(matches) != 1:
            raise DeviceConfigurationError("Expected one unambiguous synthetic demo toggle.")
        element = matches[0]
        if not element.enabled or not element.clickable:
            raise DeviceConfigurationError("The synthetic demo toggle must be enabled.")
        return snapshot, element

    def inspect(self, ctx: ActionContext, params: dict[str, Any]) -> Inspection:
        snapshot, element = self._observe(ctx)
        if element.checked == params["enabled"]:
            return Inspection(status="already_done", metrics={"enabled": element.checked})
        return Inspection(
            state=(element.ref, snapshot.layout_hash),
            metrics={"current_enabled": element.checked},
        )

    def submit(
        self, ctx: ActionContext, inspection: Inspection, params: dict[str, Any],
    ) -> None:
        ref, layout_hash = inspection.state
        ctx.interact().click_ref(ref, layout_hash=layout_hash)

    def confirm(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        _, element = self._observe(ctx)
        confirmed = element.checked == params["enabled"]
        return ActionOutcome(
            status="succeeded" if confirmed else "failed",
            summary="Demo toggle state observed.",
            metrics={"enabled": element.checked},
            error_code=None if confirmed else "demo_state_unconfirmed",
        )
