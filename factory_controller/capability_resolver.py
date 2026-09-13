"""Select an eligible execution profile for a requested capability.

Hermes asks for a capability. This resolver reads the Capability Mapping
pool, excludes live quota/unavailability, and chooses the best remaining
profile. It does not name models and does not encode Sol→Opus or Luna→Grok.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .capability_map import (
    CAP_ARCHITECTURE,
    CAP_IMPLEMENTATION,
    CAP_QA,
    CapabilityMap,
    Profile,
)
from .fleet_harness import QUOTA_EXHAUSTED, TEMPORARILY_UNAVAILABLE

AVAILABLE = "AVAILABLE"

_COST = {
    "runtime": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "max": 4,
    "xhigh": 5,
}
_DIFFICULT = ("difficult", "recovery", "high-risk", "ambiguous", "debugging", "integration-heavy")
_NORMAL = ("normal", "bounded", "well-specified")


@dataclass(frozen=True)
class Resolution:
    capability: str
    profile: Profile
    reason: str
    excluded: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "profile": self.profile.as_dict(),
            "reason": self.reason,
            "excluded": list(self.excluded),
        }


def resolve(
    capability: str,
    catalog: CapabilityMap,
    live: Mapping[str, str] | None = None,
    *,
    context: Mapping[str, Any] | None = None,
) -> Resolution | None:
    pool = catalog.for_capability(capability)
    if not pool:
        return None
    status = dict(live or {})
    excluded = tuple(
        profile.key for profile in pool
        if status.get(profile.key) in (QUOTA_EXHAUSTED, TEMPORARILY_UNAVAILABLE)
    )
    blocked = set(excluded)
    eligible = tuple(profile for profile in pool if profile.key not in blocked)
    if not eligible:
        return None
    ctx = dict(context or {})
    if capability.startswith(CAP_ARCHITECTURE):
        chosen = _architecture(pool, eligible, status)
    elif capability.startswith(CAP_IMPLEMENTATION):
        chosen = _implementation(eligible, ctx)
    elif capability.startswith(CAP_QA):
        chosen = eligible[0], "best_eligible"
    else:
        chosen = eligible[0], "best_eligible"
    if chosen is None:
        return None
    profile, reason = chosen
    return Resolution(capability, profile, reason, excluded)


def _architecture(
    pool: tuple[Profile, ...],
    eligible: tuple[Profile, ...],
    status: Mapping[str, str],
) -> tuple[Profile, str] | None:
    primary = next((profile for profile in eligible if profile.role == "primary"), None)
    if primary is not None:
        return primary, "primary_available"
    primary_all = next((profile for profile in pool if profile.role == "primary"), None)
    primary_status = status.get(primary_all.key) if primary_all is not None else None
    fallback = next(
        (profile for profile in eligible if profile.role == "continuity_fallback"
         or profile.quota_continuity),
        None,
    )
    if fallback is not None and primary_status in (QUOTA_EXHAUSTED, TEMPORARILY_UNAVAILABLE):
        return fallback, "continuity_after_%s" % primary_status.lower()
    if fallback is None and eligible:
        return eligible[0], "best_eligible"
    return None


def _implementation(
    eligible: tuple[Profile, ...],
    context: Mapping[str, Any],
) -> tuple[Profile, str] | None:
    incumbent = str(context.get("incumbent") or "")
    if incumbent:
        hold = next((profile for profile in eligible if profile.key == incumbent), None)
        if hold is not None:
            return hold, "incumbent_continue"
    difficulty = str(context.get("difficulty") or "normal").lower()
    high = difficulty in ("high", "difficult", "recovery")

    def key(profile: Profile) -> tuple:
        purpose = profile.purpose.lower()
        fit = 0
        if high:
            fit += sum(2 for word in _DIFFICULT if word in purpose)
            if profile.effort == "high":
                fit += 1
        else:
            fit += sum(1 for word in _NORMAL if word in purpose)
        cost = _COST.get(profile.effort, 3)
        # Higher fit wins; equal fit prefers cheaper effort.
        return (-fit, cost)

    chosen = sorted(eligible, key=key)[0]
    return chosen, "best_eligible_fleet"
