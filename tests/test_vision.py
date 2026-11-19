"""Offline OCR adapter, SoM renderer and public screenshot integration tests."""

import io
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from moyu_harmony_pilot.vision import (
    MacOSVisionOcrEngine,
    Mark,
    RapidOcrEngine,
    TextBox,
    VisionError,
    VisionUnavailableError,
    annotate_som,
    create_ocr_engine,
    find_text,
)
from tests.fakes import FakeDeviceState, FakeHdcTransport, make_second_page_layout_json
from tests.test_cli import _parse_single_json_envelope, _run_cli


def test_ocr_selection_and_unavailable_backend(monkeypatch):
    boxes = [TextBox("Hello", (10, 20, 100, 50), 0.9), TextBox("世界", (10, 80, 100, 100), 0.4)]
    assert find_text(boxes, text="Hello")[0].center == (55, 35)
    assert find_text(boxes, pattern="世.*", min_confidence=0.3)[0].text == "世界"
    assert find_text(boxes, text="世界") == []
    with pytest.raises(VisionUnavailableError):
        create_ocr_engine()
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    with pytest.raises(VisionUnavailableError):
        RapidOcrEngine()
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(VisionUnavailableError):
        MacOSVisionOcrEngine()
    with pytest.raises(VisionError):
        TextBox("invalid", (2, 2, 1, 1), 1.0)


def test_rapidocr_converts_quadrilaterals_and_handles_empty_results(monkeypatch):
    backend = Mock(
        return_value=(
            [[[[1.2, 2.2], [20.2, 2.2], [20.2, 30.2], [1.2, 30.2]], "Example", 0.95]],
            [0.1],
        )
    )
    monkeypatch.setitem(
        sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=lambda: backend)
    )
    engine = RapidOcrEngine()
    assert engine.recognize(b"synthetic image")[0].bounds == (1, 2, 21, 31)
    backend.return_value = (None, None)
    assert engine.recognize(b"synthetic image") == []
    backend.side_effect = RuntimeError("private OCR text")
    with pytest.raises(VisionError, match="RapidOCR recognition failed"):
        engine.recognize(b"synthetic image")


def test_macos_vision_flips_normalized_bottom_left_origin(monkeypatch):
    candidate = Mock()
    candidate.string.return_value = "Example"
    candidate.confidence.return_value = 0.9
    observation = Mock()
    observation.topCandidates_.return_value = [candidate]
    observation.boundingBox.return_value = SimpleNamespace(
        origin=SimpleNamespace(x=0.1, y=0.25), size=SimpleNamespace(width=0.5, height=0.25)
    )
    request = Mock()
    request.results.return_value = [observation]
    handler = Mock()
    handler.performRequests_error_.return_value = (True, None)
    vision = Mock(VNRequestTextRecognitionLevelAccurate=1)
    vision.VNRecognizeTextRequest.alloc.return_value.init.return_value = request
    vision.VNImageRequestHandler.alloc.return_value.initWithCGImage_options_.return_value = handler
    quartz = Mock()
    quartz.CGImageGetWidth.return_value = 200
    quartz.CGImageGetHeight.return_value = 400
    monkeypatch.setattr(sys, "platform", "darwin")
    for name, module in {
        "Vision": vision,
        "Quartz": quartz,
        "Foundation": Mock(),
        "objc": SimpleNamespace(autorelease_pool=nullcontext),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    assert MacOSVisionOcrEngine().recognize(b"synthetic image")[0].bounds == (20, 200, 120, 300)
    handler.performRequests_error_.return_value = (False, "private")
    with pytest.raises(VisionError):
        MacOSVisionOcrEngine().recognize(b"synthetic image")


def test_som_renders_scaled_marks_atomically(tmp_path):
    path = tmp_path / "screen.png"
    Image.new("RGB", (200, 400), "white").save(path)
    annotate_som(path, [Mark("@e1", (20, 40, 200, 100))], screen_size=(400, 800))
    with Image.open(path) as image:
        assert image.size == (200, 400)
        assert image.getpixel((10, 20)) != (255, 255, 255)
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".som-*")) == []
    previous = path.read_bytes()
    with pytest.raises(VisionError):
        annotate_som(path, [Mark("private", (1, 1, 10, 10))], screen_size=(200, 400))
    assert path.read_bytes() == previous


def test_screenshot_som_cli_scales_and_returns_matching_references(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (1260, 2720), "white").save(stream, format="PNG")
    state = FakeDeviceState("SYNTHETIC_VISION", png_bytes=stream.getvalue())
    transport = FakeHdcTransport([state])
    arguments = [
        "screenshot",
        "--out",
        str(tmp_path / "shot.png"),
        "--som",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--max-width",
        "630",
    ]
    code, output, error = _run_cli(arguments, transport=transport, cwd=tmp_path, env={})
    assert code == 0, output
    assert error == ""
    result = _parse_single_json_envelope(output)["data"]
    assert result["som"] and result["snapshot"]["elements"]
    assert result["width"] == 630
    assert result["height"] == 1360
    assert not transport.all_remote_tmp_files()
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_dump = True
    code, output, _ = _run_cli(arguments, transport=transport, cwd=tmp_path, env={})
    assert code == 1
    assert _parse_single_json_envelope(output)["error"]["code"] == "layout_changed"
    assert not transport.all_remote_tmp_files()
