"""Stable semantic exit codes and exception-to-exit-code mapping for `harmony-pilot`."""

from __future__ import annotations

from enum import IntEnum

from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceOverheatedError,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import (
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcError,
    HdcInvalidArgumentError,
    HdcNotFoundError,
)
from moyu_harmony_pilot.ui import UiConcurrentCallError, UiError, UiInvalidInputError
from moyu_harmony_pilot.vision import VisionError, VisionUnavailableError


class ExitCode(IntEnum):
    """Contractual Agent-friendly semantic exit codes (see docs/requirements.md FR-8)."""

    OK = 0
    FAILED = 1
    CONFIG_ERROR = 2
    DEVICE_OFFLINE = 3
    HUMAN_REQUIRED = 4
    DEVICE_OVERHEATED = 5
    DEVICE_BUSY = 6


EXIT_CODE_DESCRIPTIONS: dict[int, dict[str, str]] = {
    int(ExitCode.OK): {
        "name": "ok",
        "description": "Command succeeded (including already_done idempotent outcomes).",
    },
    int(ExitCode.FAILED): {
        "name": "failed",
        "description": "General execution failure, timeout, or unknown outcome.",
    },
    int(ExitCode.CONFIG_ERROR): {
        "name": "configuration_error",
        "description": "Invalid CLI arguments, invalid schema/config, or missing hdc binary.",
    },
    int(ExitCode.DEVICE_OFFLINE): {
        "name": "device_offline",
        "description": "Target device is disconnected, offline, or unauthorized for USB debugging.",
    },
    int(ExitCode.HUMAN_REQUIRED): {
        "name": "human_required",
        "description": (
            "Execution halted for mandatory human intervention (captcha/login/sensitive)."
        ),
    },
    int(ExitCode.DEVICE_OVERHEATED): {
        "name": "device_overheated",
        "description": (
            "Hardware thermal protection gate blocked execution due to battery temperature."
        ),
    },
    int(ExitCode.DEVICE_BUSY): {
        "name": "device_busy",
        "description": (
            "Target device is currently locked by another active process or uitest call."
        ),
    },
}


def classify_exception(
    exc: Exception,
) -> tuple[ExitCode, str, str, bool, str | None]:
    """Map any exception to `(exit_code, error_code, message, retryable, hint)`."""
    if isinstance(exc, VisionUnavailableError):
        return (ExitCode.CONFIG_ERROR, exc.code, str(exc), exc.retryable, exc.hint)
    if isinstance(exc, VisionError):
        return (ExitCode.FAILED, exc.code, str(exc), exc.retryable, exc.hint)
    if isinstance(exc, HumanRequiredError):
        return (ExitCode.HUMAN_REQUIRED, exc.code, exc.message, exc.retryable, exc.hint)

    if isinstance(exc, DeviceOverheatedError):
        return (ExitCode.DEVICE_OVERHEATED, exc.code, exc.message, exc.retryable, exc.hint)

    if isinstance(exc, DeviceBusyError | UiConcurrentCallError):
        return (ExitCode.DEVICE_BUSY, exc.code, exc.message, exc.retryable, exc.hint)

    if isinstance(exc, DeviceOfflineError | DeviceUnauthorizedError):
        return (ExitCode.DEVICE_OFFLINE, exc.code, exc.message, exc.retryable, exc.hint)

    if isinstance(
        exc,
        HdcNotFoundError
        | HdcInvalidArgumentError
        | DeviceConfigurationError
        | UiInvalidInputError,
    ):
        return (ExitCode.CONFIG_ERROR, exc.code, exc.message, exc.retryable, exc.hint)

    if isinstance(exc, HdcError | UiError | DeviceError):
        return (ExitCode.FAILED, exc.code, exc.message, exc.retryable, exc.hint)

    return (
        ExitCode.FAILED,
        "internal_error",
        f"Unexpected {type(exc).__name__}: {exc}",
        False,
        "Run with diagnostics or inspect stderr for details.",
    )
