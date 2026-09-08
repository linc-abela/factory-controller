"""One manager judgment through a frozen Bridge fleet profile.

Not part of the factory_controller package scan. Controller owns the
operation; frozen factory-bridge supplies containment and profile identity.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

FROZEN_BRIDGE_DEPENDENCY_SHA = "d4fb19bdaa153bd220f346be657657c2749cfed8"
BLOCKED = "HERMES_MANAGER_PROVIDER_ADAPTER_BLOCKED"
GIT_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _fail(detail: str, extra: dict | None = None) -> int:
    body = {"error": BLOCKED, "detail": detail}
    if extra:
        body.update(extra)
    print(json.dumps(body, sort_keys=True), file=sys.stderr)
    print(json.dumps(body, sort_keys=True))
    return 2


def _bridge_sha(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%H"],
        capture_output=True, text=True, check=False, env=GIT_ENV)
    if completed.returncode != 0:
        raise RuntimeError("bridge root is not a git checkout")
    return completed.stdout.strip()


def _prompt(snapshot: dict, operation_id: str) -> str:
    return (
        "You are the Factory engineering manager. Return only JSON with keys "
        "reasoning (string) and proposals (array). Do not approve production, "
        "widen budgets, invent gates, or execute repository changes. "
        "operation_id=%s\n%s" % (operation_id, json.dumps(snapshot, default=str)[:8000])
    )


def run_turn(bridge_root: Path, profile_id: str, snapshot: dict,
             operation_id: str, timeout: int,
             expected_sha: str = FROZEN_BRIDGE_DEPENDENCY_SHA) -> dict:
    sha = _bridge_sha(bridge_root)
    if sha != expected_sha:
        raise RuntimeError("bridge SHA %s is not frozen dependency %s" % (sha, expected_sha))
    src = bridge_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from factory_bridge import containment, provider
    except ImportError as exc:
        raise RuntimeError("frozen Bridge package is not importable: %s" % exc) from exc
    if not containment.available():
        raise RuntimeError("sandbox-exec is not present on this host")
    registry = provider.load_registry(str(bridge_root / "providers.json"))
    profile = next((item for item in registry.profiles if item.profile_id == profile_id), None)
    if profile is None:
        raise RuntimeError("fleet profile %s is absent from the frozen registry" % profile_id)
    resolved = provider.resolve_executable(profile)
    if resolved is None:
        raise RuntimeError("fleet executable is not present")
    prompt = _prompt(snapshot, operation_id)
    work = Path(tempfile.mkdtemp(prefix="sf-hermes-fleet-work-"))
    run_dir = Path(tempfile.mkdtemp(prefix="sf-hermes-fleet-run-"))
    (work / "MISSION.md").write_text(prompt + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=work, check=True, env=GIT_ENV)
    argv = [
        resolved, "exec", "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral", "--model", profile.model, "-c",
        'model_reasoning_effort="%s"' % profile.effort, "-C", str(work), prompt,
    ]
    run = SimpleNamespace(run_id=operation_id, run_dir=str(run_dir), worktree=str(work))
    # Standalone manager binaries may live under the fleet family's granted
    # home state dir. extra_reads of that path intersects protected roots.
    profile_text = containment.profile(
        worktree=str(work), run_dir=str(run_dir),
        git_common_dir=str(work / ".git"),
        network_access=True, extra_reads=(),
        runtime_family=profile.harness)
    wrapped = containment.sandbox_argv(argv, profile_text)
    env = provider.safe_environment(profile, run)
    code, stdout, stderr = provider.run_process(
        wrapped, cwd=str(work), env=env,
        timeout=min(int(profile.timeout_seconds), timeout),
        profile_text=profile_text)
    return {
        "returncode": code,
        "stdout": stdout,
        "stderr": stderr[:4000],
        "bridge_sha": sha,
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "harness": profile.harness,
        "model": profile.model,
        "effort": profile.effort,
        "observed_executable": resolved,
        "operation_id": operation_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-root", required=True)
    parser.add_argument("--profile", default="codex-luna-max")
    parser.add_argument("--snapshot-file", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--expected-sha", default=FROZEN_BRIDGE_DEPENDENCY_SHA)
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(Path(args.snapshot_file).read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            return _fail("snapshot must be a JSON object")
        result = run_turn(
            Path(args.bridge_root), args.profile, snapshot,
            args.operation_id, args.timeout, expected_sha=args.expected_sha)
    except Exception as exc:  # noqa: BLE001 — fail closed to a typed blocker
        return _fail(str(exc)[:500])
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("returncode") == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
