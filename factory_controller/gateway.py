"""The model-gateway seam: OpenRouter as an admitted execution profile.

The Owner made OpenRouter a first-class MVP execution path.  The thing that
keeps it from becoming *the* path is a boundary this module states in one line
and then enforces: **a gateway supplies inference; a harness performs admitted
repository actions.**  OpenRouter never reaches a filesystem, a shell, a Git
admission, evidence, or a deployment -- it answers a bounded execution
adapter's question, and that adapter is the one already fenced by Stages 1-4.

Two properties come out of existing machinery rather than new code, which is
why this module is small.

*No silent switch after execution begins.*  A gateway profile is an ordinary
candidate on the fallback chain the Controller has had since Stage 3, and that
chain is already closed by the side-effect boundary: a leg may be re-routed only
where the execution layer *proved* no process started.  So "pre-dispatch only"
is not a new rule to enforce, it is the old one applying to a new candidate.

*Policy stays authoritative.*  Admission is a comparison against the Owner's
allowlist, performed before any candidate is offered to the layer, so a gateway
that would route around the allowlist is refused before it can.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .routing import CANONICAL_ABSENCE, PolicyError, _non_negative_int, _optional_string


#: Reserved slug meaning "let the gateway pick".  Not a default here, ever: an
#: implicit choice cannot be checked against an allowlist, priced against a
#: budget, or reproduced from a receipt.
AUTO_SLUG = "openrouter/auto"

GATEWAYS = ("openrouter",)

#: Why a direct harness was not used.  Every one of these is a *pre-spawn* fact
#: about the harness; none of them describes a run that started.  The Controller
#: still requires the layer's ``process_started: false`` proof on top -- this
#: list only says which reasons are *eligible* to be answered by a gateway.
DIRECT_UNAVAILABLE_REASONS = (
    "quota_exhausted", "rate_limited", "authentication_failed",
    "insufficient_credits", "provider_unavailable", "conserved",
)

#: Gateway execution outcomes, and whether the outcome itself makes the run
#: uncertain.  ``True`` means the Controller refuses to re-route on this code
#: *even if the layer claims nothing started*, because the code names a
#: condition under which the layer cannot know.  A request that timed out may
#: have been served; a request refused for a bad key was not.
GATEWAY_REFUSALS: dict[str, bool] = {
    "GATEWAY_AUTHENTICATION_FAILED": False,
    "GATEWAY_INSUFFICIENT_CREDITS": False,
    "GATEWAY_RATE_LIMITED": False,
    "GATEWAY_OUTAGE": False,
    "GATEWAY_MODEL_UNAVAILABLE": False,
    "GATEWAY_TOOL_CAPABILITY_UNSUPPORTED": False,
    "GATEWAY_TIMEOUT": True,
    "GATEWAY_MALFORMED_RESPONSE": True,
    "GATEWAY_OUTCOME_UNCERTAIN": True,
}


@dataclass(frozen=True)
class GatewayProfile:
    """One admitted gateway-backed execution profile.

    ``profile`` is the same opaque execution-layer name every other candidate
    uses.  The Controller holds no OpenRouter SDK, base URL, or key: it holds
    the Owner's statement that *this* profile means *that* model, so that the
    thing it admits and the thing a receipt reports can be compared.
    """

    profile: str
    gateway: str = "openrouter"
    model_slug: str = ""
    capabilities: tuple[str, ...] = ()
    privacy: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not self.profile:
            raise PolicyError("a gateway profile needs a profile name")
        if self.gateway not in GATEWAYS:
            raise PolicyError("unknown gateway %r" % (self.gateway,))
        if not isinstance(self.model_slug, str) or not self.model_slug:
            raise PolicyError("gateway profile %s must name an explicit model slug"
                              % self.profile)


@dataclass(frozen=True)
class GatewayPolicy:
    """The Owner's word over every gateway profile in one mission.

    ``fallback_models`` is declared here rather than left to the gateway.  A
    gateway that silently substitutes a second model has changed which model
    did the work, and the receipt is the only place that would show -- so the
    substitution has to be something the Owner allowed in advance.
    """

    enabled: bool = False
    allowed_model_slugs: tuple[str, ...] = ()
    allow_auto_routing: bool = False
    required_privacy: tuple[str, ...] = ()
    fallback_models: tuple[str, ...] = ()
    required_capability: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "GatewayPolicy":
        raw = (payload or {}).get("gateway_policy")
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise PolicyError("gateway_policy must be an object")
        allowed = _strings(raw, "allowed_model_slugs")
        fallback = _strings(raw, "fallback_models")
        unlisted = [slug for slug in fallback if slug not in allowed]
        if unlisted:
            raise PolicyError("fallback models outside the allowlist: %s" % ", ".join(unlisted))
        return cls(
            enabled=bool(raw.get("enabled", False)),
            allowed_model_slugs=allowed,
            allow_auto_routing=bool(raw.get("allow_auto_routing", False)),
            required_privacy=_strings(raw, "required_privacy"),
            fallback_models=fallback,
            required_capability=_optional_string(raw.get("required_capability")),
        )


def profiles_from_payload(payload: dict[str, Any] | None) -> tuple[GatewayProfile, ...]:
    raw = (payload or {}).get("gateway_profiles")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PolicyError("gateway_profiles must be a list")
    built = []
    for item in raw:
        if not isinstance(item, dict):
            raise PolicyError("each gateway profile must be an object")
        built.append(GatewayProfile(
            profile=_optional_string(item.get("profile")) or "",
            gateway=_optional_string(item.get("gateway")) or "openrouter",
            model_slug=_optional_string(item.get("model_slug")) or "",
            capabilities=_strings(item, "capabilities"),
            privacy=_strings(item, "privacy"),
        ))
    names = [item.profile for item in built]
    if len(set(names)) != len(names):
        raise PolicyError("gateway profile names must be unique")
    return tuple(built)


def admit(profile: GatewayProfile, policy: GatewayPolicy) -> str | None:
    """The refusal code for this profile under this policy, or ``None``.

    Ordered so the most specific failure is reported: an Owner reading
    ``GATEWAY_IMPLICIT_AUTO_ROUTING_REFUSED`` learns something a bare
    ``GATEWAY_MODEL_NOT_ALLOWLISTED`` would have hidden.
    """

    if not policy.enabled:
        return "GATEWAY_DISABLED"
    if profile.model_slug == AUTO_SLUG and not policy.allow_auto_routing:
        return "GATEWAY_IMPLICIT_AUTO_ROUTING_REFUSED"
    if policy.allowed_model_slugs and profile.model_slug not in policy.allowed_model_slugs:
        return "GATEWAY_MODEL_NOT_ALLOWLISTED"
    missing = [need for need in policy.required_privacy if need not in profile.privacy]
    if missing:
        return "GATEWAY_PRIVACY_REQUIREMENT_UNMET"
    if policy.required_capability and policy.required_capability not in profile.capabilities:
        return "GATEWAY_CAPABILITY_UNSUPPORTED"
    return None


def admitted_profiles(payload: dict[str, Any] | None) -> tuple[tuple[GatewayProfile, str | None], ...]:
    """Every declared gateway profile paired with its refusal code, in order."""

    policy = GatewayPolicy.from_payload(payload)
    return tuple((profile, admit(profile, policy)) for profile in profiles_from_payload(payload))


def may_reroute(refusal_code: str | None, process_started: bool | None) -> tuple[bool, str]:
    """May a leg that ended in ``refusal_code`` be handed to another profile?

    Two independent gates, and both must pass.  The layer must have proved no
    process began, *and* the refusal code must not itself name a condition
    under which that proof is not knowable.  A layer reporting
    ``GATEWAY_TIMEOUT`` alongside ``process_started: false`` is asserting
    something a timeout does not let it observe, so the assertion loses.
    """

    if process_started is not False:
        return (False, "SIDE_EFFECT_POSSIBLE")
    if refusal_code and GATEWAY_REFUSALS.get(refusal_code, False):
        return (False, "OUTCOME_UNCERTAIN_BY_REFUSAL_CODE")
    return (True, "PRE_DISPATCH")


# --------------------------------------------------------------------------- #
# receipts
# --------------------------------------------------------------------------- #

#: What a gateway leg is expected to report.  Anything the gateway did not
#: report stays one of Evidence Core's four canonical absence words -- never
#: ``0``, never an estimate, and never a value carried over from the request.
GATEWAY_FACTS = ("gateway", "requested_model", "actual_model", "actual_provider",
                 "generation_id", "input_tokens", "output_tokens", "cost_amount",
                 "cost_currency", "cost_state", "retries", "fallback_models",
                 "privacy_enforced")


def facts_from_response(raw: Any, profile: GatewayProfile | None = None) -> dict[str, Any] | None:
    """Project a gateway's reported facts, inventing nothing.

    ``requested_model`` is the one field allowed to come from the Controller's
    own admission, because the Controller *did* request it.  Every other field
    is the gateway's claim about what actually happened, and a gateway that
    stayed silent produces ``unknown`` rather than an echo of the request --
    otherwise a failover to a different model would be invisible in the receipt.
    """

    if not isinstance(raw, dict):
        if profile is None:
            return None
        raw = {}
    reconciled = reconcile_bridge_receipt(raw)
    if reconciled is not None:
        # A real bridge receipt arrives in its own schema.  One entry point
        # reads both shapes so no caller has to know which layer answered.
        return reconciled
    priced = _priced(raw.get("cost_amount"), raw.get("cost_currency"))
    declared = raw.get("cost_state")
    facts = {
        "gateway": _optional_string(raw.get("gateway"))
        or (profile.gateway if profile else "unknown"),
        "requested_model": _optional_string(raw.get("requested_model"))
        or (profile.model_slug if profile else "unknown"),
        "actual_model": _absent_or(_optional_string(raw.get("actual_model"))),
        "actual_provider": _absent_or(_optional_string(raw.get("actual_provider"))),
        "generation_id": _absent_or(_optional_string(raw.get("generation_id"))),
        "input_tokens": _absent_or(_non_negative_int(raw.get("input_tokens"))),
        "output_tokens": _absent_or(_non_negative_int(raw.get("output_tokens"))),
        "cost_amount": float(raw["cost_amount"]) if priced else None,
        "cost_currency": raw["cost_currency"] if priced else None,
        "cost_state": "reported" if priced else (
            declared if isinstance(declared, str) and declared in CANONICAL_ABSENCE else "unknown"),
        "retries": _absent_or(_non_negative_int(raw.get("retries"))),
        "fallback_models": tuple(item for item in raw.get("fallback_models") or ()
                                 if isinstance(item, str)),
        "privacy_enforced": tuple(item for item in raw.get("privacy_enforced") or ()
                                  if isinstance(item, str)),
        "evidence_class": "reported_claim",
    }
    return facts


def undeclared_failover(facts: dict[str, Any] | None, policy: GatewayPolicy,
                        profile: GatewayProfile | None) -> str | None:
    """Refuse a run the gateway served on a model the Owner never declared.

    This is the check that keeps the boundary honest after the fact.  Admission
    happens before dispatch and constrains what the Controller *asks* for; a
    gateway is free to answer with something else, and only the receipt shows
    it.  A reported model outside the allowlist and outside the declared
    fallback order is a policy violation whoever performed the substitution.
    """

    if not facts:
        return None
    actual = facts.get("actual_model")
    if not isinstance(actual, str) or actual in CANONICAL_ABSENCE:
        return None
    permitted = set(policy.allowed_model_slugs) | set(policy.fallback_models)
    if profile is not None:
        permitted.add(profile.model_slug)
    if permitted and actual not in permitted:
        return "GATEWAY_UNDECLARED_MODEL_SUBSTITUTION"
    if profile is not None and actual != profile.model_slug \
            and actual not in policy.fallback_models:
        return "GATEWAY_UNDECLARED_FAILOVER"
    return None


def privacy_refusal(facts: dict[str, Any] | None, policy: GatewayPolicy) -> str | None:
    """A required data-policy restriction the gateway did not confirm."""

    if not policy.required_privacy:
        return None
    enforced = set((facts or {}).get("privacy_enforced") or ())
    missing = [need for need in policy.required_privacy if need not in enforced]
    return "GATEWAY_PRIVACY_NOT_CONFIRMED" if missing else None



# --------------------------------------------------------------------------- #
# reconciliation with factory-bridge
# --------------------------------------------------------------------------- #

#: The receipt schema ``factory-bridge`` emits from
#: ``src/factory_bridge/openrouter.py``.  Reproduced, not imported: neither
#: repository depends on the other, which is the point of the boundary.
BRIDGE_RECEIPT_SCHEMA = "factory.bridge.metered_execution_receipt.v1"

#: Their refusal code -> this Controller's.  Their names are shorter because
#: they are already inside a gateway module; the Controller's carry the layer
#: prefix its neighbours use, so a bare ``TIMEOUT`` cannot collide with the
#: three other ``*_TIMEOUT`` families already on this seam.
BRIDGE_REFUSALS = {
    "MODEL_NOT_ALLOWED": "GATEWAY_MODEL_NOT_ALLOWLISTED",
    "CONTEXT_TOO_LARGE": "GATEWAY_CONTEXT_TOO_LARGE",
    "LANE_INVALID": "GATEWAY_LANE_INVALID",
    "AUTH_FAILED": "GATEWAY_AUTHENTICATION_FAILED",
    "QUOTA_EXHAUSTED": "GATEWAY_INSUFFICIENT_CREDITS",
    "RATE_LIMITED": "GATEWAY_RATE_LIMITED",
    "PROVIDER_UNAVAILABLE": "GATEWAY_OUTAGE",
    "GATEWAY_REJECTED": "GATEWAY_OUTAGE",
    "MODEL_MISMATCH": "GATEWAY_UNDECLARED_MODEL_SUBSTITUTION",
    "PROVIDER_MISMATCH": "GATEWAY_UNDECLARED_MODEL_SUBSTITUTION",
    "UNSUPPORTED_TOOL_CALL": "GATEWAY_TOOL_CAPABILITY_UNSUPPORTED",
    "TIMEOUT": "GATEWAY_TIMEOUT",
    "DISCONNECTED": "GATEWAY_OUTCOME_UNCERTAIN",
    "MALFORMED_RESPONSE": "GATEWAY_MALFORMED_RESPONSE",
    "RESPONSE_TRUNCATED": "GATEWAY_MALFORMED_RESPONSE",
    "INVALID_EXECUTION_RESULT": "GATEWAY_MALFORMED_RESPONSE",
}

#: The three codes ``openrouter.py`` raises with ``dispatch_started=False``.
#: Every other code inherits the constructor default ``True``, which is the
#: safe direction -- but it is a default rather than a per-site judgement, so
#: ``AUTH_FAILED`` and ``QUOTA_EXHAUSTED`` arrive marked "may have run" even
#: though a rejected key and an exhausted balance are pre-spawn facts.  The
#: Controller does not second-guess it: an unproven negative is not a proof,
#: whichever side failed to prove it.
BRIDGE_PRE_SPAWN_CODES = ("CONTEXT_TOO_LARGE", "LANE_INVALID", "MODEL_NOT_ALLOWED")


def from_bridge_error(code: str, dispatch_started: bool) -> tuple[str, bool]:
    """Translate one ``OpenRouterError`` into this Controller's vocabulary.

    Returns the Controller's refusal code and the ``process_started`` fact.  It
    adds no rule: ``may_reroute`` then decides, exactly as it does for a leg the
    Controller refused itself.
    """

    return (BRIDGE_REFUSALS.get(code, "GATEWAY_OUTCOME_UNCERTAIN"), bool(dispatch_started))


def reconcile_bridge_receipt(raw: Any) -> dict[str, Any] | None:
    """Read a ``metered_execution_receipt.v1`` into this module's facts.

    Three translations, each losing nothing that was measured:

    *Absence.*  Their ``precision`` vocabulary is ``exact`` / ``unknown``, and
    ``unknown`` is already one of Evidence Core's four words, so no fifth
    dialect appears -- unlike the broker's ``unavailable``, which SF-136 had to
    translate.  A non-exact figure becomes ``unknown``, never ``0``.

    *Money.*  ``cost_usd`` is a decimal *string* on purpose, and turning it into
    a float to compare against a ceiling loses that exactness.  Both survive:
    ``cost_amount_text`` keeps their value verbatim and ``cost_amount`` is the
    float the budget arithmetic uses.

    *The provider.*  Final bridge receipts report the serving provider beside
    the allow/deny policy.  Older v1 receipts reported only the allowlist, so
    their serving provider remains ``unknown`` rather than being invented.
    """

    if not isinstance(raw, dict) or raw.get("schema_version") != BRIDGE_RECEIPT_SCHEMA:
        return None
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
    exact_usage = usage.get("precision") == "exact"
    text = cost.get("usd")
    exact_cost = cost.get("precision") == "exact" and isinstance(text, str) and bool(text)
    generation_ids = raw.get("generation_ids")
    generation_id = (generation_ids[0]
                     if isinstance(generation_ids, (list, tuple))
                     and generation_ids and isinstance(generation_ids[0], str)
                     else None)
    return {
        "gateway": "openrouter",
        "receipt_schema": BRIDGE_RECEIPT_SCHEMA,
        "requested_model": _absent_or(_optional_string(
            raw.get("requested_model") or raw.get("model"))),
        "actual_model": _absent_or(_optional_string(
            raw.get("actual_model") or raw.get("model"))),
        "actual_provider": _absent_or(_optional_string(raw.get("actual_provider"))),
        "provider_allowlist": tuple(item for item in raw.get("provider_allowlist") or ()
                                    if isinstance(item, str)),
        "provider_denylist": tuple(item for item in raw.get("provider_denylist") or ()
                                   if isinstance(item, str)),
        "generation_id": _absent_or(generation_id),
        "input_tokens": _absent_or(_non_negative_int(usage.get("prompt_tokens")))
        if exact_usage else "unknown",
        "output_tokens": _absent_or(_non_negative_int(usage.get("completion_tokens")))
        if exact_usage else "unknown",
        "total_tokens": _absent_or(_non_negative_int(usage.get("total_tokens")))
        if exact_usage else "unknown",
        "cost_amount": float(text) if exact_cost else None,
        "cost_amount_text": text if exact_cost else "unknown",
        "cost_currency": "USD" if exact_cost else None,
        "cost_state": "reported" if exact_cost else "unknown",
        "turns": _absent_or(_non_negative_int(raw.get("turns"))),
        "commands": _absent_or(_non_negative_int(raw.get("commands"))),
        "transcript_hash": _absent_or(_optional_string(raw.get("transcript_hash"))),
        "retries": _absent_or(_non_negative_int(raw.get("retry_count"))),
        "fallback_models": (),
        "privacy_enforced": (
            ("zero_data_retention",)
            if raw.get("zero_data_retention_required") is True else ()),
        "evidence_class": "reported_claim",
    }


# --------------------------------------------------------------------------- #

def _priced(amount: Any, currency: Any) -> bool:
    return (isinstance(amount, (int, float)) and not isinstance(amount, bool)
            and isinstance(currency, str) and bool(currency))


def _absent_or(value: Any) -> Any:
    return "unknown" if value is None else value


def _strings(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError("%s must be a list of strings" % name)
    return tuple(value)
