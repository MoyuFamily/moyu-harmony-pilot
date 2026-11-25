"""Fully offline protocol-v1 broker conformance, recovery and cancellation tests."""

import copy
import importlib
import io
import json
import shutil
import signal
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.bridge import BridgeConfig, BridgeError, BridgeWorker, HttpBridgeClient
from moyu_harmony_pilot.bridge.client import _RejectRedirect
from moyu_harmony_pilot.bridge.config import validate_configuration
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
    [
        pytest.param({"action": "ui.inspect"}, id="action-outside-allowlist"),
        pytest.param({"deviceAlias": "other"}, id="device-outside-scope"),
        pytest.param({"params": {"shell": "private-command"}}, id="undeclared-parameter"),
    ],
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


@pytest.mark.parametrize(
    "stage,cancellation",
    [
        pytest.param("inspect", "requested", id="cancel-before-submission"),
        pytest.param("submit", "conflict", id="lease-lost-after-submission"),
        pytest.param("confirm", "expired", id="lease-expires-during-confirmation"),
    ],
)
def test_broker_cancellation_respects_submission_boundary(
    bridge, monkeypatch, stage, cancellation
):
    worker, broker, plugin, _, _, _ = bridge
    broker.jobs = [job_payload()]
    if cancellation == "requested":
        broker.cancel = True
    elif cancellation == "expired":
        broker.jobs[0]["leaseExpiresAt"] = timestamp(utc_now() + timedelta(seconds=0.25))
        broker.failures["/bridge/v1/jobs/job_001/lease"] = [BridgeError("offline")] * 30
    else:
        broker.failures["/bridge/v1/jobs/job_001/lease"] = [
            BridgeError("lease lost", status=409, retryable=False)
        ]
    operation = getattr(plugin, stage)

    def delayed_operation(ctx, *args):
        result = operation(ctx, *args)
        time.sleep(0.4)
        ctx.checkpoint()
        return result

    monkeypatch.setattr(plugin, stage, delayed_operation)
    started = time.monotonic()
    worker.run_once()
    assert time.monotonic() - started < 1.5
    assert broker.receipts[0]["status"] == ("failed" if stage == "inspect" else "succeeded")
    assert ("submit" in plugin.calls) == (stage != "inspect")
    assert ("confirm" in plugin.calls) == (stage != "inspect")
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


@pytest.mark.parametrize(
    "stage,signum",
    [
        pytest.param("inspect", signal.SIGTERM, id="terminate-before-submission"),
        pytest.param("submit", signal.SIGINT, id="interrupt-after-submission"),
    ],
)
def test_signal_shutdown_respects_submission_boundary(bridge, monkeypatch, stage, signum):
    worker, broker, plugin, _, _, _ = bridge
    original = signal.getsignal(signum)
    operation = getattr(plugin, stage)

    def interrupted(ctx, *args):
        result = operation(ctx, *args)
        signal.raise_signal(signum)
        time.sleep(0.15)
        ctx.checkpoint()
        return result

    monkeypatch.setattr(plugin, stage, interrupted)
    broker.jobs = [job_payload()]
    worker.run()
    assert worker.stop.is_set()
    assert broker.receipts[-1]["status"] == ("failed" if stage == "inspect" else "succeeded")
    assert plugin.calls.count("submit") == (0 if stage == "inspect" else 1)
    assert signal.getsignal(signum) == original
    assert len([path for path, _ in broker.calls if path.endswith("/claim")]) == 1


def test_job_expiration_still_bounds_cancelled_submission(bridge):
    worker, broker, plugin, _, _, _ = bridge
    broker.jobs = [job_payload(expiresAt=timestamp(utc_now() + timedelta(seconds=0.25)))]

    def submit():
        signal.raise_signal(signal.SIGTERM)
        time.sleep(2)

    plugin.submit_hook = submit
    started = time.monotonic()
    worker.run()
    assert time.monotonic() - started < 1.5
    assert broker.receipts[0]["status"] == "unknown"
    assert plugin.calls == ["inspect", "submit"]


def test_invalid_broker_urls_are_rejected():
    with pytest.raises(BridgeProtocolError):
        broker_url("http://broker.example.org")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"ok":true,"ok":false}', id="duplicate-key"),
        pytest.param(b'{"number":1e999}', id="nonfinite-number"),
        pytest.param(b"x" * 65537, id="oversized-body"),
    ],
)
def test_broker_json_is_bounded_unambiguous_and_finite(raw):
    with pytest.raises(BridgeProtocolError):
        decode_object(raw)


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
    assert request.get_header("User-agent") == f"harmony-pilot/{__version__}"
    assert request.full_url.startswith("https://")
    for status in (403, 503):
        opener.open.side_effect = HTTPError(
            "https://broker.example.org", status, "private", {}, None
        )
        with pytest.raises(BridgeError) as error:
            client.post("/bridge/v1/jobs/claim", {})
        assert error.value.status == status
        assert "private" not in str(error.value)
        assert error.value.retryable == (status == 503)
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
    with pytest.raises(BridgeProtocolError):
        Job.parse(job_payload(jobId="../../escape"))


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


def test_configured_plugin_paths_and_contract_export_are_offline(bridge, monkeypatch):
    worker, _, _, device, _, config_path = bridge
    directory = config_path.parent / "plugins:external"
    directory.mkdir()
    (directory / "actions.json").write_text(json.dumps({
        "example.remote": "remote.py:RemoteAction",
        "example.blocked": "blocked.py:BlockedAction",
    }))
    (directory / "remote.py").write_text(
        "from moyu_harmony_pilot.actions import ActionPlugin, ActionSpec, ActionOutcome\n"
        "print('synthetic plugin initialization')\n"
        "class RemoteAction(ActionPlugin):\n"
        "    spec = ActionSpec('example.remote', '1', 'Synthetic remote action')\n"
        "    def execute(self, ctx, params):\n"
        "        return ActionOutcome()\n"
    )
    (directory / "blocked.py").write_text("raise AssertionError('unlisted module imported')\n")
    values = json.loads(config_path.read_text())
    values["plugin_paths"] = [directory.name]
    values["allowed_actions"] = {"phone": ["example.remote"]}
    config_path.write_text(json.dumps(values))
    environment = {
        "HARMONY_PILOT_BRIDGE_TOKEN": worker.config.token,
        "HARMONY_PILOT_PLUGIN_PATHS": str(directory / "missing"),
        "HARMONY_PILOT_HDC_PATH": "/not/installed",
    }
    monkeypatch.chdir(config_path.parent.parent)
    config = BridgeConfig.load(config_path, env=environment)
    assert config.plugin_paths == (directory.resolve(),)
    code, output, stderr = _run_cli(
        ["serve", "--config", str(config_path), "--check", "--json"], env=environment,
    )
    assert code == 0, output
    assert "synthetic plugin initialization" in stderr
    data = _parse_single_json_envelope(output)["data"]
    assert data["protocol_version"] == 1
    assert data["allowed_actions"] == {"phone": ["example.remote"]}
    assert [action["action_id"] for action in data["actions"]] == ["example.remote"]
    assert data["actions"][0]["remote_allowed"]
    assert data["actions"][0]["api_version"] == 1
    for private in (worker.config.token, device.serial, str(directory)):
        assert private not in output

    # Older configurations retain environment-based plugin discovery.
    values.pop("plugin_paths")
    config_path.write_text(json.dumps(values))
    environment["HARMONY_PILOT_PLUGIN_PATHS"] = str(directory)
    # The environment syntax uses a path separator, so use a separator-free path here.
    plain_directory = directory.with_name("external")
    directory.rename(plain_directory)
    environment["HARMONY_PILOT_PLUGIN_PATHS"] = str(plain_directory)
    config = BridgeConfig.load(config_path, env=environment)
    assert config.plugin_paths is None
    validate_configuration(config, env=environment)
    values["plugin_paths"] = []
    config_path.write_text(json.dumps(values))
    with pytest.raises(DeviceConfigurationError, match="Unknown action"):
        validate_configuration(BridgeConfig.load(config_path, env=environment), env=environment)


def test_probe_preserves_jobs_receipts_and_plugin_callbacks(bridge):
    worker, broker, plugin, device, transport, _ = bridge
    running = Job.parse(job_payload(jobId="job_running"))
    pending = Job.parse(job_payload(jobId="job_pending"))
    worker.store.reserve(running)
    worker.store.reserve(pending)
    worker.store.finish(pending.job_id, {"status": "unknown"})
    receipts = worker.store.pending()
    broker.jobs = [job_payload()]
    for _ in range(2):
        result = worker.probe()
        assert result["broker_reachable"]
        assert result["devices"][0]["busy"]
        assert device.serial not in json.dumps(result)
    assert [path for path, _ in broker.calls] == ["/bridge/v1/heartbeat"] * 2
    assert worker.store.pending() == receipts
    assert worker.counts == {"claimed": 0, "completed": 0, "recovered": 0, "errors": 0}
    assert not plugin.calls
    assert len(broker.jobs) == 1
    assert not any("uiInput" in " ".join(args) for _, args, _ in transport.calls)
    # The unfinished reservation remains recoverable after the probe.
    assert worker.store.recover() == 1


def test_local_ledger_survives_remote_worker_migration(bridge):
    worker, broker, plugin, _, transport, _ = bridge
    payload = job_payload()
    plugin.confirm_error = TimeoutError()
    local_outcome = worker.runner.run(
        worker.devices["phone"], payload["action"], payload["params"],
        idempotency_key=payload["idempotencyKey"],
    )
    assert local_outcome.status == "unknown"
    plugin.calls.clear()
    config = replace(
        worker.config, bridge_id="migrated-worker", broker_url="https://other.example.org",
    )
    migrated = BridgeWorker(
        config, HdcClient(transport=transport), client=broker, registry=worker.registry,
    )
    broker.jobs = [payload]
    migrated.run_once()
    assert broker.receipts[0]["status"] == "unknown"
    assert not plugin.calls
    assert worker.store.path != migrated.store.path
    assert worker.runner.state_dir == migrated.runner.state_dir


def test_remote_example_through_cli_http_encoding_and_receipt_retry(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    source = Path(__file__).resolve().parents[1] / "examples" / "bridge"
    deployment = tmp_path / "deployment"
    shutil.copytree(source, deployment)
    state = FakeDeviceState("SYNTHETIC_EXTERNAL_PLUGIN_DEVICE")
    transport = FakeHdcTransport([state])
    devices_path = deployment / "devices.local.yaml"
    devices_path.write_text(f'devices:\n  harmony-01: "{state.serial}"\n')
    devices_path.chmod(0o600)
    token_path = deployment / "bridge-token.local"
    token_path.write_text("synthetic-integration-token")
    token_path.chmod(0o600)
    config_path = deployment / "bridge.local.json"
    config_path.write_text((deployment / "bridge.example.json").read_text())
    config_path.chmod(0o600)
    broker = FakeBroker()
    broker.jobs = [job_payload(
        deviceAlias="harmony-01", action="example.health_check",
        params={"minimum_battery": 20}, timeoutSeconds=30,
    )]
    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        assert timeout > 0
        assert request.get_header("Authorization") == "Bearer synthetic-integration-token"
        assert request.get_header("X-harmony-pilot-protocol") == "1"
        path = urlsplit(request.full_url).path
        payload = json.loads(request.data)
        response = io.BytesIO(json.dumps(broker.post(path, payload)).encode())
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        return response

    worker_module = importlib.import_module("moyu_harmony_pilot.bridge.worker")
    opener = Mock()
    opener.open.side_effect = open_request
    monkeypatch.setattr(
        worker_module, "HttpBridgeClient", lambda config: HttpBridgeClient(config, opener=opener),
    )
    monkeypatch.chdir(tmp_path)
    args = ["serve", "--config", str(config_path), "--once", "--json"]
    complete_path = "/bridge/v1/jobs/job_001/complete"
    broker.failures[complete_path] = [BridgeError("synthetic disconnect")]
    code, output, _ = _run_cli(args, transport=transport, env={})
    assert code == 1, output
    assert not _parse_single_json_envelope(output)["ok"]
    saved_receipt = next(payload for path, payload in broker.calls if path == complete_path)
    assert saved_receipt["status"] == "succeeded"
    assert saved_receipt["metrics"] == {"battery_percent": 72, "ready": True}
    code, output, _ = _run_cli(args, transport=transport, env={})
    assert code == 0, output
    assert _parse_single_json_envelope(output)["data"]["claimed"] == 0
    assert broker.receipts == [saved_receipt]
    assert len([path for path, _ in broker.calls if path == complete_path]) == 2
    assert all(state.serial.encode() not in request.data for request in requests)
    assert not any("uiInput" in " ".join(args) for _, args, _ in transport.calls)
