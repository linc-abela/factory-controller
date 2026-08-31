"""The smallest machine-checkable Product Candidate Package intake seam.

The Laboratory owns product decisions; the Factory only checks that the
package carries them.  This module deliberately accepts JSON-shaped mappings
so the canonical package can be reviewed and hashed without adding a YAML
runtime dependency to the Controller.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


SCHEMA_VERSION = "1.1.0"
ABSENCE_VALUES = frozenset({
    "unknown", "not_applicable", "not_run", "not_measurable",
})
PROFILE_IDS = frozenset("P-%d" % number for number in range(1, 9))
PROTOTYPE_DISPOSITIONS = frozenset({"DISPOSABLE_SPIKE", "FOUNDATION_SEED"})
DECISIONS = frozenset({"resolved", "open"})
INVESTMENT_DECISIONS = frozenset({"build", "hold", "reject"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_TOP_LEVEL = frozenset({
    "package_id", "schema_version", "package_version", "supersedes",
    "origin", "authored_by", "created_at", "problem", "target_users",
    "decision_ledger", "outcome_criteria", "scope", "required_capabilities",
    "authority", "investment_decision", "evidence", "recommendation",
    "production_readiness_hints", "non_functional_preferences",
    "sequencing_preference", "known_risks",
})
_REQUIRED = (
    "package_id", "schema_version", "package_version", "supersedes", "origin",
    "authored_by", "created_at", "problem", "target_users", "decision_ledger",
    "outcome_criteria", "scope", "required_capabilities", "authority",
    "investment_decision", "evidence",
)


class PCPRefusal(ValueError):
    """A deterministic Product Candidate Package intake refusal."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PCPIntake:
    package_id: str
    package_version: int
    verdict: str
    package_digest: str
    checks: tuple[dict[str, Any], ...]
    mission: dict[str, Any]

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "verdict": self.verdict,
            "package_digest": self.package_digest,
            "checks": [dict(check) for check in self.checks],
            "mission": dict(self.mission),
        }


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def package_digest(package: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(package)).hexdigest()


def _refuse(code: str, detail: str) -> None:
    raise PCPRefusal(code, detail)


def _text(value: Any, field: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _refuse("PCP_FIELD_INVALID", "%s must be a bounded non-empty string" % field)
    return value


def _list(value: Any, field: str, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (not allow_empty and not value):
        _refuse("PCP_FIELD_INVALID", "%s must be a non-empty list" % field)
    return value


def _reference(value: Any, field: str) -> str:
    return _text(value, field, limit=1024)


def _authority_value(value: Any, field: str) -> None:
    if isinstance(value, str):
        if not value.strip():
            _refuse("PCP_AUTHORITY_INVALID", "%s is empty" % field)
        if value in ABSENCE_VALUES:
            return
        return
    if isinstance(value, Mapping):
        if set(value) != {"value", "reason"}:
            _refuse("PCP_AUTHORITY_INVALID", "%s must have value and reason" % field)
        selected = value.get("value")
        if not isinstance(selected, str) or not selected.strip():
            _refuse("PCP_AUTHORITY_INVALID", "%s.value is required" % field)
        _text(value.get("reason"), "%s.reason" % field)
        return
    _refuse("PCP_AUTHORITY_INVALID", "%s must carry a value or absence" % field)


def validate(package: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one PCP against the v1.1.0 contract and return a frozen copy."""

    if not isinstance(package, Mapping):
        _refuse("PCP_NOT_OBJECT", "Product Candidate Package must be an object")
    value = json.loads(json.dumps(package))
    unknown = set(value) - _TOP_LEVEL
    if unknown:
        _refuse("PCP_UNKNOWN_FIELDS", "unknown fields: %s" % ", ".join(sorted(unknown)))
    missing = [field for field in _REQUIRED if field not in value]
    if missing:
        _refuse("PCP_REQUIRED_FIELD_MISSING", ", ".join(missing))
    if value["schema_version"] != SCHEMA_VERSION:
        _refuse("PCP_SCHEMA_UNSUPPORTED", str(value["schema_version"]))
    package_id = _text(value["package_id"], "package_id", limit=128)
    if not PACKAGE_ID.fullmatch(package_id):
        _refuse("PCP_FIELD_INVALID", "package_id contains unsupported characters")
    version = value["package_version"]
    if type(version) is not int or version < 1:
        _refuse("PCP_FIELD_INVALID", "package_version must be a positive integer")
    if version == 1 and value["supersedes"] is not None:
        _refuse("PCP_SUPERSESSION_INVALID", "version 1 must not supersede another package")
    if version > 1 and not isinstance(value["supersedes"], str):
        _refuse("PCP_SUPERSESSION_INVALID", "a superseding package must name its predecessor")
    _text(value["origin"], "origin", limit=128)
    _text(value["authored_by"], "authored_by", limit=256)
    _text(value["created_at"], "created_at", limit=128)

    problem = value["problem"]
    if not isinstance(problem, Mapping) or set(problem) != {"statement", "evidence_refs"}:
        _refuse("PCP_PROBLEM_INVALID", "problem must carry statement and evidence_refs")
    _text(problem["statement"], "problem.statement", limit=4096)
    problem_refs = _list(problem["evidence_refs"], "problem.evidence_refs")
    if not any(isinstance(item, Mapping) and item.get("external") is True
               for item in problem_refs):
        _refuse("PCP_EXTERNAL_EVIDENCE_MISSING",
                "problem.evidence_refs must include an external reference")
    for item in problem_refs:
        if not isinstance(item, Mapping) or set(item) != {"ref", "external"}:
            _refuse("PCP_PROBLEM_INVALID", "problem evidence references have the wrong shape")
        _reference(item["ref"], "problem.evidence_refs.ref")
        if type(item["external"]) is not bool:
            _refuse("PCP_PROBLEM_INVALID", "problem evidence external must be boolean")

    users = _list(value["target_users"], "target_users")
    for item in users:
        if not isinstance(item, Mapping) or set(item) != {"segment", "context"}:
            _refuse("PCP_TARGET_USERS_INVALID", "target_users entries must name segment and context")
        _text(item["segment"], "target_users.segment")
        _text(item["context"], "target_users.context", limit=2048)

    decisions = _list(value["decision_ledger"], "decision_ledger")
    decision_ids: set[str] = set()
    for item in decisions:
        if not isinstance(item, Mapping):
            _refuse("PCP_DECISION_INVALID", "decision ledger entries must be objects")
        required = {"decision_id", "question", "options", "status", "rationale"}
        if not required.issubset(item):
            _refuse("PCP_DECISION_INVALID", "decision ledger entry is incomplete")
        decision_id = _text(item["decision_id"], "decision_ledger.decision_id", limit=128)
        if decision_id in decision_ids:
            _refuse("PCP_DECISION_INVALID", "decision ids must be unique")
        decision_ids.add(decision_id)
        _text(item["question"], "decision_ledger.question", limit=2048)
        _list(item["options"], "decision_ledger.options")
        if item["status"] not in DECISIONS:
            _refuse("PCP_DECISION_INVALID", "decision status is not resolved or open")
        _text(item["rationale"], "decision_ledger.rationale", limit=2048)
        if item["status"] == "resolved":
            _text(item.get("resolution"), "decision_ledger.resolution", limit=2048)
        else:
            _text(item.get("owner_role"), "decision_ledger.owner_role", limit=256)
            _text(item.get("deadline"), "decision_ledger.deadline", limit=128)

    outcomes = _list(value["outcome_criteria"], "outcome_criteria")
    outcome_ids: set[str] = set()
    for item in outcomes:
        if not isinstance(item, Mapping) or set(item) != {"outcome_id", "statement", "measurable_by"}:
            _refuse("PCP_OUTCOME_INVALID", "outcome criteria entries have the wrong shape")
        outcome_id = _text(item["outcome_id"], "outcome_criteria.outcome_id", limit=128)
        if outcome_id in outcome_ids:
            _refuse("PCP_OUTCOME_INVALID", "outcome ids must be unique")
        outcome_ids.add(outcome_id)
        _text(item["statement"], "outcome_criteria.statement", limit=2048)
        _text(item["measurable_by"], "outcome_criteria.measurable_by", limit=512)

    scope = value["scope"]
    if (not isinstance(scope, Mapping)
            or set(scope) != {"in_scope", "out_of_scope", "prohibitions"}):
        _refuse("PCP_SCOPE_INVALID", "scope must name all three boundary lists")
    for field in ("in_scope", "out_of_scope", "prohibitions"):
        items = _list(scope[field], "scope.%s" % field, allow_empty=True)
        for item in items:
            _text(item, "scope.%s" % field, limit=2048)

    capabilities = _list(value["required_capabilities"], "required_capabilities",
                         allow_empty=True)
    activation_ids = decision_ids | outcome_ids
    capability_ids: set[str] = set()
    for item in capabilities:
        if not isinstance(item, Mapping) or set(item) != {"profile_id", "activated_by", "reason"}:
            _refuse("PCP_CAPABILITY_INVALID", "required capability entry has the wrong shape")
        profile_id = _text(item["profile_id"], "required_capabilities.profile_id", limit=16)
        if profile_id not in PROFILE_IDS:
            _refuse("PCP_CAPABILITY_INVALID", "unknown capability profile %s" % profile_id)
        if profile_id in capability_ids:
            _refuse("PCP_CAPABILITY_INVALID", "capability profile ids must be unique")
        capability_ids.add(profile_id)
        if item["activated_by"] not in activation_ids:
            _refuse("PCP_CAPABILITY_INVALID", "activated_by does not resolve in this package")
        _text(item["reason"], "required_capabilities.reason", limit=2048)

    authority = value["authority"]
    if (not isinstance(authority, Mapping)
            or set(authority) != {"risk_level", "budget_ceiling", "time_expectation",
                                  "approval_owner_role"}):
        _refuse("PCP_AUTHORITY_INVALID", "authority is incomplete")
    if authority["risk_level"] not in RISK_LEVELS:
        _refuse("PCP_AUTHORITY_INVALID", "unsupported authority risk level")
    _authority_value(authority["budget_ceiling"], "authority.budget_ceiling")
    _authority_value(authority["time_expectation"], "authority.time_expectation")
    _text(authority["approval_owner_role"], "authority.approval_owner_role", limit=256)

    investment = value["investment_decision"]
    if (not isinstance(investment, Mapping)
            or set(investment) != {"decision", "decided_by", "decided_at", "conditions"}):
        _refuse("PCP_INVESTMENT_INVALID", "investment_decision is incomplete")
    if investment["decision"] not in INVESTMENT_DECISIONS:
        _refuse("PCP_INVESTMENT_INVALID", "investment decision is not build, hold, or reject")
    _text(investment["decided_by"], "investment_decision.decided_by", limit=256)
    _text(investment["decided_at"], "investment_decision.decided_at", limit=128)
    _list(investment["conditions"], "investment_decision.conditions", allow_empty=True)

    evidence = value["evidence"]
    if not isinstance(evidence, Mapping):
        _refuse("PCP_EVIDENCE_INVALID", "evidence must be an object")
    required_evidence = {"validation_findings_refs", "prototype_refs", "opportunity_refs",
                         "competitive_refs"}
    if not required_evidence.issubset(evidence):
        _refuse("PCP_EVIDENCE_INVALID", "evidence is incomplete")
    for field in ("validation_findings_refs", "opportunity_refs", "competitive_refs"):
        refs = _list(evidence[field], "evidence.%s" % field, allow_empty=True)
        for ref in refs:
            _reference(ref, "evidence.%s" % field)
    prototype_refs = _list(evidence["prototype_refs"], "evidence.prototype_refs", allow_empty=True)
    for item in prototype_refs:
        if not isinstance(item, Mapping) or not {"ref", "disposition"}.issubset(item):
            _refuse("PCP_EVIDENCE_INVALID", "prototype reference is incomplete")
        _reference(item["ref"], "evidence.prototype_refs.ref")
        if item["disposition"] not in PROTOTYPE_DISPOSITIONS:
            _refuse("PCP_EVIDENCE_INVALID", "unknown prototype disposition")
        if item["disposition"] == "FOUNDATION_SEED":
            commit_sha = item.get("commit_sha")
            if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                _refuse("PCP_EVIDENCE_INVALID", "FOUNDATION_SEED requires a commit SHA")

    if "P-5" in capability_ids and authority["risk_level"] not in {"high", "critical"}:
        _refuse("PCP_CAPABILITY_INVALID", "P-5 payments requires high or critical risk")
    if "P-6" in capability_ids and "inference" not in json.dumps(authority).lower():
        _refuse("PCP_CAPABILITY_INVALID", "P-6 AI requires a stated inference budget")
    if investment["decision"] != "build":
        _refuse("PCP_NOT_BUILDABLE", "only a build investment decision enters Factory intake")
    return value


def intake(package: Mapping[str, Any]) -> PCPIntake:
    """Validate and materialize the one mission blueprint derived from a PCP."""

    value = validate(package)
    decisions = value["decision_ledger"]
    open_decisions = [item for item in decisions if item["status"] == "open"]
    verdict = "ACCEPTED_DEGRADED" if open_decisions else "ACCEPTED"
    checks = tuple(
        {"check_id": check_id, "status": "PASS"}
        for check_id in ("C-1", "C-2", "C-3", "C-4", "C-5", "C-6", "C-7",
                         "C-8", "C-9", "C-11", "C-12")
    )
    mission = {
        "work_item_id": "%s:build" % value["package_id"],
        "product_id": value["package_id"],
        "capability": "development",
        "environment_class": "staging",
        "mutates_repository": True,
        "source_pcp": "%s@v%s" % (value["package_id"], value["package_version"]),
        "objective": value["outcome_criteria"][0]["statement"],
        "required_capabilities": value["required_capabilities"],
        "open_decisions": [item["decision_id"] for item in open_decisions],
    }
    return PCPIntake(
        package_id=value["package_id"],
        package_version=value["package_version"],
        verdict=verdict,
        package_digest=package_digest(value),
        checks=checks,
        mission=mission,
    )


def load(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _refuse("PCP_UNREADABLE", str(exc))
    return validate(value)


def materialize_casino_pcp() -> dict[str, Any]:
    """Materialize Casino from the recorded product decisions, not invention."""

    return {
        "package_id": "lodus-casino",
        "schema_version": SCHEMA_VERSION,
        "package_version": 1,
        "supersedes": None,
        "origin": "laboratory",
        "authored_by": "laboratory",
        "created_at": "2026-08-31T00:00:00Z",
        "problem": {
            "statement": "A one-player card game should make every revealed card change the visible odds and fair return.",
            "evidence_refs": [
                {"ref": "vault://archive/brainstorm/lodus-casino-game-poc-plan.md@5b38a8b", "external": True},
            ],
        },
        "target_users": [{
            "segment": "curious single player",
            "context": "A browser session exploring a finite-shoe higher/lower game; no account or payment is required.",
        }],
        "decision_ledger": [
            {
                "decision_id": "CASINO-RULES-001",
                "question": "What is the first proof product's game boundary?",
                "options": ["finite 52-card shoe", "infinite deck", "real-money table"],
                "status": "resolved",
                "resolution": "One finite 52-card shoe, no jokers, with higher, lower, and exact push outcomes.",
                "rationale": "The recorded POC plan makes changing odds after every reveal the product thesis.",
            },
            {
                "decision_id": "CASINO-SAFETY-002",
                "question": "What money, account, and server surface belongs in the MVP?",
                "options": ["browser-only local demo", "accounts and payments", "operator casino integration"],
                "status": "resolved",
                "resolution": "Browser-only static experience with local persistence; no accounts, backend, payments, or real money.",
                "rationale": "The recorded plan explicitly defers casino compliance, payments, and account systems.",
            },
            {
                "decision_id": "CASINO-AI-003",
                "question": "What authority may an optional AI dealer have?",
                "options": ["narrative interpretation", "probability authority", "wager authority"],
                "status": "resolved",
                "resolution": "AI is optional and narrative-only; deterministic game math and wager availability remain authoritative.",
                "rationale": "The recorded plan separates the game engine from any dealer commentary.",
            },
        ],
        "outcome_criteria": [
            {"outcome_id": "CASINO-OUTCOME-001", "statement": "Every playable round derives higher, lower, and push counts from the remaining shoe.", "measurable_by": "deterministic finite-shoe math tests"},
            {"outcome_id": "CASINO-OUTCOME-002", "statement": "A player can draw, choose higher or lower, see the result, and continue until a deterministic no-bet state.", "measurable_by": "browser critical-path smoke"},
            {"outcome_id": "CASINO-OUTCOME-003", "statement": "A fresh or resumed browser session preserves the local shoe state without an account.", "measurable_by": "local persistence smoke"},
            {"outcome_id": "CASINO-OUTCOME-004", "statement": "The review and production web targets serve one immutable artifact with a reachable health path.", "measurable_by": "deployment marker and HTTP health probe"},
        ],
        "scope": {
            "in_scope": ["finite 52-card shoe", "higher/lower/push math", "dynamic fair return", "responsive browser UI", "safe local persistence"],
            "out_of_scope": ["accounts", "payments", "real money", "casino compliance", "backend services", "operator tooling"],
            "prohibitions": ["do not represent a browser POC as casino-grade randomness", "do not let AI alter game math", "do not use mutable artifact tags"],
        },
        "required_capabilities": [
            {"profile_id": "P-2", "activated_by": "CASINO-OUTCOME-002", "reason": "The browser flow requires accessible loading, error, and responsive interaction primitives."},
            {"profile_id": "P-7", "activated_by": "CASINO-OUTCOME-002", "reason": "The product is a responsive web view, not a native mobile application."},
        ],
        "authority": {
            "risk_level": "medium",
            "budget_ceiling": {"value": "not_applicable", "reason": "The MVP forbids payments and external spend."},
            "time_expectation": {"value": "not_applicable", "reason": "The MVP is bounded by the Phase-1 proof, not a delivery SLA."},
            "approval_owner_role": "Owner / CEO",
        },
        "investment_decision": {
            "decision": "build",
            "decided_by": "Owner / CEO",
            "decided_at": "2026-08-31T00:00:00Z",
            "conditions": ["Keep the experience browser-only and label it as a POC.", "Require hands-on Owner Validation before Production."],
        },
        "evidence": {
            "validation_findings_refs": ["vault://active/software-factory/roadmap/phase-1-completion.md@5b38a8b"],
            "prototype_refs": [{"ref": "vault://archive/brainstorm/lodus-casino-game-poc-plan.md@5b38a8b", "disposition": "DISPOSABLE_SPIKE"}],
            "opportunity_refs": [],
            "competitive_refs": [],
        },
        "recommendation": "Proceed with the narrow browser proof and defer commercial casino decisions.",
        "production_readiness_hints": {
            "platform_indication": "static web host with separate REVIEW and Production targets",
            "core_invariants": ["W + L + T equals remaining-card count", "fair return is (W + L) / W when W > 0", "push returns stake exactly", "the promoted artifact digest equals the validated review digest"],
        },
        "non_functional_preferences": ["clear odds display", "accessible controls", "no permanent AI chat panel"],
        "sequencing_preference": "math engine, then playable browser flow, then exact-artifact review and production",
        "known_risks": ["browser randomness is suitable only for a POC", "missing external hosting access must remain an explicit blocker"],
    }


__all__ = [
    "PCPIntake", "PCPRefusal", "SCHEMA_VERSION", "intake", "load",
    "materialize_casino_pcp", "package_digest", "validate",
]
