"""Offline receipt retention, old databases and durable deduplication boundaries."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from moyu_harmony_pilot.bridge.protocol import BridgeProtocolError, Job
from moyu_harmony_pilot.bridge.store import ReceiptStore
from tests.test_bridge import job_payload
from tests.test_cli import _parse_single_json_envelope, _run_cli


def test_receipt_prune_preserves_pending_unknown_and_job_deduplication(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = ReceiptStore(path, "test-worker")
    jobs = {}
    for name, status in [("success", "succeeded"), ("done", "already_done"),
                         ("unknown", "unknown"), ("failed", "failed"),
                         ("pending", "succeeded"), ("running", None)]:
        job = Job.parse(job_payload(jobId=name, deviceAlias=name))
        jobs[name] = job
        store.reserve(job)
        if status:
            store.finish(name, {"status": status, "metrics": {"count": 1}})
            if name != "pending":
                store.acknowledge(name)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET acknowledged_at='2020-01-01T00:00:00.000Z'")
    assert store.prune(older_than_days=30) == 2
    assert store.prune(older_than_days=30) == 0
    assert store.blocked_aliases() == {"pending", "running"}
    assert store.pending() == [("pending", {"status": "succeeded", "metrics": {"count": 1}})]
    for name in ["success", "done"]:
        with pytest.raises(BridgeProtocolError, match="archived"):
            store.reserve(jobs[name])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
        assert db.execute("SELECT receipt FROM jobs WHERE job_id='success'").fetchone()[0] is None
        assert db.execute("SELECT receipt FROM jobs WHERE job_id='unknown'").fetchone()[0]
    changed = Job.parse(job_payload(jobId="success", deviceAlias="different"))
    with pytest.raises(BridgeProtocolError, match="different request"):
        store.reserve(changed)
    assert store.recover() == 1
    assert store.blocked_aliases() == {"pending", "running"}


def test_old_closed_receipts_wait_for_positive_acknowledgement(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                   "device_alias TEXT NOT NULL, lease_token TEXT NOT NULL, started_at TEXT "
                   "NOT NULL, state TEXT NOT NULL, receipt TEXT)")
        db.execute("INSERT INTO jobs VALUES('old','fingerprint','phone','lease', "
                   "'2020-01-01','closed',?)", (json.dumps({"status": "succeeded"}),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: ReceiptStore(path, "test-worker"), range(2)))
    assert stores[0].prune(older_than_days=1) == 0
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT acknowledged_at FROM jobs").fetchone()[0] is None


def test_conflicted_receipt_is_preserved_until_positive_acknowledgement(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = ReceiptStore(path, "test-worker")
    job = Job.parse(job_payload())
    store.reserve(job)
    store.finish(job.job_id, {"status": "succeeded"})
    store.acknowledge(job.job_id, accepted=False)
    assert store.prune(older_than_days=1) == 0
    assert store.blocked_aliases() == set()
    assert store.reserve(job) is False
    store.acknowledge(job.job_id)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET acknowledged_at='2020-01-01T00:00:00.000Z'")
    assert store.prune(older_than_days=1) == 1


def test_receipt_prune_cli_is_offline_and_json(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    ReceiptStore(path, "test-worker")
    code, out, err = _run_cli(
        ["receipts", "prune", "--database", str(path), "--bridge-id", "test-worker",
         "--older-than-days", "30", "--json"], cwd=tmp_path, env={},
    )
    assert code == 0 and err == ""
    assert _parse_single_json_envelope(out)["data"] == {
        "archived": 0, "job_tombstones_retained": True,
    }
    assert path.stat().st_mode & 0o777 == 0o600
