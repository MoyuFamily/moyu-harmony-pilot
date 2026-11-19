"""Optional MCP SDK integration using fake devices and real local stdio transport."""

import asyncio
import io
import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.device import DeviceConfigurationError, DeviceProfile, DeviceRegistry
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.server import create_server
from tests.fakes import FakeDeviceState, FakeHdcTransport, make_second_page_layout_json
from tests.test_actions import SampleMutation


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    image = io.BytesIO()
    Image.new("RGB", (1260, 2720), "white").save(image, format="PNG")
    state = FakeDeviceState("SYNTHETIC_MCP_DEVICE", png_bytes=image.getvalue())
    transport = FakeHdcTransport([state])
    devices = DeviceRegistry([DeviceProfile("phone", state.serial)])
    actions = ActionRegistry.discover(env={}, allowed_actions=["device.health", "example.mutate"])
    plugin = SampleMutation()
    actions.register(plugin)
    facade = PilotTools(HdcClient(transport=transport), devices, actions, state_dir=tmp_path)
    return facade, state, transport, plugin


def invoke(server, tool, arguments=None):
    result = asyncio.run(server.call_tool(tool, arguments or {}))
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


def test_safe_tools_readonly_observation_and_allowlist(adapter):
    facade, state, transport, _ = adapter
    server = create_server(facade)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "devices",
        "health",
        "dump_ui",
        "screenshot",
        "list_actions",
        "run_action",
    }
    assert invoke(server, "devices")["data"]["devices"][0]["alias"] == "phone"
    assert state.serial not in json.dumps(invoke(server, "devices"))
    assert invoke(server, "health", {"alias": "phone"})["ok"]
    assert invoke(server, "dump_ui", {"alias": "phone"})["data"]["snapshot"]["elements"]
    result = invoke(server, "screenshot", {"alias": "phone", "som": True, "max_width": 630})
    assert result["ok"]
    assert result["data"]["width"] == 630
    assert Path(result["data"]["path"]).is_relative_to(facade.state_dir / "diagnostics")
    assert invoke(
        server, "run_action", {"alias": "phone", "action_id": "device.health", "params": {}}
    )["ok"]
    denied = invoke(
        server, "run_action", {"alias": "phone", "action_id": "ui.inspect", "params": {}}
    )
    assert denied["error"]["code"] == "configuration_error"
    with pytest.raises(DeviceConfigurationError):
        facade.click_ref("phone", "@e1", "hash")
    assert not transport.all_remote_tmp_files()


def test_empty_allowlist_and_missing_sdk_fail_closed(adapter, monkeypatch):
    facade, _, transport, _ = adapter
    facade.actions = ActionRegistry.discover(env={})
    from moyu_harmony_pilot.actions import ActionRunner

    facade.runner = ActionRunner(facade.actions, state_dir=facade.state_dir)
    with pytest.raises(DeviceConfigurationError):
        facade.run_action("phone", "device.health", {})
    assert transport.calls == []
    monkeypatch.setitem(sys.modules, "mcp.server", None)
    with pytest.raises(DeviceConfigurationError, match="Install"):
        create_server(facade)


def test_mcp_mutation_reuses_shared_ledger_and_never_leaks_errors(adapter):
    facade, _, _, plugin = adapter
    plugin.submit_error = RuntimeError("PRIVATE_DATA")
    server = create_server(facade)
    arguments = {
        "alias": "phone",
        "action_id": "example.mutate",
        "params": {},
        "idempotency_key": "mcp-key",
    }
    for _ in range(2):
        result = invoke(server, "run_action", arguments)
        assert not result["ok"]
        assert result["data"]["status"] == "unknown"
        assert "PRIVATE_DATA" not in json.dumps(result)
    assert plugin.calls.count("submit") == 1


def test_lab_session_tools_differences_and_fixed_budget(adapter):
    facade, state, transport, _ = adapter
    facade.local_lab = True
    facade.max_steps = 3
    server = create_server(facade)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"lab_observe", "click_ref", "swipe", "back"} <= names
    before = invoke(server, "lab_observe", {"alias": "phone"})["data"]["snapshot"]
    ref = next(element["ref"] for element in before["elements"] if element.get("clickable"))
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_click = True
    result = invoke(
        server, "click_ref", {"alias": "phone", "ref": ref, "layout_hash": before["layout_hash"]}
    )
    assert result["data"]["changed"]
    invoke(server, "lab_observe", {"alias": "phone"})
    exhausted = invoke(server, "lab_observe", {"alias": "phone"})
    assert exhausted["error"]["code"] == "action_budget_exceeded"
    assert not transport.all_remote_tmp_files()


def test_sensitive_lab_input_and_uncertain_step_require_observation(adapter):
    facade, state, _, _ = adapter
    facade.local_lab = True
    server = create_server(facade)
    before = invoke(server, "lab_observe", {"alias": "phone"})["data"]["snapshot"]
    state.timeout_on_cmd_prefix = "uitest uiInput"
    arguments = {"alias": "phone", "layout_hash": before["layout_hash"]}
    assert invoke(server, "back", arguments)["error"]["code"] == "unknown"
    assert invoke(server, "back", arguments)["error"]["code"] == "configuration_error"
    state.timeout_on_cmd_prefix = None
    layout = json.loads(state.layouts[0])
    layout["attributes"]["text"] = "Confirm pay"
    state.layouts = [json.dumps(layout)]
    observed = invoke(server, "lab_observe", {"alias": "phone"})["data"]["snapshot"]
    result = invoke(server, "back", {"alias": "phone", "layout_hash": observed["layout_hash"]})
    assert result["error"]["code"] == "human_required"
    assert result["error"]["hint"]


def test_real_stdio_mcp_handshake_tools_and_invocation(tmp_path):
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": "SYNTHETIC_STDIO_DEVICE"}}))
    config.chmod(0o600)
    fake_hdc = tmp_path / "fake_hdc"
    fake_hdc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:] == ['list', 'targets', '-v']:\n"
        "    print('SYNTHETIC_STDIO_DEVICE\\tUSB\\tConnected\\tlocalhost')\n"
        "elif 'shell' in sys.argv:\n"
        "    print('6.0.2.3')\n",
    )
    fake_hdc.chmod(0o700)

    async def exercise():
        with anyio.fail_after(15):
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "moyu_harmony_pilot.mcp",
                    "--config",
                    str(config),
                    "--hdc-path",
                    str(fake_hdc),
                    "--state-dir",
                    str(tmp_path / "state"),
                    "--allow-action",
                    "device.ping",
                ],
                env={"HARMONY_PILOT_PLUGIN_PATHS": ""},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "harmony-pilot"
                    listed = await session.list_tools()
                    assert "click_ref" not in {tool.name for tool in listed.tools}
                    devices = await session.call_tool("devices", {})
                    assert not devices.is_error
                    assert devices.structured_content["data"]["devices"][0]["alias"] == "phone"
                    result = await session.call_tool(
                        "run_action",
                        {
                            "alias": "phone",
                            "action_id": "device.ping",
                            "params": {},
                        },
                    )
                    assert not result.is_error, result
                    assert result.structured_content["data"]["status"] == "succeeded"

    asyncio.run(exercise())
