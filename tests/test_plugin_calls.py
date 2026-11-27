"""Offline external-to-internal plugin composition and authorization contracts."""

import json
import signal
from dataclasses import replace
from textwrap import dedent
from unittest.mock import Mock

import pytest

from moyu_harmony_pilot.actions import (
    ActionLedger,
    ActionPlugin,
    ActionRegistry,
    ActionRunner,
    ActionSpec,
)
from moyu_harmony_pilot.actions.sms import SmsOpen
from moyu_harmony_pilot.device import DeviceRegistry
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.server import create_server
from moyu_harmony_pilot.testing import FakeClickTarget, make_fake_device, offline_guard
from tests.test_actions import SampleMutation
from tests.test_mcp import invoke
from tests.test_sms import SMS_BUNDLE, fake_sms, sms_layout


def test_external_plugin_opens_and_reads_sms_with_shared_context(tmp_path, monkeypatch, capsys):
    body = "合成短信" * 900 + "验证码314159"
    state_dir = tmp_path / "state"
    fake = fake_sms(
        state_dir, sms_layout(("合成发送方", body, "今天"), thread=True),
        sms_layout(("合成发送方", body, "今天")),
    )
    fake.state.click_targets = [FakeClickTarget("back_button", SMS_BUNDLE)]
    plugin_dir = tmp_path / "external"
    plugin_dir.mkdir()
    (plugin_dir / "actions.json").write_text(json.dumps({
        "example.sms_review": "review.py:Review",
    }))
    (plugin_dir / "review.py").write_text(dedent("""
        import signal
        from moyu_harmony_pilot.actions import (
            ActionSpec, ActionOutcome, IdempotentAction, Inspection,
        )

        class Review(IdempotentAction):
            spec = ActionSpec(
                'example.sms_review', '1.0', 'Inspect synthetic messages.',
                side_effect='mutating', min_pilot_version='0.2.1.dev0',
                metrics_schema={
                    'type': 'object', 'additionalProperties': False,
                    'properties': {'count': {'type': 'integer'}},
                },
            )

            def inspect(self, ctx, params):
                return Inspection()

            def prepare(self, ctx, inspection, params):
                self.timer = signal.getsignal(signal.SIGALRM)
                self.context = ctx
                self.before_steps = ctx.steps
                ctx.call('sms.open', {}, idempotency_key='review-sms-open')
                self.messages = ctx.call('sms.read', {'limit': 1})
                self.same_timer = self.timer is signal.getsignal(signal.SIGALRM)
                return Inspection(state=self.messages.data['count'])

            def submit(self, ctx, inspection, params):
                self.count = inspection.state

            def confirm(self, ctx, params):
                return ActionOutcome(metrics={'count': self.count})
    """), encoding="utf-8")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    registry = ActionRegistry.discover(
        env={}, plugin_paths=[plugin_dir], remote=True,
        allowed_actions=["example.sms_review", "sms.open", "sms.read"],
    )
    facade = PilotTools(
        HdcClient(transport=fake.transport), DeviceRegistry([fake.device.profile]), registry,
        state_dir=state_dir, enable_sms=True,
    )
    monkeypatch.setattr(facade, "_device", lambda alias: fake.device)
    lock = Mock(wraps=fake.device.acquire_lock)
    monkeypatch.setattr(fake.device, "acquire_lock", lock)
    arguments = {
        "alias": "phone", "action_id": "example.sms_review", "params": {},
        "idempotency_key": "review-request",
    }
    server = create_server(facade)
    with offline_guard():
        result = invoke(server, "run_action", arguments)
    assert result["ok"] and result["data"]["status"] == "succeeded", result
    assert result["data"]["metrics"] == {"count": 1}
    assert lock.call_count == 1
    plugin, _ = registry.get("example.sms_review")
    assert plugin.messages.data["messages"][0]["body"] == body
    assert plugin.messages.to_dict(include_data=True)["data"]["count"] == 1
    assert "data" not in plugin.messages.to_dict()
    assert plugin.same_timer and plugin.context.steps > plugin.before_steps
    assert plugin.context.stage is None
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert registry.describe("example.sms_review")["source"] == "external"
    assert registry.describe("sms.read")["source"] == "internal"
    fake.transport.assert_click_targets_consumed()
    before = list(fake.transport.calls)
    with offline_guard():
        repeated = invoke(server, "run_action", arguments)
    assert repeated["data"]["status"] == "already_done"
    assert fake.transport.calls == before
    records = ActionLedger(state_dir / "actions.sqlite3").records()
    assert {row["action_id"] for row in records} == {"sms.open", "example.sms_review"}
    assert "314159" not in json.dumps(records) + json.dumps(result)
    captured = capsys.readouterr()
    assert "314159" not in captured.out + captured.err
    assert not fake.device.is_busy() and not fake.transport.all_remote_tmp_files()


@pytest.mark.parametrize(
    "boundary,child_id,remote",
    [
        pytest.param("readonly-stage", "sms.open", False, id="readonly-cannot-navigate"),
        pytest.param("device-allowlist", "device.health", True, id="device-specific-allowlist"),
        pytest.param("private-startup", "sms.read", True, id="private-startup-required"),
    ],
)
def test_plugin_call_rejection_precedes_child_io(
    tmp_path, monkeypatch, boundary, child_id, remote,
):
    fake = make_fake_device(tmp_path)

    class Caller(ActionPlugin):
        spec = ActionSpec(
            "example.caller", "1.0", "Call a guarded child.", diagnostic=True,
            min_pilot_version="0.2.1.dev0",
        )

        def execute(self, ctx, params):
            ctx.call(child_id, {}, idempotency_key="guarded-child")
            raise AssertionError("Rejected child call returned successfully.")

    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    registry = ActionRegistry.discover(env={}, allowed_actions=["example.caller", child_id])
    registry.register(Caller())
    allowed = ["example.caller"] if boundary == "device-allowlist" else None
    with offline_guard():
        outcome = ActionRunner(registry, state_dir=tmp_path).run(
            fake.device, "example.caller", {}, remote=remote, allowed_actions=allowed,
        )
    assert outcome.status == "configuration_error"
    assert fake.transport.calls == []
    assert not (tmp_path / "actions.sqlite3").exists()
    assert not fake.device.is_busy()


def test_unknown_child_blocks_parent_submission_and_replay(tmp_path, monkeypatch):
    fake = fake_sms(tmp_path, sms_layout(("示例", "hello", "今天"), thread=True))
    fake.state.timeout_after_mutation = True
    child = SmsOpen()

    class Parent(SampleMutation):
        spec = replace(
            SampleMutation.spec, action_id="example.parent", min_pilot_version="0.2.1.dev0",
        )

        def prepare(self, ctx, inspection, params):
            ctx.call(child.spec.action_id, {}, idempotency_key="stable-child-key")
            return inspection

    parent = Parent()
    submit = child.submit

    def checked_submit(ctx, inspection, params):
        reservations = ActionLedger(tmp_path / "actions.sqlite3").records()
        assert {row["action_id"] for row in reservations} == {
            child.spec.action_id, parent.spec.action_id,
        }
        assert all(row["status"] == "submitting" for row in reservations)
        submit(ctx, inspection, params)

    monkeypatch.setattr(child, "submit", checked_submit)
    registry = ActionRegistry()
    registry.register(child)
    registry.register(parent)
    runner = ActionRunner(registry, state_dir=tmp_path)
    with offline_guard():
        first = runner.run(fake.device, parent.spec.action_id, {}, idempotency_key="parent-key")
        mutations = list(fake.transport.mutating_calls)
        calls = list(fake.transport.calls)
        repeated = runner.run(fake.device, parent.spec.action_id, {}, idempotency_key="parent-key")
    assert first.status == repeated.status == "unknown"
    assert mutations and fake.transport.mutating_calls == mutations
    assert fake.transport.calls == calls
    assert "submit" not in parent.calls
    records = ActionLedger(tmp_path / "actions.sqlite3").records()
    assert {row["action_id"] for row in records} == {child.spec.action_id, parent.spec.action_id}
    assert all(row["status"] == "unknown" for row in records)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert not fake.device.is_busy()
