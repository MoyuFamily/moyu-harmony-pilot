"""Normal CLI entry-point coverage for discovery, validation, receipts, and JSON isolation."""

from __future__ import annotations

import json

import pytest

from moyu_harmony_pilot.actions import ActionOutcome, ActionRegistry
from moyu_harmony_pilot.cli import build_cli_schema, build_parser
from moyu_harmony_pilot.device import DeviceLock
from tests.fakes import BATTERY_61_OVERHEATED, FakeDeviceState, FakeHdcTransport
from tests.test_actions import SampleMutation
from tests.test_cli import _parse_single_json_envelope, _run_cli


@pytest.fixture
def cli_setup(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    state = FakeDeviceState("OFFLINE_CLI_SERIAL")
    transport = FakeHdcTransport([state])
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": {"serial": state.serial}}}))
    config.chmod(0o600)
    return ["--config", str(config), "--state-dir", str(tmp_path / "state"), "--json"], transport


@pytest.mark.parametrize(
    "argv",
    [
        ["plugins", "list", "--json"],
        ["--json", "plugins", "list"],
        ["plugins", "--json", "describe", "device.health"],
        ["plugins", "describe", "device.health", "--json"],
    ],
)
def test_discovery_works_offline_at_each_flag_position(argv, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    code, out, err = _run_cli(argv, env={})
    assert code == 0
    assert err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["command"] == "plugins"
    if "describe" in argv:
        assert payload["data"]["action_id"] == "device.health"
        assert payload["data"]["api_version"] == 1
    else:
        assert len(payload["data"]["plugins"]) == 3
        assert all(item["api_version"] == 1 for item in payload["data"]["plugins"])
        assert not any(item["remote_allowed"] for item in payload["data"]["plugins"])


def test_run_builtin_and_dry_run(cli_setup):
    flags, transport = cli_setup
    code, out, err = _run_cli(
        flags + ["run", "--device", "phone", "--action", "device.health"],
        transport=transport,
        env={},
    )
    assert code == 0
    assert err == ""
    result = _parse_single_json_envelope(out)
    assert result["data"]["metrics"]["battery_percent"] == 72
    assert "OFFLINE_CLI_SERIAL" not in out
    transport.calls.clear()
    code, out, _ = _run_cli(
        flags + ["run", "--device", "phone", "--action", "ui.inspect", "--dry-run"],
        transport=transport,
        env={},
    )
    assert code == 0
    assert _parse_single_json_envelope(out)["data"]["dry_run"]
    assert [call[1] for call in transport.calls] == [("list", "targets", "-v")]


@pytest.mark.parametrize(
    "params",
    [
        "[]",
        "null",
        "{invalid",
        '{"bad":"private-secret"}',
        '{"max_elements":NaN}',
        '{"max_elements":Infinity}',
        '{"max_elements":1,"max_elements":2}',
        '{"max_elements":true}',
    ],
)
def test_invalid_parameters_fail_before_device_discovery(cli_setup, params):
    flags, transport = cli_setup
    code, out, _ = _run_cli(
        flags + ["run", "--action", "ui.inspect", "--params-json", params],
        transport=transport,
        env={},
    )
    assert code == 2
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert "private-secret" not in out
    assert transport.calls == []


def test_mutation_unknown_and_replay_exit_codes(cli_setup, monkeypatch):
    flags, transport = cli_setup
    plugin = SampleMutation()
    plugin.submit_error = RuntimeError("PRIVATE_TOKEN_SHOULD_STAY_LOCAL")
    registry = ActionRegistry()
    registry.register(plugin)
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    args = flags + ["run", "--device", "phone", "--action", plugin.spec.action_id]
    code, out, _ = _run_cli(args, transport=transport, env={})
    assert code == 2
    assert transport.calls == []
    for _ in range(2):
        code, out, _ = _run_cli(args + ["--idempotency-key", "key"], transport=transport, env={})
        assert code == 1
        payload = _parse_single_json_envelope(out)
        assert not payload["ok"]
        assert payload["data"]["status"] == "unknown"
        assert payload["error"]["code"] == "submission_uncertain"
        assert payload["error"]["retryable"] is False
        assert "PRIVATE_TOKEN" not in out
    assert plugin.calls.count("submit") == 1


def test_mutation_thermal_and_lock_exit_codes(cli_setup, monkeypatch, tmp_path):
    flags, transport = cli_setup
    state = next(iter(transport.devices.values()))
    registry = ActionRegistry()
    plugin = SampleMutation()
    registry.register(plugin)
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    args = flags + [
        "run",
        "--device",
        "phone",
        "--action",
        plugin.spec.action_id,
        "--idempotency-key",
        "key",
    ]
    state.battery_dump = BATTERY_61_OVERHEATED
    code, out, _ = _run_cli(args, transport=transport, env={})
    assert code == 5
    assert _parse_single_json_envelope(out)["data"]["status"] == "device_overheated"
    with DeviceLock(state.serial, alias="phone", lock_dir=tmp_path / "state" / "locks"):
        code, out, _ = _run_cli(args, transport=transport, env={})
    assert code == 6
    assert _parse_single_json_envelope(out)["error"]["code"] == "device_busy"
    assert plugin.calls == []


def test_plugin_noise_is_sent_to_stderr(cli_setup, monkeypatch):
    flags, transport = cli_setup
    registry = ActionRegistry.discover(env={})
    plugin, _ = registry.get("device.health")

    def noisy_execute(ctx, params):
        print("plugin progress")
        return ActionOutcome()

    monkeypatch.setattr(plugin, "execute", noisy_execute)

    def noisy_discovery(**kwargs):
        print("plugin loading")
        return registry

    monkeypatch.setattr(ActionRegistry, "discover", noisy_discovery)
    code, out, err = _run_cli(
        flags + ["run", "--action", "device.health"], transport=transport, env={}
    )
    assert code == 0
    assert _parse_single_json_envelope(out)["ok"]
    assert "plugin loading" in err and "plugin progress" in err


def test_run_flags_match_introspection_schema():
    parser = build_parser()
    commands = next(
        action.choices for action in parser._actions if isinstance(action.choices, dict)
    )
    run = commands["run"]
    flags = {flag for action in run._actions for flag in action.option_strings}
    schema = build_cli_schema()
    assert schema["action_contract"]["api_version"] == 1
    assert schema["action_contract"]["legacy_api_version"] == 1
    expected = set(schema["commands"]["run"]["options"])
    shared = set(schema["global_options"]) - {"--version"}
    assert flags == expected | shared | {"-h", "--help"}


def test_incompatible_directory_plugin_fails_cli_before_device_io(cli_setup, tmp_path):
    directory = tmp_path / "plugins"
    directory.mkdir()
    (directory / "actions.json").write_text(json.dumps({
        "example.future": "future.py:FutureAction",
    }))
    (directory / "future.py").write_text(
        "from moyu_harmony_pilot.actions import ActionPlugin, ActionSpec, ActionOutcome\n"
        "class FutureAction(ActionPlugin):\n"
        "    spec = ActionSpec('example.future', '1', 'Future API', api_version=2)\n"
        "    def execute(self, ctx, params):\n"
        "        raise AssertionError('incompatible action executed')\n"
    )
    flags, transport = cli_setup
    code, out, _ = _run_cli(
        flags + ["run", "--action", "example.future"], transport=transport,
        env={"HARMONY_PILOT_PLUGIN_PATHS": str(directory)},
    )
    assert code == 2
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert transport.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--version", "--json"],
        ["--help", "--json"],
        ["plugins", "list", "--json", "--help"],
        ["run", "--json", "--help"],
    ],
)
def test_json_help_and_version_preserve_single_envelope(argv):
    code, out, err = _run_cli(argv)
    assert code == 0
    assert err == ""
    assert _parse_single_json_envelope(out)["data"]["text"]


@pytest.mark.parametrize(
    "status,code",
    [("already_done", 0), ("human_required", 4), ("device_offline", 3), ("configuration_error", 2)],
)
def test_action_outcome_exit_codes(cli_setup, monkeypatch, status, code):
    flags, transport = cli_setup
    registry = ActionRegistry.discover(env={})
    plugin, _ = registry.get("device.health")
    monkeypatch.setattr(plugin, "execute", lambda ctx, params: ActionOutcome(status=status))
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    actual, out, _ = _run_cli(
        flags + ["run", "--action", "device.health"], transport=transport, env={}
    )
    assert actual == code
    payload = _parse_single_json_envelope(out)
    assert payload["data"]["status"] == status
    if status == "human_required":
        assert "manually" in payload["error"]["hint"]
