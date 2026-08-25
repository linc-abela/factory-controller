"""Adapter process that composes the frozen Stage-1 Evidence Core CLI.

The Controller does not restate admission, bridge, candidate verification, or
evidence authority.  On dispatch it invokes Evidence Core's public first-live
runner once; later Controller steps project the runner's already-bound result.

Three things this adapter now reports that SF-134 left it silent about, each of
which the Controller turns into a refusal rather than a guess:

* **execution mode** -- a `--dry-run` result is reported as ``fixture``.  It used
  to be mapped to ``completed`` and became a real mission's result.
* **the idempotency key that reached the bridge** -- re-derived from the binding
  Evidence Core verified, not from anything this process chose.  See
  ``_bound_idempotency_key``.
* **acceptance gate outcomes** -- produced by running the target repository's own
  declared evaluator commands.  A declared gate with no command is ``not_run``,
  which is a failure and never a pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _stage1_config(request: dict[str, Any]) -> dict[str, Any]:
    config = _mission(request).get("stage1")
    if not isinstance(config, dict):
        raise ValueError("mission.stage1 configuration is required")
    return config


def _mission(request: dict[str, Any]) -> dict[str, Any]:
    mission = request.get("input", {}).get("mission", {})
    if "mission" in mission:
        mission = mission["mission"]
    return mission


def _bound_idempotency_key(result: dict[str, Any]) -> str | None:
    """Re-derive the key the bridge was actually asked to bind.

    ``verify_and_bind_execution_envelope`` refuses ``IDEMPOTENCY_BINDING_MISMATCH``
    unless the admitted request's key is exactly
    ``work_item_id:context_manifest_hash``.  The binding it returns therefore
    determines that key, so reading the binding is a proof rather than a
    restatement of what this process would have liked the key to be.
    """

    binding = result.get("execution_binding") or {}
    work_item_id = binding.get("work_item_id")
    manifest = binding.get("context_manifest_hash")
    if not work_item_id or not manifest:
        return None
    return "%s:%s" % (work_item_id, manifest)


def _execution_mode(result: dict[str, Any]) -> str:
    if result.get("execution_mode") == "real" and result.get("fixture_only") is False:
        return "real"
    if result.get("fixture_only") is True:
        return "fixture"
    return "unknown"


def _dispatch(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("stage1.command must be a non-empty argument array")
    mode = config.get("mode", "dry_run")
    if mode not in {"dry_run", "real"}:
        raise ValueError("stage1.mode must be dry_run or real")
    route = request.get("input", {}).get("route") or {}
    receipt: dict[str, Any] = {
        "profile": route.get("profile"),
        "provider_identity": "factory-evidence-core/first-live",
        "usage": {"cost_state": "unknown"},
    }
    if mode == "real" and config.get("operator_opt_in") is not True:
        return {"status": "refused", "diagnostic": "MISSING_OPERATOR_OPT_IN",
                "receipt": {**receipt, "process_started": False,
                            "execution_mode": "not_applicable",
                            "refusal_code": "MISSING_OPERATOR_OPT_IN"}}
    output = Path(config.get("output", f"/tmp/factory-stage1-{request['operation_key'].replace(':', '-')}.json"))
    argv = [*command, "--dry-run" if mode == "dry_run" else "--real", "--output", str(output)]
    if mode == "real":
        for key, flag in (("admission", "--admission"), ("repository", "--repository")):
            if config.get(key):
                argv.extend((flag, str(config[key])))
        argv.append("--operator-opt-in")
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        cwd=config.get("workdir"),
        text=True,
        capture_output=True,
        timeout=float(config.get("timeout_seconds", 1800)),
        check=False,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    if not output.exists():
        # The runner produced no result file, so nothing can be said about
        # whether a provider ran. `process_started` stays absent, which the
        # Controller reads as "may have run" and refuses to reroute.
        return {"status": "retryable_error",
                "diagnostic": f"STAGE1_EXIT_{completed.returncode}: {completed.stderr.strip()}",
                "receipt": {**receipt, "duration_ms": duration_ms,
                            "execution_mode": "unknown"}}
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
        "receipt": {
            **receipt,
            "process_started": bool(result.get("transport_invocations")
                                    or result.get("fake_transport_invocations")),
            "duration_ms": duration_ms,
            "execution_mode": _execution_mode(result),
            "idempotency_key": _bound_idempotency_key(result),
            "refusal_code": result.get("refusal_code"),
        },
    }


def _run_gate(gate_id: str, argv: Any, workdir: Any, timeout: float) -> dict[str, Any]:
    """Run one declared acceptance gate in the target repository."""

    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return {"gate_id": gate_id, "passed": False, "detail": "not_run",
                "diagnostic": "ACCEPTANCE_GATE_COMMAND_UNDECLARED",
                "evidence_class": "rederived"}
    try:
        completed = subprocess.run(argv, cwd=workdir, text=True, capture_output=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"gate_id": gate_id, "passed": False, "detail": "not_run",
                "diagnostic": f"ACCEPTANCE_GATE_UNRUNNABLE: {exc}",
                "evidence_class": "rederived"}
    return {
        "gate_id": gate_id,
        "passed": completed.returncode == 0,
        "detail": " ".join(argv),
        "exit_code": completed.returncode,
        "evidence_class": "rederived",
    }


def _evaluate(request: dict[str, Any], config: dict[str, Any],
              result: dict[str, Any]) -> dict[str, Any]:
    """Execute the gates this mission declared, in the target repository.

    A gate outcome carried out of the stage-1 result is preferred when the
    runner already produced one; otherwise the declared command is run here.
    Either way every declared gate id gets a real outcome, and a gate with
    neither is `not_run`.
    """

    declared = tuple(_mission(request).get("acceptance_gate_ids") or ())
    carried = {item.get("gate_id"): item
               for item in (result.get("gate_outcomes")
                            or result.get("evaluation", {}).get("gate_outcomes") or ())
               if isinstance(item, dict)}
    if not declared:
        return {"passed": False, "gate_outcomes": [],
                "diagnostic": "ACCEPTANCE_GATE_UNDECLARED"}
    commands = config.get("gate_commands") or {}
    workdir = config.get("gate_workdir", config.get("repository", config.get("workdir")))
    timeout = float(config.get("gate_timeout_seconds", 1800))
    outcomes = [carried[gate] if gate in carried
                else _run_gate(gate, commands.get(gate), workdir, timeout)
                for gate in declared]
    passed = all(outcome.get("passed") is True for outcome in outcomes)
    return {"passed": passed, "gate_outcomes": outcomes,
            "diagnostic": None if passed else "ACCEPTANCE_GATE_FAILED"}


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
        return _evaluate(request, config, result)
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
