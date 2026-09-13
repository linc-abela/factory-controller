from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.canonical import PROFILE_ID
from factory_v2.models import CandidateIdentity, EngineeringResult, MissionContext


class NousHermesAdapter:
    """Thin EngineeringManager over official Nous Hermes.

    Hermes, not Controller Python, coordinates Grok Build through the
    factory-engineering profile / official grok skill. This adapter only
    launches Hermes in the admitted sandbox and validates the structured
    result file. It never calls Grok itself.
    """

    name = "Nous Hermes Agent"
    harness_mode = "real"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        binary: str = "hermes",
    ):
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
                executor_name="Grok Build",
                executor_called=False,
            )
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
            "instruction": (
                "Use the factory-engineering skill. Coordinate Grok Build "
                "inside this sandbox via the official grok skill / terminal. "
                "Write hermes-result.json with hermes_session_id, "
                "grok_session_ref, and candidate tuple. Do not approve PCP, "
                "waive verification, approve an RC, or promote Production."
            ),
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
            return EngineeringResult(
                blocked=True,
                reason=f"hermes launch failed: {exc}",
                harness_mode="real",
                manager_name=self.name,
                executor_name="Grok Build",
                executor_called=False,
            )
        if proc.returncode != 0:
            return EngineeringResult(
                blocked=True,
                reason=f"hermes failed: {proc.stderr[-500:]}",
                harness_mode="real",
                manager_name=self.name,
                executor_name="Grok Build",
                executor_called=False,
            )
        payload = _result(result_file, proc.stdout)
        if payload is None:
            return EngineeringResult(
                blocked=True,
                reason="hermes returned no structured candidate result",
                harness_mode="real",
                manager_name=self.name,
                executor_name="Grok Build",
                executor_called=False,
            )
        try:
            candidate = CandidateIdentity(
                candidate_id=payload["candidate"]["candidate_id"],
                source_revision=payload["candidate"]["source_revision"],
                artifact_hash=payload["candidate"]["artifact_hash"],
                artifact_uri=payload["candidate"]["artifact_uri"],
            )
        except (KeyError, TypeError) as exc:
            return EngineeringResult(
                blocked=True,
                reason=f"hermes candidate tuple missing: {exc}",
                harness_mode="real",
                manager_name=self.name,
                executor_name="Grok Build",
                executor_called=False,
            )
        return EngineeringResult(
            candidate=candidate,
            hermes_session_id=str(payload.get("hermes_session_id") or f"hermes-{ctx.mission_id}"),
            grok_session_ref=str(payload.get("grok_session_ref") or ""),
            harness_mode="real",
            manager_name=self.name,
            executor_name="Grok Build",
            executor_called=True,
        )


def _result(path: Path, stdout: str) -> dict | None:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("candidate"), dict):
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
    if isinstance(data, dict) and isinstance(data.get("candidate"), dict):
        return data
    return None
