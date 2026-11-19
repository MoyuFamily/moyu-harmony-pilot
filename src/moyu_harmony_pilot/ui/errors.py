"""Structured exceptions for Layer 2 (`moyu_harmony_pilot.ui`)."""

from __future__ import annotations


class UiError(Exception):
    """Base exception for all `ui` layer operations."""

    default_code: str = "ui_error"
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


class UiParseError(UiError):
    """Raised when `uitest dumpLayout` JSON is malformed or truncated."""

    default_code = "ui_parse_error"
    default_retryable = True
    default_hint = "Retry dumpLayout; if the screen is mid-transition, wait briefly before dumping."


class UiElementNotFoundError(UiError):
    """Raised when a selector or compact `@eN` reference cannot be resolved."""

    default_code = "element_not_found"
    default_retryable = False


class UiTimeoutError(UiError):
    """Raised when `wait_for` or `wait_gone` expires before the condition is met."""

    default_code = "ui_wait_timeout"
    default_retryable = True


class UiConcurrentCallError(UiError):
    """Raised when HarmonyOS `uitest` rejects concurrent invocations."""

    default_code = "device_busy"
    default_retryable = True
    default_hint = "Ensure only one process accesses this device at a time via the device lock."


class UiInvalidInputError(UiError):
    """Raised when coordinates, ratios, key names, or gesture parameters are invalid."""

    default_code = "configuration_error"
    default_retryable = False
