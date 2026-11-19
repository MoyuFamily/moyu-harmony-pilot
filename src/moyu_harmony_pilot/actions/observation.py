"""Shared observation operations for CLI and protocol adapters."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from moyu_harmony_pilot.device import HarmonyDevice, ScreenshotResult
from moyu_harmony_pilot.ui import CompactSnapshot, UiError
from moyu_harmony_pilot.vision import Mark, annotate_som, require_pillow


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
) -> tuple[ScreenshotResult, CompactSnapshot | None]:
    if som:
        require_pillow()
    with device.acquire_lock(wait_timeout=wait_lock):
        snapshot = None
        if som:
            _, snapshot = device.dump_ui_compact(display_id=display_id, timeout=timeout)
        result = device.screenshot(
            output,
            image_format=image_format,
            display_id=display_id,
            max_width=max_width,
            timeout=timeout,
        )
        if snapshot is not None:
            _, after = device.dump_ui_compact(display_id=display_id, timeout=timeout)
            if after.layout_hash != snapshot.layout_hash:
                raise UiError(
                    "UI changed during capture. Retry the SoM screenshot.",
                    code="layout_changed",
                    retryable=True,
                )
            marks = [
                Mark(element.ref, tuple(element.bounds.to_list())) for element in snapshot.elements
            ]
            annotate_som(result.path, marks, screen_size=snapshot.screen_size)
            result = replace(result, size_bytes=result.path.stat().st_size)
    return result, snapshot
