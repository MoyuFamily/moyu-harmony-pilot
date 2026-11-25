"""Offline OCR adapter and SoM coordinate contracts."""

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


@pytest.mark.parametrize(
    "confidence, expected",
    [
        (0.9, 0.9),
    ],
    ids=['coordinate-transform'],
)
def test_macos_vision_flips_normalized_bottom_left_origin(monkeypatch, confidence, expected):
    candidate = Mock()
    candidate.string.return_value = "Example"
    candidate.confidence.return_value = confidence
    observation = Mock()
    observation.topCandidates_.return_value = [candidate]
    observation.boundingBox.return_value = SimpleNamespace(
        origin=SimpleNamespace(x=0.1, y=0.25), size=SimpleNamespace(width=0.5, height=0.25)
    )
    request = Mock()
    request.results.return_value = [observation]
    for x, y, width, height in [(0.101, 0.25, 0, 0.25), (0.1, 0.251, 0.5, 0), (2, 2, 1, 1)]:
        degenerate = Mock()
        degenerate.topCandidates_.return_value = [candidate]
        degenerate.boundingBox.return_value = SimpleNamespace(
            origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height)
        )
        request.results.return_value.append(degenerate)
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
    assert MacOSVisionOcrEngine().recognize(b"synthetic image") == [
        TextBox("Example", (20, 200, 120, 300), expected)
    ]
    for score in (float("nan"), float("inf")):
        candidate.confidence.return_value = score
        with pytest.raises(VisionError):
            MacOSVisionOcrEngine().recognize(b"synthetic image")
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
