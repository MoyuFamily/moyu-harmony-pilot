"""Durable SQLite reservations preventing automatic replay after uncertain submission."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions.models import ActionOutcome
from moyu_harmony_pilot.actions.validation import json_copy
from moyu_harmony_pilot.device import DeviceConfigurationError, ensure_secure_dir


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerKey:
    device_hash: str
    action_id: str
    key_hash: str
    request_hash: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.device_hash, self.action_id, self.key_hash


class ActionLedger:
    """Connections are short lived. Reservations commit before returning to the caller."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        ensure_secure_dir(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise DeviceConfigurationError("Unsupported action ledger schema version.")
            db.execute("""CREATE TABLE IF NOT EXISTS actions (
                device_hash TEXT NOT NULL,
                action_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('submitting', 'unknown', 'succeeded', 'already_done')),
                receipt TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(device_hash, action_id, key_hash)
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(actions)")}
            if "inspection_metrics" not in columns:
                db.execute(
                    "ALTER TABLE actions ADD COLUMN inspection_metrics TEXT NOT NULL DEFAULT '{}'"
                )
            db.execute("PRAGMA user_version = 2")
            db.execute("""CREATE TABLE IF NOT EXISTS resolutions (
                device_hash TEXT NOT NULL, action_id TEXT NOT NULL, key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL,
                resolved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA synchronous = FULL")
        return db

    @staticmethod
    def make_key(
        serial: str, action_id: str, key: str | None, version: str, params: dict[str, Any]
    ) -> LedgerKey:
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise DeviceConfigurationError("Mutating actions require a 1–256 character key.")
        request = json.dumps(json_copy([version, params]), sort_keys=True, separators=(",", ":"))
        return LedgerKey(digest(serial), action_id, digest(key), digest(request))

    def lookup(self, key: LedgerKey) -> ActionOutcome | None:
        """Read/recover a reservation while holding the corresponding device lock."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_hash, status, receipt FROM actions "
                "WHERE device_hash=? AND action_id=? AND key_hash=?",
                key.identity,
            ).fetchone()
            if row is None:
                return None
            if row[0] != key.request_hash:
                raise DeviceConfigurationError(
                    "Idempotency key is already bound to different parameters or plugin version."
                )
            if row[1] in {"succeeded", "already_done"}:
                payload = json.loads(row[2])
                payload.update(status="already_done", summary="Previously confirmed action.")
                outcome = ActionOutcome(**payload)
                outcome.to_dict()
                return outcome
            outcome = ActionOutcome(
                status="unknown",
                summary="Previous submission needs read-only reconciliation.",
                error_code="submission_uncertain",
            )
            db.execute(
                "UPDATE actions SET status='unknown', updated_at=CURRENT_TIMESTAMP "
                "WHERE device_hash=? AND action_id=? AND key_hash=?",
                key.identity,
            )
            return outcome

    def reserve(self, key: LedgerKey, *, inspection_metrics: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO actions(device_hash, action_id, key_hash, request_hash, status, "
                    "inspection_metrics) VALUES(?, ?, ?, ?, 'submitting', ?)",
                    (*key.identity, key.request_hash,
                     json.dumps(inspection_metrics or {}, allow_nan=False)),
                )
            except sqlite3.IntegrityError as exc:
                raise DeviceConfigurationError("Action already has a durable reservation.") from exc

    def finish(self, key: LedgerKey, outcome: ActionOutcome) -> None:
        payload = outcome.to_dict()
        if payload["status"] not in {"succeeded", "already_done", "unknown"}:
            raise DeviceConfigurationError(
                "A submitted action requires a conclusive or unknown result."
            )
        with closing(self._connect()) as db, db:
            updated = db.execute(
                "UPDATE actions SET status=?, receipt=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE device_hash=? AND action_id=? AND key_hash=? "
                "AND request_hash=? AND status='submitting'",
                (
                    payload["status"],
                    json.dumps(payload, allow_nan=False),
                    *key.identity,
                    key.request_hash,
                ),
            )
            if updated.rowcount != 1:
                raise DeviceConfigurationError("Action reservation changed during execution.")

    def records(
        self, *, device_hash: str | None = None, action_id: str | None = None,
        status: str | None = None, key_hash: str | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise DeviceConfigurationError("Invalid ledger page bounds.")
        filters = {"device_hash": device_hash, "action_id": action_id,
                   "status": status, "key_hash": key_hash}
        clauses = [name + "=?" for name, value in filters.items() if value is not None]
        values = [value for value in filters.values() if value is not None]
        query = "SELECT * FROM actions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, device_hash, action_id, key_hash LIMIT ? OFFSET ?"
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(query, (*values, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["receipt"] = json.loads(item["receipt"]) if item["receipt"] else None
            item["inspection_metrics"] = json.loads(item["inspection_metrics"])
            result.append(item)
        return result

    def resolve(self, key: LedgerKey, outcome: ActionOutcome, *, note: str) -> None:
        """Conclude an uncertain reservation under the physical device lock."""
        payload = outcome.to_dict()
        if payload["status"] not in {"succeeded", "already_done"}:
            raise DeviceConfigurationError("Resolution must confirm successful completion.")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 256:
            raise DeviceConfigurationError("Resolution requires a 1–256 character audit note.")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE actions SET status=?, receipt=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE device_hash=? AND action_id=? AND key_hash=? AND request_hash=? "
                "AND status IN ('submitting', 'unknown')",
                (payload["status"], json.dumps(payload, allow_nan=False),
                 *key.identity, key.request_hash),
            )
            if updated.rowcount != 1:
                raise DeviceConfigurationError("No matching uncertain reservation to resolve.")
            db.execute(
                "INSERT INTO resolutions"
                "(device_hash, action_id, key_hash, request_hash, status, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (*key.identity, key.request_hash, payload["status"], note.strip()),
            )

    def prune(self, *, older_than_days: int) -> int:
        if type(older_than_days) is not int or older_than_days < 1:
            raise DeviceConfigurationError("Retention must be at least one day.")
        with closing(self._connect()) as db, db:
            deleted = db.execute(
                "DELETE FROM actions WHERE status IN ('succeeded', 'already_done') "
                "AND updated_at < datetime('now', ?)", (f"-{older_than_days} days",),
            )
            return deleted.rowcount
