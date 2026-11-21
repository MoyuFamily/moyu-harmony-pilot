"""Offline regressions for the public helpers used by independently installed plugins."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import replace
from unittest.mock import patch

import pytest

from moyu_harmony_pilot.actions import (
    ActionLedger,
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.device import DeviceConfigurationError, HarmonyDevice
from moyu_harmony_pilot.hdc import (
    CommandResult,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcClient,
    HdcCommandError,
    HdcFileTransferError,
    HdcTimeoutError,
    HdcTransport,
)
from moyu_harmony_pilot.testing import (
    FakeDeviceState,
    FakeHdcTransport,
    OfflineTestCase,
    assert_idempotent_success,
    assert_pre_submit_blocked,
    assert_readonly,
    assert_submitting,
    assert_unknown_no_replay,
    make_fake_device,
    make_layout,
    offline_guard,
)
from moyu_harmony_pilot.ui import UiConcurrentCallError


@pytest.fixture(autouse=True)
def prevent_external_io():
    with offline_guard():
        yield


def toggle_layout(checked=False):
    return make_layout({
        "id": "demo_toggle", "type": "Toggle", "text": "Demo toggle",
        "checkable": True, "checked": checked,
    })


class Toggle(IdempotentAction):
    spec = ActionSpec(
        "test.toggle", "1.0", "Synthetic toggle for testing the public contract assertions.",
        side_effect="mutating", min_pilot_version="0.2.0.dev0",
        metrics_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"checked": {"type": "boolean"}}, "required": ["checked"],
        },
        inspection_metrics_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"was_checked": {"type": "boolean"}}, "required": ["was_checked"],
        },
    )

    def __init__(self):
        self.context = None
        self.mutate_during = None
        self.on_submit = lambda: None

    def inspect(self, ctx, params):
        self.context = ctx
        if self.mutate_during == "inspect":
            ctx.device.ui.key_event("Back", timeout=ctx.remaining_timeout)
        snapshot = ctx.interact().observe()
        element = snapshot.elements[0]
        if element.checked:
            return Inspection(status="already_done", metrics={"checked": True})
        return Inspection(
            state=(snapshot.layout_hash, element.ref), metrics={"was_checked": False},
        )

    def submit(self, ctx, inspection, params):
        self.on_submit()
        layout_hash, ref = inspection.state
        ctx.interact().click_ref(ref, layout_hash=layout_hash)

    def confirm(self, ctx, params):
        self.context = ctx
        if self.mutate_during == "confirm":
            ctx.device.ui.key_event("Back", timeout=ctx.remaining_timeout)
        checked = ctx.interact().observe().elements[0].checked
        return ActionOutcome(
            status="succeeded" if checked else "failed", metrics={"checked": checked},
        )


@pytest.fixture
def mutation(tmp_path):
    state = FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True)], advance_layout_on_click=True,
    )
    fake = make_fake_device(tmp_path, state=state)
    plugin = Toggle()
    registry = ActionRegistry(allowed_actions=[plugin.spec.action_id])
    registry.register(plugin)
    return fake, plugin, ActionRunner(registry, state_dir=tmp_path)


def test_fake_transport_protocol_discovery_and_target_isolation(tmp_path):
    first = FakeDeviceState(serial="SYNTHETIC_A", battery_percent=12)
    second = FakeDeviceState(serial="SYNTHETIC_B", battery_percent=91)
    transport = FakeHdcTransport([first, second])
    assert isinstance(transport, HdcTransport)
    client = HdcClient(transport)
    assert client.hdc_path is None
    assert client.version() == "3.2.0c"
    assert [target.serial for target in client.list_targets()] == [first.serial, second.serial]
    assert "capacity: 91" in client.shell(
        second.serial, ["hidumper", "-s", "BatteryService", "-a", "-i"],
    ).stdout
    source = tmp_path / "synthetic.txt"
    source.write_bytes(b"synthetic payload")
    remote = "/data/local/tmp/moyu_pilot_test.txt"
    client.send_file(first.serial, source, remote)
    assert second.remote_files == {}
    assert transport.all_remote_tmp_files() == {first.serial: [remote]}
    assert client.recv_file(first.serial, remote, tmp_path / "received").read_bytes() == (
        b"synthetic payload"
    )
    assert client.cleanup_remote_tmp(first.serial, remote)
    assert transport.all_remote_tmp_files() == {}
    assert len(transport.mutating_calls) == 1
    assert all(timeout > 0 for _, _, timeout in transport.calls)
    with pytest.raises(ValueError, match="unique"):
        FakeHdcTransport([first, first])


@pytest.mark.parametrize(("status", "error"), [
    ("Offline", DeviceOfflineError), ("Unauthorized", DeviceUnauthorizedError),
])
def test_offline_and_unauthorized_use_real_client_error_semantics(status, error):
    state = FakeDeviceState(status=status)
    transport = FakeHdcTransport([state])
    with pytest.raises(error):
        HdcClient(transport).shell(state.serial, ["uitest", "--version"])
    with pytest.raises(DeviceOfflineError):
        HdcClient(transport).shell("SYNTHETIC_MISSING", ["uitest", "--version"])


def test_real_device_builder_telemetry_observation_and_locking(tmp_path):
    fake = make_fake_device(tmp_path, alias="synthetic-alias")
    assert isinstance(fake.device, HarmonyDevice)
    assert fake.device.alias == "synthetic-alias"
    assert fake.transport.calls == []
    assert fake.device.get_foreground_window().bundle_name == fake.state.foreground_bundle
    with fake.device.acquire_lock():
        assert fake.device.is_busy()
        health = fake.device.get_health(include_info=True)
        assert health.battery.battery_percent == 72
        assert health.battery.temperature_c == 30
        assert health.info.api_version == 20
    assert not fake.device.is_busy()
    fake.state.layouts = [toggle_layout()]
    tree, snapshot = assert_readonly(fake.transport, fake.device.dump_ui_compact)
    assert tree.screen_size == (1080, 1920)
    assert [(element.ref, element.id) for element in snapshot.elements] == [("@e1", "demo_toggle")]
    assert not snapshot.elements[0].checked
    assert fake.transport.all_remote_tmp_files() == {}
    fake.state.battery_percent = None
    assert fake.device.get_battery().battery_percent is None
    fake.state.temperature_c = 43.5
    assert fake.device.get_health().overheated
    assert make_fake_device(tmp_path / "second").state.remote_files == {}


def test_fake_app_and_power_effects_are_recorded(tmp_path):
    fake = make_fake_device(tmp_path)
    fake.device.suspend()
    assert not fake.state.screen_on
    fake.device.wakeup()
    assert fake.state.screen_on
    fake.device.start_app("com.example.second")
    assert fake.device.get_foreground_window().bundle_name == "com.example.second"
    fake.device.force_stop("com.example.second")
    assert fake.device.get_foreground_window().bundle_name is None
    assert len(fake.transport.mutating_calls) == 4


def test_layout_sequence_only_advances_on_configured_event(tmp_path):
    state = FakeDeviceState(layouts=[toggle_layout(), toggle_layout(True)])
    fake = make_fake_device(tmp_path, state=state)
    fake.device.ui.key_event("Back")
    fake.device.dump_ui_compact()
    assert state.layout_index == 0
    state.advance_layout_on_click = True
    fake.device.ui.key_event("Back")
    assert state.layout_index == 0
    fake.device.ui.click(100, 80)
    fake.device.ui.click(100, 80)
    assert state.layout_index == 1
    assert fake.device.dump_ui_compact()[1].elements[0].checked
    state.layout_index = 0
    state.advance_layout_on_dump = True
    assert not fake.device.dump_ui_compact()[1].elements[0].checked
    assert fake.device.dump_ui_compact()[1].elements[0].checked


@pytest.mark.parametrize("failure", ["receive", "concurrent", "timeout", "invalid-layout"])
def test_failed_observation_cleans_up_remote_files(tmp_path, failure):
    fake = make_fake_device(tmp_path)
    errors = {
        "receive": HdcFileTransferError, "concurrent": UiConcurrentCallError,
        "timeout": HdcTimeoutError, "invalid-layout": HdcCommandError,
    }
    fake.state.fail_recv = failure == "receive"
    fake.state.fail_concurrent_uitest = failure == "concurrent"
    fake.state.timeout_on_cmd_prefix = "uitest dumpLayout" if failure == "timeout" else None
    if failure == "invalid-layout":
        fake.state.layouts = []
    with pytest.raises(errors[failure]):
        fake.device.dump_ui_compact()
    assert fake.transport.all_remote_tmp_files() == {}
    assert fake.transport.mutating_calls == []


def test_shell_overrides_and_unknown_commands_fail_closed(tmp_path):
    fake = make_fake_device(tmp_path)
    client = HdcClient(fake.transport)
    command = ("bm", "dump", "-a")
    fake.state.shell_results[command] = CommandResult((), 0, "com.example.demo", "", 1)
    assert client.shell(fake.state.serial, command).stdout == "com.example.demo"
    fake.state.shell_results[command] = TimeoutError("Synthetic timeout")
    with pytest.raises(TimeoutError):
        client.shell(fake.state.serial, command)
    for command in (["unsupported"], ["uitest", "uiInput", "unsupported"]):
        with pytest.raises(HdcCommandError):
            client.shell(fake.state.serial, command)
    malformed = fake.transport.execute(["shell", "'unterminated"], target=fake.state.serial)
    assert not malformed.ok
    assert len(fake.transport.mutating_calls) == 3


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_transport_rejects_invalid_timeouts(timeout):
    transport = FakeHdcTransport()
    with pytest.raises(ValueError, match="timeout"):
        transport.execute(["-v"], timeout=timeout)
    assert transport.calls == []


def test_readonly_assertion_rejects_new_writes_even_after_earlier_writes(tmp_path):
    fake = make_fake_device(tmp_path)
    fake.device.ui.click(100, 80)
    assert assert_readonly(fake.transport, lambda: 42) == 42
    with pytest.raises(AssertionError, match="mutation"):
        assert_readonly(fake.transport, lambda: fake.device.ui.key_event("Back"))


def test_success_contract_persists_reservation_metrics_and_survives_restart(mutation):
    fake, plugin, runner = mutation
    observed = []

    def check_reservation():
        assert_submitting(
            runner, fake.device, plugin.spec.action_id, {}, idempotency_key="contract-success",
        )
        observed.append(True)

    plugin.on_submit = check_reservation
    receipt = assert_idempotent_success(runner, fake, plugin.spec.action_id, {}, remote=True)
    assert receipt.metrics == {"checked": True}
    assert observed == [True]
    assert len(fake.transport.mutating_calls) == 1
    records = ActionLedger(runner.state_dir / "actions.sqlite3").records()
    assert records[0]["inspection_metrics"] == {"was_checked": False}
    serialized = json.dumps(records)
    assert fake.state.serial not in serialized
    assert "contract-success" not in serialized
    before = list(fake.transport.calls)
    assert plugin.context.stage is None
    with pytest.raises(DeviceConfigurationError):
        plugin.context.interact().observe()
    assert fake.transport.calls == before


@pytest.mark.parametrize("failure", ["timeout-before", "timeout-after", "unconfirmed"])
def test_unknown_contract_blocks_same_and_restarted_runner_replay(mutation, failure):
    fake, plugin, runner = mutation
    if failure == "timeout-before":
        fake.state.timeout_on_cmd_prefix = "uitest uiInput click"
    elif failure == "timeout-after":
        fake.state.timeout_after_mutation = True
    else:
        fake.state.advance_layout_on_click = False
    assert_unknown_no_replay(runner, fake, plugin.spec.action_id, {}, remote=True)
    assert fake.state.layout_index == (1 if failure == "timeout-after" else 0)
    plugin.context = None
    fake.state.layout_index = 1
    fresh = ActionRunner(runner.registry, state_dir=runner.state_dir)
    receipt = assert_readonly(fake.transport, lambda: fresh.reconcile(
        fake.device, plugin.spec.action_id, {}, idempotency_key="contract-unknown",
    ))
    assert receipt.status == "succeeded"
    assert receipt.metrics == {"checked": True}
    assert plugin.context.stage is None
    with pytest.raises(DeviceConfigurationError):
        plugin.context.interact().observe()


def test_crashed_submitting_row_prevents_plugin_execution(mutation):
    fake, plugin, runner = mutation
    ledger = ActionLedger(runner.state_dir / "actions.sqlite3")
    key = ledger.make_key(
        fake.state.serial, plugin.spec.action_id, "crash", plugin.spec.version, {},
    )
    ledger.reserve(key)
    assert_submitting(runner, fake.device, plugin.spec.action_id, {}, idempotency_key="crash")
    for current in (runner, ActionRunner(runner.registry, state_dir=runner.state_dir)):
        result = current.run(fake.device, plugin.spec.action_id, {}, idempotency_key="crash")
        assert result.status == "unknown"
        assert result.error_code == "submission_uncertain"
    assert plugin.context is None
    assert fake.transport.calls == []
    assert ledger.records()[0]["status"] == "unknown"


def test_reservation_assertion_matches_exact_device_action_key_and_request(mutation):
    fake, plugin, runner = mutation
    with pytest.raises(AssertionError, match="durable reservation"):
        assert_submitting(runner, fake.device, plugin.spec.action_id, {}, idempotency_key="wanted")
    ledger = ActionLedger(runner.state_dir / "actions.sqlite3")
    key = ledger.make_key(
        fake.state.serial, plugin.spec.action_id, "other", plugin.spec.version, {},
    )
    ledger.reserve(key)
    with pytest.raises(AssertionError, match="durable reservation"):
        assert_submitting(runner, fake.device, plugin.spec.action_id, {}, idempotency_key="wanted")
    key = ledger.make_key(
        fake.state.serial, plugin.spec.action_id, "wanted", plugin.spec.version, {"changed": True},
    )
    ledger.reserve(key)
    with pytest.raises(AssertionError, match="another request"):
        assert_submitting(runner, fake.device, plugin.spec.action_id, {}, idempotency_key="wanted")


@pytest.mark.parametrize("stage", ["inspect", "confirm"])
def test_contract_rejects_side_effects_in_readonly_plugin_stages(mutation, stage):
    fake, plugin, runner = mutation
    plugin.mutate_during = stage
    with pytest.raises(AssertionError, match="Inspection|Confirmation"):
        assert_idempotent_success(runner, fake, plugin.spec.action_id, {})


def test_success_assertion_rejects_an_already_done_only_fixture(mutation):
    fake, plugin, runner = mutation
    fake.state.layout_index = 1
    with pytest.raises(AssertionError, match="inspect/submit"):
        assert_idempotent_success(runner, fake, plugin.spec.action_id, {})


def test_unknown_assertion_does_not_swallow_failed_reservation_assertion(mutation):
    fake, plugin, runner = mutation
    with patch.object(ActionLedger, "reserve", return_value=None):
        with pytest.raises(AssertionError, match="reservation contract"):
            assert_unknown_no_replay(runner, fake, plugin.spec.action_id, {}, expected_mutations=0)
    assert fake.transport.mutating_calls == []


@pytest.mark.parametrize("scenario", ["thermal", "sensitive", "sensitive-after-inspection"])
def test_boundary_assertion_stops_before_any_reservation(mutation, scenario):
    fake, plugin, runner = mutation
    expected = "human_required"
    if scenario == "thermal":
        fake.state.temperature_c = fake.device.profile.max_temperature_c
        expected = "device_overheated"
    else:
        sensitive = make_layout({"text": "Synthetic captcha"})
        fake.state.layouts = [sensitive]
        if scenario == "sensitive-after-inspection":
            fake.state.layouts.insert(0, toggle_layout())
            fake.state.advance_layout_on_dump = True
    assert_pre_submit_blocked(
        runner, fake, plugin.spec.action_id, {}, expected_status=expected, remote=True,
    )
    assert fake.state.layout_index == (1 if scenario == "sensitive-after-inspection" else 0)


def test_unknown_after_sensitive_transition_never_replays(mutation):
    fake, plugin, runner = mutation
    fake.state.layouts[1] = make_layout({"text": "Synthetic captcha"})
    assert_unknown_no_replay(runner, fake, plugin.spec.action_id, {})
    assert fake.state.layout_index == 1


def test_missing_version_declaration_is_detected_through_normal_runner(mutation):
    fake, plugin, runner = mutation
    plugin.spec = replace(plugin.spec, min_pilot_version=None, inspection_metrics_schema=None)
    registry = ActionRegistry()
    registry.register(plugin)
    result = ActionRunner(registry, state_dir=runner.state_dir).run(
        fake.device, plugin.spec.action_id, {}, idempotency_key="old-api",
    )
    assert result.status == "configuration_error"
    assert fake.transport.mutating_calls == []


def test_offline_guard_nests_restores_and_blocks_network_and_processes():
    guarded_popen = subprocess.Popen
    with offline_guard():
        with pytest.raises(AssertionError, match="offline"):
            socket.getaddrinfo("synthetic.invalid", 80)
        with pytest.raises(AssertionError, match="offline"):
            socket.create_connection(("synthetic.invalid", 80))
        with pytest.raises(AssertionError, match="offline"):
            subprocess.Popen(["synthetic-command"])  # noqa: S603, S607
        with pytest.raises(AssertionError, match="offline"):
            os.system("synthetic-command")  # noqa: S605, S607
    assert subprocess.Popen is guarded_popen


def test_unittest_base_owns_temp_state_and_restores_guards():
    case = OfflineTestCase()
    outer_popen = subprocess.Popen
    case.setUp()
    try:
        assert isinstance(case.device, HarmonyDevice)
        assert case.fake.device is case.device
        assert case.fake.state is case.device_state
        assert case.fake.transport is case.transport
        assert case.state_dir.is_dir()
    finally:
        case.doCleanups()
    assert not case.state_dir.exists()
    assert subprocess.Popen is outer_popen
