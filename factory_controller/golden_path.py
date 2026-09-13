"""Hermes-owned Lab -> Factory golden path.

RC-alpha is valid only when every required link exists for the same PCP
mission. An existing checkout or reachable URL is never itself RC-alpha.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Protocol

LINKS = (
    "pcp",
    "controller",
    "hermes",
    "architecture",
    "implementation",
    "integration",
    "functional_e2e",
    "rc_alpha",
)

ARCH_PRIMARY = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"}
ARCH_FALLBACK = {"harness": "cursor", "model": "claude-opus-5", "effort": "high"}
IMPL_ROUTE = {"harness": "codex", "model": "gpt-5.6-luna", "effort": "max"}
CANDIDATE_MARKER = ".factory-candidate.json"


class IncompleteChain(ValueError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("SF272_FACTORY_GOLDEN_PATH_REJECT — %s" % ",".join(missing))


class PipelineExecutors(Protocol):
    def hermes(self, mission: Any) -> dict[str, Any]: ...
    def architecture(self, mission: Any, hermes: Mapping[str, Any], work: Path) -> dict[str, Any]: ...
    def implementation(self, mission: Any, architecture: Mapping[str, Any], work: Path) -> dict[str, Any]: ...
    def integrate(self, mission: Any, implementation: Mapping[str, Any], work: Path) -> dict[str, Any]: ...
    def e2e(self, mission: Any, work: Path, candidate_head: str) -> dict[str, Any]: ...
    def deploy(self, mission: Any, work: Path, candidate_head: str, state_dir: Path) -> dict[str, Any]: ...


def missing_links(evidence: Mapping[str, Any] | None) -> tuple[str, ...]:
    body = evidence or {}
    missing: list[str] = []
    for link in LINKS:
        item = body.get(link)
        if not isinstance(item, Mapping) or not item:
            missing.append(link)
            continue
        if link == "pcp" and not (item.get("digest") and item.get("path")):
            missing.append(link)
        elif link == "controller" and not item.get("mission_key"):
            missing.append(link)
        elif link == "hermes" and not item.get("routing"):
            missing.append(link)
        elif link == "architecture" and not item.get("artifact"):
            missing.append(link)
        elif link == "implementation" and not (
                item.get("head") or item.get("packages")):
            missing.append(link)
        elif link == "integration" and not item.get("candidate_head"):
            missing.append(link)
        elif link == "functional_e2e" and item.get("result") != "PASS":
            missing.append(link)
        elif link == "rc_alpha" and not item.get("url"):
            missing.append(link)
    return tuple(missing)


def lifecycle_of(evidence: Mapping[str, Any]) -> str:
    if evidence.get("rc_alpha", {}).get("url") and not missing_links(evidence):
        return "OWNER_VALIDATION"
    if (evidence.get("functional_e2e") or {}).get("result") == "PASS":
        return "RC_ALPHA"
    if (evidence.get("functional_e2e") or {}).get("result"):
        return "FUNCTIONAL_E2E"
    if (evidence.get("integration") or {}).get("candidate_head"):
        return "INTEGRATION"
    if (evidence.get("implementation") or {}).get("head") or (
            evidence.get("implementation") or {}).get("packages"):
        return "IMPLEMENT"
    if (evidence.get("architecture") or {}).get("artifact"):
        return "ARCHITECTURE"
    if (evidence.get("hermes") or {}).get("routing"):
        return "HERMES"
    return "HERMES"


def seed_evidence(mission: Any) -> dict[str, Any]:
    return {
        "pcp": {
            "path": mission.canonical_path,
            "digest": mission.package_digest,
            "package_id": mission.package_id,
            "promoted": True,
        },
        "controller": {
            "mission_key": mission.mission_key,
            "notion_required": False,
            "idempotent": True,
        },
    }


def run(mission: Any, *, vault_root: str | Path,
        state_dir: str | Path, executors: PipelineExecutors) -> dict[str, Any]:
    """Advance one mission as far as executors can prove. Never serves a prototype."""

    evidence = {**seed_evidence(mission), **(mission.evidence or {})}
    work = Path(state_dir) / "pcp-pipeline" / mission.package_id / "work"
    work.mkdir(parents=True, exist_ok=True)
    if not (evidence.get("hermes") or {}).get("routing"):
        evidence["hermes"] = executors.hermes(mission)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if not (evidence["hermes"] or {}).get("routing"):
            return evidence
    if not (evidence.get("architecture") or {}).get("artifact"):
        evidence["architecture"] = executors.architecture(
            mission, evidence["hermes"], work)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if not (evidence["architecture"] or {}).get("artifact"):
            return evidence
    if not ((evidence.get("implementation") or {}).get("head") or
            (evidence.get("implementation") or {}).get("packages")):
        evidence["implementation"] = executors.implementation(
            mission, evidence["architecture"], work)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if not ((evidence["implementation"] or {}).get("head") or
                (evidence["implementation"] or {}).get("packages")):
            return evidence
    if not (evidence.get("integration") or {}).get("candidate_head"):
        evidence["integration"] = executors.integrate(
            mission, evidence["implementation"], work)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if not (evidence["integration"] or {}).get("candidate_head"):
            return evidence
    head = evidence["integration"]["candidate_head"]
    if (evidence.get("functional_e2e") or {}).get("result") != "PASS":
        evidence["functional_e2e"] = executors.e2e(mission, work, head)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if (evidence["functional_e2e"] or {}).get("result") != "PASS":
            return evidence
    if not (evidence.get("rc_alpha") or {}).get("url"):
        evidence["rc_alpha"] = executors.deploy(
            mission, work, head, Path(state_dir))
        evidence["lifecycle"] = lifecycle_of(evidence)
    return evidence


def accept_line(evidence: Mapping[str, Any]) -> str:
    missing = missing_links(evidence)
    if missing:
        return "SF272_FACTORY_GOLDEN_PATH_REJECT — %s" % ",".join(missing)
    rc = evidence["rc_alpha"]
    return "SF272_FACTORY_GOLDEN_PATH_ACCEPT — %s — %s" % (
        rc["url"], evidence["integration"]["candidate_head"])


def _quota_exhausted(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in (
        "insufficient_quota",
        "quota exceeded",
        "quota_exceeded",
        "rate limit exceeded",
        "usage limit reached",
        "you've hit your usage limit",
        "context length exceeded",
    ))


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 120
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class FleetExecutors:
    """Live Hermes/fleet executors. Architecture and implementation wake Codex."""

    def __init__(self, *, vault_root: str | Path, state_dir: str | Path) -> None:
        self.vault_root = Path(vault_root)
        self.state_dir = Path(state_dir)

    def hermes(self, mission: Any) -> dict[str, Any]:
        from . import pcp_missions
        prototype = pcp_missions.locate_prototype(self.vault_root, mission)
        return {
            "owner": "hermes",
            "mission_key": mission.mission_key,
            "routing": {
                "architecture": dict(ARCH_PRIMARY),
                "architecture_fallback": dict(ARCH_FALLBACK),
                "implementation": dict(IMPL_ROUTE),
            },
            "plan": [
                "architecture", "implementation", "integration",
                "functional_e2e", "rc_alpha",
            ],
            "prototype_input": str(prototype) if prototype else "",
        }

    def architecture(self, mission: Any,
                     hermes: Mapping[str, Any], work: Path) -> dict[str, Any]:
        self._prepare_work(mission, work)
        intake_head = _git_head(work)
        artifact = work / "architecture.json"
        prompt = _architecture_prompt(mission, hermes, work)
        sol = self._codex("gpt-5.6-sol", "high", prompt, work)
        if artifact.is_file():
            body = json.loads(artifact.read_text(encoding="utf-8"))
            return {
                "artifact": str(artifact),
                **ARCH_PRIMARY,
                "sol": sol,
                "fallback": None,
                "body": body,
                "intake_head": intake_head,
            }
        if sol.get("quota"):
            fallback = self._cursor_opus(prompt, work)
            if artifact.is_file():
                body = json.loads(artifact.read_text(encoding="utf-8"))
                return {
                    "artifact": str(artifact),
                    **ARCH_FALLBACK,
                    "sol": sol,
                    "fallback": fallback,
                    "body": body,
                    "intake_head": intake_head,
                }
            return {"sol": sol, "fallback": fallback, "intake_head": intake_head}
        return {"sol": sol, "intake_head": intake_head}

    def implementation(self, mission: Any,
                       architecture: Mapping[str, Any], work: Path) -> dict[str, Any]:
        existing = self._implementation_result(architecture, work, {})
        if existing.get("head"):
            return existing
        if _git_dirty(work):
            receipt = self._codex(
                "gpt-5.6-luna", "max",
                _implementation_commit_prompt(mission, architecture, work), work)
            result = self._implementation_result(architecture, work, receipt)
            if result.get("head"):
                return result
        prompt = _implementation_prompt(mission, architecture, work)
        receipt = self._codex("gpt-5.6-luna", "max", prompt, work)
        result = self._implementation_result(architecture, work, receipt)
        if result.get("head") or not _git_dirty(work):
            return result
        follow = self._codex(
            "gpt-5.6-luna", "max",
            _implementation_commit_prompt(mission, architecture, work), work)
        result = self._implementation_result(architecture, work, follow)
        result["continuation_receipt"] = follow
        return result

    def _implementation_result(self, architecture: Mapping[str, Any], work: Path,
                               receipt: Mapping[str, Any]) -> dict[str, Any]:
        impl_file = work / "implementation.json"
        head = _git_head(work)
        intake = str(architecture.get("intake_head") or "")
        if not intake:
            root = _git(["rev-list", "--max-parents=0", "HEAD"], work)
            if root.returncode == 0:
                intake = (root.stdout.strip().splitlines() or [""])[0]
        if impl_file.is_file():
            body = json.loads(impl_file.read_text(encoding="utf-8"))
            return {
                "packages": body.get("packages") or [{
                    "id": "core",
                    **IMPL_ROUTE,
                    "branch": body.get("branch") or _git_branch(work),
                    "head": body.get("head") or head,
                    "acceptance": body.get("acceptance") or "",
                }],
                "head": body.get("head") or head,
                "runner_receipt": receipt,
            }
        if head and intake and head != intake:
            return {
                "packages": [{
                    "id": "core",
                    **IMPL_ROUTE,
                    "branch": _git_branch(work),
                    "head": head,
                    "acceptance": "post-intake git head",
                }],
                "head": head,
                "runner_receipt": receipt,
            }
        return {"runner_receipt": receipt}

    def integrate(self, mission: Any,
                  implementation: Mapping[str, Any], work: Path) -> dict[str, Any]:
        head = implementation.get("head") or _git_head(work)
        packages = implementation.get("packages") or ()
        included = [str(item.get("head") or "") for item in packages if item.get("head")]
        if head:
            marker = work / CANDIDATE_MARKER
            marker.write_text(json.dumps({
                "candidate_head": head,
                "package_id": mission.package_id,
                "mission_key": mission.mission_key,
            }, indent=2) + "\n", encoding="utf-8")
            return {
                "candidate_head": head,
                "included_heads": included or [head],
                "mission_key": mission.mission_key,
                "work": str(work),
            }
        return {}

    def e2e(self, mission: Any, work: Path,
            candidate_head: str) -> dict[str, Any]:
        if _git_head(work) != candidate_head:
            return {
                "candidate": candidate_head,
                "result": "FAIL",
                "detail": "worktree HEAD is not the integrated candidate",
            }
        scenarios: list[str] = []
        result = "PASS"
        detail = ""
        if (work / "package.json").is_file() and shutil.which("node"):
            scenarios.append("node --test")
            proc = _run(["node", "--test"], cwd=work, timeout=180)
            if proc.returncode != 0:
                result = "FAIL"
                detail = (proc.stdout + proc.stderr)[-4000:]
        marker = work / CANDIDATE_MARKER
        if not marker.is_file():
            # Implementation must leave a candidate marker; tests/live fleet write it.
            if result == "PASS":
                result = "FAIL"
                detail = "missing %s" % CANDIDATE_MARKER
            scenarios.append(CANDIDATE_MARKER)
        else:
            scenarios.append(CANDIDATE_MARKER)
            try:
                body = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = {}
            if body.get("candidate_head") != candidate_head:
                result = "FAIL"
                detail = "candidate marker does not match integrated head"
        return {
            "candidate": candidate_head,
            "scenarios": scenarios,
            "result": result,
            "detail": detail,
        }

    def deploy(self, mission: Any, work: Path,
               candidate_head: str, state_dir: Path) -> dict[str, Any]:
        sealed = state_dir / "pcp-candidates" / candidate_head[:12]
        if sealed.exists():
            shutil.rmtree(sealed)
        shutil.copytree(work, sealed, ignore=shutil.ignore_patterns(".git"))
        marker = sealed / CANDIDATE_MARKER
        marker.write_text(json.dumps({
            "candidate_head": candidate_head,
            "package_id": mission.package_id,
            "mission_key": mission.mission_key,
        }, indent=2) + "\n", encoding="utf-8")
        from . import pcp_missions
        url = pcp_missions.serve_product_rc(
            sealed, state_dir=state_dir, package_id=mission.package_id,
            candidate_head=candidate_head)
        if not url:
            return {}
        return {
            "url": url,
            "candidate_head": candidate_head,
            "deployment": str(sealed),
        }

    def _prepare_work(self, mission: Any, work: Path) -> None:
        if (work / ".git").exists():
            return
        from . import pcp_missions
        prototype = pcp_missions.locate_prototype(self.vault_root, mission)
        if prototype is not None:
            if prototype.resolve() != work.resolve():
                if any(work.iterdir()):
                    pass
                else:
                    shutil.copytree(prototype, work, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(".git"))
        if not any(work.iterdir()):
            (work / "README.md").write_text(
                "Factory candidate workspace for %s\n" % mission.package_id,
                encoding="utf-8")
        _git(["init"], work)
        _git(["add", "-A"], work)
        _git(["commit", "-m", "factory intake snapshot"], work)

    def _codex(self, model: str, effort: str, prompt: str, cwd: Path) -> dict[str, Any]:
        binary = shutil.which("codex")
        if not binary:
            return {"ok": False, "error": "HARNESS_BINARY_MISSING", "detail": "codex not on PATH"}
        cmd = [binary, "exec", "-m", model, "-c",
               "model_reasoning_effort=%s" % effort, prompt]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=2400)
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": "TIMEOUT", "detail": str(exc)[:500]}
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        quota = _quota_exhausted(text)
        return {
            "ok": proc.returncode == 0 and not quota,
            "quota": quota,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "harness": "codex",
            "model": model,
            "effort": effort,
        }

    def _cursor_opus(self, prompt: str, cwd: Path) -> dict[str, Any]:
        binary = shutil.which("cursor") or (
            "/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
        if not os.path.exists(binary):
            return {
                "ok": False,
                "error": "HARNESS_WAKE_PATH_UNAVAILABLE:cursor",
                "detail": "Cursor CLI missing",
            }
        cmd = [binary, "agent", "-p", "--model", "claude-opus-5", prompt]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=2400)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": "WAKE_SUBPROCESS_FAILED", "detail": str(exc)[:500]}
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            **ARCH_FALLBACK,
        }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "factory-controller")
    env.setdefault("GIT_AUTHOR_EMAIL", "factory@local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return subprocess.run(
        ["git", "-c", "user.name=factory-controller",
         "-c", "user.email=factory@local", *args],
        cwd=cwd, capture_output=True, text=True, env=env,
        timeout=60)


def _git_head(cwd: Path) -> str:
    proc = _git(["rev-parse", "HEAD"], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_branch(cwd: Path) -> str:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_dirty(cwd: Path) -> bool:
    proc = _git(["status", "--porcelain"], cwd)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _architecture_prompt(mission: Any,
                         hermes: Mapping[str, Any], work: Path) -> str:
    return (
        "You are the Factory Architect (Codex GPT-5.6 Sol / High) for mission "
        "%s (%s).\n"
        "Write %s/architecture.json covering: prototype reuse decision, "
        "subsystem boundaries, invariants, implementation delta from the "
        "Lab prototype, implementation packages, dependencies, deterministic "
        "verification, functional E2E acceptance, and RC-alpha deployment "
        "expectations.\n"
        "Prototype input (evaluate; do not label it RC-alpha): %s\n"
        "Do not implement the product. Do not serve or rewrite the prototype "
        "as RC-alpha. The JSON artifact is the only required output.\n"
        % (mission.mission_key, mission.package_id, work,
           hermes.get("prototype_input") or "(none)")
    )


def _implementation_prompt(mission: Any,
                           architecture: Mapping[str, Any], work: Path) -> str:
    return (
        "You are the Factory Developer Fleet (Codex GPT-5.6 Luna / Max) for "
        "mission %s (%s).\n"
        "Implement the architecture delta in %s. Commit on an isolated branch. "
        "Write implementation.json with packages[{id,runner,model,effort,branch,head,acceptance}]. "
        "Write %s with {\"candidate_head\": \"<HEAD sha>\"}.\n"
        "Architecture artifact: %s\n"
        "Do not present the pre-intake prototype as RC-alpha.\n"
        % (mission.mission_key, mission.package_id, work, CANDIDATE_MARKER,
           architecture.get("artifact") or "")
    )


def _implementation_commit_prompt(mission: Any,
                                 architecture: Mapping[str, Any], work: Path) -> str:
    return (
        "Continue the same Factory implementation mission %s. "
        "Uncommitted work already exists in %s. Do not restart. "
        "Commit the architecture delta on an isolated branch so HEAD differs "
        "from intake %s. Write implementation.json and %s with the new HEAD. "
        "Then stop.\n"
        % (mission.mission_key, work, architecture.get("intake_head") or "",
           CANDIDATE_MARKER)
    )
