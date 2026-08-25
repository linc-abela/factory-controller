"""Token-free local provider/evaluator used by the validation harness.

This is a fixture, and it says so on every dispatch.  A mission that declares
itself real is refused here rather than quietly served a synthetic candidate --
the equality check in the Controller then makes that refusal terminal.

It also honours a small routing contract so failover can be exercised without a
provider: ``FACTORY_CONTROLLER_UNAVAILABLE_PROFILES`` names profiles this
process declines with ``process_started: false``, which is a proof and not a
claim, because nothing here is ever started before the profile is checked.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from .context import CONTEXT_SCHEMA_VERSION, sha256_hex


PROFILE_ENV = "FACTORY_CONTROLLER_UNAVAILABLE_PROFILES"


def _unavailable() -> frozenset:
    return frozenset(
        value for value in os.environ.get(PROFILE_ENV, "").split(",") if value
    )


def dispatch(request: dict) -> dict:
    route = request["input"].get("route") or {}
    profile = route.get("provider_profile")
    receipt = {
        "provider_profile": profile,
        "provider": "local-safe-provider",
        "execution_mode": "fixture",
        "idempotency_key": route.get("idempotency_key"),
        "duration_ms": 0,
        "usage": {"cost_state": "not_applicable"},
    }
    if route.get("execution_mode") == "real":
        return {"status": "refused", "diagnostic": "FIXTURE_PROVIDER_REFUSES_REAL_MISSION",
                "receipt": {**receipt, "process_started": False}}
    if profile is not None and profile in _unavailable():
        return {"status": "provider_unavailable", "diagnostic": "PROFILE_UNAVAILABLE",
                "receipt": {**receipt, "process_started": False,
                            "refusal_code": "PROFILE_UNAVAILABLE"}}
    operation_key = request["operation_key"]
    return {
        "status": "completed",
        "candidate_sha": hashlib.sha1(operation_key.encode(), usedforsecurity=False).hexdigest(),
        "execution_id": operation_key,
        "receipt": {**receipt, "process_started": True},
    }


BROKER_UNAVAILABLE_ENV = "FACTORY_CONTROLLER_BROKER_UNAVAILABLE"


def build_context(request: dict) -> dict:
    """A deterministic fixture broker: it selects exactly what it was asked for.

    It opens no file and inspects no repository, which is the point -- the real
    Context Broker is a separate program, and this exists so the local harness
    can exercise the Controller's binding and refusal paths without one.  Sizes
    are declared by the request, so they are fixture facts and say so.
    """

    if os.environ.get(BROKER_UNAVAILABLE_ENV):
        return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNAVAILABLE"}
    selected = list(dict.fromkeys(request.get("required_anchors") or []))
    manifest = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "mission_input_hash": request["mission_input_hash"],
        "corpus_identity": request["corpus_identity"],
        "policy_identity": request["policy_identity"],
        "selected_refs": selected,
        "unresolved_questions": [],
    }
    digest = sha256_hex(manifest)
    return {
        "status": "built",
        "manifest": {**manifest, "manifest_hash": digest},
        "receipt": {"schema_version": CONTEXT_SCHEMA_VERSION,
                    "context_manifest_hash": digest, "selected_refs": selected,
                    "excluded_refs": [], "mandatory_fact_coverage": selected,
                    "refusal_code": None},
        "measurement": {
            "baseline_context_bytes": None, "baseline_context_files": None,
            "selected_context_bytes": None, "selected_context_files": len(selected),
            "manifest_build_ms": 0, "cache_state": "miss", "cache_identity": digest[:16],
            "built_at": None,
            "head_sha": request.get("baseline_sha"),
            "repository_remote_url": request.get("repository_remote_url"),
        },
    }


def main() -> int:
    return main_with(json.load(sys.stdin))


def main_with(request: dict) -> int:
    """The fixture steps, over a request already read.  Shared with the
    reconciliation adapter, which handles ``context`` and delegates the rest."""

    step = request["step"]
    operation_key = request["operation_key"]
    if step == "context":
        result = build_context(request["input"]["context_request"])
    elif step == "dispatch":
        result = dispatch(request)
    elif step == "verify":
        result = {"verified": True, "evaluator": "local-safe-provider", "candidate_sha": request["input"]["dispatch"]["candidate_sha"]}
    elif step == "evaluate":
        # Run exactly the gates the mission declared. A fixture evaluator that
        # invents its own gate id is the placeholder result SF-134 left open.
        declared = request["input"]["mission"].get("acceptance_gate_ids") or ["LOCAL-SAFE"]
        outcomes = [{"gate_id": gate, "passed": True, "detail": "deterministic local harness"}
                    for gate in declared]
        result = {"passed": True, "gate_outcomes": outcomes}
    elif step == "evidence":
        result = {"accepted": True, "evidence_pointer": "local://" + operation_key, "evidence_class": "rederived"}
    else:
        result = {"accepted": False, "diagnostic": "UNKNOWN_STEP"}
    json.dump(result, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
