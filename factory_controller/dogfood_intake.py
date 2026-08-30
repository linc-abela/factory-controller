"""Materialize one frozen portfolio mission into a submittable real mission.

The Owner types ``./dev factory run``.  Everything a mission needs -- its
identity, its context manifest, the live admission document the execution layer
validates, the provider candidates, and the command for each declared
acceptance gate -- is derived here from three frozen sources and nothing else:
the run contract, the mission portfolio, and the Bridge's own project registry.
No value below is invented, and every derivation that cannot be shown to follow
from those three raises rather than guessing.

Three refusals are deliberate and fail closed:

* **A mission that changes a repository is not materialized.**  A gate outcome
  for such a mission has to be re-derived at the *candidate* commit, in the
  isolated worktree the execution layer creates, and no layer reports that path
  back to the Controller.  Running the gates against the baseline checkout
  instead would report the mission's own change as untested and call it a pass.
* **A capability the evidence layer does not admit is refused here**, where the
  Owner can read why, rather than at dispatch as an opaque provider refusal.
* **An acceptance gate whose command cannot be derived from its declared source
  is refused.**  ``stage1_adapter`` records an undeclared gate as ``not_run``,
  and ``not_run`` is a failure -- so inventing a command would be the only way
  to turn a real absence into a pass.

This module reads nothing, starts nothing, and holds no clock of its own: every
input arrives as an argument, so the same arguments always produce the same
mission identity.  That is what makes ``./dev factory run`` idempotent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .context import CONTEXT_SCHEMA_VERSION, mission_input_hash, sha256_hex


#: The capabilities the evidence layer's admission guard accepts.  Mirrored
#: from ``factory-evidence-core`` ``src/orchestration/admission.py``
#: ``SUPPORTED_CAPABILITIES``; a project registered for anything else is
#: refused before a provider is contacted rather than after.
ADMISSIBLE_CAPABILITIES = ("development", "prototype")

#: The two decisions the admission guard requires an Owner authority to name.
DECISION_IDS = ("7b", "8b")

#: Fixed vocabulary of the live path.  Anything weaker admits as a fixture and
#: the execution layer then refuses to invoke the real transport.
NATIVE_RECEIPT = "foundation_native_receipt"
OWNER_RATIFICATION = "owner_ratification"
NATIVE_REGISTRY = "foundation_project_registry"
LIVE_ACTION = "live_provider_dispatch"


class IntakeError(Exception):
    """One plain-English reason this mission cannot be materialized."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Intake:
    """Everything one Owner invocation needs, and nothing derived twice."""

    mission_ref: str
    project_id: str
    objective: str
    capability: str
    repository_remote_url: str
    checkout: str
    baseline_sha: str
    idempotency_key: str
    context_manifest_hash: str
    admission: dict[str, Any]
    payload: dict[str, Any]


def iso_utc(when: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def gate_script(acceptance_gate_source: str) -> str:
    """The script a project's gates are declared against.

    The source is ``<remote>@<sha>:<path>``; the path is the evaluator the
    portfolio froze, so it is also the only command this module may name.
    """

    _, _, tail = acceptance_gate_source.rpartition("@")
    _, separator, path = tail.partition(":")
    if not separator or not path.strip():
        raise IntakeError(
            "ACCEPTANCE_GATE_NOT_DERIVABLE",
            "The first-dogfood acceptance gates do not name the evaluator they run.")
    return path.strip()


def gate_commands(gate_ids: Sequence[str], acceptance_gate_source: str,
                  checkout: str) -> dict[str, list[str]]:
    """One argument array per declared gate, derived from its own source."""

    script = gate_script(acceptance_gate_source)
    prefix = script.rsplit("/", 1)[-1] + "-"
    commands: dict[str, list[str]] = {}
    for gate_id in gate_ids:
        if not gate_id.startswith(prefix) or not gate_id[len(prefix):]:
            raise IntakeError(
                "ACCEPTANCE_GATE_NOT_DERIVABLE",
                "Acceptance gate %r does not name a step of the evaluator the "
                "portfolio declared." % gate_id)
        commands[gate_id] = [checkout.rstrip("/") + "/" + script,
                             gate_id[len(prefix):]]
    return commands


def context_manifest(payload_identity: Mapping[str, Any], *,
                     corpus_identity: str, policy_identity: str,
                     selected_refs: Sequence[str]) -> dict[str, Any]:
    """The seven-field manifest the evidence layer re-derives and validates."""

    unique = list(dict.fromkeys(selected_refs))
    unhashed = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "mission_input_hash": mission_input_hash(dict(payload_identity)),
        "corpus_identity": corpus_identity,
        "policy_identity": policy_identity,
        "selected_refs": unique,
        "unresolved_questions": [],
    }
    return {**unhashed, "manifest_hash": sha256_hex(unhashed)}


def _self_hashed(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    body = {name: item for name, item in value.items() if name != field}
    return {**body, field: sha256_hex(body)}


def registry_row(rows: Sequence[Mapping[str, Any]], project_id: str) -> Mapping[str, Any]:
    for row in rows:
        if row.get("project_id") == project_id:
            return row
    raise IntakeError(
        "PROJECT_NOT_REGISTERED",
        "The execution layer does not know the project this mission targets. "
        "Run './dev factory install'.")


def build(mission, *, portfolio_ref: str, run_ref: str,
          registry: Sequence[Mapping[str, Any]], registry_digest: str,
          provider_profiles: Sequence[str], corpus_identity: str,
          owner: str, approval_ref: str, granted_at: float, expires_at: float,
          now: float, stage1: Mapping[str, Any]) -> Intake:
    """Turn one frozen portfolio mission into one submittable real mission."""

    if mission.mutates_repository:
        raise IntakeError(
            "MISSION_CHANGES_A_REPOSITORY",
            "The next mission changes a repository, and its acceptance gates "
            "would have to run against the changed commit. That path is not "
            "built yet, so this command will not start it.")

    row = registry_row(registry, mission.project_id)
    if row.get("resolution") not in (None, "resolved"):
        raise IntakeError(
            "PROJECT_NOT_REGISTERED",
            "The project this mission targets is not available. "
            "Run './dev factory install'.")
    remote_url = row.get("repository_remote_url")
    checkout = row.get("checkout")
    if not isinstance(remote_url, str) or not remote_url:
        raise IntakeError(
            "PROJECT_NOT_REGISTERED",
            "The project this mission targets has no verified repository source.")
    if not isinstance(checkout, str) or not checkout:
        raise IntakeError(
            "PROJECT_CHECKOUT_UNAVAILABLE",
            "The project this mission targets has no local working copy the "
            "acceptance gates could run in.")
    if not mission.acceptance_gate_source.startswith(remote_url + "@"):
        raise IntakeError(
            "ACCEPTANCE_GATE_SOURCE_MISMATCH",
            "The frozen acceptance gates name a different repository than the "
            "one this project is registered for.")

    offered = [name for name in (row.get("capabilities") or ())
               if isinstance(name, str)]
    capability = next((name for name in offered
                       if name in ADMISSIBLE_CAPABILITIES), None)
    if capability is None:
        raise IntakeError(
            "CAPABILITY_NOT_ADMISSIBLE",
            "The evidence layer does not admit the capability this project is "
            "registered for, so the mission cannot be authorized.")
    if not provider_profiles:
        raise IntakeError(
            "NO_PROVIDER_DECLARED",
            "The run contract declares no provider for this mission.")

    identity = {
        "work_item_id": mission.mission_ref,
        "capability": capability,
        "repository_remote_url": remote_url,
        "baseline_sha": mission.baseline_sha,
        "acceptance_gate_ids": list(mission.acceptance_gate_ids),
        "execution_mode": "real",
    }
    manifest = context_manifest(
        identity,
        corpus_identity=corpus_identity,
        policy_identity="%s:%s" % (portfolio_ref, mission.mission_ref),
        selected_refs=[gate_script(mission.acceptance_gate_source)],
    )
    manifest_hash = manifest["manifest_hash"]
    key = "%s:%s" % (mission.mission_ref, manifest_hash)

    dispatch_readiness = _self_hashed({
        "schema_version": "1.0",
        "receipt_id": "dispatch-%s-%s" % (mission.mission_ref.lower(),
                                          registry_digest[:12]),
        "evidence_class": "native_fact",
        "state": "ready",
        "valid_from": iso_utc(granted_at),
        "valid_until": iso_utc(expires_at),
        "authority_kind": NATIVE_RECEIPT,
        "receipt_hash": "",
    }, "receipt_hash")
    authority = _self_hashed({
        "schema_version": "1.0",
        "assertion_id": "owner-authority-%s-%s" % (
            mission.mission_ref.lower(), "-".join(DECISION_IDS)),
        "evidence_class": "human_authority",
        "owner": owner,
        "question": "May the Factory execute %s against %s on the live "
                    "execution path?" % (mission.mission_ref, mission.project_id),
        "options": [LIVE_ACTION, "refuse"],
        "chosen_action": LIVE_ACTION,
        "decision_ids": list(DECISION_IDS),
        "authority_kind": OWNER_RATIFICATION,
        "assertion_hash": "",
    }, "assertion_hash")

    admission = {
        "schema_version": "1.0",
        "request": {
            "schema_version": "1.0",
            "work_item_id": mission.mission_ref,
            "capability": capability,
            "repository_remote_url": remote_url,
            "baseline_sha": mission.baseline_sha,
            "context_manifest_hash": manifest_hash,
            "acceptance_gate_ids": list(mission.acceptance_gate_ids),
            "idempotency_key": key,
        },
        "admission_evidence": {
            "schema_version": "1.0",
            "evaluated_at": iso_utc(now),
            "trusted_dispatch": dispatch_readiness,
            "human_authority": authority,
            "work_item": {
                "schema_version": "1.0",
                "work_item_id": mission.mission_ref,
                "origin": "laboratory",
                "authority_identity": "contract://%s/%s"
                                      % (run_ref, mission.mission_ref),
            },
            "context_manifest": manifest,
            "project_registration": {
                "schema_version": "1.0",
                "project_id": mission.project_id,
                "repository_remote_url": remote_url,
                "registered": True,
                "authority_kind": NATIVE_REGISTRY,
                "registry_hash": registry_digest,
            },
            "supported_capabilities": [capability],
            "admitted_baseline_sha": mission.baseline_sha,
        },
    }

    payload = {
        **identity,
        "project_id": mission.project_id,
        "repository": remote_url,
        "context_manifest_hash": manifest_hash,
        "work_class": mission.work_class,
        "environment_class": mission.environment_class,
        "portfolio_ref": portfolio_ref,
        "policy_version": run_ref,
        "approval_ref": approval_ref,
        "provider_candidates": [{"profile": profile,
                                 "capabilities": [capability]}
                                for profile in provider_profiles],
        "stage1": {
            **dict(stage1),
            "mode": "real",
            "operator_opt_in": True,
            "repository": checkout,
            "gate_workdir": checkout,
            "gate_commands": gate_commands(
                mission.acceptance_gate_ids,
                mission.acceptance_gate_source, checkout),
        },
    }
    return Intake(
        mission_ref=mission.mission_ref, project_id=mission.project_id,
        objective=mission.objective, capability=capability,
        repository_remote_url=remote_url, checkout=checkout,
        baseline_sha=mission.baseline_sha, idempotency_key=key,
        context_manifest_hash=manifest_hash, admission=admission,
        payload=payload,
    )


__all__ = ["Intake", "IntakeError", "build", "context_manifest",
           "gate_commands", "gate_script", "iso_utc", "registry_row"]
