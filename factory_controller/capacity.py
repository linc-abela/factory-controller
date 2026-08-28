"""Phase 1: subscription quota windows as a scheduling constraint.

Every runtime the Factory can dispatch to is a subscription harness with a
rolling quota window -- roughly five hours at the time of writing.  A window
that closes is not an outage and not an error: it is a *fact about when work
may run*, and the Factory has to keep making progress across the windows it
already pays for rather than waiting for one of them to reset.

The rule this module exists to hold is one sentence: **the Factory is
quota-aware, not quota-dependent.**

Six absences carry the design, and each is an enforcement rather than a
decision deferred:

* **No probe.**  Nothing here asks a provider how much quota it has left.  An
  observation is a durable row somebody else recorded -- the execution layer,
  the Owner, or a provider's own refusal -- carrying who measured it and when.
  The Controller holds no network, no process and no clock authority over a
  vendor's private accounting, so it cannot invent one.
* **No second scheduler.**  Capacity is one more :func:`portfolio.evaluate`
  verdict computed from the same snapshot inside the same claiming
  transaction.  "Capacity-aware scheduling" therefore needed no scheduling
  code at all, exactly as portfolio-aware maintenance and improvement did not.
* **No new mission state.**  A mission that cannot run because every compatible
  runtime is cooling stays ``admitted`` with a later ``next_run_at`` -- which is
  precisely what the scheduler already means by "not yet runnable".  A second
  spelling for waiting would eventually disagree with the first.
* **No widening.**  :func:`plan` returns profiles to *deny*, never profiles to
  add, and the Controller applies them through the Owner's existing
  ``denied_profiles``.  So capacity can shrink the eligible set and has no
  expressible way to grow it: a cooling subscription harness cannot summon a
  metered one, because there is no verb here that appends a candidate.
* **No handoff verb.**  Cross-runtime resumption is the ordinary selector
  running again over a narrower set.  Nothing here moves work between runtimes,
  so nothing here can move work that already ran.
* **No conversational state.**  A capacity checkpoint is a *projection* of
  durable rows -- mission, steps, legs, context binding, capacity reading --
  re-derived on every read rather than copied, so it cannot quietly diverge
  from the ledger it describes.  ``continuity.py`` owns the portable Work Baton
  that a checkpoint lifts into; this module owns only the reading.

Nothing in this module writes.  It is a pure reading over facts the store
holds, which is what lets the scheduler call it inside a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "factory-controller/capacity/1.0"

#: Reproduced from ``factory-evidence-core`` ``src/contracts/replay.py`` and
#: equal to the same set in ``store``, ``routing``, ``production``,
#: ``maintenance``, ``improvement`` and ``supervisor``.  Capacity states are a
#: *different* fact from absence and are deliberately disjoint from these four:
#: "we could not measure the window" is an observation, "nobody recorded one"
#: is an absence, and a test pins them apart.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

#: What one runtime's quota window can be observed to be.
#:
#: ``constrained`` is a window that is open and measurably near its end;
#: ``cooling`` is a window that has closed and is expected to reopen;
#: ``exhausted`` is a window with nothing left and no reset yet observed.  The
#: last two states are the honest readings of a runtime nobody could measure --
#: kept apart because "the probe failed" and "the provider publishes no figure"
#: are different facts about different things to fix.
CAPACITY_STATES = ("available", "constrained", "cooling", "exhausted",
                   "readiness_unavailable", "capacity_unmeasurable")

#: The two states in which a runtime may be dispatched to at all.  Everything
#: else -- including both unmeasurable readings -- is *not* capacity.  An
#: unknown quota fact never becomes a positive one by assumption, which is the
#: whole of Phase-1 principle 7.
USABLE = frozenset({"available", "constrained"})

#: Whether a reset time is a measured fact or an absence.  Derived on read
#: rather than stored, so it cannot disagree with the timestamp beside it.
RESET_STATES = ("reset_known", "reset_unknown", "not_applicable")

#: The precision vocabulary is ``factory-bridge``'s own (``usage_precision``,
#: ``cost_precision`` in its metered receipt): ``exact`` or ``unknown``.  Its
#: ``unknown`` is already one of Evidence Core's four words, so no seventh
#: dialect of absence appears here.
PRECISIONS = ("exact", "unknown")

#: How much of a window a piece of work is expected to consume.  ``unknown`` is
#: the canonical absence word rather than a fifth size, because an unestimated
#: mission is not a small one.
SIZE_CLASSES = ("small", "medium", "large", "unknown")

#: Sizes that may not be started on a runtime already measurably near the end
#: of its window.  A reset returns that runtime to ``available``, where both
#: are admitted again -- so this fails safe without starving anything: the
#: bound is one window, not forever.
CONSTRAINED_REFUSED_SIZES = frozenset({"large", "unknown"})

#: May another runtime pick this work up after a window closed?
HANDOFF_MODES = ("allowed", "same_runtime_only")

#: A provider refusal is itself the freshest capacity observation available,
#: and these are the codes that mean "your window, not our fault".  The lower
#: case names are ``gateway.DIRECT_UNAVAILABLE_REASONS`` -- the execution
#: layer's existing words for a harness that could not be used -- and the upper
#: case ones are the refusal codes the Controller's own gateway seam maps a
#: metered receipt onto.  Reproduced rather than imported: ``gateway.py`` is an
#: external seam and this module is not allowed to depend on it.
QUOTA_REFUSAL_CODES: dict[str, str] = {
    "quota_exhausted": "exhausted",
    "rate_limited": "cooling",
    "insufficient_credits": "exhausted",
    "conserved": "constrained",
    "QUOTA_EXHAUSTED": "exhausted",
    "RATE_LIMITED": "cooling",
    "GATEWAY_INSUFFICIENT_CREDITS": "exhausted",
    "GATEWAY_RATE_LIMITED": "cooling",
}

DEFAULT_OBSERVATION_MAX_AGE_SECONDS = 3_600.0
#: How long a runtime whose reset nobody could observe is held out of
#: scheduling.  It is a *bounded* hold rather than a permanent one, for the
#: same reason ageing is unbounded in the scheduler: an unmeasurable fact must
#: expire into "look again", never into "never again".
DEFAULT_UNKNOWN_RESET_BACKOFF_SECONDS = 900.0


class PolicyError(ValueError):
    """A capacity declaration the Controller will not store."""


# --------------------------------------------------------------------------- #
# what the Owner declared
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RuntimePolicy:
    """One runtime's capacity envelope, as the Owner registered it.

    ``runtime_id`` is the same opaque execution-profile name every other
    candidate uses; this module never parses it, and nothing here knows what
    vendor it belongs to.

    ``managed`` is what makes capacity opt-in.  A runtime nobody registered is
    not narrowed at all, on exactly the principle
    ``supervisor.within_window`` states for an undeclared execution window: an
    undeclared constraint must never behave like a closed gate, or switching
    the feature on for the first time would silently stop the Factory.
    """

    runtime_id: str
    managed: bool = True
    max_observation_age_seconds: float = DEFAULT_OBSERVATION_MAX_AGE_SECONDS
    handoff: str = "allowed"
    unknown_reset_backoff_seconds: float = DEFAULT_UNKNOWN_RESET_BACKOFF_SECONDS
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_id, str) or not self.runtime_id:
            raise PolicyError("a capacity policy names a runtime")
        if self.handoff not in HANDOFF_MODES:
            raise PolicyError("handoff must be one of %s" % (HANDOFF_MODES,))
        if self.max_observation_age_seconds <= 0:
            raise PolicyError(
                "an observation that never goes stale would let one reading "
                "from last week stand in for capacity today")
        if self.unknown_reset_backoff_seconds < 0:
            raise PolicyError("a backoff is never negative")

    def as_row(self) -> dict[str, Any]:
        return {"runtime_id": self.runtime_id, "managed": self.managed,
                "max_observation_age_seconds": self.max_observation_age_seconds,
                "handoff": self.handoff,
                "unknown_reset_backoff_seconds": self.unknown_reset_backoff_seconds,
                "policy_version": self.policy_version,
                "contract_version": CONTRACT_VERSION}


@dataclass(frozen=True)
class CapacityObservation:
    """One measurement of one runtime's window, and who made it.

    ``source`` and ``source_ref`` are mandatory for the same reason a project's
    acceptance gates carry an ``acceptance_gate_source`` and a budget ceiling
    carries a currency: a figure nobody can trace back to a measurement is
    indistinguishable from an invented one, and this figure decides whether
    work runs.

    A measured remainder requires both a unit and ``precision == "exact"``.
    Anything else is not a number the scheduler may compare, so it is refused
    at construction rather than silently rounded into a decision.
    """

    runtime_id: str
    state: str
    observed_at: float
    source: str
    source_ref: str
    window_started_at: float | None = None
    expected_reset_at: float | None = None
    remaining_units: float | None = None
    unit: str | None = None
    precision: str = "unknown"
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_id, str) or not self.runtime_id:
            raise PolicyError("an observation names a runtime")
        if self.state not in CAPACITY_STATES:
            raise PolicyError("capacity state must be one of %s" % (CAPACITY_STATES,))
        if self.precision not in PRECISIONS:
            raise PolicyError("precision must be one of %s" % (PRECISIONS,))
        for name in ("source", "source_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PolicyError(
                    "an observation without a %s is the invented capacity "
                    "figure this field exists to replace" % name)
        if self.remaining_units is not None:
            if self.remaining_units < 0:
                raise PolicyError("a remaining figure is never negative")
            if not self.unit:
                raise PolicyError("a measured remainder requires the unit it is measured in")
            if self.precision != "exact":
                raise PolicyError(
                    "a remainder the source could not measure exactly is not a "
                    "number the scheduler may compare against work")
        if self.unit is not None and self.remaining_units is None:
            raise PolicyError("a unit without a figure measures nothing")

    @property
    def reset_state(self) -> str:
        if self.state in USABLE:
            return "not_applicable"
        return "reset_known" if self.expected_reset_at is not None else "reset_unknown"

    def as_row(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id, "state": self.state,
            "observed_at": self.observed_at, "source": self.source,
            "source_ref": self.source_ref,
            "window_started_at": _absent(self.window_started_at),
            "expected_reset_at": _absent(self.expected_reset_at),
            "remaining_units": _absent(self.remaining_units, "not_measurable"),
            "unit": _absent(self.unit, "not_measurable"),
            "precision": self.precision, "reset_state": self.reset_state,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class WorkEstimate:
    """How much of a window a mission expects to need.

    Both halves are optional and they answer different questions.  The size
    class is a coarse ordering the Owner can state without measuring anything;
    ``expected_units`` is a figure comparable against an observation *only*
    when both name the same unit.  Nothing here converts between units, and
    nothing here turns a size class into a number: a vendor's private quota
    accounting is not a fact the Controller is entitled to model.
    """

    size_class: str = "unknown"
    expected_units: float | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.size_class not in SIZE_CLASSES:
            raise PolicyError("size class must be one of %s" % (SIZE_CLASSES,))
        if self.expected_units is not None:
            if self.expected_units < 0:
                raise PolicyError("an estimate is never negative")
            if not self.unit:
                raise PolicyError("an estimate requires the unit it is stated in")
        if self.unit is not None and self.expected_units is None:
            raise PolicyError("a unit without a figure estimates nothing")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "WorkEstimate":
        raw = (payload or {}).get("capacity_estimate") or {}
        if not isinstance(raw, dict):
            raise PolicyError("capacity_estimate must be an object")
        units = raw.get("expected_units")
        if units is not None and (isinstance(units, bool) or not isinstance(units, (int, float))):
            raise PolicyError("expected_units must be a number")
        unit = raw.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise PolicyError("unit must be a string")
        return cls(size_class=str(raw.get("size_class") or "unknown"),
                   expected_units=None if units is None else float(units),
                   unit=unit or None)

    def as_row(self) -> dict[str, Any]:
        return {"size_class": self.size_class,
                "expected_units": _absent(self.expected_units, "not_measurable"),
                "unit": _absent(self.unit, "not_measurable")}


# --------------------------------------------------------------------------- #
# reading one runtime
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RuntimeReading:
    """What the durable record says about one runtime right now."""

    runtime_id: str
    state: str
    usable: bool
    reason: str
    reset_state: str = "not_applicable"
    resume_at: float | None = None
    observed_at: float | None = None
    source: str = "not_applicable"
    source_ref: str = "not_applicable"
    remaining_units: float | None = None
    unit: str | None = None
    handoff: str = "allowed"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"runtime_id": self.runtime_id, "state": self.state,
                "usable": self.usable, "reason": self.reason,
                "reset_state": self.reset_state,
                "resume_at": _absent(self.resume_at),
                "observed_at": _absent(self.observed_at),
                "source": self.source, "source_ref": self.source_ref,
                "remaining_units": _absent(self.remaining_units, "not_measurable"),
                "unit": _absent(self.unit, "not_measurable"),
                "handoff": self.handoff, "detail": dict(self.detail)}


def read(policy: RuntimePolicy | None, observation: CapacityObservation | None,
         now: float) -> RuntimeReading:
    """One runtime's capacity, from the Owner's policy and the latest reading.

    The order of the refusals is deliberate and follows the rule the
    improvement plane arrived at: decide by whichever condition is true
    *independently of the caller*.  An explicit Owner exemption is prior to
    everything; then a measurement is honoured whenever one exists, whoever
    registered the runtime; and only a runtime the Owner put under management
    is refused for having no measurement at all.

    Registering a runtime is therefore the act that makes "nobody measured it"
    a refusal.  Both halves matter: a *measured* cooling window is honoured
    even for a runtime nobody registered, because ignoring a measurement would
    be the positive assumption Phase-1 principle 7 forbids; and a runtime with
    neither a registration nor a measurement is not narrowed at all, because an
    undeclared constraint must never behave like a closed gate.
    """

    registered = policy is not None
    if registered and not policy.managed:
        return RuntimeReading(
            runtime_id=policy.runtime_id,
            state="capacity_unmeasurable" if observation is None else observation.state,
            usable=True, reason="CAPACITY_NOT_MANAGED",
            observed_at=None if observation is None else observation.observed_at,
            handoff=policy.handoff)

    if not registered and observation is None:
        return RuntimeReading(runtime_id="unknown", state="capacity_unmeasurable",
                              usable=True, reason="CAPACITY_NOT_MANAGED")

    if policy is None:
        policy = RuntimePolicy(runtime_id=observation.runtime_id,
                               policy_version="not_applicable")

    if observation is None:
        return RuntimeReading(
            runtime_id=policy.runtime_id, state="capacity_unmeasurable", usable=False,
            reason="CAPACITY_OBSERVATION_MISSING", reset_state="reset_unknown",
            resume_at=None, handoff=policy.handoff)

    age = now - observation.observed_at
    if age > policy.max_observation_age_seconds:
        # An expired reading means "we no longer know", and who that refuses
        # depends entirely on who asked for the runtime to be managed.
        #
        # For a *registered* runtime it refuses, because registering is the act
        # that makes "nobody measured this" a refusal, and the Owner undertook
        # to keep it observed.  For an unregistered one it must not, and the
        # simulation found out why: a single provider quota refusal writes an
        # observation, and if that lone reading kept the runtime out forever
        # after it expired, one refusal would silently stop a Factory that
        # never adopted capacity at all.  Nothing would ever re-observe it.
        if not registered:
            return RuntimeReading(
                runtime_id=policy.runtime_id, state="capacity_unmeasurable",
                usable=True, reason="CAPACITY_NOT_MANAGED",
                observed_at=observation.observed_at, source=observation.source,
                source_ref=observation.source_ref, handoff=policy.handoff,
                detail={"age_seconds": round(age, 6), "expired": True})
        return RuntimeReading(
            runtime_id=policy.runtime_id, state="capacity_unmeasurable", usable=False,
            reason="CAPACITY_OBSERVATION_STALE", reset_state="reset_unknown",
            resume_at=now + policy.unknown_reset_backoff_seconds,
            observed_at=observation.observed_at, source=observation.source,
            source_ref=observation.source_ref, handoff=policy.handoff,
            detail={"age_seconds": round(age, 6),
                    "max_observation_age_seconds": policy.max_observation_age_seconds})

    resume_at = observation.expected_reset_at
    if observation.state not in USABLE and resume_at is None:
        resume_at = observation.observed_at + policy.unknown_reset_backoff_seconds

    reason = _STATE_REASONS[observation.state]
    return RuntimeReading(
        runtime_id=policy.runtime_id, state=observation.state,
        usable=observation.state in USABLE, reason=reason,
        reset_state=observation.reset_state,
        resume_at=None if observation.state in USABLE else resume_at,
        observed_at=observation.observed_at, source=observation.source,
        source_ref=observation.source_ref,
        remaining_units=observation.remaining_units, unit=observation.unit,
        handoff=policy.handoff,
        detail={"window_started_at": _absent(observation.window_started_at),
                "precision": observation.precision})


_STATE_REASONS = {
    "available": "RUNTIME_AVAILABLE",
    "constrained": "RUNTIME_CONSTRAINED",
    "cooling": "RUNTIME_COOLING",
    "exhausted": "RUNTIME_QUOTA_EXHAUSTED",
    "readiness_unavailable": "RUNTIME_READINESS_UNAVAILABLE",
    "capacity_unmeasurable": "CAPACITY_UNMEASURABLE",
}


def readings(policies: Mapping[str, RuntimePolicy],
             observations: Mapping[str, CapacityObservation],
             now: float) -> dict[str, RuntimeReading]:
    """Every runtime either side knows about, read once."""

    return {runtime_id: read(policies.get(runtime_id), observations.get(runtime_id), now)
            for runtime_id in sorted(set(policies) | set(observations))}


# --------------------------------------------------------------------------- #
# fitting work to a window
# --------------------------------------------------------------------------- #

def fit(reading: RuntimeReading, estimate: WorkEstimate) -> tuple[bool, str, dict[str, Any]]:
    """Whether this work may be *started* on this runtime's remaining window.

    Two comparisons and no third.  A measured remainder against an estimate in
    the same unit is arithmetic.  Everything else is a judgement about a
    window's shape that only the size class can carry, and it is applied in
    exactly one place: work that is large or unestimated is not begun on a
    runtime already measurably near the end of its window.

    Unknown fails safe here without starving anything, because the refusal is
    per-runtime and per-window: the same work is admitted the moment any
    compatible runtime reports ``available``, which a rolling window guarantees
    it eventually will.
    """

    if not reading.usable:
        return (False, reading.reason, {})
    comparable = (reading.remaining_units is not None and estimate.expected_units is not None
                  and reading.unit is not None and reading.unit == estimate.unit)
    if comparable:
        if estimate.expected_units > reading.remaining_units:
            return (False, "CAPACITY_INSUFFICIENT_FOR_WORK",
                    {"expected_units": estimate.expected_units,
                     "remaining_units": reading.remaining_units, "unit": reading.unit})
        return (True, "WORK_FITS_REMAINING_WINDOW",
                {"expected_units": estimate.expected_units,
                 "remaining_units": reading.remaining_units, "unit": reading.unit})
    if reading.state == "constrained" and estimate.size_class in CONSTRAINED_REFUSED_SIZES:
        return (False, "WORK_SIZE_EXCEEDS_CONSTRAINED_WINDOW",
                {"size_class": estimate.size_class,
                 "remaining_units": _absent(reading.remaining_units, "not_measurable")})
    if reading.remaining_units is not None and estimate.expected_units is not None:
        # Both measured, different units.  Not comparable, and converting would
        # be modelling a vendor's accounting.  Treated as unmeasured.
        return (True, "WORK_FIT_UNIT_MISMATCH",
                {"reading_unit": reading.unit, "estimate_unit": estimate.unit})
    return (True, "WORK_FIT_UNMEASURED", {"size_class": estimate.size_class})


# --------------------------------------------------------------------------- #
# narrowing a mission's candidates
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Consideration:
    runtime_id: str
    admitted: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"runtime_id": self.runtime_id, "admitted": self.admitted,
                "reason": self.reason, "detail": dict(self.detail)}


@dataclass(frozen=True)
class CapacityPlan:
    """What capacity says about one mission's declared runtimes.

    ``denied`` is the only thing a caller acts on, and it is always a subset of
    the runtimes it was handed.  There is no field here that names a runtime
    the mission did not already declare, so applying a plan can shrink the
    eligible set and has no way to grow it.
    """

    considered: tuple[Consideration, ...]
    admitted: tuple[str, ...]
    denied: tuple[str, ...]
    reason: str
    resume_at: float | None = None

    @property
    def exhausted(self) -> bool:
        """True when capacity refused every runtime this mission declared."""

        return bool(self.considered) and not self.admitted

    def as_row(self) -> dict[str, Any]:
        return {"considered": [item.as_row() for item in self.considered],
                "admitted": list(self.admitted), "denied": list(self.denied),
                "reason": self.reason, "resume_at": _absent(self.resume_at),
                "contract_version": CONTRACT_VERSION}


def plan(runtime_ids: Sequence[str], runtime_readings: Mapping[str, RuntimeReading],
         estimate: WorkEstimate | None = None) -> CapacityPlan:
    """Narrow a mission's declared runtimes to the ones a window allows now.

    A mission that declared no runtimes has no subject here: the execution
    layer's own default serves it and capacity says ``not_applicable`` rather
    than refusing work it cannot describe.
    """

    estimate = estimate or WorkEstimate()
    if not runtime_ids:
        return CapacityPlan((), (), (), "CAPACITY_NOT_APPLICABLE")
    considered: list[Consideration] = []
    admitted: list[str] = []
    denied: list[str] = []
    resume_candidates: list[float] = []
    for runtime_id in runtime_ids:
        reading = runtime_readings.get(runtime_id)
        if reading is None:
            # Unregistered and unobserved: not a managed runtime, so not
            # narrowed.  Same principle as an undeclared execution window.
            considered.append(Consideration(runtime_id, True, "CAPACITY_NOT_MANAGED"))
            admitted.append(runtime_id)
            continue
        ok, reason, detail = fit(reading, estimate)
        considered.append(Consideration(runtime_id, ok, reason, detail))
        if ok:
            admitted.append(runtime_id)
        else:
            denied.append(runtime_id)
            if reading.resume_at is not None:
                resume_candidates.append(reading.resume_at)
    if admitted:
        return CapacityPlan(tuple(considered), tuple(admitted), tuple(denied),
                            "CAPACITY_ADMITTED")
    return CapacityPlan(tuple(considered), (), tuple(denied),
                        "CAPACITY_UNAVAILABLE",
                        min(resume_candidates) if resume_candidates else None)


#: The execution layer's own capacity record, from ``factory-bridge``
#: ``src/factory_bridge/continuity.py``.  Reproduced rather than imported:
#: neither repository depends on the other, which is the point of the boundary.
BRIDGE_OBSERVATION_SCHEMA = "factory.bridge.capacity_observation.v1"

#: Their four readiness words to this module's six states.  Two of theirs
#: collapse into one of mine, and the distinction is *kept* rather than lost:
#: the original word is carried in the observation's detail, because "the
#: account is not signed in" and "the harness could not be reached" are the
#: same scheduling fact and different things for a person to fix.
#:
#: Nothing maps to ``constrained`` or ``exhausted``.  Their vocabulary cannot
#: express either, so translating into one would be inventing a measurement --
#: the same rule SF-136 applied to the Context Broker's ``unavailable``.
BRIDGE_READINESS = {
    "available": "available",
    "auth_required": "readiness_unavailable",
    "unavailable": "readiness_unavailable",
    "unmeasurable": "capacity_unmeasurable",
}


def observation_from_bridge_status(status: Mapping[str, Any], now: float,
                                   runtime_id: str | None = None
                                   ) -> CapacityObservation | None:
    """Read one ``factory-bridge capacity status`` reading as an observation.

    This is the seam between the layer that can see a harness and the layer
    that decides what runs.  ``None`` means the bridge holds no record at all,
    which is a genuine absence and must not become a state: fabricating
    ``capacity_unmeasurable`` for it would make an unregistered runtime look
    measured-and-unreadable rather than simply unmanaged.

    A record the bridge could not parse is different, and does become an
    observation -- something is there and it is wrong, which is a fact the
    scheduler should act on rather than ignore.
    """

    if not isinstance(status, dict):
        raise PolicyError("a bridge capacity status is an object")
    state = status.get("state")
    if state == "absent":
        return None
    profile = runtime_id or status.get("profile_id")
    if not isinstance(profile, str) or not profile:
        raise PolicyError("a bridge capacity status names the profile it measured")
    if state == "invalid":
        return CapacityObservation(
            runtime_id=profile, state="capacity_unmeasurable", observed_at=now,
            source="factory_bridge_capacity_status",
            source_ref="bridge_record_invalid",
            detail={"detail": str(status.get("detail", "unknown"))[:256]})
    schema = status.get("schema_version")
    if schema != BRIDGE_OBSERVATION_SCHEMA:
        raise PolicyError("unsupported bridge capacity schema: %r" % (schema,))
    reported = status.get("classification")
    if reported not in BRIDGE_READINESS:
        raise PolicyError("unknown bridge readiness classification: %r" % (reported,))
    observed_at = status.get("observed_at")
    if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool):
        raise PolicyError("a bridge capacity status carries when it was observed")
    remaining = status.get("remaining_seconds")
    measured = isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
    return CapacityObservation(
        runtime_id=profile, state=BRIDGE_READINESS[reported],
        observed_at=float(observed_at),
        source="factory_bridge_capacity_status",
        source_ref="%s:%s" % (BRIDGE_OBSERVATION_SCHEMA,
                              status.get("observation_id") or reported),
        # Their window has a remaining *duration* rather than a reset instant,
        # so the reset is derived from the two numbers they did measure and is
        # never guessed when they measured neither.
        expected_reset_at=(float(observed_at) + float(remaining)
                           if measured and reported != "available" else None),
        remaining_units=float(remaining) if measured else None,
        unit="seconds" if measured else None,
        precision="exact" if measured else "unknown",
        detail={"reported_classification": reported,
                "reported_state": _absent(state, "unknown"),
                "stale_after_seconds": _absent(status.get("stale_after_seconds"),
                                               "not_measurable")})


def observation_from_refusal(runtime_id: str, refusal_code: str | None, now: float,
                             *, expected_reset_at: float | None = None
                             ) -> CapacityObservation | None:
    """A provider's own quota refusal, read as the observation it is.

    This is why no probe is needed.  The freshest possible statement about a
    window is the harness declining to serve one, and it arrives on the path
    that was already going to record a refused leg.  Codes that mean anything
    else return ``None`` -- a timeout is not a quota fact, and guessing that it
    is would take a runtime out of service for a reason nobody measured.
    """

    if not refusal_code:
        return None
    state = QUOTA_REFUSAL_CODES.get(refusal_code)
    if state is None:
        return None
    return CapacityObservation(
        runtime_id=runtime_id, state=state, observed_at=now,
        source="provider_refusal", source_ref=refusal_code,
        expected_reset_at=expected_reset_at)


# --------------------------------------------------------------------------- #
# the capacity checkpoint
# --------------------------------------------------------------------------- #
#
# One boundary, two owners, and the division is worth stating because the two
# nearly collided.  ``continuity.py`` owns the *portable Work Baton*: a signed,
# exactly-once token another runtime consumes, carrying the repository facts
# (head, worktree, branch, lane) the Controller does not hold.  This section
# owns the *checkpoint*: what the mission ledger itself says at the moment work
# stopped.  A checkpoint is re-derived on every read and never consumed; a
# baton is issued once and consumed once.  The vocabulary below is
# ``continuity``'s -- ``safe_boundary``, ``uncertainty.irreversible_effect``,
# ``compatible_profiles`` -- so a checkpoint at a safe boundary lifts into a
# baton by adding the repository facts, with no translation and no second
# dialect.

#: The mission's durable steps, in the order the Controller runs them.  The
#: next safe step is the first of these that has not completed, which is a
#: reading of the ledger rather than a plan somebody wrote down beside it.
MISSION_STEPS = ("context", "dispatch", "verify", "evaluate", "evidence")

#: Everything a checkpoint must carry for a later runtime to pick the work up
#: without the first runtime's conversation.  Pinned as a tuple so a field
#: cannot be dropped quietly, exactly as ``gateway.GATEWAY_FACTS`` pins the
#: gateway's.
CHECKPOINT_FACTS = (
    "mission_id", "project_id", "work_item_id", "repository", "baseline_sha",
    "candidate_sha", "mission_state", "completed_steps", "next_safe_step",
    "evaluator", "context_manifest_hash", "evidence_pointer",
    "compatible_profiles", "capacity_observation", "idempotency_key",
    "operation_keys", "unresolved_blockers", "resume_target", "safe_boundary",
    "uncertainty",
)

#: ``continuity.SAFE_BOUNDARIES`` reproduced, plus the honest third reading it
#: deliberately has no name for.  A checkpoint may be taken at any time; only
#: the first of these may become a baton, which is why the refusal lives in
#: ``continuity.validate`` rather than being restated here.
CHECKPOINT_BOUNDARIES = ("pre_dispatch", "post_dispatch_reconciled",
                         "post_dispatch_unreconciled")

#: What a resumed mission is being asked to do next.
#: ``reconcile_uncertain_dispatch`` is the one that is *not* a handoff: it is
#: the existing uncertainty path, and work there may only ever continue on the
#: runtime that already ran.
RESUME_TARGETS = ("resume_next_step", "reconcile_uncertain_dispatch")


def checkpoint_facts(mission: Mapping[str, Any], payload: Mapping[str, Any],
                     steps: Mapping[str, str], legs: Sequence[Mapping[str, Any]],
                     reading: RuntimeReading | None = None,
                     repository: str | None = None, candidate_sha: str | None = None,
                     evidence_pointer: str | None = None) -> dict[str, Any]:
    """Project the durable record of one mission into a portable checkpoint.

    Nothing is copied out of a model's context and nothing is remembered: every
    field below is read from the ledger, so a checkpoint written under one
    runtime and read under another describes the repository, not a
    conversation.

    ``safe_boundary`` is the whole of scope 6.  A leg the execution layer did
    not prove harmless means a provider process may already be running, and no
    amount of recorded state makes that a clean handoff -- so the checkpoint
    says ``post_dispatch_unreconciled``, which ``continuity.validate`` refuses
    to turn into a baton, and points at the existing reconciliation path
    instead of pretending.
    """

    committed = [leg for leg in legs if leg.get("process_started") is not False]
    safe = not committed
    # A mission that declared no context request never runs a context step, so
    # asking for its status would leave every checkpoint pointing at a step
    # that will never happen.  The applicable steps are derived from the
    # mission's own declaration, exactly as the engine derives them.
    applicable = tuple(name for name in MISSION_STEPS
                       if name != "context" or payload.get("context_request"))
    completed = tuple(name for name in applicable if steps.get(name) == "COMPLETED")
    remaining = [name for name in applicable if steps.get(name) != "COMPLETED"]
    served = committed[-1] if committed else (legs[-1] if legs else None)
    blockers: list[str] = []
    if not safe:
        blockers.append("UNCERTAIN_DISPATCH_LEG")
    if reading is not None and not reading.usable:
        blockers.append(reading.reason)
    profiles = [entry if isinstance(entry, str) else entry.get("profile")
                for entry in (payload.get("provider_candidates") or ())]
    if not safe:
        # A runtime that may already be running is the only runtime that may
        # continue.  Narrowing the list here rather than adding a refusal keeps
        # the property structural: there is no second runtime to choose.
        served_profile = None if served is None else served.get("provider_profile")
        profiles = [served_profile] if served_profile else []
    facts = {
        "mission_id": mission.get("id"),
        "project_id": _absent(mission.get("project_id"), "not_applicable"),
        "work_item_id": _absent(payload.get("work_item_id")),
        "repository": _absent(repository, "not_applicable"),
        "baseline_sha": _absent(payload.get("baseline_sha")),
        "candidate_sha": _absent(candidate_sha, "not_run"),
        "mission_state": mission.get("state"),
        "completed_steps": list(completed),
        "next_safe_step": remaining[0] if remaining else "not_applicable",
        "evaluator": _absent(steps.get("evaluate"), "not_run"),
        "context_manifest_hash": _absent(payload.get("context_manifest_hash"),
                                         "not_applicable"),
        "evidence_pointer": _absent(evidence_pointer, "not_run"),
        "compatible_profiles": [name for name in profiles if isinstance(name, str) and name],
        "capacity_observation": ({"runtime_id": "not_applicable",
                                  "state": "capacity_unmeasurable",
                                  "reason": "CAPACITY_NOT_MANAGED"}
                                 if reading is None else reading.as_row()),
        "idempotency_key": mission.get("idempotency_key"),
        "operation_keys": ["%s:%s" % (mission.get("idempotency_key"), name)
                           for name in completed],
        "unresolved_blockers": blockers,
        "resume_target": "resume_next_step" if safe else "reconcile_uncertain_dispatch",
        "safe_boundary": "pre_dispatch" if safe else "post_dispatch_unreconciled",
        "uncertainty": {"irreversible_effect": "none" if safe else "unknown",
                        "unproven_legs": len(committed)},
    }
    missing = set(CHECKPOINT_FACTS) - set(facts)
    if missing:  # pragma: no cover - CHECKPOINT_FACTS and the literal are one unit
        raise PolicyError("checkpoint is missing %s" % sorted(missing))
    return facts


def _absent(value: Any, word: str = "unknown") -> Any:
    """An absent figure as one of Evidence Core's four words, never as ``0``."""

    return word if value is None else value
