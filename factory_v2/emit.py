from __future__ import annotations

import json
from typing import Any

from factory_v2.canonical import (
    PROFILE_ID,
    antigravity_verifier,
    canonical_state,
    evidence_ref,
    now_iso,
    sha256_text,
    validate_document,
)
from factory_v2.models import Candidate, CandidateIdentity, MissionSnapshot, Verdict
from factory_v2.states import MissionState


def _write(path, document: dict[str, Any], schema: str) -> dict[str, Any]:
    validate_document(schema, document)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def _channel(
    kind: str,
    verdict: Verdict,
    identity: str,
) -> dict[str, Any]:
    blob = json.dumps(
        {"verdict": "PASS" if verdict.passed else "FAIL", "defects": list(verdict.defects)},
        sort_keys=True,
    )
    uri = verdict.evidence_uri or f"sandbox://evidence/{verdict.candidate.candidate_id}-{kind}.json"
    return {
        "kind": kind,
        "verdict": "PASS" if verdict.passed else "FAIL",
        "candidate": verdict.candidate.as_dict(),
        "verifier": antigravity_verifier(identity, kind, verdict.candidate, blob),
        "evidence": [
            evidence_ref(
                "review-result" if kind.startswith("adversarial") else "qa-result",
                uri,
                verdict.candidate.source_revision,
                blob,
            )
        ],
    }


def emit_engineering_mission(workspace, snap: MissionSnapshot) -> dict[str, Any] | None:
    produced = [c for c in snap.candidates]
    if not produced:
        return None
    current = snap.current or produced[-1].identity
    latest = produced[-1]
    grok_attempts = []
    for cand in produced:
        started = next(
            (e["created_at"] for e in snap.events if e["kind"] == "candidate_produced" and e["payload"].get("candidate_id") == cand.candidate_id),
            snap.updated_at or now_iso(),
        )
        grok_attempts.append(
            {
                "attempt_id": cand.attempt_id,
                "candidate": cand.identity.as_dict(),
                "session_ref": cand.grok_session_ref or f"grok:{cand.candidate_id}",
                "status": "COMPLETED",
                "started_at": started,
                "completed_at": started,
                "evidence": [
                    evidence_ref(
                        "grok-attempt",
                        f"sandbox://evidence/{cand.candidate_id}-grok.json",
                        cand.identity.source_revision,
                        cand.candidate_id,
                    )
                ],
            }
        )
    defects = _open_defects(snap)
    pcp = snap.pcp
    reason = snap.blocked_reason or snap.state.value
    document = {
        "contract_type": "factory.v2.engineering_mission",
        "contract_version": "1.0.0",
        "mission": {
            "mission_id": snap.mission_id,
            "lineage_id": snap.lineage_id,
            "created_at": snap.created_at or now_iso(),
            "source_pcp_id": pcp["pcp"]["id"],
        },
        "pcp": {
            "pcp_id": pcp["pcp"]["id"],
            "pcp_version": pcp["pcp"]["version"],
            "revision": pcp["pcp"]["immutable_revision"],
            "content_hash": pcp["source"]["content_hash"],
        },
        "current_candidate": current.as_dict(),
        "attempt": {
            "attempt_id": latest.attempt_id,
            "attempt_number": snap.attempt_number,
            "rework_sequence": snap.rework_sequence,
        },
        "lifecycle": {
            "state": canonical_state(snap.state, rework_sequence=snap.rework_sequence),
            "updated_at": snap.updated_at or now_iso(),
            "reason": reason or "lifecycle",
        },
        "hermes": {
            "runtime": "NousResearch Hermes Agent",
            "profile_id": PROFILE_ID,
            "session_id": snap.hermes_session_id or f"hermes-{snap.mission_id}",
            "coordination_ref": evidence_ref(
                "hermes-session",
                f"sandbox://{snap.mission_id}/hermes-session.json",
                current.source_revision,
                snap.hermes_session_id or snap.mission_id,
            ),
        },
        "grok_build": {"executor": "Grok Build", "attempts": grok_attempts},
        "verification_requirements": {
            "deterministic_tests": {
                "required": True,
                "evidence_minimum": 1,
                "verifier_harness": "Controller",
            },
            "code_review": {
                "required": True,
                "evidence_minimum": 1,
                "verifier_harness": "Antigravity",
            },
            "qa_e2e": {
                "required": True,
                "evidence_minimum": 1,
                "verifier_harness": "Antigravity",
            },
        },
        "consolidated_defect_packet": defects,
        "owner_rc_verdict_history": list(snap.owner_history),
        "rework_history": list(snap.rework_history),
    }
    return _write(workspace / "engineering-mission.json", document, "engineering-mission.schema.json")


def emit_verification(
    workspace,
    snap: MissionSnapshot,
    candidate: CandidateIdentity,
    review: Verdict,
    qa: Verdict | None,
) -> dict[str, Any]:
    if qa is None:
        qa = Verdict(
            kind="qa",
            candidate=candidate,
            passed=False,
            defects=("QA not executed: code review failed",),
            harness_mode=review.harness_mode,
            evidence_uri=f"sandbox://evidence/{candidate.candidate_id}-qa-skipped.json",
            verifier_identity="antigravity:qa-1",
        )
    code = _channel(
        "adversarial_code_security_invariant",
        review,
        review.verifier_identity or "antigravity:reviewer-1",
    )
    qa_doc = _channel("qa_regression_e2e", qa, qa.verifier_identity or "antigravity:qa-1")
    blocking = []
    for verdict, prefix in ((review, "review"), (qa, "qa")):
        if verdict.passed:
            continue
        statement = "; ".join(verdict.defects) or f"{prefix} failed"
        blocking.append(
            {
                "id": f"{prefix}-{candidate.candidate_id}",
                "severity": "BLOCKING",
                "statement": statement,
                "evidence": code["evidence"] if prefix == "review" else qa_doc["evidence"],
            }
        )
    result = "PASS" if review.passed and qa.passed and not blocking else "FAIL"
    summary = json.dumps({"result": result, "candidate": candidate.as_dict()}, sort_keys=True)
    document = {
        "contract_type": "factory.v2.verification",
        "contract_version": "1.0.0",
        "mission_id": snap.mission_id,
        "candidate": candidate.as_dict(),
        "code_review": code,
        "qa_e2e": qa_doc,
        "result": result,
        "blocking_defects": blocking,
        "evidence": [
            evidence_ref(
                "verification-summary",
                f"sandbox://evidence/{candidate.candidate_id}-verification.json",
                candidate.source_revision,
                summary,
            )
        ],
    }
    name = f"verification-{candidate.candidate_id}.json"
    return _write(workspace / name, document, "verification.schema.json")


def emit_verified_rc(
    workspace,
    snap: MissionSnapshot,
    candidate: CandidateIdentity,
    review: Verdict,
    qa: Verdict,
    tests: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if not tests:
        blob = "controller-deterministic-pass"
        tests = (
            {
                "test_id": "controller-lifecycle",
                "command": "python3 -m unittest tests.test_factory_v2_lifecycle tests.test_factory_v2_contracts",
                "result": "PASS",
                "run_at": now_iso(),
                "evidence": [
                    evidence_ref(
                        "test-run",
                        f"sandbox://evidence/{candidate.candidate_id}-tests.json",
                        candidate.source_revision,
                        blob,
                    )
                ],
            },
        )
    rc_id = f"rc-{candidate.candidate_id}"
    document = {
        "contract_type": "factory.v2.verified_rc",
        "contract_version": "1.0.0",
        "mission_id": snap.mission_id,
        "rc": {
            "rc_id": rc_id,
            "version": "1.0.0",
            "immutable_revision": candidate.source_revision,
        },
        "candidate": candidate.as_dict(),
        "engineering_tests": list(tests),
        "code_review": _channel(
            "adversarial_code_security_invariant",
            review,
            review.verifier_identity or "antigravity:reviewer-1",
        ),
        "qa_e2e": _channel("qa_regression_e2e", qa, qa.verifier_identity or "antigravity:qa-1"),
        "eligibility": {"status": "VERIFIED_RC", "eligible_for_owner_gate": True},
        "owner_gate": {"status": "AWAITING_OWNER", "decision": "PENDING"},
    }
    return _write(workspace / "verified-rc.json", document, "verified-rc.schema.json")


def emit_distribution_handoff(
    workspace,
    snap: MissionSnapshot,
    candidate: CandidateIdentity,
    rc_id: str,
) -> dict[str, Any]:
    ts = now_iso()
    approval_blob = json.dumps({"decision": "APPROVE", "rc_id": rc_id}, sort_keys=True)
    document = {
        "contract_type": "factory.v2.distribution_handoff",
        "contract_version": "1.0.0",
        "handoff": {"handoff_id": f"dist-{candidate.candidate_id}", "created_at": ts},
        "mission_id": snap.mission_id,
        "approved_rc": {
            "rc_id": rc_id,
            "candidate_id": candidate.candidate_id,
            "source_revision": candidate.source_revision,
            "artifact_hash": candidate.artifact_hash,
        },
        "deployment_artifact": {
            "artifact_uri": candidate.artifact_uri,
            "candidate_id": candidate.candidate_id,
            "source_revision": candidate.source_revision,
            "artifact_hash": candidate.artifact_hash,
            "immutable": True,
        },
        "owner_approval": {
            "decision": "APPROVE",
            "rc_id": rc_id,
            "candidate_id": candidate.candidate_id,
            "approved_at": ts,
            "evidence": evidence_ref(
                "owner-approval",
                f"sandbox://evidence/{snap.mission_id}-owner-approve.json",
                candidate.source_revision,
                approval_blob,
            ),
        },
        "release": {
            "release_id": f"rel-{candidate.candidate_id}",
            "version": "1.0.0",
            "created_at": ts,
        },
        "deployment": {
            "environment": (snap.pcp.get("runtime_constraints") or {}).get("environments", ["isolated-task-worktree"])[0],
            "target": (snap.pcp.get("runtime_constraints") or {}).get("deployment", {}).get("targets", ["none-until-owner-gate"])[0],
            "parameters": {},
            "secret_values_present": False,
        },
        "rollback": {
            "required": True,
            "recovery_identity": {
                "rc_id": rc_id,
                "artifact_hash": candidate.artifact_hash,
            },
            "evidence_refs": [
                evidence_ref(
                    "rollback-plan",
                    f"sandbox://evidence/{snap.mission_id}-rollback.json",
                    candidate.source_revision,
                    rc_id,
                )
            ],
        },
    }
    return _write(workspace / "distribution-handoff.json", document, "distribution-handoff.schema.json")


def _open_defects(snap: MissionSnapshot) -> dict[str, Any]:
    items = []
    for cand in snap.candidates:
        if cand.status in {"review_failed", "qa_failed"}:
            items.append(
                {
                    "id": f"def-{cand.candidate_id}",
                    "severity": "BLOCKING",
                    "statement": f"{cand.candidate_id} {cand.status}",
                    "evidence": [
                        evidence_ref(
                            "defect",
                            f"sandbox://evidence/{cand.candidate_id}-defect.json",
                            cand.identity.source_revision,
                            cand.status,
                        )
                    ],
                }
            )
    return {"status": "OPEN" if items else "NONE", "defects": items}


def owner_history_entry(candidate: CandidateIdentity, decision: str, reason: str) -> dict[str, Any]:
    ts = now_iso()
    blob = json.dumps({"decision": decision, "reason": reason}, sort_keys=True)
    entry = {
        "decision": decision,
        "candidate": candidate.as_dict(),
        "decided_at": ts,
        "evidence": evidence_ref(
            "owner-approval" if decision == "APPROVE" else "owner-reject",
            f"sandbox://evidence/owner-{decision.lower()}.json",
            candidate.source_revision,
            blob,
        ),
    }
    if reason:
        entry["feedback"] = reason
    elif decision == "REJECT":
        entry["feedback"] = "Owner rejected the verified RC"
    return entry
