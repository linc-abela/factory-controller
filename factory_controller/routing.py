"""Provider-neutral execution policy, routing decisions, receipts, and budget.

Nothing in this module names a vendor, builds a command line, holds a
credential, or asks this host what is installed.  A *profile* is an opaque
string minted by the execution layer; the Controller only ever orders, admits,
and records them.  That separation is what `tests/test_authority_boundaries.py`
checks mechanically, and it is the reason provider choice can change without
the mission lifecycle changing.

Three vocabularies here are reproduced from `factory-evidence-core`, not
invented.  Each is pinned by a test so a fork is a failure rather than a drift:

* ``CANONICAL_ABSENCE`` -- ``src/contracts/replay.py`` line 9.  Unknown cost is
  recorded with one of these words.  It is never a zero and never an estimate.
* ``BRIDGE_RESULT_STATUSES`` -- ``src/orchestration/verification.py``, the value
  space its ``UNSUPPORTED_ENVELOPE_STATUS`` refusal enforces.
* ``expected_idempotency_key`` -- the same function name and rule from
  ``src/orchestration/verification.py``.  A real mission whose key does not obey
  it can never reach ``factory-bridge`` under the Controller's own identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


#: Reproduced from factory-evidence-core ``src/contracts/replay.py``.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run", "not_measurable"})

#: Reproduced from factory-evidence-core ``src/orchestration/verification.py``.
BRIDGE_RESULT_STATUSES = ("completed", "blocked", "refused", "no_candidate", "partial_result")

#: The execution layer's answer when it served no provider at all.  Not a bridge
#: envelope status: no envelope exists, because nothing ran.
PROVIDER_UNAVAILABLE = "provider_unavailable"

#: A mission is a fixture mission unless it declares itself real.  The guard is
#: an equality check in both directions, so neither default can launder a run:
#: a fixture receipt fails a real mission and a real receipt fails a fixture one.
EXECUTION_MODES = ("fixture", "real")

DEFAULT_MAX_ROUTE_LEGS = 3


class PolicyError(ValueError):
    """The Owner's declared execution policy is unusable as written."""


# --------------------------------------------------------------------------- #
# owner constraints
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExecutionPolicy:
    """Owner constraints on routing.  Separate from provider selection itself.

    Empty ``allowed_profiles`` means "no allowlist", which is not the same as
    "nothing is allowed": an allowlist that exists is honoured exactly.
    """

    required_capability: str | None = None
    allowed_profiles: tuple[str, ...] = ()
    denied_profiles: tuple[str, ...] = ()
    no_fallback: bool = False
    max_route_legs: int = DEFAULT_MAX_ROUTE_LEGS
    budget_ceiling: float | None = None
    budget_currency: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "ExecutionPolicy":
        raw = (payload or {}).get("execution_policy") or {}
        if not isinstance(raw, dict):
            raise PolicyError("execution_policy must be an object")
        policy = cls(
            required_capability=_optional_str(raw, "required_capability"),
            allowed_profiles=_string_tuple(raw, "allowed_profiles"),
            denied_profiles=_string_tuple(raw, "denied_profiles"),
            no_fallback=_bool(raw, "no_fallback", False),
            max_route_legs=_int(raw, "max_route_legs", DEFAULT_MAX_ROUTE_LEGS),
            budget_ceiling=_optional_number(raw, "budget_ceiling"),
            budget_currency=_optional_str(raw, "budget_currency"),
        )
        if policy.max_route_legs < 1:
            raise PolicyError("max_route_legs must be at least 1")
        if policy.budget_ceiling is not None:
            if policy.budget_ceiling <= 0:
                raise PolicyError("budget_ceiling must be positive")
            if not policy.budget_currency:
                raise PolicyError("budget_currency is required with budget_ceiling")
        return policy


@dataclass(frozen=True)
class Candidate:
    """One provider profile the Owner is willing to have the layer serve."""

    profile: str
    capabilities: tuple[str, ...] = ()


def candidates_from_payload(payload: dict[str, Any] | None) -> tuple[Candidate, ...]:
    raw = (payload or {}).get("provider_candidates") or []
    if not isinstance(raw, (list, tuple)):
        raise PolicyError("provider_candidates must be a list")
    out: list[Candidate] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"profile": entry}
        if not isinstance(entry, dict):
            raise PolicyError("each provider candidate must be a string or object")
        profile = entry.get("profile")
        if not isinstance(profile, str) or not profile:
            raise PolicyError("provider candidate profile must be a non-empty string")
        if profile in seen:
            raise PolicyError("duplicate provider candidate: %s" % profile)
        seen.add(profile)
        out.append(Candidate(profile=profile, capabilities=_string_tuple(entry, "capabilities")))
    return tuple(out)


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Consideration:
    """Why one candidate was or was not eligible for this leg."""

    profile: str
    admissible: bool
    reason: str


@dataclass(frozen=True)
class Selection:
    profile: str | None
    reason: str
    considered: tuple[Consideration, ...]
    refusal_code: str | None = None

    @property
    def selected(self) -> bool:
        return self.profile is not None


def select(policy: ExecutionPolicy, candidates: Sequence[Candidate],
           attempted: Sequence[str] = ()) -> Selection:
    """Pick the next profile deterministically, in declared order.

    Determinism is the point: the same policy, candidate list and attempt
    history always yields the same choice and the same explanation, so route
    history can be replayed rather than merely believed.
    """

    attempted = tuple(attempted)
    considered = tuple(_consider(policy, candidate, attempted) for candidate in candidates)
    if attempted and policy.no_fallback:
        return Selection(None, "no_fallback_policy", considered, "PROVIDER_FALLBACK_FORBIDDEN")
    if len(attempted) >= policy.max_route_legs:
        return Selection(None, "route_leg_limit", considered, "PROVIDER_ROUTE_EXHAUSTED")
    for consideration in considered:
        if consideration.admissible:
            reason = ("fallback_after:" + attempted[-1]) if attempted else "first_admissible"
            return Selection(consideration.profile, reason, considered)
    return Selection(None, "no_admissible_candidate", considered, "NO_ADMISSIBLE_PROVIDER")


def _consider(policy: ExecutionPolicy, candidate: Candidate,
              attempted: Sequence[str]) -> Consideration:
    if candidate.profile in attempted:
        return Consideration(candidate.profile, False, "already_attempted")
    if candidate.profile in policy.denied_profiles:
        return Consideration(candidate.profile, False, "denied_by_policy")
    if policy.allowed_profiles and candidate.profile not in policy.allowed_profiles:
        return Consideration(candidate.profile, False, "not_in_allowlist")
    if (policy.required_capability and candidate.capabilities
            and policy.required_capability not in candidate.capabilities):
        return Consideration(candidate.profile, False, "capability_not_offered")
    return Consideration(candidate.profile, True, "admissible")


# --------------------------------------------------------------------------- #
# reported execution facts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Usage:
    """What the provider *said* it used.  Never re-derived, never estimated."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_amount: float | None = None
    cost_currency: str | None = None
    cost_state: str = "unknown"

    def __post_init__(self) -> None:
        if self.cost_state == "reported":
            if self.cost_amount is None or not self.cost_currency:
                raise PolicyError("reported cost requires an amount and a currency")
        elif self.cost_state not in CANONICAL_ABSENCE:
            raise PolicyError("cost_state must be 'reported' or a canonical absence word")
        elif self.cost_amount is not None:
            raise PolicyError("an amount was supplied while cost_state is %s" % self.cost_state)


UNKNOWN_USAGE = Usage()


def usage_from_response(raw: Any) -> Usage:
    """Read a provider's usage claim.  Anything missing stays explicitly absent.

    A provider that reports nothing produces ``unknown``, never ``0``.  That
    distinction is the whole point of the absence vocabulary, and it is exactly
    the one the corpus has lost three times.
    """

    if not isinstance(raw, dict):
        return UNKNOWN_USAGE
    amount = raw.get("cost_amount")
    currency = raw.get("cost_currency")
    priced = isinstance(amount, (int, float)) and not isinstance(amount, bool) \
        and isinstance(currency, str) and bool(currency)
    declared = raw.get("cost_state")
    if priced:
        state = "reported"
    elif isinstance(declared, str) and declared in CANONICAL_ABSENCE:
        state = declared
    else:
        state = "unknown"
    return Usage(
        input_tokens=_non_negative_int(raw.get("input_tokens")),
        output_tokens=_non_negative_int(raw.get("output_tokens")),
        cost_amount=float(amount) if priced else None,
        cost_currency=currency if priced else None,
        cost_state=state,
    )


@dataclass(frozen=True)
class Receipt:
    """The provider execution receipt shared with the bridge.

    Everything here is a *reported* fact.  Candidate validity is not decided by
    any of it: that is Git's and Evidence Core's, and the Controller keeps the
    two classes in separate fields precisely so they cannot be confused later.
    """

    profile: str | None
    provider_identity: str | None
    selection_reason: str
    fallback_chain: tuple[str, ...]
    process_started: bool | None
    duration_ms: int | None
    classification: str
    refusal_code: str | None
    execution_mode: str
    idempotency_key: str | None
    usage: Usage = field(default_factory=lambda: UNKNOWN_USAGE)
    #: Production's own word for a provider's claim, from
    #: ``src/contracts/mvp.py`` line 98 (``provider_claim_evidence_class``).
    #: Deliberately not the assertion-level ``reported``: this whole record is a
    #: claim, and the re-derived facts about the same run live elsewhere.
    evidence_class: str = "reported_claim"

    @property
    def side_effect_possible(self) -> bool:
        """True unless the layer *proved* no provider process began.

        ``None`` means the layer did not say, and an unproven negative is not a
        proof.  Only an explicit ``False`` unlocks a post-dispatch reroute.
        """

        return self.process_started is not False


def receipt_from_response(response: dict[str, Any], selection: Selection,
                          fallback_chain: Sequence[str]) -> Receipt:
    """Project one adapter response into a receipt, inventing nothing."""

    raw = response.get("receipt") if isinstance(response.get("receipt"), dict) else {}
    status = response.get("status")
    started = raw.get("process_started")
    mode = raw.get("execution_mode", response.get("execution_mode"))
    return Receipt(
        profile=raw.get("profile") or selection.profile,
        provider_identity=_optional_string(raw.get("provider_identity")),
        selection_reason=selection.reason,
        fallback_chain=tuple(fallback_chain),
        process_started=started if isinstance(started, bool) else None,
        duration_ms=_non_negative_int(raw.get("duration_ms")),
        classification=status if isinstance(status, str) and status else "unknown",
        refusal_code=_optional_string(raw.get("refusal_code") or response.get("diagnostic")),
        execution_mode=mode if mode in EXECUTION_MODES else "unknown",
        idempotency_key=_optional_string(raw.get("idempotency_key")
                                         or response.get("idempotency_key")),
        usage=usage_from_response(raw.get("usage")),
    )


def unserved_receipt(selection: Selection, fallback_chain: Sequence[str],
                     refusal_code: str) -> Receipt:
    """A receipt for a leg that never reached the execution layer at all.

    ``process_started=False`` is a fact here rather than a claim: the Controller
    refused before dispatching, so nothing can have run.
    """

    return Receipt(
        profile=selection.profile,
        provider_identity=None,
        selection_reason=selection.reason,
        fallback_chain=tuple(fallback_chain),
        process_started=False,
        duration_ms=None,
        classification=PROVIDER_UNAVAILABLE,
        refusal_code=refusal_code,
        execution_mode="not_applicable" if refusal_code else "unknown",
        idempotency_key=None,
        usage=Usage(cost_state="not_applicable"),
    )


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BudgetState:
    ceiling: float | None
    currency: str | None
    known_spend: float
    unpriced_legs: int
    currency_conflicts: int
    state: str

    @property
    def exhausted(self) -> bool:
        return self.state == "exhausted"


def accumulate(policy: ExecutionPolicy, receipts: Iterable[Receipt]) -> BudgetState:
    """Add up only what was actually reported, in the policy's own currency.

    A leg priced in another currency is not converted and not guessed; it is
    counted as a conflict, which fails the next dispatch closed.
    """

    known = 0.0
    unpriced = 0
    conflicts = 0
    for receipt in receipts:
        usage = receipt.usage
        if usage.cost_state != "reported":
            unpriced += 1
        elif policy.budget_currency and usage.cost_currency != policy.budget_currency:
            conflicts += 1
        else:
            known += float(usage.cost_amount or 0.0)
    if policy.budget_ceiling is None:
        state = "not_applicable"
    elif conflicts:
        state = "unknown"
    elif known >= policy.budget_ceiling:
        state = "exhausted"
    else:
        state = "within"
    return BudgetState(
        ceiling=policy.budget_ceiling,
        currency=policy.budget_currency,
        known_spend=round(known, 10),
        unpriced_legs=unpriced,
        currency_conflicts=conflicts,
        state=state,
    )


def refuse_dispatch(budget: BudgetState) -> str | None:
    """The pre-dispatch budget gate.  Returns a refusal code or ``None``.

    Fail-closed on a *known* exhaustion and on a currency we cannot add up.
    Unknown cost alone never blocks: refusing on it would be an estimate by
    another name, and the ceiling is a hard ceiling on measured spend.
    """

    if budget.state == "exhausted":
        return "MISSION_BUDGET_EXHAUSTED"
    if budget.state == "unknown" and budget.currency_conflicts:
        return "MISSION_BUDGET_CURRENCY_MISMATCH"
    return None


# --------------------------------------------------------------------------- #
# mission identity
# --------------------------------------------------------------------------- #

def expected_idempotency_key(work_item_id: str, context_manifest_hash: str) -> str:
    """The key ``factory-evidence-core`` will accept, and nothing else.

    ``verify_and_bind_execution_envelope`` refuses ``IDEMPOTENCY_BINDING_MISMATCH``
    for any other value, so a real mission whose Controller key differs can never
    be the key that reaches ``factory-bridge``.  Checking it here turns that from
    a runtime surprise into an admission-time refusal.
    """

    return "%s:%s" % (work_item_id, context_manifest_hash)


# --------------------------------------------------------------------------- #
# small readers -- all fail closed on the wrong type rather than coercing
# --------------------------------------------------------------------------- #

def _optional_str(raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PolicyError("%s must be a non-empty string" % name)
    return value


def _string_tuple(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name) or ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item for item in value):
        raise PolicyError("%s must be a list of non-empty strings" % name)
    return tuple(value)


def _bool(raw: dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise PolicyError("%s must be a boolean" % name)
    return value


def _int(raw: dict[str, Any], name: str, default: int) -> int:
    value = raw.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PolicyError("%s must be an integer" % name)
    return value


def _optional_number(raw: dict[str, Any], name: str) -> float | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PolicyError("%s must be a number" % name)
    return float(value)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
