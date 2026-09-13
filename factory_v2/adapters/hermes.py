from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import EngineeringResult, MissionContext, WorkItem


class NousHermesAdapter:
    """Thin EngineeringManager over official Nous Hermes (`hermes chat --oneshot`).

    Does not reimplement session/memory/subagents/skills. Official CLI only.
    Hermes receives the approved PCP + durable mission context, then must
    delegate coding through the EngineeringExecutor (Grok Build) boundary.
    """

    name = "Nous Hermes Agent"
    harness_mode = "real"

    def __init__(
        self,
        executor: EngineeringExecutor,
        *,
        env: dict[str, str] | None = None,
        binary: str = "hermes",
    ):
        self.executor = executor
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return EngineeringResult(
                blocked=True,
                reason="hermes runtime unavailable",
                harness_mode="real",
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=False,
            )
        query = {
            "role": "Engineering Manager",
            "mission_id": ctx.mission_id,
            "pcp_hash": ctx.pcp_hash,
            "pcp": ctx.pcp,
            "defects": list(ctx.defects),
            "workspace": ctx.workspace_path,
            "instruction": (
                "Interpret the approved PCP. Plan bounded coding work. "
                "Do not approve PCP, waive verification, approve an RC, "
                "or promote Production. Return JSON "
                '{"objective": "..."} only.'
            ),
        }
        query_file = Path(ctx.workspace_path) / "hermes-query.json"
        query_file.write_text(json.dumps(query, indent=2), encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    binary,
                    "chat",
                    "--oneshot",
                    "-Q",
                    "--source",
                    "tool",
                    "--in",
                    ctx.workspace_path,
                    "--query-file",
                    str(query_file),
                    "--pass-session-id",
                ],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=3600,
                check=False,
            )
        except OSError as exc:
            return EngineeringResult(
                blocked=True,
                reason=f"hermes launch failed: {exc}",
                harness_mode="real",
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=False,
            )
        if proc.returncode != 0:
            return EngineeringResult(
                blocked=True,
                reason=f"hermes failed: {proc.stderr[-500:]}",
                harness_mode="real",
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=False,
            )
        objective = _objective(proc.stdout, ctx)
        work = WorkItem(objective=objective, defects=ctx.defects)
        executed = self.executor.implement(ctx, work)
        if executed.blocked or not executed.artifact_id:
            return EngineeringResult(
                blocked=True,
                reason=executed.reason or "executor blocked",
                harness_mode=executed.harness_mode,
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=True,
            )
        return EngineeringResult(
            candidate_artifact_id=executed.artifact_id,
            harness_mode=self.harness_mode,
            manager_name=self.name,
            executor_name=self.executor.name,
            executor_called=True,
        )


def _objective(stdout: str, ctx: MissionContext) -> str:
    text = stdout.strip()
    if text:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and data.get("objective"):
                return str(data["objective"])
        except json.JSONDecodeError:
            start = text.rfind("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    if isinstance(data, dict) and data.get("objective"):
                        return str(data["objective"])
                except json.JSONDecodeError:
                    pass
    return ctx.pcp.get("intent") or ctx.pcp.get("title") or "implement admitted PCP"
