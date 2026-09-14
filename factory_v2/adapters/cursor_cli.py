from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from factory_v2.canonical import revision_for
from factory_v2.models import CandidateIdentity, ExecutorResult, MissionContext, WorkItem

_CANDIDATE_KEYS = ("candidate_id", "source_revision", "artifact_hash", "artifact_uri")
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


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
            "--force",
            "--sandbox",
            "enabled",
            "--model",
            self.requested_model,
        ]
        no_progress_timeout = float(
            self._env.get("FACTORY_V2_EXECUTOR_NO_PROGRESS_TIMEOUT", "180.0")
        )
        poll_interval = float(
            self._env.get("FACTORY_V2_EXECUTOR_POLL_INTERVAL", "0.5")
        )
        heartbeat_file = workspace / "cursor-executor-heartbeat.json"

        def _write_heartbeat(status: str, idle_s: float, elapsed_s: float, changes: int) -> None:
            hb = {
                "mission_id": ctx.mission_id,
                "work_item": work.as_dict(),
                "status": status,
                "idle_seconds": round(idle_s, 2),
                "elapsed_seconds": round(elapsed_s, 2),
                "files_fingerprinted": changes,
                "timestamp": str(time.time()),
            }
            try:
                heartbeat_file.write_text(json.dumps(hb, indent=2), encoding="utf-8")
            except OSError:
                pass

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
        except OSError as exc:
            return self._blocked(f"cursor cli launch failed: {exc}")

        start_time = time.time()
        last_activity_time = start_time
        last_fp = before
        timed_out = False

        while True:
            ret = proc.poll()
            if ret is not None:
                break

            now = time.time()
            elapsed = now - start_time
            curr_fp = _workspace_fingerprint(workspace)

            if curr_fp != last_fp:
                last_activity_time = now
                last_fp = curr_fp
                _write_heartbeat("active", 0.0, elapsed, len(curr_fp))
            else:
                idle = now - last_activity_time
                _write_heartbeat("running", idle, elapsed, len(curr_fp))
                if idle >= no_progress_timeout:
                    timed_out = True
                    break

            time.sleep(poll_interval)

        if timed_out:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass

            idle_duration = int(time.time() - last_activity_time)
            _write_heartbeat("stalled", idle_duration, time.time() - start_time, len(last_fp))

            stalled_provenance = {
                "executor_type": self.executor_type,
                "status": "stalled",
                "reason": f"no progress detected after {idle_duration}s",
                "work_item": work.as_dict(),
                "mission_id": ctx.mission_id,
                "resumable": True,
            }
            _write_provenance(workspace, stalled_provenance)
            return self._blocked(
                f"executor stalled: no file or progress activity in {idle_duration}s"
            )

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            return self._blocked(f"cursor cli failed: {stderr[-500:]}")
        envelope = _parse_envelope(stdout)
        if envelope is not None and envelope.get("is_error") is True:
            return self._blocked("cursor cli reported an error result")
        parsed = _parse_candidate(stdout)
        if parsed is None:
            after = _workspace_fingerprint(workspace)
            parsed = _fallback_identity(workspace, before, after, envelope)
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
            "observed_model": _observed_model(stdout) or self.requested_model,
            "session_ref": session_ref,
            "simulated": False,
        }
        _write_provenance(workspace, self.last_provenance)
        _write_heartbeat("completed", 0.0, time.time() - start_time, len(_workspace_fingerprint(workspace)))
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
    if path.name in {"cursor-executor-provenance.json", "cursor-executor-heartbeat.json"}:
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


def _invocation_local_fingerprint(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    return {
        name: digest
        for name, digest in after.items()
        if before.get(name) != digest
    }


def _fallback_identity(
    workspace: Path,
    before: dict[str, str],
    after: dict[str, str],
    envelope: dict | None,
) -> CandidateIdentity | None:
    if not after:
        return None
    if _invocation_local_fingerprint(before, after):
        return _identity_from_workspace(workspace, after)
    if envelope is not None and envelope.get("is_error") is not True:
        return _identity_from_workspace(workspace, after)
    return None


def _candidate_from_mapping(data: dict) -> CandidateIdentity | None:
    cand = data.get("candidate") if isinstance(data.get("candidate"), dict) else data
    if not isinstance(cand, dict):
        return None
    if all(isinstance(cand.get(key), str) and cand.get(key).strip() for key in _CANDIDATE_KEYS):
        return CandidateIdentity(
            candidate_id=cand["candidate_id"],
            source_revision=cand["source_revision"],
            artifact_hash=cand["artifact_hash"],
            artifact_uri=cand["artifact_uri"],
        )
    return None


def _objects_from_text(text: str):
    stripped = text.strip()
    if not stripped:
        return
    loaded = _load_json_object(stripped)
    if loaded is not None:
        yield loaded
    for match in _JSON_FENCE.finditer(stripped):
        fenced = _load_json_object(match.group(1))
        if fenced is not None:
            yield fenced


def _candidate_from_fields(text: str) -> CandidateIdentity | None:
    values: dict[str, str] = {}
    for key in _CANDIDATE_KEYS:
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
        if match is None:
            return None
        values[key] = match.group(1)
    return CandidateIdentity(
        candidate_id=values["candidate_id"],
        source_revision=values["source_revision"],
        artifact_hash=values["artifact_hash"],
        artifact_uri=values["artifact_uri"],
    )


def _candidate_from_text(text: str) -> CandidateIdentity | None:
    for obj in _objects_from_text(text):
        found = _candidate_from_mapping(obj)
        if found is not None:
            return found
    return _candidate_from_fields(text)


def _parse_candidate(stdout: str) -> CandidateIdentity | None:
    data = _load_json_object(stdout)
    if data is None:
        return _candidate_from_text(stdout)
    found = _candidate_from_mapping(data)
    if found is not None:
        return found
    inner = data.get("result")
    if isinstance(inner, dict):
        found = _candidate_from_mapping(inner)
        if found is not None:
            return found
        nested = inner.get("result")
        if isinstance(nested, str):
            return _candidate_from_text(nested)
    if isinstance(inner, str):
        return _candidate_from_text(inner)
    return None


def _write_provenance(workspace: Path, payload: dict[str, str | bool]) -> None:
    path = workspace / "cursor-executor-provenance.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
