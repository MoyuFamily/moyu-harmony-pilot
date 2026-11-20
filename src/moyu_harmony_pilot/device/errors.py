"""Structured exceptions for Layer 3 (`moyu_harmony_pilot.device`)."""

from __future__ import annotations


class DeviceError(Exception):
    """Base exception for device management, locking, telemetry, and hardware guards."""

    default_code: str = "device_error"
    default_retryable: bool = False
    default_hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.hint = self.default_hint if hint is None else hint


class DeviceConfigurationError(DeviceError):
    """Raised when local device configuration, alias mapping, or arguments are invalid."""

    default_code = "configuration_error"
    default_retryable = False


class UnknownDeviceAliasError(DeviceConfigurationError):
    """Raised when a requested device alias cannot be resolved from config or targets."""

    default_code = "configuration_error"
    default_retryable = False
    default_hint = (
        "Run `harmony-pilot devices` to inspect connected targets or configure devices.local.yaml."
    )


class DeviceBusyError(DeviceError):
    """Raised when another task already holds the exclusive per-device lock."""

    default_code = "device_busy"
    default_retryable = True
    default_hint = "Wait for the active task on this device to finish and retry."


class DeviceOverheatedError(DeviceError):
    """Raised when battery temperature reaches or exceeds the configured protection limit."""

    default_code = "device_overheated"
    default_retryable = True
    default_hint = (
        "Pause non-diagnostic tasks until battery temperature cools below the threshold."
    )


class HumanRequiredError(DeviceError):
    """Raised when automation must stop and hand control back to a human operator."""

    default_code = "human_required"
    default_retryable = False
    default_hint = "Complete verification, login, or sensitive confirmation manually on device."
