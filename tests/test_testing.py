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
    FakeClickTarget,
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


@pytest.mark.parametrize(("node_id", "bundle_name"), [
    ("", "com.example.demo"), (None, "com.example.demo"),
    ("demo_toggle", ""), ("demo_toggle", None), ("demo_toggle", "   "),
])
def test_click_target_requires_explicit_identity(node_id, bundle_name):
    with pytest.raises(ValueError, match="node ID and bundle"):
        FakeClickTarget(node_id, bundle_name)


def test_click_targets_follow_current_layout_and_consume_in_order(tmp_path):
    toggle = {"id": "demo_toggle", "type": "Toggle"}
    other = {"id": "other_button"}
    state = FakeDeviceState(
        layouts=[make_layout(toggle, other), make_layout(other, toggle), make_layout()],
        click_targets=[
            FakeClickTarget("demo_toggle", "com.example.demo"),
            FakeClickTarget("other_button", "com.example.demo"),
        ],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(AssertionError, match="Consumed 0 of 2"):
        fake.transport.assert_click_targets_consumed()
    for index, target in enumerate(state.click_targets, start=1):
        snapshot = fake.device.dump_ui_compact()[1]
        element = next(item for item in snapshot.elements if item.id == target.node_id)
        fake.device.ui.click_ref(snapshot, element.ref)
        assert state.layout_index == index
        assert state.click_target_index == index
        assert state.click_failures == []
    fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("fault", ["none", "timeout-before", "timeout-after", "override"])
def test_wrong_click_never_advances_and_failure_survives_a_later_correct_click(tmp_path, fault):
    layout = make_layout({"id": "demo_toggle"}, {"id": "other_button"})
    state = FakeDeviceState(
        layouts=[layout, make_layout()], advance_layout_on_click=True,
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
        timeout_on_cmd_prefix="uitest uiInput click" if fault == "timeout-before" else None,
        timeout_after_mutation=fault == "timeout-after",
    )
    if fault == "override":
        state.shell_results[("uitest", "uiInput", "click", "540", "200")] = (
            CommandResult((), 0, "ok", "", 1)
        )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError, match="target center"):
        fake.device.ui.click(540, 200)
    assert state.layout_index == state.click_target_index == 0
    assert len(state.click_failures) == 1
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()
    state.timeout_on_cmd_prefix = None
    state.timeout_after_mutation = False
    fake.device.ui.click(540, 80)
    assert state.layout_index == state.click_target_index == 1
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("inherited_bundle", [False, True])
@pytest.mark.parametrize("click_other_bundle", [False, True])
def test_click_target_binds_exact_bundle_including_inherited_bundle(
    tmp_path, inherited_bundle, click_other_bundle,
):
    layout = json.loads(make_layout(
        {"id": "demo_toggle"},
        {"id": "demo_toggle", "bundleName": "com.example.other"},
    ))
    if inherited_bundle:
        del layout["children"][0]["attributes"]["bundleName"]
    state = FakeDeviceState(
        layouts=[json.dumps(layout), make_layout()],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    if click_other_bundle:
        with pytest.raises(HdcCommandError, match="target center"):
            fake.device.ui.click(540, 200)
        assert state.layout_index == state.click_target_index == 0
        with pytest.raises(AssertionError, match="Click target checks failed"):
            fake.transport.assert_click_targets_consumed()
    else:
        fake.device.ui.click(540, 80)
        assert state.layout_index == state.click_target_index == 1
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("attributes", [
    {"id": "wrong_id"}, {"bundleName": "com.example.other"},
    {"enabled": False}, {"clickable": False}, {"visible": False},
    {"bounds": "[0,0][0,0]"}, {"bounds": "[1200,40][1300,120]"},
])
def test_click_targets_reject_missing_or_unusable_controls(tmp_path, attributes):
    control = {"id": "demo_toggle", "text": "Demo toggle", **attributes}
    state = FakeDeviceState(
        layouts=[make_layout(control), make_layout()],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError):
        fake.device.ui.click(540, 80)
    assert state.layout_index == state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


def test_click_target_rejects_center_outside_display(tmp_path):
    state = FakeDeviceState(
        layouts=[make_layout({"id": "demo_toggle", "bounds": "[1000,40][1300,120]"})],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError, match="inside the display"):
        fake.device.ui.click(1150, 80)
    assert state.layout_index == state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("intervening_controls", [0, 120])
def test_click_targets_reject_duplicates_even_beyond_compact_limit(tmp_path, intervening_controls):
    control = {"id": "demo_toggle"}
    others = [{"id": f"other_{index}"} for index in range(intervening_controls)]
    state = FakeDeviceState(
        layouts=[make_layout(control, *others, control, height=20000), make_layout()],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError, match="found 2"):
        fake.device.ui.click(540, 80)
    assert state.layout_index == state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("initial_target_count", [0, 1])
def test_click_targets_reject_extra_clicks_including_an_empty_sequence(
    tmp_path, initial_target_count,
):
    state = FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True), make_layout()],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")] * initial_target_count,
    )
    fake = make_fake_device(tmp_path, state=state)
    if initial_target_count:
        fake.device.ui.click(540, 80)
    fake.transport.assert_click_targets_consumed()
    with pytest.raises(HdcCommandError, match="Unexpected click"):
        fake.device.ui.click(540, 80)
    assert state.layout_index == state.click_target_index == initial_target_count
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("coordinates", [[], ["540"], ["540", "80", "1"], ["wrong", "80"]])
def test_click_targets_record_malformed_coordinates(tmp_path, coordinates):
    state = FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True)],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError):
        HdcClient(fake.transport).shell(state.serial, ["uitest", "uiInput", "click", *coordinates])
    assert state.layout_index == state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("layouts", [[], [""], ["{"], ["[]"]])
def test_click_targets_record_invalid_layout_fixtures(tmp_path, layouts):
    state = FakeDeviceState(
        layouts=layouts, click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    )
    fake = make_fake_device(tmp_path, state=state)
    with pytest.raises(HdcCommandError):
        fake.device.ui.click(540, 80)
    assert state.layout_index == state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


def test_click_target_progress_is_isolated_per_device():
    targets = [FakeClickTarget("demo_toggle", "com.example.demo")]
    first = FakeDeviceState(
        serial="SYNTHETIC_FIRST", layouts=[toggle_layout()], click_targets=targets,
    )
    second = FakeDeviceState(
        serial="SYNTHETIC_SECOND", layouts=[toggle_layout()], click_targets=targets,
    )
    transport = FakeHdcTransport([first, second])
    client = HdcClient(transport)
    client.shell(first.serial, ["uitest", "uiInput", "click", "540", "80"])
    assert (first.click_target_index, second.click_target_index) == (1, 0)
    with pytest.raises(AssertionError, match="Consumed 0 of 1"):
        transport.assert_click_targets_consumed()
    client.shell(second.serial, ["uitest", "uiInput", "click", "540", "80"])
    transport.assert_click_targets_consumed()


def test_click_targets_require_center_even_for_a_point_inside_target(tmp_path):
    fake = make_fake_device(tmp_path, state=FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True)],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    ))
    with pytest.raises(HdcCommandError, match="target center"):
        fake.device.ui.click(100, 80)
    assert fake.state.layout_index == fake.state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


def test_click_targets_reject_hidden_ancestor(tmp_path):
    layout = json.loads(toggle_layout())
    layout["attributes"]["visible"] = False
    fake = make_fake_device(tmp_path, state=FakeDeviceState(
        layouts=[json.dumps(layout), toggle_layout(True)],
        click_targets=[FakeClickTarget("demo_toggle", "com.example.demo")],
    ))
    with pytest.raises(HdcCommandError, match="found 0"):
        fake.device.ui.click(540, 80)
    assert fake.state.layout_index == fake.state.click_target_index == 0
    with pytest.raises(AssertionError, match="Click target checks failed"):
        fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("command", [
    "uitest uiInput click 100 80", "uitest uiInput click 'unterminated",
])
def test_legacy_timeout_prefix_precedes_shell_parsing_and_overrides(command):
    state = FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True)], advance_layout_on_click=True,
        timeout_on_cmd_prefix="uitest uiInput click",
        shell_results={("uitest", "uiInput", "click", "100", "80"): (
            CommandResult((), 0, "ok", "", 1)
        )},
    )
    transport = FakeHdcTransport([state])
    with pytest.raises(HdcTimeoutError):
        transport.execute(["shell", command], target=state.serial)
    assert state.click_targets is None
    assert state.layout_index == state.click_target_index == 0
    assert state.click_failures == []
    transport.assert_click_targets_consumed()


@pytest.mark.parametrize("response", [CommandResult((), 0, "ok", "", 1), TimeoutError("Synthetic")])
def test_legacy_shell_override_precedes_click_effect_and_post_mutation_timeout(tmp_path, response):
    fake = make_fake_device(tmp_path, state=FakeDeviceState(
        layouts=[toggle_layout(), toggle_layout(True)], advance_layout_on_click=True,
        timeout_after_mutation=True,
        shell_results={("uitest", "uiInput", "click", "100", "80"): response},
    ))
    if isinstance(response, Exception):
        with pytest.raises(TimeoutError, match="Synthetic"):
            fake.device.ui.click(100, 80)
    else:
        fake.device.ui.click(100, 80)
    assert fake.state.click_targets is None
    assert fake.state.layout_index == fake.state.click_target_index == 0
    assert fake.state.click_failures == []
    fake.transport.assert_click_targets_consumed()


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


@pytest.mark.parametrize("scenario", [
    "success", "timeout-before", "timeout-after", "unconfirmed", "override",
])
def test_targeted_contract_preserves_success_and_unknown_protection(mutation, scenario):
    fake, plugin, runner = mutation
    fake.state.click_targets = [FakeClickTarget("demo_toggle", "com.example.demo")]
    if scenario == "timeout-before":
        fake.state.timeout_on_cmd_prefix = "uitest uiInput click"
    elif scenario == "timeout-after":
        fake.state.timeout_after_mutation = True
    elif scenario == "unconfirmed":
        fake.state.layouts = [toggle_layout()]
    elif scenario == "override":
        fake.state.shell_results[("uitest", "uiInput", "click", "540", "80")] = (
            CommandResult((), 0, "ok", "", 1)
        )
    if scenario == "success":
        assert_idempotent_success(runner, fake, plugin.spec.action_id, {}, remote=True)
    else:
        assert_unknown_no_replay(runner, fake, plugin.spec.action_id, {}, remote=True)
    assert fake.state.layout_index == (1 if scenario in {"success", "timeout-after"} else 0)
    assert fake.state.click_target_index == 1
    assert fake.state.click_failures == []
    fake.transport.assert_click_targets_consumed()


@pytest.mark.parametrize("contract", [assert_idempotent_success, assert_unknown_no_replay])
def test_mutating_contract_rejects_wrong_click_even_when_runner_returns_unknown(
    mutation, monkeypatch, contract,
):
    fake, plugin, runner = mutation
    fake.state.layouts[0] = make_layout(
        {"id": "demo_toggle", "type": "Toggle"}, {"id": "other_button"},
    )
    fake.state.click_targets = [FakeClickTarget("demo_toggle", "com.example.demo")]

    def wrong_submit(ctx, inspection, params):
        snapshot = ctx.interact().observe()
        other = next(element for element in snapshot.elements if element.id == "other_button")
        ctx.interact().click_ref(other.ref, layout_hash=snapshot.layout_hash)

    monkeypatch.setattr(plugin, "submit", wrong_submit)
    with pytest.raises(AssertionError, match="Click target checks failed"):
        contract(
            runner, fake, plugin.spec.action_id, {}, idempotency_key="wrong-target", remote=True,
        )
    assert fake.state.layout_index == fake.state.click_target_index == 0
    assert len(fake.state.click_failures) == 1
    records = ActionLedger(runner.state_dir / "actions.sqlite3").records()
    assert records[0]["status"] == "unknown"
    before = list(fake.transport.calls)
    for current in (runner, ActionRunner(runner.registry, state_dir=runner.state_dir)):
        receipt = current.run(
            fake.device, plugin.spec.action_id, {}, idempotency_key="wrong-target", remote=True,
        )
        assert receipt.status == "unknown"
        assert fake.transport.calls == before


def test_unknown_contract_rejects_unconsumed_click_targets(mutation, monkeypatch):
    fake, plugin, runner = mutation
    fake.state.click_targets = [FakeClickTarget("demo_toggle", "com.example.demo")]
    monkeypatch.setattr(plugin, "submit", lambda ctx, inspection, params: None)
    with pytest.raises(AssertionError, match="Consumed 0 of 1"):
        assert_unknown_no_replay(
            runner, fake, plugin.spec.action_id, {}, expected_mutations=0,
        )
    assert fake.transport.mutating_calls == []


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


def test_contract_assertions_accept_prepare_stage(tmp_path):
    from moyu_harmony_pilot.actions import IdempotentAction

    class PreparableContractAction(IdempotentAction):
        spec = ActionSpec(
            "test.preparable_contract",
            "1",
            "Preparable contract action",
            side_effect="mutating",
            min_pilot_version="0.2.0.dev0",
        )

        def inspect(self, ctx, params):
            ctx.interact().observe()
            return Inspection(status="ready")

        def prepare(self, ctx, inspection, params):
            snap = ctx.interact().observe()
            ctx.interact().back(layout_hash=snap.layout_hash)
            return Inspection(status="ready", state="prepared")

        def submit(self, ctx, inspection, params):
            assert inspection.state == "prepared"
            snap = ctx.interact().observe()
            ctx.interact().back(layout_hash=snap.layout_hash)

        def confirm(self, ctx, params):
            ctx.interact().observe()
            return ActionOutcome(status="succeeded")

    fake = make_fake_device(
        state=FakeDeviceState("SYNTHETIC_PREPARE", layouts=[toggle_layout()]),
        state_dir=tmp_path / "ok",
    )
    registry = ActionRegistry(allowed_actions=["test.preparable_contract"])
    registry.register(PreparableContractAction())
    runner = ActionRunner(registry, state_dir=tmp_path / "ok")
    assert_idempotent_success(
        runner, fake, "test.preparable_contract", {}, expected_mutations=2, remote=True
    )
