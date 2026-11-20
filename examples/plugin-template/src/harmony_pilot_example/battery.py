"""Read-only action using the public Pilot action and device interfaces."""

from moyu_harmony_pilot.actions import ActionContext, ActionOutcome, ActionPlugin, ActionSpec


class BatteryCheck(ActionPlugin):
    spec = ActionSpec(
        action_id="example.battery_check",
        version="1.0",
        api_version=1,
        description="Check the current battery level against a minimum.",
        side_effect="readonly",
        timeout_seconds=30,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["minimum_battery"],
            "properties": {
                "minimum_battery": {"type": "integer", "minimum": 0, "maximum": 100},
            },
        },
        metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["battery_percent", "ready"],
            "properties": {
                "battery_percent": {
                    "type": ["integer", "null"], "minimum": 0, "maximum": 100,
                },
                "ready": {"type": "boolean"},
            },
        },
        examples=({"minimum_battery": 20},),
    )

    def execute(self, ctx: ActionContext, params: dict) -> ActionOutcome:
        ctx.checkpoint()
        health = ctx.device.get_health(timeout=ctx.remaining_timeout)
        battery = health.battery.battery_percent
        ready = battery is not None and battery >= params["minimum_battery"]
        return ActionOutcome(
            status="succeeded" if ready else "failed",
            summary="Battery check completed.",
            metrics={"battery_percent": battery, "ready": ready},
            error_code=None if ready else "battery_requirement_unmet",
        )
