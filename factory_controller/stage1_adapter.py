"""Adapter process that composes the frozen Stage-1 Evidence Core CLI.

The Controller does not restate admission, bridge, candidate verification, or
evidence authority.  On dispatch it invokes Evidence Core's public first-live
runner once; later Controller steps project the runner's already-bound result.

Three things this adapter now reports that SF-134 left it silent about, each of
which the Controller turns into a refusal rather than a guess:

* **execution mode** -- a `--dry-run` result is reported as ``fixture``.  It used
  to be mapped to ``completed`` and became a real mission's result.
* **the idempotency key that reached the bridge** -- consumed from the matching
  execution envelope and binding Evidence Core verified, not re-derived by this
  process.  See
  ``_bound_idempotency_key``.
* **acceptance gate outcomes** -- produced by running the target repository's own
  declared evaluator commands.  A declared gate with no command is ``not_run``,
  which is a failure and never a pass.  Mutating missions are the strict form:
  their commands run only in a detached worktree at the verified candidate
  SHA, never in the admitted baseline checkout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import context, context_adapter, safe_provider


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


def _repository(mission: dict[str, Any], stage1: dict[str, Any]) -> str:
    """Select the admitted checkout and validate revision provenance metadata."""

    repository = stage1.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("CONTEXT_REPOSITORY_UNCONFIGURED")
    grounding = stage1.get("revision_grounding")
    if grounding is None:
        return repository
    if not Path(repository).is_absolute():
        raise ValueError("REVISION_GROUNDING_INVALID")
    if not isinstance(grounding, dict) \
            or grounding.get("schema_version") \
            != context.REVISION_GROUNDING_SCHEMA \
            or grounding.get("kind") != "revision" \
            or grounding.get("source") != "factory-bridge" \
            or grounding.get("project_id") != mission.get("project_id") \
            or grounding.get("repository_remote_url") \
            != mission.get("repository_remote_url") \
            or grounding.get("revision_sha") != mission.get("baseline_sha") \
            or grounding.get("checkout") != repository:
        raise ValueError("REVISION_GROUNDING_INVALID")
    return repository


def _context(request: dict[str, Any]) -> dict[str, Any]:
    """Bind a real mission's context to the checkout it declares."""

    mission = _mission(request)
    stage1 = mission.get("stage1")
    try:
        repository = _repository(mission, stage1) \
            if isinstance(stage1, dict) else None
    except ValueError as exc:
        return {"status": "refused", "refusal_code": str(exc)}
    if repository is None:
        return {"status": "unavailable",
                "refusal_code": "CONTEXT_REPOSITORY_UNCONFIGURED"}
    return context_adapter.build(
        request["input"]["context_request"], repo=repository)


def _bound_idempotency_key(result: dict[str, Any]) -> str | None:
    """Return the key both execution proof records explicitly echoed.

    ``verify_and_bind_execution_envelope`` binds the request to the canonical
    key, but that is not proof that the execution layer returned the same key.
    Missing or conflicting echoes stay unknown and are refused by the real
    mission's Controller guard.
    """

    keys = []
    for source_name in ("execution_envelope", "execution_binding"):
        source = result.get(source_name)
        if isinstance(source, dict) and "idempotency_key" in source:
            key = source["idempotency_key"]
            if not isinstance(key, str) or not key:
                return None
            keys.append(key)
    if not keys:
        return None
    if len(keys) > 1 and len(set(keys)) != 1:
        return None
    status = result.get("status")
    if status in {"completed", "passed"} and len(keys) != 2:
        return None
    return keys[0]


def _execution_mode(result: dict[str, Any]) -> str:
    """Project mode only from execution-layer proof, never from a request hint."""

    modes = []
    for source_name in ("execution_envelope", "execution_binding"):
        source = result.get(source_name)
        if isinstance(source, dict) and "execution_mode" in source:
            modes.append(source["execution_mode"])
    if not modes or len(set(modes)) != 1:
        return "unknown"
    mode = modes[0]
    if mode == "real" and result.get("fixture_only") is False:
        return "real"
    if mode == "fixture" and result.get("fixture_only") is True:
        return "fixture"
    return "unknown"


def _revision_argv(config: dict[str, Any]) -> list[str]:
    """Declare the revision identity, when the mission's grounding names one.

    Nothing is derived here.  The values come from the immutable revision
    grounding ``_repository`` has already checked against the mission, and the
    Bridge refuses any declared base that is not the admitted baseline.
    """

    grounding = config.get("revision_grounding")
    if not isinstance(grounding, dict):
        return []
    argv: list[str] = []
    if grounding.get("revision_sha"):
        argv.extend(("--revision-base", str(grounding["revision_sha"])))
    if grounding.get("predecessor_sha"):
        argv.extend(("--revision-predecessor", str(grounding["predecessor_sha"])))
    return argv


def _reconcile(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Look one already-sealed provider result up.  Never produce a new one.

    This operation exists so that "the recovery step cannot run a provider" is
    a property of the code path rather than of the caller's intentions.  It
    refuses outright without a reconciliation proof, and the frame it sends
    carries that proof's digest -- which puts the Bridge into replay-or-refuse
    before a lane is allocated or an adapter is asked for anything.
    """

    route = request.get("input", {}).get("route", {})
    route = route if isinstance(route, dict) else {}
    proof = route.get("reconcile_proof")
    record = route.get("reconcile_proof_record")
    if not isinstance(proof, str) or len(proof) != 64 \
            or not isinstance(record, dict) or record.get("proof_digest") != proof:
        return {"status": "refused",
                "diagnostic": "RECONCILE_PROOF_MISSING",
                "receipt": {"provider": "factory-evidence-core/first-live",
                            "provider_profile": None,
                            "process_started": False,
                            "execution_mode": "not_applicable",
                            "usage": {"cost_state": "not_applicable"},
                            "refusal_code": "RECONCILE_PROOF_MISSING"}}
    if config.get("mode") != "real":
        # A fixture mission has no sealed provider result to reconcile.
        return {"status": "refused",
                "diagnostic": "RECONCILE_NOT_A_REAL_MISSION",
                "receipt": {"provider": "factory-evidence-core/first-live",
                            "provider_profile": None,
                            "process_started": False,
                            "execution_mode": "not_applicable",
                            "usage": {"cost_state": "not_applicable"},
                            "refusal_code": "RECONCILE_NOT_A_REAL_MISSION"}}
    return _dispatch(request, config, reconcile_proof=proof,
                     reconcile_record=record)


def _dispatch(request: dict[str, Any], config: dict[str, Any], *,
              reconcile_proof: str | None = None,
              reconcile_record: dict[str, Any] | None = None) -> dict[str, Any]:
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("stage1.command must be a non-empty argument array")
    mode = config.get("mode", "dry_run")
    if mode not in {"dry_run", "real"}:
        raise ValueError("stage1.mode must be dry_run or real")
    route = request.get("input", {}).get("route") or {}
    receipt: dict[str, Any] = {
        "provider_profile": route.get("provider_profile"),
        "provider": "factory-evidence-core/first-live",
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
        for key, flag in (("admission", "--admission"),
                          ("repository", "--repository"),
                          ("mission_brief", "--mission-brief")):
            if config.get(key):
                argv.extend((flag, str(config[key])))
        argv.extend(_revision_argv(config))
        if reconcile_proof is not None:
            # The whole instruction of a reconciliation: answer from the sealed
            # replay response bound to this key, or refuse.  The Bridge, not
            # this process, is what enforces it.
            argv.extend(("--reconcile-replay", reconcile_proof))
            # The proof body itself, written beside the result the runner is
            # about to produce -- provably the same filesystem, because this
            # process reads that result back from it.  The runner cross-checks
            # the body against the digest above before either is used.
            proof_path = output.with_name(output.name + ".reconciliation.json")
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            proof_path.write_text(
                json.dumps(reconcile_record, sort_keys=True), encoding="utf-8")
            argv.extend(("--reconciliation-proof", str(proof_path)))
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
    workspace = _candidate_workspace(result)
    status = result.get("status")
    if completed.returncode == 0 and status in {"completed", "passed"} and candidate:
        mapped = "completed"
    elif status in {"blocked", "refused", "no_candidate"}:
        mapped = status
    else:
        mapped = "retryable_error" if completed.returncode else "refused"
    return {
        "status": mapped,
        "candidate_sha": candidate if mapped == "completed" else None,
        "candidate_workspace": workspace if mapped == "completed" else None,
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
    # The exit status is recorded exactly as observed and never reinterpreted:
    # the portfolio's own stop conditions forbid rewriting a non-zero gate as
    # met or as an absence word, and a gate that could not find its tooling
    # exits like any other failing command.  What was missing was the evidence
    # to tell those apart afterwards -- a bare 127 with no output cost a whole
    # session -- so the process's own last words travel with the status.
    #
    # Both streams travel, because an evaluator's own numbers are on standard
    # output and `unittest` writes its count to standard error.  An improvement
    # is measured from what the project's declared gate said about itself; a
    # gate outcome that carried only a number left the Controller no honest
    # measurement to compare, and inventing a second evaluator to get one would
    # put a measurement authority here that belongs to the project.
    return {
        "gate_id": gate_id,
        "passed": completed.returncode == 0,
        "detail": " ".join(argv),
        "exit_code": completed.returncode,
        "evidence_class": "rederived",
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _tail(text: Any, limit: int = 2000) -> str:
    """The end of a gate's diagnostic stream, bounded, or a typed absence."""

    if not isinstance(text, str) or not text.strip():
        return "not_applicable"
    return text[-limit:]


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_GIT_CLEAN_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def _candidate_sha(result: dict[str, Any]) -> str | None:
    """Read the already-verified candidate identity from the dispatch result."""

    envelope = result.get("execution_envelope") or {}
    binding = result.get("execution_binding") or {}
    candidate = (result.get("candidate_sha") or envelope.get("candidate_sha")
                 or binding.get("candidate_sha"))
    return candidate if isinstance(candidate, str) and _GIT_SHA.fullmatch(candidate) else None


def _candidate_workspace(result: dict[str, Any]) -> Any:
    """Read the lane/worktree proof carried by Evidence Core's binding."""

    binding = result.get("execution_binding") or {}
    envelope = result.get("execution_envelope") or {}
    for source in (result, binding, envelope):
        if isinstance(source, dict) and "candidate_workspace" in source:
            return source["candidate_workspace"]
    return None


def _workspace_matches_candidate(value: Any, candidate_sha: str | None) -> bool:
    """Check the additive workspace proof before projecting a gate result."""

    expected = {"schema_version", "lane_id", "worktree", "source_checkout",
                "candidate_ref", "baseline_sha", "candidate_sha", "head_sha",
                "clean"}
    if not isinstance(value, dict) or set(value) != expected:
        return False
    lane_id = value.get("lane_id")
    if (not isinstance(lane_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", lane_id)
            or value.get("candidate_ref") != "refs/factory/lanes/%s" % lane_id):
        return False
    for field in ("worktree", "source_checkout"):
        path = value.get(field)
        if (not isinstance(path, str) or not path.startswith("/")
                or len(path) > 1024 or "\n" in path or "\x00" in path):
            return False
    return (value.get("schema_version") == "1.0"
            and value.get("candidate_sha") == candidate_sha
            and value.get("head_sha") == candidate_sha
            and isinstance(candidate_sha, str) and _GIT_SHA.fullmatch(candidate_sha)
            and value.get("clean") is True)


def _candidate_worktree(repository: Any, candidate_sha: str,
                         timeout: float) -> tuple[tempfile.TemporaryDirectory, Path]:
    """Materialize a detached candidate worktree without touching the source tree.

    The Bridge's disposable provider lane is gone by the time Controller
    evaluates the mission, but it imports the exact candidate object into the
    registered checkout.  A detached worktree is therefore the smallest safe
    handoff: the source checkout remains on its original branch, while every
    gate sees the candidate commit and only that commit.
    """

    if not isinstance(repository, (str, os.PathLike)):
        raise OSError("candidate source checkout is unavailable")
    source = Path(repository).resolve()
    if not source.is_dir():
        raise OSError("candidate source checkout is not a directory")
    temporary = tempfile.TemporaryDirectory(prefix="factory-candidate-")
    worktree = Path(temporary.name) / "checkout"
    try:
        completed = subprocess.run(
            ("git", "-C", str(source), "worktree", "add", "--force",
             "--quiet", "--detach", str(worktree), candidate_sha),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_GIT_CLEAN_ENV,
            check=False,
        )
        if completed.returncode != 0 or not worktree.is_dir():
            detail = completed.stderr.strip() or "git worktree add failed"
            raise OSError(detail)
        return temporary, worktree
    except Exception:
        temporary.cleanup()
        raise


def _remove_candidate_worktree(repository: Any, worktree: Path,
                                temporary: tempfile.TemporaryDirectory) -> None:
    """Release only the disposable candidate checkout, best effort."""

    try:
        subprocess.run(
            ("git", "-C", str(Path(repository).resolve()), "worktree", "remove",
             "--force", str(worktree)),
            capture_output=True,
            text=True,
            timeout=30,
            env=_GIT_CLEAN_ENV,
            check=False,
        )
    finally:
        temporary.cleanup()


def _changed_paths(repository: Any, baseline_sha: Any, candidate_sha: str,
                   timeout: float) -> Any:
    """What the candidate changed, named by the layer that owns candidate truth.

    Git is the candidate authority and this is its seam, so the list is taken
    from the repository rather than from anything the provider said about
    itself.  An unknown change set is a typed absence and never an empty list:
    `improvement.check_change_set` refuses an empty one outright rather than
    reading it as "nothing protected was touched", and that refusal only works
    if this function never manufactures the empty case.
    """

    if not isinstance(repository, (str, os.PathLike)) \
            or not isinstance(baseline_sha, str) or not _GIT_SHA.match(baseline_sha):
        return "unknown"
    try:
        completed = subprocess.run(
            ("git", "-C", str(Path(repository).resolve()), "diff",
             "--name-only", baseline_sha, candidate_sha),
            capture_output=True, text=True, timeout=timeout,
            env=_GIT_CLEAN_ENV, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return sorted({line.strip() for line in completed.stdout.splitlines()
                   if line.strip()})


def _render_candidate_command(argv: Any, baseline: Any,
                              candidate: Path) -> list[str] | None:
    """Redirect source-checkout paths in a derived command to the candidate."""

    if (not isinstance(argv, list) or not argv
            or not all(isinstance(item, str) for item in argv)):
        return None
    if not isinstance(baseline, (str, os.PathLike)):
        return None
    source = str(Path(baseline).resolve())
    target = str(candidate)
    rendered: list[str] = []
    for item in argv:
        if "{candidate_worktree}" in item:
            rendered.append(item.replace("{candidate_worktree}", target))
        elif item == source or item.startswith(source + os.sep):
            rendered.append(target + item[len(source):])
        else:
            rendered.append(item)
    return rendered


def _not_run_gate(gate_id: str, diagnostic: str,
                  candidate_sha: str | None = None) -> dict[str, Any]:
    outcome = {"gate_id": gate_id, "passed": False, "detail": "not_run",
               "diagnostic": diagnostic, "evidence_class": "rederived"}
    if candidate_sha is not None:
        outcome["target_sha"] = candidate_sha
    return outcome


def _gate_expectations(config: dict[str, Any],
                       mission: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable expected-result policy carried by intake."""

    value = config.get("gate_expectations", mission.get(
        "acceptance_gate_expectations", {}))
    return value


def _expected_gate_satisfied(outcome: dict[str, Any], expectation: Any) -> bool:
    """Match an explicit expected result without rewriting the raw outcome."""

    if expectation is None:
        return outcome.get("passed") is True
    return (
        isinstance(expectation, dict)
        and expectation.get("passed") is False
        and type(expectation.get("exit_code")) is int
        and outcome.get("passed") is False
        and outcome.get("exit_code") == expectation.get("exit_code")
    )


def _evaluate(request: dict[str, Any], config: dict[str, Any],
              result: dict[str, Any]) -> dict[str, Any]:
    """Execute the gates this mission declared, in the target repository.

    A gate outcome carried out of the stage-1 result is preferred when the
    runner already produced one; otherwise the declared command is run here.
    Either way every declared gate id gets a real outcome, and a gate with
    neither is `not_run`.
    """

    declared = tuple(_mission(request).get("acceptance_gate_ids") or ())
    mutates_repository = bool(
        config.get("mutates_repository")
        or _mission(request).get("mutates_repository"))
    carried = {} if mutates_repository else {
        item.get("gate_id"): item
        for item in (result.get("gate_outcomes")
                     or result.get("evaluation", {}).get("gate_outcomes") or ())
        if isinstance(item, dict)}
    if not declared:
        return {"passed": False, "gate_outcomes": [],
                "diagnostic": "ACCEPTANCE_GATE_UNDECLARED"}
    commands = config.get("gate_commands") or {}
    workdir = config.get("gate_workdir", config.get("repository", config.get("workdir")))
    timeout = float(config.get("gate_timeout_seconds", 1800))
    expectations = _gate_expectations(config, _mission(request))
    if not isinstance(expectations, dict):
        return {"passed": False, "gate_outcomes": [
                    _not_run_gate(gate, "ACCEPTANCE_GATE_EXPECTATION_INVALID")
                    for gate in declared],
                "diagnostic": "ACCEPTANCE_GATE_EXPECTATION_INVALID"}
    target_sha = _candidate_sha(result) if mutates_repository else None
    workspace = _candidate_workspace(result) if mutates_repository else None
    if workspace is not None and not _workspace_matches_candidate(workspace, target_sha):
        outcomes = [_not_run_gate(gate, "CANDIDATE_WORKSPACE_BINDING_FAILED", target_sha)
                    for gate in declared]
        return {"passed": False, "gate_outcomes": outcomes,
                "diagnostic": "CANDIDATE_WORKSPACE_BINDING_FAILED",
                "target": "candidate", "target_sha": target_sha}
    candidate_context = None
    if mutates_repository:
        if target_sha is None:
            outcomes = [_not_run_gate(gate, "CANDIDATE_SHA_UNAVAILABLE")
                        for gate in declared]
            return {"passed": False, "gate_outcomes": outcomes,
                    "diagnostic": "CANDIDATE_SHA_UNAVAILABLE",
                    "target": "candidate"}
        try:
            candidate_context = _candidate_worktree(
                config.get("repository", workdir), target_sha, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            outcomes = [_not_run_gate(
                gate, "CANDIDATE_CHECKOUT_UNAVAILABLE: %s" % exc, target_sha)
                        for gate in declared]
            return {"passed": False, "gate_outcomes": outcomes,
                    "diagnostic": "CANDIDATE_CHECKOUT_UNAVAILABLE",
                    "target": "candidate", "target_sha": target_sha}
        candidate_temp, candidate_worktree = candidate_context
        try:
            outcomes = []
            for gate in declared:
                command = _render_candidate_command(
                    commands.get(gate), config.get("repository", workdir),
                    candidate_worktree)
                if command is None:
                    outcome = _not_run_gate(
                        gate, "ACCEPTANCE_GATE_COMMAND_UNDECLARED", target_sha)
                    outcome["target"] = "candidate"
                else:
                    outcome = _run_gate(
                        gate, command, candidate_worktree, timeout)
                    outcome["target_sha"] = target_sha
                    outcome["target"] = "candidate"
                outcomes.append(outcome)
        finally:
            _remove_candidate_worktree(
                config.get("repository", workdir), candidate_worktree,
                candidate_temp)
    else:
        outcomes = [carried[gate] if gate in carried
                    else _run_gate(gate, commands.get(gate), workdir, timeout)
                    for gate in declared]
    invalid_expectations = set(expectations) - set(declared)
    if invalid_expectations:
        return {"passed": False, "gate_outcomes": outcomes,
                "diagnostic": "ACCEPTANCE_GATE_EXPECTATION_INVALID: "
                + ", ".join(sorted(invalid_expectations))}
    for gate, expectation in expectations.items():
        if (not isinstance(expectation, dict)
                or expectation.get("passed") is not False
                or type(expectation.get("exit_code")) is not int
                or not 0 <= expectation["exit_code"] <= 255):
            return {"passed": False, "gate_outcomes": outcomes,
                    "diagnostic": "ACCEPTANCE_GATE_EXPECTATION_INVALID: " + gate}
    for outcome in outcomes:
        expectation = expectations.get(outcome.get("gate_id"))
        if expectation is not None:
            outcome["expected_failure"] = True
            outcome["satisfied"] = _expected_gate_satisfied(outcome, expectation)
    passed = all(_expected_gate_satisfied(
        outcome, expectations.get(outcome.get("gate_id")))
                  for outcome in outcomes)
    answer = {"passed": passed, "gate_outcomes": outcomes,
              "diagnostic": None if passed else "ACCEPTANCE_GATE_FAILED"}
    if mutates_repository:
        answer.update({"target": "candidate", "target_sha": target_sha,
                       "changed_paths": _changed_paths(
                           config.get("repository", workdir),
                           _mission(request).get("baseline_sha"),
                           target_sha, timeout)})
    return answer


def execute(request: dict[str, Any]) -> dict[str, Any]:
    step = request["step"]
    if step in {"context", "context-recovery"}:
        return _context(request)
    config = _stage1_config(request)
    if step == "dispatch":
        return _dispatch(request, config)
    if step == "dispatch-reconcile":
        return _reconcile(request, config)
    dispatch = request["input"]["dispatch"]
    result = dispatch.get("stage1_result", {})
    if step == "verify":
        verification = result.get("candidate_commit_verification", {})
        verified = verification.get("verified") is True
        return {"verified": verified, "verification": verification, "diagnostic": None if verified else result.get("refusal_code", "CANDIDATE_VERIFICATION_FAILED")}
    if step == "evaluate":
        # ``candidate_sha`` is normally also present inside the Evidence Core
        # envelope.  Preserve the Controller dispatch projection here so a
        # candidate identity cannot disappear between verification and gate
        # evaluation when an adapter supplies only the top-level field.
        if dispatch.get("candidate_sha") and "candidate_sha" not in result:
            result = {**result, "candidate_sha": dispatch["candidate_sha"]}
        if (dispatch.get("candidate_workspace") is not None
                and "candidate_workspace" not in result):
            result = {**result, "candidate_workspace": dispatch["candidate_workspace"]}
        return _evaluate(request, config, result)
    if step == "evidence":
        mission = _mission(request)
        if (config.get("mutates_repository")
                or mission.get("mutates_repository")):
            expected = dispatch.get("candidate_sha") or _candidate_sha(result)
            workspace = (dispatch.get("candidate_workspace")
                         if dispatch.get("candidate_workspace") is not None
                         else _candidate_workspace(result))
            evaluation = request["input"].get("evaluation") or {}
            outcomes = evaluation.get("gate_outcomes") or ()
            target_shas = {item.get("target_sha") for item in outcomes
                           if isinstance(item, dict)}
            if (not expected or evaluation.get("target") != "candidate"
                    or evaluation.get("target_sha") != expected
                    or target_shas != {expected}
                    or any(item.get("target") != "candidate"
                           for item in outcomes if isinstance(item, dict))):
                return {"accepted": False, "retryable": False,
                        "evidence_pointer": None,
                        "diagnostic": "CANDIDATE_EVALUATION_BINDING_FAILED"}
            if workspace is not None and not _workspace_matches_candidate(workspace, expected):
                return {"accepted": False, "retryable": False,
                        "evidence_pointer": None,
                        "diagnostic": "CANDIDATE_WORKSPACE_BINDING_FAILED"}
        evidence = result.get("evidence_result", {})
        accepted = evidence.get("status") == "complete"
        return {"accepted": accepted, "retryable": False, "evidence_pointer": evidence.get("artifact_hash"), "evidence": evidence, "diagnostic": None if accepted else result.get("refusal_code", "EVIDENCE_REJECTED")}
    raise ValueError(f"unknown step: {step}")


def main() -> int:
    request = json.load(sys.stdin)
    if not isinstance(_mission(request).get("stage1"), dict):
        # A mission that declares no live execution configuration is not a
        # live mission, and answering it here would put this seam's real
        # provider path behind a mission that never asked for it.  The local
        # fixture provider owns that case and refuses a real mission itself.
        return safe_provider.main_with(request)
    try:
        response = execute(request)
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        response = {"status": "retryable_error", "diagnostic": f"STAGE1_ADAPTER_ERROR: {exc}"}
    json.dump(response, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
