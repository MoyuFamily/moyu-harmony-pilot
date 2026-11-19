"""Durable job reservations and immutable completion receipts for at-least-once delivery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from moyu_harmony_pilot.bridge.protocol import BridgeProtocolError, Job, timestamp
from moyu_harmony_pilot.device import ensure_secure_dir


class ReceiptStore:
    def __init__(self, path: Path, bridge_id: str) -> None:
        self.path = path
        self.bridge_id = bridge_id
        ensure_secure_dir(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                device_alias TEXT NOT NULL, lease_token TEXT NOT NULL,
                started_at TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def reserve(self, job: Job) -> bool:
        request = [job.device_alias, job.action, job.idempotency_key, job.params, job.lease_token]
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT fingerprint FROM jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            if prior is not None:
                if prior[0] != fingerprint:
                    raise BridgeProtocolError("Broker reused a job ID with a different request.")
                db.execute(
                    "UPDATE jobs SET state='pending' WHERE job_id=? AND state='closed'",
                    (job.job_id,),
                )
                return False
            db.execute(
                "INSERT INTO jobs VALUES(?, ?, ?, ?, ?, 'running', NULL)",
                (job.job_id, fingerprint, job.device_alias, job.lease_token, timestamp()),
            )
            return True

    def finish(self, job_id: str, payload: dict) -> None:
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT state, receipt FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise BridgeProtocolError("Missing durable job reservation.")
            if row[0] != "running":
                if row[1] != encoded:
                    raise BridgeProtocolError("Completion receipts are immutable.")
                return
            db.execute(
                "UPDATE jobs SET state='pending', receipt=? WHERE job_id=?", (encoded, job_id)
            )

    def recover(self) -> int:
        """Call under the worker instance lock before accepting any new jobs."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT job_id, lease_token, started_at FROM jobs WHERE state='running'"
            ).fetchall()
            for job_id, token, started in rows:
                receipt = {
                    "bridgeId": self.bridge_id,
                    "leaseToken": token,
                    "status": "unknown",
                    "errorCode": "worker_restarted",
                    "summary": "Worker stopped during execution.",
                    "startedAt": started,
                    "finishedAt": timestamp(),
                    "durationMs": 0,
                    "metrics": {},
                    "hasLocalDiagnostics": False,
                }
                db.execute(
                    "UPDATE jobs SET state='pending', receipt=? WHERE job_id=?",
                    (json.dumps(receipt, sort_keys=True, separators=(",", ":")), job_id),
                )
            return len(rows)

    def pending(self) -> list[tuple[str, dict]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT job_id, receipt FROM jobs WHERE state='pending' "
                "ORDER BY started_at, job_id LIMIT 32"
            ).fetchall()
        return [(job_id, json.loads(payload)) for job_id, payload in rows]

    def blocked_aliases(self) -> set[str]:
        with closing(self._connect()) as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT device_alias FROM jobs WHERE state != 'closed'"
                )
            }

    def acknowledge(self, job_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE jobs SET state='closed' WHERE job_id=? AND state='pending'", (job_id,)
            )
