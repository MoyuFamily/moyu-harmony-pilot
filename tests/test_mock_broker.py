"""Run the real Bridge worker and HTTP encoder against the public in-memory broker."""

import copy
import json
from dataclasses import replace

import pytest

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.bridge import BridgeConfig, BridgeWorker, HttpBridgeClient
from moyu_harmony_pilot.bridge.protocol import BridgeError, BridgeProtocolError
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.testing.broker import MockBroker
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_actions import SampleMutation
from tests.test_bridge import job_payload


@pytest.fixture
def broker_worker(tmp_path):
    profile = FakeDeviceState("SYNTHETIC_CONFORMANCE")
    devices = tmp_path / "devices.local.json"
    devices.write_text(json.dumps({"devices": {"phone": profile.serial}}))
    devices.chmod(0o600)
    config = BridgeConfig(
        "test-worker", "http://127.0.0.1", devices,
        {"phone": ("example.mutate",)}, tmp_path / "state", "synthetic-bearer", wait_seconds=0,
    )
    broker = MockBroker(config.bridge_id, token=config.token)
    client = HttpBridgeClient(config, opener=broker)
    registry = ActionRegistry(allowed_actions=["example.mutate"])
    plugin = SampleMutation()
    registry.register(plugin)
    worker = BridgeWorker(
        config, HdcClient(transport=FakeHdcTransport([profile])), client=client, registry=registry
    )
    return broker, client, worker, plugin


def test_full_worker_cycle_uses_http_protocol_contract_and_immutable_receipts(broker_worker):
    broker, client, worker, plugin = broker_worker
    broker.enqueue(job_payload())
    worker.run_once()
    assert plugin.calls == ["inspect", "submit", "confirm"]
    receipt = broker.receipts["job_001"]
    assert receipt["status"] == "succeeded"
    assert not worker.store.pending()
    assert any(path.endswith("heartbeat") for path, _ in broker.calls)
    assert any(path.endswith("claim") for path, _ in broker.calls)
    path = "/bridge/v1/jobs/job_001/complete"
    assert client.post(path, receipt)["ok"]
    with pytest.raises(BridgeError) as error:
        client.post(path, {**receipt, "status": "failed"})
    assert error.value.status == 409
    assert len(plugin.calls) == 3
    assert receipt["metrics"] == {"count": 1}
    with pytest.raises(BridgeError) as error:
        client.post(path, {**receipt, "metrics": {"count": True}})
    assert error.value.status == 409
    assert type(broker.receipts["job_001"]["metrics"]["count"]) is int


def test_mock_rejects_wrong_bearer_and_oversized_json(broker_worker):
    broker, client, worker, _ = broker_worker
    invalid = HttpBridgeClient(replace(worker.config, token="different-synthetic"), opener=broker)
    with pytest.raises(BridgeError) as error:
        invalid.post("/bridge/v1/heartbeat", {})
    assert error.value.status == 401
    with pytest.raises(BridgeProtocolError):
        client.post("/bridge/v1/heartbeat", {"oversized": "x" * 17000})


def test_claim_filters_capabilities_serializes_device_and_lease_cancel(broker_worker):
    broker, client, _, _ = broker_worker
    first = job_payload()
    broker.enqueue(first)
    broker.enqueue(job_payload(jobId="job_002"))
    claim = {"bridgeId": "test-worker", "maxJobs": 1, "waitSeconds": 0,
             "availableDevices": [{"deviceAlias": "phone", "capabilities": ["device.health"]}]}
    assert client.post("/bridge/v1/jobs/claim", claim) == {"jobs": []}
    claim["availableDevices"][0]["capabilities"] = ["example.mutate"]
    assert client.post("/bridge/v1/jobs/claim", claim)["jobs"] == [first]
    assert client.post("/bridge/v1/jobs/claim", claim)["jobs"] == []
    broker.cancel_requested.add("job_001")
    lease = {"bridgeId": "test-worker", "leaseToken": first["leaseToken"],
             "extendSeconds": 30, "stage": "inspect"}
    assert client.post("/bridge/v1/jobs/job_001/lease", lease)["cancelRequested"]
    with pytest.raises(BridgeProtocolError):
        client.post("/bridge/v1/jobs/job_001/lease", {**lease, "stage": "shell"})
    with pytest.raises(BridgeProtocolError):
        broker.enqueue(copy.deepcopy(first))


@pytest.mark.parametrize("status", [409], ids=["conflicting-receipt"])
def test_worker_retains_conflicted_receipt_provenance(broker_worker, monkeypatch, status):
    broker, _, worker, _ = broker_worker
    broker.enqueue(job_payload())

    def reject(job, payload):
        raise BridgeError("Synthetic closed response.", status=status, retryable=False)

    monkeypatch.setattr(broker, "_complete", reject)
    worker.run_once()
    import sqlite3

    with sqlite3.connect(worker.store.path) as db:
        row = db.execute("SELECT state, acknowledged_at FROM jobs").fetchone()
    assert row == ("closed", None)
    assert worker.store.prune(older_than_days=1) == 0
