"""Read-only local layout capture and atomic, host-sanitized candidate fixtures."""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.device import DeviceConfigurationError, HarmonyDevice
from moyu_harmony_pilot.ui import redact_layout_json


def write_sanitized_fixture(
    raw_json: str,
    destination: str | Path,
    *,
    keep_texts: Iterable[str] = (),
    keep_bundles: Iterable[str] = (),
) -> dict[str, Any]:
    """Redact in memory and atomically write a private candidate fixture for manual review.

    This offline entry point accepts the contents of a local dumpLayout file. Only
    sanitized JSON is written to the staging file or destination. A failed redaction,
    write, flush, or replacement preserves an existing destination.
    """
    sanitized, summary = redact_layout_json(
        raw_json, keep_texts=keep_texts, keep_bundles=keep_bundles
    )
    content = (sanitized + "\n").encode("utf-8")
    path = Path(destination).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".fixture-", suffix=".json", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "path": str(path),
        "size_bytes": len(content),
        "redaction": summary.to_dict(),
        "manual_review_required": True,
    }


def capture_fixture(
    device: HarmonyDevice,
    destination: str | Path,
    *,
    timeout: float = 30.0,
    keep_texts: Iterable[str] = (),
    keep_bundles: Iterable[str] = (),
) -> dict[str, Any]:
    """Capture one read-only dumpLayout under the device lock, then redact on the host.

    UiDriver owns and cleans its temporary transfer files on the host and device.
    The returned report contains the sanitized file location and aggregate counts.
    Every candidate still requires manual review before publication.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise DeviceConfigurationError("Fixture timeout must be positive and finite.")
    with device.acquire_lock():
        raw_json = device.ui.dump_layout_raw(timeout=timeout)
    return write_sanitized_fixture(
        raw_json, destination, keep_texts=keep_texts, keep_bundles=keep_bundles
    )
