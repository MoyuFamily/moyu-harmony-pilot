"""Bounded protocol-v1 messages and sanitized transport errors."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Never
from urllib.parse import urlsplit

from moyu_harmony_pilot.actions.validation import json_copy
from moyu_harmony_pilot.device import DeviceError

MAX_MESSAGE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class BridgeError(DeviceError):
    default_code = "bridge_error"
    default_retryable = True

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)
        self.status = status


class BridgeProtocolError(BridgeError):
    def __init__(self, message: str = "Invalid broker protocol message.") -> None:
        super().__init__(message, retryable=False)


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime | None = None) -> str:
    return (
        (value or utc_now())
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_time(value: Any) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise BridgeProtocolError(
            "Protocol timestamps require timezone-aware ISO-8601 values."
        ) from exc


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise BridgeProtocolError("Invalid protocol identifier.")
    return value


def opaque(value: Any, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise BridgeProtocolError("Invalid opaque protocol value.")
    return value


def number(value: Any, *, minimum: float, maximum: float) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise BridgeProtocolError("Protocol numeric value is outside its allowed range.")
    return float(value)


def broker_url(value: Any) -> str:
    try:
        if not isinstance(value, str) or any(char.isspace() for char in value):
            raise ValueError
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost"}
        ):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError) as exc:
        raise BridgeProtocolError(
            "Use an HTTPS broker URL or an explicit localhost HTTP endpoint."
        ) from exc


def decode_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value: str) -> Never:
        raise ValueError

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed

    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
            parse_float=finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeProtocolError("Broker messages must be bounded finite JSON objects.") from exc


@dataclass(frozen=True)
class Job:
    job_id: str
    device_alias: str
    action: str
    idempotency_key: str = field(repr=False)
    params: dict[str, Any] = field(repr=False)
    timeout_seconds: float
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    created_at: datetime
    expires_at: datetime

    @classmethod
    def parse(cls, payload: Any) -> Job:
        fields = {
            "jobId",
            "deviceAlias",
            "action",
            "idempotencyKey",
            "params",
            "timeoutSeconds",
            "leaseToken",
            "leaseExpiresAt",
            "createdAt",
            "expiresAt",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise BridgeProtocolError("Job fields differ from protocol v1.")
        try:
            params = json_copy(payload["params"])
            if not isinstance(params, dict):
                raise ValueError
        except Exception as exc:
            raise BridgeProtocolError("Invalid action parameter object.") from exc
        job = cls(
            identifier(payload["jobId"]),
            identifier(payload["deviceAlias"]),
            identifier(payload["action"]),
            opaque(payload["idempotencyKey"]),
            params,
            number(payload["timeoutSeconds"], minimum=0.001, maximum=3600),
            opaque(payload["leaseToken"], limit=1024),
            parse_time(payload["leaseExpiresAt"]),
            parse_time(payload["createdAt"]),
            parse_time(payload["expiresAt"]),
        )
        if job.expires_at <= job.created_at:
            raise BridgeProtocolError("Job expiration must follow creation.")
        return job
