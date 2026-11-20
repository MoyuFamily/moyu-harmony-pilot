"""Unified Agent-friendly JSON output envelope for `harmony-pilot --json`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CliErrorDetail:
    """Structured error object embedded in failed CLI JSON envelopes."""

    code: str
    message: str
    retryable: bool = False
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload


@dataclass(frozen=True, slots=True)
class CliEnvelope:
    """Top-level response envelope guaranteed on stdout in `--json` mode."""

    ok: bool
    command: str
    data: Any = None
    error: CliErrorDetail | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "command": self.command,
            "data": self.data,
            "error": None if self.error is None else self.error.to_dict(),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        separators = (",", ":") if indent is None else None
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            separators=separators,
        )


def ok_envelope(command: str, data: Any) -> CliEnvelope:
    """Build a successful `CliEnvelope`."""
    return CliEnvelope(ok=True, command=command, data=data, error=None)


def error_envelope(
    command: str,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    hint: str | None = None,
    data: Any = None,
) -> CliEnvelope:
    """Build a failed `CliEnvelope`."""
    return CliEnvelope(
        ok=False,
        command=command,
        data=data,
        error=CliErrorDetail(code=code, message=message, retryable=retryable, hint=hint),
    )
