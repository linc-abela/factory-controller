from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.canonical import revision_for
from factory_v2.models import CandidateIdentity, ExecutorResult, MissionContext, WorkItem


class CursorCLIExecutor:
    """Thin EngineeringExecutor over the official Cursor Agent CLI.

    Temporary Factory v2 runtime override. Grok Build stays selectable.
    Controller never invokes this adapter; Hermes (or a labeled simulated
    Hermes campaign) does. Missing CLI, missing auth, unbound workspace,
    nonzero exit, or a missing candidate tuple fail closed.
    """

    name = "Cursor CLI"
    harness_mode = "real"
    executor_type = "cursor_cli"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        binary: str = "agent",
        model: str | None = None,
        home: str | Path | None = None,
    ):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary
        self._home = Path(home) if home is not None else Path.home()
        requested = model if model is not None else self._env.get("FACTORY_V2_CURSOR_MODEL")
        self.requested_model = (requested or "auto").strip() or "auto"
        self.last_provenance: dict[str, str | bool] = {}

    def credentials_available(self) -> bool:
        if self._env.get("CURSOR_API_KEY") or self._env.get("CURSOR_AUTH_TOKEN"):
            return True
        return self._cli_session_authenticated()

    def auth_mode(self) -> str:
        if self._env.get("CURSOR_API_KEY"):
            return "api_key_env"
        if self._env.get("CURSOR_AUTH_TOKEN"):
            return "auth_token_env"
        if self._cli_session_authenticated():
            return "cli_session"
        return "none"

    def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
        workspace = _bound_workspace(ctx.workspace_path)
        if workspace is None:
            return self._blocked("cursor cli workspace unbound")
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return self._blocked("cursor cli unavailable")
        version = _cli_version(binary, self._env)
        if not self.credentials_available():
            return self._blocked("cursor cli unauthenticated")
        before = _workspace_fingerprint(workspace)
        prompt = (
            "Factory EngineeringExecutor work. Implement the admitted PCP in the "
            f"bounded workspace {workspace}. Mission {ctx.mission_id}. "
            f"Objective: {work.objective}. Defects: {list(work.defects)}. "
            "Do not leave the workspace. Return JSON with candidate_id, "
            "source_revision, artifact_hash, artifact_uri."
        )
        command = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--workspace",
            str(workspace),
            "--trust",
            "--sandbox",
            "enabled",
            "--model",
            self.requested_model,
        ]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=3600,
                check=False,
            )
        except OSError as exc:
            return self._blocked(f"cursor cli launch failed: {exc}")
        if proc.returncode != 0:
            return self._blocked(f"cursor cli failed: {proc.stderr[-500:]}")
        envelope = _parse_envelope(proc.stdout)
        if envelope is not None and envelope.get("is_error") is True:
            return self._blocked("cursor cli reported an error result")
        parsed = _parse_candidate(proc.stdout)
        if parsed is None:
            after = _workspace_fingerprint(workspace)
            if after == before:
                return self._blocked("cursor cli returned no structured candidate result")
            parsed = _identity_from_workspace(workspace, after)
        if parsed is None:
            return self._blocked("cursor cli returned no structured candidate result")
        session_ref = ""
        if envelope and isinstance(envelope.get("session_id"), str):
            session_ref = envelope["session_id"]
        session_ref = session_ref or ctx.mission_id
        self.last_provenance = {
            "executor_type": self.executor_type,
            "cli_path": binary,
            "cli_version": version,
            "auth_mode": self.auth_mode(),
            "requested_model": self.requested_model,
            "observed_model": _observed_model(proc.stdout) or self.requested_model,
            "session_ref": session_ref,
            "simulated": False,
        }
        _write_provenance(workspace, self.last_provenance)
        return ExecutorResult(
            candidate=parsed,
            grok_session_ref=session_ref,
            harness_mode="real",
            simulated=False,
        )

    def _cli_session_authenticated(self) -> bool:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return False
        try:
            proc = subprocess.run(
                [binary, "status"],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=30,
                check=False,
            )
        except OSError:
            return False
        text = f"{proc.stdout}\n{proc.stderr}".lower()
        if proc.returncode != 0:
            return False
        return "not logged in" not in text and "authentication required" not in text

    def _blocked(self, reason: str) -> ExecutorResult:
        return ExecutorResult(
            blocked=True,
            reason=reason,
            harness_mode="real",
            simulated=False,
        )


def _bound_workspace(path: str) -> Path | None:
    if not path or not str(path).strip():
        return None
    workspace = Path(path).resolve()
    if not workspace.is_dir():
        return None
    if workspace in {Path("/"), Path.home().resolve()}:
        return None
    return workspace


def _cli_version(binary: str, env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
    except OSError:
        return "unknown"
    return (proc.stdout or proc.stderr).strip() or "unknown"


def _observed_model(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("model", "observed_model", "model_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _load_json_object(stdout: str) -> dict | None:
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


def _parse_envelope(stdout: str) -> dict | None:
    data = _load_json_object(stdout)
    if data is None or data.get("type") != "result":
        return None
    return data


def _workspace_fingerprint(workspace: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or not _countable_workspace_file(path):
            continue
        out[str(path.relative_to(workspace))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _countable_workspace_file(path: Path) -> bool:
    if path.name == "cursor-executor-provenance.json":
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return "__pycache__" not in path.parts


def _identity_from_workspace(workspace: Path, fingerprint: dict[str, str]) -> CandidateIdentity | None:
    if not fingerprint:
        return None
    material = "\n".join(f"{name}:{digest}" for name, digest in sorted(fingerprint.items()))
    digest = hashlib.sha256(material.encode()).hexdigest()
    first = sorted(fingerprint)[0]
    return CandidateIdentity(
        candidate_id=f"cursor-{digest[:12]}",
        source_revision=revision_for(material),
        artifact_hash=f"sha256:{digest}",
        artifact_uri=f"sandbox://{workspace.name}/{first}",
    )


def _parse_candidate(stdout: str) -> CandidateIdentity | None:
    data = _load_json_object(stdout)
    if data is None:
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


def _write_provenance(workspace: Path, payload: dict[str, str | bool]) -> None:
    path = workspace / "cursor-executor-provenance.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
