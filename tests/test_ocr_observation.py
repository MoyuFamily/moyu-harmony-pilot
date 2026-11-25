"""Offline shared OCR capture, bounds, privacy, deadline and cleanup contracts."""

import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.actions.observation import capture_ocr
from moyu_harmony_pilot.device import (
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


@pytest.mark.parametrize(
    "max_width, size",
    [
        (200, (200, 400)),
    ],
    ids=['resized-coordinates'],
)
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


@pytest.mark.parametrize(
    "after_capture",
    [
        True,
    ],
    ids=['sensitive-transition'],
)
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


@pytest.mark.parametrize(
    "text",
    [
        "验证码 captcha",
    ],
    ids=['captcha-export-blocked'],
)
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


@pytest.mark.parametrize(
    "stage",
    [
        "recognition",
    ],
    ids=['recognition-failure'],
)
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
