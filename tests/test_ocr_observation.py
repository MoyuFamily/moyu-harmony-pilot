"""Offline shared OCR capture, bounds, privacy, deadline and cleanup contracts."""

import io
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PIL import Image

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.actions.observation import capture_ocr, capture_screenshot
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceProfile,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import HdcClient, HdcTimeoutError
from moyu_harmony_pilot.ui import UiError
from moyu_harmony_pilot.vision import TextBox, VisionError, VisionUnavailableError
from tests.fakes import FakeDeviceState, FakeHdcTransport, make_second_page_layout_json
from tests.test_cli import _parse_single_json_envelope, _run_cli

ObservationFixture = tuple[HarmonyDevice, FakeDeviceState, FakeHdcTransport, Mock]


@pytest.fixture
def observation(tmp_path: Path) -> ObservationFixture:
    stream = io.BytesIO()
    Image.new("RGB", (401, 803), "white").save(stream, format="PNG")
    state = FakeDeviceState("SYNTHETIC_OCR_DEVICE", png_bytes=stream.getvalue())
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport), DeviceProfile("phone", state.serial), state_dir=tmp_path,
    )
    engine = Mock()
    engine.recognize.return_value = [
        TextBox("中文按钮", (10, 20, 40, 50), 0.9),
        TextBox("Header", (3, 1, 50, 10), 0.8),
    ]
    return device, state, transport, engine


@pytest.mark.parametrize("max_width, size", [(1280, (401, 803)), (200, (200, 400))])
def test_ocr_returns_bounded_json_and_exact_coordinate_scales(
    observation: ObservationFixture, max_width: int, size: tuple[int, int],
) -> None:
    device, state, transport, engine = observation
    result = capture_ocr(device, max_width=max_width, engine=engine)
    assert result == {
        "alias": "phone",
        "backend": "custom",
        "width": size[0],
        "height": size[1],
        "source_width": 401,
        "source_height": 803,
        "scale": {"x": size[0] / 401, "y": size[1] / 803},
        "resized": max_width < 401,
        "boxes": [
            {"text": "Header", "bounds": [3, 1, 50, 10], "confidence": 0.8},
            {"text": "中文按钮", "bounds": [10, 20, 40, 50], "confidence": 0.9},
        ],
        "truncated": False,
    }
    assert state.serial not in json.dumps(result, allow_nan=False)
    with Image.open(io.BytesIO(engine.recognize.call_args.args[0])) as image:
        assert image.size == size
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()
    assert not device.is_busy()


def test_ocr_holds_lock_and_uses_owner_only_temporary_files(
    observation: ObservationFixture,
) -> None:
    device, _, _, engine = observation

    def recognize(image: bytes) -> list[TextBox]:
        assert device.is_busy()
        images = list(device.diagnostics.base_dir.rglob("*.png"))
        assert len(images) == 1
        assert images[0].stat().st_mode & 0o777 == 0o600
        assert images[0].parent.stat().st_mode & 0o777 == 0o700
        assert device.diagnostics.base_dir.stat().st_mode & 0o777 == 0o700
        return []

    engine.recognize.side_effect = recognize
    assert capture_ocr(device, engine=engine)["boxes"] == []
    assert list(device.diagnostics.base_dir.iterdir()) == []


@pytest.mark.parametrize("backend", ["auto", "macos", "rapidocr"])
def test_ocr_backend_loading_is_lazy_and_explicit(
    observation: ObservationFixture, monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    device, _, _, engine = observation
    detect = Mock(return_value="rapidocr")
    factory = Mock(return_value=engine)
    monkeypatch.setattr("moyu_harmony_pilot.actions.observation.detect_ocr_backend", detect)
    monkeypatch.setattr("moyu_harmony_pilot.actions.observation.create_ocr_engine", factory)
    result = capture_ocr(device, backend=backend)
    expected = "rapidocr" if backend == "auto" else backend
    assert result["backend"] == expected
    factory.assert_called_once_with(expected)
    assert detect.call_count == int(backend == "auto")
    factory.reset_mock()
    capture_ocr(device, engine=engine)
    factory.assert_not_called()


def test_native_size_with_injected_backend_needs_no_optional_libraries(
    observation: ObservationFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in ("PIL", "PIL.Image", "Vision", "rapidocr_onnxruntime", "mcp"):
        monkeypatch.setitem(sys.modules, module, None)
    device, _, _, engine = observation
    assert capture_ocr(device, engine=engine)["width"] == 401


@pytest.mark.parametrize(
    "options",
    [
        {"backend": "cloud"}, {"backend": None},
        {"max_width": 0}, {"max_width": 8193}, {"max_width": True}, {"max_width": 1.5},
        {"timeout": 0}, {"timeout": -1}, {"timeout": True}, {"timeout": 3601},
        {"timeout": float("nan")}, {"timeout": float("inf")},
    ],
)
def test_invalid_ocr_options_fail_before_device_access(
    observation: ObservationFixture, options: dict[str, Any],
) -> None:
    device, _, transport, engine = observation
    with pytest.raises(DeviceConfigurationError):
        capture_ocr(device, engine=engine, **options)
    assert transport.calls == []
    engine.recognize.assert_not_called()


def test_ocr_busy_fails_before_capture_or_backend_loading(observation: ObservationFixture) -> None:
    device, _, transport, engine = observation
    with device.acquire_lock(), pytest.raises(DeviceBusyError):
        capture_ocr(device, engine=engine)
    assert transport.calls == []
    engine.recognize.assert_not_called()


@pytest.mark.parametrize("after_capture", [False, True])
def test_ocr_sensitive_screen_blocks_capture_or_export(
    observation: ObservationFixture, after_capture: bool,
) -> None:
    device, state, transport, engine = observation
    sensitive = json.loads(state.layouts[0])
    sensitive["attributes"]["text"] = "Confirm pay"
    if after_capture:
        state.layouts.append(json.dumps(sensitive))
        state.advance_layout_on_dump = True
    else:
        state.layouts = [json.dumps(sensitive)]
    with pytest.raises(HumanRequiredError):
        capture_ocr(device, engine=engine)
    engine.recognize.assert_not_called()
    captured = any("screenCap" in str(args) for _, args, _ in transport.calls)
    assert captured == after_capture
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()
    assert not device.is_busy()


@pytest.mark.parametrize("text", ["Confirm pay", "Enter your password", "验证码 captcha"])
def test_sensitive_ocr_text_is_checked_before_export(
    observation: ObservationFixture, text: str,
) -> None:
    device, _, transport, engine = observation
    engine.recognize.return_value = [TextBox(text, (1, 1, 20, 20), 0.8)]
    with pytest.raises(HumanRequiredError) as error:
        capture_ocr(device, engine=engine)
    assert text not in str(error.value)
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()


def test_ocr_detects_sensitive_text_across_boxes_and_beyond_truncation(
    observation: ObservationFixture,
) -> None:
    device, _, _, engine = observation
    engine.recognize.return_value = [
        TextBox("x" * 4096, (1, y, 5, y + 1), 0.9) for y in range(9)
    ] + [
        TextBox("密码", (30, 20, 50, 40), 0.8),
        TextBox("输入", (1, 20, 20, 40), 0.8),
    ]
    with pytest.raises(HumanRequiredError):
        capture_ocr(device, engine=engine)
    assert list(device.diagnostics.base_dir.iterdir()) == []


def test_layout_change_during_capture_prevents_stale_ocr(observation: ObservationFixture) -> None:
    device, state, transport, engine = observation
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_dump = True
    with pytest.raises(UiError) as error:
        capture_ocr(device, engine=engine)
    assert error.value.code == "layout_changed"
    engine.recognize.assert_not_called()
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()


@pytest.mark.parametrize("stage", ["capture", "backend", "recognition"])
def test_ocr_failures_cleanup_and_release_lock(
    observation: ObservationFixture, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    device, state, transport, engine = observation
    if stage == "capture":
        state.timeout_on_cmd_prefix = "uitest screenCap"
    elif stage == "backend":
        monkeypatch.setattr(
            "moyu_harmony_pilot.actions.observation.detect_ocr_backend",
            Mock(side_effect=VisionUnavailableError("No local backend installed.")),
        )
    else:
        engine.recognize.side_effect = VisionError("Recognition failed.")
    with pytest.raises((HdcTimeoutError, VisionError)):
        capture_ocr(device, engine=None if stage == "backend" else engine)
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()
    assert not device.is_busy()


@pytest.mark.parametrize("stage", ["capture", "backend", "recognition"])
def test_ocr_wall_clock_budget_covers_capture_loading_and_recognition(
    observation: ObservationFixture, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    device, _, transport, engine = observation
    handler = signal.getsignal(signal.SIGALRM)

    def slow(*args: Any, **kwargs: Any) -> list[TextBox]:
        time.sleep(0.15)
        return []

    if stage == "capture":
        monkeypatch.setattr(device, "screenshot", slow)
    elif stage == "backend":
        monkeypatch.setattr("moyu_harmony_pilot.actions.observation.create_ocr_engine", slow)
    else:
        engine.recognize.side_effect = slow
    with pytest.raises(DeviceError) as error:
        capture_ocr(
            device, backend="rapidocr", timeout=0.03,
            engine=None if stage == "backend" else engine,
        )
    assert error.value.code == "action_budget_exceeded"
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    assert signal.getsignal(signal.SIGALRM) == handler
    assert list(device.diagnostics.base_dir.iterdir()) == []
    assert not transport.all_remote_tmp_files()
    assert not device.is_busy()


def test_ocr_truncates_total_text_with_deterministic_order(observation: ObservationFixture) -> None:
    device, _, _, engine = observation
    engine.recognize.return_value = [
        TextBox("x" * 4096, (1, y, 5, y + 1), 0.9) for y in range(10, 0, -1)
    ]
    result = capture_ocr(device, engine=engine)
    assert result["truncated"]
    assert sum(len(row["text"]) for row in result["boxes"]) == 32768
    assert [row["bounds"][1] for row in result["boxes"]] == list(range(1, 9))


@pytest.mark.parametrize("invalid", ["count", "type", "bounds", "confidence"])
def test_ocr_rejects_invalid_backend_results(
    observation: ObservationFixture, invalid: str,
) -> None:
    device, _, _, engine = observation
    box = TextBox("text", (1, 1, 5, 5), 0.9)
    if invalid == "count":
        engine.recognize.return_value = [box] * 1001
    elif invalid == "type":
        engine.recognize.return_value = [{"text": "unvalidated"}]
    elif invalid == "bounds":
        engine.recognize.return_value = [TextBox("text", (1, 1, 402, 804), 0.9)]
    else:
        object.__setattr__(box, "confidence", float("nan"))
        engine.recognize.return_value = [box]
    with pytest.raises(VisionError):
        capture_ocr(device, engine=engine)
    assert list(device.diagnostics.base_dir.iterdir()) == []


def test_guarded_screenshot_preserves_existing_file_on_sensitive_transition(
    observation: ObservationFixture, tmp_path: Path,
) -> None:
    device, state, transport, _ = observation
    destination = tmp_path / "previous.png"
    destination.write_bytes(b"previous capture")
    sensitive = json.loads(state.layouts[0])
    sensitive["attributes"]["text"] = "Confirm pay"
    state.layouts.append(json.dumps(sensitive))
    state.advance_layout_on_dump = True
    with pytest.raises(HumanRequiredError):
        capture_screenshot(device, destination, guard_sensitive=True)
    assert destination.read_bytes() == b"previous capture"
    assert not list(tmp_path.glob(".som-*"))
    assert not transport.all_remote_tmp_files()


def test_ocr_is_absent_from_bridge_action_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    registry = ActionRegistry.discover(env={}, remote=True)
    assert all("ocr" not in spec["action_id"] for spec in registry.list())


def test_ocr_cli_normal_entrypoint_and_json_isolation(observation, tmp_path, monkeypatch):
    _, _, transport, engine = observation

    def backend(name):
        print("synthetic backend initialization")
        return engine

    monkeypatch.setattr("moyu_harmony_pilot.actions.observation.create_ocr_engine", backend)
    code, output, error = _run_cli(
        ["ocr", "--backend", "rapidocr", "--max-width", "200", "--json",
         "--state-dir", str(tmp_path / "state")], transport=transport, cwd=tmp_path, env={},
    )
    assert code == 0
    assert error.strip() == "synthetic backend initialization"
    result = _parse_single_json_envelope(output)
    assert result["command"] == "ocr"
    assert result["data"]["width"] == 200
    assert result["data"]["boxes"][0]["text"] == "Header"
    assert not transport.all_remote_tmp_files()


@pytest.mark.parametrize("failure, expected_code", [
    (VisionUnavailableError("Backend unavailable."), 2),
    (HumanRequiredError("Manual verification required."), 4),
])
def test_ocr_cli_preserves_error_semantics(
    observation, tmp_path, monkeypatch, failure, expected_code
):
    _, _, transport, _ = observation
    monkeypatch.setattr(
        "moyu_harmony_pilot.actions.observation.create_ocr_engine", Mock(side_effect=failure)
    )
    code, output, error = _run_cli(
        ["ocr", "--backend", "rapidocr", "--json", "--state-dir", str(tmp_path / "state")],
        transport=transport, cwd=tmp_path, env={},
    )
    assert code == expected_code and error == ""
    assert _parse_single_json_envelope(output)["error"]["code"] == failure.code
    assert not transport.all_remote_tmp_files()
