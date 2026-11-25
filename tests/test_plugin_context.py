"""Phase receipts and context-bound interaction safety contracts."""

import signal
import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from moyu_harmony_pilot.actions import (
    ActionLedger,
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    Inspection,
)
from moyu_harmony_pilot.device import DeviceConfigurationError, DeviceProfile, HarmonyDevice
from moyu_harmony_pilot.hdc import HdcClient
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_actions import SampleMutation
from tests.test_cli import _parse_single_json_envelope, _run_cli


@pytest.fixture
def context_setup(tmp_path):
    state = FakeDeviceState("SYNTHETIC_CONTEXT")
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport), DeviceProfile("demo", state.serial), state_dir=tmp_path
    )
    plugin = SampleMutation()
    plugin.spec = replace(
        plugin.spec,
        min_pilot_version="0.2.0.dev0",
        metrics_schema={**plugin.spec.metrics_schema, "required": ["count"]},
    )
    return plugin, device, transport


def run_context(setup, tmp_path, **kwargs):
    plugin, device, _ = setup
    registry = ActionRegistry()
    registry.register(plugin)
    return ActionRunner(registry, state_dir=tmp_path).run(
        device, plugin.spec.action_id, {}, idempotency_key="example-key", **kwargs
    )


def test_incomplete_inspection_preserves_status_and_cli_exit(
    context_setup, tmp_path, monkeypatch
):
    plugin, _, transport = context_setup
    plugin.inspection = Inspection(
        "human_required", summary="Review this step.", error_code="review_needed"
    )
    registry = ActionRegistry()
    registry.register(plugin)
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    code, output, _ = _run_cli(
        ["run", "--action", plugin.spec.action_id, "--idempotency-key", "example-key", "--json",
         "--state-dir", str(tmp_path)], transport=transport, cwd=tmp_path, env={},
    )
    data = _parse_single_json_envelope(output)["data"]
    assert code == 4
    assert data["status"] == "human_required"
    assert data["summary"] == "Review this step."
    assert data["metrics"] == {}
    assert data["error_code"] == "review_needed"
    assert plugin.calls == ["inspect"]
    assert ActionLedger(tmp_path / "actions.sqlite3").records() == []


def test_ready_inspection_and_final_required_metrics_are_separate(context_setup, tmp_path):
    plugin, _, _ = context_setup
    plugin.spec = replace(
        plugin.spec,
        inspection_metrics_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"before": {"type": "integer"}}, "required": ["before"],
        },
    )
    plugin.inspection = Inspection(metrics={"before": 7})
    assert run_context(context_setup, tmp_path).status == "succeeded"
    record = ActionLedger(tmp_path / "actions.sqlite3").records()[0]
    assert record["inspection_metrics"] == {"before": 7}
    assert plugin.calls == ["inspect", "submit", "confirm"]


def test_final_success_still_requires_declared_metrics(context_setup, tmp_path):
    context_setup[0].outcome = ActionOutcome()
    assert run_context(context_setup, tmp_path).status == "unknown"
    assert run_context(context_setup, tmp_path).status == "unknown"
    assert context_setup[0].calls == ["inspect", "submit", "confirm"]


def test_bound_session_reuses_lock_and_timer_and_expires(context_setup, tmp_path, monkeypatch):
    plugin, device, _ = context_setup
    lock = Mock(wraps=device.acquire_lock)
    monkeypatch.setattr(device, "acquire_lock", lock)
    monkeypatch.setattr(
        "moyu_harmony_pilot.actions.interaction.bounded_call",
        Mock(side_effect=AssertionError("Nested timer")),
    )
    captured = []

    def inspect(ctx, params):
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        session = ctx.interact()
        assert session is ctx.interact()
        captured.append(session)
        assert session.observe().elements
        return Inspection()

    def submit(ctx, inspection, params):
        with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
            assert db.execute("SELECT status FROM actions").fetchone()[0] == "submitting"
        snapshot = ctx.interact().observe()
        ctx.interact().back(layout_hash=snapshot.layout_hash)

    monkeypatch.setattr(plugin, "inspect", inspect)
    monkeypatch.setattr(plugin, "submit", submit)
    assert run_context(context_setup, tmp_path).status == "succeeded"
    assert lock.call_count == 1
    assert captured[0].context.steps > 5
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    with pytest.raises(DeviceConfigurationError, match="no longer executing"):
        captured[0].observe()


@pytest.mark.parametrize(
    "stage,expected",
    [
        pytest.param("inspect", "configuration_error", id="before-reservation"),
        pytest.param("confirm", "unknown", id="after-submission"),
    ],
)
def test_bound_readonly_stages_cannot_mutate(context_setup, tmp_path, monkeypatch, stage, expected):
    plugin, _, transport = context_setup

    def forbidden(ctx, params):
        ctx.interact().back(layout_hash="irrelevant")

    monkeypatch.setattr(plugin, stage, forbidden)
    assert run_context(context_setup, tmp_path).status == expected
    assert not any("uiInput" in " ".join(args) for _, args, _ in transport.calls)


def test_prepare_stage_allows_bound_interaction_and_merges_metrics(context_setup, tmp_path):
    from moyu_harmony_pilot.actions import IdempotentAction

    _, device, _ = context_setup
    seen_metrics = {}

    class InteractivePrepareAction(IdempotentAction):
        spec = replace(
            context_setup[0].spec,
            action_id="test.interactive_prepare",
            inspection_metrics_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"before": {"type": "integer"}, "nav_steps": {"type": "integer"}},
                "required": ["before"],
            },
        )

        def __init__(self):
            self.calls = []

        def inspect(self, ctx, params):
            self.calls.append("inspect")
            ctx.interact().observe()
            return Inspection(status="ready", metrics={"before": 3})

        def prepare(self, ctx, inspection, params):
            self.calls.append("prepare")
            with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
                assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
            snapshot = ctx.interact().observe()
            ctx.interact().back(layout_hash=snapshot.layout_hash)
            return Inspection(status="ready", state="navigated", metrics={"nav_steps": 1})

        def submit(self, ctx, inspection, params):
            self.calls.append(f"submit:{inspection.state}")
            assert ctx.inspection_metrics == {"before": 3, "nav_steps": 1}

        def confirm(self, ctx, params):
            self.calls.append("confirm")
            seen_metrics.update(ctx.inspection_metrics)
            return ActionOutcome(status="succeeded", metrics={"count": 4})

    plugin = InteractivePrepareAction()
    registry = ActionRegistry()
    registry.register(plugin)
    runner = ActionRunner(registry, state_dir=tmp_path)

    # Cancelled during prepare does not create a ledger reservation.
    cancelled_once = runner.run(
        device,
        plugin.spec.action_id,
        {},
        idempotency_key="prep-key",
        cancelled=lambda: "prepare" in plugin.calls,
    )
    assert cancelled_once.error_code == "action_cancelled"
    assert ActionLedger(tmp_path / "actions.sqlite3").records() == []

    # Same key succeeds once cancellation clears, keeping merged inspect + prepare metrics.
    plugin.calls.clear()
    outcome = runner.run(device, plugin.spec.action_id, {}, idempotency_key="prep-key")
    assert outcome.status == "succeeded"
    assert seen_metrics == {"before": 3, "nav_steps": 1}
    assert plugin.calls == ["inspect", "prepare", "submit:navigated", "confirm"]
    record = ActionLedger(tmp_path / "actions.sqlite3").records()[0]
    assert record["inspection_metrics"] == {"before": 3, "nav_steps": 1}
