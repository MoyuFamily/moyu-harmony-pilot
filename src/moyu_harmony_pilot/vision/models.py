"""Backend-independent local vision contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class VisionError(Exception):
    code = "vision_error"
    retryable = False
    hint = "Check the local image and selected vision backend."


class VisionUnavailableError(VisionError):
    code = "vision_unavailable"
    hint = "Install the matching optional vision extra and select its backend explicitly."


@dataclass(frozen=True)
class TextBox:
    text: str
    bounds: tuple[int, int, int, int]
    confidence: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or len(self.text) > 4096
            or len(self.bounds) != 4
            or any(type(v) is not int for v in self.bounds)
            or min(self.bounds) < 0
            or self.bounds[2] <= self.bounds[0]
            or self.bounds[3] <= self.bounds[1]
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise VisionError("Invalid OCR observation.")

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


class OcrEngine(Protocol):
    def recognize(self, image: str | Path | bytes) -> list[TextBox]:
        """Read a local image and return top-left-origin pixel rectangles."""


def find_text(
    boxes: Sequence[TextBox],
    *,
    text: str | None = None,
    pattern: str | None = None,
    min_confidence: float = 0.5,
) -> list[TextBox]:
    if (text is None) == (pattern is None) or not 0 <= min_confidence <= 1:
        raise VisionError("Choose exactly one text or regex selector and a valid confidence.")
    try:
        expression = re.compile(pattern) if pattern is not None else None
    except re.error as exc:
        raise VisionError("Invalid OCR selector regex.") from exc
    return sorted(
        [
            box
            for box in boxes
            if box.confidence >= min_confidence
            and (box.text == text if expression is None else expression.search(box.text))
        ],
        key=lambda box: (box.bounds[1], box.bounds[0], box.text),
    )


def image_bytes(image: str | Path | bytes) -> bytes:
    try:
        if isinstance(image, bytes):
            data = image
        else:
            with Path(image).open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
        if not data or len(data) > 32 * 1024 * 1024:
            raise VisionError("Image must contain between 1 byte and 32 MiB.")
        return data
    except OSError as exc:
        raise VisionError("Unable to read the local OCR image.") from exc
