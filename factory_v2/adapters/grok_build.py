from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.models import EngineeringResult, ExecutorResult, MissionContext, WorkItem


class GrokBuildAdapter:
    """Thin EngineeringExecutor over official Grok Build (`grok -p` headless).

    Auth: `XAI_API_KEY` / `GROK_DEPLOYMENT_KEY`, or `~/.grok/auth.json`.
    Missing credentials fail closed. This adapter never fabricates success.
    """

    name = "Grok Build"
    harness_mode = "real"

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
            "Return a single JSON object {\"artifact_id\": \"...\"} as the final reply."
        )
        try:
            proc = subprocess.run(
                [
                    binary,
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
        artifact_id = _parse_artifact(proc.stdout)
        if not artifact_id:
            return ExecutorResult(
                blocked=True,
                reason="grok returned no artifact_id",
                harness_mode="real",
                simulated=False,
            )
        return ExecutorResult(
            artifact_id=artifact_id,
            harness_mode="real",
            simulated=False,
        )


def _parse_artifact(stdout: str) -> str | None:
    import json

    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        value = data.get("artifact_id") or data.get("result", {}).get("artifact_id")
        if isinstance(value, str) and value:
            return value
    return None
