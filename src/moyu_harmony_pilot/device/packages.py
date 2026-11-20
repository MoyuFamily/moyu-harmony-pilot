"""Host-side HAP acquisition and bounded metadata validation using the standard library."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from moyu_harmony_pilot.device.apps import validate_bundle
from moyu_harmony_pilot.device.errors import DeviceConfigurationError, DeviceError


def hap_metadata(path: str | Path) -> dict:
    source = Path(path).expanduser().resolve()
    try:
        if source.suffix.lower() != ".hap":
            raise ValueError("HAP required")
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo("module.json")
            if info.file_size > 1024 * 1024:
                raise ValueError("Metadata too large")
            metadata = json.loads(archive.read(info))
        app = metadata["app"]
        bundle = validate_bundle(app["bundleName"])
        version = app["versionCode"]
        if type(version) is not int or version < 0:
            raise ValueError("Invalid version")
        with source.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        return {
            "path": str(source),
            "bundle_name": bundle,
            "version_code": version,
            "sha256": checksum,
        }
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise DeviceConfigurationError(
            "HAP requires readable app metadata in module.json."
        ) from exc


def _validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except (ValueError, TypeError) as exc:
        raise DeviceConfigurationError("Invalid package URL.") from exc
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ord(char) < 32 for char in url)
    ):
        raise DeviceConfigurationError("Use an HTTP(S) package URL without embedded credentials.")
    return url


class _PackageRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise DeviceConfigurationError("Package redirect downgraded HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_hap(
    url: str,
    destination: str | Path,
    *,
    sha256: str | None = None,
    timeout: float = 120,
    max_bytes: int = 512 * 1024 * 1024,
) -> dict:
    """Atomically acquire a bounded HAP, validating any supplied expected digest."""
    _validate_url(url)
    if not math.isfinite(timeout) or timeout <= 0 or type(max_bytes) is not int or max_bytes <= 0:
        raise DeviceConfigurationError("Download limits must be positive and finite.")
    if sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise DeviceConfigurationError("Expected SHA-256 must contain 64 hexadecimal digits.")
    output = Path(destination).expanduser().absolute()
    if output.suffix.lower() != ".hap":
        raise DeviceConfigurationError("Download destination must end in .hap.")
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd, temporary = tempfile.mkstemp(prefix=".moyu-download-", suffix=".hap", dir=output.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            request = Request(  # noqa: S310 -- HTTP(S) and redirect schemes validated above
                url, headers={"Accept-Encoding": "identity"}
            )
            with build_opener(_PackageRedirects()).open(request, timeout=min(timeout, 15)) as reply:
                expected_size = reply.headers.get("Content-Length")
                if expected_size is not None and not 0 <= int(expected_size) <= max_bytes:
                    raise DeviceConfigurationError("Package exceeds the download size limit.")
                received = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = reply.read1(64 * 1024)
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise DeviceConfigurationError("Package exceeds the download size limit.")
                    stream.write(chunk)
                if expected_size is not None and received != int(expected_size):
                    raise DeviceError("Incomplete package download.", code="download_failed")
            stream.flush()
            os.fsync(stream.fileno())
        result = hap_metadata(temp_path)
        if sha256 is not None and result["sha256"] != sha256.lower():
            raise DeviceConfigurationError("Package SHA-256 does not match.")
        os.replace(temp_path, output)
        result["path"] = str(output)
        return result
    except (OSError, ValueError) as exc:
        raise DeviceError(
            "Package download failed.", code="download_failed", retryable=True
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)
