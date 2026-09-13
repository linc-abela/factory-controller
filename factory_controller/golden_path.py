"""Hermes-owned Lab -> Factory golden path.

RC-alpha is valid only when every required link exists for the same PCP
mission. An existing checkout or reachable URL is never itself RC-alpha.

Hermes routes by capability: classify the work, query the Capability
Mapping, inspect live availability, and execute the selected profile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import capability_map
from . import capability_resolver
from .capability_map import CAP_ARCHITECTURE, CAP_IMPLEMENTATION, Profile
from .fleet_harness import (
    COMPLETED,
    FAILED,
    FleetHarness,
    QUOTA_EXHAUSTED,
    TEMPORARILY_UNAVAILABLE,
)

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
CANDIDATE_MARKER = ".factory-candidate.json"
_RERUN = {QUOTA_EXHAUSTED, TEMPORARILY_UNAVAILABLE}


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
        if not ((evidence.get("implementation") or {}).get("head") or
                (evidence.get("implementation") or {}).get("packages")):
            return evidence
    if not (evidence.get("integration") or {}).get("candidate_head"):
        evidence["integration"] = executors.integrate(
            mission, evidence["implementation"], work)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if not (evidence.get("integration") or {}).get("candidate_head"):
            return evidence
    head = evidence["integration"]["candidate_head"]
    if (evidence.get("functional_e2e") or {}).get("result") != "PASS":
        evidence["functional_e2e"] = executors.e2e(mission, work, head)
        evidence["lifecycle"] = lifecycle_of(evidence)
        if (evidence.get("functional_e2e") or {}).get("result") != "PASS":
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


class FleetExecutors:
    """Live Hermes/fleet executors. Profiles come from the Capability Mapping."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        state_dir: str | Path,
        catalog: capability_map.CapabilityMap | None = None,
        harness: FleetHarness | None = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        self.state_dir = Path(state_dir)
        self._catalog = catalog
        self._harness = harness or FleetHarness()

    def catalog(self) -> capability_map.CapabilityMap:
        if self._catalog is None:
            self._catalog = capability_map.load(self.vault_root)
        return self._catalog

    def hermes(self, mission: Any) -> dict[str, Any]:
        from . import pcp_missions
        prototype = pcp_missions.locate_prototype(self.vault_root, mission)
        catalog = self.catalog()
        return {
            "owner": "hermes",
            "mission_key": mission.mission_key,
            "routing": {
                "source": catalog.source,
                "capabilities": {
                    "architecture": CAP_ARCHITECTURE,
                    "implementation": CAP_IMPLEMENTATION,
                },
                "eligible": {
                    "architecture": [
                        profile.as_dict()
                        for profile in catalog.for_capability(CAP_ARCHITECTURE)
                    ],
                    "implementation": [
                        profile.as_dict()
                        for profile in catalog.for_capability(CAP_IMPLEMENTATION)
                    ],
                },
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
        if artifact.is_file():
            return self._architecture_result(
                artifact, intake_head, attempts=[], selected=None)
        attempts = self._execute_capability(
            CAP_ARCHITECTURE, mission, work,
            prompt_for=lambda profile: _architecture_prompt(
                mission, hermes, work, profile),
            succeeded=lambda: artifact.is_file(),
            context={"difficulty": "high"},
        )
        selected = _final_profile(attempts)
        if artifact.is_file():
            return self._architecture_result(
                artifact, intake_head, attempts, selected)
        return {
            "attempts": attempts,
            "intake_head": intake_head,
            "live": self._live(work),
        }

    def implementation(self, mission: Any,
                       architecture: Mapping[str, Any], work: Path) -> dict[str, Any]:
        existing = self._implementation_result(architecture, work, None)
        if existing.get("head"):
            return existing
        attempts = self._execute_capability(
            CAP_IMPLEMENTATION, mission, work,
            prompt_for=lambda profile: (
                _implementation_commit_prompt(mission, architecture, work, profile)
                if _git_dirty(work)
                else _implementation_prompt(mission, architecture, work, profile)
            ),
            succeeded=lambda: bool(
                self._implementation_result(architecture, work, None).get("head")),
            context={
                "difficulty": "high",
                "incumbent": self._routing(work).get("incumbent") or "",
            },
        )
        selected = _final_profile(attempts)
        result = self._implementation_result(architecture, work, selected)
        result["attempts"] = attempts
        result["live"] = self._live(work)
        return result

    def _execute_capability(
        self,
        capability: str,
        mission: Any,
        work: Path,
        *,
        prompt_for,
        succeeded,
        context: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for _ in range(8):
            live = self._live(work)
            ctx = dict(context)
            ctx["incumbent"] = self._routing(work).get("incumbent") or ctx.get("incumbent") or ""
            choice = capability_resolver.resolve(
                capability, self.catalog(), live, context=ctx)
            if choice is None:
                return attempts
            profile = choice.profile
            if attempts:
                attempts[-1]["next_selected"] = profile.as_dict()
            attempts.append(self._one_attempt(
                capability, mission, work, profile, choice, prompt_for, succeeded))
            if succeeded():
                return attempts
            receipt_status = attempts[-1]["availability_result"]
            if receipt_status in _RERUN:
                continue
            if receipt_status == COMPLETED and _git_dirty(work):
                attempts.append(self._one_attempt(
                    capability, mission, work, profile, choice, prompt_for, succeeded))
                if succeeded():
                    return attempts
                if attempts[-1]["availability_result"] in _RERUN:
                    continue
            return attempts
        return attempts

    def _one_attempt(
        self, capability, mission, work, profile, choice, prompt_for, succeeded,
    ) -> dict[str, Any]:
        started = time.time()
        receipt = self._harness.run(profile, prompt_for(profile), work)
        ended = time.time()
        if _git_dirty(work):
            self._set_incumbent(work, profile.key)
        if receipt.status in _RERUN:
            self._mark(work, profile, receipt.status)
        return {
            "capability": capability,
            "mission_key": getattr(mission, "mission_key", ""),
            "candidate": profile.as_dict(),
            "runner": profile.harness,
            "model": profile.model,
            "effort": profile.effort,
            "availability_result": receipt.status,
            "quota_result": receipt.status == QUOTA_EXHAUSTED,
            "started_at": started,
            "ended_at": ended,
            "completion": _completion(receipt.status, succeeded()),
            "reselection_reason": choice.reason,
            "excluded": list(choice.excluded),
            "next_selected": None,
            "final_producer": succeeded(),
            "branch": _git_branch(work),
            "head": _git_head(work),
            "artifact": str(work / "architecture.json")
            if (work / "architecture.json").is_file() else "",
            "receipt": receipt.as_dict(),
        }

    def _architecture_result(
        self, artifact: Path, intake_head: str,
        attempts: list[dict[str, Any]], selected: Profile | None,
    ) -> dict[str, Any]:
        body = json.loads(artifact.read_text(encoding="utf-8"))
        route = selected.as_dict() if selected is not None else (
            attempts[-1]["candidate"] if attempts else {})
        return {
            "artifact": str(artifact),
            **{k: route[k] for k in ("harness", "model", "effort") if k in route},
            "body": body,
            "intake_head": intake_head,
            "attempts": attempts,
            "producer": route,
        }

    def _implementation_result(
        self, architecture: Mapping[str, Any], work: Path,
        selected: Profile | None,
    ) -> dict[str, Any]:
        impl_file = work / "implementation.json"
        head = _git_head(work)
        intake = str(architecture.get("intake_head") or "")
        if not intake:
            root = _git(["rev-list", "--max-parents=0", "HEAD"], work)
            if root.returncode == 0:
                intake = (root.stdout.strip().splitlines() or [""])[0]
        route = selected.as_dict() if selected is not None else {}
        package = {
            "id": "core",
            **{k: route[k] for k in ("harness", "model", "effort") if k in route},
            "branch": _git_branch(work),
            "head": head,
            "acceptance": "",
        }
        if impl_file.is_file():
            body = json.loads(impl_file.read_text(encoding="utf-8"))
            package.update({
                "branch": body.get("branch") or package["branch"],
                "head": body.get("head") or head,
                "acceptance": body.get("acceptance") or "",
            })
            return {
                "packages": body.get("packages") or [package],
                "head": body.get("head") or head,
                "producer": route,
            }
        if head and intake and head != intake:
            package["acceptance"] = "post-intake git head"
            return {
                "packages": [package],
                "head": head,
                "producer": route,
            }
        return {"producer": route}

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
                if not any(work.iterdir()):
                    shutil.copytree(prototype, work, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(".git"))
        if not any(work.iterdir()):
            (work / "README.md").write_text(
                "Factory candidate workspace for %s\n" % mission.package_id,
                encoding="utf-8")
        _git(["init"], work)
        _git(["add", "-A"], work)
        _git(["commit", "-m", "factory intake snapshot"], work)

    def _routing_path(self, work: Path) -> Path:
        return work.parent / "routing.json"

    def _routing(self, work: Path) -> dict[str, Any]:
        path = self._routing_path(work)
        if not path.is_file():
            return {"live": {}, "incumbent": ""}
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"live": {}, "incumbent": ""}
        if not isinstance(body, dict):
            return {"live": {}, "incumbent": ""}
        body.setdefault("live", {})
        body.setdefault("incumbent", "")
        return body

    def _live(self, work: Path) -> dict[str, str]:
        live = self._routing(work).get("live") or {}
        return {str(key): str(value) for key, value in live.items()}

    def _mark(self, work: Path, profile: Profile, status: str) -> None:
        body = self._routing(work)
        live = dict(body.get("live") or {})
        live[profile.key] = status
        body["live"] = live
        self._routing_path(work).write_text(
            json.dumps(body, indent=2) + "\n", encoding="utf-8")

    def _set_incumbent(self, work: Path, key: str) -> None:
        body = self._routing(work)
        body["incumbent"] = key
        self._routing_path(work).write_text(
            json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _final_profile(attempts: list[dict[str, Any]]) -> Profile | None:
    for attempt in reversed(attempts):
        if attempt.get("final_producer") or attempt.get("availability_result") == COMPLETED:
            candidate = attempt.get("candidate") or {}
            try:
                return Profile(
                    capability=str(candidate.get("capability") or ""),
                    role=str(candidate.get("role") or "member"),
                    harness=str(candidate.get("harness") or ""),
                    model=str(candidate.get("model") or ""),
                    effort=str(candidate.get("effort") or ""),
                    purpose=str(candidate.get("purpose") or ""),
                    quota_continuity=bool(candidate.get("quota_continuity")),
                )
            except TypeError:
                return None
    return None


def _completion(status: str, succeeded: bool) -> str:
    if succeeded:
        return "completed"
    if status in _RERUN:
        return "interrupted"
    if status == FAILED:
        return "failed"
    return status.lower()


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 120
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


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


def _architecture_prompt(mission: Any, hermes: Mapping[str, Any],
                         work: Path, profile: Profile) -> str:
    return (
        "You are the Factory Architect (%s %s / %s) for mission %s (%s).\n"
        "Write %s/architecture.json covering: prototype reuse decision, "
        "subsystem boundaries, invariants, implementation delta from the "
        "Lab prototype, implementation packages, dependencies, deterministic "
        "verification, functional E2E acceptance, and RC-alpha deployment "
        "expectations.\n"
        "Prototype input (evaluate; do not label it RC-alpha): %s\n"
        "Do not implement the product. Do not serve or rewrite the prototype "
        "as RC-alpha. The JSON artifact is the only required output.\n"
        % (profile.harness, profile.model, profile.effort,
           mission.mission_key, mission.package_id, work,
           hermes.get("prototype_input") or "(none)")
    )


def _implementation_prompt(mission: Any, architecture: Mapping[str, Any],
                           work: Path, profile: Profile) -> str:
    return (
        "You are the Factory Developer Fleet (%s %s / %s) for mission %s (%s).\n"
        "Implement the architecture delta in %s. Commit on an isolated branch. "
        "Write implementation.json with packages"
        "[{id,runner,model,effort,branch,head,acceptance}]. "
        "Write %s with {\"candidate_head\": \"<HEAD sha>\"}.\n"
        "Architecture artifact: %s\n"
        "Do not present the pre-intake prototype as RC-alpha.\n"
        % (profile.harness, profile.model, profile.effort,
           mission.mission_key, mission.package_id, work, CANDIDATE_MARKER,
           architecture.get("artifact") or "")
    )


def _implementation_commit_prompt(mission: Any, architecture: Mapping[str, Any],
                                  work: Path, profile: Profile) -> str:
    return (
        "Continue the same Factory implementation mission %s as %s %s / %s. "
        "Uncommitted work already exists in %s. Do not restart. "
        "Commit the architecture delta on an isolated branch so HEAD differs "
        "from intake %s. Write implementation.json and %s with the new HEAD. "
        "Then stop.\n"
        % (mission.mission_key, profile.harness, profile.model, profile.effort,
           work, architecture.get("intake_head") or "", CANDIDATE_MARKER)
    )
