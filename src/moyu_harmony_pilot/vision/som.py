"""Set-of-Mark annotation with protocol-only snapshot input and optional Pillow."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from moyu_harmony_pilot.vision.models import VisionError, VisionUnavailableError


@dataclass(frozen=True)
class Mark:
    ref: str
    bounds: tuple[int, int, int, int]


def require_pillow() -> None:
    try:
        import PIL.Image  # noqa: F401
    except ImportError as exc:
        raise VisionUnavailableError("Install the vision extra for SoM annotation.") from exc


def annotate_som(
    path: str | Path,
    marks: Sequence[Mark],
    *,
    screen_size: tuple[int, int],
) -> None:
    require_pillow()
    from PIL import Image, ImageDraw, ImageFont

    width, height = screen_size
    if width <= 0 or height <= 0 or len(marks) > 120:
        raise VisionError("Invalid SoM screen dimensions or mark count.")
    destination = Path(path)
    temporary: str | None = None
    try:
        with Image.open(destination) as source:
            image = source.convert("RGB")
            image_format = source.format
        scale_x, scale_y = image.width / width, image.height / height
        if abs(scale_x / scale_y - 1) > 0.02:
            raise VisionError("Screenshot and UI snapshot aspect ratios differ.")
        drawing = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=max(12, min(24, round(image.width / 55))))
        for mark in marks:
            if not re.fullmatch(r"@e[1-9][0-9]*", mark.ref) or len(mark.bounds) != 4:
                raise VisionError("Invalid SoM reference or rectangle.")
            left, top, right, bottom = mark.bounds
            if right <= left or bottom <= top:
                raise VisionError("Invalid SoM rectangle.")
            x1 = max(0, min(image.width - 1, round(left * scale_x)))
            y1 = max(0, min(image.height - 1, round(top * scale_y)))
            x2 = max(x1, min(image.width - 1, round(right * scale_x)))
            y2 = max(y1, min(image.height - 1, round(bottom * scale_y)))
            drawing.rectangle((x1, y1, x2, y2), outline="#e63946", width=2)
            label = drawing.textbbox((0, 0), mark.ref, font=font)
            label_width, label_height = label[2] + 6, label[3] - label[1] + 6
            x1 = max(0, min(x1, image.width - label_width))
            y1 = max(0, min(y1, image.height - label_height))
            drawing.rectangle((x1, y1, x1 + label_width, y1 + label_height), fill="#e63946")
            drawing.text((x1 + 3, y1 + 3 - label[1]), mark.ref, fill="white", font=font)
        fd, temporary = tempfile.mkstemp(prefix=".som-", dir=destination.parent)
        with os.fdopen(fd, "wb") as stream:
            image.save(stream, format=image_format)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except (OSError, ValueError) as exc:
        raise VisionError("Unable to annotate the local screenshot.") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
