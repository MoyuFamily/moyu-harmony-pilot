"""Read-only external action for verifying a remote Bridge integration."""

from moyu_harmony_pilot.actions import ActionOutcome, ActionPlugin, ActionSpec


class HealthCheck(ActionPlugin):
    spec = ActionSpec(
        action_id="example.health_check",
        version="1.0",
        description="Check device battery against a caller-provided minimum.",
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
                "battery_percent": {"type": ["integer", "null"]},
                "ready": {"type": "boolean"},
            },
        },
        timeout_seconds=30,
        examples=({"minimum_battery": 20},),
    )

    def execute(self, ctx, params):
        ctx.checkpoint()
        health = ctx.device.get_health(timeout=ctx.remaining_timeout)
        battery = health.battery.battery_percent
        ready = battery is not None and battery >= params["minimum_battery"]
        return ActionOutcome(
            status="succeeded" if ready else "failed",
            summary="Device health check completed.",
            metrics={"battery_percent": battery, "ready": ready},
            error_code=None if ready else "battery_requirement_unmet",
        )
