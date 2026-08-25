"""Token-free local provider/evaluator used by the validation harness."""

from __future__ import annotations

import hashlib
import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    step = request["step"]
    operation_key = request["operation_key"]
    if step == "dispatch":
        result = {"status": "completed", "candidate_sha": hashlib.sha1(operation_key.encode(), usedforsecurity=False).hexdigest(), "execution_id": operation_key}
    elif step == "verify":
        result = {"verified": True, "evaluator": "local-safe-provider", "candidate_sha": request["input"]["dispatch"]["candidate_sha"]}
    elif step == "evidence":
        result = {"accepted": True, "evidence_pointer": "local://" + operation_key, "evidence_class": "rederived"}
    else:
        result = {"accepted": False, "diagnostic": "UNKNOWN_STEP"}
    json.dump(result, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

