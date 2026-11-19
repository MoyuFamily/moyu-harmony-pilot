"""L3 (optional): Pluggable OCR engines and popup watcher helpers."""

from moyu_harmony_pilot.vision.models import (
    OcrEngine,
    TextBox,
    VisionError,
    VisionUnavailableError,
    find_text,
)
from moyu_harmony_pilot.vision.ocr import (
    MacOSVisionOcrEngine,
    RapidOcrEngine,
    create_ocr_engine,
)
from moyu_harmony_pilot.vision.som import Mark, annotate_som, require_pillow
from moyu_harmony_pilot.vision.watcher import PopupRule, PopupWatcher

__all__ = [
    "MacOSVisionOcrEngine",
    "Mark",
    "OcrEngine",
    "PopupRule",
    "PopupWatcher",
    "RapidOcrEngine",
    "TextBox",
    "VisionError",
    "VisionUnavailableError",
    "annotate_som",
    "create_ocr_engine",
    "find_text",
    "require_pillow",
]
