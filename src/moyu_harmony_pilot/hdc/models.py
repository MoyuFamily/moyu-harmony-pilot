"""Core data models for Layer 1 (`moyu_harmony_pilot.hdc`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


def mask_serial(serial: str) -> str:
    """Return a privacy-safe redacted representation of a device serial."""
    cleaned = serial.strip()
    if not cleaned:
        return "***"
    if len(cleaned) <= 6:
        return "***"
    return f"{cleaned[:4]}***{cleaned[-4:]}"


class ConnectionState(StrEnum):
    """Normalized HarmonyOS device connection status."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


class HdcErrorKind(StrEnum):
    """Normalized error category for an `hdc` invocation."""

    NONE = "none"
    NOT_FOUND = "hdc_not_found"
    TIMEOUT = "hdc_timeout"
    DEVICE_OFFLINE = "device_offline"
    DEVICE_UNAUTHORIZED = "device_unauthorized"
    COMMAND_FAILED = "hdc_command_failed"
    FILE_TRANSFER_FAILED = "hdc_file_transfer_failed"


@dataclass(frozen=True, slots=True)
class HdcTarget:
    """Single target entry parsed from `hdc list targets -v`."""

    serial: str
    transport: str
    raw_status: str
    connection_state: ConnectionState
    host: str = ""

    @property
    def serial_masked(self) -> str:
        """Return a redacted device serial suitable for logs and telemetry."""
        return mask_serial(self.serial)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured outcome of a single `hdc` command execution."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    error_kind: HdcErrorKind = HdcErrorKind.NONE

    @property
    def ok(self) -> bool:
        """Return True iff exit status is zero and no hdc failure was detected."""
        return self.returncode == 0 and self.error_kind == HdcErrorKind.NONE

    @property
    def combined_output(self) -> str:
        """Return stdout and stderr joined for diagnostic parsing."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr
