"""Shared observation operations for CLI and protocol adapters."""

from __future__ import annotations

import math
import os
import tempfile
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions.budget import bounded_call, ensure_deadline_available
from moyu_harmony_pilot.actions.models import ActionBudgetExceeded, ActionContext
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    HarmonyDevice,
    HumanRequiredError,
    ScreenshotResult,
)
from moyu_harmony_pilot.device.diagnostics import resize_image_max_width
from moyu_harmony_pilot.ui import (
    CompactSnapshot,
    UiError,
    detect_sensitive_signals,
    parse_layout_dict,
)
from moyu_harmony_pilot.vision import (
    Mark,
    OcrEngine,
    TextBox,
    VisionError,
    annotate_som,
    create_ocr_engine,
    detect_ocr_backend,
    require_pillow,
)
from moyu_harmony_pilot.vision.models import image_bytes

_MAX_OCR_BOXES = 1000
_MAX_OCR_TEXT_CHARS = 32768


def capture_screenshot(
    device: HarmonyDevice,
    output: str | Path,
    *,
    som: bool = False,
    image_format: str | None = None,
    display_id: int | None = None,
    max_width: int | None = None,
    timeout: float = 20,
    wait_lock: float = 0,
    guard_sensitive: bool = False,
) -> tuple[ScreenshotResult, CompactSnapshot | None]:
    with device.acquire_lock(wait_timeout=wait_lock):
        return _capture_screenshot_locked(
            device, output, som=som, image_format=image_format, display_id=display_id,
            max_width=max_width, timeout=timeout, guard_sensitive=guard_sensitive,
        )


def _capture_screenshot_locked(
    device: HarmonyDevice,
    output: str | Path,
    *,
    som: bool = False,
    image_format: str | None = None,
    display_id: int | None = None,
    max_width: int | None = None,
    timeout: float = 20,
    guard_sensitive: bool = False,
) -> tuple[ScreenshotResult, CompactSnapshot | None]:
    """Capture under the caller's device lock, staging guarded output until validated."""
    if som:
        require_pillow()
    with ExitStack() as cleanup:
        snapshot = None
        destination = Path(output).expanduser()
        capture_path = destination
        if som or guard_sensitive:
            _, snapshot = device.dump_ui_compact(display_id=display_id, timeout=timeout)
            if guard_sensitive and snapshot.sensitive_signals:
                raise HumanRequiredError(
                    "Sensitive screen detected. Take over manually before capturing an image."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(
                prefix=".som-capture-", suffix=destination.suffix, dir=destination.parent
            )
            capture_path = Path(filename)
            cleanup.callback(capture_path.unlink, missing_ok=True)
            os.close(fd)
        result = device.screenshot(
            capture_path,
            image_format=image_format,
            display_id=display_id,
            max_width=max_width,
            timeout=timeout,
        )
        if snapshot is not None:
            _, after = device.dump_ui_compact(display_id=display_id, timeout=timeout)
            if guard_sensitive and after.sensitive_signals:
                raise HumanRequiredError(
                    "Capture reached a sensitive screen. Take over manually."
                )
            if after.layout_hash != snapshot.layout_hash:
                raise UiError(
                    "UI changed during capture. Observe again and retry the screenshot.",
                    code="layout_changed",
                    retryable=True,
                )
            if som:
                marks = [
                    Mark(element.ref, (
                        element.bounds.left, element.bounds.top,
                        element.bounds.right, element.bounds.bottom,
                    ))
                    for element in snapshot.elements
                ]
                annotate_som(result.path, marks, screen_size=snapshot.screen_size)
            result = replace(result, path=destination, size_bytes=result.path.stat().st_size)
            os.replace(capture_path, destination)
    return result, snapshot if som else None


def capture_ocr(
    device: HarmonyDevice,
    *,
    backend: str = "auto",
    max_width: int = 1280,
    timeout: float = 30.0,
    engine: OcrEngine | None = None,
) -> dict[str, Any]:
    """Return bounded local OCR boxes in resized-image pixels and source scaling metadata.

    The device lock and shared main-thread deadline cover capture and local recognition.
    Optional backends load on demand. Temporary images are removed on every exit path.
    """
    if backend not in ("auto", "macos", "rapidocr"):
        raise DeviceConfigurationError("backend must be auto, macos or rapidocr.")
    if type(max_width) is not int or not 1 <= max_width <= 8192:
        raise DeviceConfigurationError("max_width must be an integer between 1 and 8192.")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 3600
    ):
        raise DeviceConfigurationError("timeout must be a finite number in (0, 3600].")
    ensure_deadline_available()
    context = ActionContext(device, time.monotonic() + timeout)

    def observe(directory: str) -> dict[str, Any]:
        result, _ = _capture_screenshot_locked(
            device, Path(directory) / "screen.png", image_format="png",
            timeout=context.remaining_timeout, guard_sensitive=True,
        )
        source_width, source_height = result.width, result.height
        if source_width is None or source_height is None or min(source_width, source_height) <= 0:
            raise VisionError("Unable to determine screenshot dimensions.")
        # Apply the standard byte bound before decoding or resizing the captured image.
        image_bytes(result.path)
        width, height, resized = resize_image_max_width(result.path, max_width)
        if width is None or height is None or min(width, height) <= 0:
            raise VisionError("Unable to determine OCR image dimensions.")
        selected_backend = "custom"
        selected_engine = engine
        if selected_engine is None:
            selected_backend = detect_ocr_backend() if backend == "auto" else backend
            selected_engine = create_ocr_engine(selected_backend)
        boxes = selected_engine.recognize(image_bytes(result.path))
        if not isinstance(boxes, list) or len(boxes) > _MAX_OCR_BOXES:
            raise VisionError("OCR must return at most 1000 text boxes.")
        for box in boxes:
            if not isinstance(box, TextBox):
                raise VisionError("Invalid OCR observation.")
            # Revalidate injected backends as well as the built-in adapters.
            TextBox(box.text, box.bounds, box.confidence)
            if box.bounds[2] > width or box.bounds[3] > height:
                raise VisionError("OCR bounds exceed the image dimensions.")
        boxes = sorted(boxes, key=lambda item: (item.bounds[1], item.bounds[0], item.text))
        text_tree = parse_layout_dict({
            "attributes": {"type": "Text", "text": " ".join(box.text for box in boxes)},
        })
        if detect_sensitive_signals(text_tree):
            raise HumanRequiredError(
                "OCR detected sensitive content. Take over manually before observing again."
            )
        rows: list[dict[str, Any]] = []
        remaining_chars = _MAX_OCR_TEXT_CHARS
        for box in boxes:
            if len(box.text) > remaining_chars:
                break
            rows.append({
                "text": box.text, "confidence": box.confidence, "bounds": list(box.bounds),
            })
            remaining_chars -= len(box.text)
        return {
            "alias": device.alias,
            "backend": selected_backend,
            "width": width,
            "height": height,
            "source_width": source_width,
            "source_height": source_height,
            "scale": {"x": width / source_width, "y": height / source_height},
            "resized": resized,
            "boxes": rows,
            "truncated": len(rows) < len(boxes),
        }

    with device.acquire_lock(), tempfile.TemporaryDirectory(
        prefix="ocr-", dir=device.diagnostics.ensure_ready(),
    ) as directory:
        try:
            return bounded_call(context, lambda: observe(directory))
        except ActionBudgetExceeded as exc:
            raise DeviceError(
                "Local OCR deadline exhausted. Observe again with a sufficient timeout.",
                code="action_budget_exceeded",
            ) from exc
