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
RECOVERABLE = {"dispatched", "candidate_verified", "evaluated", "evidence_sealed"}
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
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  mission_id TEXT NOT NULL REFERENCES missions(id),
                  attempt_number INTEGER NOT NULL,
                  leg INTEGER NOT NULL,
                  profile TEXT,
                  selection_reason TEXT NOT NULL,
                  considered_json TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  process_started INTEGER,
                  idempotency_key TEXT NOT NULL,
                  receipt_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  UNIQUE(mission_id, attempt_number, leg)
                );
                CREATE INDEX IF NOT EXISTS runs_by_mission ON runs(mission_id, id);
                CREATE TRIGGER IF NOT EXISTS runs_no_update
                BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'runs are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS runs_no_delete
                BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'runs are append-only'); END;
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

    def log(self, mission_id: str, kind: str, detail: Any) -> None:
        """Append one observation to the ledger without changing mission state."""

        with self.transaction() as db:
            self._event(db, mission_id, kind, None, None, detail)

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
            # Mission identity binds the key as well as the payload. Deriving it
            # from the payload alone collided two distinct missions that happened
            # to carry identical input -- routine once one repository is targeted
            # more than once -- and surfaced as a raw IntegrityError rather than a
            # refusal. `controller_contract.mission_identity` already derives from
            # `request_identity_hash`, which includes the key; this matches it.
            mission_id = f"fm_{payload_hash({'idempotency_key': idempotency_key, 'payload_hash': digest})[:24]}"
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
                "SELECT * FROM missions WHERE state IN ('admitted','dispatched','candidate_verified','evaluated','evidence_sealed') AND lease_token IS NULL AND next_run_at<=? AND cancel_requested=0 ORDER BY next_run_at,created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            fresh_attempt = row["state"] == "admitted"
            attempt = row["attempt_count"] + 1 if fresh_attempt else row["attempt_count"]
            next_state = "dispatching" if fresh_attempt else row["state"]
            changed = db.execute(
                "UPDATE missions SET state=?,attempt_count=?,lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE id=? AND state=? AND lease_token IS NULL",
                (next_state, attempt, worker_id, token, now + lease_seconds, now, row["id"], row["state"]),
            ).rowcount
            if changed != 1:
                return None
            if fresh_attempt:
                db.execute(
                    "INSERT INTO attempts(mission_id,number,worker_id,lease_token,started_at) VALUES(?,?,?,?,?)",
                    (row["id"], attempt, worker_id, token, now),
                )
            self._event(db, row["id"], "CLAIMED_ATTEMPT_STARTED" if fresh_attempt else "CLAIMED_RESUME", row["state"], next_state, {"worker_id": worker_id, "attempt": attempt, "attempt_id": payload_hash({"mission_id": row["id"], "attempt": attempt, "request_identity": row["payload_hash"]}), "lease_token": token})
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
                state = "cancelled" if row["cancel_requested"] else (row["state"] if row["state"] in RECOVERABLE else "admitted")
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

    # ----------------------------------------------------------------- #
    # provider route history
    # ----------------------------------------------------------------- #

    def record_run(self, mission_id: str, attempt_number: int, selection: Any,
                   receipt: Any, idempotency_key: str) -> int:
        """Append one routing leg.  Legs are facts, so the table never updates.

        The leg number is derived inside the transaction rather than counted by
        the caller, so two writers cannot mint the same one.
        """

        with self.transaction() as db:
            leg = db.execute(
                "SELECT COALESCE(MAX(leg),0)+1 n FROM runs WHERE mission_id=? AND attempt_number=?",
                (mission_id, attempt_number),
            ).fetchone()["n"]
            db.execute(
                "INSERT INTO runs(mission_id,attempt_number,leg,profile,selection_reason,considered_json,outcome,process_started,idempotency_key,receipt_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, attempt_number, leg, receipt["profile"], selection["reason"],
                 canonical_json(selection["considered"]), receipt["classification"],
                 None if receipt["process_started"] is None else int(receipt["process_started"]),
                 idempotency_key, canonical_json(receipt), self.clock()),
            )
            self._event(db, mission_id, "ROUTE_LEG", None, None, {
                "attempt": attempt_number, "leg": leg, "profile": receipt["profile"],
                "selection_reason": selection["reason"], "outcome": receipt["classification"],
                "process_started": receipt["process_started"],
            })
            return leg

    def runs(self, mission_id: str) -> list[dict[str, Any]]:
        """Every routing leg for one mission, oldest first.  Scoped by id only."""

        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE mission_id=? ORDER BY id", (mission_id,)).fetchall()
            return [
                {**dict(row), "considered": json.loads(row["considered_json"]),
                 "receipt": json.loads(row["receipt_json"]),
                 "process_started": None if row["process_started"] is None else bool(row["process_started"])}
                for row in rows
            ]

    def route_history(self, mission_id: str) -> dict[str, Any]:
        """The operator's question, answered from durable state alone.

        Which provider ran, why it was chosen, what else was considered, whether
        a fallback happened, where the irreversible boundary was crossed, and
        why any later switch was refused.
        """

        legs = self.runs(mission_id)
        mission = self.get(mission_id)
        side_effect = next(
            (leg for leg in legs if leg["process_started"] is not False), None
        )
        refusals = [
            {"attempt": event["detail"].get("attempt"), "code": event["detail"].get("code"),
             "profile": event["detail"].get("profile"), "detail": event["detail"].get("detail")}
            for event in self.history(mission_id) if event["kind"] == "ROUTE_SWITCH_REFUSED"
        ]
        return {
            "mission_id": mission_id,
            "state": None if mission is None else mission["state"],
            "selected_profile": None if side_effect is None else side_effect["profile"],
            "legs": [
                {"attempt": leg["attempt_number"], "leg": leg["leg"], "profile": leg["profile"],
                 "selection_reason": leg["selection_reason"], "considered": leg["considered"],
                 "outcome": leg["outcome"], "process_started": leg["process_started"],
                 "idempotency_key": leg["idempotency_key"]}
                for leg in legs
            ],
            "fallback_count": max(0, len(legs) - 1),
            "side_effect_boundary": None if side_effect is None else {
                "attempt": side_effect["attempt_number"], "leg": side_effect["leg"],
                "profile": side_effect["profile"],
                "process_started": side_effect["process_started"],
            },
            "switch_refusals": refusals,
        }

    def receipts(self, mission_id: str) -> list[dict[str, Any]]:
        return [leg["receipt"] for leg in self.runs(mission_id)]

    def telemetry(self, mission_id: str) -> dict[str, Any]:
        """The Stage-4 seam: measured inputs only, absence kept explicit.

        Every field is either a fact this Controller observed or one a provider
        reported.  Nothing here is estimated, and an unreported number stays
        absent rather than becoming a zero.
        """

        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        legs = self.runs(mission_id)
        usages = [leg["receipt"].get("usage") or {} for leg in legs]
        durations = [leg["receipt"].get("duration_ms") for leg in legs]
        measured = [value for value in durations if isinstance(value, int)]
        events = self.history(mission_id)
        payload = mission.get("payload") or {}
        return {
            "mission_id": mission_id,
            "outcome": mission["state"],
            "terminal_reason": mission["terminal_reason"],
            "execution_mode": payload.get("execution_mode", "fixture"),
            "provider_profile": next(
                (leg["profile"] for leg in reversed(legs) if leg["process_started"] is not False),
                None,
            ),
            "route_legs": len(legs),
            "fallback_count": max(0, len(legs) - 1),
            "retries": max(0, mission["attempt_count"] - 1),
            "elapsed_execution_ms": sum(measured) if measured else "unknown",
            "unmeasured_legs": len(durations) - len(measured),
            "reported_input_tokens": _sum_reported(usages, "input_tokens"),
            "reported_output_tokens": _sum_reported(usages, "output_tokens"),
            "reported_cost": _sum_cost(usages),
            "owner_intervention": mission["state"] == "escalated" or mission["cancel_requested"],
            "context_reference": {
                "work_item_id": payload.get("work_item_id", "unknown"),
                "context_manifest_hash": payload.get("context_manifest_hash", "unknown"),
                "repository_remote_url": payload.get("repository_remote_url", "unknown"),
                "idempotency_key": mission["idempotency_key"],
            },
            "evidence_pointer": (mission.get("result") or {}).get("evidence", {}).get("evidence_pointer", "unknown"),
            "event_count": len(events),
        }


def _sum_reported(usages: list[dict[str, Any]], name: str) -> Any:
    """Sum only what was reported; return ``unknown`` when nothing was."""

    values = [usage.get(name) for usage in usages]
    measured = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    if not measured:
        return "unknown"
    return {"total": sum(measured), "reported_legs": len(measured), "unreported_legs": len(values) - len(measured)}


def _sum_cost(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost never becomes a number unless a provider produced one."""

    priced = [usage for usage in usages if usage.get("cost_state") == "reported"]
    currencies = {usage.get("cost_currency") for usage in priced}
    if not priced:
        return {"state": "unknown", "unpriced_legs": len(usages)}
    if len(currencies) > 1:
        return {"state": "unknown", "reason": "mixed_currencies",
                "currencies": sorted(str(value) for value in currencies),
                "unpriced_legs": len(usages) - len(priced)}
    return {"state": "reported", "amount": round(sum(float(usage["cost_amount"]) for usage in priced), 10),
            "currency": priced[0].get("cost_currency"), "priced_legs": len(priced),
            "unpriced_legs": len(usages) - len(priced)}
