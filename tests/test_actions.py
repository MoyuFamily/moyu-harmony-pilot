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
    ACTION_API_VERSION,
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
from moyu_harmony_pilot.actions.validation import check_schema, json_copy, validate_value
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceProfile,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import HdcClient
from tests.fakes import BATTERY_61_OVERHEATED, FakeDeviceState, FakeHdcTransport


def test_validation_reports_declared_paths_without_echoing_input():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["retry.limit"],
                    "properties": {"retry.limit": {"type": "integer", "minimum": 1}},
                },
            },
        },
    }
    check_schema(schema)
    cases = [
        ({}, '$["items"]'),
        ({"items": [{}]}, '$["items"][0]["retry.limit"]'),
        ({"items": [{"retry.limit": "private-value"}]}, '$["items"][0]["retry.limit"]'),
        ({"items": [{"retry.limit": 0}]}, '$["items"][0]["retry.limit"]'),
        ({"items": [{"retry.limit": 1, "private-key": "private-value"}]}, '$["items"][0]'),
    ]
    for value, expected_path in cases:
        with pytest.raises(DeviceConfigurationError) as error:
            validate_value(value, schema)
        assert expected_path in str(error.value)
        assert "private-key" not in str(error.value)
        assert "private-value" not in str(error.value)


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
    "error",
    [
        TimeoutError(),
        HumanRequiredError("private"),
        RuntimeError("private token"),
        KeyboardInterrupt(),
        SystemExit(),
    ],
)
@pytest.mark.parametrize("stage", ["submit", "confirm"])
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


def test_failed_or_invalid_confirmation_stays_unknown(execution):
    execution[2].outcome = ActionOutcome(status="failed")
    assert run_mutation(execution).status == "unknown"


def test_invalid_metric_after_submit_stays_unknown(execution):
    execution[2].outcome = ActionOutcome(metrics={"undeclared": "raw page"})
    assert run_mutation(execution).status == "unknown"


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


def test_cancellation_before_reservation_leaves_action_retriable(execution, tmp_path):
    result = run_mutation(execution, cancelled=lambda: True)
    assert result.error_code == "action_cancelled"
    assert execution[2].calls == []
    with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert run_mutation(execution).status == "succeeded"


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


@pytest.mark.parametrize("stage", ["reserve", "submit", "confirm"])
def test_cancellation_after_reservation_finishes_confirmation(
    execution, monkeypatch, tmp_path, stage
):
    cancelled = threading.Event()
    if stage == "reserve":
        reserve = ActionLedger.reserve

        def cancel_after_commit(self, key, **kwargs):
            reserve(self, key, **kwargs)
            cancelled.set()

        monkeypatch.setattr(ActionLedger, "reserve", cancel_after_commit)

    def on_stage(current):
        if current == stage:
            cancelled.set()

    plugin = execution[2]
    confirm = plugin.confirm

    def confirm_with_checkpoints(ctx, params):
        ctx.checkpoint()
        assert ctx.remaining_timeout > 0
        return confirm(ctx, params)

    monkeypatch.setattr(plugin, "confirm", confirm_with_checkpoints)
    plugin.submit_hook = lambda: time.sleep(0.15)
    assert run_mutation(
        execution, cancelled=cancelled.is_set, on_stage=on_stage
    ).status == "succeeded"
    assert cancelled.is_set()
    assert plugin.calls == ["inspect", "submit", "confirm"]
    assert read_status(tmp_path) == "succeeded"
    assert run_mutation(execution).status == "already_done"
    assert plugin.calls.count("submit") == 1


@pytest.mark.parametrize("stage", ["submit", "confirm"])
def test_cancelled_submission_still_obeys_deadline(execution, monkeypatch, tmp_path, stage):
    cancelled = threading.Event()
    operation = getattr(execution[2], stage)

    def expire(ctx, *args):
        result = operation(ctx, *args)
        cancelled.set()
        time.sleep(2)
        return result

    monkeypatch.setattr(execution[2], stage, expire)
    result = run_mutation(execution, cancelled=cancelled.is_set, timeout=0.2)
    assert result.status == "unknown"
    assert read_status(tmp_path) == "unknown"
    assert run_mutation(execution).status == "unknown"
    assert execution[2].calls.count("submit") == 1
    assert ("confirm" in execution[2].calls) == (stage == "confirm")


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


@pytest.mark.parametrize("status", ["already_done", "human_required", "failed"])
def test_readonly_inspection_terminal_states(execution, status):
    plugin = execution[2]
    plugin.inspection = Inspection(status=status)
    assert run_mutation(execution).status == status
    assert plugin.calls == ["inspect"]


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


def test_step_budget_and_dry_run_never_submit(execution):
    assert run_mutation(execution, max_steps=1).status == "failed"
    assert execution[2].calls == []
    assert run_mutation(execution, dry_run=True).status == "succeeded"
    assert execution[2].calls == []
    with pytest.raises(DeviceConfigurationError):
        run_mutation(execution, max_steps=0)
    with pytest.raises(ActionBudgetExceeded):
        ActionContext(execution[1], time.monotonic() - 1).checkpoint()


def test_busy_signal_timer_and_worker_thread_are_rejected(execution):
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        with pytest.raises(DeviceConfigurationError):
            run_mutation(execution)
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    with ThreadPoolExecutor() as pool:
        with pytest.raises(DeviceConfigurationError):
            pool.submit(run_mutation, execution).result()


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


def test_registry_examples_remain_tuples_and_isolated():
    plugin = SampleMutation()
    plugin.spec = replace(plugin.spec, examples=({"value": 1},))
    registry = ActionRegistry()
    registry.register(plugin)
    plugin.spec.examples[0]["value"] = 2
    _, spec = registry.get(plugin.spec.action_id)
    assert spec.examples == ({"value": 1},)
    spec.examples[0]["value"] = 3
    assert registry.get(plugin.spec.action_id)[1].examples == ({"value": 1},)
    assert registry.describe(plugin.spec.action_id)["examples"] == [{"value": 1}]


@pytest.mark.parametrize(
    "mode, message",
    [("duplicate", "Duplicate action ID"), ("schema", "Parameters and metrics")],
)
def test_discovery_preserves_configuration_errors(monkeypatch, mode, message):
    class Plugin(SampleMutation):
        spec = replace(SampleMutation.spec, action_id="device.health")

    if mode == "schema":
        Plugin.spec = replace(Plugin.spec, params_schema={"type": "integer"})
    entry = Mock()
    entry.name = "device.health"
    entry.value = "synthetic:Plugin"
    entry.load.return_value = Plugin
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry])
    with pytest.raises(DeviceConfigurationError, match=message):
        ActionRegistry.discover(env={})


def test_discovery_hides_uncontrolled_import_errors(monkeypatch):
    entry = Mock()
    entry.name = "example.failed"
    entry.value = "synthetic:Plugin"
    entry.load.side_effect = RuntimeError("PRIVATE_IMPORT_DETAILS")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry])
    with pytest.raises(DeviceConfigurationError, match="Plugin discovery failed") as error:
        ActionRegistry.discover(env={})
    assert "PRIVATE_IMPORT_DETAILS" not in str(error.value)


def test_legacy_action_spec_defaults_to_api_v1(execution):
    registry = execution[0].registry
    assert ACTION_API_VERSION == 1
    assert registry.describe("example.mutate")["api_version"] == 1
    description = registry.describe("example.mutate")
    description["api_version"] = 2
    assert registry.describe("example.mutate")["api_version"] == 1
    assert run_mutation(execution).status == "succeeded"


@pytest.mark.parametrize("api_version", [0, 2, -1, True, 1.0, "1", None])
def test_unsupported_action_api_is_rejected_before_registration(execution, api_version):
    plugin = execution[2]
    plugin.spec = replace(plugin.spec, api_version=api_version)
    registry = ActionRegistry()
    with pytest.raises(DeviceConfigurationError, match="Unsupported action API"):
        registry.register(plugin)
    assert registry.list() == []
    assert plugin.calls == []
    assert execution[3].calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"large": "a" * 17000}])
def test_json_values_are_finite_and_bounded(value):
    with pytest.raises(DeviceConfigurationError):
        json_copy(value)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {"type": "object", "additionalProperties": True},
        {"type": "string", "pattern": ".*"},
        {"type": "array"},
        {"type": "string", "maxLength": -1},
        {"type": "number", "maximum": "bad"},
        {"type": "string", "minimum": 0},
    ],
)
def test_unsupported_or_malformed_schema_fails_closed(schema):
    with pytest.raises(DeviceConfigurationError):
        check_schema(schema)


def test_nested_parameter_validation_and_boolean_integer_distinction():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["values"],
        "properties": {
            "values": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "integer", "minimum": 1},
            }
        },
    }
    check_schema(schema)
    validate_value({"values": [1, 2]}, schema)
    for value in (
        {},
        {"values": []},
        {"values": [True]},
        {"values": [0]},
        {"values": [1, 2, 3]},
        {"values": [1], "unknown": "secret"},
    ):
        with pytest.raises(DeviceConfigurationError):
            validate_value(value, schema)
    validate_value(1.0, {"type": "number", "enum": [1]})
    with pytest.raises(DeviceConfigurationError):
        validate_value(True, {"type": "boolean", "enum": [1]})


def test_invalid_action_results_and_specs_are_rejected():
    for outcome in (
        ActionOutcome(metrics={"a": {}}),
        ActionOutcome(metrics={"a": "x" * 129}),
        ActionOutcome(error_code="TOKEN secret"),
        ActionOutcome(status="other"),
    ):
        with pytest.raises(DeviceConfigurationError):
            outcome.to_dict()
    plugin = SampleMutation()
    plugin.spec = replace(plugin.spec, side_effect="readonly")
    with pytest.raises(DeviceConfigurationError):
        ActionRegistry().register(plugin)


def test_external_entry_points_filter_before_import(monkeypatch):
    entry = Mock(name="entry")
    entry.name = "example.mutate"
    entry.value = "test:SampleMutation"
    entry.load.return_value = SampleMutation
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [entry])
    registry = ActionRegistry.discover(env={}, remote=True)
    entry.load.assert_not_called()
    assert {item["action_id"] for item in registry.list()} == {
        "device.ping", "device.health", "ui.inspect", "app.install", "app.uninstall",
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


def test_builtin_readonly_actions_and_local_ui_diagnostics(execution, tmp_path):
    _, device, _, transport, state = execution
    runner = ActionRunner(ActionRegistry.discover(env={}), state_dir=tmp_path)
    state.battery_dump = BATTERY_61_OVERHEATED
    assert runner.run(device, "device.ping", {}).status == "succeeded"
    health = runner.run(device, "device.health", {})
    assert health.metrics["overheated"] is True
    result = runner.run(device, "ui.inspect", {"max_elements": 2})
    assert result.has_local_diagnostics
    assert result.metrics["kept_elements"] == 2
    files = list((tmp_path / "diagnostics").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["layout_hash"] == result.metrics["layout_hash"]
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert not transport.all_remote_tmp_files()


def test_repository_external_example_runs_through_discovery(execution, tmp_path):
    directory = Path(__file__).resolve().parents[1] / "examples" / "actions"
    registry = ActionRegistry.discover(env={"HARMONY_PILOT_PLUGIN_PATHS": str(directory)})
    runner = ActionRunner(registry, state_dir=tmp_path)
    result = runner.run(execution[1], "example.ui_summary", {})
    assert result.status == "succeeded"
    assert result.metrics["visible_elements"] > 0
    assert not execution[3].all_remote_tmp_files()


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
            self.prepare_mode = "raise_human"
            self.submit_raises = False

        def inspect(self, ctx, params):
            self.calls.append("inspect")
            return Inspection(status="ready", state="needs_prepare")

        def prepare(self, ctx, inspection, params):
            self.calls.append(f"prepare:{inspection.state}")
            if self.prepare_mode == "raise_human":
                assert row_count() == 0
                raise HumanRequiredError("App launch blocked before submission.")
            if self.prepare_mode == "return_human":
                assert row_count() == 0
                return Inspection(status="human_required")
            return Inspection(status="ready", state="prepared")

        def submit(self, ctx, inspection, params):
            self.calls.append(f"submit:{inspection.state}")
            with sqlite3.connect(tmp_path / "actions.sqlite3") as db:
                statuses = [row[0] for row in db.execute("SELECT status FROM actions").fetchall()]
            assert "submitting" in statuses
            if self.submit_raises:
                raise TimeoutError("click timeout")

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

    plugin.prepare_mode = "return_human"
    second = runner.run(device, plugin.spec.action_id, {}, idempotency_key="day-1")
    assert second.status == "human_required"
    assert row_count() == 0

    stages.clear()
    plugin.prepare_mode = "ok"
    third = runner.run(
        device, plugin.spec.action_id, {}, idempotency_key="day-1", on_stage=stages.append
    )
    assert third.status == "succeeded"
    assert read_status(tmp_path) == "succeeded"
    assert stages == ["inspect", "preparing", "submit", "confirm"]
    assert plugin.calls[-4:] == ["inspect", "prepare:needs_prepare", "submit:prepared", "confirm"]

    # Post-reservation submit failure still locks the key as unknown.
    plugin.submit_raises = True
    failed = runner.run(device, plugin.spec.action_id, {}, idempotency_key="day-2")
    assert failed.status == "unknown"
    calls_before_replay = len(plugin.calls)
    replay = runner.run(device, plugin.spec.action_id, {}, idempotency_key="day-2")
    assert replay.status == "unknown"
    assert len(plugin.calls) == calls_before_replay
