"""Read-only external plugin example using the public driver and action APIs."""

from moyu_harmony_pilot.actions import ActionContext, ActionOutcome, ActionPlugin, ActionSpec


class UiSummary(ActionPlugin):
    spec = ActionSpec(
        action_id="example.ui_summary",
        version="1.0",
        description="Count visible semantic elements in the current screen.",
        local_only=True,
        metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "visible_elements": {"type": "integer", "minimum": 0},
                "sensitive_screen": {"type": "boolean"},
            },
        },
        examples=({},),
    )

    def execute(self, ctx: ActionContext, params: dict) -> ActionOutcome:
        ctx.checkpoint()
        _, snapshot = ctx.device.dump_ui_compact(timeout=ctx.remaining_timeout)
        return ActionOutcome(
            summary="Screen structure inspected.",
            metrics={
                "visible_elements": snapshot.kept_elements,
                "sensitive_screen": bool(snapshot.sensitive_signals),
            },
        )
