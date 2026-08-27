"""Stage 8: recursive self-improvement as a bounded lineage, not a loop.

Stage 7 gave the Factory a way to repair a known failure against an intended
behaviour that already existed.  Stage 8 is the other thing: changing what the
intended behaviour *is* -- capability, quality, cost, performance, reliability,
maintainability, operability.  That is a different act with a different danger,
so it gets a different contract rather than a wider maintenance one.

The distinction is the first thing this module enforces and the reason it is a
separate file.  A repair says *this stopped working, put it back*.  An
improvement says *this works and should be better*, and "better" is a claim
that has to be measured against something pinned, by somebody who did not make
the change.  Maintenance needs neither of those; improvement is worthless
without both.

Seven shapes are deliberately absent, and each absence is the enforcement.

There is **no improvement process**.  Nothing here polls, ticks, schedules or
wakes up, and an experiment's own completion calls nothing in this module.
Generation N+1 exists because somebody called :meth:`ImprovementPlane.open_generation`;
there is no code path by which generation N causes that call.  Runaway
recursion is not refused at run time, because there is nothing that could
execute it.

There is **no improvement mission kind**.  An experiment's candidate is an
ordinary mission on the ordinary store, so it inherits the acceptance gates,
the evaluator, the Context Broker entitlement, the execution lanes, Evidence
Core, and the whole Stage-5 portfolio scheduler -- fairness, dependencies,
budgets, pause, drain and emergency stop -- without one line here restating any
of it.  There is no self-improvement agent runtime and no second execution
path, because an experiment that needed one would not be running under the
Factory's own gates.

There is **nowhere to put a sentence**.  :meth:`ImprovementPlane.admit_experiment`
takes an objective reference, a trigger class and a source reference into this
Controller's own tables.  Model output, advisory opinion, gateway metadata and
free prompt text cannot be a trigger here for the same reason a secret cannot
reach an environment schema: the contract has no container for them.  The one
place human intent enters is :meth:`register_objective`, which is an explicit
versioned Owner act, and an experiment has no method that writes one.  A
retrospective reaches this module the same way -- through an Owner objective --
because the Controller reads no files and a document is not a durable row here.

There is **no way to write a policy that leaves a protected surface
unprotected**.  ``protected_surfaces`` is not a list a policy may shorten; every
name in :data:`MANDATORY_SURFACES` must be present *with at least one path
prefix*, checked in ``__post_init__``.  A policy that omits one, or declares one
covering nothing, is not stored.  So the autonomous path cannot be widened by
editing policy, only by an Owner writing a policy this module refuses.

There is **no promotion verb that reaches a gated environment or the Factory
itself**.  :meth:`stage_promotion` calls Stage 6's ledger, which applies the
same admission a person's release gets, and refuses outright for a gated class
or a self-target experiment.  An accepted self-improvement candidate is a
commit in an isolated lane and an evidence record.  Installing it is an Owner
act with no representation here.

There is **no metric this module can change after execution begins**.  Metrics
live on the objective, the objective is digested at registration, and the
experiment pins that digest.  A later edit to the objective produces a
different digest, so a candidate cannot be compared under metrics that moved --
a mutated objective is a different objective, which is a different experiment.

There is **nothing that reads a repair and opens an improvement from it**.
``maintenance_history`` requires a *measured* count of closed repairs sharing
one failure signature, which is a fact about a pattern rather than about an
event, so one repair can never become one improvement.  Nothing in this module
writes to the maintenance tables either, so the two cannot drive each other.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import portfolio, production, routing


CONTRACT_VERSION = "factory-controller/improvement/1.0"

#: Reproduced from ``factory-evidence-core`` ``src/contracts/replay.py``.  The
#: literal is checked against every other layer's copy by test, because this
#: exact set has now been forked six times across the corpus.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

#: What an improvement may be about.  ``bug`` is absent on purpose: repairing a
#: known failure against an existing intended behaviour is Stage 7's contract,
#: and admitting it here would let a repair skip the maintenance bounds by
#: calling itself an improvement.
IMPROVEMENT_CLASSES = ("capability", "quality", "cost", "performance",
                       "reliability", "maintainability", "operability")

#: The only facts that may open a generation.  Each names a row this Controller
#: wrote itself; none of them is a message from anywhere, and an experiment is
#: not among them, so a candidate cannot authorize its own successor.
TRIGGER_CLASSES = ("owner_objective", "maintenance_history",
                   "operational_pain", "cost_inefficiency")

#: Which recorded table each class reads.  Adding a fifth class would require a
#: fifth durable fact, which is the point of the mapping being explicit.
SOURCE_TABLE = {"owner_objective": "objectives",
                "maintenance_history": "repairs",
                "operational_pain": "incidents",
                "cost_inefficiency": "projects"}

#: The surfaces an autonomous improvement may never touch.  A policy may add
#: names; it may not drop one, and it may not declare one that covers nothing.
MANDATORY_SURFACES = ("governance", "production_authority",
                      "admission_integrity", "evaluator_independence",
                      "improvement_policy", "secret_handling",
                      "emergency_stop", "release_authority")

#: How a metric participates.  An ``objective`` metric must improve; a
#: ``non_regression`` metric must not get worse beyond its stated tolerance.
METRIC_ROLES = ("objective", "non_regression")
METRIC_DIRECTIONS = ("increase", "decrease")

#: Overall comparative outcomes.  ``not_measurable`` is one of them and is not
#: a pass: an unknown is an unknown, and Stage 8 never reads one as improvement.
VERDICTS = ("improved", "not_improved", "regressed", "not_measurable")

EXPERIMENT_STATES = ("admitted", "baseline_measured", "mission_created",
                     "candidate_sealed", "evaluated", "promotion_staged",
                     "closed")

#: A lineage is over when its latest generation reaches one of these.  Only
#: ``accepted`` may carry a lineage forward.
DISPOSITIONS = ("accepted", "rejected", "abandoned", "superseded")
TERMINAL = frozenset(DISPOSITIONS)

#: Mission states that mean the mission is over.  ``store.TERMINAL`` plus
#: ``escalated``, which stops a mission without ever being one of the four.
MISSION_SETTLED = frozenset({"completed", "refused", "failed", "cancelled",
                             "escalated"})

#: Only the lowest risk class may stage a promotion without a person.  This is
#: not a default that can be flipped from a candidate: the check reads the
#: policy row, and no method here takes an experiment and writes a policy.
AUTONOMOUS_RISK_CLASSES = frozenset({"low"})
RISK_CLASSES = ("low", "medium", "high")

DEFAULT_GENERATION_CEILING = 3
DEFAULT_EXPERIMENT_BUDGET = 6
DEFAULT_CONCURRENT_EXPERIMENTS = 1
DEFAULT_COOLDOWN_SECONDS = 900.0
DEFAULT_MAINTENANCE_PRESSURE = 3
DEFAULT_INCIDENT_PRESSURE = 3
DEFAULT_COST_PRESSURE_RATIO = 0.75


SCHEMA = """
CREATE TABLE IF NOT EXISTS improvement_policies (
  project_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL,
  improvement_classes_json TEXT NOT NULL,
  trigger_classes_json TEXT NOT NULL,
  environment_classes_json TEXT NOT NULL,
  protected_surfaces_json TEXT NOT NULL,
  self_target_repositories_json TEXT NOT NULL,
  generation_ceiling INTEGER NOT NULL,
  experiment_budget INTEGER NOT NULL,
  concurrent_experiments INTEGER NOT NULL,
  cooldown_seconds REAL NOT NULL,
  risk_class TEXT NOT NULL,
  maintenance_pressure INTEGER NOT NULL,
  incident_pressure INTEGER NOT NULL,
  cost_pressure_ratio REAL NOT NULL,
  execution_mode TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  policy_digest TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS objectives (
  objective_ref TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  improvement_class TEXT NOT NULL,
  statement TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  authority TEXT NOT NULL,
  objective_version TEXT NOT NULL,
  objective_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
  experiment_ref TEXT PRIMARY KEY,
  lineage_ref TEXT NOT NULL,
  parent_ref TEXT,
  generation INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  objective_ref TEXT NOT NULL,
  objective_digest TEXT NOT NULL,
  improvement_class TEXT NOT NULL,
  trigger_class TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  target_repository TEXT NOT NULL,
  baseline_sha TEXT NOT NULL,
  rollback_target TEXT NOT NULL,
  self_target INTEGER NOT NULL,
  isolation_ref TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  policy_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  disposition TEXT,
  baseline_json TEXT,
  mission_ref TEXT,
  idempotency_key TEXT,
  candidate_sha TEXT,
  producer_identity TEXT,
  change_set_json TEXT,
  evaluator_identity TEXT,
  candidate_json TEXT,
  comparison_json TEXT,
  verdict TEXT,
  bundle_ref TEXT,
  promotion_deployment_id TEXT,
  promotion_environment_id TEXT,
  reverted_to TEXT,
  admitted_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (objective_ref, generation)
);
CREATE INDEX IF NOT EXISTS experiments_by_lineage
  ON experiments(lineage_ref, generation);
CREATE INDEX IF NOT EXISTS experiments_by_project
  ON experiments(project_id, state);
CREATE TABLE IF NOT EXISTS improvement_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  experiment_ref TEXT,
  kind TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS improvement_events_no_update
BEFORE UPDATE ON improvement_events
BEGIN SELECT RAISE(ABORT, 'improvement events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS improvement_events_no_delete
BEFORE DELETE ON improvement_events
BEGIN SELECT RAISE(ABORT, 'improvement events are append-only'); END;
"""


class PolicyError(ValueError):
    """An improvement declaration the Controller will not store."""


class ImprovementRefusal(Exception):
    """A bounded stop, carrying the code and why.

    Named with an ``IMPROVEMENT_`` prefix throughout.  ``production.py`` owns
    ``PRODUCTION_*``, ``maintenance.py`` owns ``MAINTENANCE_*`` and the host
    layer owns bare codes of its own; a layer that refuses under a name a
    neighbour also uses cannot be routed on.
    """

    def __init__(self, code: str, detail: str, *, experiment_ref: str | None = None,
                 project_id: str | None = None) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.experiment_ref = experiment_ref
        self.project_id = project_id

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "experiment_ref": self.experiment_ref,
                "project_id": self.project_id}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def experiment_reference(objective_ref: str, generation: int,
                         baseline_sha: str) -> str:
    """One objective, one generation, one baseline, one experiment, forever.

    Derived rather than allocated, so a replay after a restart computes the
    same value and collides with the row already there instead of opening a
    second experiment against the same pinned baseline.
    """

    return "imp_%s" % digest({"objective_ref": objective_ref,
                              "generation": int(generation),
                              "baseline_sha": baseline_sha})[:24]


@dataclass(frozen=True)
class Metric:
    """One frozen, directional, objective measurement.

    ``min_delta_ratio`` is a *relative* requirement, so a metric whose baseline
    is zero cannot express one -- there is no ratio against zero.  That case
    records ``not_measurable`` rather than quietly passing on a sign change,
    which is the same rule the rest of the corpus applies to an absent fact.
    """

    metric_id: str
    direction: str
    role: str = "objective"
    min_delta_ratio: float = 0.0
    tolerance_ratio: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.metric_id, str) or not self.metric_id:
            raise PolicyError("a metric needs an identifier")
        if self.direction not in METRIC_DIRECTIONS:
            raise PolicyError("metric %s direction must be one of %s"
                              % (self.metric_id, ", ".join(METRIC_DIRECTIONS)))
        if self.role not in METRIC_ROLES:
            raise PolicyError("metric %s role must be one of %s"
                              % (self.metric_id, ", ".join(METRIC_ROLES)))
        if self.min_delta_ratio < 0 or self.tolerance_ratio < 0:
            raise PolicyError("metric %s ratios must not be negative"
                              % self.metric_id)
        if self.role == "objective" and self.tolerance_ratio:
            raise PolicyError(
                "metric %s is an objective metric; a tolerance for getting "
                "worse is a non-regression concept" % self.metric_id)
        if self.role == "non_regression" and self.min_delta_ratio:
            raise PolicyError(
                "metric %s is a non-regression metric; requiring it to improve "
                "makes it an objective metric" % self.metric_id)

    def as_row(self) -> dict[str, Any]:
        return {"metric_id": self.metric_id, "direction": self.direction,
                "role": self.role, "min_delta_ratio": self.min_delta_ratio,
                "tolerance_ratio": self.tolerance_ratio}


@dataclass(frozen=True)
class Objective:
    """A versioned improvement intent, authored by the Owner.

    This is the only container in Stage 8 that holds a human sentence, and it
    exists at the Owner's own act rather than at any point an experiment can
    reach.  ``authority`` is fixed at ``owner`` for that reason: there is no
    other admissible value, so no caller can register an objective on behalf of
    a model, an advisory service or a candidate.
    """

    objective_ref: str
    project_id: str
    improvement_class: str
    statement: str
    metrics: tuple[Metric, ...]
    authority: str = "owner"
    objective_version: str = "unset"

    def __post_init__(self) -> None:
        for name in ("objective_ref", "project_id", "statement"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PolicyError("%s is required" % name)
        if len(self.statement) > 512:
            raise PolicyError("an objective statement is a sentence, not a brief")
        if self.improvement_class not in IMPROVEMENT_CLASSES:
            raise PolicyError(
                "improvement class %r is not one of %s; repairing a known "
                "failure is Stage 7's contract and is not an improvement"
                % (self.improvement_class, ", ".join(IMPROVEMENT_CLASSES)))
        if self.authority != "owner":
            raise PolicyError(
                "an objective is an Owner act; %r cannot author one"
                % (self.authority,))
        if not self.metrics:
            raise PolicyError(
                "an objective with no metric cannot be shown to have been met; "
                "state the metrics or do not register it")
        seen = set()
        for metric in self.metrics:
            if metric.metric_id in seen:
                raise PolicyError("metric %s is declared twice" % metric.metric_id)
            seen.add(metric.metric_id)
        if not any(metric.role == "objective" for metric in self.metrics):
            raise PolicyError(
                "every metric is a non-regression metric; nothing here could "
                "ever be an improvement")

    @property
    def objective_digest(self) -> str:
        """What makes two objectives *the same* objective.

        The experiment pins this value.  Editing the statement, the class, the
        metrics or the version all move it, so an experiment admitted under one
        objective can never be compared under another.
        """

        return digest({"objective_ref": self.objective_ref,
                       "project_id": self.project_id,
                       "improvement_class": self.improvement_class,
                       "statement": self.statement,
                       "objective_version": self.objective_version,
                       "metrics": [metric.as_row() for metric in self.metrics]})

    def as_row(self) -> dict[str, Any]:
        return {"objective_ref": self.objective_ref,
                "project_id": self.project_id,
                "improvement_class": self.improvement_class,
                "statement": self.statement,
                "authority": self.authority,
                "objective_version": self.objective_version,
                "objective_digest": self.objective_digest,
                "metrics": [metric.as_row() for metric in self.metrics]}

    def metric(self, metric_id: str) -> Metric | None:
        for metric in self.metrics:
            if metric.metric_id == metric_id:
                return metric
        return None


@dataclass(frozen=True)
class ImprovementPolicy:
    """One project's autonomous-improvement envelope, written by the Owner.

    Every field is a bound and every bound ends a lineage rather than delaying
    it.  ``experiment_budget`` and ``generation_ceiling`` both count against
    ``policy_version``: when either is spent, improvement for the project stops
    until the Owner writes a new version, which is a deliberate human act and
    not a timer.
    """

    project_id: str
    enabled: bool = False
    improvement_classes: tuple[str, ...] = IMPROVEMENT_CLASSES
    trigger_classes: tuple[str, ...] = TRIGGER_CLASSES
    environment_classes: tuple[str, ...] = ("local-sim", "staging")
    protected_surfaces: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    self_target_repositories: tuple[str, ...] = ()
    generation_ceiling: int = DEFAULT_GENERATION_CEILING
    experiment_budget: int = DEFAULT_EXPERIMENT_BUDGET
    concurrent_experiments: int = DEFAULT_CONCURRENT_EXPERIMENTS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    risk_class: str = "low"
    maintenance_pressure: int = DEFAULT_MAINTENANCE_PRESSURE
    incident_pressure: int = DEFAULT_INCIDENT_PRESSURE
    cost_pressure_ratio: float = DEFAULT_COST_PRESSURE_RATIO
    execution_mode: str = "fixture"
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise PolicyError("project_id is required")
        if not self.improvement_classes:
            raise PolicyError("state at least one improvement class")
        for name in self.improvement_classes:
            if name not in IMPROVEMENT_CLASSES:
                raise PolicyError("improvement class %r is not one of %s"
                                  % (name, ", ".join(IMPROVEMENT_CLASSES)))
        if not self.trigger_classes:
            raise PolicyError("state at least one trigger class")
        for name in self.trigger_classes:
            if name not in TRIGGER_CLASSES:
                raise PolicyError("trigger class %r is not one of %s"
                                  % (name, ", ".join(TRIGGER_CLASSES)))
        if not self.environment_classes:
            raise PolicyError(
                "an improvement policy with no environment class stages "
                "nothing; state the classes or leave improvement disabled")
        for name in self.environment_classes:
            if name not in production.ENVIRONMENT_CLASSES:
                raise PolicyError("environment class %r is not one of %s"
                                  % (name, ", ".join(production.ENVIRONMENT_CLASSES)))
            if name in production.GATED_CLASSES:
                # Not a default that can be flipped.  Autonomous improvement
                # has no representation in which it releases to a gated class,
                # so the production gate cannot be configured away from here.
                raise PolicyError(
                    "autonomous improvement cannot be scoped to a %s "
                    "environment; releasing there is approved by a person" % name)
        self._check_surfaces()
        if self.risk_class not in RISK_CLASSES:
            raise PolicyError("risk_class must be one of %s"
                              % ", ".join(RISK_CLASSES))
        if self.execution_mode not in routing.EXECUTION_MODES:
            raise PolicyError("execution_mode must be one of %s"
                              % ", ".join(sorted(routing.EXECUTION_MODES)))
        for name, value in (("generation_ceiling", self.generation_ceiling),
                            ("experiment_budget", self.experiment_budget),
                            ("concurrent_experiments", self.concurrent_experiments),
                            ("maintenance_pressure", self.maintenance_pressure),
                            ("incident_pressure", self.incident_pressure)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError("%s must be a non-negative integer" % name)
        if self.generation_ceiling < 1:
            raise PolicyError(
                "a generation ceiling below 1 would admit a lineage that can "
                "never run; disable improvement instead")
        if self.cooldown_seconds < 0:
            raise PolicyError("cooldown_seconds must not be negative")
        if not 0 < self.cost_pressure_ratio <= 1:
            raise PolicyError(
                "cost_pressure_ratio is a fraction of the recorded budget "
                "ceiling and must fall in (0, 1]")

    def _check_surfaces(self) -> None:
        """Every mandatory surface, present and actually covering something.

        Two failures are refused here rather than at admission, because a
        policy that reached durable state with a hole in it would make every
        later check a formality: a missing surface name, and a surface name
        declared with no path prefix under it.
        """

        if not isinstance(self.protected_surfaces, Mapping):
            raise PolicyError("protected_surfaces maps a surface name to path prefixes")
        missing = [name for name in MANDATORY_SURFACES
                   if name not in self.protected_surfaces]
        if missing:
            raise PolicyError(
                "protected surfaces %s are not declared; an improvement policy "
                "may add surfaces but may never drop one" % ", ".join(missing))
        for name, prefixes in self.protected_surfaces.items():
            if isinstance(prefixes, str) or not prefixes:
                raise PolicyError(
                    "protected surface %r covers nothing; a surface declared "
                    "with no path prefix protects nothing at all" % name)
            for prefix in prefixes:
                if not isinstance(prefix, str) or not prefix.strip():
                    raise PolicyError("protected surface %r has an empty prefix" % name)

    @property
    def policy_digest(self) -> str:
        """What the experiment pins so a generation cannot move its own bounds."""

        return digest(self.as_row())

    def as_row(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "enabled": self.enabled,
                "improvement_classes": list(self.improvement_classes),
                "trigger_classes": list(self.trigger_classes),
                "environment_classes": list(self.environment_classes),
                "protected_surfaces": {name: list(prefixes) for name, prefixes
                                       in sorted(self.protected_surfaces.items())},
                "self_target_repositories": list(self.self_target_repositories),
                "generation_ceiling": self.generation_ceiling,
                "experiment_budget": self.experiment_budget,
                "concurrent_experiments": self.concurrent_experiments,
                "cooldown_seconds": self.cooldown_seconds,
                "risk_class": self.risk_class,
                "maintenance_pressure": self.maintenance_pressure,
                "incident_pressure": self.incident_pressure,
                "cost_pressure_ratio": self.cost_pressure_ratio,
                "execution_mode": self.execution_mode,
                "policy_version": self.policy_version}

    def surface_for(self, path: str) -> str | None:
        """The protected surface a changed path falls under, if any.

        Prefix matching rather than a pattern language, deliberately.  A
        pattern language is a thing an improvement could later be taught to
        write around; a prefix either contains the path or does not.
        """

        for name in sorted(self.protected_surfaces):
            for prefix in self.protected_surfaces[name]:
                if path == prefix or path.startswith(prefix):
                    return name
        return None


@dataclass(frozen=True)
class MetricOutcome:
    """One metric's comparative reading, with its absence spelled out."""

    metric_id: str
    role: str
    direction: str
    baseline: Any
    candidate: Any
    delta_ratio: Any
    verdict: str

    def as_row(self) -> dict[str, Any]:
        return {"metric_id": self.metric_id, "role": self.role,
                "direction": self.direction, "baseline": self.baseline,
                "candidate": self.candidate, "delta_ratio": self.delta_ratio,
                "verdict": self.verdict}


def compare_metric(metric: Metric, baseline: Any, candidate: Any) -> MetricOutcome:
    """One metric, one reading, no interpretation of an unknown.

    Four things are all recorded as ``not_measurable`` rather than as a
    failure or a pass: a missing baseline, a missing candidate value, a
    non-numeric reading, and a relative requirement stated against a zero
    baseline.  None of them is evidence that anything got better, and Stage 8
    treats "we could not tell" as exactly that.
    """

    if not _numeric(baseline) or not _numeric(candidate):
        return MetricOutcome(metric.metric_id, metric.role, metric.direction,
                             _reading(baseline), _reading(candidate),
                             "not_measurable", "not_measurable")
    base, cand = float(baseline), float(candidate)
    gain = cand - base if metric.direction == "increase" else base - cand
    if base == 0:
        if metric.role == "objective" and metric.min_delta_ratio > 0:
            # A relative requirement has no meaning against zero.  Saying so is
            # more useful than inventing a denominator.
            return MetricOutcome(metric.metric_id, metric.role, metric.direction,
                                 base, cand, "not_measurable", "not_measurable")
        ratio: Any = "not_measurable"
    else:
        ratio = gain / abs(base)
    if metric.role == "objective":
        if isinstance(ratio, float):
            verdict = "improved" if ratio >= metric.min_delta_ratio and gain > 0 \
                else "not_improved"
        else:
            verdict = "improved" if gain > 0 else "not_improved"
    else:
        if isinstance(ratio, float):
            verdict = "improved" if ratio >= -metric.tolerance_ratio else "regressed"
        else:
            verdict = "improved" if gain >= 0 else "regressed"
    return MetricOutcome(metric.metric_id, metric.role, metric.direction,
                         base, cand, ratio, verdict)


def compare(objective: Objective, baseline: Mapping[str, Any],
            candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The whole comparative reading, and the one verdict it produces.

    The order the three failing outcomes are checked in is the safety
    property.  A regression on any non-regression metric ends it regardless of
    how well the objective metrics did, because a policy that trades a
    protected property for a headline number is precisely the goal-gaming this
    contract exists to prevent.  An unmeasurable objective metric comes next,
    because an unknown is never an improvement.  Only a reading where every
    objective metric cleared its own stated threshold is ``improved``.
    """

    outcomes = [compare_metric(metric, baseline.get(metric.metric_id),
                               candidate.get(metric.metric_id))
                for metric in objective.metrics]
    regressed = [item for item in outcomes if item.verdict == "regressed"]
    unmeasured = [item for item in outcomes
                  if item.role == "objective" and item.verdict == "not_measurable"]
    unmet = [item for item in outcomes
             if item.role == "objective" and item.verdict == "not_improved"]
    if regressed:
        verdict = "regressed"
    elif unmeasured:
        verdict = "not_measurable"
    elif unmet:
        verdict = "not_improved"
    else:
        verdict = "improved"
    return {"verdict": verdict,
            "objective_digest": objective.objective_digest,
            "metrics": [item.as_row() for item in outcomes],
            "regressed": [item.metric_id for item in regressed],
            "unmeasured": [item.metric_id for item in unmeasured],
            "unmet": [item.metric_id for item in unmet]}


class ImprovementPlane:
    """Durable Stage-8 state, on the mission store's own connection.

    It borrows the store for the same reason :class:`production.ProductionLedger`
    and :class:`maintenance.MaintenancePlane` do: admitting an experiment and
    submitting its mission have to commit or fail together, and two database
    files cannot do that.  It does not import the maintenance module -- it
    reads the ``repairs`` table when a policy admits a maintenance-history
    trigger, and reading a table is not a dependency edge between two control
    planes that must not drive each other.
    """

    def __init__(self, store, ledger: production.ProductionLedger) -> None:
        self._store = store
        self._ledger = ledger
        with store.transaction() as db:
            db.executescript(SCHEMA)

    # -- policy ------------------------------------------------------------ #

    def set_policy(self, policy: ImprovementPolicy) -> dict[str, Any]:
        now = time.time()
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO improvement_policies (project_id, enabled,"
                " improvement_classes_json, trigger_classes_json,"
                " environment_classes_json, protected_surfaces_json,"
                " self_target_repositories_json, generation_ceiling,"
                " experiment_budget, concurrent_experiments, cooldown_seconds,"
                " risk_class, maintenance_pressure, incident_pressure,"
                " cost_pressure_ratio, execution_mode, policy_version,"
                " policy_digest, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,"
                " improvement_classes_json=excluded.improvement_classes_json,"
                " trigger_classes_json=excluded.trigger_classes_json,"
                " environment_classes_json=excluded.environment_classes_json,"
                " protected_surfaces_json=excluded.protected_surfaces_json,"
                " self_target_repositories_json=excluded.self_target_repositories_json,"
                " generation_ceiling=excluded.generation_ceiling,"
                " experiment_budget=excluded.experiment_budget,"
                " concurrent_experiments=excluded.concurrent_experiments,"
                " cooldown_seconds=excluded.cooldown_seconds,"
                " risk_class=excluded.risk_class,"
                " maintenance_pressure=excluded.maintenance_pressure,"
                " incident_pressure=excluded.incident_pressure,"
                " cost_pressure_ratio=excluded.cost_pressure_ratio,"
                " execution_mode=excluded.execution_mode,"
                " policy_version=excluded.policy_version,"
                " policy_digest=excluded.policy_digest,"
                " updated_at=excluded.updated_at",
                (policy.project_id, int(policy.enabled),
                 canonical_json(list(policy.improvement_classes)),
                 canonical_json(list(policy.trigger_classes)),
                 canonical_json(list(policy.environment_classes)),
                 canonical_json({name: list(prefixes) for name, prefixes
                                 in policy.protected_surfaces.items()}),
                 canonical_json(list(policy.self_target_repositories)),
                 policy.generation_ceiling, policy.experiment_budget,
                 policy.concurrent_experiments, policy.cooldown_seconds,
                 policy.risk_class, policy.maintenance_pressure,
                 policy.incident_pressure, policy.cost_pressure_ratio,
                 policy.execution_mode, policy.policy_version,
                 policy.policy_digest, now, now))
            self._append(db, "policy_set", policy.project_id,
                         detail=policy.as_row())
        return policy.as_row()

    def policy(self, project_id: str) -> ImprovementPolicy | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM improvement_policies WHERE project_id=?",
                (project_id,)).fetchone()
        return None if row is None else _policy_from_row(row)

    def set_enabled(self, project_id: str, enabled: bool) -> dict[str, Any]:
        current = self.policy(project_id)
        if current is None:
            raise PolicyError("no improvement policy is declared for %s" % project_id)
        return self.set_policy(ImprovementPolicy(
            **{**_policy_kwargs(current), "enabled": enabled}))

    # -- objectives -------------------------------------------------------- #

    def register_objective(self, objective: Objective) -> dict[str, Any]:
        """The Owner's own act, and the only door human intent comes through.

        Re-registering the same reference with different content is allowed and
        deliberate -- an Owner may revise an objective -- but it moves the
        digest, so every experiment already admitted under the old content is
        pinned to a value this row no longer produces.  Revising an objective
        therefore ends the lineage under it rather than silently retargeting a
        running experiment.
        """

        now = time.time()
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO objectives (objective_ref, project_id,"
                " improvement_class, statement, metrics_json, authority,"
                " objective_version, objective_digest, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,'active',?,?)"
                " ON CONFLICT(objective_ref) DO UPDATE SET"
                " project_id=excluded.project_id,"
                " improvement_class=excluded.improvement_class,"
                " statement=excluded.statement,"
                " metrics_json=excluded.metrics_json,"
                " objective_version=excluded.objective_version,"
                " objective_digest=excluded.objective_digest,"
                " updated_at=excluded.updated_at",
                (objective.objective_ref, objective.project_id,
                 objective.improvement_class, objective.statement,
                 canonical_json([metric.as_row() for metric in objective.metrics]),
                 objective.authority, objective.objective_version,
                 objective.objective_digest, now, now))
            self._append(db, "objective_registered", objective.project_id,
                         detail=objective.as_row())
        return objective.as_row()

    def objective(self, objective_ref: str) -> Objective | None:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM objectives WHERE objective_ref=?",
                             (objective_ref,)).fetchone()
        return None if row is None else _objective_from_row(row)

    def retire_objective(self, objective_ref: str) -> dict[str, Any]:
        objective = self.objective(objective_ref)
        if objective is None:
            raise PolicyError("no objective %r is registered" % objective_ref)
        with self._store.transaction() as db:
            db.execute("UPDATE objectives SET state='retired', updated_at=?"
                       " WHERE objective_ref=?", (time.time(), objective_ref))
            self._append(db, "objective_retired", objective.project_id,
                         detail={"objective_ref": objective_ref})
        return {"objective_ref": objective_ref, "state": "retired"}

    # -- admission --------------------------------------------------------- #

    def admit_experiment(self, objective_ref: str, trigger_class: str,
                         source_ref: str, *, target_repository: str,
                         baseline_sha: str, isolation_ref: str) -> dict[str, Any]:
        """Open generation 1 of a lineage, or refuse.

        The whole input is an objective this Owner registered, a trigger class,
        a reference into this Controller's own tables, and the three facts that
        pin where the work happens.  There is no field for a description, a
        prompt, a plan or an opinion, so the question "could a model authorize
        an experiment here?" has a structural answer rather than a policy one.
        """

        return self._admit(objective_ref, trigger_class, source_ref,
                           target_repository=target_repository,
                           baseline_sha=baseline_sha,
                           isolation_ref=isolation_ref,
                           parent=None)

    def open_generation(self, parent_ref: str, *, baseline_sha: str,
                        isolation_ref: str) -> dict[str, Any]:
        """Generation N+1, only from an accepted N under an unchanged policy.

        Six conditions hold recursion finite, and each of them is a property of
        rows already written rather than of anything this call is told.  The
        caller cannot raise a ceiling, change an evaluator, widen a permission
        or alter the policy version that admitted the parent, because none of
        those is an argument here and no method in this module takes an
        experiment and writes a policy.
        """

        parent = self._experiment(parent_ref)
        policy = self._require_policy(parent["project_id"], parent_ref)
        if parent["disposition"] != "accepted":
            raise ImprovementRefusal(
                "IMPROVEMENT_PARENT_NOT_ACCEPTED",
                "generation %d is %s; only an accepted generation carries a "
                "lineage forward"
                % (parent["generation"], parent["disposition"] or "still open"),
                experiment_ref=parent_ref, project_id=parent["project_id"])
        if parent["policy_digest"] != policy.policy_digest:
            raise ImprovementRefusal(
                "IMPROVEMENT_POLICY_CHANGED",
                "generation %d was admitted under a policy that has since "
                "changed; a lineage does not continue across its own rules"
                % parent["generation"],
                experiment_ref=parent_ref, project_id=parent["project_id"])
        with self._store.transaction() as db:
            in_flight = db.execute(
                "SELECT COUNT(*) AS n FROM experiments WHERE lineage_ref=?"
                " AND disposition IS NULL", (parent["lineage_ref"],)).fetchone()["n"]
        if in_flight:
            raise ImprovementRefusal(
                "IMPROVEMENT_GENERATION_IN_FLIGHT",
                "lineage %s still has %d open generation(s); a generation "
                "cannot spawn another while it is running"
                % (parent["lineage_ref"], in_flight),
                experiment_ref=parent_ref, project_id=parent["project_id"])
        if baseline_sha == parent["baseline_sha"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_NOT_ADVANCED",
                "generation %d already ran against %s; a new generation pins "
                "the baseline its parent produced"
                % (parent["generation"], baseline_sha),
                experiment_ref=parent_ref, project_id=parent["project_id"])
        if parent["candidate_sha"] and baseline_sha != parent["candidate_sha"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_NOT_ADVANCED",
                "the accepted candidate of generation %d is %s; a successor "
                "pins that, not %s"
                % (parent["generation"], parent["candidate_sha"], baseline_sha),
                experiment_ref=parent_ref, project_id=parent["project_id"])
        return self._admit(parent["objective_ref"], parent["trigger_class"],
                           parent["source_ref"],
                           target_repository=parent["target_repository"],
                           baseline_sha=baseline_sha,
                           isolation_ref=isolation_ref,
                           parent=parent)

    def _admit(self, objective_ref: str, trigger_class: str, source_ref: str, *,
               target_repository: str, baseline_sha: str, isolation_ref: str,
               parent) -> dict[str, Any]:
        if trigger_class not in TRIGGER_CLASSES:
            raise ImprovementRefusal(
                "IMPROVEMENT_TRIGGER_CLASS_UNKNOWN",
                "%r is not a trigger class; the admitted classes are %s"
                % (trigger_class, ", ".join(TRIGGER_CLASSES)))
        generation = 1 if parent is None else parent["generation"] + 1
        experiment_ref = experiment_reference(objective_ref, generation, baseline_sha)
        refusal = None
        row = None
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM experiments WHERE experiment_ref=?",
                (experiment_ref,)).fetchone()
            if existing is not None:
                # The same objective, generation and baseline, replayed.  This
                # is the same experiment, not a second one, and saying so is the
                # whole idempotency mechanism.
                return dict(existing)
            refusal, row = self._admission(
                db, objective_ref, trigger_class, source_ref, experiment_ref,
                target_repository, baseline_sha, isolation_ref, generation, parent)
            if refusal is not None:
                self._append(db, "experiment_refused",
                             refusal.project_id or "unbound",
                             experiment_ref=experiment_ref, detail=refusal.as_row())
        if refusal is not None:
            raise refusal
        return row

    def _admission(self, db, objective_ref: str, trigger_class: str,
                   source_ref: str, experiment_ref: str, target_repository: str,
                   baseline_sha: str, isolation_ref: str, generation: int, parent):
        """Every bound, checked in one place, before anything durable is written."""

        objective_row = db.execute(
            "SELECT * FROM objectives WHERE objective_ref=?",
            (objective_ref,)).fetchone()
        if objective_row is None:
            return ImprovementRefusal(
                "IMPROVEMENT_OBJECTIVE_UNKNOWN",
                "no objective %r is registered; an experiment is bound to an "
                "Owner objective, never to a suggestion" % objective_ref,
                experiment_ref=experiment_ref), None
        if objective_row["state"] != "active":
            return ImprovementRefusal(
                "IMPROVEMENT_OBJECTIVE_RETIRED",
                "objective %s is %s" % (objective_ref, objective_row["state"]),
                experiment_ref=experiment_ref,
                project_id=objective_row["project_id"]), None
        objective = _objective_from_row(objective_row)
        project_id = objective.project_id

        policy_row = db.execute(
            "SELECT * FROM improvement_policies WHERE project_id=?",
            (project_id,)).fetchone()
        if policy_row is None or not policy_row["enabled"]:
            return ImprovementRefusal(
                "IMPROVEMENT_DISABLED",
                "autonomous improvement is not enabled for %s" % project_id,
                experiment_ref=experiment_ref, project_id=project_id), None
        policy = _policy_from_row(policy_row)

        if trigger_class not in policy.trigger_classes:
            return ImprovementRefusal(
                "IMPROVEMENT_TRIGGER_CLASS_NOT_ADMITTED",
                "%s does not admit %s triggers" % (project_id, trigger_class),
                experiment_ref=experiment_ref, project_id=project_id), None
        if objective.improvement_class not in policy.improvement_classes:
            return ImprovementRefusal(
                "IMPROVEMENT_CLASS_NOT_ADMITTED",
                "%s does not admit %s improvements"
                % (project_id, objective.improvement_class),
                experiment_ref=experiment_ref, project_id=project_id), None

        stopped = db.execute("SELECT emergency_stop FROM portfolio WHERE id=1").fetchone()
        if stopped is not None and stopped["emergency_stop"]:
            return ImprovementRefusal(
                "IMPROVEMENT_EMERGENCY_STOP",
                "the portfolio is under an emergency stop",
                experiment_ref=experiment_ref, project_id=project_id), None
        project_row = db.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project_row is None or project_row["state"] not in portfolio.ADMITTING:
            state = "unregistered" if project_row is None else project_row["state"]
            return ImprovementRefusal(
                "IMPROVEMENT_PROJECT_NOT_ADMITTING",
                "project %s is %s; improvement does not create work a paused "
                "project would not accept" % (project_id, state),
                experiment_ref=experiment_ref, project_id=project_id), None

        evidence = self._trigger_evidence(db, policy, project_id, trigger_class,
                                          source_ref, objective_ref, project_row)
        if isinstance(evidence, ImprovementRefusal):
            evidence.experiment_ref = experiment_ref
            return evidence, None

        self_target = target_repository in policy.self_target_repositories
        if not isolation_ref or not isolation_ref.strip():
            return ImprovementRefusal(
                "IMPROVEMENT_ISOLATION_REQUIRED",
                "an experiment names the disposable lane it runs in; there is "
                "no representation here for working in the live checkout",
                experiment_ref=experiment_ref, project_id=project_id), None
        if isolation_ref == target_repository:
            return ImprovementRefusal(
                "IMPROVEMENT_ISOLATION_REQUIRED",
                "the isolation reference is the target repository itself, "
                "which is not isolation",
                experiment_ref=experiment_ref, project_id=project_id), None

        spent = db.execute(
            "SELECT COUNT(*) AS n FROM experiments WHERE project_id=?"
            " AND policy_version=?",
            (project_id, policy.policy_version)).fetchone()["n"]
        if spent >= policy.experiment_budget:
            return ImprovementRefusal(
                "IMPROVEMENT_BUDGET_EXHAUSTED",
                "%s has spent its experiment budget of %d under policy version "
                "%r; a new budget is an Owner decision, not a timer"
                % (project_id, policy.experiment_budget, policy.policy_version),
                experiment_ref=experiment_ref, project_id=project_id), None

        open_now = db.execute(
            "SELECT COUNT(*) AS n FROM experiments WHERE project_id=?"
            " AND disposition IS NULL", (project_id,)).fetchone()["n"]
        if open_now >= policy.concurrent_experiments:
            return ImprovementRefusal(
                "IMPROVEMENT_CONCURRENCY_EXCEEDED",
                "%s already has %d experiment(s) open" % (project_id, open_now),
                experiment_ref=experiment_ref, project_id=project_id), None

        if generation > policy.generation_ceiling:
            return ImprovementRefusal(
                "IMPROVEMENT_GENERATION_CEILING_REACHED",
                "generation %d exceeds the ceiling of %d; recursion stops here "
                "rather than continuing under a raised bound"
                % (generation, policy.generation_ceiling),
                experiment_ref=experiment_ref, project_id=project_id), None

        prior = db.execute(
            "SELECT admitted_at FROM experiments WHERE objective_ref=?"
            " ORDER BY admitted_at", (objective_ref,)).fetchall()
        if prior:
            waited = time.time() - prior[-1]["admitted_at"]
            if waited < policy.cooldown_seconds:
                return ImprovementRefusal(
                    "IMPROVEMENT_COOLDOWN_ACTIVE",
                    "the previous generation of this objective was admitted "
                    "%.1fs ago and the cooldown is %.1fs"
                    % (waited, policy.cooldown_seconds),
                    experiment_ref=experiment_ref, project_id=project_id), None

        lineage_ref = experiment_ref if parent is None else parent["lineage_ref"]
        rollback_target = baseline_sha
        now = time.time()
        db.execute(
            "INSERT INTO experiments (experiment_ref, lineage_ref, parent_ref,"
            " generation, project_id, objective_ref, objective_digest,"
            " improvement_class, trigger_class, source_kind, source_ref,"
            " target_repository, baseline_sha, rollback_target, self_target,"
            " isolation_ref, risk_class, policy_version, policy_digest, state,"
            " admitted_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'admitted',?,?)",
            (experiment_ref, lineage_ref,
             None if parent is None else parent["experiment_ref"], generation,
             project_id, objective_ref, objective.objective_digest,
             objective.improvement_class, trigger_class,
             SOURCE_TABLE[trigger_class], source_ref, target_repository,
             baseline_sha, rollback_target, int(self_target), isolation_ref,
             policy.risk_class, policy.policy_version, policy.policy_digest,
             now, now))
        self._append(db, "experiment_admitted", project_id,
                     experiment_ref=experiment_ref, to_state="admitted",
                     detail={"objective_ref": objective_ref,
                             "generation": generation,
                             "lineage_ref": lineage_ref,
                             "trigger_class": trigger_class,
                             "source_ref": source_ref,
                             "baseline_sha": baseline_sha,
                             "self_target": self_target,
                             "isolation_ref": isolation_ref,
                             "policy_version": policy.policy_version,
                             "trigger_evidence": evidence})
        row = db.execute("SELECT * FROM experiments WHERE experiment_ref=?",
                         (experiment_ref,)).fetchone()
        return None, dict(row)

    def _trigger_evidence(self, db, policy: ImprovementPolicy, project_id: str,
                          trigger_class: str, source_ref: str,
                          objective_ref: str, project_row):
        """The measured fact behind the trigger, or a refusal because there is none.

        This is where "telemetry is evidence, not authority" is enforced for
        Stage 8.  Three of the four classes require a *count* or a *ratio* over
        rows this Controller wrote, so a single event cannot open an
        experiment, and an unmeasurable reading refuses rather than passing.
        The fourth is the Owner saying so directly, which is the only authority
        that needs no measurement behind it.
        """

        if trigger_class == "owner_objective":
            if source_ref != objective_ref:
                return ImprovementRefusal(
                    "IMPROVEMENT_SOURCE_MISMATCH",
                    "an owner_objective trigger names its own objective; %r is "
                    "not %r" % (source_ref, objective_ref),
                    project_id=project_id)
            return {"class": "owner_objective", "authority": "owner",
                    "objective_ref": objective_ref}

        if trigger_class == "maintenance_history":
            if not _table_exists(db, "repairs"):
                return ImprovementRefusal(
                    "IMPROVEMENT_SOURCE_UNKNOWN",
                    "no maintenance history is recorded on this Controller",
                    project_id=project_id)
            rows = db.execute(
                "SELECT disposition FROM repairs WHERE project_id=? AND signature=?",
                (project_id, source_ref)).fetchall()
            closed = [item for item in rows if item["disposition"]]
            if len(closed) < policy.maintenance_pressure:
                return ImprovementRefusal(
                    "IMPROVEMENT_TRIGGER_NOT_MEASURED",
                    "failure signature %s has %d closed repair(s) and the "
                    "admitted pressure is %d; one repair is a repair, not a "
                    "case for changing what the software does"
                    % (source_ref, len(closed), policy.maintenance_pressure),
                    project_id=project_id)
            return {"class": "maintenance_history", "signature": source_ref,
                    "closed_repairs": len(closed),
                    "pressure": policy.maintenance_pressure}

        if trigger_class == "operational_pain":
            if not _table_exists(db, "incidents"):
                return ImprovementRefusal(
                    "IMPROVEMENT_SOURCE_UNKNOWN",
                    "no incident history is recorded on this Controller",
                    project_id=project_id)
            rows = db.execute(
                "SELECT incident_ref FROM incidents WHERE project_id=?"
                " AND failing_behaviour=?", (project_id, source_ref)).fetchall()
            if len(rows) < policy.incident_pressure:
                return ImprovementRefusal(
                    "IMPROVEMENT_TRIGGER_NOT_MEASURED",
                    "%s has %d recorded incident(s) against %s and the admitted "
                    "pressure is %d"
                    % (project_id, len(rows), source_ref, policy.incident_pressure),
                    project_id=project_id)
            return {"class": "operational_pain", "failing_behaviour": source_ref,
                    "incidents": len(rows), "pressure": policy.incident_pressure}

        if source_ref != project_id:
            return ImprovementRefusal(
                "IMPROVEMENT_SOURCE_MISMATCH",
                "a cost_inefficiency trigger names its own project; %r is not %r"
                % (source_ref, project_id), project_id=project_id)
        ceiling = project_row["budget_ceiling"]
        if ceiling is None or ceiling <= 0:
            return ImprovementRefusal(
                "IMPROVEMENT_TRIGGER_NOT_MEASURED",
                "%s has no recorded budget ceiling, so cost pressure against it "
                "is not_measurable" % project_id, project_id=project_id)
        spend = self._store.portfolio_economics(project_id)
        groups = [group for group in spend["projects"]
                  if group["project_id"] == project_id]
        known = groups[0]["provider_spend"]["known_spend"] if groups else "not_measurable"
        if not _numeric(known):
            return ImprovementRefusal(
                "IMPROVEMENT_TRIGGER_NOT_MEASURED",
                "%s reports known_spend %r; an unmeasured cost is not evidence "
                "of an inefficient one" % (project_id, known),
                project_id=project_id)
        ratio = float(known) / float(ceiling)
        if ratio < policy.cost_pressure_ratio:
            return ImprovementRefusal(
                "IMPROVEMENT_TRIGGER_NOT_MEASURED",
                "%s has consumed %.3f of its budget ceiling and the admitted "
                "pressure ratio is %.3f" % (project_id, ratio,
                                            policy.cost_pressure_ratio),
                project_id=project_id)
        return {"class": "cost_inefficiency", "known_spend": float(known),
                "budget_ceiling": float(ceiling), "ratio": ratio,
                "pressure_ratio": policy.cost_pressure_ratio,
                "evidence_class": "reported_claim"}

    # -- the frozen baseline ------------------------------------------------ #

    def record_baseline(self, experiment_ref: str,
                        measurements: Mapping[str, Any]) -> dict[str, Any]:
        """Measure the pinned baseline, once, before any candidate exists.

        The order is the anti-gaming property, not a convention.  A baseline
        recorded after the candidate ran could be chosen to flatter it, so this
        refuses once a mission exists, and refuses a second recording outright.
        Every objective metric must be present: an experiment that cannot state
        where it started cannot later claim to have moved.
        """

        row = self._experiment(experiment_ref)
        self._require_open(row)
        if row["baseline_json"] is not None:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_SEALED",
                "the baseline of %s is already recorded; re-measuring it after "
                "the fact is how a comparison stops meaning anything"
                % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if row["mission_ref"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_AFTER_CANDIDATE",
                "mission %s already exists for %s; a baseline is measured "
                "before the candidate, never after it"
                % (row["mission_ref"], experiment_ref),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        objective = self._require_objective(row)
        missing = [metric.metric_id for metric in objective.metrics
                   if metric.role == "objective"
                   and not _numeric(measurements.get(metric.metric_id))]
        if missing:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_NOT_MEASURABLE",
                "objective metric(s) %s have no numeric baseline; an unknown "
                "starting point cannot become an improvement"
                % ", ".join(sorted(missing)),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        values = {key: value for key, value in measurements.items()}
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET baseline_json=?, state='baseline_measured',"
                " updated_at=? WHERE experiment_ref=? AND disposition IS NULL",
                (canonical_json(values), time.time(), experiment_ref))
            self._append(db, "baseline_measured", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="baseline_measured",
                         detail={"metrics": sorted(values),
                                 "objective_digest": row["objective_digest"]})
        return self.lineage(experiment_ref)

    # -- the candidate mission --------------------------------------------- #

    def experiment_payload(self, experiment_ref: str, *,
                           acceptance_gate_ids: tuple[str, ...] | list[str],
                           provider_candidates: list[dict[str, Any]] | None = None,
                           context_manifest_hash: str | None = None,
                           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The ordinary mission payload an experiment becomes.

        Nothing here is special to Stage 8 except ``origin`` and the lineage
        references, which are recorded so a mission can be traced back to the
        objective that opened it.  The repository and the baseline come from the
        experiment row, which pinned them at admission, so no caller can point a
        candidate at a repository or a baseline the experiment was not admitted
        against.
        """

        row = self._experiment(experiment_ref)
        policy = self._require_policy(row["project_id"], experiment_ref)
        payload: dict[str, Any] = {
            "work_item_id": experiment_ref,
            "project_id": row["project_id"],
            "repository": row["target_repository"],
            "baseline_sha": row["baseline_sha"],
            "capability": row["improvement_class"],
            "origin": "improvement_experiment",
            "experiment_ref": experiment_ref,
            "lineage_ref": row["lineage_ref"],
            "generation": row["generation"],
            "objective_ref": row["objective_ref"],
            "objective_digest": row["objective_digest"],
            "isolation_ref": row["isolation_ref"],
            "self_target": bool(row["self_target"]),
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
    def experiment_idempotency_key(payload: Mapping[str, Any]) -> str:
        """The key the rest of the stack will accept, derived not chosen.

        For a real mission ``factory-evidence-core`` refuses any value other
        than ``work_item_id:context_manifest_hash``, so choosing one here would
        produce an experiment that provably could never reach the host layer.
        The fixture case uses the experiment reference alone, which is already
        unique per objective, generation and baseline.
        """

        manifest = payload.get("context_manifest_hash")
        if payload.get("execution_mode") == "real" and isinstance(manifest, str) and manifest:
            return routing.expected_idempotency_key(payload["work_item_id"], manifest)
        return str(payload["work_item_id"])

    def create_candidate_mission(self, experiment_ref: str, controller,
                                 **payload_kwargs) -> tuple[dict[str, Any], bool]:
        """Submit the experiment as an ordinary mission, exactly once.

        Two independent mechanisms make a duplicate impossible, and both are
        already load-bearing elsewhere: the store refuses a second mission under
        one idempotency key, and this row records the mission reference it got.
        A restart between the submission and the record therefore recovers the
        same mission rather than opening a second one.
        """

        row = self._experiment(experiment_ref)
        self._require_open(row)
        if row["baseline_json"] is None:
            raise ImprovementRefusal(
                "IMPROVEMENT_BASELINE_REQUIRED",
                "%s has no recorded baseline; a candidate created first could "
                "only ever be compared against a number chosen afterwards"
                % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if row["mission_ref"]:
            existing = controller.store.get(row["mission_ref"])
            if existing is not None:
                return existing, False
        payload = self.experiment_payload(experiment_ref, **payload_kwargs)
        key = self.experiment_idempotency_key(payload)
        mission, created = controller.submit(payload, key)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET mission_ref=?, idempotency_key=?,"
                " state='mission_created', updated_at=? WHERE experiment_ref=?"
                " AND disposition IS NULL",
                (mission["id"], key, time.time(), experiment_ref))
            self._append(db, "candidate_mission_created", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="mission_created",
                         detail={"mission_ref": mission["id"],
                                 "idempotency_key": key, "created": created})
        return mission, created

    # -- protected surfaces ------------------------------------------------- #

    def check_change_set(self, experiment_ref: str,
                         changed_paths: tuple[str, ...] | list[str]
                         ) -> dict[str, Any]:
        """What a candidate touched, against what it may never touch.

        Fail-closed in two directions.  An empty change set is refused rather
        than treated as "nothing protected was touched", because not knowing
        what changed is not the same fact as knowing nothing did.  And the
        surfaces come from the stored policy row, which the candidate has no
        method to write, so widening the check is not something the autonomous
        path can reach.
        """

        row = self._experiment(experiment_ref)
        policy = self._require_policy(row["project_id"], experiment_ref)
        paths = [str(path) for path in changed_paths if str(path).strip()]
        if not paths:
            raise ImprovementRefusal(
                "IMPROVEMENT_CHANGE_SET_UNKNOWN",
                "no change set is recorded for %s; an unknown change set is "
                "refused rather than assumed harmless" % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        violations = []
        for path in sorted(paths):
            surface = policy.surface_for(path)
            if surface is not None:
                violations.append({"path": path, "surface": surface})
        if violations:
            raise ImprovementRefusal(
                "IMPROVEMENT_PROTECTED_SURFACE_TOUCHED",
                "candidate touches protected surface(s): %s; changing these is "
                "an Owner act with a stronger gate and has no autonomous path"
                % ", ".join("%s (%s)" % (item["path"], item["surface"])
                            for item in violations),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        return {"experiment_ref": experiment_ref, "paths": sorted(paths),
                "protected_surfaces": sorted(policy.protected_surfaces),
                "violations": []}

    def seal_candidate(self, experiment_ref: str, mission: Mapping[str, Any], *,
                       producer_identity: str,
                       changed_paths: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Bind the candidate, its author and its change set, once.

        Sealing is where the protected surfaces are enforced, because it is the
        first moment the change set is a fact rather than an intention.  The
        producer is recorded here and never supplied again, which is what makes
        the independence check at evaluation time a comparison of two things
        written at different times by different callers.
        """

        row = self._experiment(experiment_ref)
        self._require_open(row)
        if not producer_identity or not str(producer_identity).strip():
            raise ImprovementRefusal(
                "IMPROVEMENT_PRODUCER_UNKNOWN",
                "the identity that produced the candidate is not recorded; an "
                "unattributed candidate cannot be independently evaluated",
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if row["candidate_sha"]:
            return self.lineage(experiment_ref)
        self.check_change_set(experiment_ref, changed_paths)
        mission_id = mission["id"]
        state = mission.get("state")
        candidate = self._candidate_sha(mission_id)
        if not candidate:
            raise ImprovementRefusal(
                "IMPROVEMENT_CANDIDATE_ABSENT",
                "mission %s is %s and produced no candidate; there is nothing "
                "to compare" % (mission_id, state or "unknown"),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        evaluation = self._store.step_output(mission_id, "evaluate")
        if not isinstance(evaluation, Mapping):
            gates = "not_run" if state in MISSION_SETTLED else "unknown"
        else:
            gates = "passed" if evaluation.get("passed") else "failed"
        if gates != "passed":
            raise ImprovementRefusal(
                "IMPROVEMENT_ACCEPTANCE_GATES_UNMET",
                "the candidate's own acceptance gates are %r; a candidate that "
                "does not pass the ordinary gates is never compared for "
                "improvement" % gates,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET candidate_sha=?, producer_identity=?,"
                " change_set_json=?, state='candidate_sealed', updated_at=?"
                " WHERE experiment_ref=? AND disposition IS NULL",
                (candidate, str(producer_identity),
                 canonical_json(sorted(str(path) for path in changed_paths)),
                 time.time(), experiment_ref))
            self._append(db, "candidate_sealed", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="candidate_sealed",
                         detail={"candidate_sha": candidate,
                                 "producer_identity": str(producer_identity),
                                 "acceptance_gates": gates,
                                 "changed_paths": sorted(str(p) for p in changed_paths)})
        return self.lineage(experiment_ref)

    # -- comparative evaluation --------------------------------------------- #

    def evaluate_candidate(self, experiment_ref: str, *, evaluator_identity: str,
                           measurements: Mapping[str, Any],
                           objective_digest: str | None = None) -> dict[str, Any]:
        """Compare the sealed candidate with the pinned baseline, independently.

        Three refusals stand between a candidate and a verdict of improvement,
        and each closes a way a system could otherwise be talked into approving
        itself.  The evaluator may not be the producer.  The objective must
        still digest to the value the experiment pinned, so metrics cannot have
        moved since execution began.  And the baseline must already be sealed,
        which it is by construction because a candidate cannot exist without it.
        """

        row = self._experiment(experiment_ref)
        self._require_open(row)
        if not row["candidate_sha"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_CANDIDATE_NOT_SEALED",
                "%s has no sealed candidate to compare" % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if not evaluator_identity or not str(evaluator_identity).strip():
            raise ImprovementRefusal(
                "IMPROVEMENT_EVALUATOR_UNKNOWN",
                "an anonymous evaluation cannot be shown to be independent",
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if str(evaluator_identity) == row["producer_identity"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_EVALUATOR_NOT_INDEPENDENT",
                "%s produced the candidate and cannot also judge it"
                % evaluator_identity,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        objective = self._require_objective(row)
        if objective.objective_digest != row["objective_digest"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_OBJECTIVE_MUTATED",
                "objective %s no longer digests to the value %s was admitted "
                "under; metrics frozen before execution cannot be revised after"
                % (row["objective_ref"], experiment_ref),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if objective_digest is not None and objective_digest != row["objective_digest"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_OBJECTIVE_MUTATED",
                "the evaluation states objective digest %s and %s was admitted "
                "under %s" % (objective_digest, experiment_ref,
                              row["objective_digest"]),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        baseline = json.loads(row["baseline_json"])
        candidate_values = {key: value for key, value in measurements.items()}
        comparison = compare(objective, baseline, candidate_values)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET evaluator_identity=?, candidate_json=?,"
                " comparison_json=?, verdict=?, state='evaluated', updated_at=?"
                " WHERE experiment_ref=? AND disposition IS NULL",
                (str(evaluator_identity), canonical_json(candidate_values),
                 canonical_json(comparison), comparison["verdict"], time.time(),
                 experiment_ref))
            self._append(db, "candidate_evaluated", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="evaluated",
                         detail={"evaluator_identity": str(evaluator_identity),
                                 "verdict": comparison["verdict"],
                                 "regressed": comparison["regressed"],
                                 "unmeasured": comparison["unmeasured"],
                                 "unmet": comparison["unmet"]})
        return comparison

    # -- promotion ---------------------------------------------------------- #

    def stage_promotion(self, experiment_ref: str, bundle: production.ReleaseBundle,
                        environment_id: str) -> str:
        """Hand an improved candidate to an ungated environment, or refuse.

        This is the whole of what autonomous improvement may do to a running
        system, and it is a call into Stage 6 rather than a release of its own:
        the ledger applies the same admission a person's release gets, including
        emergency stop, drain, concurrency and the unresolved ``uncertain``
        deployment refusal.  Four refusals come first, and the self-target one
        is the reason this method exists rather than being reused from Stage 7 --
        the Factory improving itself is exactly the case where an automatic
        promotion would be the whole danger.
        """

        row = self._experiment(experiment_ref)
        self._require_open(row)
        if row["verdict"] != "improved":
            raise ImprovementRefusal(
                "IMPROVEMENT_NOT_DEMONSTRATED",
                "the comparative verdict is %r; only a candidate measured "
                "better than its pinned baseline is staged"
                % (row["verdict"] or "unknown"),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if row["risk_class"] not in AUTONOMOUS_RISK_CLASSES:
            raise ImprovementRefusal(
                "IMPROVEMENT_RISK_CLASS_REQUIRES_OWNER",
                "%s was admitted at risk class %r; only %s stages without a "
                "person" % (experiment_ref, row["risk_class"],
                            ", ".join(sorted(AUTONOMOUS_RISK_CLASSES))),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if row["self_target"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_SELF_PROMOTION_REQUIRES_OWNER",
                "%s targets the Factory itself; an accepted self-improvement "
                "candidate is a commit in an isolated lane and an evidence "
                "record, and installing it is an Owner act" % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        policy = self._ledger.environment(environment_id)
        if policy.gated:
            raise ImprovementRefusal(
                "IMPROVEMENT_PRODUCTION_AUTHORITY_REQUIRED",
                "%s is a %s environment that a person approves; autonomous "
                "improvement stages a release, it never grants itself the "
                "authority to make one" % (environment_id, policy.environment_class),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if policy.project_id != row["project_id"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_PROJECT_ISOLATION",
                "experiment %s belongs to %s and %s belongs to %s"
                % (experiment_ref, row["project_id"], environment_id,
                   policy.project_id),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        declared = self.policy(row["project_id"])
        if declared is not None and policy.environment_class not in declared.environment_classes:
            raise ImprovementRefusal(
                "IMPROVEMENT_ENVIRONMENT_OUT_OF_SCOPE",
                "%s is a %s environment and %s scopes improvement to %s"
                % (environment_id, policy.environment_class, row["project_id"],
                   ", ".join(declared.environment_classes)),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        deployment_id = self._ledger.admit_release(
            bundle, environment_id, requested_by="improvement:%s" % experiment_ref)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET bundle_ref=?, promotion_deployment_id=?,"
                " promotion_environment_id=?, state='promotion_staged',"
                " updated_at=? WHERE experiment_ref=?",
                (bundle.bundle_ref, deployment_id, environment_id, time.time(),
                 experiment_ref))
            self._append(db, "promotion_staged", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="promotion_staged",
                         detail={"deployment_id": deployment_id,
                                 "environment_id": environment_id,
                                 "bundle_ref": bundle.bundle_ref})
        return deployment_id

    def revert(self, experiment_ref: str, *, reason: str) -> dict[str, Any]:
        """Return to the baseline this experiment pinned at admission.

        Deterministic because the target was recorded before anything ran:
        reverting a promoted improvement is a lookup, never a new decision about
        what "before" meant.  Nothing is deployed from here -- the recorded
        rollback target is what a Stage-6 release uses, so the rollback goes
        through the same gate the promotion did.
        """

        row = self._experiment(experiment_ref)
        if not row["promotion_deployment_id"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_NOTHING_PROMOTED",
                "%s staged no promotion; there is nothing to revert"
                % experiment_ref,
                experiment_ref=experiment_ref, project_id=row["project_id"])
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET reverted_to=?, updated_at=?"
                " WHERE experiment_ref=?",
                (row["rollback_target"], time.time(), experiment_ref))
            self._append(db, "promotion_reverted", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state=row["state"],
                         detail={"reverted_to": row["rollback_target"],
                                 "deployment_id": row["promotion_deployment_id"],
                                 "reason": reason})
        return self.lineage(experiment_ref)

    def close(self, experiment_ref: str, disposition: str, *,
              reason: str) -> dict[str, Any]:
        """End a generation.  Every bound in this module arrives here.

        ``accepted`` is the only disposition that carries a lineage forward, and
        it is refused unless the comparative verdict says the candidate was
        actually measured better.  That is the join between "the numbers moved"
        and "the recursion may continue", and it is one condition rather than
        two so there is no state in which they disagree.
        """

        if disposition not in DISPOSITIONS:
            raise PolicyError("disposition must be one of %s"
                              % ", ".join(DISPOSITIONS))
        row = self._experiment(experiment_ref)
        if row["disposition"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_EXPERIMENT_CLOSED",
                "experiment %s is already %s" % (experiment_ref, row["disposition"]),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        if disposition == "accepted" and row["verdict"] != "improved":
            raise ImprovementRefusal(
                "IMPROVEMENT_NOT_DEMONSTRATED",
                "the comparative verdict is %r; an experiment is not accepted "
                "because it finished" % (row["verdict"] or "unknown"),
                experiment_ref=experiment_ref, project_id=row["project_id"])
        with self._store.transaction() as db:
            db.execute(
                "UPDATE experiments SET disposition=?, state='closed', updated_at=?"
                " WHERE experiment_ref=?", (disposition, time.time(), experiment_ref))
            self._append(db, "experiment_closed", row["project_id"],
                         experiment_ref=experiment_ref, from_state=row["state"],
                         to_state="closed",
                         detail={"disposition": disposition, "reason": reason,
                                 "verdict": row["verdict"] or "not_run"})
        return self.lineage(experiment_ref)

    # -- reading ------------------------------------------------------------ #

    def lineage(self, experiment_ref: str) -> dict[str, Any]:
        """Objective through terminal disposition, absences spelled out."""

        row = self._experiment(experiment_ref)
        with self._store.transaction() as db:
            events = db.execute(
                "SELECT kind, from_state, to_state, created_at FROM improvement_events"
                " WHERE experiment_ref=? ORDER BY sequence",
                (experiment_ref,)).fetchall()
        return {
            "contract_version": CONTRACT_VERSION,
            "experiment_ref": experiment_ref,
            "lineage_ref": row["lineage_ref"],
            "parent_ref": _absent(row["parent_ref"], "not_applicable"),
            "generation": row["generation"],
            "project_id": row["project_id"],
            "objective_ref": row["objective_ref"],
            "objective_digest": row["objective_digest"],
            "improvement_class": row["improvement_class"],
            "trigger_class": row["trigger_class"],
            "source_kind": row["source_kind"],
            "source_ref": row["source_ref"],
            "target_repository": row["target_repository"],
            "baseline_sha": row["baseline_sha"],
            "rollback_target": row["rollback_target"],
            "self_target": bool(row["self_target"]),
            "isolation_ref": row["isolation_ref"],
            "risk_class": row["risk_class"],
            "policy_version": row["policy_version"],
            "policy_digest": row["policy_digest"],
            "state": row["state"],
            "baseline": _json_or(row["baseline_json"], "not_run"),
            "mission_ref": _absent(row["mission_ref"], "not_run"),
            "idempotency_key": _absent(row["idempotency_key"], "not_run"),
            "candidate_sha": _absent(row["candidate_sha"], "not_run"),
            "producer_identity": _absent(row["producer_identity"], "not_run"),
            "change_set": _json_or(row["change_set_json"], "not_run"),
            "evaluator_identity": _absent(row["evaluator_identity"], "not_run"),
            "candidate_measurements": _json_or(row["candidate_json"], "not_run"),
            "comparison": _json_or(row["comparison_json"], "not_run"),
            "verdict": _absent(row["verdict"], "not_run"),
            "bundle_ref": _absent(row["bundle_ref"], "not_applicable"),
            "promotion_deployment_id": _absent(row["promotion_deployment_id"],
                                               "not_applicable"),
            "promotion_environment_id": _absent(row["promotion_environment_id"],
                                                "not_applicable"),
            "reverted_to": _absent(row["reverted_to"], "not_applicable"),
            "disposition": _absent(row["disposition"], "unknown"),
            "transitions": [dict(event) for event in events],
        }

    def generations(self, lineage_ref: str) -> tuple[dict[str, Any], ...]:
        """Every generation of one lineage, in order, with its own verdict."""

        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM experiments WHERE lineage_ref=? ORDER BY generation",
                (lineage_ref,)).fetchall()
        return tuple({
            "experiment_ref": row["experiment_ref"],
            "generation": row["generation"],
            "parent_ref": _absent(row["parent_ref"], "not_applicable"),
            "baseline_sha": row["baseline_sha"],
            "candidate_sha": _absent(row["candidate_sha"], "not_run"),
            "policy_version": row["policy_version"],
            "policy_digest": row["policy_digest"],
            "objective_digest": row["objective_digest"],
            "verdict": _absent(row["verdict"], "not_run"),
            "disposition": _absent(row["disposition"], "unknown"),
        } for row in rows)

    def experiments(self, project_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            if project_id is None:
                rows = db.execute(
                    "SELECT * FROM experiments ORDER BY admitted_at").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM experiments WHERE project_id=? ORDER BY admitted_at",
                    (project_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    def objectives(self, project_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            if project_id is None:
                rows = db.execute("SELECT * FROM objectives ORDER BY created_at").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM objectives WHERE project_id=? ORDER BY created_at",
                    (project_id,)).fetchall()
        return tuple(_objective_from_row(row).as_row() | {"state": row["state"]}
                     for row in rows)

    def events(self, project_id: str) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM improvement_events WHERE project_id=? ORDER BY sequence",
                (project_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    # -- internals ---------------------------------------------------------- #

    def _candidate_sha(self, mission_id: str) -> str | None:
        """The candidate the mission actually produced, from its own history.

        Taken from the recorded transition rather than from the terminal
        result, for the same reason Stage 7 does it: a mission can produce a
        real candidate and then stop at a gate, and the candidate is still a
        fact about what happened.
        """

        for event in self._store.history(mission_id):
            detail = event.get("detail")
            if isinstance(detail, Mapping) and detail.get("candidate_sha"):
                return str(detail["candidate_sha"])
        return None

    def _experiment(self, experiment_ref: str):
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM experiments WHERE experiment_ref=?",
                             (experiment_ref,)).fetchone()
        if row is None:
            raise ImprovementRefusal("IMPROVEMENT_EXPERIMENT_UNKNOWN",
                                     "no experiment %r is recorded" % experiment_ref,
                                     experiment_ref=experiment_ref)
        return row

    def _require_open(self, row) -> None:
        if row["disposition"]:
            raise ImprovementRefusal(
                "IMPROVEMENT_EXPERIMENT_CLOSED",
                "experiment %s is already %s; a closed experiment does no "
                "further work" % (row["experiment_ref"], row["disposition"]),
                experiment_ref=row["experiment_ref"], project_id=row["project_id"])

    def _require_policy(self, project_id: str, experiment_ref: str) -> ImprovementPolicy:
        policy = self.policy(project_id)
        if policy is None:
            raise ImprovementRefusal(
                "IMPROVEMENT_DISABLED",
                "no improvement policy is declared for %s" % project_id,
                experiment_ref=experiment_ref, project_id=project_id)
        return policy

    def _require_objective(self, row) -> Objective:
        objective = self.objective(row["objective_ref"])
        if objective is None:
            raise ImprovementRefusal(
                "IMPROVEMENT_OBJECTIVE_UNKNOWN",
                "objective %s is no longer registered" % row["objective_ref"],
                experiment_ref=row["experiment_ref"], project_id=row["project_id"])
        return objective

    @staticmethod
    def _append(db, kind: str, project_id: str, *, experiment_ref: str | None = None,
                from_state: str | None = None, to_state: str | None = None,
                detail: Mapping[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO improvement_events (project_id, experiment_ref, kind,"
            " from_state, to_state, detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, experiment_ref, kind, from_state, to_state,
             canonical_json(dict(detail or {})), time.time()))


def _numeric(value: Any) -> bool:
    """A reading this module may do arithmetic on.

    ``bool`` is excluded deliberately: ``True`` is an ``int`` in Python, and a
    metric that flipped from ``False`` to ``True`` would otherwise read as an
    infinite relative gain against a zero baseline.
    """

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reading(value: Any) -> Any:
    """An absent or unusable measurement, kept as one of the four words."""

    if _numeric(value):
        return value
    if isinstance(value, str) and value in CANONICAL_ABSENCE:
        return value
    return "not_measurable"


def _absent(value: Any, word: str) -> Any:
    if word not in CANONICAL_ABSENCE:
        raise PolicyError("%r is not one of the four absence words" % word)
    return word if value in (None, "") else value


def _json_or(value: Any, word: str) -> Any:
    if word not in CANONICAL_ABSENCE:
        raise PolicyError("%r is not one of the four absence words" % word)
    return word if value in (None, "") else json.loads(value)


def _table_exists(db, name: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return row is not None


def _policy_from_row(row) -> ImprovementPolicy:
    surfaces = json.loads(row["protected_surfaces_json"])
    return ImprovementPolicy(
        project_id=row["project_id"],
        enabled=bool(row["enabled"]),
        improvement_classes=tuple(json.loads(row["improvement_classes_json"])),
        trigger_classes=tuple(json.loads(row["trigger_classes_json"])),
        environment_classes=tuple(json.loads(row["environment_classes_json"])),
        protected_surfaces={name: tuple(prefixes)
                            for name, prefixes in surfaces.items()},
        self_target_repositories=tuple(json.loads(row["self_target_repositories_json"])),
        generation_ceiling=row["generation_ceiling"],
        experiment_budget=row["experiment_budget"],
        concurrent_experiments=row["concurrent_experiments"],
        cooldown_seconds=row["cooldown_seconds"],
        risk_class=row["risk_class"],
        maintenance_pressure=row["maintenance_pressure"],
        incident_pressure=row["incident_pressure"],
        cost_pressure_ratio=row["cost_pressure_ratio"],
        execution_mode=row["execution_mode"],
        policy_version=row["policy_version"])


def _policy_kwargs(policy: ImprovementPolicy) -> dict[str, Any]:
    return {"project_id": policy.project_id, "enabled": policy.enabled,
            "improvement_classes": policy.improvement_classes,
            "trigger_classes": policy.trigger_classes,
            "environment_classes": policy.environment_classes,
            "protected_surfaces": dict(policy.protected_surfaces),
            "self_target_repositories": policy.self_target_repositories,
            "generation_ceiling": policy.generation_ceiling,
            "experiment_budget": policy.experiment_budget,
            "concurrent_experiments": policy.concurrent_experiments,
            "cooldown_seconds": policy.cooldown_seconds,
            "risk_class": policy.risk_class,
            "maintenance_pressure": policy.maintenance_pressure,
            "incident_pressure": policy.incident_pressure,
            "cost_pressure_ratio": policy.cost_pressure_ratio,
            "execution_mode": policy.execution_mode,
            "policy_version": policy.policy_version}


def _objective_from_row(row) -> Objective:
    return Objective(
        objective_ref=row["objective_ref"],
        project_id=row["project_id"],
        improvement_class=row["improvement_class"],
        statement=row["statement"],
        metrics=tuple(Metric(**item) for item in json.loads(row["metrics_json"])),
        authority=row["authority"],
        objective_version=row["objective_version"])
