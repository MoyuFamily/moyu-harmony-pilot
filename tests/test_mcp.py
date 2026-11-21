"""Optional MCP SDK integration using fake devices and real local stdio transport."""

import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceProfile,
    DeviceRegistry,
    HarmonyDevice,
)
from moyu_harmony_pilot.hdc import (
    CommandResult,
    DeviceOfflineError,
    HdcClient,
    HdcCommandError,
    HdcTimeoutError,
)
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.adapter import MAX_IMAGE_BYTES
from moyu_harmony_pilot.mcp.server import create_server, main
from moyu_harmony_pilot.ui import UiError
from moyu_harmony_pilot.vision import TextBox, VisionError, VisionUnavailableError
from tests.fakes import FakeDeviceState, FakeHdcTransport, make_second_page_layout_json
from tests.test_actions import SampleMutation

AdapterFixture = tuple[PilotTools, FakeDeviceState, FakeHdcTransport, SampleMutation]


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterFixture:
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


def invoke(
    server: MCPServer, tool: str, arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(tool, arguments or {}))
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    assert json.loads(result.content[0].text) == result.structured_content
    payload: dict[str, Any] = result.structured_content
    return payload


def test_safe_tools_readonly_observation_and_allowlist(adapter: AdapterFixture) -> None:
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
    assert all(
        action["api_version"] == 1
        for action in invoke(server, "list_actions")["data"]["actions"]
    )
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


@pytest.mark.parametrize(
    "error",
    [
        DeviceBusyError("Device is busy."),
        DeviceOfflineError("Device is offline."),
        HdcTimeoutError("hdc request timed out."),
        UiError("Observe the UI again.", code="layout_changed", retryable=True),
        DeviceConfigurationError("Reference must select an enabled clickable element."),
        VisionError("Invalid SoM rectangle."),
    ],
)
def test_tool_error_preserves_controlled_details_and_retryability(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
    error: DeviceError | UiError | HdcTimeoutError | DeviceOfflineError | VisionError,
) -> None:
    facade = adapter[0]

    def fail(alias: str) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(facade, "health", fail)
    result = invoke(create_server(facade), "health", {"alias": "phone"})
    assert result == {
        "ok": False,
        "data": None,
        "error": {
            "code": error.code,
            "message": str(error),
            "hint": error.hint,
            "retryable": error.retryable,
        },
    }


def test_unknown_tool_errors_hide_uncontrolled_text_and_attributes(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UncontrolledError(RuntimeError):
        code = "PRIVATE_ERROR_CODE"
        hint = "PRIVATE_ERROR_HINT"
        retryable = True

    error = UncontrolledError("PRIVATE_ERROR_TEXT")

    def fail(alias: str) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(adapter[0], "health", fail)
    result = invoke(create_server(adapter[0]), "health", {"alias": "phone"})
    assert result["error"]["code"] == "internal_error"
    assert result["error"]["message"] == "Tool execution stopped."
    assert not result["error"]["retryable"]
    assert "PRIVATE_ERROR" not in json.dumps(result)


def test_hdc_tool_error_retains_retryability_without_raw_subprocess_output(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = CommandResult(
        args=("shell",), returncode=1, stdout="PRIVATE_OUTPUT", stderr="", duration_ms=1
    )
    error = HdcCommandError("hdc failed (PRIVATE_OUTPUT)", result=raw, retryable=True)

    def fail(alias: str) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(adapter[0], "health", fail)
    result = invoke(create_server(adapter[0]), "health", {"alias": "phone"})
    assert result["error"]["code"] == "hdc_command_failed"
    assert result["error"]["retryable"]
    assert "PRIVATE_OUTPUT" not in json.dumps(result)


def test_real_device_busy_and_offline_errors_are_retryable(adapter: AdapterFixture) -> None:
    facade, state, _, _ = adapter
    server = create_server(facade)
    with facade._device("phone").acquire_lock():
        result = invoke(server, "health", {"alias": "phone"})
        assert result["error"]["code"] == "device_busy"
        assert result["error"]["retryable"]
    state.status = "Offline"
    result = invoke(server, "health", {"alias": "phone"})
    assert result["error"]["code"] == "device_offline"
    assert result["error"]["retryable"]


def test_empty_allowlist_and_missing_sdk_fail_closed(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_mcp_mutation_reuses_shared_ledger_and_never_leaks_errors(adapter: AdapterFixture) -> None:
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


def test_lab_session_tools_differences_and_fixed_budget(adapter: AdapterFixture) -> None:
    facade, state, transport, _ = adapter
    facade.local_lab = True
    facade.max_steps = 3
    server = create_server(facade)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "lab_observe", "click_ref", "swipe", "back", "home", "input_text_ref",
        "long_click_ref", "start_app", "wait_for", "scroll_until",
    } <= names
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


def test_sensitive_lab_input_and_uncertain_step_require_observation(
    adapter: AdapterFixture,
) -> None:
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


def test_new_lab_tools_use_real_shared_session(adapter: AdapterFixture) -> None:
    facade, state, _, _ = adapter
    facade.local_lab = True
    facade.max_steps = 20
    state.layouts = [json.dumps({
        "attributes": {"type": "Root", "bounds": "[0,0][400,800]"},
        "children": [{"attributes": {
            "type": "TextInput", "id": "search", "bounds": "[10,20][390,100]",
            "clickable": "true", "longClickable": "true", "enabled": "true",
        }}],
    })]
    server = create_server(facade)
    snapshot = invoke(server, "lab_observe", {"alias": "phone"})["data"]["snapshot"]
    element = next(item for item in snapshot["elements"] if item.get("id") == "search")
    arguments = {"alias": "phone", "layout_hash": snapshot["layout_hash"]}
    assert invoke(server, "input_text_ref", {**arguments, "ref": element["ref"],
                                              "text": "中文查询"})["ok"]
    assert invoke(server, "long_click_ref", {**arguments, "ref": element["ref"]})["ok"]
    assert invoke(server, "home", arguments)["ok"]
    assert invoke(server, "start_app", {**arguments, "bundle_name": "com.example.demo",
                                         "ability_name": "EntryAbility"})["ok"]
    assert invoke(server, "wait_for", {"alias": "phone", "selector": {"id": "search"}})["ok"]
    assert invoke(server, "scroll_until", {"alias": "phone", "selector": {"id": "search"},
                                            "max_swipes": 0})["ok"]


def test_real_stdio_mcp_handshake_tools_and_invocation(tmp_path: Path) -> None:
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": "SYNTHETIC_STDIO_DEVICE"}}))
    config.chmod(0o600)
    png_path = tmp_path / "synthetic.png"
    Image.new("RGB", (64, 128), "white").save(png_path)
    fake_hdc = tmp_path / "fake_hdc"
    fake_hdc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['list', 'targets', '-v']:\n"
        "    print('SYNTHETIC_STDIO_DEVICE\\tUSB\\tConnected\\tlocalhost')\n"
        "elif 'file' in sys.argv and 'recv' in sys.argv:\n"
        "    remote, local = sys.argv[-2:]\n"
        "    if remote.endswith('.json'):\n"
        "        Path(local).write_text(json.dumps({'attributes': {\n"
        "            'type': 'Text', 'text': 'Offline test', 'bounds': '[0,0][64,128]'}}))\n"
        "    else:\n"
        f"        Path(local).write_bytes(Path({str(png_path)!r}).read_bytes())\n"
        "    print('FileTransfer finish')\n"
        "elif 'shell' in sys.argv:\n"
        "    print('6.0.2.3')\n",
    )
    fake_hdc.chmod(0o700)

    async def exercise() -> None:
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
                    "--inline-images",
                    "--max-image-bytes",
                    "2097152",
                    "--enable-ocr",
                ],
                env={"HARMONY_PILOT_PLUGIN_PATHS": ""},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "harmony-pilot"
                    listed = await session.list_tools()
                    assert "click_ref" not in {tool.name for tool in listed.tools}
                    assert "ocr" in {tool.name for tool in listed.tools}
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
                    screenshot = await session.call_tool(
                        "screenshot", {"alias": "phone", "inline": True},
                    )
                    assert not screenshot.is_error
                    assert isinstance(screenshot.content[0], TextContent)
                    assert isinstance(screenshot.content[1], ImageContent)
                    assert base64.b64decode(screenshot.content[1].data) == png_path.read_bytes()
                    assert json.loads(screenshot.content[0].text) == screenshot.structured_content
                    assert screenshot.structured_content["data"]["width"] == 64

    asyncio.run(exercise())


@pytest.mark.parametrize("enabled", [False, True])
def test_screenshot_defaults_to_json_metadata_even_when_inline_enabled(
    adapter: AdapterFixture, enabled: bool,
) -> None:
    facade, _, _, _ = adapter
    facade.inline_images = enabled
    response = asyncio.run(create_server(facade).call_tool("screenshot", {"alias": "phone"}))
    assert isinstance(response, CallToolResult)
    assert len(response.content) == 1
    assert isinstance(response.content[0], TextContent)
    assert json.loads(response.content[0].text) == response.structured_content
    assert Path(response.structured_content["data"]["path"]).is_file()


def test_inline_requires_startup_permission_before_device_access(adapter: AdapterFixture) -> None:
    facade, _, transport, _ = adapter
    result = invoke(create_server(facade), "screenshot", {"alias": "phone", "inline": True})
    assert result["error"]["code"] == "configuration_error"
    assert "--inline-images" in result["error"]["message"]
    assert transport.calls == []


@pytest.mark.parametrize("som", [False, True])
def test_inline_image_content_matches_private_file_and_serializable_metadata(
    adapter: AdapterFixture, som: bool,
) -> None:
    facade, state, transport, _ = adapter
    facade.inline_images = True
    server = create_server(facade)
    response = asyncio.run(server.call_tool(
        "screenshot", {"alias": "phone", "inline": True, "som": som, "max_width": 630},
    ))
    assert isinstance(response, CallToolResult)
    assert not response.is_error
    assert len(response.content) == 2
    text, image = response.content
    assert isinstance(text, TextContent)
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    assert json.loads(text.text) == response.structured_content
    metadata = response.structured_content["data"]
    path = Path(metadata["path"])
    assert base64.b64decode(image.data, validate=True) == path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert metadata["width"] == 630
    assert ("snapshot" in metadata) == som
    serialized = json.dumps(response.structured_content, allow_nan=False)
    assert image.data not in serialized
    assert "base64" not in serialized
    assert state.serial not in serialized
    assert not transport.all_remote_tmp_files()
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "screenshot")
    assert tool.input_schema["properties"]["inline"]["default"] is False
    assert tool.model_dump(by_alias=True)["_meta"] == {
        "inline_images": True, "max_image_bytes": facade.max_image_bytes,
    }
    assert tool.input_schema["properties"]["max_width"]["maximum"] == 8192
    assert tool.annotations is not None and tool.annotations.read_only_hint is True


@pytest.mark.parametrize("limit_delta", [-1, 0])
def test_inline_byte_limit_is_enforced_at_exact_boundary(
    adapter: AdapterFixture, limit_delta: int,
) -> None:
    facade, state, transport, _ = adapter
    facade.inline_images = True
    facade.max_image_bytes = len(state.png_bytes) + limit_delta
    response = asyncio.run(create_server(facade).call_tool(
        "screenshot", {"alias": "phone", "inline": True},
    ))
    assert isinstance(response, CallToolResult)
    assert response.is_error == (limit_delta < 0)
    if limit_delta < 0:
        assert response.structured_content["error"]["code"] == "image_too_large"
        assert len(response.content) == 1
        assert not list((facade.state_dir / "diagnostics").iterdir())
    else:
        assert isinstance(response.content[1], ImageContent)
    assert not transport.all_remote_tmp_files()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, MAX_IMAGE_BYTES + 1])
def test_invalid_startup_byte_limits_fail_closed(adapter: AdapterFixture, limit: Any) -> None:
    facade, _, transport, _ = adapter
    with pytest.raises(DeviceConfigurationError):
        PilotTools(facade.hdc, facade.devices, facade.actions, max_image_bytes=limit)
    assert transport.calls == []


@pytest.mark.parametrize("after_capture", [False, True])
def test_sensitive_inline_capture_returns_only_human_required_and_cleans_up(
    adapter: AdapterFixture, after_capture: bool,
) -> None:
    facade, state, transport, _ = adapter
    facade.inline_images = True
    sensitive = json.loads(state.layouts[0])
    sensitive["attributes"]["text"] = "Confirm pay"
    if after_capture:
        state.layouts.append(json.dumps(sensitive))
        state.advance_layout_on_dump = True
    else:
        state.layouts = [json.dumps(sensitive)]
    response = asyncio.run(create_server(facade).call_tool(
        "screenshot", {"alias": "phone", "inline": True},
    ))
    assert isinstance(response, CallToolResult)
    assert response.is_error
    assert len(response.content) == 1
    assert response.structured_content["error"]["code"] == "human_required"
    assert response.structured_content["data"] is None
    assert not list((facade.state_dir / "diagnostics").iterdir())
    assert not transport.all_remote_tmp_files()


def test_inline_conversion_failure_removes_capture(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade = adapter[0]
    facade.inline_images = True
    monkeypatch.setattr(
        "mcp.server.mcpserver.Image.to_image_content",
        Mock(side_effect=RuntimeError("PRIVATE_IMAGE_ERROR")),
    )
    result = invoke(create_server(facade), "screenshot", {"alias": "phone", "inline": True})
    assert result["error"]["code"] == "internal_error"
    assert "PRIVATE_IMAGE_ERROR" not in json.dumps(result)
    assert not list((facade.state_dir / "diagnostics").iterdir())


def test_inline_retention_reuses_diagnostics_pruning(adapter: AdapterFixture) -> None:
    facade = adapter[0]
    facade.inline_images = True
    device = facade._device("phone")
    for number in range(50):
        device.diagnostics.save_text(f"previous-{number}", "synthetic diagnostic")
    result = invoke(create_server(facade), "screenshot", {"alias": "phone", "inline": True})
    assert result["ok"]
    assert len(list(device.diagnostics.base_dir.iterdir())) == 50
    assert Path(result["data"]["path"]).is_file()


@pytest.mark.parametrize("tool", ["screenshot", "ocr"])
def test_mcp_observation_tools_honor_device_lock(adapter: AdapterFixture, tool: str) -> None:
    facade, _, transport, _ = adapter
    facade.inline_images = True
    facade.enable_ocr = True
    device = facade._device("phone")
    transport.calls.clear()
    arguments: dict[str, Any] = {"alias": "phone"}
    if tool == "screenshot":
        arguments["inline"] = True
    with device.acquire_lock():
        result = invoke(create_server(facade), tool, arguments)
    assert result["error"]["code"] == "device_busy"
    assert result["error"]["retryable"] is True
    assert all(args == ("list", "targets", "-v") for _, args, _ in transport.calls)
    assert not list((facade.state_dir / "diagnostics").glob("*"))


def test_screenshot_wall_clock_timeout_cleans_up(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, _, transport, _ = adapter
    facade.inline_images = True
    facade.timeout = 0.02

    def slow(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.1)

    monkeypatch.setattr(HarmonyDevice, "screenshot", slow)
    result = invoke(create_server(facade), "screenshot", {"alias": "phone", "inline": True})
    assert result["error"]["code"] == "action_budget_exceeded"
    assert not list((facade.state_dir / "diagnostics").iterdir())
    assert not transport.all_remote_tmp_files()
    assert not facade._device("phone").is_busy()


def test_mcp_ocr_opt_in_is_independent_readonly_and_reuses_observation(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    facade, _, transport, _ = adapter
    with pytest.raises(DeviceConfigurationError, match="--enable-ocr"):
        facade.ocr("phone")
    assert transport.calls == []
    facade.enable_ocr = True
    engine = Mock()

    def recognize(image: bytes) -> list[TextBox]:
        print("OCR backend diagnostics")
        return [TextBox("Local text", (1, 2, 30, 40), 0.95)]

    engine.recognize.side_effect = recognize
    monkeypatch.setattr(
        "moyu_harmony_pilot.actions.observation.detect_ocr_backend", lambda: "rapidocr",
    )
    monkeypatch.setattr(
        "moyu_harmony_pilot.actions.observation.create_ocr_engine", lambda backend: engine,
    )
    server = create_server(facade)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "ocr" in tools and "lab_observe" not in tools
    annotations = tools["ocr"].annotations
    assert annotations is not None and annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    properties = tools["ocr"].input_schema["properties"]
    assert properties["backend"]["default"] == "auto"
    assert properties["max_width"]["default"] == 1280
    assert properties["timeout"]["default"] == 30.0
    assert properties["backend"]["enum"] == ["auto", "macos", "rapidocr"]
    assert properties["timeout"]["maximum"] == 3600
    result = invoke(server, "ocr", {"alias": "phone", "max_width": 630})
    assert result["ok"]
    assert result["data"]["boxes"][0]["text"] == "Local text"
    assert result["data"]["width"] == 630
    assert result["data"]["source_width"] == 1260
    assert result["data"]["scale"] == {"x": 0.5, "y": 0.5}
    assert "path" not in result["data"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OCR backend diagnostics" in captured.err
    assert not list((facade.state_dir / "diagnostics").iterdir())
    assert not transport.all_remote_tmp_files()


def test_mcp_missing_ocr_backend_has_controlled_error(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade = adapter[0]
    facade.enable_ocr = True
    monkeypatch.setattr(
        "moyu_harmony_pilot.actions.observation.detect_ocr_backend",
        Mock(side_effect=VisionUnavailableError("Install a local OCR backend.")),
    )
    result = invoke(create_server(facade), "ocr", {"alias": "phone"})
    assert result["error"]["code"] == "vision_unavailable"
    assert result["data"] is None


def test_startup_options_reach_adapter(
    adapter: AdapterFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    facade = adapter[0]
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": "SYNTHETIC_STARTUP_DEVICE"}}))
    config.chmod(0o600)
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr("moyu_harmony_pilot.mcp.server.create_server", factory)
    monkeypatch.setattr("moyu_harmony_pilot.mcp.server.HdcClient", lambda **kwargs: facade.hdc)
    options = [
        "--config", str(config), "--state-dir", str(tmp_path),
        "--inline-images", "--enable-ocr", "--max-image-bytes", "1024",
    ]
    assert main(options) == 0
    configured = factory.call_args.args[0]
    assert configured.inline_images is True
    assert configured.enable_ocr is True
    assert configured.local_lab is False
    assert configured.max_image_bytes == 1024
    server.run.assert_called_once_with(transport="stdio")
    factory.reset_mock()
    assert main([*options, "--max-image-bytes", "0"]) == 2
    factory.assert_not_called()
