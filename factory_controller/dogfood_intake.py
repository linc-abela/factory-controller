"""Materialize one frozen portfolio mission into a submittable real mission.

The Owner types ``./dev factory run``.  Everything a mission needs -- its
identity, its context manifest, the live admission document the execution layer
validates, the provider candidates, and the command for each declared
acceptance gate -- is derived here from three frozen sources and nothing else:
the run contract, the mission portfolio, and the Bridge's own project registry.
No value below is invented, and every derivation that cannot be shown to follow
from those three raises rather than guessing.

Two derivations still refuse deliberately and fail closed:

* **A mission that changes a repository is materialized with candidate-targeted
  gates.**  The execution layer returns an immutable candidate commit, and
  ``stage1_adapter`` materializes a detached checkout of that commit before
  running any declared evaluator.  Running the gates against the baseline
  checkout instead would report the mission's own change as untested and call
  it a pass.
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
from typing import Any, Callable, Mapping, Sequence

from .context import (
    CONTEXT_SCHEMA_VERSION, ContextBudget, ContextError, ContextPackage,
    ContextRequest, mission_input_hash, sha256_hex, verify,
)


#: The capabilities the evidence layer's admission guard accepts.  Mirrored
#: from ``factory-evidence-core`` ``src/orchestration/admission.py``
#: ``SUPPORTED_CAPABILITIES``; a project registered for anything else is
#: refused before a provider is contacted rather than after.
ADMISSIBLE_CAPABILITIES = ("bug", "development", "prototype")

#: The two decisions the admission guard requires an Owner authority to name.
DECISION_IDS = ("7b", "8b")

#: Fixed vocabulary of the live path.  Anything weaker admits as a fixture and
#: the execution layer then refuses to invoke the real transport.
NATIVE_RECEIPT = "foundation_native_receipt"
OWNER_RATIFICATION = "owner_ratification"
NATIVE_REGISTRY = "foundation_project_registry"
LIVE_ACTION = "live_provider_dispatch"

# The first dogfood path asks for a useful but finite repository picture.  The
# Broker owns classification and selection; these names are only the request
# contract carried into that seam.
DOGFOOD_CONTEXT_OVERVIEW = ("authoritative", "runtime", "execution", "tests")
DOGFOOD_CONTEXT_BUDGET = {"max_bytes": 200_000, "max_files": 40}


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
    attempt: int
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


def context_request(payload_identity: Mapping[str, Any], *,
                    corpus_identity: str, policy_identity: str) -> dict[str, Any]:
    """Declare the bounded Broker view for one frozen dogfood mission."""

    return {
        "corpus_identity": corpus_identity,
        "policy_identity": policy_identity,
        "repository_remote_url": payload_identity["repository_remote_url"],
        "baseline_sha": payload_identity["baseline_sha"],
        "required_anchors": ["MISSION.md"],
        "overview": list(DOGFOOD_CONTEXT_OVERVIEW),
    }


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


def gate_expectations(mission: Any) -> dict[str, dict[str, Any]]:
    """Serialize the portfolio's explicit expected-failure policy."""

    values = getattr(mission, "acceptance_gate_expectations", ())
    if isinstance(values, Mapping):
        return {str(gate): dict(expectation)
                for gate, expectation in values.items()
                if isinstance(expectation, Mapping)}
    return {
        item.gate_id: {"passed": item.passed, "exit_code": item.exit_code}
        for item in values
        if hasattr(item, "gate_id") and hasattr(item, "exit_code")
    }


def build(mission, *, portfolio_ref: str, run_ref: str,
          registry: Sequence[Mapping[str, Any]], registry_digest: str,
          provider_profiles: Sequence[str], corpus_identity: str,
          owner: str, approval_ref: str, granted_at: float, expires_at: float,
          now: float, stage1: Mapping[str, Any], attempt: int = 1,
          context_builder: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None
          ) -> Intake:
    """Turn one frozen portfolio mission into one submittable real mission.

    ``attempt`` is the slot's attempt number, counting the first.  It reaches
    the mission's identity through the context manifest's ``policy_identity``
    and nowhere else, which is what makes a retry a distinct mission without
    inventing a second identity scheme:

    * ``work_item_id`` stays the portfolio reference.  The slot *is* the work
      item; a second name for it would be a second work item.
    * the manifest hash therefore differs per attempt, so the idempotency key
      ``<ref>:<manifest_hash>`` is unique per attempt while still carrying the
      shape ``routing.expected_idempotency_key`` mandates and Evidence Core
      refuses anything else for.
    * because the key differs, the execution layer's memo of the previous
      attempt is neither replayed nor overwritten -- a stored refusal answers
      forever for the key it was stored under, which is exactly why a retry may
      not reuse one.

    Attempt 1 is byte-identical to what this module produced before retries
    existed, so the identity of a mission already in the ledger cannot move
    under it.
    """

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

    expectations = gate_expectations(mission)
    identity = {
        "work_item_id": mission.mission_ref,
        "capability": capability,
        "repository_remote_url": remote_url,
        "baseline_sha": mission.baseline_sha,
        "acceptance_gate_ids": list(mission.acceptance_gate_ids),
        "acceptance_gate_expectations": expectations,
        "execution_mode": "real",
    }
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IntakeError(
            "MISSION_ATTEMPT_INVALID",
            "A mission attempt is counted from one; this one was not.")
    policy_identity = "%s:%s" % (portfolio_ref, mission.mission_ref)
    if attempt > 1:
        policy_identity += "#%d" % attempt
    declared_context = context_request(
        identity, corpus_identity=corpus_identity,
        policy_identity=policy_identity)
    context_payload = {
        **identity,
        "context_request": declared_context,
        "context_budget": dict(DOGFOOD_CONTEXT_BUDGET),
    }
    try:
        request = ContextRequest.from_payload(context_payload)
        budget = ContextBudget.from_payload(context_payload)
    except ContextError as exc:
        raise IntakeError(
            "CONTEXT_REQUEST_INVALID",
            "The dogfood repository-grounding request is invalid: %s" % exc) from None
    selected_refs = [gate_script(mission.acceptance_gate_source)]
    package = None
    if context_builder is not None:
        try:
            package = ContextPackage.from_response(context_builder(request.as_wire()))
        except Exception as exc:  # noqa: BLE001
            raise IntakeError(
                "CONTEXT_PREFLIGHT_FAILED",
                "The Context Broker preflight could not be completed (%s)."
                % type(exc).__name__) from None
        refusal = verify(request, package, budget=budget, now=now)
        if refusal:
            raise IntakeError(
                refusal,
                "The Context Broker did not produce an admissible repository "
                "grounding package (%s)." % refusal)
        if package.manifest is None:
            raise IntakeError(
                "CONTEXT_MANIFEST_MISSING",
                "The Context Broker preflight returned no repository manifest.")
        selected_refs = list(package.manifest.selected_refs)
    manifest = context_manifest(
        identity,
        corpus_identity=corpus_identity,
        policy_identity=policy_identity,
        selected_refs=selected_refs,
    )
    manifest_hash = manifest["manifest_hash"]
    if package is not None and package.manifest is not None \
            and package.manifest.manifest_hash != manifest_hash:
        raise IntakeError(
            "CONTEXT_PREFLIGHT_MISMATCH",
            "The Context Broker package did not match the mission manifest identity.")
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
        # The slot's lineage, durable and readable without recomputing a hash.
        # `work_item_id` already names the slot; this says which try it is.
        "attempt": attempt,
        "project_id": mission.project_id,
        "repository": remote_url,
        "context_manifest_hash": manifest_hash,
        "context_request": declared_context,
        "context_budget": dict(DOGFOOD_CONTEXT_BUDGET),
        "acceptance_gate_expectations": expectations,
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
            "mutates_repository": mission.mutates_repository,
            "gate_expectations": expectations,
            "repository": checkout,
            "gate_workdir": checkout,
            "gate_commands": gate_commands(
                mission.acceptance_gate_ids,
                mission.acceptance_gate_source, checkout),
        },
    }
    return Intake(
        mission_ref=mission.mission_ref, attempt=attempt,
        project_id=mission.project_id,
        objective=mission.objective, capability=capability,
        repository_remote_url=remote_url, checkout=checkout,
        baseline_sha=mission.baseline_sha, idempotency_key=key,
        context_manifest_hash=manifest_hash, admission=admission,
        payload=payload,
    )


__all__ = ["DOGFOOD_CONTEXT_BUDGET", "DOGFOOD_CONTEXT_OVERVIEW", "Intake",
           "IntakeError", "build", "context_manifest", "context_request",
           "gate_commands", "gate_expectations", "gate_script", "iso_utc",
           "registry_row"]
