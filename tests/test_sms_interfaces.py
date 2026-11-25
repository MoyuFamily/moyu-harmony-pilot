"""Offline CLI/MCP SMS contracts with the shared reader replaced at its public boundary."""

import asyncio
import json
import sys
from unittest.mock import Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from moyu_harmony_pilot.actions import ActionRegistry, sms
from moyu_harmony_pilot.cli import build_cli_schema
from moyu_harmony_pilot.device import (
    DeviceProfile,
    DeviceRegistry,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import DeviceUnauthorizedError, HdcClient
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.server import create_server
from moyu_harmony_pilot.testing import offline_guard
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


@pytest.mark.parametrize(
    "options, reason",
    [({"limit": 1, "sender": "Synthetic sender", "wait_seconds": 12.5, "timeout": 20.5},
      "new_message")],
    ids=["filtered-new-message"],
)
def test_cli_sms_read_forwards_options_and_opaque_results(sms_setup, sms_reader, options, reason):
    flags, facade, transport = sms_setup
    sms_reader.return_value["reason"] = reason
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


@pytest.mark.parametrize(
    "flags", [["--wait-seconds", "5", "--timeout", "5"]],
    ids=["wait-exhausts-timeout"],
)
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


def test_sms_mcp_is_absent_and_rejected_by_default(sms_setup, sms_reader):
    _, facade, transport = sms_setup
    server = create_server(facade)
    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "devices", "health", "dump_ui", "screenshot", "list_actions", "run_action",
    }
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("sms_read", {"alias": "phone"}))
    sms_reader.assert_not_called()
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


@pytest.mark.parametrize(
    "options, page",
    [({"limit": 1, "sender": "Synthetic sender", "wait_seconds": 12.5, "timeout": 20.5},
      "thread")],
    ids=["filtered-thread"],
)
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


@pytest.mark.parametrize(
    "options", [{"wait_seconds": 5, "timeout": 5}],
    ids=["wait-exhausts-timeout"],
)
def test_sms_mcp_cross_field_validation_uses_error_envelope(sms_setup, sms_reader, options):
    _, facade, transport = sms_setup
    facade.enable_sms = True
    payload = invoke(create_server(facade), "sms_read", {"alias": "phone", **options})
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == "configuration_error"
    assert transport.calls == []
    sms_reader.assert_not_called()


@pytest.mark.parametrize(
    "surface, failure", [("cli-json", False), ("mcp", True)],
    ids=["cli-success-with-noisy-reader", "mcp-failure-with-noisy-reader"],
)
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
        assert payload["ok"] is False
        assert payload["data"] is None
        assert body not in json.dumps(payload, ensure_ascii=False)
    else:
        code, out, err = _run_cli([*flags, "sms", "read"], transport=transport, env={})
        assert code == 0 and err == ""
        assert _parse_single_json_envelope(out)["data"] == sms_reader.return_value
    sms_reader.assert_called_once()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "surface, error_type",
    [("cli-json", HumanRequiredError), ("mcp", DeviceUnauthorizedError)],
    ids=["cli-human-required", "mcp-unauthorized"],
)
def test_sms_errors_preserve_semantics_without_private_content(
    sms_setup, sms_reader, surface, error_type,
):
    flags, facade, transport = sms_setup
    facade.enable_sms = True
    body = sms_reader.return_value["messages"][0]["body"]
    error = error_type(body, hint=body)
    sms_reader.side_effect = error
    if surface == "cli-json":
        code, out, err = _run_cli([*flags, "sms", "read"], transport=transport, env={})
        assert code == 4 and err == ""
        payload = _parse_single_json_envelope(out)
        assert "manually" in payload["error"]["hint"]
        assert "sms.open" in payload["error"]["hint"]
        assert body not in out + err
    else:
        payload = invoke(create_server(facade), "sms_read", {"alias": "phone"})
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == error.code
    assert payload["error"]["retryable"] == error.retryable
    assert body not in json.dumps(payload, ensure_ascii=False)
    sms_reader.assert_called_once()
    assert [args for _, args, _ in transport.calls] == [("list", "targets", "-v")]
