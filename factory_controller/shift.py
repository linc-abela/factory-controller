"""What turns "ready" into "authorized", and keeps the authorization finite.

The Factory already knows how to read whether it *could* run: ``dogfood``
composes the run contract against durable state and against reports the other
repositories produced, and answers ``met``/``unmet``/``unknown`` per check.
What it has never had is the act in between -- a person deciding that this
reading, on this day, for this bounded list of work, is enough.  Without that
act ``supervisor start`` is the whole of activation, and a green preflight is
one command away from becoming live authority.  That is the shape the corpus
keeps recording: a harness standing in for the milestone it was built to prove.

So three things live here and they are deliberately different in kind.

``Grant`` is a *decision*: who approved it, where that approval is recorded,
which portfolio it authorizes, how many missions, how much money, and when it
expires.  Nothing in this module can author one -- ``apply`` refuses without a
durable approval record it did not write -- and every field that bounds it is
mandatory, because an activation with no ceiling and no expiry is a daemon
wearing a decision's clothes.

``gate`` is a *reading*: the preflight the Factory already had, plus the three
facts a bounded shift adds -- that the portfolio is lawful against the run
contract, that some runtime is actually eligible, and that the request itself
is finite.  It composes; it does not re-decide.  A check that was ``unknown``
upstream is ``unknown`` here, and ``unknown`` is never a pass.

``state`` is a *consequence*: ``off -> preparing -> active -> draining ->
suspended/off`` is derived from the grant, the control plane, the gate and the
work in flight, and is stored nowhere.  Only the Owner's acts are written
(apply, revoke, suspend, resume); everything else is arithmetic over them.  A
second durable state machine beside ``supervisor_control`` would be a second
place the answer lives, and the corpus already has four names for one artifact
more than once.

Two boundaries are worth stating because they are easy to cross by accident.

*Host preparation is not mission authority.*  Reinstalling a bridge, loading a
service or authenticating a runtime moves checks from ``unmet`` to ``met``.  It
creates no grant.  A Factory that admitted work because its host got healthier
would have made readiness into permission.

*Capacity narrows; it never widens.*  An unusable runtime removes eligibility
and can drain a shift.  A restored one restores eligibility and cannot revive a
grant the Owner revoked, or one that expired.  ``eligible`` is an intersection,
never a union, and ``tests/test_dogfood_shift.py`` holds that both ways.

The advisory seam is absent from this module on purpose.  Proposing a
decomposition is advice; starting a shift is authority, and the two are kept
apart by ``advisor.FORBIDDEN_KINDS`` rather than by anything here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from factory_controller import activation, dogfood, store as ledger

CONTRACT_VERSION = "factory-controller/shift/1.0"

#: The Owner's recorded decision to open a shift.  A distinct schema from the
#: supervisor's service approval so that neither record can be replayed as the
#: other: installing a host service and admitting missions are different acts.
APPROVAL_SCHEMA = "factory-controller/dogfood-shift-activation-approval/1.0"

PORTFOLIO_SCHEMA = "factory.controller.internal_dogfood_mission_portfolio.v1"

#: Derived, never stored.  ``suspended`` is distinct from ``off`` because the
#: grant survives it: a suspended shift resumes on the same approval, while an
#: ``off`` one needs a new decision.
SHIFT_STATES = ("off", "preparing", "active", "draining", "suspended")

#: Reproduced from ``dogfood``; equal by test.  Stated literally because this
#: set has forked across the corpus more than any other vocabulary.
MET, UNMET, UNKNOWN = dogfood.MET, dogfood.UNMET, dogfood.UNKNOWN

#: A shift may not outlast a working day.  The number is a ceiling on the
#: Owner's own request, not a schedule: the point is that "until I say stop"
#: cannot be expressed, so an unattended host cannot inherit yesterday's grant.
MAX_SHIFT_SECONDS = 12 * 3600.0

#: The largest portfolio a single grant may authorize.  Chosen from the first
#: portfolio's own length rather than a round figure: a grant that could
#: authorize more missions than any portfolio contains would be bounded only in
#: principle.
MAX_MISSION_CEILING = 25

#: Why a shift stops admitting.  Every one of these is a *reading*: none of
#: them is written down, and none can be cleared by editing a record.  Scope 7
#: of SF-144 names nine; ``OWNER_STOP`` and ``EMERGENCY_STOP`` are the two the
#: Owner causes, and ``PORTFOLIO_COMPLETE`` is the ordinary end.
DRAIN_REASONS = (
    "OWNER_STOP",
    "EMERGENCY_STOP",
    "PORTFOLIO_COMPLETE",
    "MISSION_CEILING_REACHED",
    "SHIFT_WINDOW_EXPIRED",
    "BUDGET_CEILING_REACHED",
    "READINESS_LOST",
    "CAPACITY_EXHAUSTED_NO_ELIGIBLE_RUNTIME",
    "PROTECTED_SURFACE_CONFLICT",
    "PROVIDER_UNCERTAINTY_UNRESOLVED",
    "REPEATED_MISSION_FAILURE",
    "ACCEPTANCE_GATE_UNAVAILABLE",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS shift_grants (
  request_ref TEXT PRIMARY KEY,
  run_ref TEXT NOT NULL,
  portfolio_ref TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approval_ref TEXT NOT NULL,
  gate_digest TEXT NOT NULL,
  mission_ceiling INTEGER NOT NULL,
  budget_ceiling REAL NOT NULL,
  budget_currency TEXT NOT NULL,
  granted_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  suspended_at REAL,
  resume_ref TEXT,
  revoked_at REAL,
  revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS shift_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  request_ref TEXT NOT NULL,
  event TEXT NOT NULL,
  actor TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS shift_events_by_request
  ON shift_events(request_ref, sequence);
CREATE TRIGGER IF NOT EXISTS shift_events_no_update
BEFORE UPDATE ON shift_events
BEGIN SELECT RAISE(ABORT, 'shift events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS shift_events_no_delete
BEFORE DELETE ON shift_events
BEGIN SELECT RAISE(ABORT, 'shift events are append-only'); END;
"""


class ShiftError(ValueError):
    """A shift request or portfolio the Controller will not read as authority."""


class ShiftGovernanceRefusal(Exception):
    """One named refusal, carried far enough to outlive the transaction.

    Prefixed because the neighbours already own the bare names: the bridge owns
    ``IDEMPOTENCY_CONFLICT`` and Evidence Core owns ``ADMISSION_*``.  A bare
    name here would be the eighth collision in the ``glob``/``global`` family
    the corpus has recorded.
    """

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code, self.detail, self.extra = code, detail, extra

    def as_row(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "detail": self.detail,
                            **self.extra}}


# --------------------------------------------------------------------------- #
# the mission portfolio
# --------------------------------------------------------------------------- #

#: Settled, for the purpose of walking a portfolio.  Taken from the ledger that
#: owns the vocabulary rather than restated: the first draft of this set
#: invented ``blocked``, which is not a mission state this store writes, and
#: omitted ``escalated``, which is.
#:
#: ``escalated`` is added deliberately.  The store keeps it out of ``TERMINAL``
#: because the mission is not finished; for a *portfolio* it is settled all the
#: same, because escalation is the point at which a person owns the mission and
#: the sequence would otherwise offer the same one forever with no signal that
#: it had stalled.  It is counted as a failure instead, so three of them reach
#: ``REPEATED_MISSION_FAILURE`` and drain the shift.
TERMINAL_MISSION_STATES = frozenset(ledger.TERMINAL) | {"escalated"}

#: Settled without succeeding.  Consecutive members of this set are what
#: ``REPEATED_MISSION_FAILURE`` counts.
UNSUCCESSFUL_MISSION_STATES = TERMINAL_MISSION_STATES - {"completed"}

#: A portfolio mission carries its own stop and rollback rules because the run
#: contract's are about the *run*.  "Roll back the whole run" is not a boundary
#: a first dogfood mission can act on.
REQUIRED_MISSION_KEYS = ("mission_ref", "project_id", "work_class",
                         "environment_class", "objective", "baseline_sha",
                         "acceptance_gate_ids", "acceptance_gate_source",
                         "stop_conditions", "rollback_boundary",
                         "evidence_required")


@dataclass(frozen=True)
class PortfolioMission:
    order: int
    mission_ref: str
    project_id: str
    work_class: str
    environment_class: str
    objective: str
    baseline_sha: str
    acceptance_gate_ids: tuple[str, ...]
    acceptance_gate_source: str
    stop_conditions: tuple[str, ...]
    rollback_boundary: str
    evidence_required: tuple[str, ...]
    mutates_repository: bool

    def as_row(self) -> dict[str, Any]:
        return {"order": self.order, "mission_ref": self.mission_ref,
                "project_id": self.project_id, "work_class": self.work_class,
                "environment_class": self.environment_class,
                "objective": self.objective,
                "baseline_sha": self.baseline_sha,
                "acceptance_gate_ids": list(self.acceptance_gate_ids),
                "acceptance_gate_source": self.acceptance_gate_source,
                "stop_conditions": list(self.stop_conditions),
                "rollback_boundary": self.rollback_boundary,
                "evidence_required": list(self.evidence_required),
                "mutates_repository": self.mutates_repository}


@dataclass(frozen=True)
class Portfolio:
    """An ordered, finite list of missions and the rule for taking the next one.

    Strictly serial on purpose.  The first portfolio's whole job is to produce
    evidence about a path nobody has run end to end with a real provider; two
    missions in flight would make every failure a question about which one
    caused it.  Concurrency is a later portfolio's decision, expressed by a
    later contract, not a flag here.
    """

    portfolio_ref: str
    rationale: str
    missions: tuple[PortfolioMission, ...]

    def as_row(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION,
                "schema_version": PORTFOLIO_SCHEMA,
                "portfolio_ref": self.portfolio_ref,
                "rationale": self.rationale,
                "mission_count": len(self.missions),
                "missions": [mission.as_row() for mission in self.missions]}

    def next_mission(self, outcomes: Mapping[str, str]) -> PortfolioMission | None:
        """The first mission not yet terminal, or ``None`` when all are.

        A mission whose predecessor has not settled is not returned, which is
        what makes the sequence a sequence rather than a set.
        """

        for mission in self.missions:
            if outcomes.get(mission.mission_ref) not in TERMINAL_MISSION_STATES:
                return mission
        return None

    def complete(self, outcomes: Mapping[str, str]) -> bool:
        return all(outcomes.get(mission.mission_ref) in TERMINAL_MISSION_STATES
                   for mission in self.missions)


def load_portfolio(path: str) -> Portfolio:
    try:
        body = json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise ShiftError("no mission portfolio exists at %s" % path) from exc
    except (OSError, ValueError) as exc:
        raise ShiftError("mission portfolio is unreadable: %s" % exc) from exc
    return portfolio_from_payload(body)


def portfolio_from_payload(body: Any) -> Portfolio:
    if not isinstance(body, Mapping):
        raise ShiftError("a mission portfolio is a JSON object")
    if body.get("schema_version") != PORTFOLIO_SCHEMA:
        raise ShiftError("schema_version must be %s" % PORTFOLIO_SCHEMA)
    ref = body.get("portfolio_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ShiftError("a mission portfolio names itself")
    rows = body.get("missions")
    if not isinstance(rows, list) or not rows:
        raise ShiftError("a mission portfolio carries at least one mission")
    if len(rows) > MAX_MISSION_CEILING:
        raise ShiftError("a portfolio of %d missions exceeds the %d-mission "
                         "ceiling a single grant may authorize"
                         % (len(rows), MAX_MISSION_CEILING))
    missions, seen = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ShiftError("mission %d is not an object" % index)
        missing = [key for key in REQUIRED_MISSION_KEYS if key not in row]
        if missing:
            raise ShiftError("mission %d is missing %s" % (index, missing))
        mission_ref = row["mission_ref"]
        if mission_ref in seen:
            raise ShiftError("two missions share the reference %r" % mission_ref)
        seen.add(mission_ref)
        for key in ("acceptance_gate_ids", "stop_conditions", "evidence_required"):
            value = row[key]
            if (not isinstance(value, list) or not value
                    or not all(isinstance(item, str) and item.strip()
                               for item in value)):
                raise ShiftError("mission %s: %s must be a non-empty list of "
                                 "names" % (mission_ref, key))
        missions.append(PortfolioMission(
            order=index + 1, mission_ref=mission_ref,
            project_id=row["project_id"], work_class=row["work_class"],
            environment_class=row["environment_class"],
            objective=row["objective"], baseline_sha=row["baseline_sha"],
            acceptance_gate_ids=tuple(row["acceptance_gate_ids"]),
            acceptance_gate_source=row["acceptance_gate_source"],
            stop_conditions=tuple(row["stop_conditions"]),
            rollback_boundary=row["rollback_boundary"],
            evidence_required=tuple(row["evidence_required"]),
            mutates_repository=bool(row.get("mutates_repository", True))))
    return Portfolio(portfolio_ref=ref,
                     rationale=str(body.get("rationale", "unknown")),
                     missions=tuple(missions))


# --------------------------------------------------------------------------- #
# the activation request
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ActivationRequest:
    """What the Owner is asking for, before anything has been read.

    Every bound is required.  An optional ceiling would be a ceiling only for
    Owners who remembered one, and the request that skipped it would be the
    open-ended daemon scope 3 exists to refuse.
    """

    request_ref: str
    run_ref: str
    portfolio_ref: str
    mission_ceiling: int
    duration_seconds: float
    budget_ceiling: float
    budget_currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_ref, str) or not self.request_ref.strip():
            raise ShiftError("an activation request names itself, so a second "
                             "apply can be recognised as the same act")
        if not isinstance(self.mission_ceiling, int) or isinstance(self.mission_ceiling, bool):
            raise ShiftError("mission_ceiling is a whole number of missions")
        if not 1 <= self.mission_ceiling <= MAX_MISSION_CEILING:
            raise ShiftError("mission_ceiling must be between 1 and %d"
                             % MAX_MISSION_CEILING)
        if not 0 < self.duration_seconds <= MAX_SHIFT_SECONDS:
            raise ShiftError("a shift lasts between 0 and %.0f seconds; "
                             "'until stopped' is not expressible"
                             % MAX_SHIFT_SECONDS)
        if not isinstance(self.budget_ceiling, (int, float)) or self.budget_ceiling <= 0:
            raise ShiftError("a shift carries a positive budget ceiling")
        if not isinstance(self.budget_currency, str) or not self.budget_currency.strip():
            raise ShiftError("a budget ceiling names its currency")

    def as_row(self) -> dict[str, Any]:
        return {"request_ref": self.request_ref, "run_ref": self.run_ref,
                "portfolio_ref": self.portfolio_ref,
                "mission_ceiling": self.mission_ceiling,
                "duration_seconds": self.duration_seconds,
                "budget_ceiling": self.budget_ceiling,
                "budget_currency": self.budget_currency}


@dataclass(frozen=True)
class Grant:
    """A decision that has been written down.  Read-only once granted."""

    request_ref: str
    run_ref: str
    portfolio_ref: str
    approved_by: str
    approval_ref: str
    gate_digest: str
    mission_ceiling: int
    budget_ceiling: float
    budget_currency: str
    granted_at: float
    expires_at: float
    suspended_at: float | None = None
    resume_ref: str | None = None
    revoked_at: float | None = None
    revoke_reason: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION,
                "request_ref": self.request_ref, "run_ref": self.run_ref,
                "portfolio_ref": self.portfolio_ref,
                "approved_by": self.approved_by,
                "approval_ref": self.approval_ref,
                "gate_digest": self.gate_digest,
                "mission_ceiling": self.mission_ceiling,
                "budget_ceiling": self.budget_ceiling,
                "budget_currency": self.budget_currency,
                "granted_at": self.granted_at, "expires_at": self.expires_at,
                "suspended_at": _absent(self.suspended_at),
                "resume_ref": _absent(self.resume_ref),
                "revoked_at": _absent(self.revoked_at),
                "revoke_reason": _absent(self.revoke_reason)}


def _absent(value: Any, word: str = "not_applicable") -> Any:
    return word if value is None else value


# --------------------------------------------------------------------------- #
# the Dogfood Activation Gate
# --------------------------------------------------------------------------- #

@dataclass
class GateFacts:
    """Everything the gate reads, gathered by whoever can actually reach it.

    The Controller starts no process and opens no repository, so the bridge's
    doctor, the broker's health and the labs' gate declarations arrive as rows
    somebody else collected.  A fact nobody supplied stays absent here rather
    than being inferred from a neighbour.
    """

    preflight: Mapping[str, Any]
    portfolio: Portfolio
    request: ActivationRequest
    contract_projects: Sequence[str] = ()
    contract_work_classes: Sequence[str] = ()
    contract_environment_classes: Sequence[str] = ()
    contract_budget_ceiling: float | None = None
    contract_budget_currency: str = "unknown"
    declared_gates: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Per project, the commits an operator confirmed a second host can fetch.
    #: Absent means unmeasured; an empty list for a named project means it was
    #: measured and nothing was reachable.
    fetchable_shas: Mapping[str, Sequence[str]] | None = None
    #: The execution layer's own project registry, as the operator read it.
    #: Absent means unmeasured; a mapping present but missing a project means
    #: the layer was asked and does not know that project.
    project_registry: Mapping[str, Mapping[str, Any]] | None = None
    #: The capabilities the admitted provider profiles actually offer, as the
    #: operator read them from the execution layer.  Kept apart from the
    #: project registry on purpose: deriving what is offered from what is
    #: requested would make the comparison circular, and a circular check can
    #: never fire.
    offered_capabilities: Sequence[str] | None = None
    capacity_readings: Mapping[str, Any] = field(default_factory=dict)
    eligible_profiles: Sequence[str] = ()


def gate(facts: GateFacts) -> dict[str, Any]:
    """The single composed reading a shift is authorized against.

    It is composition and not a second authority: every ``preflight`` check is
    carried through unchanged, keeping its state, its detail and its evidence
    class, and the checks added here are the three facts a *bounded* shift
    introduces that a run contract never had -- that the portfolio is lawful,
    that some runtime is eligible, and that the request is finite.
    """

    checks = [dict(row) for row in facts.preflight.get("checks", [])]
    for row in checks:
        row.setdefault("source", "preflight")
    out = _GateChecks(checks)
    _check_portfolio(out, facts)
    _check_sources(out, facts)
    _check_dispatchable(out, facts)
    _check_eligibility(out, facts)
    _check_finite(out, facts)
    unmet = [row for row in checks if row.get("required", True)
             and row.get("state") != MET]
    return {
        "contract_version": CONTRACT_VERSION,
        "gate": "DOGFOOD-ACTIVATION-GATE",
        "run_ref": facts.preflight.get("run_ref", "unknown"),
        "portfolio_ref": facts.portfolio.portfolio_ref,
        "request_ref": facts.request.request_ref,
        "ready": not unmet,
        "blockers": [{"check": row["check"], "state": row["state"],
                      "detail": row["detail"], "source": row.get("source", "shift")}
                     for row in unmet],
        "states": {state: sum(1 for row in checks if row.get("state") == state)
                   for state in (MET, UNMET, UNKNOWN)},
        "checks": checks,
        "digest": _digest(checks),
    }


class _GateChecks:
    def __init__(self, checks: list) -> None:
        self.checks = checks

    def record(self, check: str, state: str, detail: str, *,
               required: bool = True, evidence_class: str = "rederived",
               **extra: Any) -> None:
        self.checks.append({"check": check, "state": state, "detail": detail,
                            "evidence_class": evidence_class,
                            "required": required, "source": "shift", **extra})


def _digest(checks: Sequence[Mapping[str, Any]]) -> str:
    import hashlib

    body = json.dumps([{key: row.get(key) for key in ("check", "state")}
                       for row in checks], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _check_portfolio(out: _GateChecks, facts: GateFacts) -> None:
    """Whether every mission the portfolio names is lawful under the run contract.

    A portfolio is not a second admission surface.  It may only name projects,
    classes and environments the run contract already admits, and gates the
    project registry already declares -- SF-141's finding, asked here before a
    mission exists rather than at promotion when one already does.
    """

    entry = facts.portfolio
    outside = [
        {"mission_ref": mission.mission_ref,
         "project_id": mission.project_id if mission.project_id
         not in facts.contract_projects else "ok",
         "work_class": mission.work_class if mission.work_class
         not in facts.contract_work_classes else "ok",
         "environment_class": mission.environment_class
         if mission.environment_class not in facts.contract_environment_classes
         else "ok"}
        for mission in entry.missions
        if mission.project_id not in facts.contract_projects
        or mission.work_class not in facts.contract_work_classes
        or mission.environment_class not in facts.contract_environment_classes]
    out.record("PORTFOLIO_WITHIN_RUN_CONTRACT", UNMET if outside else MET,
               "missions outside the run contract: %s" % outside if outside
               else "all %d missions name projects, classes and environments "
                    "the run contract admits" % len(entry.missions))
    if not facts.declared_gates:
        out.record("PORTFOLIO_GATES_DECLARED", UNKNOWN,
                   "no acceptance-gate declarations were supplied, so no "
                   "mission's gates could be checked against the registry",
                   evidence_class="not_run")
    else:
        undeclared = []
        for mission in entry.missions:
            declared = facts.declared_gates.get(mission.project_id)
            if declared is None:
                undeclared.append({"mission_ref": mission.mission_ref,
                                   "reason": "no declaration for the project"})
                continue
            ids = set(declared.get("acceptance_gate_ids") or ())
            extra = sorted(set(mission.acceptance_gate_ids) - ids)
            if extra:
                undeclared.append({"mission_ref": mission.mission_ref,
                                   "undeclared_gate_ids": extra})
            elif declared.get("source") not in (None, mission.acceptance_gate_source):
                undeclared.append({"mission_ref": mission.mission_ref,
                                   "registry_source": declared.get("source"),
                                   "mission_source": mission.acceptance_gate_source})
        out.record("PORTFOLIO_GATES_DECLARED", UNMET if undeclared else MET,
                   "gates not declared by the registry: %s" % undeclared
                   if undeclared else
                   "every mission's gates come from the project registry at "
                   "the source the registry names")
    mutating = [mission.mission_ref for mission in entry.missions
                if mission.mutates_repository]
    out.record("PORTFOLIO_STARTS_NON_MUTATING",
               MET if entry.missions and not entry.missions[0].mutates_repository
               else UNMET,
               "the first mission mutates a repository: %s" % mutating[:1]
               if entry.missions and entry.missions[0].mutates_repository
               else "the first mission produces evidence without mutating a "
                    "repository, so the path is proved before it can damage "
                    "anything")


def _check_sources(out: _GateChecks, facts: GateFacts) -> None:
    """That every commit the portfolio names can be reached from somewhere else.

    A mission baseline and a gate declaration are both identified by a commit,
    and a commit that exists only in one working copy identifies nothing a
    second host, a rerun or an auditor can resolve.  The Controller opens no
    repository, so reachability arrives as an operator's reading -- and when it
    was not taken, the answer is ``unknown``.
    """

    if facts.fetchable_shas is None:
        out.record("PORTFOLIO_SOURCES_FETCHABLE", UNKNOWN,
                   "no reachability reading was supplied, so no baseline or "
                   "gate source could be confirmed to exist off this host",
                   evidence_class="not_run")
        return
    unreachable = []
    for mission in facts.portfolio.missions:
        known = set(facts.fetchable_shas.get(mission.project_id) or ())
        named = {"baseline_sha": mission.baseline_sha,
                 "acceptance_gate_source": mission.acceptance_gate_source}
        for field_name, value in named.items():
            commit = value.rpartition("@")[2].partition(":")[0] or value
            if commit not in known:
                unreachable.append({"mission_ref": mission.mission_ref,
                                    "field": field_name, "commit": commit})
    out.record("PORTFOLIO_SOURCES_FETCHABLE", UNMET if unreachable else MET,
               "commits that exist only on this host: %s" % unreachable
               if unreachable else
               "every baseline and gate source the portfolio names is "
               "reachable from the project's remote")


def _check_dispatchable(out: _GateChecks, facts: GateFacts) -> None:
    """That the execution layer knows the projects the portfolio names.

    Registration in the Controller and registration at the execution layer are
    two different facts, and until SF-144 nothing compared them: a run contract
    could admit a project, a supervisor policy could exist for it, and the
    layer would still refuse every dispatch because it had never heard of the
    repository.  Measured on this host on 2026-08-28, that is not hypothetical
    -- one of the two admitted labs is absent from the layer's registry.
    """

    if facts.project_registry is None:
        out.record("PORTFOLIO_PROJECTS_DISPATCHABLE", UNKNOWN,
                   "no execution-layer project registry was supplied, so no "
                   "mission could be confirmed dispatchable",
                   evidence_class="not_run")
    else:
        absent = [{"mission_ref": mission.mission_ref,
                   "project_id": mission.project_id}
                  for mission in facts.portfolio.missions
                  if mission.project_id not in facts.project_registry]
        out.record("PORTFOLIO_PROJECTS_DISPATCHABLE", UNMET if absent else MET,
                   "the execution layer has no registration for: %s" % absent
                   if absent else
                   "every project the portfolio names is registered with the "
                   "execution layer",
                   registered=sorted(facts.project_registry))
    _check_capabilities(out, facts)


def _check_capabilities(out: _GateChecks, facts: GateFacts) -> None:
    """That something can actually be selected for each project's capability.

    A registered project whose declared capability no admitted profile offers
    is a project nothing can be chosen for, and the refusal arrives at dispatch
    rather than at the gate.  The two sides are read from different places so
    the comparison is a comparison: what a project asks for comes from the
    layer's project registry, what is available comes from its profiles.
    """

    if facts.offered_capabilities is None or facts.project_registry is None:
        out.record("PORTFOLIO_CAPABILITIES_OFFERED", UNKNOWN,
                   "the capabilities the admitted profiles offer were not "
                   "supplied, so no project's capability could be matched",
                   evidence_class="not_run")
        return
    offered = set(facts.offered_capabilities)
    uncovered = []
    for mission in facts.portfolio.missions:
        entry = facts.project_registry.get(mission.project_id)
        wanted = tuple((entry or {}).get("capabilities") or ())
        if wanted and not set(wanted) & offered:
            uncovered.append({"mission_ref": mission.mission_ref,
                              "project_id": mission.project_id,
                              "wants": list(wanted)})
    out.record("PORTFOLIO_CAPABILITIES_OFFERED", UNMET if uncovered else MET,
               "no admitted profile offers what these need: %s" % uncovered
               if uncovered else
               "every project's declared capability is offered by an admitted "
               "profile",
               offered=sorted(offered))


def _check_eligibility(out: _GateChecks, facts: GateFacts) -> None:
    """Capacity, read as a narrowing and never as a grant.

    An unregistered runtime is not narrowed at all -- the same rule
    ``capacity.RuntimePolicy`` states for an unmanaged runtime and
    ``supervisor.within_window`` states for an undeclared window: switching a
    constraint on for the first time must not read as a closed gate.  What is
    refused is the opposite case, a *registered* runtime whose reading says it
    cannot take work.
    """

    if not facts.capacity_readings:
        out.record("RUNTIME_ELIGIBILITY", UNKNOWN,
                   "no capacity readings were supplied, so no runtime's "
                   "eligibility could be established",
                   evidence_class="not_run",
                   eligible=list(facts.eligible_profiles))
        return
    out.record("RUNTIME_ELIGIBILITY",
               MET if facts.eligible_profiles else UNMET,
               "eligible runtimes: %s" % list(facts.eligible_profiles)
               if facts.eligible_profiles else
               "every declared runtime is unusable, so a shift would be "
               "authorized with nothing able to take a mission",
               eligible=list(facts.eligible_profiles),
               readings={name: row.get("state", UNKNOWN) if isinstance(row, Mapping)
                         else getattr(row, "state", UNKNOWN)
                         for name, row in facts.capacity_readings.items()})


def _check_finite(out: _GateChecks, facts: GateFacts) -> None:
    """That the thing being authorized has an end, expressed three ways."""

    request, entry = facts.request, facts.portfolio
    out.record("ACTIVATION_IS_FINITE", MET,
               "at most %d missions, at most %.0f seconds, at most %.2f %s"
               % (request.mission_ceiling, request.duration_seconds,
                  request.budget_ceiling, request.budget_currency))
    out.record("CEILING_COVERS_PORTFOLIO",
               MET if request.mission_ceiling >= len(entry.missions) else UNMET,
               "the ceiling of %d admits fewer missions than the portfolio's "
               "%d, so the shift would end mid-portfolio"
               % (request.mission_ceiling, len(entry.missions))
               if request.mission_ceiling < len(entry.missions)
               else "the mission ceiling covers the whole portfolio",
               required=False)
    if facts.contract_budget_ceiling is None:
        out.record("BUDGET_WITHIN_RUN_CONTRACT", UNKNOWN,
                   "the run contract's ceiling was not supplied",
                   evidence_class="not_run")
        return
    within = (request.budget_ceiling <= facts.contract_budget_ceiling
              and request.budget_currency == facts.contract_budget_currency)
    out.record("BUDGET_WITHIN_RUN_CONTRACT", MET if within else UNMET,
               "the request asks for %.2f %s against a run ceiling of %.2f %s"
               % (request.budget_ceiling, request.budget_currency,
                  facts.contract_budget_ceiling, facts.contract_budget_currency))
    out.record("PORTFOLIO_MATCHES_REQUEST",
               MET if request.portfolio_ref == entry.portfolio_ref else UNMET,
               "the request authorizes %r and the portfolio supplied is %r"
               % (request.portfolio_ref, entry.portfolio_ref))


def eligible(profiles: Sequence[str], readings: Mapping[str, Any],
             denied: Sequence[str] = ()) -> tuple[str, ...]:
    """The declared profiles a shift may still use, as an intersection.

    Three narrowings compose and none of them can widen: the run contract's own
    list, the Owner's denials, and capacity.  A profile with no reading is kept
    -- capacity is opt-in, and an unmeasured runtime is unmeasured, not unusable
    -- while a profile whose reading says it is not usable is dropped.
    """

    out = []
    for profile in profiles:
        if profile in denied:
            continue
        reading = readings.get(profile)
        if reading is None:
            out.append(profile)
            continue
        usable = (reading.get("usable") if isinstance(reading, Mapping)
                  else getattr(reading, "usable", None))
        if usable:
            out.append(profile)
    return tuple(out)


# --------------------------------------------------------------------------- #
# the state, which is arithmetic and not a record
# --------------------------------------------------------------------------- #

@dataclass
class ShiftFacts:
    """The present, as five readings.  Nothing here is a decision."""

    gate_ready: bool = False
    control_state: str = "stopped"
    missions_in_flight: int = 0
    missions_admitted: int = 0
    spend: float = 0.0
    emergency_stop: bool = False
    portfolio_complete: bool = False
    protected_surface_conflict: bool = False
    unresolved_uncertain_dispatches: int = 0
    consecutive_failures: int = 0
    failure_threshold: int = 3
    eligible_profiles: Sequence[str] = ()
    capacity_measured: bool = False
    acceptance_gate_available: bool = True


#: The supervisor states in which the control plane will admit new work.  Read
#: from ``supervisor.ADMITTING`` by test rather than copied by hand; stated here
#: so this module does not import the plane it is describing.
ADMITTING_CONTROL_STATES = frozenset({"running"})


def drain_reasons(grant: Grant | None, facts: ShiftFacts, now: float) -> tuple[str, ...]:
    """Every reason the shift must stop admitting, in the order they are read.

    All twelve are readings of durable state.  None is a flag somebody sets,
    which is what makes "the shift stopped itself" checkable after the fact.
    """

    if grant is None:
        return ()
    out = []
    if grant.revoked_at is not None:
        out.append("OWNER_STOP")
    if facts.emergency_stop:
        out.append("EMERGENCY_STOP")
    if facts.portfolio_complete:
        out.append("PORTFOLIO_COMPLETE")
    if facts.missions_admitted >= grant.mission_ceiling:
        out.append("MISSION_CEILING_REACHED")
    if now >= grant.expires_at:
        out.append("SHIFT_WINDOW_EXPIRED")
    if facts.spend >= grant.budget_ceiling:
        out.append("BUDGET_CEILING_REACHED")
    if not facts.gate_ready and facts.missions_in_flight:
        out.append("READINESS_LOST")
    if facts.capacity_measured and not facts.eligible_profiles:
        out.append("CAPACITY_EXHAUSTED_NO_ELIGIBLE_RUNTIME")
    if facts.protected_surface_conflict:
        out.append("PROTECTED_SURFACE_CONFLICT")
    if facts.unresolved_uncertain_dispatches:
        out.append("PROVIDER_UNCERTAINTY_UNRESOLVED")
    if facts.consecutive_failures >= facts.failure_threshold:
        out.append("REPEATED_MISSION_FAILURE")
    if not facts.acceptance_gate_available:
        out.append("ACCEPTANCE_GATE_UNAVAILABLE")
    return tuple(out)


def state(grant: Grant | None, facts: ShiftFacts, now: float) -> str:
    """``off | preparing | active | draining | suspended``, derived every time.

    The order matters and is the contract: a revoked or expired grant with work
    still running is ``draining`` and not ``off``, because calling it ``off``
    while a provider process is alive is exactly the misreport that makes a
    duplicate irreversible effect possible on the next start.
    """

    if grant is None:
        return "off"
    if grant.revoked_at is not None:
        return "draining" if facts.missions_in_flight else "off"
    if grant.suspended_at is not None:
        return "draining" if facts.missions_in_flight else "suspended"
    if drain_reasons(grant, facts, now):
        return "draining" if facts.missions_in_flight else "off"
    if not facts.gate_ready:
        return "preparing"
    if facts.control_state not in ADMITTING_CONTROL_STATES:
        return "preparing"
    return "active"


def admission(grant: Grant | None, portfolio_: Portfolio, facts: ShiftFacts,
              outcomes: Mapping[str, str], now: float) -> dict[str, Any]:
    """Whether one more mission may start, and which one it would be.

    Deliberately the only place that says yes.  Everything else in this module
    reads; this is where a reading becomes an admission, so the refusal codes
    are named and the next mission is returned by the portfolio's own order
    rather than chosen here.
    """

    current = state(grant, facts, now)
    if current != "active":
        reasons = drain_reasons(grant, facts, now)
        return {"admitted": False, "state": current,
                "code": ("SHIFT_NOT_ACTIVE" if not reasons
                         else "SHIFT_DRAINING"),
                "detail": "the shift is %s" % current,
                "drain_reasons": list(reasons), "mission": None}
    if facts.missions_in_flight:
        return {"admitted": False, "state": current,
                "code": "SHIFT_MISSION_IN_FLIGHT",
                "detail": "the first portfolio is strictly serial and one "
                          "mission is already running",
                "drain_reasons": [], "mission": None}
    mission = portfolio_.next_mission(outcomes)
    if mission is None:
        return {"admitted": False, "state": current,
                "code": "SHIFT_PORTFOLIO_COMPLETE",
                "detail": "every mission in the portfolio has settled",
                "drain_reasons": ["PORTFOLIO_COMPLETE"], "mission": None}
    return {"admitted": True, "state": current, "code": "SHIFT_ADMITTED",
            "detail": "mission %d of %d" % (mission.order, len(portfolio_.missions)),
            "drain_reasons": [], "mission": mission.as_row(),
            "eligible_profiles": list(facts.eligible_profiles),
            "remaining_missions": grant.mission_ceiling - facts.missions_admitted}


# --------------------------------------------------------------------------- #
# the Owner's four acts
# --------------------------------------------------------------------------- #

def approval_record(path: str | None, *, request_ref: str) -> dict[str, Any]:
    """Read the Owner's recorded shift decision, or report its absence.

    ``activation.approval`` already does this work for the supervisor service
    and is reused rather than copied, keyed on ``request_ref`` under this
    module's own schema so neither record can be replayed as the other.  Named
    ``approval`` for the reason ``activation.py`` records: the word the brief
    uses is credential-shaped, ``tests/test_authority_boundaries.py`` matches
    it as a bare substring, and ``production.py`` has spelled a person's
    recorded decision ``approved_by``/``approval_ref`` since Stage 6.
    """

    return activation.approval(path, label=request_ref, schema=APPROVAL_SCHEMA,
                               subject_key="request_ref")


class ShiftPlane:
    """The four Owner acts, over a store that already holds everything else.

    It writes exactly four kinds of row -- a grant, a revocation, a suspension
    and a resumption -- and reads everything else from planes that already own
    it.  It holds no copy of readiness, capacity, budget or mission state,
    because a governance layer that cached any of those would be a second place
    they could disagree.
    """

    def __init__(self, store, *, clock=time.time) -> None:
        self._store = store
        self.clock = clock
        with store.transaction() as db:
            db.executescript(SCHEMA)

    # -- reads ------------------------------------------------------------- #

    def grant(self, request_ref: str | None = None) -> Grant | None:
        """The named grant, or the one live grant if no name is given."""

        with self._store.transaction() as db:
            if request_ref is not None:
                row = db.execute("SELECT * FROM shift_grants WHERE request_ref=?",
                                 (request_ref,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM shift_grants WHERE revoked_at IS NULL"
                    " ORDER BY granted_at DESC LIMIT 1").fetchone()
        return None if row is None else Grant(**dict(row))

    def grants(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._store.transaction() as db:
            rows = db.execute("SELECT * FROM shift_grants ORDER BY granted_at"
                              " DESC LIMIT ?", (limit,)).fetchall()
        return [Grant(**dict(row)).as_row() for row in rows]

    def events(self, request_ref: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        with self._store.transaction() as db:
            if request_ref is None:
                rows = db.execute("SELECT * FROM shift_events ORDER BY sequence"
                                  " DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM shift_events WHERE request_ref=?"
                                  " ORDER BY sequence DESC LIMIT ?",
                                  (request_ref, limit)).fetchall()
        return [{"sequence": row["sequence"], "request_ref": row["request_ref"],
                 "event": row["event"], "actor": row["actor"],
                 "created_at": row["created_at"],
                 "detail": json.loads(row["detail_json"])} for row in rows]

    def _event(self, db, request_ref: str, event: str, actor: str,
               detail: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO shift_events"
                   " (request_ref, event, actor, detail_json, created_at)"
                   " VALUES (?,?,?,?,?)",
                   (request_ref, event, actor,
                    json.dumps(dict(detail), sort_keys=True), self.clock()))

    def outcomes(self, portfolio_: Portfolio) -> dict[str, str]:
        """Each portfolio mission's durable state, or absence of one.

        Keyed on the mission's idempotency key rather than its generated id: a
        portfolio names work, and the key is the only identifier a portfolio
        author can know before the mission exists.
        """

        refs = [mission.mission_ref for mission in portfolio_.missions]
        if not refs:
            return {}
        marks = ",".join("?" * len(refs))
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT idempotency_key, state FROM missions"
                " WHERE idempotency_key IN (%s)" % marks, refs).fetchall()
        return {row["idempotency_key"]: row["state"] for row in rows}

    def observe(self, portfolio_: Portfolio, *, control_state: str,
                gate_ready: bool, capacity_readings: Mapping[str, Any] | None = None,
                profiles: Sequence[str] = (), denied: Sequence[str] = (),
                emergency_stop: bool = False, failure_threshold: int = 3,
                protected_surface_conflict: bool = False,
                acceptance_gate_available: bool = True,
                unresolved_uncertain_dispatches: int = 0,
                consecutive_failures: int = 0) -> ShiftFacts:
        """The present, read once, from the planes that already own each fact.

        Spend is taken as measured or not taken at all.  A portfolio whose
        provider returned no usage block reports ``not_measurable``, and
        treating that as ``0.0`` would let an unmeasured shift run to a ceiling
        it was never compared against -- so it is carried as unmeasured and the
        budget drain reason simply does not fire, which is a different fact
        from the budget being fine.
        """

        readings = dict(capacity_readings or {})
        settled = self.outcomes(portfolio_)
        refs = {mission.mission_ref for mission in portfolio_.missions}
        in_flight = sum(1 for ref, state_ in settled.items()
                        if ref in refs and state_ not in TERMINAL_MISSION_STATES)
        total = self._store.portfolio_economics()["portfolio"]
        spend = total.get("known_spend")
        # Counted from the portfolio's own order, not supplied by a caller: a
        # threshold nobody can reach from durable state is a stop condition
        # that can never fire.
        streak = 0
        for mission in portfolio_.missions:
            outcome = settled.get(mission.mission_ref)
            if outcome in UNSUCCESSFUL_MISSION_STATES:
                streak += 1
            elif outcome is not None:
                streak = 0
        return ShiftFacts(
            gate_ready=gate_ready, control_state=control_state,
            missions_in_flight=in_flight, missions_admitted=len(settled),
            spend=float(spend) if isinstance(spend, float) else 0.0,
            emergency_stop=emergency_stop,
            portfolio_complete=portfolio_.complete(settled),
            protected_surface_conflict=protected_surface_conflict,
            unresolved_uncertain_dispatches=unresolved_uncertain_dispatches,
            consecutive_failures=max(streak, consecutive_failures),
            failure_threshold=failure_threshold,
            eligible_profiles=eligible(profiles, readings, denied),
            capacity_measured=bool(readings),
            acceptance_gate_available=acceptance_gate_available)

    # -- preview ----------------------------------------------------------- #

    def preview(self, facts: GateFacts, *,
                approval: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Every blocker, and the exact effects an apply would authorize.

        Writes nothing.  The point of showing the effects beside the blockers
        is that the Owner is approving a specific bounded thing rather than
        agreeing that the lights are green: "what would become true" is the
        half of an activation decision a readiness report never contains.
        """

        reading = gate(facts)
        request = facts.request
        existing = self.grant(request.request_ref)
        now = self.clock()
        return {
            "contract_version": CONTRACT_VERSION,
            "action": "preview",
            "request": request.as_row(),
            "gate": reading,
            "ready": reading["ready"],
            "blockers": reading["blockers"],
            "approval": dict(approval or approval_record(None,
                                                         request_ref=request.request_ref)),
            "already_granted": existing is not None and existing.revoked_at is None,
            "would_authorize": {
                "portfolio_ref": facts.portfolio.portfolio_ref,
                "missions": [mission.as_row() for mission in facts.portfolio.missions],
                "mission_ceiling": request.mission_ceiling,
                "expires_at": now + request.duration_seconds,
                "budget_ceiling": request.budget_ceiling,
                "budget_currency": request.budget_currency,
                "projects": sorted({mission.project_id
                                    for mission in facts.portfolio.missions}),
                "work_classes": sorted({mission.work_class
                                        for mission in facts.portfolio.missions}),
                "environment_classes": sorted({mission.environment_class
                                               for mission in facts.portfolio.missions}),
                "eligible_profiles": list(facts.eligible_profiles),
                "repository_mutating_missions": [
                    mission.mission_ref for mission in facts.portfolio.missions
                    if mission.mutates_repository],
            },
            "would_not_authorize": [
                "loading, installing or unloading any host service",
                "widening an admitted capability or project",
                "any production environment class",
                "any provider not named by the run contract",
                "any mission beyond the portfolio's ordered list",
            ],
        }

    # -- apply ------------------------------------------------------------- #

    def apply(self, facts: GateFacts, approval: Mapping[str, Any], *,
              actor: str = "owner") -> dict[str, Any]:
        """Turn a positive reading plus a recorded decision into a grant.

        Idempotent by ``request_ref``: a second apply of the same request
        returns the same grant and writes nothing.  That is not a convenience.
        An Owner who runs the command twice, or a script that retries, must not
        end up with two overlapping ceilings -- the mission-ceiling arithmetic
        would then be counting against a bound nobody chose.
        """

        request = facts.request
        existing = self.grant(request.request_ref)
        if existing is not None:
            if existing.revoked_at is not None:
                raise ShiftGovernanceRefusal(
                    "SHIFT_GRANT_REVOKED",
                    "request %r was revoked at %.0f and cannot be re-applied; "
                    "a new decision needs a new request_ref"
                    % (request.request_ref, existing.revoked_at))
            return {"action": "apply", "created": False,
                    "grant": existing.as_row(),
                    "detail": "this request already holds a grant"}
        live = self.grant()
        if live is not None:
            raise ShiftGovernanceRefusal(
                "SHIFT_ALREADY_ACTIVE",
                "request %r already holds a live grant; revoke it before "
                "opening another shift" % live.request_ref,
                request_ref=live.request_ref)
        reading = gate(facts)
        if not reading["ready"]:
            raise ShiftGovernanceRefusal(
                "SHIFT_GATE_UNMET",
                "%d gate checks are not met" % len(reading["blockers"]),
                blockers=reading["blockers"])
        if not approval.get("approved"):
            raise ShiftGovernanceRefusal(
                "SHIFT_UNAPPROVED",
                "opening a shift needs a durable Owner approval; %s"
                % approval.get("detail", "none was supplied"),
                approval=dict(approval))
        now = self.clock()
        grant = Grant(
            request_ref=request.request_ref, run_ref=request.run_ref,
            portfolio_ref=facts.portfolio.portfolio_ref,
            approved_by=approval["approved_by"],
            approval_ref=approval["approval_ref"],
            gate_digest=reading["digest"],
            mission_ceiling=request.mission_ceiling,
            budget_ceiling=float(request.budget_ceiling),
            budget_currency=request.budget_currency,
            granted_at=now, expires_at=now + request.duration_seconds)
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO shift_grants (request_ref, run_ref, portfolio_ref,"
                " approved_by, approval_ref, gate_digest, mission_ceiling,"
                " budget_ceiling, budget_currency, granted_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (grant.request_ref, grant.run_ref, grant.portfolio_ref,
                 grant.approved_by, grant.approval_ref, grant.gate_digest,
                 grant.mission_ceiling, grant.budget_ceiling,
                 grant.budget_currency, grant.granted_at, grant.expires_at))
            self._event(db, grant.request_ref, "granted", actor,
                        {"gate_digest": grant.gate_digest,
                         "mission_ceiling": grant.mission_ceiling,
                         "expires_at": grant.expires_at,
                         "approval_ref": grant.approval_ref})
        return {"action": "apply", "created": True, "grant": grant.as_row(),
                "detail": "shift authorized until %.0f" % grant.expires_at}

    # -- revoke, suspend, resume ------------------------------------------- #

    def revoke(self, request_ref: str, *, reason: str,
               actor: str = "owner") -> dict[str, Any]:
        """End a grant.  Idempotent, and never conditional on the work's state.

        A revocation stops *admission* immediately; missions already running
        are drained rather than killed, which is why ``state`` reports
        ``draining`` and not ``off`` until the last one settles.
        """

        grant = self.grant(request_ref)
        if grant is None:
            raise ShiftGovernanceRefusal("SHIFT_GRANT_UNKNOWN",
                               "no shift grant named %r" % request_ref)
        if grant.revoked_at is not None:
            return {"action": "revoke", "changed": False,
                    "grant": grant.as_row(),
                    "detail": "already revoked"}
        now = self.clock()
        with self._store.transaction() as db:
            db.execute("UPDATE shift_grants SET revoked_at=?, revoke_reason=?"
                       " WHERE request_ref=?", (now, reason, request_ref))
            self._event(db, request_ref, "revoked", actor, {"reason": reason})
        return {"action": "revoke", "changed": True,
                "grant": self.grant(request_ref).as_row(),
                "detail": "admission stopped; work already running drains"}

    def suspend(self, request_ref: str, *, resume_ref: str,
                missions_in_flight: int, actor: str = "owner") -> dict[str, Any]:
        """Park a live grant behind a durable handover reference.

        Refused while work is in flight, and that refusal is the contract, not
        a limitation: suspension exists so a shift can be resumed from
        repository, Evidence and Context state alone.  A suspension recorded
        over a running provider process would be handing over a state that was
        not yet true.
        """

        grant = self.grant(request_ref)
        if grant is None:
            raise ShiftGovernanceRefusal("SHIFT_GRANT_UNKNOWN",
                               "no shift grant named %r" % request_ref)
        if grant.revoked_at is not None:
            raise ShiftGovernanceRefusal("SHIFT_GRANT_REVOKED",
                               "a revoked grant has nothing to suspend")
        if missions_in_flight:
            raise ShiftGovernanceRefusal(
                "SHIFT_DRAIN_REQUIRED",
                "%d missions are still running; drain before suspending so "
                "the handover describes a state that is actually true"
                % missions_in_flight)
        if not resume_ref or not isinstance(resume_ref, str):
            raise ShiftGovernanceRefusal(
                "SHIFT_RESUME_REF_REQUIRED",
                "a suspension names where the durable state is recorded, or "
                "resuming would depend on somebody remembering it")
        if grant.suspended_at is not None:
            return {"action": "suspend", "changed": False,
                    "grant": grant.as_row(), "detail": "already suspended"}
        now = self.clock()
        with self._store.transaction() as db:
            db.execute("UPDATE shift_grants SET suspended_at=?, resume_ref=?"
                       " WHERE request_ref=?", (now, resume_ref, request_ref))
            self._event(db, request_ref, "suspended", actor,
                        {"resume_ref": resume_ref})
        return {"action": "suspend", "changed": True,
                "grant": self.grant(request_ref).as_row(),
                "detail": "resume from %s" % resume_ref}

    def resume(self, request_ref: str, *, actor: str = "owner") -> dict[str, Any]:
        """Unpark a suspended grant.  The expiry is not extended.

        Resuming is not a new decision, so it does not get a new window: a
        suspension that could push the expiry out would make the twelve-hour
        ceiling a suggestion.
        """

        grant = self.grant(request_ref)
        if grant is None:
            raise ShiftGovernanceRefusal("SHIFT_GRANT_UNKNOWN",
                               "no shift grant named %r" % request_ref)
        if grant.revoked_at is not None:
            raise ShiftGovernanceRefusal("SHIFT_GRANT_REVOKED",
                               "a revoked grant cannot be resumed; a new "
                               "decision needs a new request_ref")
        if grant.suspended_at is None:
            return {"action": "resume", "changed": False,
                    "grant": grant.as_row(), "detail": "not suspended"}
        with self._store.transaction() as db:
            db.execute("UPDATE shift_grants SET suspended_at=NULL,"
                       " resume_ref=NULL WHERE request_ref=?", (request_ref,))
            self._event(db, request_ref, "resumed", actor,
                        {"resume_ref": grant.resume_ref,
                         "expires_at": grant.expires_at})
        return {"action": "resume", "changed": True,
                "grant": self.grant(request_ref).as_row(),
                "detail": "the original expiry stands"}


# --------------------------------------------------------------------------- #
# the Owner-facing brief
# --------------------------------------------------------------------------- #

def brief(grant: Grant | None, facts: ShiftFacts, reading: Mapping[str, Any],
          portfolio_: Portfolio, outcomes: Mapping[str, str], now: float, *,
          phase: str = "internal dogfood, on-demand",
          admitted_projects: Sequence[str] = (),
          admitted_capabilities: Sequence[str] = (),
          checkpoints: Sequence[Mapping[str, Any]] = (),
          owner_actions: Sequence[Mapping[str, str]] = ()) -> dict[str, Any]:
    """The eleven answers an Owner needs, and the one act that follows them.

    Assembled rather than computed: every value here is produced by the module
    that owns it.  The brief's only original content is ``next_owner_action``,
    which is the first unmet blocker expressed as something a person can do --
    and when there is none, the activation command itself.
    """

    current = state(grant, facts, now)
    reasons = drain_reasons(grant, facts, now)
    blockers = list(reading.get("blockers", ()))
    if blockers:
        first = blockers[0]
        action = {"act": "resolve %s" % first["check"], "detail": first["detail"]}
    elif grant is None:
        action = {"act": "record a shift approval and apply it",
                  "detail": "the gate is met; what is missing is the decision"}
    elif current == "active":
        action = {"act": "none", "detail": "the shift is active and bounded"}
    elif current == "suspended":
        action = {"act": "resume the shift",
                  "detail": "durable state is at %s" % _absent(grant.resume_ref)}
    else:
        action = {"act": "review the drain reasons",
                  "detail": ", ".join(reasons) or "no admission is possible"}
    return {
        "contract_version": CONTRACT_VERSION,
        "brief": "OWNER-DOGFOOD-BRIEF",
        "phase": phase,
        "shift_state": current,
        "activation_readiness": {"ready": bool(reading.get("ready")),
                                 "states": reading.get("states", {}),
                                 "gate": reading.get("gate", "unknown")},
        "unresolved_owner_actions": [dict(item) for item in owner_actions],
        "admitted_projects": list(admitted_projects),
        "admitted_capabilities": list(admitted_capabilities),
        "usable_runtimes": list(facts.eligible_profiles),
        "capacity": {"measured": facts.capacity_measured,
                     "eligible": list(facts.eligible_profiles)},
        "proposed_missions": [
            {**mission.as_row(),
             "outcome": outcomes.get(mission.mission_ref, "not_run")}
            for mission in portfolio_.missions],
        "next_mission": (lambda mission: None if mission is None
                         else mission.as_row())(portfolio_.next_mission(outcomes)),
        "risk": {"drain_reasons": list(reasons),
                 "stop_conditions_armed": list(DRAIN_REASONS)},
        "checkpoints": [dict(item) for item in checkpoints],
        "grant": None if grant is None else grant.as_row(),
        "blockers": blockers,
        "next_owner_action": action,
    }
