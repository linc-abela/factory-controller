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


PROFILE_ENV = "FACTORY_CONTROLLER_UNAVAILABLE_PROFILES"


def _unavailable() -> frozenset:
    return frozenset(
        value for value in os.environ.get(PROFILE_ENV, "").split(",") if value
    )


def dispatch(request: dict) -> dict:
    route = request["input"].get("route") or {}
    profile = route.get("profile")
    receipt = {
        "profile": profile,
        "provider_identity": "local-safe-provider",
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


def main() -> int:
    request = json.load(sys.stdin)
    step = request["step"]
    operation_key = request["operation_key"]
    if step == "dispatch":
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
