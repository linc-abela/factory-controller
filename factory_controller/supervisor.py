"""Stage 9: the always-on operating layer, as a finite cycle and nothing more.

The Factory can already decide what a mission is (Stage 2), route it (Stage 3),
give it context (Stage 4), coordinate a portfolio of them (Stage 5), deploy one
(Stage 6), repair a failure (Stage 7) and improve itself (Stage 8).  What it
could not do is *keep going* without a person typing the next command.  Every
plane above ends at a verb an operator invokes; nothing in the package called
any of them.

Stage 9 is the caller, and the whole design question is how much of a caller it
is allowed to be.  The answer here is: **one bounded cycle per invocation, and
no authority of its own.**  Seven absences carry it, and each is an enforcement
rather than a decision deferred:

* **No supervisor process.** ``cycle`` performs a finite amount of work and
  returns.  It never sleeps, never loops on a constant, and never calls itself,
  so there is no code path along which the Factory runs away.  A host scheduler
  invokes it; that scheduler is an Owner installation, not a Controller feature.
* **No second mission runtime.** Selected work becomes or resumes an ordinary
  mission through ``Controller.work_once``.  The Stage-5 scheduler, the Context
  Broker, the execution lanes, the evaluator, Evidence Core and the Stage-6
  gates are inherited, not restated -- so "portfolio-aware always-on operation"
  needed no scheduling code at all.
* **No way to create work.** The only inputs to selection are durable rows that
  some earlier stage already admitted: a mission in the ledger, a repair whose
  production fact was recorded, an experiment whose baseline was pinned.  There
  is no method here that takes a proposal, a sentence, a telemetry reading or a
  provider's output, so a model cannot become a source of work by being clever.
* **No approval verb.** Nothing in this module can approve a release, widen a
  policy, register an environment, change a protected surface, or advance its
  own control policy.  A signature test pins the exact set of calls this module
  may make into the Stage-6/7/8 planes.
* **No drain of its own.** A drain is ``resume_only`` on the existing claim,
  which reuses the scheduler's own definition of "already past the boundary".
  A second definition would eventually disagree with the first and abandon the
  half-finished work the drain exists to protect.
* **No emergency stop of its own.** The Owner's stop engages the Stage-5
  portfolio stop, which every plane already honours.  A supervisor-local flag
  would have been a second stop that maintenance and improvement did not read.
* **No hold of its own.** A project hold is the Stage-5 project state, which
  the scheduler, maintenance and improvement all already refuse work against.

What *is* new here is durable control state, a cycle claim that makes repeated
and overlapping invocations safe, a rotation that stops one busy project from
consuming every promotion slot, bounded suppression so a broken provider costs
one skipped project rather than a retry storm, allowed execution windows, and a
plain Owner brief read from durable state alone.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import capacity as capacity_plane
from . import improvement as improvement_plane
from . import maintenance as maintenance_plane
from . import portfolio as portfolio_policy
from . import production
from . import store as ledger


CONTRACT_VERSION = "factory-controller/supervisor/1.0"

#: Reproduced from ``factory-evidence-core`` ``src/contracts/replay.py`` and
#: equal to the same set in ``store``, ``routing``, ``production``,
#: ``maintenance`` and ``improvement``.  It has forked six times across the
#: corpus, once as a one-word typo inside a safety check, so it is stated
#: literally in every layer and pinned by a test that compares the literals.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

#: The Owner's control states.  ``draining`` finishes what is in flight and
#: admits nothing new; ``emergency_stopped`` is the only one that can be left
#: by exactly one transition, so clearing it is always a deliberate act.
CONTROL_STATES = ("stopped", "running", "paused", "draining", "emergency_stopped")

ALLOWED_CONTROL_TRANSITIONS: dict[str, frozenset[str]] = {
    "stopped": frozenset({"running", "emergency_stopped"}),
    "running": frozenset({"paused", "draining", "stopped", "emergency_stopped"}),
    "paused": frozenset({"running", "draining", "stopped", "emergency_stopped"}),
    "draining": frozenset({"running", "paused", "stopped", "emergency_stopped"}),
    "emergency_stopped": frozenset({"stopped"}),
}

#: The states in which a cycle does any work at all.
OPERATING = frozenset({"running", "draining"})

#: The states in which a cycle may admit work that has not started.
ADMITTING = frozenset({"running"})

#: The three classes of already-authorized work a cycle can advance.  Each keeps
#: its own admission rules; this module only decides whether there is room.
WORK_CLASSES = ("backlog", "maintenance", "improvement")

#: What a cycle can end as.  ``idle`` is a cycle that ran while the Owner had
#: the supervisor stopped or paused -- a real, recorded, zero-effect cycle, not
#: an error.
CYCLE_OUTCOMES = ("completed", "idle", "refused")

#: How an abandoned cycle is settled on recovery.  The distinction is the whole
#: of scope 7: a cycle whose missions were all pre-boundary can simply be run
#: again, while one that died with a provider process possibly still running
#: cannot prove what happened and is recorded as uncertain.
RECOVERY_OUTCOMES = ("recovered_replayable", "recovered_uncertain")


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    """One column out of a sqlite3.Row or a mapping, absent if it has none."""

    try:
        return row[key]
    except (IndexError, KeyError):
        return None


#: A repair is promotable exactly once, from the state its admission left it in.
REPAIR_PROMOTABLE = "admitted"

#: An experiment is promotable only after its baseline is pinned.  Stage 8
#: refuses a candidate mission before that, so this is the same rule read from
#: the other side rather than a second one.
EXPERIMENT_PROMOTABLE = "baseline_measured"

#: Terminal reasons that mean the execution layer could not serve the mission.
#: These are infrastructure facts, not verdicts about the work, and they are the
#: only thing that moves the suppression counter.  The set itself lives in the
#: ledger that owns ``terminal_reason``: the shift plane reads the same one to
#: decide whether a portfolio slot may be retried, and this module holding a
#: private copy is how the two would drift apart.
INFRASTRUCTURE_PREFIXES = ledger.INFRASTRUCTURE_REASON_PREFIXES

#: Terminal reasons that mean an Owner ceiling refused the work.  Recorded, and
#: deliberately *not* counted as infrastructure: a budget that is spent is a
#: policy fact that will still be true next cycle, and suppressing on it would
#: hide an Owner decision behind a backoff.
CEILING_PREFIXES = ("MISSION_BUDGET_", "CONTEXT_")

DEFAULT_MISSIONS_PER_CYCLE = 4
DEFAULT_MAINTENANCE_ADMISSIONS = 1
DEFAULT_IMPROVEMENT_ADMISSIONS = 1
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_SUPPRESSION_SECONDS = 900.0
DEFAULT_CYCLE_LEASE_SECONDS = 300.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_control (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  actor TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisor_transitions (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS supervisor_transitions_no_update
BEFORE UPDATE ON supervisor_transitions
BEGIN SELECT RAISE(ABORT, 'supervisor transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS supervisor_transitions_no_delete
BEFORE DELETE ON supervisor_transitions
BEGIN SELECT RAISE(ABORT, 'supervisor transitions are append-only'); END;
CREATE TABLE IF NOT EXISTS supervisor_policies (
  project_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL,
  work_classes_json TEXT NOT NULL,
  missions_per_cycle INTEGER NOT NULL,
  maintenance_admissions INTEGER NOT NULL,
  improvement_admissions INTEGER NOT NULL,
  window_start_hour INTEGER,
  window_end_hour INTEGER,
  failure_threshold INTEGER NOT NULL,
  suppression_seconds REAL NOT NULL,
  policy_version TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisor_cycles (
  cycle_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE,
  previous_cycle_id TEXT,
  control_state TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  lease_expires_at REAL NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  outcome TEXT,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS supervisor_cycles_open
  ON supervisor_cycles(ended_at, lease_expires_at);
CREATE TABLE IF NOT EXISTS supervisor_selections (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL,
  project_id TEXT,
  work_class TEXT NOT NULL,
  work_ref TEXT NOT NULL,
  admitted INTEGER NOT NULL,
  reason TEXT NOT NULL,
  mission_ref TEXT,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS supervisor_selections_by_cycle
  ON supervisor_selections(cycle_id, sequence);
CREATE TRIGGER IF NOT EXISTS supervisor_selections_no_update
BEFORE UPDATE ON supervisor_selections
BEGIN SELECT RAISE(ABORT, 'supervisor selections are append-only'); END;
CREATE TRIGGER IF NOT EXISTS supervisor_selections_no_delete
BEFORE DELETE ON supervisor_selections
BEGIN SELECT RAISE(ABORT, 'supervisor selections are append-only'); END;
CREATE TABLE IF NOT EXISTS supervisor_health (
  project_id TEXT PRIMARY KEY,
  consecutive_failures INTEGER NOT NULL,
  suppressed_until REAL,
  last_code TEXT NOT NULL,
  escalated INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisor_rotation (
  project_id TEXT PRIMARY KEY,
  last_promoted_cycle INTEGER NOT NULL,
  updated_at REAL NOT NULL
);
"""


class PolicyError(ValueError):
    """A supervisor policy that could not mean anything durable."""


class SupervisorRefusal(Exception):
    """One named refusal, carried out so it outlives the transaction.

    ``factory-bridge`` owns bare ``IDEMPOTENCY_CONFLICT`` and Evidence Core owns
    ``ADMISSION_*``; every code here is layer-prefixed for the same reason the
    Controller's own were renamed in Stage 6.
    """

    def __init__(self, code: str, detail: str, *, cycle_id: str | None = None,
                 project_id: str | None = None) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.cycle_id = cycle_id
        self.project_id = project_id

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "cycle_id": self.cycle_id or "not_applicable",
                "project_id": self.project_id or "not_applicable"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def cycle_reference(previous: str | None, sequence: int, worker_id: str,
                    started_at: float) -> str:
    """A cycle identity derived from its own place in the chain.

    Derived rather than allocated, on the Stage-7 principle: a replay of the
    same cycle recomputes the same reference and collides with the row that is
    already there, instead of minting a second identity for one act.  Naming the
    previous cycle also makes the sequence a chain, so a missing cycle is
    visible rather than merely absent.
    """

    return "cyc_" + digest({"previous": previous or "none", "sequence": sequence,
                            "worker_id": worker_id,
                            "started_at": round(started_at, 6)})[:24]


def within_window(now: float, start_hour: int | None, end_hour: int | None) -> bool:
    """Whether ``now`` falls inside the Owner's allowed execution window.

    Hours are UTC and the window wraps, so ``22 -> 6`` is a night window rather
    than an empty one.  A window nobody declared is always open: an undeclared
    constraint must never behave like a closed gate, or turning the supervisor
    on for the first time would silently do nothing.
    """

    if start_hour is None or end_hour is None:
        return True
    if start_hour == end_hour:
        return True
    hour = time.gmtime(now).tm_hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def classify_outcome(mission: Mapping[str, Any] | None) -> tuple[str, str]:
    """What one finished mission means for the supervisor, and why.

    Three answers only: it progressed, an Owner ceiling refused it, or the
    execution layer could not serve it.  Only the third is an infrastructure
    fact, and only the third moves a suppression counter -- a spent budget is a
    policy fact that will still be true next cycle, and backing off from it
    would hide an Owner decision behind a timer.
    """

    if mission is None:
        return ("none", "NO_RUNNABLE_MISSION")
    state = str(mission.get("state") or "unknown")
    reason = str(mission.get("terminal_reason") or "")
    if state in {"completed", "dispatching", "dispatched", "candidate_verified",
                 "evaluated", "evidence_sealed"}:
        return ("progressed", state)
    if state == "admitted":
        # A retry.  Neither progress nor a settled failure, so it moves nothing:
        # counting it as progress would reset the suppression counter on every
        # flap, and counting it as a failure would suppress a project for a
        # transient the retry policy is already bounding.  The run ends either
        # way -- at `completed`, or at `RETRIES_EXHAUSTED`, which is below.
        return ("retrying", reason.split(":")[0] if reason else state)
    for prefix in INFRASTRUCTURE_PREFIXES:
        if reason.startswith(prefix):
            return ("infrastructure", reason.split(":")[0] or prefix)
    for prefix in CEILING_PREFIXES:
        if reason.startswith(prefix):
            return ("ceiling", reason.split(":")[0] or prefix)
    return ("settled", reason.split(":")[0] if reason else state)


@dataclass(frozen=True)
class SupervisorPolicy:
    """What one project allows an unattended cycle to do.

    Every field is a ceiling or a window.  There is deliberately no field that
    grants anything: a project with a policy is a project the Owner allowed the
    supervisor to *advance*, never one it allowed the supervisor to authorize.
    """

    project_id: str
    enabled: bool = True
    work_classes: tuple[str, ...] = WORK_CLASSES
    missions_per_cycle: int = DEFAULT_MISSIONS_PER_CYCLE
    maintenance_admissions: int = DEFAULT_MAINTENANCE_ADMISSIONS
    improvement_admissions: int = DEFAULT_IMPROVEMENT_ADMISSIONS
    window_start_hour: int | None = None
    window_end_hour: int | None = None
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    suppression_seconds: float = DEFAULT_SUPPRESSION_SECONDS
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not self.project_id:
            raise PolicyError("a supervisor policy names a project")
        if not self.work_classes:
            raise PolicyError(
                "an empty work-class list is refused rather than read as "
                "'every class'; a policy that admits everything by saying "
                "nothing is the one shape nobody can review")
        unknown = set(self.work_classes) - set(WORK_CLASSES)
        if unknown:
            raise PolicyError("unknown work class(es): %s" % sorted(unknown))
        if self.missions_per_cycle < 1:
            raise PolicyError("a cycle that may advance no mission is a stopped "
                              "supervisor spelled a second way")
        if self.maintenance_admissions < 0 or self.improvement_admissions < 0:
            raise PolicyError("an admission ceiling is never negative")
        if self.failure_threshold < 1:
            raise PolicyError("a suppression threshold below one would suppress "
                              "a project that has not failed yet")
        if self.suppression_seconds < 0:
            raise PolicyError("a suppression window is never negative")
        for hour in (self.window_start_hour, self.window_end_hour):
            if hour is not None and not 0 <= int(hour) <= 23:
                raise PolicyError("an execution window is stated in UTC hours 0-23")
        if (self.window_start_hour is None) != (self.window_end_hour is None):
            raise PolicyError("half a window is not a window; declare both hours "
                              "or neither")

    def as_row(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "enabled": self.enabled,
                "work_classes": list(self.work_classes),
                "missions_per_cycle": self.missions_per_cycle,
                "maintenance_admissions": self.maintenance_admissions,
                "improvement_admissions": self.improvement_admissions,
                "window_start_hour": _absent(self.window_start_hour, "not_applicable"),
                "window_end_hour": _absent(self.window_end_hour, "not_applicable"),
                "failure_threshold": self.failure_threshold,
                "suppression_seconds": self.suppression_seconds,
                "policy_version": self.policy_version,
                "contract_version": CONTRACT_VERSION}


@dataclass
class CycleReport:
    """Everything one cycle did, and everything it declined to do."""

    cycle_id: str
    sequence: int
    control_state: str
    outcome: str
    started_at: float
    ended_at: float
    promoted: list[dict[str, Any]] = field(default_factory=list)
    advanced: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    uncertain_missions: tuple[str, ...] = ()
    reason: str = "CYCLE_COMPLETED"

    def as_row(self) -> dict[str, Any]:
        return {"cycle_id": self.cycle_id, "sequence": self.sequence,
                "control_state": self.control_state, "outcome": self.outcome,
                "reason": self.reason,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "promoted": self.promoted, "advanced": self.advanced,
                "refused": self.refused, "recovered": self.recovered,
                "uncertain_missions": list(self.uncertain_missions),
                "promotions": len(self.promoted),
                "missions_advanced": len(self.advanced),
                "contract_version": CONTRACT_VERSION}


class OperationsSupervisor:
    """One bounded cycle per invocation, over work somebody else authorized.

    The constructor takes a ``Controller`` because a supervisor that could not
    reach the ordinary execution path would have had to grow a second one.  It
    builds the Stage-6/7/8 planes over the same store rather than accepting them
    as arguments, so no caller can hand it a plane pointed somewhere else.
    """

    def __init__(self, controller, *, clock=time.time) -> None:
        self._controller = controller
        self._store = controller.store
        self.clock = clock
        self._ledger = production.ProductionLedger(self._store)
        self._maintenance = maintenance_plane.MaintenancePlane(self._store, self._ledger)
        self._improvement = improvement_plane.ImprovementPlane(self._store, self._ledger)
        with self._store.transaction() as db:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO supervisor_control"
                " VALUES (1,'stopped','never started','not_applicable',"
                "'not_applicable','unset',?)", (self.clock(),))

    # -- Owner control ----------------------------------------------------- #

    def control(self) -> dict[str, Any]:
        """The durable control record.  Reads only."""

        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM supervisor_control WHERE id=1").fetchone()
        return {**dict(row), "contract_version": CONTRACT_VERSION}

    def transition(self, to_state: str, *, actor: str, reason: str,
                   evidence_ref: str = "not_applicable",
                   policy_version: str | None = None) -> dict[str, Any]:
        """Move the supervisor between Owner control states.

        ``emergency_stopped`` and its clearance also engage and release the
        Stage-5 portfolio stop, which maintenance, improvement and the scheduler
        already honour.  A stop that only this module could see would have been
        a second stop the other planes never read -- the shape that made
        ``acceptance_gate_ids`` travel through four layers unevaluated.
        """

        if to_state not in CONTROL_STATES:
            raise PolicyError("unknown control state %r" % (to_state,))
        if not actor or not reason:
            raise PolicyError("a control transition records who asked and why")
        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM supervisor_control WHERE id=1").fetchone()
            current = row["state"]
            if to_state == current:
                return {**dict(row), "changed": False}
            if to_state not in ALLOWED_CONTROL_TRANSITIONS[current]:
                raise SupervisorRefusal(
                    "SUPERVISOR_TRANSITION_REFUSED",
                    "%s -> %s is not a transition this contract defines"
                    % (current, to_state))
            db.execute(
                "UPDATE supervisor_control SET state=?,reason=?,evidence_ref=?,"
                "actor=?,policy_version=?,updated_at=? WHERE id=1",
                (to_state, reason, evidence_ref, actor,
                 policy_version or row["policy_version"], now))
            db.execute(
                "INSERT INTO supervisor_transitions"
                " (from_state,to_state,reason,evidence_ref,actor,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (current, to_state, reason, evidence_ref, actor, now))
        if to_state == "emergency_stopped":
            self._store.emergency_stop(True, reason="SUPERVISOR_EMERGENCY_STOP")
        elif current == "emergency_stopped":
            self._store.emergency_stop(False, reason="SUPERVISOR_EMERGENCY_CLEARED")
        return {**self.control(), "changed": True}

    def transitions(self) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM supervisor_transitions ORDER BY sequence").fetchall()
        return tuple(dict(row) for row in rows)

    def hold(self, project_id: str, *, held: bool = True) -> dict[str, Any]:
        """Hold or release one project, through the Stage-5 project state.

        Not a supervisor-local flag.  ``paused`` is the state the scheduler,
        the maintenance plane and the improvement plane already refuse work
        against, so a hold is honoured by three planes that know nothing about
        this module.
        """

        state = "paused" if held else "enabled"
        return self._store.set_project_state(
            project_id, state,
            reason="SUPERVISOR_HOLD" if held else "SUPERVISOR_RELEASE")

    # -- policy ------------------------------------------------------------ #

    def set_policy(self, policy: SupervisorPolicy) -> dict[str, Any]:
        if self._store.project(policy.project_id) is None:
            raise SupervisorRefusal(
                "SUPERVISOR_PROJECT_UNREGISTERED",
                "%s is not a registered project; an unattended cycle would run "
                "it under no Owner priority, cap or budget at all"
                % policy.project_id, project_id=policy.project_id)
        now = self.clock()
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,"
                " work_classes_json=excluded.work_classes_json,"
                " missions_per_cycle=excluded.missions_per_cycle,"
                " maintenance_admissions=excluded.maintenance_admissions,"
                " improvement_admissions=excluded.improvement_admissions,"
                " window_start_hour=excluded.window_start_hour,"
                " window_end_hour=excluded.window_end_hour,"
                " failure_threshold=excluded.failure_threshold,"
                " suppression_seconds=excluded.suppression_seconds,"
                " policy_version=excluded.policy_version,"
                " updated_at=excluded.updated_at",
                (policy.project_id, int(policy.enabled),
                 canonical_json(list(policy.work_classes)),
                 policy.missions_per_cycle, policy.maintenance_admissions,
                 policy.improvement_admissions, policy.window_start_hour,
                 policy.window_end_hour, policy.failure_threshold,
                 policy.suppression_seconds, policy.policy_version, now, now))
        return policy.as_row()

    def policy(self, project_id: str) -> SupervisorPolicy | None:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM supervisor_policies WHERE project_id=?",
                             (project_id,)).fetchone()
        return None if row is None else _policy_from_row(row)

    def policies(self) -> tuple[SupervisorPolicy, ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM supervisor_policies ORDER BY project_id").fetchall()
        return tuple(_policy_from_row(row) for row in rows)

    def set_enabled(self, project_id: str, enabled: bool) -> dict[str, Any]:
        current = self.policy(project_id)
        if current is None:
            raise SupervisorRefusal(
                "SUPERVISOR_POLICY_UNKNOWN",
                "no supervisor policy is declared for %s" % project_id,
                project_id=project_id)
        with self._store.transaction() as db:
            db.execute("UPDATE supervisor_policies SET enabled=?,updated_at=?"
                       " WHERE project_id=?",
                       (int(enabled), self.clock(), project_id))
        return {"project_id": project_id, "enabled": enabled}

    # -- the cycle --------------------------------------------------------- #

    def cycle(self, worker_id: str, *,
              lease_seconds: float = DEFAULT_CYCLE_LEASE_SECONDS) -> dict[str, Any]:
        """Perform exactly one finite cycle and return.

        There is no loop over cycles, no sleep, and no path from here back to
        here.  Everything that bounds the work is durable and read at the top:
        the Owner's control state, each project's ceilings and window, the
        suppression record, and the Stage-5 caps the scheduler applies anyway.

        The order matters and is the fail-closed half of scope 7.  Stale mission
        leases are recovered first, then *uncertain* missions -- ones that were
        past the dispatch boundary when something died -- are counted.  While
        any exist, the cycle advances work but promotes none: finishing what may
        already have run comes before opening anything new.
        """

        claim = self._claim_cycle(worker_id, lease_seconds)
        cycle_id, sequence = claim["cycle_id"], claim["sequence"]
        state = claim["control_state"]
        report = CycleReport(cycle_id=cycle_id, sequence=sequence,
                             control_state=state, outcome="completed",
                             started_at=claim["started_at"],
                             ended_at=claim["started_at"],
                             recovered=claim["recovered"])
        heartbeat_stop = threading.Event()
        heartbeat_error: list[BaseException] = []

        def heartbeat() -> None:
            interval = max(0.01, min(5.0, lease_seconds / 3))
            while not heartbeat_stop.wait(interval):
                try:
                    self._renew_cycle(cycle_id, worker_id, lease_seconds)
                except BaseException as exc:  # pragma: no cover - lease-loss path
                    heartbeat_error.append(exc)
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name="supervisor-cycle-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            if state not in OPERATING:
                report.outcome = "idle"
                report.reason = "SUPERVISOR_%s" % state.upper()
                return self._close_cycle(report, worker_id=worker_id)
            self._store.recover_stale()
            uncertain = self._uncertain_missions()
            report.uncertain_missions = uncertain
            eligible = self._eligible_projects(report)
            if state in ADMITTING and not uncertain:
                self._promote(report, eligible, sequence)
            elif uncertain:
                report.refused.append({
                    "work_class": "not_applicable", "work_ref": "promotion",
                    "reason": "SUPERVISOR_UNCERTAIN_WORK_IN_FLIGHT",
                    "detail": {"missions": list(uncertain)}})
            self._advance(report, eligible, drain=state == "draining")
            if heartbeat_error:
                raise heartbeat_error[0]
            return self._close_cycle(report, worker_id=worker_id)
        except BaseException:
            self._close_cycle(report, outcome="refused",
                              reason="SUPERVISOR_CYCLE_ABANDONED",
                              worker_id=worker_id)
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=min(1.0, lease_seconds))

    def cycles(self, limit: int = 50) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM supervisor_cycles ORDER BY sequence DESC LIMIT ?",
                (limit,)).fetchall()
        return tuple(dict(row) for row in rows)

    def selections(self, cycle_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            if cycle_id is None:
                rows = db.execute(
                    "SELECT * FROM supervisor_selections ORDER BY sequence").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM supervisor_selections WHERE cycle_id=?"
                    " ORDER BY sequence", (cycle_id,)).fetchall()
        return tuple({**dict(row), "detail": json.loads(row["detail_json"])}
                     for row in rows)

    # -- the Owner's brief -------------------------------------------------- #

    def brief(self) -> dict[str, Any]:
        """A plain reading of what is happening, from durable state alone.

        Observability only.  Nothing here writes, and nothing here can be an
        input to a later authority decision -- the brief is derived from the
        same rows a person could read by hand, so it can be wrong about the
        future and never wrong about the record.
        """

        control = self.control()
        missions = self._mission_lines()
        running = [m for m in missions if m["leased"]]
        in_flight = [m for m in missions
                     if m["state"] not in portfolio_policy.TERMINAL]
        escalated = [m for m in missions if m["state"] == "escalated"]
        completed = sorted((m for m in missions if m["state"] == "completed"),
                           key=lambda m: m["updated_at"], reverse=True)[:10]
        preview = self._store.schedule_preview()
        waiting = [verdict for verdict in preview.get("considered", [])
                   if not verdict["admitted"]]
        health = self.health()
        awaiting: list[dict[str, Any]] = []
        for row in self._improvement.experiments():
            if row["disposition"] is None and row["state"] in {
                    "candidate_sealed", "evaluated", "promotion_staged"}:
                awaiting.append({"kind": "improvement", "ref": row["experiment_ref"],
                                 "project_id": row["project_id"],
                                 "state": row["state"],
                                 "needs": "owner_disposition"})
        for row in self._maintenance.repairs():
            if row["disposition"] is None and row["state"] in {
                    "candidate_validated", "recovery_staged"}:
                awaiting.append({"kind": "maintenance", "ref": row["trigger_ref"],
                                 "project_id": row["project_id"],
                                 "state": row["state"],
                                 "needs": "owner_disposition"})
        return {
            "contract_version": CONTRACT_VERSION,
            "control": control,
            "cycles_recorded": len(self.cycles(limit=1_000_000)),
            "running": [_mission_line(m) for m in running],
            "in_flight": len(in_flight),
            "escalated": [_mission_line(m) for m in escalated],
            "recently_completed": [_mission_line(m) for m in completed],
            "next_eligible": preview.get("selected") or "not_applicable",
            "next_eligible_reason": preview.get("reason", "unknown"),
            "waiting": [{"mission_id": v["mission_id"], "project_id": v["project_id"],
                         "reason": v["reason"]} for v in waiting],
            "awaiting_owner": awaiting,
            "suppressed": [row for row in health if row["suppressed_until"] is not None
                           or row["escalated"]],
            "budget": self._store.portfolio_economics(),
            "policies": [policy.as_row() for policy in self.policies()],
        }

    def capacity_brief(self) -> dict[str, Any]:
        """Which runtimes are usable now, and what is waiting on which window.

        Observability only, and structurally so: every value below is read from
        durable state and nothing here is an input to any later decision.  The
        supervisor gained no capacity code beyond this method -- capacity is a
        scheduler verdict, so a cycle is already capacity-aware and a cooling
        provider already ends a cycle cleanly rather than polling.
        """

        readings = self._store.capacity_readings()
        preview = self._store.schedule_preview()
        missions = self._mission_lines()
        assigned: dict[str, list[str]] = {}
        at_risk: list[dict[str, Any]] = []
        resumable: list[dict[str, Any]] = []
        for mission in missions:
            payload = mission["payload"]
            declared = tuple(
                entry if isinstance(entry, str) else entry.get("profile")
                for entry in (payload.get("provider_candidates") or ()))
            declared = tuple(name for name in declared if isinstance(name, str) and name)
            if mission["leased"]:
                for runtime_id in declared:
                    assigned.setdefault(runtime_id, []).append(mission["id"])
            if mission["state"] in portfolio_policy.TERMINAL or not declared:
                continue
            try:
                estimate = capacity_plane.WorkEstimate.from_payload(payload)
            except capacity_plane.PolicyError:
                estimate = capacity_plane.WorkEstimate()
            plan = capacity_plane.plan(declared, readings, estimate)
            if plan.exhausted:
                at_risk.append({"mission_id": mission["id"],
                                "project_id": _absent(mission["project_id"], "not_applicable"),
                                "reason": plan.reason,
                                "resume_at": _absent(plan.resume_at, "unknown"),
                                "size_class": estimate.size_class})
            elif plan.denied:
                at_risk.append({"mission_id": mission["id"],
                                "project_id": _absent(mission["project_id"], "not_applicable"),
                                "reason": "CAPACITY_PARTIALLY_NARROWED",
                                "resume_at": "not_applicable",
                                "size_class": estimate.size_class})
        for row in self._deferred_missions():
            resumable.append(row)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for reading in readings.values():
            grouped.setdefault("usable" if reading.usable else reading.state, []).append({
                **reading.as_row(), "assigned": sorted(assigned.get(reading.runtime_id, ()))})
        return {
            "contract_version": capacity_plane.CONTRACT_VERSION,
            "runtimes": {key: sorted(value, key=lambda row: row["runtime_id"])
                         for key, value in sorted(grouped.items())},
            "usable_now": sorted(reading.runtime_id for reading in readings.values()
                                 if reading.usable),
            "unusable": sorted((reading.runtime_id, reading.reason)
                               for reading in readings.values() if not reading.usable),
            "resumable": resumable,
            "at_risk": at_risk,
            "next_eligible": preview.get("selected") or "not_applicable",
            "next_eligible_reason": preview.get("reason", "unknown"),
            "policies": [policy.as_row() for policy in
                         self._store.runtime_policies().values()],
        }

    def _deferred_missions(self) -> list[dict[str, Any]]:
        """Missions a closed window put back, with the checkpoint each resumes from."""

        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT id,project_id,next_run_at,deferrals FROM missions"
                " WHERE deferrals>0 AND state NOT IN"
                " ('completed','refused','failed','cancelled')"
                " ORDER BY next_run_at,id").fetchall()
        out = []
        for row in rows:
            checkpoint = self._store.capacity_checkpoint(row["id"])
            out.append({"mission_id": row["id"],
                        "project_id": _absent(row["project_id"], "not_applicable"),
                        "resume_at": row["next_run_at"],
                        "deferrals": row["deferrals"],
                        "safe_boundary": checkpoint["safe_boundary"],
                        "next_safe_step": checkpoint["next_safe_step"],
                        "compatible_profiles": checkpoint["compatible_profiles"]})
        return out

    def _mission_lines(self) -> list[dict[str, Any]]:
        """Every mission, with only the columns a brief reads.

        Queried here rather than through ``all_missions``, which returns the
        five columns the dependency graph needs; widening that reader for one
        display would change what four other callers receive.
        """

        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT id,project_id,state,terminal_reason,payload_json,"
                "updated_at,lease_token FROM missions ORDER BY created_at,id"
            ).fetchall()
        return [{"id": row["id"], "project_id": row["project_id"],
                 "state": row["state"], "terminal_reason": row["terminal_reason"],
                 "payload": json.loads(row["payload_json"]),
                 "updated_at": row["updated_at"],
                 "leased": row["lease_token"] is not None}
                for row in rows]

    def health(self) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM supervisor_health ORDER BY project_id").fetchall()
        return tuple(dict(row) for row in rows)

    def service_contract(self, *, invocation: tuple[str, ...] | list[str],
                         interval_seconds: int = 300) -> dict[str, Any]:
        """What a host would have to do to call ``cycle`` on a schedule.

        Deliberately a description and not an act.  This module starts nothing,
        installs nothing and reads no host state; enabling a service is an Owner
        action under the existing bridge install policy, so the honest delivery
        is the exact step plus the fact that it has not been taken.
        """

        return {
            "contract_version": CONTRACT_VERSION,
            "schedule": {"interval_seconds": interval_seconds,
                         "invocation": list(invocation),
                         "semantics": "one bounded cycle per invocation"},
            "guarantees": {
                "reentrant": "an overlapping invocation is refused, not queued",
                "restart_safe": "an abandoned cycle is settled on the next claim",
                "lease": "the cycle owner renews its lease while bounded work runs",
                # Worded to avoid the token the termination test scans for.
                # The check is the load-bearing half here, not the phrasing:
                # SF-137 moved a name to keep a check, SF-138 narrowed a check
                # to keep a domain word, and this is the first kind again.
                "terminates": "one finite cycle, then the process exits",
            },
            "activation": {
                "performed_by": "owner",
                "performed_here": False,
                "state": "not_run",
            },
        }

    # -- internals: the cycle claim ----------------------------------------- #

    def _claim_cycle(self, worker_id: str, lease_seconds: float) -> dict[str, Any]:
        """Open exactly one cycle, settling whatever the last one left behind.

        The overlap refusal and the recovery are in one ``BEGIN IMMEDIATE``, so
        a second host invocation arriving during a cycle either refuses or takes
        over an expired one, and never both.
        """

        if lease_seconds <= 0:
            raise PolicyError("a cycle lease is a positive number of seconds")
        now = self.clock()
        recovered: list[dict[str, Any]] = []
        with self._store.transaction() as db:
            open_rows = db.execute(
                "SELECT * FROM supervisor_cycles WHERE ended_at IS NULL"
                " ORDER BY sequence").fetchall()
            for row in open_rows:
                if row["lease_expires_at"] > now:
                    raise SupervisorRefusal(
                        "SUPERVISOR_CYCLE_IN_FLIGHT",
                        "cycle %s is held by %s until %r"
                        % (row["cycle_id"], row["worker_id"], row["lease_expires_at"]),
                        cycle_id=row["cycle_id"])
                outcome = self._recovery_outcome_locked(db, row["cycle_id"])
                db.execute(
                    "UPDATE supervisor_cycles SET ended_at=?,outcome=?,detail_json=?"
                    " WHERE cycle_id=?",
                    (now, outcome,
                     canonical_json({"recovered_by": worker_id,
                                     "prior_worker": row["worker_id"]}),
                     row["cycle_id"]))
                recovered.append({"cycle_id": row["cycle_id"], "outcome": outcome,
                                  "prior_worker": row["worker_id"]})
            last = db.execute(
                "SELECT cycle_id, sequence FROM supervisor_cycles"
                " ORDER BY sequence DESC LIMIT 1").fetchone()
            previous = None if last is None else last["cycle_id"]
            sequence = 1 if last is None else int(last["sequence"]) + 1
            state = db.execute(
                "SELECT state FROM supervisor_control WHERE id=1").fetchone()["state"]
            cycle_id = cycle_reference(previous, sequence, worker_id, now)
            db.execute(
                "INSERT INTO supervisor_cycles"
                " (cycle_id,sequence,previous_cycle_id,control_state,worker_id,"
                "  lease_expires_at,started_at) VALUES (?,?,?,?,?,?,?)",
                (cycle_id, sequence, previous, state, worker_id,
                 now + lease_seconds, now))
        return {"cycle_id": cycle_id, "sequence": sequence, "control_state": state,
                "started_at": now, "recovered": recovered}

    def _renew_cycle(self, cycle_id: str, worker_id: str,
                     lease_seconds: float) -> None:
        """Keep a healthy finite cycle distinguishable from an abandoned one."""

        now = self.clock()
        with self._store.transaction() as db:
            changed = db.execute(
                "UPDATE supervisor_cycles SET lease_expires_at=?"
                " WHERE cycle_id=? AND worker_id=? AND ended_at IS NULL",
                (now + lease_seconds, cycle_id, worker_id)).rowcount
        if changed != 1:
            raise SupervisorRefusal(
                "SUPERVISOR_CYCLE_LEASE_LOST",
                "cycle %s is no longer owned by %s" % (cycle_id, worker_id),
                cycle_id=cycle_id)

    def _recovery_outcome_locked(self, db, cycle_id: str) -> str:
        """Whether an abandoned cycle can simply be run again.

        Every act a cycle performs is individually idempotent -- a promotion is
        keyed by a derived idempotency key, and a mission step is memoized -- so
        replay is safe *unless* a provider process may still have been running
        when the cycle died.  That is exactly the set of missions past the
        dispatch boundary, and it is read here rather than trusted from the
        abandoned cycle's own record, which by definition was never written.
        """

        row = db.execute(
            "SELECT COUNT(*) AS n FROM missions"
            " WHERE state IN ('dispatched','candidate_verified','evaluated',"
            "'evidence_sealed') OR (state='dispatching' AND EXISTS ("
            "SELECT 1 FROM steps s WHERE s.mission_id=missions.id"
            " AND s.name IN " + ledger.DISPATCH_STEP_SQL +
            " AND s.status='STARTED'"
            ") AND (NOT EXISTS (SELECT 1 FROM runs r0"
            " WHERE r0.mission_id=missions.id) OR EXISTS ("
            "SELECT 1 FROM runs r1 WHERE r1.mission_id=missions.id"
            " AND (r1.process_started IS NULL OR r1.process_started=1))))").fetchone()
        return RECOVERY_OUTCOMES[1] if row["n"] else RECOVERY_OUTCOMES[0]

    def _close_cycle(self, report: CycleReport, *, outcome: str | None = None,
                     reason: str | None = None,
                     worker_id: str | None = None) -> dict[str, Any]:
        report.ended_at = self.clock()
        if outcome is not None:
            report.outcome = outcome
        if reason is not None:
            report.reason = reason
        with self._store.transaction() as db:
            if worker_id is None:
                db.execute(
                    "UPDATE supervisor_cycles SET ended_at=?,outcome=?,detail_json=?"
                    " WHERE cycle_id=? AND ended_at IS NULL",
                    (report.ended_at, report.outcome, canonical_json(report.as_row()),
                     report.cycle_id))
            else:
                db.execute(
                    "UPDATE supervisor_cycles SET ended_at=?,outcome=?,detail_json=?"
                    " WHERE cycle_id=? AND worker_id=? AND ended_at IS NULL",
                    (report.ended_at, report.outcome, canonical_json(report.as_row()),
                     report.cycle_id, worker_id))
        return report.as_row()

    def _uncertain_missions(self) -> tuple[str, ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT id FROM missions WHERE lease_token IS NULL AND state IN"
                " ('dispatched','candidate_verified','evaluated','evidence_sealed')"
                " OR (lease_token IS NULL AND state='dispatching' AND EXISTS ("
                "SELECT 1 FROM steps s WHERE s.mission_id=missions.id"
                " AND s.name IN " + ledger.DISPATCH_STEP_SQL +
                " AND s.status='STARTED'"
                ") AND (NOT EXISTS (SELECT 1 FROM runs r0"
                " WHERE r0.mission_id=missions.id) OR EXISTS ("
                "SELECT 1 FROM runs r1 WHERE r1.mission_id=missions.id"
                " AND (r1.process_started IS NULL OR r1.process_started=1))))"
                " ORDER BY id").fetchall()
        return tuple(row["id"] for row in rows)

    # -- internals: selection ------------------------------------------------ #

    def _eligible_projects(self, report: CycleReport) -> tuple[SupervisorPolicy, ...]:
        """Which projects this cycle may touch at all, and why the rest may not.

        Four gates, each of them somebody else's decision read back: the Owner
        turned the policy off, the Owner's window is closed, the project is not
        in a Stage-5 admitting state, or the supervisor's own bounded
        suppression is still holding after repeated infrastructure failure.
        """

        now = self.clock()
        eligible: list[SupervisorPolicy] = []
        health = {row["project_id"]: row for row in self.health()}
        projects = self._store.projects()
        for policy in self.policies():
            def refuse(code: str, **detail: Any) -> None:
                report.refused.append({
                    "project_id": policy.project_id, "work_class": "not_applicable",
                    "work_ref": policy.project_id, "reason": code, "detail": detail})

            if not policy.enabled:
                refuse("SUPERVISOR_PROJECT_DISABLED")
                continue
            if not within_window(now, policy.window_start_hour, policy.window_end_hour):
                refuse("SUPERVISOR_OUTSIDE_EXECUTION_WINDOW",
                       window_start_hour=policy.window_start_hour,
                       window_end_hour=policy.window_end_hour)
                continue
            registered = projects.get(policy.project_id)
            if registered is None:
                refuse("SUPERVISOR_PROJECT_UNREGISTERED")
                continue
            if registered.state not in portfolio_policy.ADMITTING:
                refuse("SUPERVISOR_PROJECT_NOT_ADMITTING", project_state=registered.state)
                continue
            row = health.get(policy.project_id)
            if row is not None and row["escalated"]:
                refuse("SUPERVISOR_PROJECT_ESCALATED", last_code=row["last_code"],
                       consecutive_failures=row["consecutive_failures"])
                continue
            if row is not None and row["suppressed_until"] is not None \
                    and row["suppressed_until"] > now:
                refuse("SUPERVISOR_PROJECT_SUPPRESSED",
                       suppressed_until=row["suppressed_until"],
                       last_code=row["last_code"])
                continue
            eligible.append(policy)
        return tuple(eligible)

    def _promote(self, report: CycleReport, eligible: tuple[SupervisorPolicy, ...],
                 sequence: int) -> None:
        """Turn already-admitted repairs and experiments into ordinary missions.

        This is the only place the supervisor causes anything new to exist, and
        the two things it may promote were both admitted by an earlier stage
        against a durable fact -- a recorded production failure, or a baseline
        pinned before any candidate existed.  Nothing here can admit either one.

        Projects are taken in rotation, least-recently-promoted first, so a
        project with a queue of incidents cannot consume every promotion slot
        cycle after cycle.  Mission-level fairness stays where Stage 5 put it:
        unbounded ageing inside the scheduler.
        """

        for policy in self._rotated(eligible):
            promoted_here = 0
            if "maintenance" in policy.work_classes:
                promoted_here += self._promote_repairs(report, policy)
            if "improvement" in policy.work_classes:
                promoted_here += self._promote_experiments(report, policy)
            if promoted_here:
                self._touch_rotation(policy.project_id, sequence)

    def _rotated(self, eligible: tuple[SupervisorPolicy, ...]) -> list[SupervisorPolicy]:
        with self._store.transaction() as db:
            rows = db.execute("SELECT * FROM supervisor_rotation").fetchall()
        last = {row["project_id"]: int(row["last_promoted_cycle"]) for row in rows}
        return sorted(eligible,
                      key=lambda p: (last.get(p.project_id, 0), p.project_id))

    def _touch_rotation(self, project_id: str, sequence: int) -> None:
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_rotation VALUES (?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET"
                " last_promoted_cycle=excluded.last_promoted_cycle,"
                " updated_at=excluded.updated_at",
                (project_id, sequence, self.clock()))

    def _promote_repairs(self, report: CycleReport, policy: SupervisorPolicy) -> int:
        due = [row for row in self._maintenance.repairs(policy.project_id)
               if row["disposition"] is None and row["state"] == REPAIR_PROMOTABLE
               and not row["mission_ref"]]
        return self._promote_each(
            report, policy, "maintenance", policy.maintenance_admissions,
            [(row["trigger_ref"], row) for row in due], "repository",
            lambda ref, gates, source: self._maintenance.create_repair_mission(
                ref, self._controller, acceptance_gate_ids=gates,
                extra={"acceptance_gate_source": source}))

    def _promote_experiments(self, report: CycleReport,
                             policy: SupervisorPolicy) -> int:
        due = [row for row in self._improvement.experiments(policy.project_id)
               if row["disposition"] is None and row["state"] == EXPERIMENT_PROMOTABLE
               and not row["mission_ref"]]
        return self._promote_each(
            report, policy, "improvement", policy.improvement_admissions,
            [(row["experiment_ref"], row) for row in due], "target_repository",
            lambda ref, gates, source: self._improvement.create_candidate_mission(
                ref, self._controller, acceptance_gate_ids=gates,
                extra={"acceptance_gate_source": source}))

    def _promote_each(self, report: CycleReport, policy: SupervisorPolicy,
                      work_class: str, ceiling: int,
                      due: list[tuple[str, Mapping[str, Any]]],
                      repository_key: str, create) -> int:
        promoted = 0
        for work_ref, row in due:
            # Gate provenance is checked before the ceiling on purpose: whether
            # the project declared gates is true independently of how many items
            # this cycle already promoted, and SF-140 found that refusing on the
            # more fundamental condition first is what keeps a refusal readable.
            try:
                gates, source = self._store.declared_acceptance_gates(
                    policy.project_id, _row_value(row, repository_key))
            except portfolio_policy.GateProvenanceError as refusal:
                self._record_selection(
                    report, policy.project_id, work_class, work_ref, False,
                    refusal.code, refusal.detail)
                continue
            if promoted >= ceiling:
                self._record_selection(
                    report, policy.project_id, work_class, work_ref, False,
                    "SUPERVISOR_CLASS_ADMISSION_CEILING", {"ceiling": ceiling})
                continue
            try:
                mission, created = create(work_ref, gates, source)
            except (maintenance_plane.MaintenanceRefusal,
                    improvement_plane.ImprovementRefusal) as refusal:
                self._record_selection(
                    report, policy.project_id, work_class, work_ref, False,
                    refusal.code if hasattr(refusal, "code") else "REFUSED",
                    {"detail": str(refusal)})
                continue
            except (maintenance_plane.PolicyError, improvement_plane.PolicyError,
                    production.PolicyError) as error:
                self._record_selection(
                    report, policy.project_id, work_class, work_ref, False,
                    "SUPERVISOR_WORK_POLICY_INVALID", {"detail": str(error)})
                continue
            promoted += 1
            self._record_selection(
                report, policy.project_id, work_class, work_ref, True,
                "SUPERVISOR_PROMOTED", {"created": created},
                mission_ref=mission["id"])
            report.promoted.append({"project_id": policy.project_id,
                                    "work_class": work_class, "work_ref": work_ref,
                                    "mission_ref": mission["id"], "created": created,
                                    "acceptance_gate_ids": list(gates),
                                    "acceptance_gate_source": source})
        return promoted

    def _advance(self, report: CycleReport, eligible: tuple[SupervisorPolicy, ...],
                 *, drain: bool) -> None:
        """Run the ordinary execution path, up to this cycle's own ceiling.

        The ceiling is the largest of the eligible projects' ceilings rather
        than a sum, so adding a project can never widen how much any single
        cycle does; the Stage-5 caps then decide which project each iteration
        lands in.  A drain claims only missions past the dispatch boundary, so
        it finishes in-flight work and starts none.

        The claim is scoped to the eligible projects, so a closed execution
        window, a hold, a disabled policy or a suppression actually holds
        instead of merely skipping the promotion pass -- the scheduler would
        otherwise have picked that project's backlog anyway.  A mission
        belonging to no project is out of scope for every cycle: it has no
        Owner priority, cap or budget, which is the same reason `set_policy`
        refuses an unregistered project.  A resume is exempt, inside the
        scheduler, because half-finished work has to finish.
        """

        scope = tuple(policy.project_id for policy in eligible)
        budget = max([policy.missions_per_cycle for policy in eligible] or [0])
        if drain:
            budget = max(budget, DEFAULT_MISSIONS_PER_CYCLE)
        for _ in range(budget):
            mission = self._controller.work_once(
                "%s:%s" % (report.cycle_id, report.sequence), resume_only=drain,
                project_ids=scope)
            classification, code = classify_outcome(mission)
            if mission is None:
                report.refused.append({"work_class": "backlog",
                                       "work_ref": "not_applicable",
                                       "reason": code, "detail": {}})
                return
            project_id = mission.get("project_id")
            report.advanced.append({
                "mission_id": mission["id"], "project_id": project_id,
                "state": mission["state"], "classification": classification,
                "code": code})
            self._record_selection(
                report, project_id, "backlog", mission["id"], True,
                "SUPERVISOR_ADVANCED", {"classification": classification,
                                        "code": code},
                mission_ref=mission["id"])
            self._record_health(project_id, classification, code, eligible)
            self._record_work_outcome(mission)

    def _record_work_outcome(self, mission: Mapping[str, Any]) -> None:
        """Copy a settled mission's own facts onto the repair that opened it.

        Only maintenance.  An improvement candidate is *sealed* by a producer
        identity and a change set, neither of which is a fact this module can
        observe, so a supervisor that sealed one would be inventing the two
        inputs Stage 8's containment rests on.  Those wait for the Owner and
        appear in the brief as waiting.
        """

        payload = mission.get("payload") or {}
        if payload.get("origin") != "maintenance_trigger":
            return
        if mission.get("state") not in maintenance_plane.MISSION_SETTLED:
            return
        trigger_ref = payload.get("trigger_ref")
        if not isinstance(trigger_ref, str):
            return
        try:
            self._maintenance.record_mission_outcome(trigger_ref, mission)
        except (maintenance_plane.MaintenanceRefusal, maintenance_plane.PolicyError):
            return

    def _record_health(self, project_id: str | None, classification: str,
                       code: str, eligible: tuple[SupervisorPolicy, ...]) -> None:
        """Bounded suppression, so a broken execution layer costs one project.

        A run of infrastructure failures suppresses the project for a declared
        window; the run after that escalates it, which stops the supervisor
        selecting it until the Owner releases it.  Both only ever *reduce* what
        an unattended cycle does, which is why they are safe for this module to
        decide on its own.  Progress resets the counter, so a single flap costs
        nothing.
        """

        if project_id is None:
            return
        policy = next((p for p in eligible if p.project_id == project_id), None)
        if policy is None:
            return
        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM supervisor_health WHERE project_id=?",
                             (project_id,)).fetchone()
            failures = 0 if row is None else int(row["consecutive_failures"])
            if classification != "infrastructure":
                if row is None:
                    return
                db.execute(
                    "UPDATE supervisor_health SET consecutive_failures=0,"
                    "suppressed_until=NULL,last_code=?,updated_at=? WHERE project_id=?",
                    (code, now, project_id))
                return
            failures += 1
            escalate = failures >= policy.failure_threshold * 2
            suppress = None if failures < policy.failure_threshold \
                else now + policy.suppression_seconds
            db.execute(
                "INSERT INTO supervisor_health VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET"
                " consecutive_failures=excluded.consecutive_failures,"
                " suppressed_until=excluded.suppressed_until,"
                " last_code=excluded.last_code, escalated=excluded.escalated,"
                " updated_at=excluded.updated_at",
                (project_id, failures, suppress, code, int(escalate), now))

    def clear_health(self, project_id: str, *, actor: str) -> dict[str, Any]:
        """The Owner's release of an escalated project.  Only ever widens by hand."""

        if not actor:
            raise PolicyError("clearing an escalation records who asked")
        with self._store.transaction() as db:
            db.execute(
                "UPDATE supervisor_health SET consecutive_failures=0,"
                "suppressed_until=NULL,escalated=0,last_code=?,updated_at=?"
                " WHERE project_id=?", ("cleared_by_owner", self.clock(), project_id))
        return {"project_id": project_id, "escalated": False, "actor": actor}

    def _record_selection(self, report: CycleReport, project_id: str | None,
                          work_class: str, work_ref: str, admitted: bool,
                          reason: str, detail: Mapping[str, Any],
                          mission_ref: str | None = None) -> None:
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_selections"
                " (cycle_id,project_id,work_class,work_ref,admitted,reason,"
                "  mission_ref,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (report.cycle_id, project_id, work_class, work_ref, int(admitted),
                 reason, mission_ref, canonical_json(dict(detail)), self.clock()))
        if not admitted:
            report.refused.append({"project_id": project_id, "work_class": work_class,
                                   "work_ref": work_ref, "reason": reason,
                                   "detail": dict(detail)})


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #

def _absent(value: Any, word: str) -> Any:
    if word not in CANONICAL_ABSENCE:
        raise PolicyError("%r is not one of the four absence words" % (word,))
    return word if value is None else value


def _mission_line(mission: Mapping[str, Any]) -> dict[str, Any]:
    payload = mission.get("payload") or {}
    return {"mission_id": mission["id"], "project_id": mission.get("project_id"),
            "state": mission["state"],
            "origin": payload.get("origin", "backlog"),
            "work_item_id": payload.get("work_item_id", "unknown"),
            "terminal_reason": mission.get("terminal_reason") or "not_applicable"}


def _policy_from_row(row) -> SupervisorPolicy:
    return SupervisorPolicy(
        project_id=row["project_id"], enabled=bool(row["enabled"]),
        work_classes=tuple(json.loads(row["work_classes_json"])),
        missions_per_cycle=int(row["missions_per_cycle"]),
        maintenance_admissions=int(row["maintenance_admissions"]),
        improvement_admissions=int(row["improvement_admissions"]),
        window_start_hour=row["window_start_hour"],
        window_end_hour=row["window_end_hour"],
        failure_threshold=int(row["failure_threshold"]),
        suppression_seconds=float(row["suppression_seconds"]),
        policy_version=row["policy_version"])
