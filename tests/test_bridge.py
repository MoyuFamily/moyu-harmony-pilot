"""Fully offline protocol-v1 broker conformance, recovery and cancellation tests."""

import copy
import importlib
import io
import json
import signal
import time
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from moyu_harmony_pilot.actions import ActionRegistry, Inspection
from moyu_harmony_pilot.bridge import BridgeConfig, BridgeError, BridgeWorker, HttpBridgeClient
from moyu_harmony_pilot.bridge.client import _RejectRedirect
from moyu_harmony_pilot.bridge.lease import LeaseKeeper
from moyu_harmony_pilot.bridge.protocol import (
    BridgeProtocolError,
    Job,
    broker_url,
    decode_object,
    timestamp,
    utc_now,
)
from moyu_harmony_pilot.bridge.worker import health_status
from moyu_harmony_pilot.device import DeviceBusyError, DeviceConfigurationError
from moyu_harmony_pilot.hdc import HdcClient
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_actions import SampleMutation
from tests.test_cli import _parse_single_json_envelope, _run_cli


def job_payload(**updates):
    now = utc_now()
    payload = {
        "jobId": "job_001",
        "deviceAlias": "phone",
        "action": "example.mutate",
        "idempotencyKey": "synthetic-job-key",
        "params": {},
        "timeoutSeconds": 10,
        "leaseToken": "synthetic-lease-token",
        "leaseExpiresAt": timestamp(now + timedelta(seconds=30)),
        "createdAt": timestamp(now - timedelta(seconds=1)),
        "expiresAt": timestamp(now + timedelta(hours=1)),
    }
    payload.update(updates)
    return payload


class FakeBroker:
    def __init__(self):
        self.jobs = []
        self.calls = []
        self.receipts = []
        self.failures = {}
        self.cancel = False
        self.on_claim = lambda: None

    def post(self, path, payload, *, timeout=None):
        self.calls.append((path, copy.deepcopy(payload)))
        failures = self.failures.get(path, [])
        if failures:
            raise failures.pop(0)
        if path.endswith("/heartbeat"):
            return {
                "ok": True,
                "serverTime": timestamp(),
                "recommendedPollIntervalSeconds": 0.1,
                "hasPendingJobs": bool(self.jobs),
            }
        if path.endswith("/claim"):
            self.on_claim()
            return {"jobs": [self.jobs.pop(0)] if self.jobs else []}
        if path.endswith("/lease"):
            return {
                "ok": True,
                "leaseExpiresAt": timestamp(utc_now() + timedelta(seconds=30)),
                "cancelRequested": self.cancel,
            }
        if path.endswith("/complete"):
            self.receipts.append(copy.deepcopy(payload))
            return {"ok": True, "jobId": path.split("/")[-2], "status": payload["status"]}
        raise AssertionError(path)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    device = FakeDeviceState("SYNTHETIC_BRIDGE_SERIAL")
    hdc = FakeHdcTransport([device])
    devices = tmp_path / "devices.local.json"
    devices.write_text(json.dumps({"devices": {"phone": device.serial}}))
    devices.chmod(0o600)
    config_path = tmp_path / "bridge.local.json"
    values = {
        "bridge_id": "test-worker",
        "broker_url": "https://broker.example.org",
        "devices_config": "devices.local.json",
        "state_dir": "state",
        "wait_seconds": 0,
        "allowed_actions": {"phone": ["device.health", "example.mutate"]},
        "lease_seconds": 0.1,
    }
    config_path.write_text(json.dumps(values))
    config_path.chmod(0o600)
    config = BridgeConfig.load(config_path, env={"HARMONY_PILOT_BRIDGE_TOKEN": "synthetic-bearer"})
    registry = ActionRegistry.discover(env={}, allowed_actions=["device.health", "example.mutate"])
    plugin = SampleMutation()
    registry.register(plugin)
    broker = FakeBroker()
    worker = BridgeWorker(config, HdcClient(transport=hdc), client=broker, registry=registry)
    return worker, broker, plugin, device, hdc, config_path


def test_worker_protocol_and_duplicate_job_never_reexecutes(bridge):
    worker, broker, plugin, device, _, _ = bridge
    payload = job_payload()
    assert not health_status(worker.config)["healthy"]
    broker.jobs = [payload, copy.deepcopy(payload)]
    worker.run_once()
    worker.run_once()
    assert plugin.calls == ["inspect", "submit", "confirm"]
    assert len(broker.receipts) == 2
    assert broker.receipts[0] == broker.receipts[1]
    assert broker.receipts[0]["status"] == "succeeded"
    assert device.serial not in json.dumps(broker.calls)
    assert worker.config.token not in json.dumps(broker.calls)
    heartbeat = broker.calls[0][1]
    assert heartbeat["devices"][0]["deviceAlias"] == "phone"
    assert heartbeat["devices"][0]["capabilities"] == ["device.health", "example.mutate"]
    assert worker.store.path.stat().st_mode & 0o777 == 0o600
    assert health_status(worker.config)["healthy"]
    health_file = worker.directory / "health.json"
    health = json.loads(health_file.read_text())
    health["heartbeat_at"] = timestamp(utc_now() - timedelta(hours=1))
    health_file.write_text(json.dumps(health))
    assert not health_status(worker.config)["healthy"]


@pytest.mark.parametrize(
    "updates",
    [{"action": "ui.inspect"}, {"deviceAlias": "other"}, {"params": {"shell": "private-command"}}],
)
def test_remote_scope_and_schema_fail_closed(bridge, updates):
    worker, broker, plugin, _, _, _ = bridge
    broker.jobs = [job_payload(**updates)]
    worker.run_once()
    assert plugin.calls == []
    assert broker.receipts[0]["status"] == "failed"
    assert broker.receipts[0]["errorCode"] == "configuration_error"
    assert "private-command" not in json.dumps(broker.receipts)


def test_completion_retry_replays_identical_receipt(bridge):
    worker, broker, plugin, _, _, _ = bridge
    broker.jobs = [job_payload()]
    endpoint = "/bridge/v1/jobs/job_001/complete"
    broker.failures[endpoint] = [BridgeError("synthetic disconnect", status=503)]
    with pytest.raises(BridgeError):
        worker.run_once()
    saved = worker.store.pending()[0][1]
    worker.run_once()
    assert broker.receipts == [saved]
    assert plugin.calls.count("submit") == 1
    assert not worker.store.pending()


def test_restart_recovers_unfinished_job_without_submission(bridge):
    worker, broker, plugin, _, hdc, _ = bridge
    worker.store.reserve(Job.parse(job_payload()))
    restarted = BridgeWorker(
        worker.config, HdcClient(transport=hdc), client=broker, registry=worker.registry
    )
    restarted.run_once()
    assert broker.receipts[0]["status"] == "unknown"
    assert broker.receipts[0]["errorCode"] == "worker_restarted"
    assert plugin.calls == []
    assert restarted.counts["recovered"] == 1


@pytest.mark.parametrize("stage", ["inspect", "submit"])
def test_broker_cancellation_interrupts_active_stage(bridge, monkeypatch, stage):
    worker, broker, plugin, _, _, _ = bridge
    broker.jobs = [job_payload()]
    broker.cancel = True
    if stage == "inspect":

        def inspect(ctx, params):
            plugin.calls.append("inspect")
            time.sleep(2)
            return Inspection()

        monkeypatch.setattr(plugin, "inspect", inspect)
    else:
        plugin.submit_hook = lambda: time.sleep(2)
    started = time.monotonic()
    worker.run_once()
    assert time.monotonic() - started < 1.5
    assert broker.receipts[0]["status"] == ("failed" if stage == "inspect" else "unknown")
    assert ("submit" in plugin.calls) == (stage == "submit")
    assert any(path.endswith("/lease") for path, _ in broker.calls)


def test_expired_lease_and_lost_device(bridge):
    worker, broker, plugin, device, _, _ = bridge
    broker.jobs = [job_payload(leaseExpiresAt=timestamp(utc_now() - timedelta(seconds=1)))]
    worker.run_once()
    assert broker.receipts[-1]["errorCode"] == "job_expired"
    assert plugin.calls == []
    broker.jobs = [job_payload(jobId="job_offline")]
    broker.on_claim = lambda: setattr(device, "status", "Offline")
    worker.run_once()
    assert broker.receipts[-1]["status"] == "device_offline"
    assert plugin.calls == []


def test_action_timeout_and_instance_lock(bridge):
    worker, broker, plugin, _, hdc, _ = bridge
    competitor = BridgeWorker(
        worker.config, HdcClient(transport=hdc), client=broker, registry=worker.registry
    )
    with worker.lock:
        with pytest.raises(DeviceBusyError):
            competitor.run_once()
    plugin.submit_hook = lambda: time.sleep(2)
    broker.jobs = [job_payload(timeoutSeconds=0.2)]
    worker.run_once()
    assert broker.receipts[-1]["status"] == "unknown"


def test_lease_conflict_and_terminal_completion_conflict(bridge):
    worker, broker, _, _, _, _ = bridge
    job = Job.parse(job_payload())
    lease = LeaseKeeper(job, worker.config, broker, worker.stop, lambda: None)
    broker.failures["/bridge/v1/jobs/job_001/lease"] = [
        BridgeError("expired", status=409, retryable=False)
    ]
    with lease:
        assert lease.lost.wait(1)
    assert lease.cancelled()
    broker.jobs = [job_payload()]
    broker.failures["/bridge/v1/jobs/job_001/complete"] = [
        BridgeError("closed", status=410, retryable=False)
    ]
    worker.run_once()
    assert not worker.store.pending()


def test_finishing_during_renewal_skips_heartbeat_and_stops_thread(bridge, monkeypatch):
    worker, broker, _, _, _, _ = bridge
    heartbeat = Mock()
    lease = LeaseKeeper(Job.parse(job_payload()), worker.config, broker, worker.stop, heartbeat)
    post = broker.post

    def finish_during_request(path, payload, *, timeout=None):
        lease.finished.set()
        return post(path, payload, timeout=timeout)

    monkeypatch.setattr(broker, "post", finish_during_request)
    with lease:
        assert lease.finished.wait(1)
    assert not lease.thread.is_alive()
    heartbeat.assert_not_called()


def test_shutdown_during_renewal_wait_skips_request(bridge, monkeypatch):
    worker, broker, _, _, _, _ = bridge
    lease = LeaseKeeper(Job.parse(job_payload()), worker.config, broker, worker.stop, Mock())

    def stop_during_wait(timeout):
        worker.stop.set()
        return False

    monkeypatch.setattr(lease.finished, "wait", stop_during_wait)
    lease._loop()
    assert not broker.calls


def test_late_renewal_does_not_revive_expired_lease(bridge, monkeypatch):
    worker, broker, _, _, _, _ = bridge
    job = Job.parse(job_payload())
    lease = LeaseKeeper(job, worker.config, broker, worker.stop, Mock())
    lease_module = importlib.import_module("moyu_harmony_pilot.bridge.lease")

    def late_response(path, payload, *, timeout=None):
        monkeypatch.setattr(lease_module, "utc_now", lambda: job.lease_expires_at)
        return {
            "ok": True,
            "cancelRequested": False,
            "leaseExpiresAt": timestamp(job.lease_expires_at + timedelta(seconds=60)),
        }

    monkeypatch.setattr(broker, "post", late_response)
    lease.renew()
    assert lease.lost.is_set()
    assert lease.expires_at == job.lease_expires_at
    assert lease.cancelled()


def test_signal_shutdown_after_submit_records_unknown_and_restores_handlers(bridge):
    worker, broker, plugin, _, _, _ = bridge
    original = signal.getsignal(signal.SIGTERM)
    plugin.submit_hook = lambda: signal.raise_signal(signal.SIGTERM)
    broker.jobs = [job_payload()]
    worker.run()
    assert worker.stop.is_set()
    assert broker.receipts[-1]["status"] == "unknown"
    assert plugin.calls.count("submit") == 1
    assert signal.getsignal(signal.SIGTERM) == original
    assert len([path for path, _ in broker.calls if path.endswith("/claim")]) == 1


def test_reconnect_backoff_resumes_and_exits_cleanly(bridge, monkeypatch):
    worker, broker, _, _, _, _ = bridge
    broker.failures["/bridge/v1/heartbeat"] = [BridgeError("offline")]
    broker.on_claim = worker.stop.set
    delays = []
    monkeypatch.setattr(worker.stop, "wait", lambda delay: delays.append(delay))
    worker.run()
    assert worker.counts["errors"] == 1
    assert 1 <= delays[0] <= 2
    assert len([path for path, _ in broker.calls if path.endswith("/heartbeat")]) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://broker.example.org",
        "https://user:private@example.org",
        "https://example.org/?token=private",
        "file:///private/file",
        "http://localhost.example.org",
        "https://example.org/#fragment",
    ],
)
def test_invalid_broker_urls_are_rejected(url):
    with pytest.raises(BridgeProtocolError):
        broker_url(url)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"ok":true,"ok":false}',
        b'{"number":NaN}',
        b'{"number":1e999}',
        b'{"nested":[{"number":-1e999}]}',
        b"[]",
        b"x" * 65537,
    ],
)
def test_broker_json_is_bounded_unambiguous_and_finite(raw):
    with pytest.raises(BridgeProtocolError):
        decode_object(raw)


def test_private_config_token_file_and_offline_cli_check(bridge, monkeypatch):
    worker, broker, _, _, hdc, config_path = bridge
    values = json.loads(config_path.read_text())
    values["allowed_actions"] = {"phone": ["device.health"]}
    values["token_file"] = "bridge-token"
    config_path.write_text(json.dumps(values))
    secret = config_path.parent / "bridge-token"
    secret.write_text("synthetic-file-token")
    secret.chmod(0o600)
    config = BridgeConfig.load(config_path, env={})
    assert config.token == "synthetic-file-token"
    assert config.token not in repr(config)
    code, output, _ = _run_cli(
        ["serve", "--config", str(config_path), "--check", "--json"],
        env={"HARMONY_PILOT_HDC_PATH": "/not/installed"},
    )
    assert code == 0
    assert _parse_single_json_envelope(output)["data"]["valid"]
    worker_module = importlib.import_module("moyu_harmony_pilot.bridge.worker")
    monkeypatch.setattr(worker_module, "HttpBridgeClient", lambda config: broker)
    code, output, _ = _run_cli(
        ["serve", "--config", str(config_path), "--once", "--json"], env={}, transport=hdc
    )
    assert code == 0, output
    secret.chmod(0o644)
    with pytest.raises(DeviceConfigurationError):
        BridgeConfig.load(config_path, env={})


def test_http_transport_headers_limits_errors_and_redirect_rejection(bridge):
    config = bridge[0].config
    response = io.BytesIO(b'{"ok": true}')
    response.status = 200
    response.headers = {"Content-Type": "application/json; charset=utf-8"}
    opener = Mock()
    opener.open.return_value = response
    client = HttpBridgeClient(config, opener=opener)
    assert client.post("/bridge/v1/heartbeat", {}) == {"ok": True}
    request = opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer " + config.token
    assert request.get_header("X-harmony-pilot-protocol") == "1"
    assert request.full_url.startswith("https://")
    for status in (401, 403, 409, 410, 429, 503):
        opener.open.side_effect = HTTPError(
            "https://broker.example.org", status, "private", {}, None
        )
        with pytest.raises(BridgeError) as error:
            client.post("/bridge/v1/jobs/claim", {})
        assert error.value.status == status
        assert "private" not in str(error.value)
        assert error.value.retryable == (status in {429, 503})
    opener.open.side_effect = URLError("private-token")
    with pytest.raises(BridgeError, match="connection failed"):
        client.post("/bridge/v1/heartbeat", {})
    with pytest.raises(BridgeProtocolError):
        client.post("/bridge/v1/heartbeat", {"oversized": "a" * 17000})
    with pytest.raises(BridgeError):
        _RejectRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.org")
    assert broker_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"


def test_local_only_capability_and_malformed_jobs(bridge):
    worker, broker, _, _, hdc, _ = bridge
    config = replace(worker.config, allowed_actions={"phone": ("ui.inspect",)})
    with pytest.raises(DeviceConfigurationError):
        BridgeWorker(config, HdcClient(transport=hdc), client=broker, env={})
    for updates in (
        {"jobId": "../../escape"},
        {"timeoutSeconds": float("nan")},
        {"leaseToken": "line\nbreak"},
        {"expiresAt": "2026-01-01"},
    ):
        with pytest.raises(BridgeProtocolError):
            Job.parse(job_payload(**updates))


def test_receipts_redact_known_secrets(bridge):
    from moyu_harmony_pilot.actions import ActionOutcome

    worker = bridge[0]
    job = Job.parse(job_payload())
    result = worker._receipt(
        job,
        ActionOutcome(
            summary="private page text", metrics={"private": worker.config.token, "ok": 1}
        ),
        timestamp(),
        0,
    )
    assert result["metrics"] == {"ok": 1}
    assert "private page text" not in json.dumps(result)
