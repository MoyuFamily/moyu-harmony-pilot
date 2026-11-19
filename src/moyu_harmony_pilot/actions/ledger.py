"""Durable SQLite reservations preventing automatic replay after uncertain submission."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

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
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
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
            db.execute("PRAGMA user_version = 1")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA synchronous = FULL")
        return db

    @staticmethod
    def make_key(
        serial: str, action_id: str, key: str | None, version: str, params: dict
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

    def reserve(self, key: LedgerKey) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO actions(device_hash, action_id, key_hash, request_hash, status) "
                    "VALUES(?, ?, ?, ?, 'submitting')",
                    (*key.identity, key.request_hash),
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
