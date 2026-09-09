"""Owner-to-app fast path: a concise brief becomes Factory-owned admission.

The Owner names who the app serves, the job, essential behavior, and known
constraints.  This module infers the supported envelope, Product Candidate
Package, run contract pointers, and Owner-facing status.  It does not invent a
portal, and it does not let a generated package manufacture new spend or
Production authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import pcp


SCHEMA_VERSION = "factory.controller.owner_app_fast_path.v1"
OWNER_STATES = (
    "Accepted / waiting",
    "Working",
    "Blocked",
    "Needs your decision",
    "Ready for validation",
    "Released / stopped",
)
ENVELOPE_ID = "phase21-browser-local-firebase"
UNSUPPORTED_MARKERS = (
    ("payment", "payments"),
    ("checkout", "payments"),
    ("stripe", "payments"),
    ("real money", "payments"),
    ("login", "authentication"),
    ("sign in", "authentication"),
    ("sign-in", "authentication"),
    ("auth0", "authentication"),
    ("oauth", "authentication"),
    ("authentication", "authentication"),
    ("multi-user", "shared multi-user backend"),
    ("multi user", "shared multi-user backend"),
    ("shared account", "shared multi-user backend"),
    ("postgres", "shared multi-user backend"),
    ("mysql", "shared multi-user backend"),
    ("mongodb", "shared multi-user backend"),
    ("websocket server", "shared multi-user backend"),
    ("shared backend", "shared multi-user backend"),
    ("privileged migration", "privileged data migration"),
    ("migrate production data", "privileged data migration"),
    ("paid service", "new external paid service"),
)

ACTIVE_CONTRACT_NAME = "active-product-contract.json"
MISSION_STATUS_NAME = "STATUS.md"


class BriefRefusal(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Envelope:
    envelope_id: str = ENVELOPE_ID
    stack: str = "single-user browser web application"
    hosting: str = "Firebase Hosting"
    persistence: str = "client-side / browser-local"
    authentication: str = "none required"
    shared_backend: str = "none"
    payments: str = "none"
    new_paid_service: str = "none"


@dataclass(frozen=True)
class AcceptedBrief:
    package_id: str
    original_brief: str
    brief_digest: str
    envelope: Envelope
    package: dict[str, Any]
    assumptions: tuple[str, ...]
    limits: tuple[str, ...]
    owner_state: str
    next_action: str
    evidence_ref: str


def brief_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def unsupported_reasons(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    found: list[str] = []
    for marker, reason in UNSUPPORTED_MARKERS:
        if marker in lowered and reason not in found:
            found.append(reason)
    return tuple(found)


def inspect_supported_envelope(text: str) -> None:
    blocked = unsupported_reasons(_normalize(text))
    if blocked:
        raise BriefRefusal(
            "OWNER_BRIEF_UNSUPPORTED",
            "This request is outside the supported Phase-2.1 envelope (%s). "
            "The Factory will not accept it through the ordinary fast path."
            % ", ".join(blocked),
        )


def package_id_for(text: str) -> str:
    lowered = text.lower()
    if "inventory" in lowered:
        return "household-inventory"
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = slug.strip("-")[:48] or "owner-app"
    if slug[0].isdigit():
        slug = "app-" + slug
    return slug


def inspect_brief(text: str) -> None:
    body = _normalize(text)
    if len(body) < 24:
        raise BriefRefusal(
            "OWNER_BRIEF_TOO_THIN",
            "The brief must say who the app serves, the job, essential behavior, "
            "and any known constraints.")
    if len(body) > 4000:
        raise BriefRefusal(
            "OWNER_BRIEF_TOO_LONG",
            "Keep the Owner brief concise; the Factory will derive the rest.")
    inspect_supported_envelope(body)


def materialize_package(text: str, *, created_at: str,
                        package_id: str | None = None) -> dict[str, Any]:
    inspect_brief(text)
    body = _normalize(text)
    digest = brief_digest(body)
    ident = package_id or package_id_for(body)
    evidence_ref = "owner://brief/%s" % digest
    return {
        "package_id": ident,
        "schema_version": pcp.SCHEMA_VERSION,
        "package_version": 1,
        "supersedes": None,
        "origin": "owner-brief",
        "authored_by": "factory-from-owner-brief",
        "created_at": created_at,
        "problem": {
            "statement": body,
            "evidence_refs": [{"ref": evidence_ref, "external": True}],
        },
        "target_users": [{
            "segment": "household or small-shop operator",
            "context": "A single browser session that must keep local records after refresh; no account is required.",
        }],
        "decision_ledger": [
            {
                "decision_id": "FASTPATH-ENVELOPE-001",
                "question": "What application envelope does this brief fit?",
                "options": [
                    "browser-local Firebase static app",
                    "authenticated multi-user backend",
                    "paid external service",
                ],
                "status": "resolved",
                "resolution": (
                    "Single-user browser web app on Firebase Hosting with "
                    "client-side persistence, no authentication, no shared "
                    "backend, no payments, and no new paid service."
                ),
                "rationale": "Phase-2.1 frozen Owner-to-App envelope.",
            },
            {
                "decision_id": "FASTPATH-INTENT-002",
                "question": "What may the Factory infer without asking the Owner?",
                "options": [
                    "repository, stack, harness, gates, REVIEW config",
                    "ask the Owner to author admission files",
                ],
                "status": "resolved",
                "resolution": "The Factory infers ordinary run and admission material inside standing authority.",
                "rationale": "The Owner supplied only the product brief.",
            },
        ],
        "outcome_criteria": [
            {"outcome_id": "FASTPATH-OUTCOME-001",
             "statement": "A person can add, edit, delete, and search records named in the Owner brief.",
             "measurable_by": "independent product behavior evaluation against the original brief"},
            {"outcome_id": "FASTPATH-OUTCOME-002",
             "statement": "Records remain after refresh or reopen without an account.",
             "measurable_by": "browser-local persistence evaluation"},
            {"outcome_id": "FASTPATH-OUTCOME-003",
             "statement": "The UI is usable on desktop and mobile viewports.",
             "measurable_by": "responsive layout evaluation"},
            {"outcome_id": "FASTPATH-OUTCOME-004",
             "statement": "An exact-artifact Firebase REVIEW URL is produced for Owner Validation.",
             "measurable_by": "sealed REVIEW deployment identity and HTTP health"},
        ],
        "scope": {
            "in_scope": [
                "browser UI for the stated job",
                "client-side persistence",
                "search and basic record editing",
                "Firebase Hosting REVIEW of the exact candidate",
            ],
            "out_of_scope": [
                "accounts", "authentication", "payments",
                "shared multi-user backends", "privileged data migration",
                "new paid cloud services",
            ],
            "prohibitions": [
                "do not ask the Owner to author admission JSON or MISSION files",
                "do not treat REVIEW-ready work as released",
                "do not silently broaden the supported envelope",
            ],
        },
        "required_capabilities": [
            {"profile_id": "P-2", "activated_by": "FASTPATH-OUTCOME-001",
             "reason": "The brief is an interactive browser workflow."},
            {"profile_id": "P-7", "activated_by": "FASTPATH-OUTCOME-003",
             "reason": "The product is a responsive web view."},
        ],
        "authority": {
            "risk_level": "low",
            "budget_ceiling": {
                "value": "not_applicable",
                "reason": "The envelope forbids payments and new paid services.",
            },
            "time_expectation": {
                "value": "not_applicable",
                "reason": "Cycle time is measured; it is not an Owner SLA.",
            },
            "approval_owner_role": "Owner / CEO",
        },
        "investment_decision": {
            "decision": "build",
            "decided_by": "Owner / CEO",
            "decided_at": created_at,
            "conditions": [
                "Keep the experience browser-local.",
                "Require explicit Owner Validation before Production.",
            ],
        },
        "evidence": {
            "validation_findings_refs": [],
            "prototype_refs": [],
            "opportunity_refs": [],
            "competitive_refs": [],
        },
        "recommendation": "Accept the brief inside the frozen Phase-2.1 envelope and derive admission internally.",
        "production_readiness_hints": {
            "platform_indication": "Firebase Hosting static web with separate REVIEW and Production targets",
            "core_invariants": [
                "Owner brief is the product intent",
                "independent QA judges the brief and observable behavior",
                "REVIEW bytes equal the sealed candidate",
            ],
        },
        "non_functional_preferences": ["usable on desktop and mobile", "no required account"],
        "sequencing_preference": "internal admission, provider candidate, independent QA, exact-artifact REVIEW",
        "known_risks": [
            "browser-local storage is not multi-device sync",
            "unsupported requirements must stay disclosed before acceptance",
        ],
    }


def accept_brief(text: str, *, created_at: str,
                 package_id: str | None = None) -> AcceptedBrief:
    package = materialize_package(text, created_at=created_at, package_id=package_id)
    pcp.validate(package)
    intake = pcp.intake(package)
    if intake.verdict != "ACCEPTED":
        raise BriefRefusal("OWNER_BRIEF_NOT_BUILDABLE", intake.verdict)
    body = _normalize(text)
    return AcceptedBrief(
        package_id=package["package_id"],
        original_brief=body,
        brief_digest=brief_digest(body),
        envelope=Envelope(),
        package=package,
        assumptions=(
            "Repository, stack, harness/model, branch, tests, admission and REVIEW configuration are Factory-derived.",
            "Persistence is browser-local to this device/browser.",
        ),
        limits=(
            "No authentication, shared multi-user backend, payments, privileged migration, or new paid service.",
            "Owner Validation and Production remain reserved Owner acts.",
        ),
        owner_state="Accepted / waiting",
        next_action="The Factory will admit and execute the mission; watch the status link.",
        evidence_ref="owner://brief/%s" % brief_digest(body),
    )


def owner_state_for(*, mission_state: str | None, review_ready: bool = False,
                    released: bool = False, owner_attention: bool = False,
                    blocked: bool = False, stale: bool = False) -> dict[str, str]:
    """Map engine facts onto the frozen Owner-facing states."""

    if released:
        state = "Released / stopped"
        next_action = "Use the Production URL, or stop. REVIEW-ready is not released."
    elif owner_attention:
        state = "Needs your decision"
        next_action = "The Factory needs one Owner decision before it can continue."
    elif blocked or stale or mission_state in {"failed", "refused", "cancelled", "escalated"}:
        state = "Blocked"
        if stale:
            next_action = "Heartbeat is stale; the Factory owns recovery of the stuck component."
        else:
            next_action = "The Factory owns recovery unless a reserved Owner act is required."
    elif review_ready:
        state = "Ready for validation"
        next_action = "Open the REVIEW URL and record Owner Validation for this exact RC."
    elif mission_state in {"admitted", "dispatching", "dispatched", "candidate_verified",
                           "evaluated", "evidence_sealed"} or (
            mission_state == "completed" and not review_ready):
        state = "Working"
        if mission_state == "completed":
            next_action = "Preparing the exact-artifact REVIEW; no Owner command is required."
        else:
            next_action = "The Factory is executing the admitted mission."
    elif mission_state is None:
        state = "Accepted / waiting"
        next_action = "Waiting to admit or start provider work."
    else:
        state = "Accepted / waiting"
        next_action = "Durable mission exists; provider work has not started."
    freshness = "stale" if stale else "current"
    return {
        "owner_state": state,
        "next_action": next_action,
        "freshness": freshness,
    }


def render_status(row: Mapping[str, Any]) -> str:
    lines = [
        row.get("owner_state") or "Accepted / waiting",
        "Stage: %s" % (row.get("stage") or "waiting"),
        "Freshness: %s" % (row.get("freshness") or "current"),
        "Next: %s" % (row.get("next_action") or "The Factory owns the next action."),
    ]
    if row.get("review_url"):
        lines.append("REVIEW: %s" % row["review_url"])
    if row.get("known_limitations"):
        lines.append("Limits: %s" % row["known_limitations"])
    return "\n".join(lines)


def write_status(path: str | Path, row: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, **dict(row)}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    markdown = target.with_name(MISSION_STATUS_NAME)
    markdown.write_text(render_status(payload) + "\n", encoding="utf-8")
    return markdown


def active_contract_pointer(state_dir: str | Path) -> Path:
    return Path(state_dir) / ACTIVE_CONTRACT_NAME


def is_envelope_run(run_ref: str | None) -> bool:
    return isinstance(run_ref, str) and run_ref.startswith("owner-brief-")


def derived_contract(package_id: str, *, baseline_sha: str, run_ref: str,
                     remote: str, provider_profiles: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": "factory.controller.product_run_contract.v1",
        "run_ref": run_ref,
        "rationale": (
            "Factory-derived run contract for a Phase-2.1 Owner brief. "
            "The Owner did not author this file."
        ),
        "package_id": package_id,
        "project_id": package_id,
        "provider_profiles": list(provider_profiles),
        "work_class": "backlog",
        "environment_class": "staging",
        "mutates_repository": True,
        "baseline_sha": baseline_sha,
        "acceptance_gate_ids": ["dev-check", "dev-test", "dev-evaluate"],
        "acceptance_gate_source": "%s@%s:dev" % (remote, baseline_sha),
        "acceptance_gate_expectations": {},
        "publish_prefix": "public/",
        "capability_request": "%s-capability-admission-request.json" % package_id,
        "review_environment_id": "%s-review" % package_id,
        "production_environment_id": "%s-production" % package_id,
    }


def mission_statement(accepted: AcceptedBrief) -> str:
    envelope = accepted.envelope
    return "\n".join((
        "# %s — Owner brief mission" % accepted.package_id,
        "",
        "Authoritative Owner brief:",
        "",
        "> %s" % accepted.original_brief,
        "",
        "The Factory derived this file. Do not ask the Owner to edit it.",
        "",
        "## Envelope",
        "",
        "- Stack: %s" % envelope.stack,
        "- Hosting: %s" % envelope.hosting,
        "- Persistence: %s" % envelope.persistence,
        "- Authentication: %s" % envelope.authentication,
        "",
        "## Done when",
        "",
        "Independent QA confirms the original brief and observable behavior, "
        "then an exact-artifact Firebase REVIEW URL is ready for Owner Validation.",
        "",
    )) + "\n"
