"""Secure local diagnostics storage (`0700`/`0600`), rolling retention, and image utilities."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from moyu_harmony_pilot.device.errors import DeviceConfigurationError
from moyu_harmony_pilot.device.lock import ensure_secure_dir

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def write_secure_bytes(path: str | Path, data: bytes) -> Path:
    """Write bytes to `path` creating/enforcing mode `0600` on POSIX."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(dest, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(dest, 0o600)
    return dest


def write_secure_text(path: str | Path, text: str) -> Path:
    """Write UTF-8 text to `path` with mode `0600` on POSIX."""
    return write_secure_bytes(path, text.encode("utf-8"))


def enforce_file_mode_0600(path: str | Path) -> None:
    """Tighten an existing file's permissions to owner read/write (`0600`)."""
    if os.name == "posix":
        os.chmod(Path(path), 0o600)


def read_image_dimensions(path: str | Path) -> tuple[int | None, int | None]:
    """Read `(width, height)` from JPEG or PNG binary headers using stdlib only."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return (None, None)

    # PNG header: 8-byte signature + IHDR chunk (width, height at bytes 16..24)
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return (width, height)

    # JPEG header: SOI (0xFFD8) followed by segments until SOF*
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        pos = 2
        length = len(data)
        while pos < length - 1:
            if data[pos] != 0xFF:
                pos += 1
                continue
            while pos < length and data[pos] == 0xFF:
                pos += 1
            if pos >= length:
                break
            marker = data[pos]
            pos += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if pos + 2 > length:
                break
            seg_len = struct.unpack(">H", data[pos : pos + 2])[0]
            if seg_len < 2 or pos + seg_len > length:
                break
            if marker in _SOF_MARKERS and pos + 7 <= length:
                height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
                return (width, height)
            pos += seg_len

    return (None, None)


def resize_image_max_width(path: str | Path, max_width: int) -> tuple[int | None, int | None, bool]:
    """Resize `path` in-place if its width exceeds `max_width`.

    Uses optional Pillow when installed, or built-in macOS `/usr/bin/sips` when on Darwin.
    Raises `DeviceConfigurationError` if resizing is requested on a host with neither tool.
    """
    if max_width <= 0:
        raise DeviceConfigurationError(f"max_width must be positive, got {max_width!r}.")

    img_path = Path(path)
    width, height = read_image_dimensions(img_path)
    if width is not None and width <= max_width:
        return (width, height, False)

    try:
        from PIL import Image
    except ImportError:
        pass
    else:
        with Image.open(img_path) as img:
            orig_w, orig_h = img.size
            if orig_w <= max_width:
                return (orig_w, orig_h, False)
            new_h = max(1, round(orig_h * (max_width / orig_w)))
            resized = img.resize((max_width, new_h))
            resized.save(img_path)
        enforce_file_mode_0600(img_path)
        return (max_width, new_h, True)

    sips_path = shutil.which("sips") if sys.platform == "darwin" else None
    if sips_path:
        proc = subprocess.run(  # noqa: S603
            [sips_path, "--resampleWidth", str(max_width), str(img_path)],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if proc.returncode != 0:
            raise DeviceConfigurationError(
                f"Failed to resize screenshot via macOS sips: {proc.stderr.strip()}"
            )
        enforce_file_mode_0600(img_path)
        sips_width, sips_height = read_image_dimensions(img_path)
        return (sips_width, sips_height, True)

    raise DeviceConfigurationError(
        "Resizing screenshots with --max-width requires Pillow (`pip install pillow`) on this OS.",
        hint="Install Pillow or omit `--max-width` to keep the native screenshot resolution.",
    )


class DiagnosticsStore:
    """Privacy-hardened (`0700`/`0600`) local diagnostics storage with rolling retention."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        max_files: int = 50,
        max_total_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if max_files <= 0 or max_total_bytes <= 0:
            raise DeviceConfigurationError("max_files and max_total_bytes must be positive.")
        self._base_dir = Path(base_dir)
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def ensure_ready(self) -> Path:
        return ensure_secure_dir(self._base_dir)

    def _build_path(self, name_hint: str, suffix: str) -> Path:
        self.ensure_ready()
        safe_hint = _SAFE_NAME_RE.sub("_", name_hint).strip("._") or "artifact"
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        clean_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return self._base_dir / f"{ts}_{safe_hint}{clean_suffix}"

    def save_bytes(self, name_hint: str, data: bytes, *, suffix: str) -> Path:
        dest = self._build_path(name_hint, suffix)
        write_secure_bytes(dest, data)
        self.prune()
        return dest

    def save_text(self, name_hint: str, text: str, *, suffix: str = ".json") -> Path:
        return self.save_bytes(name_hint, text.encode("utf-8"), suffix=suffix)

    def prune(self) -> int:
        """Delete oldest files exceeding `max_files` or `max_total_bytes`."""
        if not self._base_dir.is_dir():
            return 0

        entries: list[tuple[float, int, Path]] = []
        for item in self._base_dir.iterdir():
            if not item.is_file():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, item))

        # Oldest first
        entries.sort(key=lambda row: (row[0], row[2].name))
        total_bytes = sum(row[1] for row in entries)
        removed = 0

        while entries and (len(entries) > self._max_files or total_bytes > self._max_total_bytes):
            _, size, victim = entries.pop(0)
            try:
                victim.unlink()
            except OSError:
                continue
            total_bytes -= size
            removed += 1

        return removed
