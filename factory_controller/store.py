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

from . import capacity as capacity_policy
from . import context as context_contract
from . import portfolio


CONTRACT_VERSION = "factory-controller/1.0"
#: Reproduced from factory-evidence-core ``src/contracts/replay.py``; see
#: ``routing.CANONICAL_ABSENCE``, which this must equal.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run", "not_measurable"})
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


class _Refused(Exception):
    """A refusal carried out of a transaction so its explanation can outlive it."""

    def __init__(self, code: str, detail: dict[str, Any], mission_id: str | None,
                 project_id: str | None) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail
        self.mission_id, self.project_id = mission_id, project_id


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
                  provider_profile TEXT,
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
                CREATE TABLE IF NOT EXISTS projects (
                  project_id TEXT PRIMARY KEY,
                  repository TEXT NOT NULL,
                  state TEXT NOT NULL,
                  priority INTEGER NOT NULL,
                  concurrency_cap INTEGER NOT NULL,
                  budget_ceiling REAL,
                  budget_currency TEXT,
                  context_ceiling_bytes INTEGER,
                  acceptance_gate_ids TEXT,
                  acceptance_gate_source TEXT,
                  policy_version TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  portfolio_concurrency INTEGER NOT NULL,
                  emergency_stop INTEGER NOT NULL DEFAULT 0,
                  aging_seconds REAL NOT NULL,
                  policy_version TEXT NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                  mission_id TEXT NOT NULL REFERENCES missions(id),
                  depends_on TEXT NOT NULL REFERENCES missions(id),
                  on_failure TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  released_at REAL,
                  PRIMARY KEY(mission_id, depends_on)
                );
                CREATE INDEX IF NOT EXISTS dependencies_by_prerequisite
                  ON dependencies(depends_on);
                CREATE TABLE IF NOT EXISTS coordination (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  mission_id TEXT,
                  project_id TEXT,
                  decision TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  detail_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS coordination_by_mission
                  ON coordination(mission_id, sequence);
                CREATE TRIGGER IF NOT EXISTS coordination_no_update
                BEFORE UPDATE ON coordination BEGIN SELECT RAISE(ABORT, 'coordination is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS coordination_no_delete
                BEFORE DELETE ON coordination BEGIN SELECT RAISE(ABORT, 'coordination is append-only'); END;
                CREATE TABLE IF NOT EXISTS capacity_runtimes (
                  runtime_id TEXT PRIMARY KEY,
                  managed INTEGER NOT NULL,
                  max_observation_age_seconds REAL NOT NULL,
                  handoff TEXT NOT NULL,
                  unknown_reset_backoff_seconds REAL NOT NULL,
                  policy_version TEXT NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_observations (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  runtime_id TEXT NOT NULL,
                  state TEXT NOT NULL,
                  observed_at REAL NOT NULL,
                  recorded_at REAL NOT NULL,
                  source TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  window_started_at REAL,
                  expected_reset_at REAL,
                  remaining_units REAL,
                  unit TEXT,
                  precision TEXT NOT NULL,
                  detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS capacity_observations_by_runtime
                  ON capacity_observations(runtime_id, observed_at, sequence);
                CREATE TRIGGER IF NOT EXISTS capacity_observations_no_update
                BEFORE UPDATE ON capacity_observations BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS capacity_observations_no_delete
                BEFORE DELETE ON capacity_observations BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
                """
            )
            # A Stage-4 database predates the coordination columns, and dropping
            # it to add two nullable ones would destroy the durable state the
            # whole stage exists to protect.  NULL keeps the Stage-4 meaning:
            # a mission belonging to no project, scheduled under portfolio
            # limits alone.
            present = {row["name"] for row in db.execute("PRAGMA table_info(missions)")}
            for column, ddl in (("project_id", "TEXT"), ("priority", "INTEGER"),
                                # A Stage-9 database predates capacity, and a
                                # deferral it never had is zero -- the one case
                                # where a default of 0 is the fact rather than a
                                # stand-in for an absent measurement.
                                ("deferrals", "INTEGER NOT NULL DEFAULT 0")):
                if column not in present:
                    db.execute("ALTER TABLE missions ADD COLUMN %s %s" % (column, ddl))
            # Same reasoning one stage later: a Stage-5..8 database predates the
            # declared acceptance gates.  NULL is the honest value -- the
            # project has declared none -- and unattended promotion refuses on
            # it rather than inventing a gate name, which is the whole point.
            present = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
            for column, ddl in (("acceptance_gate_ids", "TEXT"),
                                ("acceptance_gate_source", "TEXT")):
                if column not in present:
                    db.execute("ALTER TABLE projects ADD COLUMN %s %s" % (column, ddl))
            db.execute(
                "INSERT OR IGNORE INTO portfolio VALUES (1,?,0,?,'unset',?)",
                (portfolio.DEFAULT_PORTFOLIO_CONCURRENCY, portfolio.DEFAULT_AGING_SECONDS,
                 self.clock()),
            )
            db.execute("INSERT OR IGNORE INTO schema_meta VALUES (1, ?)", (self.clock(),))
            db.execute("INSERT OR IGNORE INTO schema_meta VALUES (2, ?)", (self.clock(),))
            db.execute("INSERT OR IGNORE INTO schema_meta VALUES (3, ?)", (self.clock(),))

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
            project_id = payload.get("project_id") if isinstance(payload, dict) else None
            priority = payload.get("priority") if isinstance(payload, dict) else None
            if not isinstance(project_id, str) or not project_id:
                project_id = None
            if not isinstance(priority, int) or isinstance(priority, bool):
                priority = None
            db.execute(
                "INSERT INTO missions(id,idempotency_key,payload_hash,payload_json,state,max_attempts,next_run_at,created_at,updated_at,project_id,priority) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, idempotency_key, digest, canonical_json(payload), "admitted", max_attempts, now, now, now, project_id, priority),
            )
            self._event(db, mission_id, "SUBMITTED_ADMITTED", None, "admitted", {"payload_hash": digest, "contract_version": CONTRACT_VERSION})
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            return self._row(row), True  # type: ignore[return-value]

    def get(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    def claim(self, worker_id: str, *, lease_seconds: float = 30,
              resume_only: bool = False,
              project_ids: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        """Take the lease on the one mission the portfolio scheduler picked.

        Scheduling happens *inside* the claiming transaction, not in a separate
        pass, so the Stage-2 no-duplicate-claim property still comes from the
        same ``BEGIN IMMEDIATE`` plus the guarded update below.  A second worker
        running the identical scheduler concurrently either loses the write and
        returns ``None``, or sees the first worker's lease and schedules around
        it -- there is no window in which both succeed.

        ``project_ids`` narrows the candidate set to a named set of projects
        before the scheduler runs, for a caller that is only entitled to some
        of them -- an unattended cycle whose Owner window has closed for one
        project, say.  It is a *narrowing* only: caps, budgets, dependencies
        and ageing are still computed over the whole portfolio, so restricting
        a caller can never let it past a bound it would otherwise have hit.

        ``resume_only`` is what a drain is.  It narrows the candidate set to
        missions that already crossed the dispatch boundary *before* the
        scheduler runs, inside the same transaction, so there is no read-then-act
        window in which a drain could still start something new.  It reuses
        ``MissionCandidate.resume`` rather than a second definition of "already
        in flight", because a drain that disagreed with the scheduler about which
        missions those are would abandon exactly the half-finished work it exists
        to protect.
        """

        now = self.clock()
        token = str(uuid.uuid4())
        with self.transaction() as db:
            decision = self._schedule_locked(db, now, resume_only=resume_only,
                                             project_ids=project_ids)
            if decision.verdicts:
                # An idle poll with nothing to consider writes nothing; a poll
                # that passed over real work explains why, once, in one row.
                self._coordination_locked(
                    db, decision.selected, self._project_of(db, decision.selected),
                    "claim", decision.reason, decision.as_row())
            if decision.selected is None:
                return None
            row = db.execute("SELECT * FROM missions WHERE id=?", (decision.selected,)).fetchone()
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
            # Dependency release is a consequence of reaching a terminal state,
            # so it lives at the one choke point every terminal state passes
            # through rather than at each caller that happens to finish work.
            if new_state == "completed":
                released = self._release_dependencies_locked(db, mission_id)
                if released:
                    self._coordination_locked(db, mission_id, row["project_id"], "dependency",
                                              "DEPENDENTS_RELEASED",
                                              {"released": released, "count": len(released)})
            elif new_state in TERMINAL:
                self._propagate_failure_locked(db, mission_id, new_state)

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
            # A capacity deferral is not an attempt.  Nothing was dispatched,
            # no leg was recorded, and counting a closed quota window against a
            # mission's retry budget would make a five-hour window look like a
            # broken provider -- the mission would be escalated for a reason
            # that has nothing to do with the work.
            exhausted = row["attempt_count"] - row["deferrals"] >= row["max_attempts"]
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

    def defer(self, mission_id: str, lease_token: str, reason: str,
              resume_at: float | None) -> dict[str, Any]:
        """Put a mission back for a later window, having started nothing.

        This is the one new mission verb capacity needed, and it exists because
        the alternative loses work: a quota window that closed between the
        claim and the dispatch would otherwise walk the mission through
        ``NO_ADMISSIBLE_PROVIDER`` into ``refused``, which is terminal.

        Two guards make it safe rather than convenient, and both are checked
        here rather than at the caller so no second caller can skip them.  The
        mission must still be ``dispatching`` -- the only pre-boundary state a
        lease-holder can be in -- and no run leg may have failed to prove that
        nothing started.  A deferral after either is refused, and the existing
        uncertainty path handles that case instead.
        """

        with self.transaction() as db:
            row = db.execute("SELECT * FROM missions WHERE id=? AND lease_token=?",
                             (mission_id, lease_token)).fetchone()
            if row is None:
                raise LeaseLostError("LEASE_LOST")
            if row["state"] != "dispatching":
                raise ValueError("CAPACITY_DEFER_AFTER_BOUNDARY: state=%s" % row["state"])
            committed = db.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE mission_id=?"
                " AND (process_started IS NULL OR process_started=1)",
                (mission_id,)).fetchone()["n"]
            if committed:
                raise ValueError("CAPACITY_DEFER_AFTER_BOUNDARY: %d unproven leg(s)" % committed)
            now = self.clock()
            when = now if resume_at is None else max(now, float(resume_at))
            db.execute(
                "UPDATE missions SET state='admitted',next_run_at=?,deferrals=deferrals+1,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?"
                " WHERE id=?", (when, now, mission_id))
            db.execute(
                "UPDATE attempts SET ended_at=?,outcome='CAPACITY_DEFERRED',diagnostic=?"
                " WHERE mission_id=? AND number=?",
                (now, reason, mission_id, row["attempt_count"]))
            self._event(db, mission_id, "CAPACITY_DEFERRED", "dispatching", "admitted",
                        {"reason": reason, "resume_at": when,
                         "deferrals": row["deferrals"] + 1})
            return {"mission_id": mission_id, "state": "admitted", "resume_at": when,
                    "reason": reason, "deferrals": row["deferrals"] + 1}

    # ----------------------------------------------------------------- #
    # capacity
    # ----------------------------------------------------------------- #

    def set_runtime_policy(self, policy: capacity_policy.RuntimePolicy) -> dict[str, Any]:
        """Put one runtime under capacity management, or take it out again."""

        with self.transaction() as db:
            db.execute(
                "INSERT INTO capacity_runtimes VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(runtime_id) DO UPDATE SET managed=excluded.managed,"
                " max_observation_age_seconds=excluded.max_observation_age_seconds,"
                " handoff=excluded.handoff,"
                " unknown_reset_backoff_seconds=excluded.unknown_reset_backoff_seconds,"
                " policy_version=excluded.policy_version, updated_at=excluded.updated_at",
                (policy.runtime_id, int(policy.managed), policy.max_observation_age_seconds,
                 policy.handoff, policy.unknown_reset_backoff_seconds,
                 policy.policy_version, self.clock()))
        return policy.as_row()

    def runtime_policies(self) -> dict[str, capacity_policy.RuntimePolicy]:
        with self.connect() as db:
            return {row["runtime_id"]: _runtime_policy(row)
                    for row in db.execute("SELECT * FROM capacity_runtimes")}

    def observe_capacity(self, observation: capacity_policy.CapacityObservation) -> dict[str, Any]:
        """Append one measurement.  Observations are facts, so nothing updates."""

        with self.transaction() as db:
            db.execute(
                "INSERT INTO capacity_observations(runtime_id,state,observed_at,recorded_at,"
                "source,source_ref,window_started_at,expected_reset_at,remaining_units,unit,"
                "precision,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation.runtime_id, observation.state, observation.observed_at,
                 self.clock(), observation.source, observation.source_ref,
                 observation.window_started_at, observation.expected_reset_at,
                 observation.remaining_units, observation.unit, observation.precision,
                 canonical_json(observation.detail)))
        return observation.as_row()

    def latest_observations(self) -> dict[str, capacity_policy.CapacityObservation]:
        """The newest measurement per runtime, by observation time then arrival.

        Ordering by ``observed_at`` before ``sequence`` is what stops a
        late-arriving *older* reading from reopening a window that a newer one
        closed -- the direction in which a mistake would invent capacity.
        """

        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM capacity_observations ORDER BY observed_at,sequence").fetchall()
        return {row["runtime_id"]: _observation(row) for row in rows}

    def capacity_readings(self, now: float | None = None) -> dict[str, capacity_policy.RuntimeReading]:
        return capacity_policy.readings(self.runtime_policies(), self.latest_observations(),
                                        self.clock() if now is None else now)

    def capacity_observations(self, runtime_id: str | None = None, *,
                              limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            if runtime_id is None:
                rows = db.execute("SELECT * FROM capacity_observations"
                                  " ORDER BY sequence DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM capacity_observations WHERE runtime_id=?"
                                  " ORDER BY sequence DESC LIMIT ?",
                                  (runtime_id, limit)).fetchall()
        return [{**_observation(row).as_row(), "sequence": row["sequence"],
                 "recorded_at": row["recorded_at"]} for row in rows]

    def capacity_checkpoint(self, mission_id: str,
                            reading: capacity_policy.RuntimeReading | None = None
                            ) -> dict[str, Any]:
        """What the ledger says about one mission, as a portable checkpoint.

        Re-derived on every call rather than stored.  ``continuity.py`` owns the
        Work Baton -- a token issued once and consumed once -- and this is the
        reading a baton is built *from*, so the two cannot drift: there is only
        one copy of these facts and it is the mission ledger.
        """

        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        with self.connect() as db:
            steps = {row["name"]: row["status"] for row in db.execute(
                "SELECT name,status FROM steps WHERE mission_id=?", (mission_id,))}
        dispatch = self.step_output(mission_id, "dispatch") or {}
        evidence = self.step_output(mission_id, "evidence") or {}
        project = self.project(mission["project_id"]) if mission["project_id"] else None
        return capacity_policy.checkpoint_facts(
            mission, mission["payload"], steps, self.runs(mission_id), reading,
            repository=None if project is None else project.repository,
            candidate_sha=dispatch.get("candidate_sha") if isinstance(dispatch, dict) else None,
            evidence_pointer=evidence.get("evidence_pointer") if isinstance(evidence, dict) else None)

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

    def step_output(self, mission_id: str, name: str) -> Any | None:
        """What a completed step recorded, or ``None`` if it never completed."""

        with self.connect() as db:
            row = db.execute(
                "SELECT output_json FROM steps WHERE mission_id=? AND name=? AND status='COMPLETED'",
                (mission_id, name),
            ).fetchone()
            return None if row is None or row["output_json"] is None else json.loads(row["output_json"])

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

        prof = receipt.get("provider_profile") if "provider_profile" in receipt else receipt.get("profile")
        with self.transaction() as db:
            leg = db.execute(
                "SELECT COALESCE(MAX(leg),0)+1 n FROM runs WHERE mission_id=? AND attempt_number=?",
                (mission_id, attempt_number),
            ).fetchone()["n"]
            db.execute(
                "INSERT INTO runs(mission_id,attempt_number,leg,provider_profile,selection_reason,considered_json,outcome,process_started,idempotency_key,receipt_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, attempt_number, leg, prof, selection["reason"],
                 canonical_json(selection["considered"]), receipt["classification"],
                 None if receipt["process_started"] is None else int(receipt["process_started"]),
                 idempotency_key, canonical_json(receipt), self.clock()),
            )
            self._event(db, mission_id, "ROUTE_LEG", None, None, {
                "attempt": attempt_number, "leg": leg, "provider_profile": prof,
                "selection_reason": selection["reason"], "outcome": receipt["classification"],
                "process_started": receipt["process_started"],
            })
            return leg

    def runs(self, mission_id: str) -> list[dict[str, Any]]:
        """Every routing leg for one mission, oldest first.  Scoped by id only."""

        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE mission_id=? ORDER BY id", (mission_id,)).fetchall()
            return [
                {**dict(row), "profile": row["provider_profile"], "considered": json.loads(row["considered_json"]),
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
             "provider_profile": event["detail"].get("provider_profile"),
             "detail": event["detail"].get("detail")}
            for event in self.history(mission_id) if event["kind"] == "ROUTE_SWITCH_REFUSED"
        ]
        # One name per concept. `provider_profile` is the bridge's own word for
        # this, so a second spelling kept alive beside it would be the identity
        # divergence the corpus already records, not a kindness to old readers.
        return {
            "mission_id": mission_id,
            "state": None if mission is None else mission["state"],
            "selected_provider_profile": None if side_effect is None else side_effect["provider_profile"],
            "legs": [
                {"attempt": leg["attempt_number"], "leg": leg["leg"],
                 "provider_profile": leg["provider_profile"],
                 "selection_reason": leg["selection_reason"], "considered": leg["considered"],
                 "outcome": leg["outcome"], "process_started": leg["process_started"],
                 "idempotency_key": leg["idempotency_key"],
                 "layer_selection_trace": leg["receipt"].get("selection_trace") or []}
                for leg in legs
            ],
            "fallback_count": max(0, len(legs) - 1),
            "side_effect_boundary": None if side_effect is None else {
                "attempt": side_effect["attempt_number"], "leg": side_effect["leg"],
                "provider_profile": side_effect["provider_profile"],
                "process_started": side_effect["process_started"],
            },
            "switch_refusals": refusals,
        }

    def context_history(self, mission_id: str) -> dict[str, Any]:
        """Which context manifest this mission used, why, how big, and what refused.

        Everything here is read back from the durable ledger, so the answer after
        a restart is the answer during the run.  A mission that declared no
        context request says ``not_applicable`` rather than reporting a zero.
        """

        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        payload = mission.get("payload") or {}
        request = context_contract.ContextRequest.from_payload(payload)
        budget = context_contract.ContextBudget.from_payload(payload)
        row = self.step_output(mission_id, "context")
        package = None if row is None else context_contract.package_from_row(row)
        refusals = [
            {"attempt": event["detail"].get("attempt"), "code": event["detail"].get("code"),
             "broker_status": event["detail"].get("broker_status"),
             "context_manifest_hash": event["detail"].get("context_manifest_hash")}
            for event in self.history(mission_id) if event["kind"] == "CONTEXT_REFUSED"
        ]
        explained = context_contract.explain(
            request, package, budget,
            refusal=refusals[-1]["code"] if refusals and package is None else None)
        return {
            "mission_id": mission_id,
            "state": mission["state"],
            "declared_context_manifest_hash": payload.get("context_manifest_hash", "not_applicable"),
            "idempotency_key": mission["idempotency_key"],
            "context_refusals": refusals,
            **explained,
        }

    def economics(self, corpus_identity: str | None = None) -> dict[str, Any]:
        """Context economics per project, from durable state alone.

        This is the Stage-4 measurement model and the handoff seam for the later
        coordination stage in one method: missions group by the corpus their own
        context request named, so a second project is a second group rather than
        a schema change.  Nothing is summed that was not measured -- a project
        whose broker reported no bytes reports ``not_measurable``, never zero.
        """

        groups: dict[str, dict[str, Any]] = {}
        with self.connect() as db:
            rows = db.execute("SELECT id,payload_json,state FROM missions ORDER BY created_at").fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            request = (payload or {}).get("context_request")
            if not isinstance(request, dict):
                continue
            corpus = request.get("corpus_identity")
            if corpus_identity is not None and corpus != corpus_identity:
                continue
            group = groups.setdefault(str(corpus), {
                "corpus_identity": corpus, "missions": 0, "bound": 0, "refused": 0,
                "measured_missions": 0, "baseline_context_bytes": 0,
                "selected_context_bytes": 0, "cache_hits": 0, "cache_misses": 0,
                "mission_ids": [], "gate_passed": 0,
            })
            group["missions"] += 1
            group["mission_ids"].append(row["id"])
            block = self.telemetry(row["id"])["context"]
            if block["state"] == "bound":
                group["bound"] += 1
            elif block["state"] == "refused" or block.get("context_refusals"):
                group["refused"] += 1
            if block.get("cache_state") == "hit":
                group["cache_hits"] += 1
            elif block.get("cache_state") == "miss":
                group["cache_misses"] += 1
            base = block.get("baseline_context_bytes")
            chosen = block.get("selected_context_bytes")
            if isinstance(base, int) and isinstance(chosen, int):
                group["measured_missions"] += 1
                group["baseline_context_bytes"] += base
                group["selected_context_bytes"] += chosen
            if row["state"] == "completed":
                group["gate_passed"] += 1
        for group in groups.values():
            group["reduction"] = _group_reduction(group)
            if not group["measured_missions"]:
                # The broker measured nothing, so there is nothing to add up.
                group["baseline_context_bytes"] = "not_measurable"
                group["selected_context_bytes"] = "not_measurable"
        return {"projects": [groups[key] for key in sorted(groups)],
                "project_count": len(groups)}

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
                (leg["provider_profile"] for leg in reversed(legs) if leg["process_started"] is not False),
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
            "context": self._context_telemetry(mission_id, payload, events),
        }

    def _context_telemetry(self, mission_id: str, payload: dict[str, Any],
                           events: list[dict[str, Any]]) -> dict[str, Any]:
        """Measured context economics.  Absent measurements stay absent words."""

        refusals = [event["detail"].get("code") for event in events
                    if event["kind"] == "CONTEXT_REFUSED"]
        if payload.get("context_request") is None:
            return {"state": "not_applicable", "context_refusals": refusals}
        row = self.step_output(mission_id, "context")
        if row is None:
            return {"state": "not_run", "context_refusals": refusals}
        package = context_contract.package_from_row(row)
        measurement = package.as_row()["measurement"]
        return {
            "state": "bound" if package.manifest is not None else "refused",
            "context_manifest_hash": None if package.manifest is None
            else package.manifest.manifest_hash,
            "corpus_identity": None if package.manifest is None
            else package.manifest.corpus_identity,
            "selected_context_bytes": measurement["selected_context_bytes"],
            "selected_context_files": measurement["selected_context_files"],
            "baseline_context_bytes": measurement["baseline_context_bytes"],
            "baseline_context_files": measurement["baseline_context_files"],
            "manifest_build_ms": measurement["manifest_build_ms"],
            "cache_state": measurement["cache_state"],
            "cache_identity": measurement["cache_identity"],
            "reduction": package.measurement.reduction,
            "context_refusals": refusals,
        }


    # ------------------------------------------------------------------ #
    # Stage 5: the portfolio
    # ------------------------------------------------------------------ #

    def _coordination_locked(self, db: sqlite3.Connection, mission_id: str | None,
                             project_id: str | None, decision: str, reason: str,
                             detail: Any) -> None:
        db.execute(
            "INSERT INTO coordination(mission_id,project_id,decision,reason,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (mission_id, project_id, decision, reason, canonical_json(detail), self.clock()),
        )

    def coordinate(self, mission_id: str | None, project_id: str | None,
                   decision: str, reason: str, detail: Any = None) -> None:
        """Record why something was or was not done.  Append-only, like events."""

        with self.transaction() as db:
            self._coordination_locked(db, mission_id, project_id, decision, reason, detail or {})

    def coordination(self, mission_id: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM coordination"
        params: tuple[Any, ...] = ()
        if mission_id is not None:
            query += " WHERE mission_id=?"
            params = (mission_id,)
        query += " ORDER BY sequence DESC LIMIT ?"
        with self.connect() as db:
            rows = db.execute(query, params + (limit,)).fetchall()
        out = []
        for row in reversed(rows):
            value = dict(row)
            value["detail"] = json.loads(value.pop("detail_json"))
            out.append(value)
        return out

    @staticmethod
    def _project_of(db: sqlite3.Connection, mission_id: str | None) -> str | None:
        if mission_id is None:
            return None
        row = db.execute("SELECT project_id FROM missions WHERE id=?", (mission_id,)).fetchone()
        return None if row is None else row["project_id"]

    # -- registry ------------------------------------------------------- #

    def register_project(self, policy: portfolio.ProjectPolicy) -> dict[str, Any]:
        """Create or update one project.  The Owner's word, stored verbatim."""

        now = self.clock()
        row = policy.as_row()
        with self.transaction() as db:
            existing = db.execute("SELECT project_id FROM projects WHERE project_id=?",
                                  (policy.project_id,)).fetchone()
            db.execute(
                "INSERT INTO projects(project_id,repository,state,priority,concurrency_cap,"
                "budget_ceiling,budget_currency,context_ceiling_bytes,acceptance_gate_ids,"
                "acceptance_gate_source,policy_version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET repository=excluded.repository,"
                " state=excluded.state, priority=excluded.priority,"
                " concurrency_cap=excluded.concurrency_cap, budget_ceiling=excluded.budget_ceiling,"
                " budget_currency=excluded.budget_currency,"
                " context_ceiling_bytes=excluded.context_ceiling_bytes,"
                " acceptance_gate_ids=excluded.acceptance_gate_ids,"
                " acceptance_gate_source=excluded.acceptance_gate_source,"
                " policy_version=excluded.policy_version, updated_at=excluded.updated_at",
                (policy.project_id, policy.repository, policy.state, policy.priority,
                 policy.concurrency_cap, policy.budget_ceiling, policy.budget_currency,
                 policy.context_ceiling_bytes,
                 canonical_json(list(policy.acceptance_gate_ids)),
                 policy.acceptance_gate_source, policy.policy_version, now, now),
            )
            self._coordination_locked(db, None, policy.project_id, "registry",
                                      "PROJECT_UPDATED" if existing else "PROJECT_REGISTERED", row)
        return row

    def set_project_state(self, project_id: str, state: str, *, reason: str = "OWNER") -> dict[str, Any]:
        """Pause, resume, drain, or stop one project.

        No mission row is touched.  A paused project stops *new* claims; every
        mission already past the dispatch boundary is resumed exactly as before,
        because abandoning one is the durable-state corruption the pause exists
        to avoid.
        """

        if state not in portfolio.PROJECT_STATES:
            raise portfolio.PolicyError("unknown project state %r" % (state,))
        with self.transaction() as db:
            row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            db.execute("UPDATE projects SET state=?,updated_at=? WHERE project_id=?",
                       (state, self.clock(), project_id))
            in_flight = db.execute(
                "SELECT COUNT(*) AS n FROM missions WHERE project_id=? AND lease_token IS NOT NULL",
                (project_id,)).fetchone()["n"]
            detail = {"from": row["state"], "to": state, "in_flight": in_flight,
                      "drained": in_flight == 0, "reason": reason}
            self._coordination_locked(db, None, project_id, "registry", "PROJECT_STATE_CHANGED", detail)
        return detail

    def declared_acceptance_gates(self, project_id: str | None,
                                  repository: str | None = None
                                  ) -> tuple[list[str], str]:
        """The acceptance gates the Owner declared for one project.

        This is the whole of SF-141 finding SR-F6, in the one place every caller
        reaches.  Unattended promotion used to pass a literal ``["ACCEPTANCE"]``,
        which no repository declares and no evaluator can run: the stage-1
        adapter returns ``not_run`` for a gate with no command, ``not_run`` is a
        failure, so every unattended repair was on a path to escalate for a
        reason that said nothing about the work.  Gates now come from the
        registry -- an Owner act that names where they were copied from -- or
        the work is not promoted at all.

        The repository equality is the second half.  Gates are declared against
        one repository; applying them to a mission targeting another would let
        one project's registry act quietly govern work it never admitted.
        """

        project = self.project(project_id) if project_id else None
        if project is None:
            raise portfolio.GateProvenanceError(
                "ACCEPTANCE_GATE_PROJECT_UNREGISTERED",
                {"project_id": project_id or "unknown",
                 "detail": "no registered project declares acceptance gates"})
        if not project.acceptance_gate_ids:
            raise portfolio.GateProvenanceError(
                "ACCEPTANCE_GATES_UNDECLARED",
                {"project_id": project.project_id,
                 "acceptance_gate_ids": "not_applicable",
                 "detail": "the project declares no acceptance gates; work "
                           "nobody typed may not invent one"})
        if repository and repository != project.repository:
            raise portfolio.GateProvenanceError(
                "ACCEPTANCE_GATE_REPOSITORY_NOT_ADMITTED",
                {"project_id": project.project_id,
                 "declared_for": project.repository, "work_targets": repository,
                 "detail": "acceptance gates are declared against one repository"})
        return list(project.acceptance_gate_ids), project.acceptance_gate_source

    def project(self, project_id: str) -> portfolio.ProjectPolicy | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return None if row is None else _project_policy(row)

    def projects(self) -> dict[str, portfolio.ProjectPolicy]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY project_id").fetchall()
        return {row["project_id"]: _project_policy(row) for row in rows}

    def portfolio_policy(self) -> portfolio.PortfolioPolicy:
        with self.connect() as db:
            row = db.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
        return _portfolio_policy(row)

    def set_portfolio_policy(self, policy: portfolio.PortfolioPolicy, *,
                             reason: str = "OWNER") -> dict[str, Any]:
        with self.transaction() as db:
            db.execute(
                "UPDATE portfolio SET portfolio_concurrency=?,emergency_stop=?,aging_seconds=?,"
                "policy_version=?,updated_at=? WHERE id=1",
                (policy.portfolio_concurrency, int(policy.emergency_stop), policy.aging_seconds,
                 policy.policy_version, self.clock()),
            )
            self._coordination_locked(db, None, None, "registry", "PORTFOLIO_POLICY_SET",
                                      {**policy.as_row(), "reason": reason})
        return policy.as_row()

    def emergency_stop(self, engaged: bool = True, *, reason: str = "OWNER") -> dict[str, Any]:
        """Stop the whole portfolio starting anything new, immediately.

        One boolean, checked ahead of every other admission gate.  In-flight
        missions are still resumed: a portfolio-wide stop that orphaned a
        provider process mid-run would be the thing it is meant to prevent.
        """

        current = self.portfolio_policy()
        return self.set_portfolio_policy(
            portfolio.PortfolioPolicy(current.portfolio_concurrency, engaged,
                                      current.aging_seconds, current.policy_version),
            reason="EMERGENCY_STOP" if engaged else ("EMERGENCY_STOP_CLEARED: " + reason))

    # -- the dependency graph -------------------------------------------- #

    def add_dependency(self, mission_id: str, depends_on: str, *,
                       on_failure: str = "block") -> dict[str, Any]:
        if on_failure not in portfolio.ON_FAILURE:
            raise portfolio.PolicyError("on_failure must be one of %s" % (portfolio.ON_FAILURE,))
        try:
            return self._add_dependency(mission_id, depends_on, on_failure)
        except _Refused as refusal:
            self.coordinate(refusal.mission_id, refusal.project_id, "dependency",
                            refusal.code, refusal.detail)
            raise portfolio.PolicyError(
                "%s: %s" % (refusal.code, " -> ".join(refusal.detail.get("cycle", ())))) from None

    def _add_dependency(self, mission_id: str, depends_on: str,
                        on_failure: str) -> dict[str, Any]:
        with self.transaction() as db:
            for identifier in (mission_id, depends_on):
                if db.execute("SELECT 1 FROM missions WHERE id=?", (identifier,)).fetchone() is None:
                    raise KeyError(identifier)
            edges = _edges_locked(db)
            cycle = portfolio.cycle_path(edges, mission_id, depends_on)
            project = self._project_of(db, mission_id)
            if cycle:
                # Recorded *after* this transaction rolls back, not inside it.
                # Writing the explanation next to the refusal loses it: the
                # raise unwinds the transaction and takes the coordination row
                # with it, leaving a refusal nobody can read afterwards.
                raise _Refused("DEPENDENCY_CYCLE",
                               {"cycle": list(cycle), "depends_on": depends_on},
                               mission_id, project)
            released = db.execute("SELECT state FROM missions WHERE id=?", (depends_on,)).fetchone()
            now = self.clock()
            db.execute(
                "INSERT OR IGNORE INTO dependencies(mission_id,depends_on,on_failure,created_at,released_at)"
                " VALUES(?,?,?,?,?)",
                (mission_id, depends_on, on_failure, now,
                 now if released["state"] == portfolio.SATISFIED else None),
            )
            detail = {"mission_id": mission_id, "depends_on": depends_on,
                      "on_failure": on_failure,
                      "already_satisfied": released["state"] == portfolio.SATISFIED}
            self._coordination_locked(db, mission_id, project, "dependency",
                                      "DEPENDENCY_DECLARED", detail)
        return detail

    def dependencies(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.*, m.state AS prerequisite_state FROM dependencies d"
                " JOIN missions m ON m.id=d.depends_on WHERE d.mission_id=?"
                " ORDER BY d.depends_on", (mission_id,)).fetchall()
        return [dict(row) for row in rows]

    def dependency_status(self, mission_id: str) -> dict[str, Any]:
        """The derived reading -- ready, waiting, blocked -- plus its evidence."""

        rows = self.dependencies(mission_id)
        prerequisites = tuple(
            portfolio.Prerequisite(row["depends_on"], row["prerequisite_state"], row["on_failure"])
            for row in rows)
        reading = portfolio.dependency_reading(prerequisites)
        reading["mission_id"] = mission_id
        reading["edges"] = [
            {"depends_on": row["depends_on"], "state": row["prerequisite_state"],
             "on_failure": row["on_failure"], "released_at": row["released_at"]}
            for row in rows]
        reading["released"] = sum(1 for row in rows if row["released_at"] is not None)
        return reading

    def all_missions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,project_id,state,priority,created_at FROM missions ORDER BY created_at,id")]

    def dependency_graph(self) -> dict[str, list[str]]:
        with self.connect() as db:
            return {key: list(value) for key, value in _edges_locked(db).items()}

    def _release_dependencies_locked(self, db: sqlite3.Connection, mission_id: str) -> list[str]:
        """Mark this mission's dependents released.  Exactly once, per edge.

        The guard is ``released_at IS NULL`` inside the claiming transaction, so
        a repeated or concurrent completion releases nothing a second time and
        the count is a fact rather than an intention.
        """

        rows = db.execute(
            "SELECT mission_id FROM dependencies WHERE depends_on=? AND released_at IS NULL",
            (mission_id,)).fetchall()
        released = []
        for row in rows:
            changed = db.execute(
                "UPDATE dependencies SET released_at=? WHERE mission_id=? AND depends_on=?"
                " AND released_at IS NULL",
                (self.clock(), row["mission_id"], mission_id)).rowcount
            if changed == 1:
                released.append(row["mission_id"])
                self._coordination_locked(
                    db, row["mission_id"], self._project_of(db, row["mission_id"]),
                    "dependency", "DEPENDENCY_RELEASED",
                    {"depends_on": mission_id, "released_at": self.clock()})
        return released

    def _propagate_failure_locked(self, db: sqlite3.Connection, mission_id: str,
                                  state: str) -> list[str]:
        """Apply each dependent's declared failure policy.  ``block`` does nothing.

        Blocking needs no write: the dependent's reading is derived from the
        edge and the prerequisite's state, and both are already durable.  Only
        ``cancel`` mutates, and only for a dependent that has not started.
        """

        rows = db.execute(
            "SELECT mission_id,on_failure FROM dependencies WHERE depends_on=?",
            (mission_id,)).fetchall()
        affected = []
        for row in rows:
            self._coordination_locked(
                db, row["mission_id"], self._project_of(db, row["mission_id"]),
                "dependency", "PREREQUISITE_FAILED",
                {"depends_on": mission_id, "prerequisite_state": state,
                 "on_failure": row["on_failure"]})
            if row["on_failure"] != "cancel":
                continue
            dependent = db.execute("SELECT * FROM missions WHERE id=?",
                                   (row["mission_id"],)).fetchone()
            if dependent is None or dependent["state"] in TERMINAL:
                continue
            if dependent["state"] == "admitted" and dependent["lease_token"] is None:
                db.execute(
                    "UPDATE missions SET state='cancelled',cancel_requested=1,"
                    "terminal_reason=?,updated_at=? WHERE id=? AND state='admitted'",
                    ("DEPENDENCY_FAILURE_PROPAGATED: " + mission_id, self.clock(), row["mission_id"]))
                self._event(db, row["mission_id"], "CANCELLED", "admitted", "cancelled",
                            {"cause": "DEPENDENCY_FAILURE_PROPAGATED", "depends_on": mission_id})
            else:
                # Past the boundary, cancellation is a request the running
                # attempt observes; forcing it here would be a cancel after a
                # side effect, which Stage 2 already refuses.
                db.execute("UPDATE missions SET cancel_requested=1,updated_at=? WHERE id=?",
                           (self.clock(), row["mission_id"]))
                self._event(db, row["mission_id"], "CANCELLATION_REQUESTED",
                            dependent["state"], dependent["state"],
                            {"cause": "DEPENDENCY_FAILURE_PROPAGATED", "depends_on": mission_id})
            affected.append(row["mission_id"])
        return affected

    # -- scheduling ------------------------------------------------------ #

    def _schedule_locked(self, db: sqlite3.Connection, now: float,
                         *, resume_only: bool = False,
                         project_ids: tuple[str, ...] | None = None
                         ) -> portfolio.ScheduleDecision:
        rows = db.execute(
            "SELECT id,project_id,priority,state,created_at,next_run_at,payload_json FROM missions"
            " WHERE state IN ('admitted','dispatched','candidate_verified','evaluated','evidence_sealed')"
            " AND lease_token IS NULL AND cancel_requested=0 ORDER BY created_at,id").fetchall()
        if not rows:
            return portfolio.ScheduleDecision(None, "NO_RUNNABLE_MISSION", ())
        edges: dict[str, list[tuple[str, str, str]]] = {}
        for edge in db.execute(
                "SELECT d.mission_id,d.depends_on,d.on_failure,m.state FROM dependencies d"
                " JOIN missions m ON m.id=d.depends_on").fetchall():
            edges.setdefault(edge["mission_id"], []).append(
                (edge["depends_on"], edge["state"], edge["on_failure"]))
        in_flight: dict[str, int] = {}
        portfolio_in_flight = 0
        for row in db.execute(
                "SELECT project_id,COUNT(*) AS n FROM missions WHERE lease_token IS NOT NULL"
                " AND state NOT IN ('completed','refused','failed','cancelled')"
                " GROUP BY project_id").fetchall():
            portfolio_in_flight += row["n"]
            if row["project_id"] is not None:
                in_flight[row["project_id"]] = row["n"]
        capacity_readings = self._capacity_readings_locked(db, now)
        candidates = tuple(
            portfolio.MissionCandidate(
                mission_id=row["id"], project_id=row["project_id"], state=row["state"],
                created_at=row["created_at"], ready_at=row["next_run_at"],
                priority=row["priority"],
                prerequisites=tuple(portfolio.Prerequisite(*item)
                                    for item in edges.get(row["id"], ())),
                **_capacity_declaration(row["payload_json"]))
            for row in rows)
        if project_ids is not None:
            # A resume survives the narrowing, ahead of it, for the same reason
            # `evaluate` puts RESUME_AFTER_BOUNDARY ahead of every gate: a
            # mission whose provider process may already have run has to be
            # finished, and leaving it for a window to reopen would strand
            # durable state half-written.
            allowed = frozenset(project_ids)
            candidates = tuple(item for item in candidates
                               if item.resume or item.project_id in allowed)
            if not candidates:
                return portfolio.ScheduleDecision(None, "NO_MISSION_IN_SCOPE", ())
        if resume_only:
            candidates = tuple(item for item in candidates if item.resume)
            if not candidates:
                return portfolio.ScheduleDecision(None, "DRAINED_NO_RESUMABLE_MISSION", ())
        snapshot = portfolio.Snapshot(
            portfolio=_portfolio_policy(db.execute("SELECT * FROM portfolio WHERE id=1").fetchone()),
            projects={row["project_id"]: _project_policy(row)
                      for row in db.execute("SELECT * FROM projects").fetchall()},
            candidates=candidates, in_flight=in_flight, portfolio_in_flight=portfolio_in_flight,
            project_spend=self._spend_locked(db), now=now, capacity=capacity_readings)
        return portfolio.schedule(snapshot)

    def _capacity_readings_locked(self, db: sqlite3.Connection,
                                  now: float) -> dict[str, capacity_policy.RuntimeReading]:
        """Capacity, read inside the claiming transaction with everything else.

        Read here rather than through :meth:`capacity_readings` for the same
        reason the scheduler itself runs inside ``claim``: a reading taken on a
        separate connection would be a snapshot of a different instant, and two
        workers comparing different instants is exactly the read-then-act
        window the single transaction exists to close.
        """

        policies = {row["runtime_id"]: _runtime_policy(row)
                    for row in db.execute("SELECT * FROM capacity_runtimes")}
        observations = {row["runtime_id"]: _observation(row) for row in db.execute(
            "SELECT * FROM capacity_observations ORDER BY observed_at,sequence")}
        if not policies and not observations:
            return {}
        return capacity_policy.readings(policies, observations, now)

    def schedule_preview(self) -> dict[str, Any]:
        """What the scheduler would do right now.  Reads only; claims nothing."""

        with self.connect() as db:
            return self._schedule_locked(db, self.clock()).as_row()

    def _spend_locked(self, db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """Measured spend per project, from receipts alone.

        A leg's cost is counted once.  The provider-neutral ``usage`` figure is
        preferred, and a gateway's own priced figure is used only where ``usage``
        reported none -- they describe the same money, and adding both would
        double-charge a project for one call.  Anything unpriced increments
        ``unpriced_legs`` and contributes nothing: unknown cost is not zero.
        """

        spend: dict[str, dict[str, Any]] = {}
        for row in db.execute(
                "SELECT m.project_id AS pid, r.receipt_json FROM runs r"
                " JOIN missions m ON m.id=r.mission_id WHERE m.project_id IS NOT NULL").fetchall():
            group = spend.setdefault(row["pid"], {"known_spend": 0.0, "currency": None,
                                                  "priced_legs": 0, "unpriced_legs": 0,
                                                  "currencies": []})
            receipt = json.loads(row["receipt_json"])
            usage = receipt.get("usage") or {}
            gateway = receipt.get("gateway") or {}
            amount = currency = None
            if usage.get("cost_state") == "reported":
                amount, currency = usage.get("cost_amount"), usage.get("cost_currency")
            elif gateway.get("cost_state") == "reported":
                amount, currency = gateway.get("cost_amount"), gateway.get("cost_currency")
            if amount is None or not currency:
                group["unpriced_legs"] += 1
                continue
            group["priced_legs"] += 1
            group["known_spend"] += float(amount)
            if currency not in group["currencies"]:
                group["currencies"].append(currency)
            group["currency"] = group["currencies"][0] if len(group["currencies"]) == 1 else currency
            if len(group["currencies"]) > 1:
                # Mixed currencies are never converted.  The first one stays the
                # comparison basis and the mismatch fails the next dispatch.
                group["currency"] = group["currencies"][0]
                group["mixed_currencies"] = list(group["currencies"])
        return spend

    # -- portfolio economics --------------------------------------------- #

    def portfolio_economics(self, project_id: str | None = None) -> dict[str, Any]:
        """Context and provider spend per project and for the portfolio.

        Two fact classes stay apart and are never blended: measured context
        bytes come from the broker, priced provider spend comes from receipts,
        and each keeps its own absence word when nothing measured it.  Nothing
        here estimates an unknown, so a portfolio total is the sum of what was
        actually reported plus an explicit count of what was not.
        """

        with self.connect() as db:
            projects = {row["project_id"]: _project_policy(row)
                        for row in db.execute("SELECT * FROM projects ORDER BY project_id").fetchall()}
            spend = self._spend_locked(db)
            rows = db.execute("SELECT id,project_id,state FROM missions ORDER BY created_at").fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for key, policy in projects.items():
            groups[key] = _economics_group(key, policy)
        for row in rows:
            key = row["project_id"]
            if key is None or (project_id is not None and key != project_id):
                continue
            group = groups.setdefault(key, _economics_group(key, None))
            group["missions"] += 1
            if row["state"] == "completed":
                group["completed"] += 1
            elif row["state"] in TERMINAL:
                group["terminal_not_completed"] += 1
            block = self.telemetry(row["id"])["context"]
            base, chosen = block.get("baseline_context_bytes"), block.get("selected_context_bytes")
            if isinstance(base, int) and isinstance(chosen, int):
                group["measured_missions"] += 1
                group["baseline_context_bytes"] += base
                group["selected_context_bytes"] += chosen
            else:
                group["unmeasured_missions"] += 1
        for key, group in groups.items():
            group.update(_spend_block(spend.get(key, {})))
            group["reduction"] = _group_reduction(group)
            if not group["measured_missions"]:
                group["baseline_context_bytes"] = "not_measurable"
                group["selected_context_bytes"] = "not_measurable"
        selected = {key: group for key, group in groups.items()
                    if project_id is None or key == project_id}
        return {"projects": [selected[key] for key in sorted(selected)],
                "project_count": len(selected),
                "portfolio": _portfolio_total(selected.values()),
                "policy": self.portfolio_policy().as_row()}


def _capacity_declaration(payload_json: str) -> dict[str, Any]:
    """The runtimes and estimate one mission declared, for the scheduler.

    A payload the Controller already accepted cannot become unreadable later,
    but a malformed declaration must not take the whole scheduling pass down
    with it: an unreadable estimate narrows nothing, and the mission is then
    refused by ``Controller.validate`` on its own terms rather than by
    disappearing from every other mission's schedule.
    """

    try:
        payload = json.loads(payload_json)
        runtimes = tuple(
            entry if isinstance(entry, str) else entry.get("profile")
            for entry in (payload.get("provider_candidates") or ()))
        estimate = capacity_policy.WorkEstimate.from_payload(payload)
    except (ValueError, TypeError, AttributeError, capacity_policy.PolicyError):
        return {}
    return {"runtimes": tuple(name for name in runtimes if isinstance(name, str) and name),
            "estimate": estimate}


def _absent_number(value: Any) -> Any:
    """An absent time as a canonical absence word, never as ``0``."""

    return "unknown" if value is None else value


def _runtime_policy(row: sqlite3.Row) -> capacity_policy.RuntimePolicy:
    return capacity_policy.RuntimePolicy(
        runtime_id=row["runtime_id"], managed=bool(row["managed"]),
        max_observation_age_seconds=row["max_observation_age_seconds"],
        handoff=row["handoff"],
        unknown_reset_backoff_seconds=row["unknown_reset_backoff_seconds"],
        policy_version=row["policy_version"])


def _observation(row: sqlite3.Row) -> capacity_policy.CapacityObservation:
    return capacity_policy.CapacityObservation(
        runtime_id=row["runtime_id"], state=row["state"], observed_at=row["observed_at"],
        source=row["source"], source_ref=row["source_ref"],
        window_started_at=row["window_started_at"],
        expected_reset_at=row["expected_reset_at"],
        remaining_units=row["remaining_units"], unit=row["unit"],
        precision=row["precision"], detail=json.loads(row["detail_json"]))


def _project_policy(row: sqlite3.Row) -> portfolio.ProjectPolicy:
    return portfolio.ProjectPolicy(
        project_id=row["project_id"], repository=row["repository"], state=row["state"],
        priority=row["priority"], concurrency_cap=row["concurrency_cap"],
        budget_ceiling=row["budget_ceiling"], budget_currency=row["budget_currency"],
        context_ceiling_bytes=row["context_ceiling_bytes"],
        acceptance_gate_ids=tuple(json.loads(row["acceptance_gate_ids"] or "[]")),
        acceptance_gate_source=row["acceptance_gate_source"],
        policy_version=row["policy_version"])


def _portfolio_policy(row: sqlite3.Row | None) -> portfolio.PortfolioPolicy:
    if row is None:
        return portfolio.PortfolioPolicy()
    return portfolio.PortfolioPolicy(
        portfolio_concurrency=row["portfolio_concurrency"], emergency_stop=bool(row["emergency_stop"]),
        aging_seconds=row["aging_seconds"], policy_version=row["policy_version"])


def _edges_locked(db: sqlite3.Connection) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for row in db.execute("SELECT mission_id,depends_on FROM dependencies").fetchall():
        edges.setdefault(row["mission_id"], []).append(row["depends_on"])
    return edges


def _economics_group(project_id: str, policy: portfolio.ProjectPolicy | None) -> dict[str, Any]:
    return {"project_id": project_id, "policy": None if policy is None else policy.as_row(),
            "missions": 0, "completed": 0, "terminal_not_completed": 0,
            "measured_missions": 0, "unmeasured_missions": 0,
            "baseline_context_bytes": 0, "selected_context_bytes": 0}


def _spend_block(spend: dict[str, Any]) -> dict[str, Any]:
    priced = int(spend.get("priced_legs", 0))
    return {"provider_spend": {
        "known_spend": float(spend.get("known_spend", 0.0)) if priced else "not_measurable",
        "currency": spend.get("currency") if priced else "not_applicable",
        "priced_legs": priced, "unpriced_legs": int(spend.get("unpriced_legs", 0)),
        "mixed_currencies": spend.get("mixed_currencies", []),
        "evidence_class": "reported_claim"}}


def _portfolio_total(groups) -> dict[str, Any]:
    groups = list(groups)
    priced = [group["provider_spend"] for group in groups
              if isinstance(group["provider_spend"]["known_spend"], float)]
    currencies = sorted({block["currency"] for block in priced if isinstance(block["currency"], str)})
    total: Any = "not_measurable"
    if priced and len(currencies) == 1:
        total = sum(block["known_spend"] for block in priced)
    elif priced:
        # Two currencies are not added.  Converting them would invent a rate,
        # and the corpus's rule is that an unmeasurable figure stays one.
        total = "not_measurable"
    return {"projects": len(groups),
            "missions": sum(group["missions"] for group in groups),
            "completed": sum(group["completed"] for group in groups),
            "known_spend": total,
            "currency": currencies[0] if len(currencies) == 1 else "not_applicable",
            "currencies": currencies,
            "unpriced_legs": sum(group["provider_spend"]["unpriced_legs"] for group in groups),
            "unmeasured_missions": sum(group["unmeasured_missions"] for group in groups)}


def _group_reduction(group: dict[str, Any]) -> dict[str, Any]:
    """Aggregate reduction over the missions that actually reported both sides."""

    if not group["measured_missions"]:
        return {"state": "not_measurable", "measured_missions": 0}
    base = group["baseline_context_bytes"]
    if base == 0:
        return {"state": "not_applicable", "measured_missions": group["measured_missions"]}
    chosen = group["selected_context_bytes"]
    return {"state": "measured", "measured_missions": group["measured_missions"],
            "baseline_context_bytes": base, "selected_context_bytes": chosen,
            "saved_bytes": base - chosen,
            "reduction_ratio": round((base - chosen) / base, 6)}


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
        # Keep the legs' own absence word when they agree on one. Flattening
        # `not_applicable` into `unknown` throws away the only distinction the
        # absence vocabulary exists to make.
        declared = {usage.get("cost_state") for usage in usages}
        state = declared.pop() if len(declared) == 1 and declared <= CANONICAL_ABSENCE \
            else "unknown"
        return {"state": state, "unpriced_legs": len(usages)}
    if len(currencies) > 1:
        return {"state": "unknown", "reason": "mixed_currencies",
                "currencies": sorted(str(value) for value in currencies),
                "unpriced_legs": len(usages) - len(priced)}
    return {"state": "reported", "amount": round(sum(float(usage["cost_amount"]) for usage in priced), 10),
            "currency": priced[0].get("cost_currency"), "priced_legs": len(priced),
            "unpriced_legs": len(usages) - len(priced)}
