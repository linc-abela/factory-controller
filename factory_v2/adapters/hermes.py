from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.canonical import PROFILE_ID
from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import CandidateIdentity, EngineeringResult, MissionContext, WorkItem


class NousHermesAdapter:
    """Thin EngineeringManager over official Nous Hermes.

    Hermes, not Controller Python, coordinates the selected
    EngineeringExecutor. When an executor adapter is supplied, Hermes
    decomposes the mission and this adapter invokes executor.implement().
    Controller never calls the executor.
    """

    name = "Nous Hermes Agent"
    harness_mode = "real"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        binary: str = "hermes",
        executor: EngineeringExecutor | None = None,
    ):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary
        self.executor = executor

    def _executor_name(self) -> str:
        if self.executor is not None:
            return self.executor.name
        return "Grok Build"

    def _blocked(self, reason: str, *, called: bool = False) -> EngineeringResult:
        return EngineeringResult(
            blocked=True,
            reason=reason,
            harness_mode="real",
            manager_name=self.name,
            executor_name=self._executor_name(),
            executor_called=called,
        )

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return self._blocked("hermes runtime unavailable")
        workspace = Path(ctx.workspace_path)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "pcp-handoff.json").write_text(
            json.dumps(ctx.pcp, indent=2), encoding="utf-8"
        )
        request = {
            "role": "Engineering Manager",
            "profile_id": PROFILE_ID,
            "mission_id": ctx.mission_id,
            "lineage_id": ctx.lineage_id,
            "pcp_hash": ctx.pcp_hash,
            "attempt_number": ctx.attempt_number,
            "rework_sequence": ctx.rework_sequence,
            "defects": list(ctx.defects),
            "workspace": ctx.workspace_path,
            "instruction": _instruction(self._executor_name()),
        }
        query_file = workspace / "hermes-query.json"
        query_file.write_text(json.dumps(request, indent=2), encoding="utf-8")
        result_file = workspace / "hermes-result.json"
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
            return self._blocked(f"hermes launch failed: {exc}")
        if proc.returncode != 0:
            return self._blocked(f"hermes failed: {proc.stderr[-500:]}")
        payload = _result(result_file, proc.stdout, require_candidate=self.executor is None)
        if payload is None:
            return self._blocked("hermes returned no structured candidate result")
        session = str(payload.get("hermes_session_id") or f"hermes-{ctx.mission_id}")
        if self.executor is not None:
            work = _work_item(ctx, payload)
            executed = self.executor.implement(ctx, work)
            if executed.blocked or executed.candidate is None:
                return self._blocked(
                    executed.reason or "executor blocked",
                    called=True,
                )
            return EngineeringResult(
                candidate=executed.candidate,
                hermes_session_id=session,
                grok_session_ref=executed.grok_session_ref,
                harness_mode="real",
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=True,
                engineering_tests=executed.engineering_tests,
            )
        if payload is None:
            return self._blocked("hermes returned no structured candidate result")
        try:
            candidate = CandidateIdentity(
                candidate_id=payload["candidate"]["candidate_id"],
                source_revision=payload["candidate"]["source_revision"],
                artifact_hash=payload["candidate"]["artifact_hash"],
                artifact_uri=payload["candidate"]["artifact_uri"],
            )
        except (KeyError, TypeError) as exc:
            return self._blocked(f"hermes candidate tuple missing: {exc}")
        return EngineeringResult(
            candidate=candidate,
            hermes_session_id=session,
            grok_session_ref=str(payload.get("grok_session_ref") or ""),
            harness_mode="real",
            manager_name=self.name,
            executor_name=self._executor_name(),
            executor_called=True,
        )


def _instruction(executor_name: str) -> str:
    return (
        "Use the factory-engineering skill. Coordinate the selected "
        f"EngineeringExecutor ({executor_name}) inside this sandbox. "
        "Decompose the admitted PCP into a bounded coding objective. "
        "Write hermes-result.json with hermes_session_id and work.objective. "
        "Do not approve PCP, waive verification, approve an RC, or promote "
        "Production. Do not implement the candidate yourself."
    )


def _work_item(ctx: MissionContext, payload: dict | None) -> WorkItem:
    work = (payload or {}).get("work") if isinstance(payload, dict) else None
    if isinstance(work, dict) and isinstance(work.get("objective"), str) and work["objective"]:
        defects = work.get("defects")
        extra = tuple(defects) if isinstance(defects, list) else ctx.defects
        return WorkItem(objective=work["objective"], defects=extra)
    return WorkItem(
        objective=ctx.pcp.get("product", {}).get("objective") or "implement admitted PCP",
        defects=ctx.defects,
    )


def _result(path: Path, stdout: str, *, require_candidate: bool = True) -> dict | None:
    data = _load_json(path, stdout)
    if data is None:
        return None
    if require_candidate and not isinstance(data.get("candidate"), dict):
        return None
    return data


def _load_json(path: Path, stdout: str) -> dict | None:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
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
    return data if isinstance(data, dict) else None
