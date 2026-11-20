"""High-level stateless `HdcClient` with safe quoting and remote temp cleanup."""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from moyu_harmony_pilot.hdc.errors import (
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcCommandError,
    HdcError,
    HdcFileTransferError,
    HdcInvalidArgumentError,
    HdcTimeoutError,
)
from moyu_harmony_pilot.hdc.models import CommandResult, ConnectionState, HdcErrorKind, HdcTarget
from moyu_harmony_pilot.hdc.transport import (
    HdcTransport,
    SubprocessHdcTransport,
    classify_hdc_error,
)

REMOTE_TMP_PREFIX = "/data/local/tmp/moyu_pilot_"
_REMOTE_TMP_RE = re.compile(r"^/data/local/tmp/moyu_pilot_[A-Za-z0-9._-]+$")
_PURPOSE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")
_PARAM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_STATUS_MAP: dict[str, ConnectionState] = {
    "connected": ConnectionState.ONLINE,
    "online": ConnectionState.ONLINE,
    "offline": ConnectionState.OFFLINE,
    "unauthorized": ConnectionState.UNAUTHORIZED,
}


def build_safe_shell_command(argv: Sequence[str]) -> str:
    """Safely shell-quote an argument vector for `hdc shell` without raw splicing."""
    if not argv:
        raise HdcInvalidArgumentError("Remote shell command argv must not be empty.")
    for item in argv:
        if "\x00" in item or "\n" in item or "\r" in item:
            raise HdcInvalidArgumentError(
                "Remote shell arguments must not contain NUL or newline characters."
            )
    return shlex.join(argv)


def make_remote_tmp_path(purpose: str, suffix: str) -> str:
    """Generate a unique, strictly scoped remote temp path under `/data/local/tmp/`."""
    if not _PURPOSE_RE.match(purpose):
        raise HdcInvalidArgumentError(f"Invalid remote tmp purpose token: {purpose!r}")
    if not _SUFFIX_RE.match(suffix):
        raise HdcInvalidArgumentError(f"Invalid remote tmp suffix: {suffix!r}")
    token = uuid.uuid4().hex[:12]
    return f"{REMOTE_TMP_PREFIX}{purpose}_{token}{suffix}"


def validate_remote_tmp_path(remote_path: str) -> str:
    """Ensure a remote cleanup path strictly resides under our temporary prefix."""
    if ".." in remote_path or not _REMOTE_TMP_RE.match(remote_path):
        raise HdcInvalidArgumentError(
            f"Refusing to operate on unmanaged remote temporary path: {remote_path!r}"
        )
    return remote_path


def parse_targets_output(stdout: str) -> list[HdcTarget]:
    """Parse `hdc list targets -v` (or `hdc list targets`) output into `HdcTarget` objects."""
    targets: list[HdcTarget] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.lower() == "[empty]":
            continue
        if line.startswith("[Fail]"):
            continue

        # Official `hdc list targets -v` uses tabs, e.g. `SERIAL\t\tUSB\tConnected\tlocalhost`.
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if len(parts) == 1:
            parts = line.split()

        if len(parts) >= 3:
            serial = parts[0]
            transport = parts[1]
            raw_status = parts[2]
            host = parts[3] if len(parts) >= 4 else ""
            state = _STATUS_MAP.get(raw_status.lower(), ConnectionState.UNKNOWN)
        elif len(parts) == 2:
            serial = parts[0]
            transport = "UNKNOWN"
            raw_status = parts[1]
            host = ""
            state = _STATUS_MAP.get(raw_status.lower(), ConnectionState.UNKNOWN)
        else:
            serial = parts[0]
            transport = "UNKNOWN"
            raw_status = "Connected"
            host = ""
            state = ConnectionState.ONLINE

        targets.append(
            HdcTarget(
                serial=serial,
                transport=transport,
                raw_status=raw_status,
                connection_state=state,
                host=host,
            )
        )
    return targets


def _validate_target(target: str) -> str:
    cleaned = target.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned) or "\x00" in cleaned:
        raise HdcInvalidArgumentError("Explicit non-empty target serial is required.")
    return cleaned


def _validate_remote_path(remote_path: str) -> str:
    if (
        not remote_path
        or "\x00" in remote_path
        or "\n" in remote_path
        or "\r" in remote_path
    ):
        raise HdcInvalidArgumentError(f"Invalid remote file path: {remote_path!r}")
    return remote_path


def _effective_result(result: CommandResult, *, is_file_transfer: bool = False) -> CommandResult:
    """Re-verify error classification so even simple FakeTransports get exact error semantics."""
    classified = classify_hdc_error(
        result.returncode,
        result.stdout,
        result.stderr,
        is_file_transfer=is_file_transfer,
    )
    if result.error_kind != HdcErrorKind.NONE:
        return result
    if classified == HdcErrorKind.NONE:
        return result
    return CommandResult(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        error_kind=classified,
    )


def _raise_for_result(result: CommandResult, *, context: str) -> None:
    if result.ok:
        return

    detail = (result.combined_output.strip().splitlines() or ["no output"])[0][:240]
    if result.error_kind == HdcErrorKind.DEVICE_UNAUTHORIZED:
        raise DeviceUnauthorizedError(
            f"{context}: target device is unauthorized ({detail})",
            result=result,
        )
    if result.error_kind == HdcErrorKind.DEVICE_OFFLINE:
        raise DeviceOfflineError(
            f"{context}: target device is offline or unreachable ({detail})",
            result=result,
        )
    if result.error_kind == HdcErrorKind.TIMEOUT:
        raise HdcTimeoutError(
            f"{context}: hdc command timed out ({detail})",
            result=result,
        )
    if result.error_kind == HdcErrorKind.FILE_TRANSFER_FAILED:
        raise HdcFileTransferError(
            f"{context}: hdc file transfer failed ({detail})",
            result=result,
        )
    raise HdcCommandError(
        f"{context}: hdc command failed (rc={result.returncode}, detail={detail})",
        result=result,
    )


class HdcClient:
    """Stateless high-level wrapper over an `HdcTransport`."""

    def __init__(
        self,
        transport: HdcTransport | None = None,
        *,
        hdc_path: str | Path | None = None,
        default_timeout: float = 15.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._transport: HdcTransport = (
            transport
            if transport is not None
            else SubprocessHdcTransport(
                hdc_path=hdc_path,
                default_timeout=default_timeout,
                env=env,
            )
        )
        self._default_timeout = float(default_timeout)

    @property
    def transport(self) -> HdcTransport:
        """Underlying `HdcTransport` instance."""
        return self._transport

    @property
    def hdc_path(self) -> Path | None:
        """Return resolved hdc binary path when backed by `SubprocessHdcTransport`."""
        if isinstance(self._transport, SubprocessHdcTransport):
            return self._transport.hdc_path
        return None

    def version(self, *, timeout: float = 5.0) -> str:
        """Query official `hdc -v` version string (e.g. `3.2.0c`)."""
        result = _effective_result(self._transport.execute(["-v"], target=None, timeout=timeout))
        _raise_for_result(result, context="hdc -v")
        raw = result.stdout.strip()
        if raw.lower().startswith("ver:"):
            return raw.split(":", 1)[1].strip()
        return raw

    def list_targets(self, *, timeout: float = 10.0) -> list[HdcTarget]:
        """Enumerate targets via `hdc list targets -v` with normalized connection states."""
        result = _effective_result(
            self._transport.execute(["list", "targets", "-v"], target=None, timeout=timeout)
        )
        _raise_for_result(result, context="hdc list targets -v")
        return parse_targets_output(result.stdout)

    def shell(
        self,
        target: str,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Execute a safely quoted command inside `hdc -t <target> shell`."""
        valid_target = _validate_target(target)
        quoted = build_safe_shell_command(argv)
        raw_result = self._transport.execute(
            ["shell", quoted],
            target=valid_target,
            timeout=self._default_timeout if timeout is None else timeout,
        )
        result = _effective_result(raw_result)
        if check:
            _raise_for_result(result, context=f"hdc shell ({argv[0]})")
        return result

    def recv_file(
        self,
        target: str,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: float = 30.0,
    ) -> Path:
        """Receive a file from the HarmonyOS device using `hdc -t <target> file recv`."""
        valid_target = _validate_target(target)
        valid_remote = _validate_remote_path(remote_path)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        raw_result = self._transport.execute(
            ["file", "recv", valid_remote, str(dest)],
            target=valid_target,
            timeout=timeout,
        )
        result = _effective_result(raw_result, is_file_transfer=True)
        _raise_for_result(result, context="hdc file recv")

        if not dest.is_file():
            raise HdcFileTransferError(
                f"hdc file recv reported success but local file was not created: {dest}",
                result=result,
            )
        return dest

    def send_file(
        self,
        target: str,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        """Send a local file to the HarmonyOS device using `hdc -t <target> file send`."""
        valid_target = _validate_target(target)
        valid_remote = _validate_remote_path(remote_path)
        src = Path(local_path)
        if not src.is_file():
            raise HdcInvalidArgumentError(f"Local source file does not exist: {src}")

        raw_result = self._transport.execute(
            ["file", "send", str(src), valid_remote],
            target=valid_target,
            timeout=timeout,
        )
        result = _effective_result(raw_result, is_file_transfer=True)
        _raise_for_result(result, context="hdc file send")

    def install(
        self, target: str, packages: Sequence[str | Path], *, timeout: float = 120.0
    ) -> None:
        """Install/update one or more local HAPs using the official host command."""
        if isinstance(packages, (str, bytes, Path)) or not 1 <= len(packages) <= 32:
            raise HdcInvalidArgumentError("Provide between 1 and 32 HAP paths.")
        paths = [Path(package).expanduser().resolve() for package in packages]
        if any(path.suffix.lower() != ".hap" or not path.is_file() for path in paths):
            raise HdcInvalidArgumentError("Every package must be an existing HAP file.")
        self._package_command(
            target, ["install", *(str(path) for path in paths)], "install", timeout
        )

    def uninstall(self, target: str, bundle_name: str, *, timeout: float = 120.0) -> None:
        """Uninstall a named bundle once, without transport-level retries."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", bundle_name):
            raise HdcInvalidArgumentError("Invalid bundle name.")
        self._package_command(target, ["uninstall", bundle_name], "uninstall", timeout)

    def _package_command(
        self, target: str, argv: list[str], operation: str, timeout: float
    ) -> None:
        result = _effective_result(
            self._transport.execute(argv, target=_validate_target(target), timeout=timeout)
        )
        _raise_for_result(result, context=f"hdc {operation}")
        acknowledged = re.search(rf"\b{operation} bundle successfully\b", result.stdout, re.I)
        failed = re.search(r"\b(?:error|failed|failure)\b", result.combined_output, re.I)
        if not acknowledged or failed:
            raise HdcCommandError(
                "Package operation did not return a success acknowledgement.",
                result=result, retryable=False,
            )

    def cleanup_remote_tmp(
        self,
        target: str,
        remote_path: str,
        *,
        timeout: float = 10.0,
        ignore_errors: bool = False,
    ) -> bool:
        """Remove a managed `/data/local/tmp/moyu_pilot_*` temporary file on device."""
        valid_target = _validate_target(target)
        valid_remote = validate_remote_tmp_path(remote_path)
        try:
            result = self.shell(
                valid_target,
                ["rm", "-f", valid_remote],
                timeout=timeout,
                check=not ignore_errors,
            )
            return result.ok
        except HdcError:
            if ignore_errors:
                return False
            raise

    @contextmanager
    def remote_tmp_file(
        self,
        target: str,
        purpose: str,
        suffix: str,
        *,
        cleanup_timeout: float = 10.0,
    ) -> Iterator[str]:
        """Yield a unique managed remote tmp path and always clean it up on exit."""
        valid_target = _validate_target(target)
        remote_path = make_remote_tmp_path(purpose, suffix)
        try:
            yield remote_path
        finally:
            self.cleanup_remote_tmp(
                valid_target,
                remote_path,
                timeout=cleanup_timeout,
                ignore_errors=True,
            )

    def get_param(self, target: str, key: str, *, timeout: float = 10.0) -> str:
        """Read a HarmonyOS system property via `param get <key>`."""
        if not _PARAM_KEY_RE.match(key):
            raise HdcInvalidArgumentError(f"Invalid HarmonyOS system param key: {key!r}")
        result = self.shell(target, ["param", "get", key], timeout=timeout, check=True)
        return result.stdout.strip()
