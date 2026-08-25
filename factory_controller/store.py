"""SQLite mission ledger and single-host runnable queue."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


CONTRACT_VERSION = "factory-controller/1.0"
TERMINAL = {"completed", "refused", "failed", "cancelled"}
RUNNABLE = {"admitted"}
ALLOWED_TRANSITIONS = {
    "dispatching": {"dispatched", "admitted", "refused", "failed", "cancelled"},
    "dispatched": {"candidate_verified", "refused", "failed", "escalated"},
    "candidate_verified": {"evaluated", "refused", "failed", "escalated"},
    "evaluated": {"evidence_sealed", "refused", "failed", "escalated"},
    "evidence_sealed": {"completed", "escalated"},
    "escalated": {"failed", "cancelled"},
}


class ConflictError(ValueError):
    """An idempotency key was reused with different immutable input."""


class LeaseLostError(RuntimeError):
    """A worker attempted to mutate a mission after losing its lease."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class MissionStore:
    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = str(path)
        self.clock = clock
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missions (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  payload_hash TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL,
                  next_run_at REAL NOT NULL,
                  lease_owner TEXT,
                  lease_token TEXT,
                  lease_expires_at REAL,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  terminal_reason TEXT,
                  result_json TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS missions_runnable
                  ON missions(state, next_run_at, created_at);
                CREATE TABLE IF NOT EXISTS attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  mission_id TEXT NOT NULL REFERENCES missions(id),
                  number INTEGER NOT NULL,
                  worker_id TEXT NOT NULL,
                  lease_token TEXT NOT NULL,
                  started_at REAL NOT NULL,
                  ended_at REAL,
                  outcome TEXT,
                  diagnostic TEXT,
                  UNIQUE(mission_id, number)
                );
                CREATE TABLE IF NOT EXISTS steps (
                  mission_id TEXT NOT NULL REFERENCES missions(id),
                  name TEXT NOT NULL,
                  status TEXT NOT NULL,
                  operation_key TEXT NOT NULL UNIQUE,
                  input_hash TEXT NOT NULL,
                  output_json TEXT,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(mission_id, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  mission_id TEXT NOT NULL REFERENCES missions(id),
                  kind TEXT NOT NULL,
                  from_state TEXT,
                  to_state TEXT,
                  detail_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                """
            )
            db.execute("INSERT OR IGNORE INTO schema_meta VALUES (1, ?)", (self.clock(),))

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("payload_json", "result_json"):
            if value.get(key) is not None:
                value[key.removesuffix("_json")] = json.loads(value.pop(key))
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def _event(self, db: sqlite3.Connection, mission_id: str, kind: str,
               old: str | None, new: str | None, detail: Any) -> None:
        db.execute(
            "INSERT INTO events(mission_id,kind,from_state,to_state,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (mission_id, kind, old, new, canonical_json(detail), self.clock()),
        )

    def submit(self, payload: dict[str, Any], idempotency_key: str,
               *, max_attempts: int = 3) -> tuple[dict[str, Any], bool]:
        if not idempotency_key or max_attempts < 1:
            raise ValueError("idempotency_key is required and max_attempts must be positive")
        digest = payload_hash(payload)
        now = self.clock()
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM missions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise ConflictError("IDEMPOTENCY_CONFLICT: key already binds different input")
                return self._row(existing), False  # type: ignore[return-value]
            mission_id = f"fm_{digest[:24]}"
            db.execute(
                "INSERT INTO missions(id,idempotency_key,payload_hash,payload_json,state,max_attempts,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mission_id, idempotency_key, digest, canonical_json(payload), "admitted", max_attempts, now, now, now),
            )
            self._event(db, mission_id, "SUBMITTED_ADMITTED", None, "admitted", {"payload_hash": digest, "contract_version": CONTRACT_VERSION})
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            return self._row(row), True  # type: ignore[return-value]

    def get(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    def claim(self, worker_id: str, *, lease_seconds: float = 30) -> dict[str, Any] | None:
        now = self.clock()
        token = str(uuid.uuid4())
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM missions WHERE state='admitted' AND next_run_at<=? AND cancel_requested=0 ORDER BY next_run_at,created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempt = row["attempt_count"] + 1
            changed = db.execute(
                "UPDATE missions SET state='dispatching',attempt_count=?,lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE id=? AND state='admitted'",
                (attempt, worker_id, token, now + lease_seconds, now, row["id"]),
            ).rowcount
            if changed != 1:
                return None
            db.execute(
                "INSERT INTO attempts(mission_id,number,worker_id,lease_token,started_at) VALUES(?,?,?,?,?)",
                (row["id"], attempt, worker_id, token, now),
            )
            self._event(db, row["id"], "CLAIMED_ATTEMPT_STARTED", "admitted", "dispatching", {"worker_id": worker_id, "attempt": attempt, "attempt_id": payload_hash({"mission_id": row["id"], "attempt": attempt, "request_identity": row["payload_hash"]}), "lease_token": token})
            return self._row(db.execute("SELECT * FROM missions WHERE id=?", (row["id"],)).fetchone())

    def transition(self, mission_id: str, lease_token: str, new_state: str,
                   *, detail: Any = None, result: Any = None, reason: str | None = None,
                   release_lease: bool = False) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None or row["lease_token"] != lease_token:
                raise LeaseLostError("LEASE_LOST")
            if new_state not in ALLOWED_TRANSITIONS.get(row["state"], set()):
                raise ValueError(f"INVALID_TRANSITION: {row['state']} -> {new_state}")
            now = self.clock()
            owner = token = expiry = None if release_lease else row["lease_owner"]
            if not release_lease:
                token, expiry = row["lease_token"], row["lease_expires_at"]
            db.execute(
                "UPDATE missions SET state=?,result_json=?,terminal_reason=?,lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE id=? AND lease_token=?",
                (new_state, None if result is None else canonical_json(result), reason, owner, token, expiry, now, mission_id, lease_token),
            )
            self._event(db, mission_id, "TRANSITION", row["state"], new_state, detail or {})
            if new_state in TERMINAL or release_lease:
                db.execute(
                    "UPDATE attempts SET ended_at=?,outcome=?,diagnostic=? WHERE mission_id=? AND number=?",
                    (now, new_state, reason, mission_id, row["attempt_count"]),
                )

    def renew(self, mission_id: str, lease_token: str, lease_seconds: float) -> None:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE missions SET lease_expires_at=?,updated_at=? WHERE id=? AND lease_token=?",
                (self.clock() + lease_seconds, self.clock(), mission_id, lease_token),
            ).rowcount
            if changed != 1:
                raise LeaseLostError("LEASE_LOST")

    def retry(self, mission_id: str, lease_token: str, diagnostic: str,
              delay: float | None = None, *, delay_seconds: float | None = None) -> str:
        if delay is None:
            delay = delay_seconds
        if delay is None:
            raise ValueError("retry delay is required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM missions WHERE id=? AND lease_token=?", (mission_id, lease_token)).fetchone()
            if row is None:
                raise LeaseLostError("LEASE_LOST")
            now = self.clock()
            exhausted = row["attempt_count"] >= row["max_attempts"]
            state = "escalated" if exhausted else "admitted"
            reason = "RETRIES_EXHAUSTED: " + diagnostic if exhausted else diagnostic
            db.execute(
                "UPDATE missions SET state=?,next_run_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,terminal_reason=?,updated_at=? WHERE id=?",
                (state, now + delay, reason if exhausted else None, now, mission_id),
            )
            db.execute(
                "UPDATE attempts SET ended_at=?,outcome=?,diagnostic=? WHERE mission_id=? AND number=?",
                (now, "RETRIES_EXHAUSTED" if exhausted else "RETRY", diagnostic, mission_id, row["attempt_count"]),
            )
            self._event(db, mission_id, "RETRY_EXHAUSTED" if exhausted else "RETRY_SCHEDULED", row["state"], state, {"diagnostic": diagnostic, "delay": delay})
            return state

    def cancel(self, mission_id: str) -> str:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise KeyError(mission_id)
            if row["state"] in TERMINAL:
                return row["state"]
            if row["state"] == "admitted":
                db.execute("UPDATE missions SET state='cancelled',cancel_requested=1,terminal_reason='OPERATOR_CANCELLED',updated_at=? WHERE id=?", (self.clock(), mission_id))
                self._event(db, mission_id, "CANCELLED", "admitted", "cancelled", {})
                return "cancelled"
            if row["state"] in {"dispatched", "candidate_verified", "evaluated", "evidence_sealed"}:
                raise ValueError("CANCELLATION_AFTER_SIDE_EFFECT")
            db.execute("UPDATE missions SET cancel_requested=1,updated_at=? WHERE id=?", (self.clock(), mission_id))
            self._event(db, mission_id, "CANCELLATION_REQUESTED", row["state"], row["state"], {})
            return row["state"]

    def recover_stale(self) -> int:
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM missions WHERE lease_token IS NOT NULL AND lease_expires_at<=? AND state NOT IN ('completed','refused','failed','cancelled')",
                (now,),
            ).fetchall()
            for row in rows:
                state = "cancelled" if row["cancel_requested"] else "admitted"
                db.execute("UPDATE missions SET state=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,terminal_reason=?,updated_at=? WHERE id=?", (state, "OPERATOR_CANCELLED" if state == "cancelled" else None, now, row["id"]))
                db.execute("UPDATE attempts SET ended_at=?,outcome='STALE_LEASE',diagnostic='lease expired' WHERE mission_id=? AND number=?", (now, row["id"], row["attempt_count"]))
                self._event(db, row["id"], "STALE_LEASE_RECOVERED", row["state"], state, {"prior_worker": row["lease_owner"]})
            return len(rows)

    def begin_step(self, mission_id: str, lease_token: str, name: str,
                   input_value: Any, compatibility_input: Any = None) -> dict[str, Any]:
        # Compatibility with the landed boundary's provisional call shape;
        # the Controller still derives the durable operation key itself.
        if compatibility_input is not None:
            input_value = compatibility_input
        digest = payload_hash(input_value)
        with self.transaction() as db:
            mission = db.execute("SELECT * FROM missions WHERE id=? AND lease_token=?", (mission_id, lease_token)).fetchone()
            if mission is None:
                raise LeaseLostError("LEASE_LOST")
            row = db.execute("SELECT * FROM steps WHERE mission_id=? AND name=?", (mission_id, name)).fetchone()
            if row:
                if row["input_hash"] != digest:
                    raise ConflictError("STEP_REPLAY_CONFLICT")
                value = dict(row)
                if value.get("output_json"):
                    value["output"] = json.loads(value["output_json"])
                return value
            operation_key = f"{mission['idempotency_key']}:{name}"
            db.execute("INSERT INTO steps VALUES(?,?,?,?,?,?,?)", (mission_id, name, "STARTED", operation_key, digest, None, self.clock()))
            self._event(db, mission_id, "STEP_STARTED", mission["state"], mission["state"], {"step": name, "operation_key": operation_key})
            return {"mission_id": mission_id, "name": name, "status": "STARTED", "operation_key": operation_key, "input_hash": digest}

    def complete_step(self, mission_id: str, lease_token: str, name: str, output: Any) -> None:
        with self.transaction() as db:
            mission = db.execute("SELECT * FROM missions WHERE id=? AND lease_token=?", (mission_id, lease_token)).fetchone()
            if mission is None:
                raise LeaseLostError("LEASE_LOST")
            changed = db.execute("UPDATE steps SET status='COMPLETED',output_json=?,updated_at=? WHERE mission_id=? AND name=?", (canonical_json(output), self.clock(), mission_id, name)).rowcount
            if changed != 1:
                raise KeyError(name)
            self._event(db, mission_id, "STEP_COMPLETED", mission["state"], mission["state"], {"step": name})

    def history(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE mission_id=? ORDER BY sequence", (mission_id,)).fetchall()
            return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            return {row["state"]: row["n"] for row in db.execute("SELECT state,count(*) n FROM missions GROUP BY state")}
