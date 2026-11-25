"""Offline plugin contracts and crash-safe execution regressions."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from moyu_harmony_pilot.actions import (
    ActionContext,
    ActionLedger,
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.actions.budget import bounded_call
from moyu_harmony_pilot.actions.models import ActionBudgetExceeded
from moyu_harmony_pilot.actions.validation import check_schema
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceProfile,
    HarmonyDevice,
)
from moyu_harmony_pilot.hdc import HdcClient
from tests.fakes import BATTERY_61_OVERHEATED, FakeDeviceState, FakeHdcTransport


@pytest.fixture(autouse=True)
def isolated_plugin_environment(monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])


class SampleMutation(IdempotentAction):
    spec = ActionSpec(
        "example.mutate",
        "1.0",
        "Synthetic offline mutation.",
        side_effect="mutating",
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "integer"}},
        },
        metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"count": {"type": "integer"}},
        },
    )

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inspection = Inspection()
        self.outcome = ActionOutcome(metrics={"count": 1})
        self.submit_error: BaseException | None = None
        self.confirm_error: BaseException | None = None
        self.submit_hook = None

    def inspect(self, ctx, params):
        self.calls.append("inspect")
        return self.inspection

    def submit(self, ctx, inspection, params):
        self.calls.append("submit")
        if self.submit_hook:
            self.submit_hook()
        if self.submit_error:
            raise self.submit_error

    def confirm(self, ctx, params):
        self.calls.append("confirm")
        if self.confirm_error:
            raise self.confirm_error
        return self.outcome


@pytest.fixture
def execution(tmp_path):
    state = FakeDeviceState("OFFLINE_TEST_DEVICE")
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport),
        DeviceProfile("test-phone", state.serial),
        state_dir=tmp_path,
    )
    plugin = SampleMutation()
    registry = ActionRegistry()
    registry.register(plugin)
    return ActionRunner(registry, state_dir=tmp_path), device, plugin, transport, state


def run_mutation(execution, **kwargs):
    runner, device, plugin, _, _ = execution
    return runner.run(
        device,
        plugin.spec.action_id,
        kwargs.pop("params", {}),
        idempotency_key="test-key",
        **kwargs,
    )


def read_status(path):
    with sqlite3.connect(path / "actions.sqlite3") as db:
        return db.execute("SELECT status FROM actions").fetchone()[0]


def test_commit_precedes_submit_and_success_short_circuits(execution, tmp_path):
    runner, device, plugin, transport, state = execution

    def inspect_reservation():
        assert read_status(tmp_path) == "submitting"
        assert device.is_busy()

    plugin.submit_hook = inspect_reservation
    assert run_mutation(execution).status == "succeeded"
    assert plugin.calls == ["inspect", "submit", "confirm"]
    before = len(transport.calls)
    assert run_mutation(execution).status == "already_done"
    assert len(transport.calls) == before
    assert read_status(tmp_path) == "succeeded"
    assert state.serial.encode() not in (tmp_path / "actions.sqlite3").read_bytes()
    assert b"test-key" not in (tmp_path / "actions.sqlite3").read_bytes()
    assert (tmp_path / "actions.sqlite3").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert not device.is_busy()
    assert not transport.all_remote_tmp_files()
    # Two aliases for the same physical device share the durable deduplication identity.
    alternate = HarmonyDevice(
        HdcClient(transport=transport),
        DeviceProfile("other-alias", state.serial),
        state_dir=tmp_path,
    )
    assert (
        runner.run(alternate, plugin.spec.action_id, {}, idempotency_key="test-key").status
        == "already_done"
    )


@pytest.mark.parametrize(
    "stage,error",
    [
        pytest.param("submit", RuntimeError("private token"), id="submit-exception"),
        pytest.param("confirm", KeyboardInterrupt(), id="confirm-interrupted"),
    ],
)
def test_all_post_submit_errors_are_unknown_without_replay(execution, tmp_path, error, stage):
    plugin = execution[2]
    setattr(plugin, f"{stage}_error", error)
    result = run_mutation(execution)
    assert result.status == "unknown"
    assert "private" not in json.dumps(result.to_dict())
    assert read_status(tmp_path) == "unknown"
    calls = list(plugin.calls)
    assert run_mutation(execution).status == "unknown"
    assert plugin.calls == calls
    assert not execution[1].is_busy()


def test_restart_recovers_submitting_and_changed_requests_fail_closed(execution, tmp_path):
    runner, device, plugin, transport, _ = execution
    ledger = ActionLedger(tmp_path / "actions.sqlite3")
    key = ledger.make_key(device.profile.serial, plugin.spec.action_id, "test-key", "1.0", {})
    ledger.reserve(key)
    assert run_mutation(execution).status == "unknown"
    assert read_status(tmp_path) == "unknown"
    assert plugin.calls == []
    assert transport.calls == []
    assert run_mutation(execution, params={"value": 2}).status == "configuration_error"
    assert read_status(tmp_path) == "unknown"


def test_changed_plugin_version_never_resubmits(execution):
    assert run_mutation(execution).status == "succeeded"
    runner, _, plugin, _, _ = execution
    plugin.spec = replace(plugin.spec, version="2.0")
    registry = ActionRegistry()
    registry.register(plugin)
    runner.registry = registry
    assert run_mutation(execution).status == "configuration_error"
    assert plugin.calls.count("submit") == 1


def test_durable_finalization_failure_returns_unknown(execution, monkeypatch, tmp_path):
    monkeypatch.setattr(ActionLedger, "finish", Mock(side_effect=OSError("private path")))
    assert run_mutation(execution).status == "unknown"
    assert read_status(tmp_path) == "submitting"
    assert run_mutation(execution).status == "unknown"
    assert read_status(tmp_path) == "unknown"
    assert execution[2].calls.count("submit") == 1


def test_interruption_immediately_after_reservation_commit(execution, monkeypatch, tmp_path):
    reserve = ActionLedger.reserve

    def interrupted_reserve(self, key, **kwargs):
        reserve(self, key, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(ActionLedger, "reserve", interrupted_reserve)
    assert run_mutation(execution).status == "unknown"
    assert read_status(tmp_path) == "unknown"
    assert execution[2].calls == ["inspect"]
    assert run_mutation(execution).status == "unknown"


def test_cancellation_after_inspection_stops_before_reservation(execution, monkeypatch, tmp_path):
    cancelled = threading.Event()
    inspect = execution[2].inspect

    def cancel_after_inspect(ctx, params):
        result = inspect(ctx, params)
        cancelled.set()
        return result

    monkeypatch.setattr(execution[2], "inspect", cancel_after_inspect)
    result = run_mutation(execution, cancelled=cancelled.is_set)
    assert result.error_code == "action_cancelled"
    assert execution[2].calls == ["inspect"]
    with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_cancellation_after_reservation_finishes_confirmation(
    execution, monkeypatch, tmp_path
):
    cancelled = threading.Event()
    reserve = ActionLedger.reserve

    def cancel_after_commit(self, key, **kwargs):
        reserve(self, key, **kwargs)
        cancelled.set()

    monkeypatch.setattr(ActionLedger, "reserve", cancel_after_commit)

    plugin = execution[2]
    confirm = plugin.confirm

    def confirm_with_checkpoints(ctx, params):
        ctx.checkpoint()
        assert ctx.remaining_timeout > 0
        return confirm(ctx, params)

    monkeypatch.setattr(plugin, "confirm", confirm_with_checkpoints)
    plugin.submit_hook = lambda: time.sleep(0.15)
    assert run_mutation(execution, cancelled=cancelled.is_set).status == "succeeded"
    assert cancelled.is_set()
    assert plugin.calls == ["inspect", "submit", "confirm"]
    assert read_status(tmp_path) == "succeeded"
    assert run_mutation(execution).status == "already_done"
    assert plugin.calls.count("submit") == 1


def test_process_death_during_submit_recovers_without_replay(execution, tmp_path):
    script = """
import os
import sys
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_actions import SampleMutation
from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner
from moyu_harmony_pilot.device import DeviceProfile, HarmonyDevice
from moyu_harmony_pilot.hdc import HdcClient
state = FakeDeviceState("OFFLINE_TEST_DEVICE")
device = HarmonyDevice(HdcClient(transport=FakeHdcTransport([state])),
                       DeviceProfile("test-phone", state.serial), state_dir=sys.argv[1])
plugin = SampleMutation()
plugin.submit_hook = lambda: os._exit(23)
registry = ActionRegistry()
registry.register(plugin)
ActionRunner(registry, state_dir=sys.argv[1]).run(
    device, plugin.spec.action_id, {}, idempotency_key="test-key")
"""
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 23, process.stderr.decode()
    assert read_status(tmp_path) == "submitting"
    assert run_mutation(execution).status == "unknown"
    assert read_status(tmp_path) == "unknown"
    assert execution[2].calls == []
    assert not execution[1].is_busy()


def test_thermal_and_sensitive_screen_guards(execution):
    runner, device, plugin, _, state = execution
    state.battery_dump = BATTERY_61_OVERHEATED
    assert run_mutation(execution).status == "device_overheated"
    assert plugin.calls == []
    state.battery_dump = "capacity: 80\ntemperature: 300"
    layout = json.loads(state.layouts[0])
    layout["attributes"]["text"] = "验证码 captcha"
    state.layouts = [json.dumps(layout)]
    assert run_mutation(execution).status == "human_required"
    assert plugin.calls == ["inspect"]
    assert not (runner.state_dir / "diagnostics").exists()
    with device.acquire_lock():
        with pytest.raises(DeviceBusyError):
            run_mutation(execution)


def test_main_thread_timeout_preempts_plugin_sleep_and_restores_timer(execution):
    before_handler = signal.getsignal(signal.SIGALRM)
    execution[2].submit_hook = lambda: time.sleep(2)
    started = time.monotonic()
    assert run_mutation(execution, timeout=0.2).status == "unknown"
    assert time.monotonic() - started < 1.5
    assert signal.getsignal(signal.SIGALRM) == before_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert not execution[1].is_busy()
    assert run_mutation(execution).status == "unknown"


def test_deadline_interrupts_once_and_allows_bounded_cleanup(execution):
    cleaned = []

    def operation():
        try:
            time.sleep(1)
        finally:
            time.sleep(0.12)
            cleaned.append(True)

    context = ActionContext(execution[1], time.monotonic() + 0.04)
    with pytest.raises(ActionBudgetExceeded):
        bounded_call(context, operation)
    assert cleaned == [True]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_ledger_atomic_reservation_has_one_winner(tmp_path):
    ledger = ActionLedger(tmp_path / "actions.sqlite3")
    key = ledger.make_key("test-serial", "example.mutate", "key", "1.0", {})

    def reserve_once():
        try:
            ledger.reserve(key)
            return True
        except DeviceConfigurationError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: reserve_once(), range(8))) == 1
    assert read_status(tmp_path) == "submitting"


def test_registry_metadata_snapshot_duplicate_and_remote_allowlist(execution):
    runner, device, plugin, transport, _ = execution
    with pytest.raises(DeviceConfigurationError):
        runner.registry.register(plugin)
    with pytest.raises(DeviceConfigurationError):
        runner.run(device, plugin.spec.action_id, {}, idempotency_key="key", remote=True)
    assert transport.calls == []
    registry = ActionRegistry.discover(env={}, allowed_actions=["device.health", "ui.inspect"])
    assert registry.describe("device.health")["remote_allowed"]
    assert not registry.describe("ui.inspect")["remote_allowed"]
    with pytest.raises(DeviceConfigurationError):
        registry.get("ui.inspect", remote=True)
    description = runner.registry.describe(plugin.spec.action_id)
    description["params_schema"]["properties"]["bad"] = {"type": "string"}
    assert (
        "bad" not in runner.registry.describe(plugin.spec.action_id)["params_schema"]["properties"]
    )


def test_unsupported_action_api_is_rejected_before_registration(execution):
    plugin = execution[2]
    plugin.spec = replace(plugin.spec, api_version=2)
    registry = ActionRegistry()
    with pytest.raises(DeviceConfigurationError, match="Unsupported action API"):
        registry.register(plugin)
    assert registry.list() == []
    assert plugin.calls == []
    assert execution[3].calls == []


def test_unsupported_or_malformed_schema_fails_closed():
    with pytest.raises(DeviceConfigurationError):
        check_schema({"type": "object", "additionalProperties": True})


def test_external_entry_points_filter_before_import(monkeypatch):
    entry = Mock(name="entry")
    entry.name = "example.mutate"
    entry.value = "test:SampleMutation"
    entry.load.return_value = SampleMutation
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry])
    registry = ActionRegistry.discover(env={}, remote=True)
    entry.load.assert_not_called()
    assert {item["action_id"] for item in registry.list()} == {
        "device.ping", "device.health", "ui.inspect", "app.install", "app.uninstall", "sms.open",
    }
    registry = ActionRegistry.discover(env={}, remote=True, allowed_actions=["example.mutate"])
    entry.load.assert_called_once()
    assert registry.describe("example.mutate")["remote_allowed"]


def test_external_directory_manifest_and_traversal_guard(tmp_path):
    plugin_file = tmp_path / "example.py"
    plugin_file.write_text(
        "from moyu_harmony_pilot.actions import ActionPlugin, ActionSpec, ActionOutcome\n"
        "class Example(ActionPlugin):\n"
        "    spec = ActionSpec('example.local', '1', 'Offline example')\n"
        "    def execute(self, ctx, params):\n"
        "        return ActionOutcome()\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "actions.json"
    manifest.write_text(json.dumps({"example.local": "example.py:Example"}), encoding="utf-8")
    registry = ActionRegistry.discover(env={"HARMONY_PILOT_PLUGIN_PATHS": str(tmp_path)})
    assert registry.describe("example.local")["side_effect"] == "readonly"
    plugin_file.write_text("raise RuntimeError('should never import')", encoding="utf-8")
    registry = ActionRegistry.discover(
        env={"HARMONY_PILOT_PLUGIN_PATHS": str(tmp_path)}, remote=True
    )
    assert "example.local" not in [item["action_id"] for item in registry.list()]
    manifest.write_text(json.dumps({"example.local": "../outside.py:Example"}), encoding="utf-8")
    with pytest.raises(DeviceConfigurationError):
        ActionRegistry.discover(env={"HARMONY_PILOT_PLUGIN_PATHS": str(tmp_path)})


def test_prepare_stage_runs_before_reservation_and_allows_retry(execution, tmp_path):
    from moyu_harmony_pilot.device import HumanRequiredError

    _, device, _, _, _ = execution

    def row_count():
        with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
            return db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

    class PreparableMutation(IdempotentAction):
        spec = ActionSpec("test.preparable", "1", "Preparable mutation", side_effect="mutating")

        def __init__(self):
            self.calls = []
            self.prepare_blocked = True

        def inspect(self, ctx, params):
            self.calls.append("inspect")
            return Inspection(status="ready", state="needs_prepare")

        def prepare(self, ctx, inspection, params):
            self.calls.append(f"prepare:{inspection.state}")
            if self.prepare_blocked:
                assert row_count() == 0
                raise HumanRequiredError("App launch blocked before submission.")
            return Inspection(status="ready", state="prepared")

        def submit(self, ctx, inspection, params):
            self.calls.append(f"submit:{inspection.state}")
            with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
                statuses = [row[0] for row in db.execute("SELECT status FROM actions").fetchall()]
            assert "submitting" in statuses

        def confirm(self, ctx, params):
            self.calls.append("confirm")
            return ActionOutcome(status="succeeded")

    plugin = PreparableMutation()
    registry = ActionRegistry()
    registry.register(plugin)
    runner = ActionRunner(registry, state_dir=tmp_path)
    stages = []

    first = runner.run(
        device, plugin.spec.action_id, {}, idempotency_key="day-1", on_stage=stages.append
    )
    assert first.status == "human_required"
    assert row_count() == 0
    assert stages == ["inspect", "preparing"]

    stages.clear()
    plugin.prepare_blocked = False
    retried = runner.run(
        device, plugin.spec.action_id, {}, idempotency_key="day-1", on_stage=stages.append
    )
    assert retried.status == "succeeded"
    assert read_status(tmp_path) == "succeeded"
    assert stages == ["inspect", "preparing", "submit", "confirm"]
    assert plugin.calls[-4:] == ["inspect", "prepare:needs_prepare", "submit:prepared", "confirm"]
