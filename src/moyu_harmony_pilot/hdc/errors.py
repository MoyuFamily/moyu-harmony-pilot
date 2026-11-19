"""Structured exceptions for Layer 1 (`moyu_harmony_pilot.hdc`)."""

from __future__ import annotations

from moyu_harmony_pilot.hdc.models import CommandResult


class HdcError(Exception):
    """Base exception for all `hdc` layer failures."""

    default_code: str = "hdc_error"
    default_retryable: bool = False
    default_hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        hint: str | None = None,
        result: CommandResult | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.hint = self.default_hint if hint is None else hint
        self.result = result


class HdcNotFoundError(HdcError):
    """Raised when no executable official `hdc` binary can be located."""

    default_code = "hdc_not_found"
    default_retryable = False
    default_hint = (
        "Install official HarmonyOS/OpenHarmony hdc 3.x (see docs/device-setup.md) "
        "or set HARMONY_PILOT_HDC_PATH."
    )


class HdcInvalidArgumentError(HdcError):
    """Raised when invalid or unsafe parameters are passed to the hdc layer."""

    default_code = "configuration_error"
    default_retryable = False


class HdcTimeoutError(HdcError):
    """Raised when an `hdc` subprocess exceeds its wall-clock timeout."""

    default_code = "hdc_timeout"
    default_retryable = True
    default_hint = "Verify the device USB connection is responsive or increase the command timeout."


class DeviceOfflineError(HdcError):
    """Raised when the target device is missing, offline, or disconnected."""

    default_code = "device_offline"
    default_retryable = True
    default_hint = (
        "Check USB cable connection, ensure USB debugging is enabled, "
        "and run `harmony-pilot doctor`."
    )


class DeviceUnauthorizedError(HdcError):
    """Raised when the host public key has not been trusted on the device."""

    default_code = "device_unauthorized"
    default_retryable = False
    default_hint = (
        "Unlock the HarmonyOS device and accept the 'Allow USB debugging?' dialog "
        "(check 'Always allow this computer')."
    )


class HdcCommandError(HdcError):
    """Raised when an `hdc` command fails with a non-zero exit or `[Fail]` output."""

    default_code = "hdc_command_failed"
    default_retryable = False


class HdcFileTransferError(HdcError):
    """Raised when `hdc file recv` or `hdc file send` fails."""

    default_code = "hdc_file_transfer_failed"
    default_retryable = True
    default_hint = "Verify the remote path exists and the host output directory is writable."
