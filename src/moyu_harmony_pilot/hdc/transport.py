"""Transport protocol, failure classification, and subprocess `hdc` execution."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from moyu_harmony_pilot.hdc.discovery import discover_hdc
from moyu_harmony_pilot.hdc.errors import HdcInvalidArgumentError, HdcTimeoutError
from moyu_harmony_pilot.hdc.models import CommandResult, HdcErrorKind

_FAIL_LINE_RE = re.compile(r"(?:^|\n)\s*\[Fail\]", re.IGNORECASE)
_UNAUTHORIZED_MARKERS = (
    "[fail][e000002]",
    "the device unauthorized",
    "device unauthorized",
    "unauthorized",
)
_OFFLINE_MARKERS = (
    "[fail][e000001]",
    "not match target founded",
    "executecommand need connect-key",
    "target not found",
    "device not found",
    "device offline",
    "connect server failed",
    "daemon not running",
    "connectkey",
    "no any target",
)
_FILE_TRANSFER_MARKERS = (
    "error opening file",
    "filetransfer",
    "file recv",
    "file send",
    "transfer failed",
)


def classify_hdc_error(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    is_file_transfer: bool = False,
) -> HdcErrorKind:
    """Classify an `hdc` process result using exit status and known CLI markers.

    Note that official `hdc 3.x` frequently exits with code `0` while emitting
    `[Fail]...` diagnostics on `stdout`.
    """
    combined = f"{stdout}\n{stderr}"
    lowered = combined.lower()
    has_fail_marker = bool(_FAIL_LINE_RE.search(combined))

    if has_fail_marker or returncode != 0:
        if any(marker in lowered for marker in _UNAUTHORIZED_MARKERS):
            # Avoid false positives on arbitrary text unless paired with fail/e000002/device.
            if (
                "[fail][e000002]" in lowered
                or "device unauthorized" in lowered
                or (has_fail_marker and "unauthorized" in lowered)
            ):
                return HdcErrorKind.DEVICE_UNAUTHORIZED

        if any(marker in lowered for marker in _OFFLINE_MARKERS):
            return HdcErrorKind.DEVICE_OFFLINE

        if is_file_transfer or any(marker in lowered for marker in _FILE_TRANSFER_MARKERS):
            return HdcErrorKind.FILE_TRANSFER_FAILED

        return HdcErrorKind.COMMAND_FAILED

    return HdcErrorKind.NONE


def _validate_cli_arg(value: str, *, field_name: str) -> None:
    if "\x00" in value:
        raise HdcInvalidArgumentError(f"{field_name} contains a NUL byte.")


@runtime_checkable
class HdcTransport(Protocol):
    """Low-level transport interface for executing official `hdc` commands."""

    def execute(
        self,
        args: Sequence[str],
        *,
        target: str | None = None,
        timeout: float = 15.0,
    ) -> CommandResult:
        """Run an `hdc` command and return a structured `CommandResult`."""


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Forcefully terminate a timed-out `hdc` subprocess and its process group."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.kill()
        except OSError:
            return
    except OSError:
        return


class SubprocessHdcTransport:
    """Default `HdcTransport` backed by official stateless `hdc` CLI invocations."""

    def __init__(
        self,
        hdc_path: str | Path | None = None,
        *,
        default_timeout: float = 15.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if default_timeout <= 0:
            raise HdcInvalidArgumentError("default_timeout must be positive.")
        self._env = dict(os.environ if env is None else env)
        self._hdc_path = discover_hdc(hdc_path, env=self._env)
        self._default_timeout = float(default_timeout)

    @property
    def hdc_path(self) -> Path:
        """Resolved executable path for the official `hdc` binary."""
        return self._hdc_path

    def execute(
        self,
        args: Sequence[str],
        *,
        target: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        effective_timeout = self._default_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise HdcInvalidArgumentError("timeout must be positive.")
        if not args:
            raise HdcInvalidArgumentError("hdc command args must not be empty.")

        cmd: list[str] = [str(self._hdc_path)]
        if target is not None:
            _validate_cli_arg(target, field_name="target")
            if not target.strip() or any(ch.isspace() for ch in target):
                raise HdcInvalidArgumentError("hdc target serial must not be empty or whitespace.")
            cmd.extend(["-t", target])

        for item in args:
            _validate_cli_arg(item, field_name="arg")
            cmd.append(item)

        is_file_transfer = len(args) >= 2 and args[0] == "file" and args[1] in {"recv", "send"}
        started = time.monotonic()

        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env,
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError as exc:
            raise HdcInvalidArgumentError(
                f"hdc binary disappeared or is not executable: {self._hdc_path}"
            ) from exc

        try:
            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(proc)
            try:
                out_after, err_after = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                out_after, err_after = "", ""
            duration_ms = int((time.monotonic() - started) * 1000)
            partial_stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            ) + (out_after or "")
            partial_stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            ) + (err_after or "")
            timeout_result = CommandResult(
                args=tuple(args),
                returncode=-9,
                stdout=partial_stdout,
                stderr=partial_stderr,
                duration_ms=duration_ms,
                error_kind=HdcErrorKind.TIMEOUT,
            )
            raise HdcTimeoutError(
                f"hdc command timed out after {effective_timeout:.1f}s.",
                result=timeout_result,
            ) from exc
        except BaseException:
            # An enclosing action deadline or SIGINT must also reap the active hdc process.
            _terminate_process_tree(proc)
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        returncode = proc.returncode if proc.returncode is not None else 1
        error_kind = classify_hdc_error(
            returncode,
            stdout,
            stderr,
            is_file_transfer=is_file_transfer,
        )
        return CommandResult(
            args=tuple(args),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            error_kind=error_kind,
        )
