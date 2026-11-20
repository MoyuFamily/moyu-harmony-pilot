"""Lazy optional OCR adapters. Backend results remain local to the caller."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from moyu_harmony_pilot.vision.models import (
    OcrEngine,
    TextBox,
    VisionError,
    VisionUnavailableError,
    image_bytes,
)


class RapidOcrEngine:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise VisionUnavailableError("Install the vision-rapidocr extra.") from exc
        try:
            self._engine = RapidOCR()
        except Exception as exc:
            raise VisionUnavailableError("RapidOCR could not initialize its local models.") from exc

    def recognize(self, image: str | Path | bytes) -> list[TextBox]:
        data = image_bytes(image)
        try:
            rows, _timings = self._engine(data, use_det=True, use_cls=True, use_rec=True)
            boxes = []
            for polygon, text, confidence in (rows or [])[:1000]:
                xs, ys = zip(*polygon, strict=True)
                if max(xs) <= min(xs) or max(ys) <= min(ys):
                    continue
                bounds = (
                    max(0, math.floor(min(xs))),
                    max(0, math.floor(min(ys))),
                    math.ceil(max(xs)),
                    math.ceil(max(ys)),
                )
                if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    continue
                score = float(confidence)
                if not math.isfinite(score):
                    raise VisionError("Invalid OCR confidence.")
                boxes.append(TextBox(text, bounds, max(0.0, min(1.0, score))))
            return sorted(boxes, key=lambda box: (box.bounds[1], box.bounds[0], box.text))
        except Exception as exc:
            raise VisionError("RapidOCR recognition failed.") from exc


class MacOSVisionOcrEngine:
    def __init__(self, *, languages: tuple[str, ...] = ("zh-Hans", "en-US")) -> None:
        if sys.platform != "darwin":
            raise VisionUnavailableError("The macOS Vision backend requires macOS.")
        try:
            import Foundation
            import objc
            import Quartz
            import Vision
        except ImportError as exc:
            raise VisionUnavailableError("Install the vision-macos extra.") from exc
        self._foundation = Foundation
        self._quartz = Quartz
        self._vision = Vision
        self._objc = objc
        self._languages = languages

    def recognize(self, image: str | Path | bytes) -> list[TextBox]:
        data = image_bytes(image)
        try:
            with self._objc.autorelease_pool():
                nsdata = self._foundation.NSData.dataWithBytes_length_(data, len(data))
                source = self._quartz.CGImageSourceCreateWithData(nsdata, None)
                cgimage = self._quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
                if cgimage is None:
                    raise VisionError("Image decoding failed.")
                width = self._quartz.CGImageGetWidth(cgimage)
                height = self._quartz.CGImageGetHeight(cgimage)
                request = self._vision.VNRecognizeTextRequest.alloc().init()
                request.setRecognitionLevel_(self._vision.VNRequestTextRecognitionLevelAccurate)
                request.setRecognitionLanguages_(list(self._languages))
                request.setUsesLanguageCorrection_(False)
                handler = self._vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
                    cgimage,
                    self._foundation.NSDictionary.dictionary(),
                )
                success, error = handler.performRequests_error_([request], None)
                if not success or error is not None:
                    raise VisionError("Vision recognition request failed.")
                boxes = []
                for observation in list(request.results() or [])[:1000]:
                    candidates = observation.topCandidates_(1)
                    if not candidates:
                        continue
                    candidate = candidates[0]
                    rect = observation.boundingBox()
                    if rect.size.width <= 0 or rect.size.height <= 0:
                        continue
                    left = max(0, math.floor(rect.origin.x * width))
                    top = max(0, math.floor((1 - rect.origin.y - rect.size.height) * height))
                    right = min(width, math.ceil((rect.origin.x + rect.size.width) * width))
                    bottom = min(height, math.ceil((1 - rect.origin.y) * height))
                    if right <= left or bottom <= top:
                        continue
                    score = float(candidate.confidence())
                    if not math.isfinite(score):
                        raise VisionError("Invalid OCR confidence.")
                    boxes.append(
                        TextBox(
                            candidate.string(),
                            (left, top, right, bottom),
                            max(0.0, min(1.0, score)),
                        )
                    )
                return sorted(boxes, key=lambda box: (box.bounds[1], box.bounds[0], box.text))
        except Exception as exc:
            raise VisionError("macOS Vision recognition failed.") from exc


def create_ocr_engine(backend: str | None = None) -> OcrEngine:
    if backend == "macos":
        return MacOSVisionOcrEngine()
    if backend == "rapidocr":
        return RapidOcrEngine()
    raise VisionUnavailableError("Select an installed OCR backend using macos or rapidocr.")
