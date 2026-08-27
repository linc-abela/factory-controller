"""Multi-project coordination: registry policy, dependency graph, scheduler.

Stage 4 asked one question of one mission at a time -- *may this proceed?*
Stage 5 adds the second question, *which one*, across projects competing for a
single host.  That question is the whole of this module.  Nothing here
dispatches, picks a provider, opens a repository, or touches a network: it is a
decision over durable facts the store already holds.

Two shapes are deliberately absent.

There is no ``blocked`` mission state.  Whether a mission's prerequisites are
met is *derived* from its dependency edges every time it is asked, so the answer
cannot drift away from the edges the way a stored flag can.  "Blocked" and
"ready" are readings of the graph, not rows.

There is no scheduler process either.  ``MissionStore.claim`` calls
:func:`schedule` inside the same ``BEGIN IMMEDIATE`` transaction that takes the
lease, so the Stage-2 property that no two workers claim one mission is
inherited rather than re-implemented -- and a second scheduler could not hand
out a duplicate claim even if one existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import capacity as capacity_policy


#: A project admits *new* missions only while ``enabled``.  ``paused`` and
#: ``draining`` share their claim mechanics exactly and differ only in declared
#: intent -- draining means the Owner is waiting for in-flight work to finish --
#: so :func:`drained` is the only place they diverge.  Saying that plainly is
#: better than inventing a mechanical difference to justify two words.
PROJECT_STATES = ("enabled", "paused", "draining", "stopped")
ADMITTING = frozenset({"enabled"})

#: What happens to a dependent when a prerequisite reaches a non-completed
#: terminal state.  ``block`` is the default because a prerequisite that failed
#: has not produced the thing the dependent was waiting for.
ON_FAILURE = ("block", "cancel", "ignore")

DEFAULT_PRIORITY = 100
DEFAULT_AGING_SECONDS = 300.0
DEFAULT_PORTFOLIO_CONCURRENCY = 4
DEFAULT_PROJECT_CONCURRENCY = 2

#: A prerequisite is satisfied only by ``completed``.  Every other terminal
#: state is a failure for the purposes of the graph, including ``cancelled``:
#: the dependent is waiting for an artifact, and a cancelled mission produced
#: none.
SATISFIED = "completed"
TERMINAL = frozenset({"completed", "refused", "failed", "cancelled"})


class PolicyError(ValueError):
    """A registry or dependency declaration the Controller will not store."""


class GateProvenanceError(ValueError):
    """No lawful acceptance gate can be sourced for a piece of work.

    Carried rather than raised through a cycle: a project whose gates are
    undeclared is a recorded refusal for that project, not a failed cycle for
    the whole portfolio.  One type, one owner -- the supervisor, the CLI and the
    two planes all read the same declaration, so a second resolution rule cannot
    disagree with the first.
    """

    def __init__(self, code: str, detail: dict[str, Any]) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": dict(self.detail)}


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProjectPolicy:
    """One project's Owner-set operating envelope.

    ``priority`` is an ordering position, not a weight: lower runs first, and
    it is only ever compared, never multiplied by anything.  A weight would make
    the effect of one project's number depend on every other project's number,
    which is exactly what makes starvation hard to reason about.

    ``portfolio_concurrency`` is spelled that way rather than "global" on
    purpose: ``tests/test_authority_boundaries.py`` matches ``glob`` as a
    substring to catch a directory scan, and "global" contains it.  The check is
    right to be blunt, so the name moved rather than the check.

    ``acceptance_gate_ids`` is the project's *declared* acceptance gates and the
    only lawful source of a gate identifier for work nobody typed.  It carries
    ``acceptance_gate_source`` for the same reason a budget carries a currency:
    a gate list with no provenance is indistinguishable from an invented one,
    and SF-141 found the supervisor promoting repairs against a literal
    ``ACCEPTANCE`` that no repository declares.  The Controller cannot read the
    target repository -- that is the Context Broker's authority and
    ``tests/test_authority_boundaries.py`` enforces it -- so the declaration
    reaches durable state through the Owner's registry act, which names where it
    was copied from.  Neither field may be defaulted: an empty list means the
    project has declared none, and unattended promotion fails closed on it.
    """

    project_id: str
    repository: str
    state: str = "enabled"
    priority: int = DEFAULT_PRIORITY
    concurrency_cap: int = DEFAULT_PROJECT_CONCURRENCY
    budget_ceiling: float | None = None
    budget_currency: str | None = None
    context_ceiling_bytes: int | None = None
    acceptance_gate_ids: tuple[str, ...] = ()
    acceptance_gate_source: str | None = None
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise PolicyError("project_id is required")
        if not isinstance(self.repository, str) or not self.repository:
            raise PolicyError("project %s needs a repository binding" % self.project_id)
        if self.state not in PROJECT_STATES:
            raise PolicyError("project state must be one of %s" % (PROJECT_STATES,))
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise PolicyError("priority must be an integer")
        if not isinstance(self.concurrency_cap, int) or self.concurrency_cap < 0:
            raise PolicyError("concurrency_cap must be a non-negative integer")
        if self.budget_ceiling is not None:
            if self.budget_ceiling < 0:
                raise PolicyError("budget_ceiling must not be negative")
            if not self.budget_currency:
                # A ceiling without a currency cannot be compared to a priced
                # receipt, and comparing it anyway is how a budget silently
                # becomes advice.
                raise PolicyError("a budget ceiling requires a currency")
        if self.context_ceiling_bytes is not None and self.context_ceiling_bytes < 0:
            raise PolicyError("context_ceiling_bytes must not be negative")
        if not isinstance(self.acceptance_gate_ids, tuple):
            raise PolicyError("acceptance_gate_ids must be a tuple of gate ids")
        for gate in self.acceptance_gate_ids:
            if not isinstance(gate, str) or not gate.strip():
                raise PolicyError("an acceptance gate id is a non-empty string")
        if len(set(self.acceptance_gate_ids)) != len(self.acceptance_gate_ids):
            raise PolicyError("acceptance gate ids are declared once each")
        if bool(self.acceptance_gate_ids) != bool(self.acceptance_gate_source):
            # Exactly the budget/currency rule, for exactly the same reason: a
            # declaration nobody can trace back to the target repository is the
            # invented gate this field exists to replace.
            raise PolicyError(
                "declared acceptance gates require an acceptance_gate_source "
                "naming where they were read from, and a source without gates "
                "declares nothing")

    @property
    def admits_new_work(self) -> bool:
        return self.state in ADMITTING

    def as_row(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "repository": self.repository,
            "state": self.state, "priority": self.priority,
            "concurrency_cap": self.concurrency_cap,
            "budget_ceiling": self.budget_ceiling,
            "budget_currency": self.budget_currency,
            "context_ceiling_bytes": self.context_ceiling_bytes,
            "acceptance_gate_ids": list(self.acceptance_gate_ids),
            "acceptance_gate_source": self.acceptance_gate_source or "not_applicable",
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class PortfolioPolicy:
    """Portfolio-wide limits.  One row; the Owner's global word."""

    portfolio_concurrency: int = DEFAULT_PORTFOLIO_CONCURRENCY
    emergency_stop: bool = False
    aging_seconds: float = DEFAULT_AGING_SECONDS
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not isinstance(self.portfolio_concurrency, int) or self.portfolio_concurrency < 0:
            raise PolicyError("portfolio_concurrency must be a non-negative integer")
        if self.aging_seconds < 0:
            raise PolicyError("aging_seconds must not be negative")

    def as_row(self) -> dict[str, Any]:
        return {"portfolio_concurrency": self.portfolio_concurrency,
                "emergency_stop": self.emergency_stop,
                "aging_seconds": self.aging_seconds,
                "policy_version": self.policy_version}


# --------------------------------------------------------------------------- #
# the dependency graph
# --------------------------------------------------------------------------- #

def reachable(edges: Mapping[str, Iterable[str]], start: str) -> set[str]:
    """Every node reachable from ``start`` along ``mission -> prerequisite``."""

    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in edges.get(node, ()):  # type: ignore[arg-type]
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def cycle_path(edges: Mapping[str, Iterable[str]], mission_id: str,
               depends_on: str) -> tuple[str, ...] | None:
    """The cycle that adding ``mission_id -> depends_on`` would create, if any.

    Edges point from a mission to what it waits for, so a new edge closes a
    cycle exactly when ``mission_id`` is already reachable from ``depends_on``.
    The path is returned rather than a bare boolean because a refusal nobody can
    act on is barely better than no refusal.
    """

    if mission_id == depends_on:
        return (mission_id, depends_on)
    if mission_id not in reachable(edges, depends_on):
        return None
    path = [depends_on]
    seen = {depends_on}
    while path[-1] != mission_id:
        for nxt in edges.get(path[-1], ()):  # type: ignore[arg-type]
            if nxt == mission_id or (nxt not in seen and mission_id in reachable(edges, nxt)):
                seen.add(nxt)
                path.append(nxt)
                break
        else:  # pragma: no cover - reachability above guarantees a step exists
            return None
    return (mission_id, *path)


@dataclass(frozen=True)
class Prerequisite:
    mission_id: str
    state: str
    on_failure: str

    @property
    def satisfied(self) -> bool:
        return self.state == SATISFIED

    @property
    def failed(self) -> bool:
        return self.state in TERMINAL and self.state != SATISFIED


def dependency_reading(prerequisites: Sequence[Prerequisite]) -> dict[str, Any]:
    """Derive ready/waiting/blocked for one mission from its edges alone."""

    unmet = tuple(item.mission_id for item in prerequisites if not item.satisfied)
    blocking = tuple(item.mission_id for item in prerequisites
                     if item.failed and item.on_failure == "block")
    cancelling = tuple(item.mission_id for item in prerequisites
                       if item.failed and item.on_failure == "cancel")
    ignored = tuple(item.mission_id for item in prerequisites
                    if item.failed and item.on_failure == "ignore")
    if blocking:
        reading = "blocked"
    elif cancelling:
        reading = "cancelling"
    elif unmet and set(unmet) - set(ignored):
        reading = "waiting"
    else:
        reading = "ready"
    return {"reading": reading, "unmet": unmet, "blocking": blocking,
            "cancelling": cancelling, "failure_ignored": ignored,
            "prerequisite_count": len(prerequisites)}


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MissionCandidate:
    mission_id: str
    project_id: str | None
    state: str
    created_at: float
    ready_at: float
    prerequisites: tuple[Prerequisite, ...] = ()
    priority: int | None = None
    #: The execution profiles this mission declared, in declared order.  Empty
    #: means the mission named none and the execution layer's own default
    #: serves it -- capacity has no subject and does not narrow it.
    runtimes: tuple[str, ...] = ()
    estimate: "capacity_policy.WorkEstimate | None" = None

    @property
    def resume(self) -> bool:
        """True when this mission already crossed the dispatch boundary.

        A resume is not new work.  It is a mission whose provider process may
        already have run and whose lease was lost, and refusing to pick it up
        because a project was paused would leave durable state half-finished --
        the corruption the pause is supposed to prevent.
        """

        return self.state != "admitted"


@dataclass(frozen=True)
class Snapshot:
    portfolio: PortfolioPolicy
    projects: Mapping[str, ProjectPolicy]
    candidates: Sequence[MissionCandidate]
    in_flight: Mapping[str, int]
    portfolio_in_flight: int
    project_spend: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    now: float = 0.0
    #: One reading per runtime the Owner registered or anybody measured.  An
    #: empty mapping is the pre-capacity Factory and narrows nothing, which is
    #: what keeps capacity opt-in.
    capacity: Mapping[str, "capacity_policy.RuntimeReading"] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    mission_id: str
    project_id: str | None
    admitted: bool
    reason: str
    effective_priority: int | None
    waited_seconds: float
    aging_steps: int
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "project_id": self.project_id,
                "admitted": self.admitted, "reason": self.reason,
                "effective_priority": self.effective_priority,
                "waited_seconds": round(self.waited_seconds, 6),
                "aging_steps": self.aging_steps, "detail": self.detail}


@dataclass(frozen=True)
class ScheduleDecision:
    selected: str | None
    reason: str
    verdicts: tuple[Verdict, ...]

    def as_row(self) -> dict[str, Any]:
        return {"selected": self.selected, "reason": self.reason,
                "considered": [verdict.as_row() for verdict in self.verdicts]}


def _aging_steps(waited: float, aging_seconds: float) -> int:
    if aging_seconds <= 0:
        return 0
    return int(waited // aging_seconds)


def _budget_reason(project: ProjectPolicy, spend: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Refuse the *next* dispatch once measured spend reaches the ceiling.

    Only measured, priced spend counts.  Unknown provider cost stays unknown --
    it is never estimated, never zeroed, and never counted toward a ceiling,
    which is the rule Stage 3 set for the mission ceiling and this is the same
    rule one level up.
    """

    if project.budget_ceiling is None:
        return None
    currency = spend.get("currency")
    if currency is not None and currency != project.budget_currency:
        return ("PROJECT_BUDGET_CURRENCY_MISMATCH",
                {"ceiling_currency": project.budget_currency, "spend_currency": currency})
    known = float(spend.get("known_spend") or 0.0)
    if known >= project.budget_ceiling:
        return ("PROJECT_BUDGET_EXHAUSTED",
                {"known_spend": known, "ceiling": project.budget_ceiling,
                 "currency": project.budget_currency,
                 "unpriced_legs": spend.get("unpriced_legs", 0)})
    return None


def evaluate(candidate: MissionCandidate, snapshot: Snapshot) -> Verdict:
    """Why this one mission may or may not start right now."""

    waited = max(0.0, snapshot.now - candidate.created_at)
    steps = _aging_steps(waited, snapshot.portfolio.aging_seconds)

    def verdict(admitted: bool, reason: str, priority: int | None = None,
                **detail: Any) -> Verdict:
        return Verdict(candidate.mission_id, candidate.project_id, admitted, reason,
                       priority, waited, steps, detail)

    if candidate.ready_at > snapshot.now:
        return verdict(False, "NOT_YET_RUNNABLE", ready_at=candidate.ready_at)
    if candidate.resume:
        # Deliberately ahead of every gate below.  See MissionCandidate.resume.
        return verdict(True, "RESUME_AFTER_BOUNDARY", state=candidate.state)
    if snapshot.portfolio.emergency_stop:
        return verdict(False, "PORTFOLIO_EMERGENCY_STOP")

    project: ProjectPolicy | None = None
    if candidate.project_id is not None:
        project = snapshot.projects.get(candidate.project_id)
        if project is None:
            # A mission naming a project nobody registered has no budget, no
            # cap, and no Owner policy version.  It is refused rather than run
            # under portfolio defaults it was never admitted against.
            return verdict(False, "PROJECT_UNREGISTERED")

    reading = dependency_reading(candidate.prerequisites)
    if reading["blocking"]:
        return verdict(False, "DEPENDENCY_PREREQUISITE_FAILED", **reading)
    if reading["cancelling"]:
        return verdict(False, "DEPENDENCY_PREREQUISITE_FAILED", **reading)
    if reading["reading"] == "waiting":
        return verdict(False, "DEPENDENCY_UNMET", **reading)

    if project is not None:
        if not project.admits_new_work:
            return verdict(False, "PROJECT_NOT_ADMITTING", project_state=project.state)
        used = int(snapshot.in_flight.get(project.project_id, 0))
        if used >= project.concurrency_cap:
            return verdict(False, "PROJECT_CONCURRENCY_CAP",
                           in_flight=used, cap=project.concurrency_cap)
        budget = _budget_reason(project, dict(snapshot.project_spend.get(project.project_id, {})))
        if budget:
            return verdict(False, budget[0], **budget[1])

    if snapshot.portfolio_in_flight >= snapshot.portfolio.portfolio_concurrency:
        return verdict(False, "PORTFOLIO_CONCURRENCY_CAP",
                       in_flight=snapshot.portfolio_in_flight,
                       cap=snapshot.portfolio.portfolio_concurrency)

    # Capacity is deliberately the last gate, and the reason is the same one
    # that orders the refusals inside a capacity reading: report the condition
    # that is true independently of the moment first.  An unregistered project
    # or a spent budget will still be true in five hours; a closed quota window
    # will not, so it is the least useful thing to say about a mission that is
    # also unregistered.  Capacity can only *narrow* what the gates above
    # already admitted -- there is no path from here to an admission.
    if snapshot.capacity and candidate.runtimes:
        plan = capacity_policy.plan(candidate.runtimes, snapshot.capacity, candidate.estimate)
        if plan.exhausted:
            return verdict(False, "CAPACITY_UNAVAILABLE",
                           resume_at="unknown" if plan.resume_at is None else plan.resume_at,
                           considered=[item.as_row() for item in plan.considered])

    base = candidate.priority if candidate.priority is not None else (
        project.priority if project is not None else DEFAULT_PRIORITY)
    return verdict(True, "SCHEDULED", base - steps, base_priority=base)


def schedule(snapshot: Snapshot) -> ScheduleDecision:
    """Pick at most one mission, and explain every mission that was passed over.

    Fairness is unbounded ageing rather than a weight or a lottery: a mission's
    effective rank improves by one step for every ``aging_seconds`` it has
    waited, without limit.  So for *any* pair of priorities there is a finite
    wait after which the lower-priority mission outranks the higher one, which
    makes permanent starvation impossible rather than merely unlikely -- and it
    is a pure function of two durable numbers, so two workers reading the same
    database reach the same answer.
    """

    verdicts = tuple(evaluate(candidate, snapshot) for candidate in snapshot.candidates)
    by_id = {candidate.mission_id: candidate for candidate in snapshot.candidates}
    eligible = [verdict for verdict in verdicts if verdict.admitted]
    if not eligible:
        return ScheduleDecision(None, "NO_ELIGIBLE_MISSION", verdicts)

    def key(verdict: Verdict) -> tuple:
        candidate = by_id[verdict.mission_id]
        return (0 if candidate.resume else 1,
                verdict.effective_priority if verdict.effective_priority is not None else 0,
                candidate.created_at, candidate.mission_id)

    winner = min(eligible, key=key)
    promoted = winner.aging_steps > 0 and any(
        other.effective_priority is not None and winner.effective_priority is not None
        and other.effective_priority > winner.effective_priority
        and (other.effective_priority + other.aging_steps)
        < (winner.effective_priority + winner.aging_steps)
        for other in eligible if other.mission_id != winner.mission_id)
    reason = "STARVATION_PROMOTED" if promoted else winner.reason
    return ScheduleDecision(winner.mission_id, reason, verdicts)


def drained(project_id: str, in_flight: Mapping[str, int]) -> bool:
    return int(in_flight.get(project_id, 0)) == 0
