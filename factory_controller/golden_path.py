"""Hermes-owned Lab -> Factory golden path.

RC-alpha is valid only when every required link exists for the same PCP
mission. An existing checkout or reachable URL is never itself RC-alpha.

Hermes routes by capability: classify the work, query the Capability
Mapping, inspect live availability, and execute the selected profile.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import capability_map
from . import capability_resolver
from .capability_map import CAP_ARCHITECTURE, CAP_IMPLEMENTATION, CAP_QA, Profile
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
AG_RECEIPT = "ag-e2e.json"
MAX_REPAIR = 8
_RERUN = {QUOTA_EXHAUSTED, TEMPORARILY_UNAVAILABLE}


class IncompleteChain(ValueError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("SF272_FACTORY_GOLDEN_PATH_REJECT — %s" % ",".join(missing))


class PipelineExecutors(Protocol):
    def hermes(self, mission: Any) -> dict[str, Any]: ...
    def architecture(self, mission: Any, hermes: Mapping[str, Any], work: Path) -> dict[str, Any]: ...
    def implementation(self, mission: Any, architecture: Mapping[str, Any], work: Path,
                       repair: Mapping[str, Any] | None = None) -> dict[str, Any]: ...
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


def apply_owner_reject(evidence: dict[str, Any]) -> dict[str, Any]:
    """Invalidate RC evidence for a rejected candidate and keep the same mission."""

    ov = evidence.get("owner_validation") or {}
    if str(ov.get("decision") or "").upper() != "REJECT":
        return evidence
    rejected = str(ov.get("candidate_head") or "")
    current = str(
        (evidence.get("integration") or {}).get("candidate_head")
        or (evidence.get("rc_alpha") or {}).get("candidate_head")
        or "")
    if rejected and current and rejected != current:
        return evidence
    if not rejected:
        return evidence
    if rejected in list(evidence.get("rejected_candidates") or []):
        evidence.setdefault("_repair", {
            "reason": "owner_reject",
            "rejected_head": rejected,
            "feedback": ov.get("feedback") or "",
            "defects": _split_defects(ov.get("feedback") or ""),
        })
        return evidence
    history = list(evidence.get("repair_history") or [])
    history.append({
        "reason": "owner_reject",
        "candidate_head": rejected,
        "feedback": ov.get("feedback") or "",
        "functional_e2e": evidence.get("functional_e2e"),
        "rc_alpha": evidence.get("rc_alpha"),
        "implementation": evidence.get("implementation"),
        "integration": evidence.get("integration"),
    })
    rejected_heads = list(evidence.get("rejected_candidates") or [])
    if rejected not in rejected_heads:
        rejected_heads.append(rejected)
    e2e_runs = list(evidence.get("e2e_runs") or [])
    if evidence.get("functional_e2e"):
        e2e_runs.append(evidence["functional_e2e"])
    evidence["repair_history"] = history
    evidence["rejected_candidates"] = rejected_heads
    evidence["e2e_runs"] = e2e_runs
    evidence["_repair"] = {
        "reason": "owner_reject",
        "rejected_head": rejected,
        "feedback": ov.get("feedback") or "",
        "defects": _split_defects(ov.get("feedback") or ""),
    }
    evidence.pop("rc_alpha", None)
    evidence.pop("functional_e2e", None)
    evidence.pop("implementation", None)
    evidence.pop("integration", None)
    evidence["lifecycle"] = lifecycle_of(evidence)
    return evidence


def _split_defects(text: str) -> list[str]:
    parts = [part.strip(" -") for part in re.split(r"[;\n]+", text) if part.strip()]
    return parts or ([text] if text else [])


def _stale_failed_e2e(evidence: dict[str, Any]) -> dict[str, Any]:
    e2e = evidence.get("functional_e2e") or {}
    if e2e.get("result") != "FAIL":
        return evidence
    head = str(e2e.get("candidate") or e2e.get("candidate_head") or "")
    history = list(evidence.get("repair_history") or [])
    history.append({
        "reason": "ag_e2e_fail",
        "candidate_head": head,
        "functional_e2e": e2e,
        "implementation": evidence.get("implementation"),
        "integration": evidence.get("integration"),
    })
    e2e_runs = list(evidence.get("e2e_runs") or [])
    e2e_runs.append(e2e)
    defects = e2e.get("defects") or _split_defects(str(e2e.get("detail") or ""))
    evidence["repair_history"] = history
    evidence["e2e_runs"] = e2e_runs
    evidence["_repair"] = {
        "reason": "ag_e2e_fail",
        "rejected_head": head,
        "feedback": e2e.get("detail") or "",
        "defects": defects,
    }
    evidence.pop("functional_e2e", None)
    evidence.pop("implementation", None)
    evidence.pop("integration", None)
    evidence.pop("rc_alpha", None)
    evidence["lifecycle"] = lifecycle_of(evidence)
    return evidence


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
    evidence = apply_owner_reject(dict(evidence))
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
    for _ in range(MAX_REPAIR):
        evidence = apply_owner_reject(evidence)
        repair = evidence.get("_repair") if isinstance(evidence.get("_repair"), Mapping) else None
        if not ((evidence.get("implementation") or {}).get("head") or
                (evidence.get("implementation") or {}).get("packages")):
            evidence["implementation"] = executors.implementation(
                mission, evidence["architecture"], work, repair)
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
        head = str(evidence["integration"]["candidate_head"])
        rejected = {str(item) for item in (evidence.get("rejected_candidates") or ())}
        if head in rejected:
            evidence["_repair"] = {
                **(repair or {}),
                "reason": (repair or {}).get("reason") or "rejected_head_reuse",
                "rejected_head": head,
            }
            evidence.pop("implementation", None)
            evidence.pop("integration", None)
            continue
        if (evidence.get("functional_e2e") or {}).get("result") != "PASS":
            evidence["functional_e2e"] = executors.e2e(mission, work, head)
            evidence["lifecycle"] = lifecycle_of(evidence)
            if (evidence.get("functional_e2e") or {}).get("result") != "PASS":
                if (evidence.get("functional_e2e") or {}).get("result") == "FAIL":
                    evidence = _stale_failed_e2e(evidence)
                    continue
                return evidence
        if not (evidence.get("rc_alpha") or {}).get("url"):
            evidence["rc_alpha"] = executors.deploy(
                mission, work, head, Path(state_dir))
            evidence["lifecycle"] = lifecycle_of(evidence)
        evidence.pop("_repair", None)
        return evidence
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
                    "functional_e2e": CAP_QA,
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
                    "functional_e2e": [
                        profile.as_dict()
                        for profile in catalog.for_capability(CAP_QA)
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
                       architecture: Mapping[str, Any], work: Path,
                       repair: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rejected = str((repair or {}).get("rejected_head") or "")
        current = _git_head(work)
        if repair and current and current != rejected:
            selected = None
            result = self._implementation_result(architecture, work, selected)
            result["head"] = current
            result["live"] = self._live(work)
            result["repair"] = dict(repair)
            result["acceptance"] = result.get("acceptance") or "post-reject git head"
            return result
        if not repair:
            existing = self._implementation_result(architecture, work, None)
            if existing.get("head"):
                return existing

        def _new_head() -> bool:
            head = _git_head(work)
            if not head:
                return False
            if rejected:
                return head != rejected
            intake = str(architecture.get("intake_head") or "")
            return (not intake) or head != intake

        attempts = self._execute_capability(
            CAP_IMPLEMENTATION, mission, work,
            prompt_for=lambda profile: (
                _repair_prompt(mission, architecture, work, profile, repair)
                if repair else
                _implementation_commit_prompt(mission, architecture, work, profile)
                if _git_dirty(work)
                else _implementation_prompt(mission, architecture, work, profile)
            ),
            succeeded=_new_head,
            context={
                "difficulty": "recovery" if repair else "high",
                "incumbent": self._routing(work).get("incumbent") or "",
            },
        )
        selected = _final_profile(attempts)
        result = self._implementation_result(architecture, work, selected)
        if rejected and str(result.get("head") or "") == rejected:
            result = {"producer": result.get("producer") or {}}
        result["attempts"] = attempts
        result["live"] = self._live(work)
        if repair:
            result["repair"] = dict(repair)
        if not result.get("head"):
            result["detail"] = result.get("detail") or (
                "no_new_head" if attempts else "no_eligible_live_profile")
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
        self._clear_transient(work)
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
                "candidate_head": candidate_head,
                "result": "FAIL",
                "detail": "worktree HEAD is not the integrated candidate",
                "scenarios": [],
            }
        from . import pcp_missions
        exported = Path(self.state_dir) / "pcp-e2e-export" / candidate_head[:12]
        if not _export_head(work, exported, candidate_head):
            return {
                "candidate": candidate_head,
                "candidate_head": candidate_head,
                "result": "FAIL",
                "detail": "could not export committed candidate for rendered E2E",
                "scenarios": [],
            }
        (exported / CANDIDATE_MARKER).write_text(json.dumps({
            "candidate_head": candidate_head,
            "package_id": mission.package_id,
            "mission_key": mission.mission_key,
        }, indent=2) + "\n", encoding="utf-8")
        url = pcp_missions.serve_product_rc(
            exported, state_dir=self.state_dir, package_id=mission.package_id,
            candidate_head=candidate_head, lane="pcp-e2e")
        receipt = work / AG_RECEIPT
        if receipt.is_file():
            receipt.unlink()
        architecture = {}
        try:
            raw = (work / "architecture.json").read_text(encoding="utf-8")
            architecture = json.loads(raw)
        except (OSError, ValueError):
            architecture = {}
        routing = self._routing(work)

        def succeeded() -> bool:
            body = _ag_receipt(work)
            return (
                body.get("result") == "PASS"
                and str(body.get("candidate_head") or "") == candidate_head
            )

        attempts = self._execute_capability(
            CAP_QA, mission, work,
            prompt_for=lambda profile: _e2e_prompt(
                mission, work, candidate_head, url, profile, architecture,
                routing.get("repair") or {}),
            succeeded=succeeded,
            context={"difficulty": "medium", "rendered": True},
        )
        _capture_ag_receipt(work, attempts, candidate_head, mission)
        selected = _final_profile(attempts)
        body = _ag_receipt(work)
        scenarios = list(body.get("scenarios") or [])
        if (work / "package.json").is_file() and shutil.which("node"):
            scenarios.append("node --test (supplemental)")
            proc = _run(["node", "--test"], cwd=work, timeout=180)
            if proc.returncode != 0 and body.get("result") == "PASS":
                # Supplemental only: cannot grant PASS, may add evidence.
                body.setdefault("defects", []).append("supplemental node --test failed")
        result = "PASS" if succeeded() else "FAIL"
        if not attempts and not body:
            result = "FAIL"
        if attempts and not succeeded():
            last = attempts[-1]
            if last.get("availability_result") in _RERUN:
                result = last["availability_result"]
        route = selected.as_dict() if selected is not None else (
            attempts[-1]["candidate"] if attempts else {})
        return {
            "candidate": candidate_head,
            "candidate_head": candidate_head,
            "mission_key": mission.mission_key,
            "capability": CAP_QA,
            "url": url,
            "scenarios": scenarios or body.get("scenarios") or ["rendered_browser"],
            "result": result,
            "defects": body.get("defects") or [],
            "detail": body.get("detail") or "",
            "run_id": body.get("run_id") or "",
            "evidence_artifacts": body.get("evidence") or [str(receipt)],
            "attempts": attempts,
            **{k: route[k] for k in ("harness", "model", "effort") if k in route},
            "receipt": body,
        }

    def deploy(self, mission: Any, work: Path,
               candidate_head: str, state_dir: Path) -> dict[str, Any]:
        sealed = state_dir / "pcp-candidates" / candidate_head[:12]
        if not _export_head(work, sealed, candidate_head):
            return {}
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

    def _clear_transient(self, work: Path) -> None:
        """Re-probe live availability at the start of a capability attempt.

        Stale QUOTA_EXHAUSTED / TEMPORARILY_UNAVAILABLE from a prior process is
        not current truth. Marks written during this attempt still exclude.
        """

        body = self._routing(work)
        live = dict(body.get("live") or {})
        if live:
            body["live"] = {}
            self._routing_path(work).write_text(
                json.dumps(body, indent=2) + "\n", encoding="utf-8")

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


def _repair_prompt(mission: Any, architecture: Mapping[str, Any],
                   work: Path, profile: Profile,
                   repair: Mapping[str, Any] | None) -> str:
    repair = repair or {}
    defects = repair.get("defects") or _split_defects(str(repair.get("feedback") or ""))
    return (
        "Continue the SAME Factory implementation mission %s as %s %s / %s. "
        "Do not start a new product. Repair the current candidate in %s.\n"
        "Rejected/failed head that must not be reused: %s\n"
        "Owner/QA defects to fix:\n- %s\n"
        "Architecture artifact: %s\n"
        "Commit a new HEAD on an isolated branch. Write implementation.json "
        "and %s with the new HEAD. Do not present the Lab prototype as RC-alpha. "
        "Do not stop until the defects above have a concrete product change.\n"
        % (mission.mission_key, profile.harness, profile.model, profile.effort,
           work, repair.get("rejected_head") or "",
           "\n- ".join(str(item) for item in defects) or "(see feedback)",
           architecture.get("artifact") or "", CANDIDATE_MARKER)
    )


def _e2e_prompt(mission: Any, work: Path, candidate_head: str, url: str,
                profile: Profile, architecture: Mapping[str, Any],
                repair: Mapping[str, Any]) -> str:
    arch_e2e = architecture.get("functional_e2e") or architecture.get(
        "functional_e2e_acceptance") or architecture.get("unmet_visual_bar") or ""
    defects = repair.get("defects") or ()
    receipt = work / AG_RECEIPT
    return (
        "You are Factory QA / rendered functional E2E (%s %s / %s).\n"
        "Mission: %s\nCandidate head: %s\nRendered URL: %s\n"
        "Workspace: %s\n"
        "Test the ACTUAL running/rendered integrated candidate that would become "
        "RC-alpha. For browser/visual products, exercise the golden Owner journey "
        "in a real browser and inspect visible behavior.\n"
        "node --test, unit tests, candidate-marker checks, HTTP 200, and a "
        "reachable URL may supplement this run but MUST NOT be treated as PASS.\n"
        "Evaluate at minimum:\n"
        "- persistent/repeated map or render flicker during normal tick/update;\n"
        "- road/map rendering quality against the approved PCP/architecture visual bar;\n"
        "- obvious layout/art regressions in the golden Owner journey;\n"
        "- interaction failures in the Owner-testable flow;\n"
        "- architecture-recorded unmet visual/product acceptance: %s\n"
        "Prior defects: %s\n"
        "Write %s as JSON with keys: mission_key, candidate_head, capability, "
        "runner, model, effort, run_id, scenarios, result (PASS or FAIL), "
        "defects (array), evidence (array), detail.\n"
        "candidate_head MUST equal %s. Do not modify product source. "
        "Do not fake PASS. If the URL is missing or the page is a stub, FAIL.\n"
        % (profile.harness, profile.model, profile.effort,
           mission.mission_key, candidate_head, url or "(missing rendered URL)",
           work, arch_e2e, defects, receipt, candidate_head)
    )


def _export_head(work: Path, dest: Path, head: str) -> bool:
    if not head:
        return False
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", head],
        cwd=work, capture_output=True, timeout=60)
    if archive.returncode != 0 or not archive.stdout:
        return False
    proc = subprocess.run(
        ["tar", "-xf", "-"], cwd=dest, input=archive.stdout,
        capture_output=True, timeout=60)
    return proc.returncode == 0 and any(dest.iterdir())


def _capture_ag_receipt(work: Path, attempts: list[dict[str, Any]],
                        candidate_head: str, mission: Any) -> None:
    if _ag_receipt(work):
        return
    for attempt in reversed(attempts):
        receipt = (attempt.get("receipt") or {})
        text = "%s\n%s" % (receipt.get("stdout_tail") or "",
                           receipt.get("stderr_tail") or "")
        body = _extract_json(text)
        if not body:
            continue
        body.setdefault("candidate_head", candidate_head)
        body.setdefault("mission_key", getattr(mission, "mission_key", ""))
        body.setdefault("result", "FAIL")
        (work / AG_RECEIPT).write_text(
            json.dumps(body, indent=2) + "\n", encoding="utf-8")
        return


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        body = json.loads(text)
        return body if isinstance(body, dict) else {}
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            body = json.loads(text[start:end + 1])
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}
    return {}


def _ag_receipt(work: Path) -> dict[str, Any]:
    path = work / AG_RECEIPT
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


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
