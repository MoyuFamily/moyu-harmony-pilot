"""Outbound protocol-v1 worker with durable completion delivery and per-device allowlists."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import random
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionOutcome, ActionRegistry, ActionRunner
from moyu_harmony_pilot.bridge.client import HttpBridgeClient, JsonTransport
from moyu_harmony_pilot.bridge.config import BridgeConfig, validate_configuration
from moyu_harmony_pilot.bridge.lease import LeaseKeeper
from moyu_harmony_pilot.bridge.protocol import (
    MAX_MESSAGE_BYTES,
    BridgeError,
    BridgeProtocolError,
    Job,
    number,
    parse_time,
    timestamp,
    utc_now,
)
from moyu_harmony_pilot.bridge.store import ReceiptStore
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceLock,
    HarmonyDevice,
    ensure_secure_dir,
    write_secure_text,
)
from moyu_harmony_pilot.hdc import ConnectionState, HdcClient, HdcError


def worker_directory(config: BridgeConfig) -> Path:
    namespace = hashlib.sha256((config.broker_url + "\n" + config.bridge_id).encode()).hexdigest()
    return config.state_dir / "bridge" / namespace


def health_status(config: BridgeConfig) -> dict:
    try:
        payload = json.loads((worker_directory(config) / "health.json").read_text())
        age = (utc_now() - parse_time(payload["heartbeat_at"])).total_seconds()
        fresh = (
            0
            <= age
            <= max(3 * config.heartbeat_seconds, 2 * config.request_timeout + config.poll_seconds)
        )
        return {
            "healthy": fresh,
            "bridge_id": config.bridge_id,
            "heartbeat_at": payload["heartbeat_at"],
            "age_seconds": round(age, 1),
        }
    except (OSError, ValueError, KeyError, BridgeProtocolError):
        return {"healthy": False, "bridge_id": config.bridge_id, "heartbeat_at": None}


class BridgeWorker:
    def __init__(
        self,
        config: BridgeConfig,
        hdc: HdcClient,
        *,
        client: JsonTransport | None = None,
        registry: ActionRegistry | None = None,
        env=None,
    ) -> None:
        self.config = config
        self.hdc = hdc
        self.client = client if client is not None else HttpBridgeClient(config)
        devices, self.registry = validate_configuration(config, registry=registry, env=env)
        self.devices = {}
        for alias in config.allowed_actions:
            profile = devices.get_by_alias(alias)
            if profile is None:
                raise DeviceConfigurationError("Bridge allowlist refers to an unconfigured alias.")
            self.devices[alias] = HarmonyDevice(hdc, profile, state_dir=config.state_dir)
        self.runner = ActionRunner(self.registry, state_dir=config.state_dir)
        ensure_secure_dir(config.state_dir)
        ensure_secure_dir(config.state_dir / "bridge")
        self.directory = ensure_secure_dir(worker_directory(config))
        namespace = self.directory.name
        self.store = ReceiptStore(self.directory / "receipts.sqlite3", config.bridge_id)
        self.lock = DeviceLock(
            "bridge-" + namespace,
            alias=config.bridge_id,
            lock_dir=config.state_dir / "bridge-locks",
        )
        self.stop = threading.Event()
        self.poll_seconds = config.poll_seconds
        self.last_heartbeat = float("-inf")
        self.has_pending_jobs = False
        self._heartbeat_lock = threading.Lock()
        self.inventory: list[dict] = []
        self.counts = {"claimed": 0, "completed": 0, "recovered": 0, "errors": 0}

    def _inventory(self, *, telemetry: bool = True) -> list[dict]:
        try:
            targets = {
                target.serial: target
                for target in self.hdc.list_targets(timeout=self.config.request_timeout)
            }
        except HdcError:
            targets = {}
        rows = []
        blocked = self.store.blocked_aliases()
        for alias, device in self.devices.items():
            target = targets.get(device.profile.serial)
            state = target.connection_state if target else ConnectionState.OFFLINE
            row = {
                "deviceAlias": alias,
                "connectionState": state.value,
                "osFamily": "harmonyos-next",
                "osVersion": None,
                "batteryPercent": None,
                "charging": None,
                "temperatureC": None,
                "screenState": "unknown",
                "busy": device.is_busy() or alias in blocked,
                "capabilities": list(self.config.allowed_actions[alias]),
            }
            if telemetry and state == ConnectionState.ONLINE and not row["busy"]:
                try:
                    with device.acquire_lock():
                        health = device.get_health(timeout=self.config.request_timeout)
                    row.update(
                        batteryPercent=health.battery.battery_percent,
                        charging=health.battery.charging,
                        temperatureC=health.battery.temperature_c,
                        screenState=health.power.screen_state.value,
                    )
                except DeviceBusyError:
                    row["busy"] = True
                except HdcError:
                    row["connectionState"] = "unknown"
            rows.append(row)
        return rows

    def heartbeat(self, *, active_alias: str | None = None) -> None:
        with self._heartbeat_lock:
            self._heartbeat(active_alias=active_alias)

    def _heartbeat(self, *, active_alias: str | None = None) -> None:
        if time.monotonic() - self.last_heartbeat < self.config.heartbeat_seconds:
            return
        rows = [dict(row) for row in self.inventory]
        for row in rows:
            if row["deviceAlias"] == active_alias:
                row.update(
                    busy=True,
                    batteryPercent=None,
                    charging=None,
                    temperatureC=None,
                    screenState="unknown",
                )
        response = self.client.post(
            "/bridge/v1/heartbeat",
            {
                "bridgeId": self.config.bridge_id,
                "pilotVersion": __version__,
                "hostPlatform": platform.system().lower() + "-" + platform.machine(),
                "sentAt": timestamp(),
                "devices": rows,
            },
        )
        if response.get("ok") is not True:
            raise BridgeProtocolError("Broker rejected heartbeat.")
        parse_time(response.get("serverTime"))
        pending = response.get("hasPendingJobs", False)
        if type(pending) is not bool:
            raise BridgeProtocolError("Invalid heartbeat pending-job flag.")
        self.poll_seconds = number(
            response.get("recommendedPollIntervalSeconds", self.poll_seconds),
            minimum=0.1,
            maximum=300,
        )
        self.has_pending_jobs = pending
        temporary = self.directory / "health.tmp"
        write_secure_text(
            temporary,
            json.dumps(
                {
                    "bridge_id": self.config.bridge_id,
                    "heartbeat_at": timestamp(),
                    "active_alias": active_alias,
                    **self.counts,
                }
            ),
        )

        os.replace(temporary, self.directory / "health.json")
        self.last_heartbeat = time.monotonic()

    def flush_receipts(self) -> None:
        for job_id, payload in self.store.pending():
            try:
                reply = self.client.post(f"/bridge/v1/jobs/{job_id}/complete", payload)
                if (
                    reply.get("ok") is not True
                    or reply.get("jobId") != job_id
                    or reply.get("status") != payload["status"]
                ):
                    raise BridgeProtocolError("Invalid completion acknowledgement.")
            except BridgeError as exc:
                if exc.status not in {409, 410}:
                    raise
            self.store.acknowledge(job_id)

    def _receipt(self, job: Job, outcome: ActionOutcome, started: str, duration: float) -> dict:
        result = outcome.to_dict()
        secrets = [
            self.config.token,
            job.lease_token,
            *(device.profile.serial for device in self.devices.values()),
        ]
        metrics = {
            key: value
            for key, value in result["metrics"].items()
            if not any(secret in key for secret in secrets)
            and (not isinstance(value, str) or not any(secret in value for secret in secrets))
        }
        status = result["status"]
        error = result["error_code"]
        if error and any(secret in error for secret in secrets):
            error = "action_error"
        if status == "configuration_error":
            status, error = "failed", "configuration_error"
        payload = {
            "bridgeId": self.config.bridge_id,
            "leaseToken": job.lease_token,
            "status": status,
            "errorCode": error,
            "summary": "Action execution " + status + ".",
            "startedAt": started,
            "finishedAt": timestamp(),
            "durationMs": max(0, round(duration * 1000)),
            "metrics": metrics,
            "hasLocalDiagnostics": result["has_local_diagnostics"],
        }
        if len(json.dumps(payload).encode()) > MAX_MESSAGE_BYTES:
            payload["metrics"] = {}
        return payload

    def _execute(self, job: Job) -> None:
        if not self.store.reserve(job):
            return
        started_at = timestamp()
        started_clock = time.monotonic()
        try:
            if job.action not in self.config.allowed_actions.get(job.device_alias, ()):
                raise DeviceConfigurationError("Job is outside the per-device allowlist.")
            if self.stop.is_set() or utc_now() >= min(job.expires_at, job.lease_expires_at):
                outcome = ActionOutcome(
                    status="failed",
                    error_code="job_expired",
                    summary="Job is no longer executable.",
                )
            else:
                with LeaseKeeper(
                    job,
                    self.config,
                    self.client,
                    self.stop,
                    lambda: self.heartbeat(active_alias=job.device_alias),
                ) as lease:
                    outcome = self.runner.run(
                        self.devices[job.device_alias],
                        job.action,
                        job.params,
                        remote=True,
                        idempotency_key=job.idempotency_key,
                        timeout=min(
                            job.timeout_seconds, (job.expires_at - utc_now()).total_seconds()
                        ),
                        cancelled=lease.cancelled,
                        on_stage=lease.set_stage,
                    )
        except DeviceConfigurationError:
            outcome = ActionOutcome(status="configuration_error", error_code="configuration_error")
        except DeviceBusyError:
            outcome = ActionOutcome(status="failed", error_code="device_busy")
        except Exception:
            outcome = ActionOutcome(status="unknown", error_code="worker_error")
        self.store.finish(
            job.job_id, self._receipt(job, outcome, started_at, time.monotonic() - started_clock)
        )
        self.counts["completed"] += 1

    def _cycle(self) -> dict:
        self.counts["recovered"] += self.store.recover()
        self.flush_receipts()
        if self.stop.is_set():
            return dict(self.counts)
        self.inventory = self._inventory(
            telemetry=time.monotonic() - self.last_heartbeat >= self.config.heartbeat_seconds
        )
        self.heartbeat()
        available = [
            {"deviceAlias": row["deviceAlias"], "capabilities": row["capabilities"]}
            for row in self.inventory
            if row["connectionState"] == "online" and not row["busy"] and row["capabilities"]
        ]
        if available and not self.stop.is_set():
            wait_seconds = 0 if self.has_pending_jobs else self.config.wait_seconds
            reply = self.client.post(
                "/bridge/v1/jobs/claim",
                {
                    "bridgeId": self.config.bridge_id,
                    "maxJobs": 1,
                    "waitSeconds": wait_seconds,
                    "availableDevices": available,
                },
                timeout=wait_seconds + self.config.request_timeout,
            )
            jobs = reply.get("jobs")
            if not isinstance(jobs, list) or len(jobs) > 1:
                raise BridgeProtocolError("Broker claim must contain at most one job.")
            self.has_pending_jobs = bool(jobs)
            for payload in jobs:
                job = Job.parse(payload)
                self.counts["claimed"] += 1
                self._execute(job)
            self.flush_receipts()
        else:
            self.has_pending_jobs = False
        return dict(self.counts)

    def run_once(self) -> dict:
        with self.lock, self._signals():
            return self._cycle()

    def probe(self) -> dict:
        """Publish read-only inventory without claiming jobs or changing saved receipts."""
        with self.lock:
            self.inventory = self._inventory()
            self.last_heartbeat = float("-inf")
            self.heartbeat()
            return {
                "bridge_id": self.config.bridge_id,
                "broker_reachable": True,
                "devices": self.inventory,
            }

    def run(self) -> dict:
        failures = 0
        with self.lock, self._signals():
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self._cycle()
                    failures = 0
                    delay = max(0.0, self.poll_seconds - (time.monotonic() - started))
                    if self.has_pending_jobs:
                        delay = 0.0
                except Exception as exc:
                    self.counts["errors"] += 1
                    failures = min(failures + 1, 6)
                    base = min(60, 2**failures)
                    if isinstance(exc, BridgeError) and not exc.retryable:
                        base = 60
                    delay = random.uniform(base / 2, base)  # noqa: S311 - reconnect jitter
                    logging.getLogger(__name__).warning(
                        "Bridge cycle failed (%s), retrying in %.1fs",
                        getattr(exc, "code", "internal_error"),
                        delay,
                    )
                self.stop.wait(delay)
            try:
                self.flush_receipts()
            except BridgeError:
                self.counts["errors"] += 1
        return dict(self.counts)

    @contextmanager
    def _signals(self):
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            for signum in previous:
                signal.signal(signum, lambda number, frame: self.stop.set())
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
