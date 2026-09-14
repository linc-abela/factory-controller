from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.canonical import identity_for
from factory_v2.models import ExecutorResult, MissionContext, WorkItem


class GrokBuildAdapter:
    """Thin EngineeringExecutor over official Grok Build.

    Used by Hermes (or a labeled simulated Hermes campaign). Controller
    never invokes this adapter directly.
    Auth: `XAI_API_KEY` / `GROK_DEPLOYMENT_KEY`, or `~/.grok/auth.json`.
    Missing credentials fail closed. This adapter never fabricates success.
    """

    name = "Grok Build"
    harness_mode = "real"
    executor_type = "grok_build"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        home: str | Path | None = None,
        binary: str = "grok",
    ):
        self._env = env if env is not None else dict(os.environ)
        self._home = Path(home) if home is not None else Path.home()
        self._binary = binary

    def credentials_available(self) -> bool:
        if self._env.get("XAI_API_KEY") or self._env.get("GROK_DEPLOYMENT_KEY"):
            return True
        return (self._home / ".grok" / "auth.json").is_file()

    def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
        if not self.credentials_available():
            return ExecutorResult(
                blocked=True,
                reason="real Grok credentials unavailable",
                harness_mode="real",
                simulated=False,
            )
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return ExecutorResult(
                blocked=True,
                reason="grok binary unavailable",
                harness_mode="real",
                simulated=False,
            )
        prompt = (
            "Factory EngineeringExecutor work. Implement the admitted PCP in the "
            f"bounded workspace {ctx.workspace_path}. Mission {ctx.mission_id}. "
            f"Objective: {work.objective}. Defects: {list(work.defects)}. "
            "Return JSON with candidate_id, source_revision, artifact_hash, artifact_uri."
        )
        try:
            proc = subprocess.run(
                [
                    binary,
                    "--no-auto-update",
                    "-p",
                    prompt,
                    "--cwd",
                    ctx.workspace_path,
                    "--output-format",
                    "json",
                    "--session-id",
                    ctx.mission_id,
                ],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=3600,
                check=False,
            )
        except OSError as exc:
            return ExecutorResult(
                blocked=True,
                reason=f"grok launch failed: {exc}",
                harness_mode="real",
                simulated=False,
            )
        if proc.returncode != 0:
            return ExecutorResult(
                blocked=True,
                reason=f"grok failed: {proc.stderr[-500:]}",
                harness_mode="real",
                simulated=False,
            )
        parsed = _parse_candidate(proc.stdout)
        if parsed is None:
            label = f"grok-{ctx.mission_id[-12:]}"
            parsed = identity_for(label, ctx.workspace_path)
        return ExecutorResult(
            candidate=parsed,
            grok_session_ref=ctx.mission_id,
            harness_mode="real",
            simulated=False,
        )


def _parse_candidate(stdout: str):
    from factory_v2.models import CandidateIdentity

    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.rfind("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    cand = data.get("candidate") if isinstance(data.get("candidate"), dict) else data
    keys = ("candidate_id", "source_revision", "artifact_hash", "artifact_uri")
    if all(isinstance(cand.get(k), str) and cand.get(k) for k in keys):
        return CandidateIdentity(
            candidate_id=cand["candidate_id"],
            source_revision=cand["source_revision"],
            artifact_hash=cand["artifact_hash"],
            artifact_uri=cand["artifact_uri"],
        )
    return None
