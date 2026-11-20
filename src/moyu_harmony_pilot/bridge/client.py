"""Verified HTTPS JSON transport with redirect rejection and bounded bodies."""

from __future__ import annotations

import json
import ssl
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.bridge.config import BridgeConfig
from moyu_harmony_pilot.bridge.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_RESPONSE_BYTES,
    BridgeError,
    BridgeProtocolError,
    decode_object,
)


class JsonTransport(Protocol):
    def post(self, path: str, payload: dict, *, timeout: float | None = None) -> dict: ...


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("Broker redirects are rejected.", status=code, retryable=False)


class HttpBridgeClient:
    def __init__(self, config: BridgeConfig, *, opener=None) -> None:
        self.config = config
        self._opener = opener

    def post(self, path: str, payload: dict, *, timeout: float | None = None) -> dict:
        if not path.startswith("/bridge/v1/") or ".." in path or "?" in path or "#" in path:
            raise BridgeProtocolError("Invalid Bridge endpoint path.")
        try:
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_MESSAGE_BYTES:
                raise ValueError
        except (ValueError, TypeError, RecursionError) as exc:
            raise BridgeProtocolError("Bridge requests must fit within 16 KiB.") from exc
        request = Request(  # noqa: S310 - config validates HTTPS or explicit loopback HTTP
            self.config.broker_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.config.token,
                "Content-Type": "application/json; charset=utf-8",
                "X-Harmony-Pilot-Protocol": "1",
                "Accept": "application/json",
                "User-Agent": f"harmony-pilot/{__version__}",
            },
        )
        opener = self._opener or build_opener(
            _RejectRedirect(),
            HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with opener.open(request, timeout=timeout or self.config.request_timeout) as response:
                if response.status != 200:
                    raise BridgeError("Unexpected broker response status.", status=response.status)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type != "application/json":
                    raise BridgeProtocolError("Broker responses must use application/json.")
                return decode_object(response.read(MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise BridgeError(
                "Broker rejected the request.",
                status=status,
                retryable=status == 429 or status >= 500,
            ) from None
        except (URLError, OSError, TimeoutError, ValueError) as exc:
            raise BridgeError("Broker connection failed.") from exc
