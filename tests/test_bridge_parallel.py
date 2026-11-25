"""Real spawned-process concurrency against offline fake devices and broker."""

import json
import os
import time
from dataclasses import replace

import pytest

from moyu_harmony_pilot.actions import ActionOutcome, ActionPlugin, ActionRegistry, ActionSpec
from moyu_harmony_pilot.bridge import BridgeConfig, BridgeWorker
from moyu_harmony_pilot.bridge.protocol import BridgeProtocolError
from moyu_harmony_pilot.device import DeviceConfigurationError
from moyu_harmony_pilot.hdc import HdcClient
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_bridge import FakeBroker, job_payload


class ParallelProbe(ActionPlugin):
    spec = ActionSpec(
        "example.parallel",
        "1.0",
        "Synthetic process concurrency probe.",
        diagnostic=True,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"crash": {"type": "boolean"}, "sleep": {"type": "boolean"}},
        },
        metrics_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pid": {"type": "integer"},
                "started": {"type": "number"},
                "finished": {"type": "number"},
            },
        },
        timeout_seconds=5,
    )

    def execute(self, ctx, params):
        started = time.monotonic()
        directory = ctx.device.lock_dir.parent
        (directory / f"{ctx.device.alias}.ready").write_text("synthetic")
        if params.get("crash"):
            os._exit(9)
        if params.get("sleep"):
            time.sleep(30)
        while not all((directory / f"{alias}.ready").exists() for alias in ("one", "two")):
            _ = ctx.remaining_timeout
            time.sleep(0.01)
        time.sleep(0.1)
        return ActionOutcome(
            metrics={"pid": os.getpid(), "started": started, "finished": time.monotonic()}
        )


class BatchBroker(FakeBroker):
    def post(self, path, payload, *, timeout=None):
        if path.endswith("/claim"):
            self.calls.append((path, payload))
            jobs = self.jobs[: payload["maxJobs"]]
            del self.jobs[: len(jobs)]
            return {"jobs": jobs}
        return super().post(path, payload, timeout=timeout)


@pytest.fixture
def parallel_worker(tmp_path):
    states = [FakeDeviceState("SYNTHETIC_ONE"), FakeDeviceState("SYNTHETIC_TWO")]
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"devices": {"one": states[0].serial, "two": states[1].serial}}))
    devices.chmod(0o600)
    config = BridgeConfig(
        "parallel-test",
        "https://broker.example.org",
        devices,
        {"one": ("example.parallel",), "two": ("example.parallel",)},
        tmp_path,
        "synthetic-token",
        max_jobs=2,
        wait_seconds=0,
        lease_seconds=0.1,
    )
    registry = ActionRegistry(allowed_actions=["example.parallel"])
    registry.register(ParallelProbe())
    broker = BatchBroker()
    worker = BridgeWorker(
        config,
        HdcClient(transport=FakeHdcTransport(states)),
        client=broker,
        registry=registry,
        env={},
    )
    broker.jobs = [
        job_payload(jobId="job_one", deviceAlias="one", action="example.parallel"),
        job_payload(jobId="job_two", deviceAlias="two", action="example.parallel"),
    ]
    return worker, broker


def test_parallel_slots_use_distinct_main_threads_and_overlap(parallel_worker):
    worker, broker = parallel_worker
    counts = worker.run_once()
    assert counts["completed"] == 2
    assert [receipt["status"] for receipt in broker.receipts] == ["succeeded", "succeeded"]
    metrics = [receipt["metrics"] for receipt in broker.receipts]
    assert len({metric["pid"] for metric in metrics}) == 2
    assert max(metric["started"] for metric in metrics) < min(
        metric["finished"] for metric in metrics
    )
    assert not worker.active_aliases
    assert not worker.store.pending()
    assert worker.store.recover() == 0


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("crash", id="process-death-isolated"),
        pytest.param("sleep", id="timeout-isolated"),
    ],
)
def test_child_failure_does_not_interrupt_other_device(parallel_worker, mode):
    worker, broker = parallel_worker
    broker.jobs[0]["params"] = {mode: True}
    if mode == "sleep":
        broker.jobs[0]["timeoutSeconds"] = 0.5
    worker.run_once()
    statuses = {receipt["status"] for receipt in broker.receipts}
    assert "succeeded" in statuses
    assert ("unknown" if mode == "crash" else "failed") in statuses
    assert not worker.active_aliases


def test_duplicate_device_assignment_fails_before_launch(parallel_worker):
    worker, broker = parallel_worker
    broker.jobs[1]["deviceAlias"] = "one"
    with pytest.raises(BridgeProtocolError):
        worker.run_once()
    assert not list(worker.config.state_dir.glob("*.ready"))
    assert not worker.store.blocked_aliases()
    with pytest.raises(DeviceConfigurationError):
        replace(worker.config, max_jobs=0)
