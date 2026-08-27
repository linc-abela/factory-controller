"""Stage 7: bounded autonomous maintenance, as admission rather than as a loop.

Stage 6 gave Production a way to say *this release is broken* and a way to hand
the defect back to the Factory.  What it never had was the join: nothing turned
a recorded production failure into a mission that anybody would run.  A person
read a receipt and typed a submission.  This module is that join, and every
design choice in it exists to keep the join from becoming a self-improving
loop.

Five shapes are deliberately absent, and each absence is the enforcement.

There is **no maintenance process**.  Nothing here polls, ticks, or wakes up.
:meth:`MaintenancePlane.admit_trigger` runs once per recorded production
failure, and a repair mission's own completion calls nothing in this module.
An unbounded repair loop is not refused at run time; there is no code path that
could execute one, because nothing here ever calls itself.

There is **no maintenance mission kind**.  A repair is an ordinary mission on
the ordinary store, so it inherits the acceptance gates, the evaluator, the
Context Broker entitlement, the execution lanes, Evidence Core, and the whole
Stage-5 portfolio scheduler -- fairness, dependencies, budgets, pause, drain
and emergency stop -- without one line here restating any of it.  Scheduling
maintenance across projects is therefore not a feature of this module; it is a
consequence of repairs being missions.

There is **nowhere to put a sentence**.  :meth:`admit_trigger` takes a source
*kind* and a source *reference* into this Controller's own production tables,
and nothing else.  There is no prompt field, no advice field, no description
field, no metadata blob.  Text produced by a model, an advisory service, a
gateway or an unbound external event cannot be a trigger here for the same reason a
secret cannot reach an environment schema: the contract has no container for
it.  Telemetry remains evidence; only a fact this ledger already recorded --
an incident a *person* declared, or a deployment this ledger itself settled as
failed -- can open a repair.

There is **no deployment verb**.  A validated repair reaches an environment
through :meth:`stage_recovery`, which calls the Stage-6 ledger's existing
``admit_release``.  Autonomous recovery is refused outright for a gated class,
and even if that refusal were removed the ledger would still park the release
at ``awaiting_approval`` for a person.  Nothing in this module can approve.

There is **no source-editing verb**.  The repair payload names a repository and
a baseline and stops.  The candidate is produced by the same execution path as
any other mission and is a real commit or it is nothing.

What *is* here is bounded: a policy the Owner writes, a cooldown, an attempt
ceiling per failure signature, a suppression threshold for a failure that keeps
coming back identical, a repair budget per policy version, and a concurrency
cap.  Each of them ends a repair rather than deferring it, and every terminal
disposition is recorded.  A fact that is not known is spelled with one of the
four canonical absence words, never with a zero.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

from . import portfolio, production, routing


CONTRACT_VERSION = "factory-controller/maintenance/1.0"

#: Reproduced from ``factory-evidence-core`` ``src/contracts/replay.py``, the
#: same four words ``store``, ``routing`` and ``production`` already carry.
CANONICAL_ABSENCE = production.CANONICAL_ABSENCE

#: The only two things that may open a repair.  Both name a row this
#: Controller wrote itself; neither is a message from anywhere.
TRIGGER_CLASSES = ("production_incident", "deployment_health_failure")

#: Which recorded table each class reads.  A third entry would need a third
#: durable production fact, which is the point.
SOURCE_TABLE = {"production_incident": "incidents",
                "deployment_health_failure": "deployments"}

#: An incident may open a repair from these states and no other.  ``closed``
#: is absent on purpose: a closed incident that recurs is a new declaration.
INCIDENT_REPAIRABLE = frozenset({"declared", "classified", "contained"})

#: A deployment may open a repair only once this ledger has settled it as
#: broken.  ``uncertain`` is absent: nobody knows what happened, so nobody
#: knows what to repair, and Stage 6 already refuses further releases there.
DEPLOYMENT_REPAIRABLE = frozenset({"failed", "rollback_failed", "escalated"})

#: A repair is open until it reaches one of these.  Every bound in this module
#: lands on one of them rather than on a delay.
DISPOSITIONS = ("recovered", "escalated", "suppressed", "abandoned")
TERMINAL = frozenset(DISPOSITIONS)

REPAIR_STATES = ("admitted", "mission_created", "candidate_validated",
                 "recovery_staged", "closed")

#: Mission states that mean the mission is over.  ``store.TERMINAL`` plus
#: ``escalated``, which stops a mission without ever being one of the four.
MISSION_SETTLED = frozenset({"completed", "refused", "failed", "cancelled",
                             "escalated"})

DEFAULT_REPAIR_BUDGET = 8
DEFAULT_CONCURRENCY = 1
DEFAULT_COOLDOWN_SECONDS = 900.0
DEFAULT_ATTEMPT_CEILING = 2
DEFAULT_SUPPRESSION_THRESHOLD = 3


SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_policies (
  project_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL,
  environment_classes_json TEXT NOT NULL,
  trigger_classes_json TEXT NOT NULL,
  repair_budget INTEGER NOT NULL,
  concurrency INTEGER NOT NULL,
  cooldown_seconds REAL NOT NULL,
  attempt_ceiling INTEGER NOT NULL,
  suppression_threshold INTEGER NOT NULL,
  execution_mode TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
  trigger_ref TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  trigger_class TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  signature TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  repository TEXT NOT NULL,
  baseline_sha TEXT NOT NULL,
  state TEXT NOT NULL,
  disposition TEXT,
  mission_ref TEXT,
  idempotency_key TEXT,
  candidate_sha TEXT,
  evaluator_result TEXT,
  evidence_ref TEXT,
  bundle_ref TEXT,
  recovery_deployment_id TEXT,
  recovery_environment_id TEXT,
  recovery_outcome TEXT,
  admitted_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (source_kind, source_ref)
);
CREATE INDEX IF NOT EXISTS repairs_by_signature
  ON repairs(signature, admitted_at);
CREATE INDEX IF NOT EXISTS repairs_by_project
  ON repairs(project_id, state);
CREATE TABLE IF NOT EXISTS maintenance_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  trigger_ref TEXT,
  kind TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS maintenance_events_no_update
BEFORE UPDATE ON maintenance_events
BEGIN SELECT RAISE(ABORT, 'maintenance events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS maintenance_events_no_delete
BEFORE DELETE ON maintenance_events
BEGIN SELECT RAISE(ABORT, 'maintenance events are append-only'); END;
"""


class PolicyError(ValueError):
    """A maintenance declaration the Controller will not store."""


class MaintenanceRefusal(Exception):
    """A bounded stop, carrying the code and why.

    Named with a ``MAINTENANCE_`` prefix throughout: ``production.py`` already
    owns ``PRODUCTION_*`` and the bridge owns bare codes of its own, and a
    layer that refuses under a name a neighbour also uses cannot be routed on.
    """

    def __init__(self, code: str, detail: str, *, trigger_ref: str | None = None,
                 project_id: str | None = None) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.trigger_ref = trigger_ref
        self.project_id = project_id

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "trigger_ref": self.trigger_ref, "project_id": self.project_id}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def trigger_reference(source_kind: str, source_ref: str) -> str:
    """One recorded failure, one repair reference, forever.

    Derived rather than allocated, so a replay of the same production fact
    after a restart computes the same value and collides with the row that is
    already there instead of opening a second repair.
    """

    return "mnt_%s" % digest({"source_kind": source_kind,
                              "source_ref": source_ref})[:24]


def failure_signature(project_id: str, environment_id: str,
                      trigger_class: str, failing_identity: str) -> str:
    """What makes two failures *the same* failure.

    Deliberately coarser than the trigger reference: two distinct incidents
    describing one unfixed defect on one release share this value, which is how
    a repair that keeps failing the same way is recognised and suppressed
    rather than retried forever under a new reference each time.
    """

    return digest({"project_id": project_id, "environment_id": environment_id,
                   "trigger_class": trigger_class,
                   "failing_identity": failing_identity})[:32]


@dataclass(frozen=True)
class MaintenancePolicy:
    """One project's autonomous-repair envelope, written by the Owner.

    Every field is a bound, and every bound ends a repair rather than delaying
    it indefinitely.  ``repair_budget`` counts against ``policy_version``: when
    it is spent, maintenance for the project stops until the Owner writes a new
    version, which is a deliberate human act and not a timer.
    """

    project_id: str
    enabled: bool = False
    environment_classes: tuple[str, ...] = ("local-sim", "staging")
    trigger_classes: tuple[str, ...] = TRIGGER_CLASSES
    repair_budget: int = DEFAULT_REPAIR_BUDGET
    concurrency: int = DEFAULT_CONCURRENCY
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    attempt_ceiling: int = DEFAULT_ATTEMPT_CEILING
    suppression_threshold: int = DEFAULT_SUPPRESSION_THRESHOLD
    execution_mode: str = "fixture"
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise PolicyError("project_id is required")
        if not self.environment_classes:
            raise PolicyError(
                "a maintenance policy with no environment class admits nothing; "
                "state the classes or leave maintenance disabled")
        for name in self.environment_classes:
            if name not in production.ENVIRONMENT_CLASSES:
                raise PolicyError("environment class %r is not one of %s"
                                  % (name, ", ".join(production.ENVIRONMENT_CLASSES)))
            if name in production.GATED_CLASSES:
                # Not a default that can be flipped.  Autonomous maintenance has
                # no representation in which it recovers a gated class, so the
                # production gate cannot be configured away from this side.
                raise PolicyError(
                    "autonomous maintenance cannot be scoped to a %s "
                    "environment; recovery there is approved by a person" % name)
        if not self.trigger_classes:
            raise PolicyError("state at least one trigger class")
        for name in self.trigger_classes:
            if name not in TRIGGER_CLASSES:
                raise PolicyError("trigger class %r is not one of %s"
                                  % (name, ", ".join(TRIGGER_CLASSES)))
        if self.execution_mode not in routing.EXECUTION_MODES:
            raise PolicyError("execution_mode must be one of %s"
                              % ", ".join(sorted(routing.EXECUTION_MODES)))
        for name, value in (("repair_budget", self.repair_budget),
                            ("concurrency", self.concurrency),
                            ("attempt_ceiling", self.attempt_ceiling),
                            ("suppression_threshold", self.suppression_threshold)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError("%s must be a non-negative integer" % name)
        if self.attempt_ceiling < 1:
            raise PolicyError(
                "attempt_ceiling below 1 would admit a repair that may never "
                "run; disable maintenance instead")
        if self.suppression_threshold < 1:
            raise PolicyError("suppression_threshold must be at least 1")
        if self.cooldown_seconds < 0:
            raise PolicyError("cooldown_seconds must not be negative")

    def as_row(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "enabled": self.enabled,
                "environment_classes": list(self.environment_classes),
                "trigger_classes": list(self.trigger_classes),
                "repair_budget": self.repair_budget,
                "concurrency": self.concurrency,
                "cooldown_seconds": self.cooldown_seconds,
                "attempt_ceiling": self.attempt_ceiling,
                "suppression_threshold": self.suppression_threshold,
                "execution_mode": self.execution_mode,
                "policy_version": self.policy_version}


class MaintenancePlane:
    """Durable Stage-7 state, on the mission store's own connection.

    It borrows the store for the same reason :class:`production.ProductionLedger`
    does: admitting a repair and submitting its mission have to commit or fail
    together, and two database files cannot do that.
    """

    def __init__(self, store, ledger: production.ProductionLedger) -> None:
        self._store = store
        self._ledger = ledger
        with store.transaction() as db:
            db.executescript(SCHEMA)

    # -- policy ------------------------------------------------------------ #

    def set_policy(self, policy: MaintenancePolicy) -> dict[str, Any]:
        now = time.time()
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO maintenance_policies (project_id, enabled,"
                " environment_classes_json, trigger_classes_json, repair_budget,"
                " concurrency, cooldown_seconds, attempt_ceiling,"
                " suppression_threshold, execution_mode, policy_version,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,"
                " environment_classes_json=excluded.environment_classes_json,"
                " trigger_classes_json=excluded.trigger_classes_json,"
                " repair_budget=excluded.repair_budget,"
                " concurrency=excluded.concurrency,"
                " cooldown_seconds=excluded.cooldown_seconds,"
                " attempt_ceiling=excluded.attempt_ceiling,"
                " suppression_threshold=excluded.suppression_threshold,"
                " execution_mode=excluded.execution_mode,"
                " policy_version=excluded.policy_version,"
                " updated_at=excluded.updated_at",
                (policy.project_id, int(policy.enabled),
                 canonical_json(list(policy.environment_classes)),
                 canonical_json(list(policy.trigger_classes)),
                 policy.repair_budget, policy.concurrency,
                 policy.cooldown_seconds, policy.attempt_ceiling,
                 policy.suppression_threshold, policy.execution_mode,
                 policy.policy_version, now, now))
            self._append(db, "policy_set", policy.project_id,
                         detail=policy.as_row())
        return policy.as_row()

    def policy(self, project_id: str) -> MaintenancePolicy | None:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM maintenance_policies WHERE project_id=?",
                             (project_id,)).fetchone()
        return None if row is None else _policy_from_row(row)

    def set_enabled(self, project_id: str, enabled: bool) -> dict[str, Any]:
        current = self.policy(project_id)
        if current is None:
            raise PolicyError("no maintenance policy is declared for %s" % project_id)
        updated = MaintenancePolicy(**{**_policy_kwargs(current), "enabled": enabled})
        return self.set_policy(updated)

    # -- admission --------------------------------------------------------- #

    def admit_trigger(self, trigger_class: str, source_ref: str) -> dict[str, Any]:
        """Open at most one repair for one recorded production failure.

        The whole input is a class and a reference into this Controller's own
        tables.  Everything the repair needs -- project, environment,
        repository, baseline -- is read from the recorded row, so no caller can
        supply a repository or a baseline the production ledger did not already
        bind, and no caller can supply anything else at all.
        """

        if trigger_class not in TRIGGER_CLASSES:
            raise MaintenanceRefusal(
                "MAINTENANCE_TRIGGER_CLASS_UNKNOWN",
                "%r is not a trigger class; the admitted classes are %s"
                % (trigger_class, ", ".join(TRIGGER_CLASSES)))
        source_kind = SOURCE_TABLE[trigger_class]
        trigger_ref = trigger_reference(source_kind, source_ref)
        refusal = None
        row = None
        with self._store.transaction() as db:
            existing = db.execute("SELECT * FROM repairs WHERE trigger_ref=?",
                                  (trigger_ref,)).fetchone()
            if existing is not None:
                # The same recorded failure, replayed.  This is the same repair,
                # not a second one, and saying so is the whole idempotency
                # mechanism: nothing downstream had to be consulted to know it.
                return dict(existing)
            fact = self._failing_fact(db, trigger_class, source_ref)
            if isinstance(fact, MaintenanceRefusal):
                refusal = fact
            else:
                refusal, row = self._admission(db, trigger_class, source_kind,
                                               source_ref, trigger_ref, fact)
            if refusal is not None:
                self._append(db, "trigger_refused",
                             refusal.project_id or "unbound",
                             trigger_ref=trigger_ref, detail=refusal.as_row())
        if refusal is not None:
            raise refusal
        return row

    def _failing_fact(self, db, trigger_class: str,
                      source_ref: str) -> dict[str, Any] | MaintenanceRefusal:
        """Read the recorded production row, or refuse because there is none.

        This is where "telemetry is evidence, not authority" is enforced: an
        alert, a health sample or a model's opinion never reaches here, because
        the only thing that reaches here is a primary key.
        """

        if trigger_class == "production_incident":
            row = db.execute("SELECT * FROM incidents WHERE incident_ref=?",
                             (source_ref,)).fetchone()
            if row is None:
                return MaintenanceRefusal(
                    "MAINTENANCE_SOURCE_NOT_RECORDED",
                    "no incident %r is recorded; a repair cannot be opened on "
                    "a claim this ledger did not write" % source_ref)
            if row["state"] not in INCIDENT_REPAIRABLE:
                return MaintenanceRefusal(
                    "MAINTENANCE_SOURCE_NOT_REPAIRABLE",
                    "incident %s is %r; a repair opens from %s"
                    % (source_ref, row["state"], ", ".join(sorted(INCIDENT_REPAIRABLE))),
                    project_id=row["project_id"])
            environment = db.execute(
                "SELECT * FROM environments WHERE environment_id=?",
                (row["environment_id"],)).fetchone()
            return {"project_id": row["project_id"],
                    "environment_id": row["environment_id"],
                    "repository": environment["repository"],
                    "baseline_sha": row["affected_release_sha"],
                    "environment_class": environment["environment_class"],
                    "failing_identity": "%s/%s" % (row["affected_release_sha"],
                                                   row["failing_behaviour"]),
                    "summary": row["failing_behaviour"]}

        row = db.execute("SELECT * FROM deployments WHERE id=?",
                         (source_ref,)).fetchone()
        if row is None:
            return MaintenanceRefusal(
                "MAINTENANCE_SOURCE_NOT_RECORDED",
                "no deployment %r is recorded" % source_ref)
        if row["state"] not in DEPLOYMENT_REPAIRABLE:
            return MaintenanceRefusal(
                "MAINTENANCE_SOURCE_NOT_REPAIRABLE",
                "deployment %s is %r; a repair opens from %s"
                % (source_ref, row["state"], ", ".join(sorted(DEPLOYMENT_REPAIRABLE))),
                project_id=row["project_id"])
        environment = db.execute("SELECT * FROM environments WHERE environment_id=?",
                                 (row["environment_id"],)).fetchone()
        outcome = row["health_outcome"] or "not_run"
        return {"project_id": row["project_id"],
                "environment_id": row["environment_id"],
                "repository": environment["repository"],
                "baseline_sha": row["release_sha"],
                "environment_class": environment["environment_class"],
                "failing_identity": "%s/%s/%s" % (row["release_sha"], row["state"],
                                                  outcome),
                "summary": "deployment %s settled %s with health %s"
                           % (source_ref, row["state"], outcome)}

    def _admission(self, db, trigger_class: str, source_kind: str, source_ref: str,
                   trigger_ref: str, fact: Mapping[str, Any]):
        """Every bound, checked in one place, before anything durable is written."""

        project_id = fact["project_id"]
        policy_row = db.execute(
            "SELECT * FROM maintenance_policies WHERE project_id=?",
            (project_id,)).fetchone()
        if policy_row is None or not policy_row["enabled"]:
            return MaintenanceRefusal(
                "MAINTENANCE_DISABLED",
                "autonomous maintenance is not enabled for %s" % project_id,
                trigger_ref=trigger_ref, project_id=project_id), None
        policy = _policy_from_row(policy_row)

        if trigger_class not in policy.trigger_classes:
            return MaintenanceRefusal(
                "MAINTENANCE_TRIGGER_CLASS_NOT_ADMITTED",
                "%s does not admit %s triggers" % (project_id, trigger_class),
                trigger_ref=trigger_ref, project_id=project_id), None
        if fact["environment_class"] not in policy.environment_classes:
            return MaintenanceRefusal(
                "MAINTENANCE_ENVIRONMENT_OUT_OF_SCOPE",
                "%s is a %s environment and %s scopes maintenance to %s"
                % (fact["environment_id"], fact["environment_class"], project_id,
                   ", ".join(policy.environment_classes)),
                trigger_ref=trigger_ref, project_id=project_id), None

        stopped = db.execute("SELECT emergency_stop FROM portfolio WHERE id=1").fetchone()
        if stopped is not None and stopped["emergency_stop"]:
            return MaintenanceRefusal(
                "MAINTENANCE_EMERGENCY_STOP",
                "the portfolio is under an emergency stop",
                trigger_ref=trigger_ref, project_id=project_id), None
        project_row = db.execute("SELECT state FROM projects WHERE project_id=?",
                                 (project_id,)).fetchone()
        if project_row is None or project_row["state"] not in portfolio.ADMITTING:
            state = "unregistered" if project_row is None else project_row["state"]
            return MaintenanceRefusal(
                "MAINTENANCE_PROJECT_NOT_ADMITTING",
                "project %s is %s; maintenance does not create work a paused "
                "project would not accept" % (project_id, state),
                trigger_ref=trigger_ref, project_id=project_id), None

        spent = db.execute(
            "SELECT COUNT(*) AS n FROM repairs WHERE project_id=? AND policy_version=?",
            (project_id, policy.policy_version)).fetchone()["n"]
        if spent >= policy.repair_budget:
            return MaintenanceRefusal(
                "MAINTENANCE_BUDGET_EXHAUSTED",
                "%s has spent its repair budget of %d under policy version %r; "
                "a new budget is an Owner decision, not a timer"
                % (project_id, policy.repair_budget, policy.policy_version),
                trigger_ref=trigger_ref, project_id=project_id), None

        open_repairs = db.execute(
            "SELECT COUNT(*) AS n FROM repairs WHERE project_id=? AND disposition IS NULL",
            (project_id,)).fetchone()["n"]
        if open_repairs >= policy.concurrency:
            return MaintenanceRefusal(
                "MAINTENANCE_CONCURRENCY_EXCEEDED",
                "%s already has %d repair(s) open" % (project_id, open_repairs),
                trigger_ref=trigger_ref, project_id=project_id), None

        signature = failure_signature(project_id, fact["environment_id"],
                                      trigger_class, fact["failing_identity"])
        prior = db.execute(
            "SELECT disposition, admitted_at FROM repairs WHERE signature=?"
            " ORDER BY admitted_at", (signature,)).fetchall()
        attempt = len(prior) + 1
        if attempt > policy.attempt_ceiling:
            return MaintenanceRefusal(
                "MAINTENANCE_ATTEMPT_CEILING_REACHED",
                "this failure has already been repaired %d time(s) and the "
                "ceiling is %d; it stops here rather than recurring"
                % (len(prior), policy.attempt_ceiling),
                trigger_ref=trigger_ref, project_id=project_id), None
        unrecovered = sum(1 for row in prior if row["disposition"] != "recovered")
        if unrecovered >= policy.suppression_threshold:
            return MaintenanceRefusal(
                "MAINTENANCE_SIGNATURE_SUPPRESSED",
                "the identical failure %s has failed to recover %d time(s); "
                "repeating it is not a repair" % (signature, unrecovered),
                trigger_ref=trigger_ref, project_id=project_id), None
        if prior:
            waited = time.time() - prior[-1]["admitted_at"]
            if waited < policy.cooldown_seconds:
                return MaintenanceRefusal(
                    "MAINTENANCE_COOLDOWN_ACTIVE",
                    "the previous repair of this failure was %.1fs ago and the "
                    "cooldown is %.1fs" % (waited, policy.cooldown_seconds),
                    trigger_ref=trigger_ref, project_id=project_id), None

        now = time.time()
        db.execute(
            "INSERT INTO repairs (trigger_ref, project_id, environment_id,"
            " trigger_class, source_kind, source_ref, signature, attempt,"
            " policy_version, repository, baseline_sha, state, admitted_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'admitted', ?,?)",
            (trigger_ref, project_id, fact["environment_id"], trigger_class,
             source_kind, source_ref, signature, attempt, policy.policy_version,
             fact["repository"], fact["baseline_sha"], now, now))
        self._append(db, "trigger_admitted", project_id, trigger_ref=trigger_ref,
                     to_state="admitted",
                     detail={"trigger_class": trigger_class,
                             "source_ref": source_ref, "signature": signature,
                             "attempt": attempt,
                             "policy_version": policy.policy_version})
        row = db.execute("SELECT * FROM repairs WHERE trigger_ref=?",
                         (trigger_ref,)).fetchone()
        return None, dict(row)

    # -- the repair mission ------------------------------------------------ #

    def repair_payload(self, trigger_ref: str, *,
                       acceptance_gate_ids: tuple[str, ...] | list[str],
                       provider_candidates: list[dict[str, Any]] | None = None,
                       context_manifest_hash: str | None = None,
                       extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The ordinary mission payload a repair becomes.

        Nothing here is special to maintenance except ``origin`` and the
        lineage references, which are recorded so a mission can be traced back
        to the production fact that opened it.  The repository and the baseline
        come from the repair row, which took them from the production ledger.
        """

        row = self._repair(trigger_ref)
        policy = self.policy(row["project_id"])
        if policy is None:
            raise PolicyError("no maintenance policy is declared for %s"
                              % row["project_id"])
        payload: dict[str, Any] = {
            "work_item_id": trigger_ref,
            "project_id": row["project_id"],
            "repository": row["repository"],
            "baseline_sha": row["baseline_sha"],
            "capability": "bug",
            "origin": "maintenance_trigger",
            "trigger_ref": trigger_ref,
            "trigger_class": row["trigger_class"],
            "source_ref": row["source_ref"],
            "environment_id": row["environment_id"],
            "execution_mode": policy.execution_mode,
            "acceptance_gate_ids": list(acceptance_gate_ids),
        }
        if provider_candidates is not None:
            payload["provider_candidates"] = provider_candidates
        if context_manifest_hash is not None:
            payload["context_manifest_hash"] = context_manifest_hash
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def repair_idempotency_key(payload: Mapping[str, Any]) -> str:
        """The key the rest of the stack will accept, derived not chosen.

        For a real mission ``factory-evidence-core`` refuses any value other
        than ``work_item_id:context_manifest_hash``, so choosing one here would
        produce a repair that provably could never reach the bridge.  The
        fixture case uses the trigger reference alone, which is already unique
        per recorded failure and therefore already replay-safe.
        """

        manifest = payload.get("context_manifest_hash")
        if payload.get("execution_mode") == "real" and isinstance(manifest, str) and manifest:
            return routing.expected_idempotency_key(payload["work_item_id"], manifest)
        return str(payload["work_item_id"])

    def create_repair_mission(self, trigger_ref: str, controller,
                              **payload_kwargs) -> tuple[dict[str, Any], bool]:
        """Submit the repair as an ordinary mission, exactly once.

        Two independent mechanisms make a duplicate impossible, and both are
        already load-bearing elsewhere: the store refuses a second mission under
        one idempotency key, and this row records the mission reference it got.
        A restart between the submission and the record therefore recovers the
        same mission rather than opening a second one.
        """

        row = self._repair(trigger_ref)
        if row["disposition"]:
            raise MaintenanceRefusal(
                "MAINTENANCE_REPAIR_CLOSED",
                "repair %s is already %s; a closed repair does not submit work"
                % (trigger_ref, row["disposition"]),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        if row["mission_ref"]:
            existing = controller.store.get(row["mission_ref"])
            if existing is not None:
                return existing, False
        payload = self.repair_payload(trigger_ref, **payload_kwargs)
        key = self.repair_idempotency_key(payload)
        mission, created = controller.submit(payload, key)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE repairs SET mission_ref=?, idempotency_key=?, state=?,"
                " updated_at=? WHERE trigger_ref=? AND disposition IS NULL",
                (mission["id"], key, "mission_created", time.time(), trigger_ref))
            self._append(db, "repair_mission_created", row["project_id"],
                         trigger_ref=trigger_ref, from_state=row["state"],
                         to_state="mission_created",
                         detail={"mission_ref": mission["id"],
                                 "idempotency_key": key, "created": created})
        return mission, created

    def record_mission_outcome(self, trigger_ref: str, mission: Mapping[str, Any]
                               ) -> dict[str, Any]:
        """Copy the mission's own terminal facts onto the repair's lineage.

        Nothing is inferred.  A mission that never produced a candidate records
        ``not_run`` for the candidate, not an empty string, and a mission still
        in flight records ``unknown`` rather than a guess at where it will land.
        """

        row = self._repair(trigger_ref)
        mission_id = mission["id"]
        state = mission.get("state")
        # The step table, not the mission result: a mission escalated by a
        # failing acceptance gate never writes a result, and reading only the
        # result would record `not_run` for an evaluator that ran and failed.
        # Those are different facts and the lineage has to keep them apart.
        evaluation = self._store.step_output(mission_id, "evaluate")
        evidence = self._store.step_output(mission_id, "evidence")
        candidate = self._candidate_sha(mission_id) or "not_run"
        if not isinstance(evaluation, Mapping):
            evaluator = "not_run" if state in MISSION_SETTLED else "unknown"
        else:
            evaluator = "passed" if evaluation.get("passed") else "failed"
        if isinstance(evidence, Mapping) and evidence.get("evidence_pointer"):
            evidence_ref = evidence["evidence_pointer"]
        else:
            evidence_ref = "not_run" if state in MISSION_SETTLED else "unknown"
        target = ("candidate_validated" if state == "completed" and evaluator == "passed"
                  else row["state"])
        with self._store.transaction() as db:
            db.execute(
                "UPDATE repairs SET candidate_sha=?, evaluator_result=?,"
                " evidence_ref=?, state=?, updated_at=? WHERE trigger_ref=?",
                (candidate, evaluator, evidence_ref, target, time.time(), trigger_ref))
            self._append(db, "repair_mission_outcome", row["project_id"],
                         trigger_ref=trigger_ref, from_state=row["state"],
                         to_state=target,
                         detail={"mission_state": state, "candidate_sha": candidate,
                                 "evaluator_result": evaluator,
                                 "evidence_ref": evidence_ref})
        return self.lineage(trigger_ref)

    # -- recovery ---------------------------------------------------------- #

    def stage_recovery(self, trigger_ref: str, bundle: production.ReleaseBundle,
                       environment_id: str) -> str:
        """Hand a validated repair to an ungated environment, or refuse.

        This is the whole of what autonomous maintenance may do to a running
        system, and it is a call into Stage 6 rather than a deployment of its
        own: the ledger applies the same admission rules a person's release
        gets, including emergency stop, drain, concurrency and the unresolved
        ``uncertain`` deployment refusal.
        """

        row = self._repair(trigger_ref)
        if row["disposition"]:
            raise MaintenanceRefusal(
                "MAINTENANCE_REPAIR_CLOSED",
                "repair %s is already %s" % (trigger_ref, row["disposition"]),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        if row["evaluator_result"] != "passed":
            raise MaintenanceRefusal(
                "MAINTENANCE_CANDIDATE_UNVALIDATED",
                "the repair candidate is %r; only a candidate the evaluator "
                "passed reaches an environment"
                % (row["evaluator_result"] or "unknown"),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        policy = self._ledger.environment(environment_id)
        if policy.gated:
            raise MaintenanceRefusal(
                "MAINTENANCE_PRODUCTION_AUTHORITY_REQUIRED",
                "%s is a %s environment that a person approves; autonomous "
                "maintenance stages a recovery, it never grants itself the "
                "authority to release one"
                % (environment_id, policy.environment_class),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        if policy.project_id != row["project_id"]:
            raise MaintenanceRefusal(
                "MAINTENANCE_PROJECT_ISOLATION",
                "repair %s belongs to %s and %s belongs to %s"
                % (trigger_ref, row["project_id"], environment_id, policy.project_id),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        deployment_id = self._ledger.admit_release(
            bundle, environment_id, requested_by="maintenance:%s" % trigger_ref)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE repairs SET bundle_ref=?, recovery_deployment_id=?,"
                " recovery_environment_id=?, state=?, updated_at=?"
                " WHERE trigger_ref=?",
                (bundle.bundle_ref, deployment_id, environment_id,
                 "recovery_staged", time.time(), trigger_ref))
            self._append(db, "recovery_staged", row["project_id"],
                         trigger_ref=trigger_ref, from_state=row["state"],
                         to_state="recovery_staged",
                         detail={"deployment_id": deployment_id,
                                 "environment_id": environment_id,
                                 "bundle_ref": bundle.bundle_ref})
        return deployment_id

    def close(self, trigger_ref: str, disposition: str, *, reason: str,
              recovery_outcome: str | None = None) -> dict[str, Any]:
        """End a repair.  Every bound in this module arrives here."""

        if disposition not in DISPOSITIONS:
            raise PolicyError("disposition must be one of %s"
                              % ", ".join(DISPOSITIONS))
        row = self._repair(trigger_ref)
        if row["disposition"]:
            raise MaintenanceRefusal(
                "MAINTENANCE_REPAIR_CLOSED",
                "repair %s is already %s" % (trigger_ref, row["disposition"]),
                trigger_ref=trigger_ref, project_id=row["project_id"])
        with self._store.transaction() as db:
            db.execute(
                "UPDATE repairs SET disposition=?, state='closed',"
                " recovery_outcome=COALESCE(?, recovery_outcome), updated_at=?"
                " WHERE trigger_ref=?",
                (disposition, recovery_outcome, time.time(), trigger_ref))
            self._append(db, "repair_closed", row["project_id"],
                         trigger_ref=trigger_ref, from_state=row["state"],
                         to_state="closed",
                         detail={"disposition": disposition, "reason": reason,
                                 "recovery_outcome": recovery_outcome or "not_applicable"})
        return self.lineage(trigger_ref)

    # -- reading ----------------------------------------------------------- #

    def lineage(self, trigger_ref: str) -> dict[str, Any]:
        """Originating fact through terminal disposition, absences spelled out."""

        row = self._repair(trigger_ref)
        with self._store.transaction() as db:
            events = db.execute(
                "SELECT kind, from_state, to_state, created_at FROM maintenance_events"
                " WHERE trigger_ref=? ORDER BY sequence", (trigger_ref,)).fetchall()
        return {
            "contract_version": CONTRACT_VERSION,
            "trigger_ref": trigger_ref,
            "project_id": row["project_id"],
            "environment_id": row["environment_id"],
            "trigger_class": row["trigger_class"],
            "source_kind": row["source_kind"],
            "source_ref": row["source_ref"],
            "signature": row["signature"],
            "attempt": row["attempt"],
            "policy_version": row["policy_version"],
            "repository": row["repository"],
            "baseline_sha": row["baseline_sha"],
            "state": row["state"],
            "mission_ref": _absent(row["mission_ref"], "not_run"),
            "idempotency_key": _absent(row["idempotency_key"], "not_run"),
            "candidate_sha": _absent(row["candidate_sha"], "not_run"),
            "evaluator_result": _absent(row["evaluator_result"], "not_run"),
            "evidence_ref": _absent(row["evidence_ref"], "not_run"),
            "bundle_ref": _absent(row["bundle_ref"], "not_applicable"),
            "recovery_deployment_id": _absent(row["recovery_deployment_id"],
                                              "not_applicable"),
            "recovery_environment_id": _absent(row["recovery_environment_id"],
                                               "not_applicable"),
            "recovery_outcome": _absent(row["recovery_outcome"], "not_run"),
            "disposition": _absent(row["disposition"], "unknown"),
            "transitions": [dict(event) for event in events],
        }

    def repairs(self, project_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            if project_id is None:
                rows = db.execute("SELECT * FROM repairs ORDER BY admitted_at").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM repairs WHERE project_id=? ORDER BY admitted_at",
                    (project_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    def events(self, project_id: str) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM maintenance_events WHERE project_id=? ORDER BY sequence",
                (project_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    # -- internals --------------------------------------------------------- #

    def _candidate_sha(self, mission_id: str) -> str | None:
        """The candidate the mission actually produced, from its own history.

        Taken from the recorded transition rather than from the terminal
        result, for the same reason the evaluator reading is: a mission can
        produce a real candidate and then stop at a gate, and the candidate is
        still a fact about what happened.
        """

        for event in self._store.history(mission_id):
            detail = event.get("detail")
            if isinstance(detail, Mapping) and detail.get("candidate_sha"):
                return str(detail["candidate_sha"])
        return None

    def _repair(self, trigger_ref: str):
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM repairs WHERE trigger_ref=?",
                             (trigger_ref,)).fetchone()
        if row is None:
            raise MaintenanceRefusal("MAINTENANCE_REPAIR_UNKNOWN",
                                     "no repair %r is recorded" % trigger_ref,
                                     trigger_ref=trigger_ref)
        return row

    @staticmethod
    def _append(db, kind: str, project_id: str, *, trigger_ref: str | None = None,
                from_state: str | None = None, to_state: str | None = None,
                detail: Mapping[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO maintenance_events (project_id, trigger_ref, kind,"
            " from_state, to_state, detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, trigger_ref, kind, from_state, to_state,
             canonical_json(dict(detail or {})), time.time()))


def _absent(value: Any, word: str) -> Any:
    if word not in CANONICAL_ABSENCE:
        raise PolicyError("%r is not one of the four absence words" % word)
    return word if value in (None, "") else value


def _policy_from_row(row) -> MaintenancePolicy:
    return MaintenancePolicy(
        project_id=row["project_id"],
        enabled=bool(row["enabled"]),
        environment_classes=tuple(json.loads(row["environment_classes_json"])),
        trigger_classes=tuple(json.loads(row["trigger_classes_json"])),
        repair_budget=row["repair_budget"],
        concurrency=row["concurrency"],
        cooldown_seconds=row["cooldown_seconds"],
        attempt_ceiling=row["attempt_ceiling"],
        suppression_threshold=row["suppression_threshold"],
        execution_mode=row["execution_mode"],
        policy_version=row["policy_version"])


def _policy_kwargs(policy: MaintenancePolicy) -> dict[str, Any]:
    return {"project_id": policy.project_id, "enabled": policy.enabled,
            "environment_classes": policy.environment_classes,
            "trigger_classes": policy.trigger_classes,
            "repair_budget": policy.repair_budget,
            "concurrency": policy.concurrency,
            "cooldown_seconds": policy.cooldown_seconds,
            "attempt_ceiling": policy.attempt_ceiling,
            "suppression_threshold": policy.suppression_threshold,
            "execution_mode": policy.execution_mode,
            "policy_version": policy.policy_version}
