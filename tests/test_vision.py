"""Offline OCR adapter, SoM renderer and public screenshot integration tests."""

import io
import sys
from contextlib import nullcontext
from pathlib import Path
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
    backend.return_value = (
        [
            [[(1, 2), (1, 2), (1, 30), (1, 30)], "zero width", 0.9],
            [[(1.2, 2), (1.2, 2), (1.2, 30), (1.2, 30)], "subpixel zero width", 0.9],
            [[(1, 2.2), (20, 2.2), (20, 2.2), (1, 2.2)], "zero height", 0.9],
            [[(-2, -2), (-1, -2), (-1, -1), (-2, -1)], "outside image", 0.9],
            [[(1, 40), (20, 40), (20, 50), (1, 50)], "low", -0.0000001],
            [[(1, 2), (20, 2), (20, 30), (1, 30)], "high", 1.0000001],
        ],
        [],
    )
    assert engine.recognize(b"synthetic image") == [
        TextBox("high", (1, 2, 20, 30), 1.0),
        TextBox("low", (1, 40, 20, 50), 0.0),
    ]
    for confidence in (float("nan"), float("inf")):
        backend.return_value = ([([(1, 2), (20, 30)], "invalid", confidence)], [])
        with pytest.raises(VisionError):
            engine.recognize(b"synthetic image")
    backend.return_value = (None, None)
    assert engine.recognize(b"synthetic image") == []
    backend.side_effect = RuntimeError("private OCR text")
    with pytest.raises(VisionError, match="RapidOCR recognition failed"):
        engine.recognize(b"synthetic image")


@pytest.mark.parametrize("confidence, expected", [(0.9, 0.9), (1.0000001, 1), (-0.0000001, 0)])
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
    assert result["path"] == str(tmp_path / "shot.png")
    assert not transport.all_remote_tmp_files()
    previous = (tmp_path / "shot.png").read_bytes()
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_dump = True
    code, output, _ = _run_cli(arguments, transport=transport, cwd=tmp_path, env={})
    assert code == 1
    assert _parse_single_json_envelope(output)["error"]["code"] == "layout_changed"
    assert (tmp_path / "shot.png").read_bytes() == previous
    assert not list(tmp_path.glob(".som-*"))
    assert not transport.all_remote_tmp_files()


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("failure", ["layout", "annotation", "capture"])
def test_som_cli_failure_preserves_destination_and_removes_temporary_files(
    tmp_path, monkeypatch, existing, failure
):
    image = io.BytesIO()
    Image.new("RGB", (1260, 2720), "white").save(image, format="PNG")
    state = FakeDeviceState("SYNTHETIC_SOM_FAILURE", png_bytes=image.getvalue())
    transport = FakeHdcTransport([state])
    destination = tmp_path / "nested" / "capture.png"
    if existing:
        destination.parent.mkdir()
        destination.write_bytes(b"previous output")
    if failure == "layout":
        state.layouts.append(make_second_page_layout_json())
        state.advance_layout_on_dump = True
    elif failure == "annotation":

        def fail_annotation(path, *args, **kwargs):
            Path(path).write_bytes(b"partial annotation")
            raise VisionError("Annotation failed.")

        monkeypatch.setattr("moyu_harmony_pilot.actions.observation.annotate_som", fail_annotation)
    else:
        state.timeout_on_cmd_prefix = "uitest screenCap"
    code, output, _ = _run_cli(
        [
            "screenshot", "--out", str(destination), "--som", "--json",
            "--state-dir", str(tmp_path / "state"),
        ],
        transport=transport,
        cwd=tmp_path,
        env={},
    )
    assert code == 1
    assert not _parse_single_json_envelope(output)["ok"]
    if existing:
        assert destination.read_bytes() == b"previous output"
    else:
        assert not destination.exists()
    assert not list(destination.parent.glob(".som-*"))
    assert not transport.all_remote_tmp_files()


@pytest.mark.parametrize(
    "filename, explicit_format, expected_format",
    [("shot.PNG", None, "png"), ("shot.jpg", None, "jpeg"), ("shot.jpg", "png", "png")],
)
def test_som_temporary_capture_preserves_format_selection(
    tmp_path, filename, explicit_format, expected_format
):
    png, jpeg = io.BytesIO(), io.BytesIO()
    picture = Image.new("RGB", (1260, 2720), "white")
    picture.save(png, format="PNG")
    picture.save(jpeg, format="JPEG")
    state = FakeDeviceState(
        "SYNTHETIC_SOM_FORMAT", png_bytes=png.getvalue(), jpeg_bytes=jpeg.getvalue()
    )
    transport = FakeHdcTransport([state])
    destination = tmp_path / filename
    arguments = [
        "screenshot", "--out", str(destination), "--som", "--json",
        "--state-dir", str(tmp_path / "state"),
    ]
    if explicit_format:
        arguments.extend(["--format", explicit_format])
    code, output, _ = _run_cli(arguments, transport=transport, cwd=tmp_path, env={})
    assert code == 0, output
    data = _parse_single_json_envelope(output)["data"]
    assert data["format"] == expected_format
    assert data["path"] == str(destination)
    with Image.open(destination) as picture:
        assert picture.format.lower() == expected_format
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".som-*"))
    assert not transport.all_remote_tmp_files()
