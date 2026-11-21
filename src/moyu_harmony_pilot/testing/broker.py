"""In-memory protocol-v1 conformance broker with an optional urllib opener facade.

Inject this transport into BridgeWorker, or pass it as HttpBridgeClient's opener
to exercise HTTP headers and JSON encoding. It never opens a network listener.
"""

from __future__ import annotations

import hmac
import io
import json
import secrets
import threading
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from moyu_harmony_pilot.actions import ActionOutcome
from moyu_harmony_pilot.actions.validation import json_copy
from moyu_harmony_pilot.bridge.protocol import (
    MAX_MESSAGE_BYTES,
    BridgeError,
    BridgeProtocolError,
    Job,
    decode_object,
    identifier,
    number,
    parse_time,
    timestamp,
    utc_now,
)


def _fields(payload: Any, required: set[str], optional: set[str] | None = None) -> None:
    if (not isinstance(payload, dict) or not required <= payload.keys()
            or payload.keys() - required - (optional or set())):
        raise BridgeProtocolError("Mock broker request fields differ from protocol v1.")


class _Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json; charset=utf-8"}


class MockBroker:
    """Validate four Bridge endpoints against a finite queue of synthetic fixtures."""

    def __init__(self, bridge_id: str = "test-worker", *, token: str | None = None) -> None:
        self.bridge_id = identifier(bridge_id)
        self._token = token if token is not None else secrets.token_urlsafe(24)
        self._lock = threading.RLock()
        self._queued: list[dict[str, Any]] = []
        self._leased: dict[str, Job] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancel_requested: set[str] = set()

    def enqueue(self, payload: dict[str, Any]) -> None:
        job = Job.parse(payload)
        with self._lock:
            if job.job_id in self._leased or any(p["jobId"] == job.job_id for p in self._queued):
                raise BridgeProtocolError("Mock fixture job IDs must be unique.")
            self._queued.append(json_copy(payload))

    def open(self, request: Request, timeout: float | None = None) -> _Response:
        """urllib-compatible opener exercising authentication and real JSON wire payloads."""
        headers = {key.lower(): value for key, value in request.header_items()}
        if not hmac.compare_digest(
            headers.get("authorization", ""), "Bearer " + self._token
        ):
            raise BridgeError("Mock bearer rejected.", status=401, retryable=False)
        if request.get_method() != "POST" or headers.get("x-harmony-pilot-protocol") != "1":
            raise BridgeProtocolError("Mock broker requires protocol-v1 POST requests.")
        if headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise BridgeProtocolError("Mock broker requires JSON requests.")
        raw = request.data
        if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
            raise BridgeProtocolError("Mock broker requests must fit within 16 KiB.")
        parsed = urlsplit(request.full_url)
        if parsed.query or parsed.fragment:
            raise BridgeProtocolError("Unexpected endpoint query or fragment.")
        result = self.post(parsed.path, decode_object(raw), timeout=timeout)
        return _Response(json.dumps(result, allow_nan=False).encode())

    def post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        clean = json_copy(payload)
        if not isinstance(clean, dict) or clean.get("bridgeId") != self.bridge_id:
            raise BridgeProtocolError("Unexpected bridge identity.")
        reply: dict[str, Any]
        with self._lock:
            if path == "/bridge/v1/heartbeat":
                reply = self._heartbeat(clean)
            elif path == "/bridge/v1/jobs/claim":
                reply = self._claim(clean)
            else:
                parts = path.split("/")
                if len(parts) != 6 or parts[:4] != ["", "bridge", "v1", "jobs"]:
                    raise BridgeProtocolError("Unknown mock endpoint.")
                job_id, operation = identifier(parts[4]), parts[5]
                job = self._leased.get(job_id)
                if job is None or clean.get("leaseToken") != job.lease_token:
                    raise BridgeError("Mock lease rejected.", status=409, retryable=False)
                if operation == "lease":
                    reply = self._lease(job, clean)
                elif operation == "complete":
                    reply = self._complete(job, clean)
                else:
                    raise BridgeProtocolError("Unknown mock endpoint.")
            self.calls.append((path, clean))
            copied: dict[str, Any] = json_copy(reply)
            return copied

    def _heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        _fields(payload, {"bridgeId", "pilotVersion", "hostPlatform", "sentAt", "devices"})
        parse_time(payload["sentAt"])
        for field in ("pilotVersion", "hostPlatform"):
            identifier(payload[field])
        devices = payload["devices"]
        if not isinstance(devices, list) or len(devices) > 32:
            raise BridgeProtocolError("Invalid mock heartbeat device list.")
        aliases: set[str] = set()
        for row in devices:
            _fields(row, {"deviceAlias", "connectionState", "osFamily", "osVersion",
                          "batteryPercent", "charging", "temperatureC", "screenState",
                          "busy", "capabilities"})
            alias = identifier(row["deviceAlias"])
            if alias in aliases or row["osFamily"] != "harmonyos-next":
                raise BridgeProtocolError("Invalid or duplicate heartbeat device.")
            aliases.add(alias)
            if row["connectionState"] not in {"online", "offline", "unauthorized", "unknown"}:
                raise BridgeProtocolError("Invalid connection state.")
            if row["screenState"] not in {"on", "off", "unknown"} or type(row["busy"]) is not bool:
                raise BridgeProtocolError("Invalid device state.")
            if row["osVersion"] is not None and not isinstance(row["osVersion"], str):
                raise BridgeProtocolError("Invalid OS version.")
            if row["charging"] is not None and type(row["charging"]) is not bool:
                raise BridgeProtocolError("Invalid charging state.")
            for field, low, high in [("batteryPercent", 0, 100), ("temperatureC", -100, 200)]:
                if row[field] is not None:
                    number(row[field], minimum=low, maximum=high)
            self._capabilities(row["capabilities"])
        return {"ok": True, "serverTime": timestamp(),
                "recommendedPollIntervalSeconds": 1, "hasPendingJobs": bool(self._queued)}

    @staticmethod
    def _capabilities(value: Any) -> set[str]:
        if not isinstance(value, list) or len(value) > 64:
            raise BridgeProtocolError("Invalid capability list.")
        result = {identifier(item) for item in value}
        if len(result) != len(value):
            raise BridgeProtocolError("Duplicate capabilities.")
        return result

    def _claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        _fields(payload, {"bridgeId", "maxJobs", "waitSeconds", "availableDevices"})
        maximum = payload["maxJobs"]
        wait = payload["waitSeconds"]
        if type(maximum) is not int or not 1 <= maximum <= 32:
            raise BridgeProtocolError("Invalid maxJobs.")
        if type(wait) is not int or not 0 <= wait <= 25:
            raise BridgeProtocolError("Invalid waitSeconds.")
        rows = payload["availableDevices"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32 or maximum > len(rows):
            raise BridgeProtocolError("Invalid available device list.")
        available = {}
        for row in rows:
            _fields(row, {"deviceAlias", "capabilities"})
            alias = identifier(row["deviceAlias"])
            if alias in available:
                raise BridgeProtocolError("Duplicate available device.")
            available[alias] = self._capabilities(row["capabilities"])
        busy = {job.device_alias for key, job in self._leased.items() if key not in self.receipts}
        selected: list[dict[str, Any]] = []
        for raw in list(self._queued):
            job = Job.parse(raw)
            if job.expires_at <= utc_now() or job.lease_expires_at <= utc_now():
                self._queued.remove(raw)
                continue
            if (job.device_alias in busy or job.action not in available.get(job.device_alias, set())
                    or len(selected) == maximum):
                continue
            selected.append(raw)
            self._queued.remove(raw)
            self._leased[job.job_id] = job
            busy.add(job.device_alias)
        return {"jobs": selected}

    def _lease(self, job: Job, payload: dict[str, Any]) -> dict[str, Any]:
        _fields(payload, {"bridgeId", "leaseToken", "extendSeconds"}, {"stage"})
        seconds = number(payload["extendSeconds"], minimum=0.001, maximum=3600)
        if payload.get("stage", "preparing") not in {
            "preparing", "inspect", "submit", "confirm", "cleaning"
        }:
            raise BridgeProtocolError("Invalid lease stage.")
        now = utc_now()
        if (job.job_id in self.receipts or job.lease_expires_at <= now
                or job.expires_at <= now):
            raise BridgeError("Mock job is closed.", status=410, retryable=False)
        renewed = min(now + timedelta(seconds=seconds), job.expires_at)
        self._leased[job.job_id] = replace(job, lease_expires_at=renewed)
        return {"ok": True, "leaseExpiresAt": timestamp(renewed),
                "cancelRequested": job.job_id in self.cancel_requested}

    def _complete(self, job: Job, payload: dict[str, Any]) -> dict[str, Any]:
        _fields(payload, {"bridgeId", "leaseToken", "status", "errorCode", "summary",
                          "startedAt", "finishedAt", "durationMs", "metrics",
                          "hasLocalDiagnostics"})
        if payload["status"] not in {"succeeded", "already_done", "human_required",
                                     "device_offline", "device_overheated", "failed", "unknown"}:
            raise BridgeProtocolError("Invalid execution terminal status.")
        if parse_time(payload["finishedAt"]) < parse_time(payload["startedAt"]):
            raise BridgeProtocolError("Receipt finishes before it starts.")
        if type(payload["durationMs"]) is not int or payload["durationMs"] < 0:
            raise BridgeProtocolError("Invalid receipt duration.")
        ActionOutcome(
            status=payload["status"], summary=payload["summary"], metrics=payload["metrics"],
            error_code=payload["errorCode"], has_local_diagnostics=payload["hasLocalDiagnostics"],
        ).to_dict()
        previous = self.receipts.get(job.job_id)
        if previous is not None:
            if json.dumps(previous, sort_keys=True) != json.dumps(payload, sort_keys=True):
                raise BridgeError("Conflicting completion receipt.", status=409, retryable=False)
            return {"ok": True, "jobId": job.job_id, "status": previous["status"]}
        if job.lease_expires_at <= utc_now() or job.expires_at <= utc_now():
            raise BridgeError("Mock job is expired.", status=410, retryable=False)
        self.receipts[job.job_id] = json_copy(payload)
        return {"ok": True, "jobId": job.job_id, "status": payload["status"]}
