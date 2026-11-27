"""Normal CLI entry-point coverage for discovery, validation, receipts, and JSON isolation."""

from __future__ import annotations

import json

import pytest

from moyu_harmony_pilot.actions import ActionOutcome, ActionRegistry
from moyu_harmony_pilot.cli import build_cli_schema
from moyu_harmony_pilot.device import DeviceLock
from moyu_harmony_pilot.testing import offline_guard
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
        pytest.param(["plugins", "list", "--json"], id="list-default-deny"),
    ],
)
def test_discovery_works_offline_at_each_flag_position(argv, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    code, out, err = _run_cli(argv, env={})
    assert code == 0
    assert err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["command"] == "plugins"
    assert {item["action_id"] for item in payload["data"]["plugins"]} == {
        "device.ping", "device.health", "ui.inspect", "app.install", "app.uninstall",
        "sms.open", "sms.read", "files.query", "files.export",
        "record.start", "record.status", "record.stop",
    }
    assert all(item["api_version"] == 1 for item in payload["data"]["plugins"])
    assert all(item["source"] == "internal" for item in payload["data"]["plugins"])
    sms = next(item for item in payload["data"]["plugins"] if item["action_id"] == "sms.read")
    assert sms["private_content"] and "messages" in sms["result_schema"]["properties"]
    assert not any(item["remote_allowed"] for item in payload["data"]["plugins"])


def test_plugin_validate_is_offline_and_does_not_submit(cli_setup, monkeypatch, tmp_path):
    flags, transport = cli_setup
    plugin = SampleMutation()
    registry = ActionRegistry()
    registry.register(plugin)
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    args = flags + ["plugins", "validate", plugin.spec.action_id, "--params-json", '{"value":1}']

    code, out, _ = _run_cli(args, transport=transport, env={})
    assert code == 2
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert not (tmp_path / "state").exists()

    code, out, err = _run_cli(
        args + ["--idempotency-key", "private-test-key"], transport=transport, env={},
    )
    assert code == 0 and err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["data"] == {
        "valid": True,
        "action_id": plugin.spec.action_id,
        "api_version": 1,
        "version": "1.0",
        "side_effect": "mutating",
        "device_checked": False,
        "executed": False,
    }
    schema = build_cli_schema()["commands"]["plugins"]["subcommands"]["validate"]["result_schema"]
    assert set(payload["data"]) == set(schema["required"]) == set(schema["properties"])
    for name, field in schema["properties"].items():
        if "const" in field:
            assert payload["data"][name] is field["const"]
        if "enum" in field:
            assert payload["data"][name] in field["enum"]
    assert "private-test-key" not in out
    assert plugin.calls == []
    assert transport.calls == []
    assert not (tmp_path / "state").exists()


def test_plugin_validate_rejects_invalid_params_without_leaking_values(cli_setup):
    flags, transport = cli_setup
    params = '{"private-key-should-not-appear":"private-value"}'
    code, out, err = _run_cli(
        flags + ["plugins", "validate", "ui.inspect", "--params-json", params],
        transport=transport, env={},
    )
    assert code == 2 and err == ""
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert "private-key-should-not-appear" not in out
    assert "private-value" not in out
    assert transport.calls == []


def test_plugin_validate_reports_declared_parameter_path(cli_setup):
    flags, transport = cli_setup
    code, out, _ = _run_cli(
        flags + ["plugins", "validate", "ui.inspect", "--params-json", '{"max_elements":true}'],
        transport=transport, env={},
    )
    assert code == 2
    assert '$["max_elements"]' in _parse_single_json_envelope(out)["error"]["message"]
    assert not transport.calls


def test_plugin_init_creates_a_project_without_discovery_or_device_io(
    tmp_path, monkeypatch,
):
    def unexpected_discovery(**kwargs):
        raise AssertionError("Project creation loaded installed plugin code.")

    monkeypatch.setattr(ActionRegistry, "discover", unexpected_discovery)
    transport = FakeHdcTransport([])
    with offline_guard():
        code, out, err = _run_cli(
            ["plugins", "init", "--json", "team-pilot", "--namespace", "team"],
            transport=transport, cwd=tmp_path, env={},
        )
    assert code == 0 and err == "", out
    payload = _parse_single_json_envelope(out)
    data = payload["data"]
    assert data["path"] == str(tmp_path / "team-pilot")
    assert data["package_name"] == "team-pilot"
    assert data["module_name"] == "team_pilot"
    assert data["action_ids"] == ["team.battery_check", "team.demo_toggle"]
    assert (tmp_path / "team-pilot" / "src" / "team_pilot" / "toggle.py").is_file()
    contract = build_cli_schema()["commands"]["plugins"]["subcommands"]["init"]
    assert contract["side_effect"] == "local_file"
    assert set(data) == set(contract["result_schema"]["required"])
    assert transport.calls == []
    assert not (tmp_path / ".state").exists()


def test_plugin_init_returns_configuration_error_for_existing_destination(tmp_path):
    destination = tmp_path / "team-pilot"
    destination.mkdir()
    existing = destination / "README.md"
    existing.write_text("Existing community project", encoding="utf-8")
    code, out, err = _run_cli(
        ["plugins", "init", str(destination), "--namespace", "team", "--json"], env={},
    )
    assert code == 2 and err == ""
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert existing.read_text(encoding="utf-8") == "Existing community project"
    assert sorted(path.name for path in destination.iterdir()) == ["README.md"]


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
        pytest.param('{"max_elements":NaN}', id="nonfinite-number"),
        pytest.param('{"max_elements":1,"max_elements":2}', id="duplicate-key"),
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


@pytest.mark.parametrize(
    "status,code",
    [
        pytest.param("human_required", 4, id="manual-handoff"),
        pytest.param("device_offline", 3, id="offline-device"),
    ],
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
