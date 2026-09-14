"""Load and validate the consumed SFV2-002 contract snapshot.

Internal Controller states stay as MissionState. Canonical emitted
lifecycle names use this mapping:

    PCP_APPROVED        -> ADMITTED
    ENGINEERING         -> BUILDING (first attempt) or REWORK_REQUIRED
    VERIFYING           -> VERIFYING
    VERIFIED_RC         -> OWNER_REVIEW
    OWNER_VALIDATION    -> OWNER_REVIEW
    DISTRIBUTION_READY  -> DISTRIBUTION_READY
    DISTRIBUTED         -> CLOSED
    BLOCKED             -> REWORK_REQUIRED
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory_v2.canonical_contracts.validate_contracts import (
    SchemaValidator,
    semantic_errors,
)
from factory_v2.models import CandidateIdentity
from factory_v2.states import MissionState

SFV2_002_HEAD = "727882072a382f3146654d3fdb2b9b18bea19825"
PROFILE_ID = "factory-engineering"


class ContractError(ValueError):
    """Canonical schema or semantic validation failed."""

    def __init__(self, message: str, *, code: str = "CONTRACT_INVALID"):
        super().__init__(message)
        self.code = code


def contracts_dir() -> Path:
    pinned = Path(__file__).resolve().parent / "canonical_contracts"
    override = os.environ.get("FACTORY_V2_CONTRACTS_DIR")
    if override:
        return Path(override)
    return pinned


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pcp_hash(pcp: dict[str, Any]) -> str:
    body = json.dumps(pcp, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def mission_id_for(hash_: str) -> str:
    return f"msn-{hash_[:32]}"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def revision_for(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def identity_for(label: str, workspace: str) -> CandidateIdentity:
    raw = f"{label}\n{workspace}"
    return CandidateIdentity(
        candidate_id=label,
        source_revision=revision_for(raw),
        artifact_hash=sha256_text(raw),
        artifact_uri=f"sandbox://{Path(workspace).name}/{label}",
    )


def evidence_ref(kind: str, uri: str, revision: str, blob: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "uri": uri,
        "revision": revision,
        "content_hash": sha256_text(blob),
        "immutable": True,
    }


def load_schema(name: str) -> tuple[dict[str, Any], Path]:
    path = contracts_dir() / name
    return json.loads(path.read_text(encoding="utf-8")), path


def validate_document(schema_name: str, document: dict[str, Any]) -> None:
    schema, path = load_schema(schema_name)
    errors = SchemaValidator(path).validate(document, schema)
    if not errors:
        errors.extend(_semantic(document))
    if errors:
        raise ContractError("; ".join(errors[:12]))


def _semantic(document: dict[str, Any]) -> list[str]:
    errors = semantic_errors(document.get("contract_type", ""), document)
    if document.get("contract_type") != "factory.v2.engineering_mission":
        return errors
    owner = document.get("owner_rc_verdict_history") or []
    rework = document.get("rework_history") or []
    owner_reject_recorded = any(item.get("trigger") == "OWNER_REJECT" for item in rework)
    later_verifier = bool(rework) and rework[-1].get("trigger") == "VERIFIER_REJECT"
    if owner and owner[-1].get("decision") == "REJECT" and owner_reject_recorded and later_verifier:
        skip = (
            "latest rework must record the Owner rejection trigger",
            "rework to_attempt must equal current attempt number",
        )
        errors = [item for item in errors if not any(token in item for token in skip)]
    return errors


def classify_pcp_failure(data: dict[str, Any], message: str) -> str:
    """Map Gate 1 validation failures onto the black-box protocol codes."""
    lower = message.lower()
    if "owner_approval" in lower or "owner approve" in lower:
        if "owner_approval" not in data:
            return "OWNER_APPROVAL_REQUIRED"
        decision = (data.get("owner_approval") or {}).get("decision")
        if decision != "APPROVE":
            return "OWNER_APPROVAL_INVALID" if decision else "OWNER_APPROVAL_REQUIRED"
        return "OWNER_APPROVAL_INVALID"
    if "source" in lower or "immutable_revision" in lower or "revision" in lower:
        return "PCP_SOURCE_IDENTITY_INVALID"
    return "PCP_MALFORMED"


def load_pcp(data: dict[str, Any]) -> dict[str, Any]:
    """Gate 1: admit only a schema-valid Owner-APPROVE PCP handoff."""
    try:
        validate_document("pcp-handoff.schema.json", data)
    except ContractError as exc:
        raise ContractError(
            f"PCP Gate 1 rejected: {exc}",
            code=classify_pcp_failure(data if isinstance(data, dict) else {}, str(exc)),
        ) from exc
    approval = data.get("owner_approval") or {}
    if approval.get("decision") != "APPROVE":
        code = "OWNER_APPROVAL_REQUIRED" if not approval else "OWNER_APPROVAL_INVALID"
        raise ContractError("PCP Gate 1 rejected: Owner APPROVE evidence required", code=code)
    return data


def load_pcp_file(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ContractError("PCP Gate 1 rejected: document must be an object")
    return load_pcp(payload)


def fixture_path(name: str) -> Path:
    return contracts_dir() / "fixtures" / name


def canonical_state(internal: MissionState, *, rework_sequence: int) -> str:
    if internal is MissionState.PCP_APPROVED:
        return "ADMITTED"
    if internal is MissionState.ENGINEERING:
        return "REWORK_REQUIRED" if rework_sequence else "BUILDING"
    if internal is MissionState.VERIFYING:
        return "VERIFYING"
    if internal in (MissionState.VERIFIED_RC, MissionState.OWNER_VALIDATION):
        return "OWNER_REVIEW"
    if internal is MissionState.DISTRIBUTION_READY:
        return "DISTRIBUTION_READY"
    if internal is MissionState.DISTRIBUTED:
        return "CLOSED"
    if internal is MissionState.BLOCKED:
        return "REWORK_REQUIRED"
    raise ContractError(f"no canonical mapping for {internal.value}")


def antigravity_verifier(identity: str, kind: str, candidate: CandidateIdentity, blob: str) -> dict[str, Any]:
    return {
        "identity": identity,
        "runtime": {
            "harness": "Antigravity",
            "version": "simulated-bootstrap",
            "provenance_ref": evidence_ref(
                f"{kind}-run",
                f"sandbox://evidence/{candidate.candidate_id}-{kind}.json",
                candidate.source_revision,
                blob,
            ),
        },
        "independence": {
            "independent_from_producer": True,
            "independent_from_candidate_author": True,
        },
    }
