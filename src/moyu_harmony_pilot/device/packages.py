"""Host-side HarmonyOS package acquisition, validation, and private APP expansion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.client import HTTPMessage
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from moyu_harmony_pilot.device.apps import validate_bundle
from moyu_harmony_pilot.device.errors import DeviceConfigurationError, DeviceError

MODULE_SUFFIXES = frozenset({".hap", ".hsp"})
PACKAGE_SUFFIXES = MODULE_SUFFIXES | {".app"}
MAX_INSTALL_MODULES = 32
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    entries = [entry for entry in archive.infolist() if entry.filename == name]
    if len(entries) != 1 or entries[0].file_size > MAX_METADATA_BYTES:
        raise ValueError("Missing, duplicate, or oversized package metadata")
    value = json.loads(archive.read(entries[0]))
    if not isinstance(value, dict):
        raise ValueError("Package metadata must be an object")
    return value


def _identity(bundle: str, version: int) -> tuple[str, int]:
    bundle = validate_bundle(bundle)
    if type(version) is not int or version < 0:
        raise ValueError("Invalid package version")
    return bundle, version


def _checksum(source: Path) -> str:
    with source.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def hap_metadata(path: str | Path) -> dict[str, Any]:
    """Read the identity and digest of a HAP or application-internal HSP."""
    source = Path(path).expanduser().resolve()
    try:
        if source.suffix.lower() not in MODULE_SUFFIXES:
            raise ValueError("HAP or HSP required")
        with zipfile.ZipFile(source) as archive:
            metadata = _read_json(archive, "module.json")
        app = metadata["app"]
        bundle, version = _identity(app["bundleName"], app["versionCode"])
        return {
            "path": str(source),
            "bundle_name": bundle,
            "version_code": version,
            "sha256": _checksum(source),
        }
    except (
        OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, zlib.error,
        RuntimeError, EOFError,
    ) as exc:
        raise DeviceConfigurationError(
            "HAP/HSP requires readable app metadata in module.json."
        ) from exc


def _expand_app(source: Path, directory: Path, remaining_bytes: int) -> list[Path]:
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 4096 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Too many or duplicate APP members")
        pack = _read_json(archive, "pack.info")
        app = pack["summary"]["app"]
        identity = _identity(app["bundleName"], app["version"]["code"])
        modules = sorted(
            (entry for entry in entries if Path(entry.filename).suffix.lower() in MODULE_SUFFIXES),
            key=lambda entry: entry.filename,
        )
        if not 1 <= len(modules) <= MAX_INSTALL_MODULES:
            raise ValueError("APP must contain between 1 and 32 HAP/HSP files")
        if sum(entry.file_size for entry in modules) > remaining_bytes:
            raise ValueError("Expanded APP modules exceed 512 MiB")
        paths = []
        for index, entry in enumerate(modules):
            name = PurePosixPath(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in entry.filename
                or ":" in entry.filename
                or entry.is_dir()
                or mode not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
            ):
                raise ValueError("Unsafe APP module member")
            # Generated names keep all writes inside the private staging directory.
            destination = directory / f"module-{index}{name.suffix.lower()}"
            with archive.open(entry) as incoming, destination.open("xb") as outgoing:
                os.fchmod(outgoing.fileno(), 0o600)
                shutil.copyfileobj(incoming, outgoing, length=64 * 1024)
            metadata = hap_metadata(destination)
            if (metadata["bundle_name"], metadata["version_code"]) != identity:
                raise ValueError("APP manifest and module identities differ")
            paths.append(destination)
        return paths


@contextmanager
def prepare_install_packages(
    packages: Sequence[str | Path], *, expand_apps: bool = True,
) -> Iterator[list[Path]]:
    """Validate one installation and optionally expand APPs for older device APIs.

    Extracted modules retain their exact bytes and exist only inside this context.
    Standalone HAP/HSP paths are retained, so callers can stage pinned copies first.
    """
    if isinstance(packages, (str, bytes, Path)) or not 1 <= len(packages) <= MAX_INSTALL_MODULES:
        raise DeviceConfigurationError("Provide between 1 and 32 package paths.")
    with tempfile.TemporaryDirectory(prefix="moyu-packages-") as temporary:
        paths = []
        sources = []
        remaining_bytes = MAX_EXPANDED_BYTES
        try:
            for index, package in enumerate(packages):
                source = Path(package).expanduser().resolve()
                if source.suffix.lower() not in PACKAGE_SUFFIXES or not source.is_file():
                    raise ValueError("Expected an existing .hap, .hsp, or .app file")
                sources.append(source)
                if source.suffix.lower() == ".app":
                    directory = Path(temporary) / str(index)
                    directory.mkdir(mode=0o700)
                    expanded = _expand_app(source, directory, remaining_bytes)
                    remaining_bytes -= sum(path.stat().st_size for path in expanded)
                    paths.extend(expanded)
                else:
                    paths.append(source)
                if len(paths) > MAX_INSTALL_MODULES:
                    raise ValueError("Installation exceeds 32 HAP/HSP modules")
            identities = {
                (metadata["bundle_name"], metadata["version_code"])
                for metadata in (hap_metadata(path) for path in paths)
            }
            if len(identities) != 1:
                raise ValueError("All modules must have the same bundle and version")
        except (
            OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, zlib.error,
            RuntimeError, EOFError,
        ) as exc:
            raise DeviceConfigurationError(f"Invalid installation package: {exc}") from exc
        yield paths if expand_apps else sources


def package_metadata(path: str | Path) -> dict[str, Any]:
    """Read source identity, retaining the original APP path and digest for the ledger."""
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() in MODULE_SUFFIXES:
        return hap_metadata(source)
    with prepare_install_packages([source]) as modules:
        metadata = hap_metadata(modules[0])
        return {
            **metadata,
            "path": str(source),
            "sha256": _checksum(source),
            "module_count": len(modules),
            "expanded_bytes": sum(module.stat().st_size for module in modules),
        }


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
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str,
    ) -> Request | None:
        _validate_url(newurl)
        if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise DeviceConfigurationError("Package redirect downgraded HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_package(
    url: str,
    destination: str | Path,
    *,
    sha256: str | None = None,
    timeout: float = 120,
    max_bytes: int = 512 * 1024 * 1024,
) -> dict[str, Any]:
    """Atomically acquire a HAP/HSP/APP, validating structure and the expected digest."""
    _validate_url(url)
    if not math.isfinite(timeout) or timeout <= 0 or type(max_bytes) is not int or max_bytes <= 0:
        raise DeviceConfigurationError("Download limits must be positive and finite.")
    if sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise DeviceConfigurationError("Expected SHA-256 must contain 64 hexadecimal digits.")
    output = Path(destination).expanduser().absolute()
    suffix = output.suffix.lower()
    if suffix not in PACKAGE_SUFFIXES:
        raise DeviceConfigurationError("Download destination must end in .hap, .hsp, or .app.")
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd, temporary = tempfile.mkstemp(prefix=".moyu-download-", suffix=suffix, dir=output.parent)
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
        result = package_metadata(temp_path)
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


def download_hap(
    url: str,
    destination: str | Path,
    *,
    sha256: str | None = None,
    timeout: float = 120,
    max_bytes: int = 512 * 1024 * 1024,
) -> dict[str, Any]:
    """Backward-compatible HAP download entry point."""
    if Path(destination).suffix.lower() != ".hap":
        raise DeviceConfigurationError("Download destination must end in .hap.")
    return download_package(url, destination, sha256=sha256, timeout=timeout, max_bytes=max_bytes)
