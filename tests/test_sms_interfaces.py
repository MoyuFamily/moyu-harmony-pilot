"""Offline CLI/MCP SMS contracts with the shared reader replaced at its public boundary."""

import asyncio
import json
import sys
from unittest.mock import Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from moyu_harmony_pilot.actions import ActionOutcome, ActionRegistry, sms
from moyu_harmony_pilot.cli import build_cli_schema, build_parser
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceOverheatedError,
    DeviceProfile,
    DeviceRegistry,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import (
    CommandResult,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcClient,
    HdcCommandError,
    HdcTimeoutError,
)
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.server import create_server, main
from moyu_harmony_pilot.testing import offline_guard
from moyu_harmony_pilot.ui import UiConcurrentCallError, UiParseError
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_cli import _parse_single_json_envelope, _run_cli
from tests.test_mcp import invoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    monkeypatch.setenv("HARMONY_PILOT_PLUGIN_PATHS", "")
    with offline_guard():
        yield


@pytest.fixture
def sms_setup(tmp_path):
    state = FakeDeviceState("SYNTHETIC_SMS_INTERFACE_DEVICE")
    transport = FakeHdcTransport([state])
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": state.serial}}), encoding="utf-8")
    config.chmod(0o600)
    facade = PilotTools(
        HdcClient(transport=transport), DeviceRegistry([DeviceProfile("phone", state.serial)]),
        ActionRegistry(), state_dir=tmp_path / "state",
    )
    flags = ["--config", str(config), "--state-dir", str(facade.state_dir), "--json"]
    return flags, facade, transport


@pytest.fixture
def sms_reader(monkeypatch):
    reader = Mock(return_value={
        "alias": "phone",
        "status": "succeeded",
        "page": "thread",
        "messages": [{
            "sender": "Synthetic sender",
            "body": "合成测试短信，验证码 314159。",
            "time_text": "10:00",
            "verification_codes": ["314159"],
            "source": "synthetic",
        }],
        "truncated": False,
        "observed_at": "2026-11-25T02:00:00Z",
        "reason": "snapshot",
    })
    monkeypatch.setattr(sms, "read_sms", reader)
    return reader


@pytest.mark.parametrize("options", [
    {},
    {"limit": 1, "sender": "Synthetic sender", "wait_seconds": 12.5, "timeout": 20.5},
    {"limit": 50, "sender": "测" * 128, "wait_seconds": 300, "timeout": 3600},
])
@pytest.mark.parametrize("reason", ["snapshot", "new_message", "timeout"])
def test_cli_sms_read_forwards_options_and_opaque_results(sms_setup, sms_reader, options, reason):
    flags, facade, transport = sms_setup
    sms_reader.return_value["reason"] = reason
    if reason == "timeout":
        sms_reader.return_value["messages"] = []
    argv = [*flags, "sms", "read", "--device", "phone"]
    for name, value in options.items():
        argv.extend(["--" + name.replace("_", "-"), str(value)])
    code, out, err = _run_cli(argv, transport=transport, env={})
    assert code == 0 and err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["ok"] and payload["command"] == "sms"
    assert payload["data"] == sms_reader.return_value
    assert payload["error"] is None
    sms_reader.assert_called_once()
    device = sms_reader.call_args.args[0]
    assert isinstance(device, HarmonyDevice) and device.alias == "phone"
    assert device.lock_dir == facade.state_dir / "locks"
    assert sms_reader.call_args.kwargs == {
        "limit": 10, "sender": None, "wait_seconds": 0, "timeout": 30.0, **options,
    }
    assert [args for _, args, _ in transport.calls] == [("list", "targets", "-v")]
    assert device.profile.serial not in out


@pytest.mark.parametrize("flags", [
    ["--limit", "0"], ["--limit", "51"], ["--limit", "1.5"],
    ["--sender", ""], ["--sender", " \t\n"], ["--sender", "s" * 129],
    ["--wait-seconds", "-1"], ["--wait-seconds", "301"],
    ["--wait-seconds", "nan"], ["--wait-seconds", "inf"],
    ["--timeout", "0"], ["--timeout", "-1"], ["--timeout", "3601"],
    ["--timeout", "nan"], ["--timeout", "inf"],
    ["--wait-seconds", "30"],
    ["--wait-seconds", "5", "--timeout", "5"],
    ["--wait-seconds", "6", "--timeout", "5"],
])
def test_cli_sms_invalid_options_fail_before_device_access(sms_setup, sms_reader, flags):
    base, facade, transport = sms_setup
    code, out, err = _run_cli([*base, "sms", "read", *flags], transport=transport, env={})
    assert code == 2 and err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["error"]["code"] == "configuration_error"
    assert payload["data"] is None
    assert transport.calls == []
    sms_reader.assert_not_called()
    assert not facade.state_dir.exists()


@pytest.mark.parametrize("operation", ["read", "open"])
def test_sms_cli_schema_matches_registered_flags(operation):
    code, out, err = _run_cli(["schema", "--json"], env={})
    assert code == 0 and err == ""
    schema = _parse_single_json_envelope(out)["data"]
    contract = schema["commands"]["sms"]["subcommands"][operation]
    parser = build_parser()
    commands = next(
        action.choices for action in parser._actions if isinstance(action.choices, dict)
    )
    subcommands = next(
        action.choices for action in commands["sms"]._actions if isinstance(action.choices, dict)
    )
    flags = {flag for action in subcommands[operation]._actions for flag in action.option_strings}
    shared = set(schema["global_options"]) - {"--version"}
    assert flags == set(contract["options"]) | shared | {"-h", "--help"}
    assert contract["side_effect"] == ("readonly" if operation == "read" else "mutating")
    if operation == "read":
        assert contract["private_content"] is True
        assert contract["scope"] == "current-screen"
        assert "less than --timeout" in contract["options"]["--wait-seconds"]["description"]
    else:
        assert contract["options"]["--idempotency-key"]["required"] is True


def test_sms_mcp_is_absent_and_rejected_by_default(sms_setup, sms_reader):
    _, facade, transport = sms_setup
    server = create_server(facade)
    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "devices", "health", "dump_ui", "screenshot", "list_actions", "run_action",
    }
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("sms_read", {"alias": "phone"}))
    with pytest.raises(DeviceConfigurationError, match="--enable-sms"):
        facade.sms_read("unconfigured-alias", sender="", timeout=0)
    sms_reader.assert_not_called()
    assert transport.calls == []


@pytest.mark.parametrize("enabled", [None, 0, 1, "true", [], {}])
def test_sms_mcp_startup_switch_is_strict(sms_setup, enabled):
    _, facade, transport = sms_setup
    with pytest.raises(DeviceConfigurationError):
        PilotTools(facade.hdc, facade.devices, facade.actions, enable_sms=enabled)
    assert transport.calls == []


def test_sms_mcp_schema_matches_cli_contract(sms_setup):
    _, facade, transport = sms_setup
    facade.enable_sms = True
    tools = {tool.name: tool for tool in asyncio.run(create_server(facade).list_tools())}
    assert set(tools) == {
        "devices", "health", "dump_ui", "screenshot", "list_actions", "run_action", "sms_read",
    }
    tool = tools["sms_read"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.model_dump(by_alias=True)["_meta"] == {
        "private_content": True, "scope": "current-screen",
        "result_schema": sms.SMS_RESULT_SCHEMA,
    }
    assert tool.input_schema["required"] == ["alias"]
    properties = tool.input_schema["properties"]
    assert set(properties) == {"alias", "limit", "sender", "wait_seconds", "timeout"}
    cli_options = build_cli_schema()["commands"]["sms"]["subcommands"]["read"]["options"]
    for name in ("limit", "wait_seconds", "timeout"):
        cli_field = cli_options["--" + name.replace("_", "-")]
        for key in ("default", "minimum", "exclusiveMinimum", "maximum"):
            assert properties[name].get(key) == cli_field.get(key)
    sender = next(field for field in properties["sender"]["anyOf"] if field["type"] == "string")
    assert sender["minLength"] == 1 and sender["maxLength"] == 128
    assert properties["sender"]["default"] is None
    assert transport.calls == []


@pytest.mark.parametrize("options", [
    {},
    {"limit": 1, "sender": "Synthetic sender", "wait_seconds": 12.5, "timeout": 20.5},
    {"limit": 50, "sender": "测" * 128, "wait_seconds": 300, "timeout": 3600},
])
@pytest.mark.parametrize("page", ["conversations", "thread"])
def test_sms_mcp_forwards_options_and_opaque_results(sms_setup, sms_reader, options, page, capsys):
    _, facade, transport = sms_setup
    facade.enable_sms = True
    sms_reader.return_value["page"] = page
    payload = invoke(create_server(facade), "sms_read", {"alias": "phone", **options})
    assert payload == {"ok": True, "data": sms_reader.return_value, "error": None}
    sms_reader.assert_called_once()
    device = sms_reader.call_args.args[0]
    assert isinstance(device, HarmonyDevice) and device.alias == "phone"
    assert sms_reader.call_args.kwargs == {
        "limit": 10, "sender": None, "wait_seconds": 0, "timeout": 30.0, **options,
    }
    assert [args for _, args, _ in transport.calls] == [("list", "targets", "-v")]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("options", [
    {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": 1.5}, {"limit": "1"},
    {"sender": ""}, {"sender": "s" * 129}, {"sender": 123},
    {"wait_seconds": -1}, {"wait_seconds": 301}, {"wait_seconds": True},
    {"wait_seconds": "1"}, {"wait_seconds": float("nan")},
    {"wait_seconds": float("inf")},
    {"timeout": 0}, {"timeout": -1}, {"timeout": 3601}, {"timeout": True},
    {"timeout": "30"}, {"timeout": float("nan")}, {"timeout": float("inf")},
])
def test_sms_mcp_invalid_fields_fail_before_device_access(sms_setup, sms_reader, options):
    _, facade, transport = sms_setup
    facade.enable_sms = True
    with pytest.raises(DeviceConfigurationError):
        facade.sms_read("phone", **options)
    with pytest.raises(ToolError):
        asyncio.run(create_server(facade).call_tool("sms_read", {"alias": "phone", **options}))
    assert transport.calls == []
    sms_reader.assert_not_called()


@pytest.mark.parametrize("options", [
    {"sender": " \t\n"}, {"wait_seconds": 30},
    {"wait_seconds": 5, "timeout": 5}, {"wait_seconds": 6, "timeout": 5},
])
def test_sms_mcp_cross_field_validation_uses_error_envelope(sms_setup, sms_reader, options):
    _, facade, transport = sms_setup
    facade.enable_sms = True
    with pytest.raises(DeviceConfigurationError):
        facade.sms_read("phone", **options)
    payload = invoke(create_server(facade), "sms_read", {"alias": "phone", **options})
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == "configuration_error"
    assert transport.calls == []
    sms_reader.assert_not_called()


@pytest.mark.parametrize("surface", ["cli-json", "cli-text", "mcp"])
@pytest.mark.parametrize("failure", [False, True])
def test_sms_private_text_is_confined_to_success_results(
    sms_setup, sms_reader, surface, failure, capsys,
):
    flags, facade, transport = sms_setup
    facade.enable_sms = True
    body = sms_reader.return_value["messages"][0]["body"]

    def noisy_read(*args, **kwargs):
        print(body)
        print(body, file=sys.stderr)
        if failure:
            raise RuntimeError(body)
        return sms_reader.return_value

    sms_reader.side_effect = noisy_read
    if surface == "mcp":
        payload = invoke(create_server(facade), "sms_read", {"alias": "phone"})
        assert payload["ok"] is not failure
        if failure:
            assert body not in json.dumps(payload, ensure_ascii=False)
        else:
            assert payload["data"] == sms_reader.return_value
    else:
        if surface == "cli-text":
            flags = [flag for flag in flags if flag != "--json"]
        code, out, err = _run_cli([*flags, "sms", "read"], transport=transport, env={})
        assert code == (1 if failure else 0)
        assert body not in err
        if failure:
            assert body not in out
        elif surface == "cli-json":
            assert _parse_single_json_envelope(out)["data"] == sms_reader.return_value
        else:
            assert json.loads(out) == sms_reader.return_value
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("error_type,exit_code", [
    (DeviceConfigurationError, 2), (DeviceOfflineError, 3), (DeviceUnauthorizedError, 3),
    (HumanRequiredError, 4), (DeviceOverheatedError, 5), (DeviceBusyError, 6),
    (UiConcurrentCallError, 6), (UiParseError, 1), (HdcTimeoutError, 1), (HdcCommandError, 1),
])
def test_sms_errors_preserve_semantics_without_private_content(
    sms_setup, sms_reader, error_type, exit_code,
):
    flags, facade, transport = sms_setup
    facade.enable_sms = True
    body = sms_reader.return_value["messages"][0]["body"]
    error = error_type(body, hint=body)
    if isinstance(error, HdcCommandError):
        error.result = CommandResult(("shell",), 1, body, body, 1)
    sms_reader.side_effect = error
    code, out, err = _run_cli([*flags, "sms", "read"], transport=transport, env={})
    assert code == exit_code and err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["data"] is None
    assert payload["error"]["code"] == error.code
    assert payload["error"]["retryable"] == error.retryable
    assert body not in out + err
    payload = invoke(create_server(facade), "sms_read", {"alias": "phone"})
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == error.code
    assert payload["error"]["retryable"] == error.retryable
    assert body not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("error_type,error_code,exit_code,hint_parts", [
    (HumanRequiredError, "human_required", 4,
     ("Unlock the device", "sms open", "idempotency key", "run_action", "sms.open")),
    (DeviceError, "sms_layout_unsupported", 1,
     ("supported system SMS conversation list", "conversation detail page")),
    (DeviceError, "sms_view_changed", 1,
     ("new SMS read", "current page", "fresh message baseline")),
])
def test_sms_common_errors_return_controlled_recovery_hints(
    sms_setup, sms_reader, error_type, error_code, exit_code, hint_parts,
):
    flags, facade, transport = sms_setup
    facade.enable_sms = True
    body = sms_reader.return_value["messages"][0]["body"]
    sms_reader.side_effect = error_type(body, code=error_code, hint=body)
    code, out, err = _run_cli([*flags, "sms", "read"], transport=transport, env={})
    assert code == exit_code and err == ""
    cli_error = _parse_single_json_envelope(out)["error"]
    assert cli_error["code"] == error_code
    assert all(part in cli_error["hint"] for part in hint_parts)
    assert body not in out + err

    text_flags = [flag for flag in flags if flag != "--json"]
    code, out, err = _run_cli([*text_flags, "sms", "read"], transport=transport, env={})
    assert code == exit_code and out == ""
    assert cli_error["hint"] in err
    assert body not in err

    payload = invoke(create_server(facade), "sms_read", {"alias": "phone"})
    assert payload["data"] is None
    assert payload["error"] == cli_error
    assert body not in json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def sms_open_registry(monkeypatch):
    registry = ActionRegistry.discover(env={}, allowed_actions=["sms.open"])
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    return registry


@pytest.mark.parametrize("status,exit_code", [
    ("succeeded", 0), ("already_done", 0), ("failed", 1), ("unknown", 1),
    ("configuration_error", 2), ("device_offline", 3), ("human_required", 4),
    ("device_overheated", 5),
])
def test_cli_sms_open_uses_action_runner(
    sms_setup, sms_open_registry, monkeypatch, status, exit_code,
):
    flags, _, transport = sms_setup
    outcome = ActionOutcome(
        status=status, summary="Synthetic SMS open result.",
        metrics={"ready": status in {"succeeded", "already_done"}},
    )
    run = Mock(return_value=outcome)
    monkeypatch.setattr("moyu_harmony_pilot.cli.operations.ActionRunner.run", run)
    code, out, err = _run_cli(
        [*flags, "sms", "open", "--device", "phone",
         "--idempotency-key", "synthetic-open-key", "--timeout", "45"],
        transport=transport, env={},
    )
    assert code == exit_code and err == ""
    payload = _parse_single_json_envelope(out)
    assert payload["data"] == {
        "alias": "phone", "action": "sms.open", "params": {}, **outcome.to_dict(),
    }
    assert payload["ok"] == (exit_code == 0)
    run.assert_called_once()
    assert run.call_args.args[0].alias == "phone"
    assert run.call_args.args[1:] == ("sms.open", {})
    assert run.call_args.kwargs == {"idempotency_key": "synthetic-open-key", "timeout": 45}
    assert "synthetic-open-key" not in out
    assert [args for _, args, _ in transport.calls] == [("list", "targets", "-v")]


@pytest.mark.parametrize("flags", [
    [], ["--idempotency-key", ""], ["--idempotency-key", "k" * 257],
    ["--idempotency-key", "key", "--timeout", "0"],
    ["--idempotency-key", "key", "--timeout", "-1"],
    ["--idempotency-key", "key", "--timeout", "3601"],
    ["--idempotency-key", "key", "--timeout", "nan"],
])
def test_cli_sms_open_invalid_options_fail_before_device_access(
    sms_setup, sms_open_registry, flags,
):
    base, _, transport = sms_setup
    code, out, err = _run_cli([*base, "sms", "open", *flags], transport=transport, env={})
    assert code == 2 and err == ""
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert transport.calls == []


def test_mcp_sms_open_uses_existing_action_allowlist(sms_setup, sms_open_registry, monkeypatch):
    _, base, transport = sms_setup
    denied = invoke(create_server(base), "run_action", {
        "alias": "phone", "action_id": "sms.open", "params": {},
        "idempotency_key": "synthetic-open-key",
    })
    assert denied["error"]["code"] == "configuration_error"
    assert transport.calls == []
    facade = PilotTools(base.hdc, base.devices, sms_open_registry, state_dir=base.state_dir)
    run = Mock(return_value=ActionOutcome(
        summary="Synthetic SMS open result.", metrics={"ready": True},
    ))
    monkeypatch.setattr(facade.runner, "run", run)
    server = create_server(facade)
    assert "sms_read" not in {tool.name for tool in asyncio.run(server.list_tools())}
    payload = invoke(server, "run_action", {
        "alias": "phone", "action_id": "sms.open", "params": {},
        "idempotency_key": "synthetic-open-key", "timeout": 45,
    })
    assert payload["ok"] and payload["data"]["action"] == "sms.open"
    assert payload["data"]["metrics"] == {"ready": True}
    run.assert_called_once()
    assert run.call_args.args[1:] == ("sms.open", {})
    assert run.call_args.kwargs == {
        "remote": True, "idempotency_key": "synthetic-open-key", "timeout": 45,
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_sms_mcp_startup_flag_reaches_adapter(sms_setup, monkeypatch, enabled):
    flags, facade, transport = sms_setup
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr("moyu_harmony_pilot.mcp.server.create_server", factory)
    monkeypatch.setattr("moyu_harmony_pilot.mcp.server.HdcClient", lambda **kwargs: facade.hdc)
    argv = [flag for flag in flags if flag != "--json"]
    if enabled:
        argv.append("--enable-sms")
    assert main(argv) == 0
    configured = factory.call_args.args[0]
    assert configured.enable_sms is enabled
    assert configured.enable_ocr is False and configured.local_lab is False
    server.run.assert_called_once_with(transport="stdio")
    assert transport.calls == []
