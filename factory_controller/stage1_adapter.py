"""Adapter process that composes the frozen Stage-1 Evidence Core CLI.

The Controller does not restate admission, bridge, candidate verification, or
evidence authority.  On dispatch it invokes Evidence Core's public first-live
runner once; later Controller steps project the runner's already-bound result.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _stage1_config(request: dict[str, Any]) -> dict[str, Any]:
    mission = request.get("input", {}).get("mission", {})
    if "mission" in mission:
        mission = mission["mission"]
    config = mission.get("stage1")
    if not isinstance(config, dict):
        raise ValueError("mission.stage1 configuration is required")
    return config


def _dispatch(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("stage1.command must be a non-empty argument array")
    mode = config.get("mode", "dry_run")
    if mode not in {"dry_run", "real"}:
        raise ValueError("stage1.mode must be dry_run or real")
    if mode == "real" and config.get("operator_opt_in") is not True:
        return {"status": "refused", "diagnostic": "MISSING_OPERATOR_OPT_IN"}
    output = Path(config.get("output", f"/tmp/factory-stage1-{request['operation_key'].replace(':', '-')}.json"))
    argv = [*command, "--dry-run" if mode == "dry_run" else "--real", "--output", str(output)]
    if mode == "real":
        for key, flag in (("admission", "--admission"), ("repository", "--repository")):
            if config.get(key):
                argv.extend((flag, str(config[key])))
        argv.append("--operator-opt-in")
    completed = subprocess.run(
        argv,
        cwd=config.get("workdir"),
        text=True,
        capture_output=True,
        timeout=float(config.get("timeout_seconds", 1800)),
        check=False,
    )
    if not output.exists():
        return {"status": "retryable_error", "diagnostic": f"STAGE1_EXIT_{completed.returncode}: {completed.stderr.strip()}"}
    result = json.loads(output.read_text())
    envelope = result.get("execution_envelope", {})
    binding = result.get("execution_binding", {})
    candidate = envelope.get("candidate_sha") or binding.get("candidate_sha")
    status = result.get("status")
    if completed.returncode == 0 and status in {"completed", "passed"} and candidate:
        mapped = "completed"
    elif status in {"blocked", "refused", "no_candidate"}:
        mapped = status
    else:
        mapped = "retryable_error" if completed.returncode else "refused"
    return {
        "status": mapped,
        "candidate_sha": candidate,
        "execution_id": envelope.get("execution_id"),
        "diagnostic": result.get("refusal_code") or result.get("status"),
        "stage1_result": result,
    }


def execute(request: dict[str, Any]) -> dict[str, Any]:
    step = request["step"]
    config = _stage1_config(request)
    if step == "dispatch":
        return _dispatch(request, config)
    dispatch = request["input"]["dispatch"]
    result = dispatch.get("stage1_result", {})
    if step == "verify":
        verification = result.get("candidate_commit_verification", {})
        verified = verification.get("verified") is True
        return {"verified": verified, "verification": verification, "diagnostic": None if verified else result.get("refusal_code", "CANDIDATE_VERIFICATION_FAILED")}
    if step == "evaluate":
        outcomes = result.get("gate_outcomes") or result.get("evaluation", {}).get("gate_outcomes")
        if outcomes is None:
            return {"passed": False, "gate_outcomes": [], "diagnostic": "ACCEPTANCE_GATE_UNEVALUATED"}
        passed = bool(outcomes) and all(item.get("passed") is True for item in outcomes)
        return {"passed": passed, "gate_outcomes": outcomes, "diagnostic": None if passed else "ACCEPTANCE_GATE_FAILED"}
    if step == "evidence":
        evidence = result.get("evidence_result", {})
        accepted = evidence.get("status") == "complete"
        return {"accepted": accepted, "retryable": False, "evidence_pointer": evidence.get("artifact_hash"), "evidence": evidence, "diagnostic": None if accepted else result.get("refusal_code", "EVIDENCE_REJECTED")}
    raise ValueError(f"unknown step: {step}")


def main() -> int:
    try:
        response = execute(json.load(sys.stdin))
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        response = {"status": "retryable_error", "diagnostic": f"STAGE1_ADAPTER_ERROR: {exc}"}
    json.dump(response, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
