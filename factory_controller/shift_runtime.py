"""Crash-safe on-demand shift checkpoints, drain, and cold-start recovery.

This module is deliberately a projection and a bounded operator act. Mission
state, steps, runs, capacity observations, context bindings, evidence pointers,
and Work Batons remain owned by their existing ledgers. A shift checkpoint is
re-derived from those records and an append-only coordination observation may
record the exact snapshot that was shown to an operator. There is no second
mutable shift state table and no conversational handoff.

The supervisor remains the owner of the durable operating control state. This
module composes with it for suspend and drain, while the Controller remains the
only path that can claim or advance a mission.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import capacity
from . import continuity
from . import context as context_contract
from . import supervisor
from .store import (DISPATCH_STEP, effective_dispatch_step)
from .store import TERMINAL as LEDGER_TERMINAL
from .store import MissionStore, canonical_json


CONTRACT_VERSION = "factory-controller/shift-runtime/1.0"
CHECKPOINT_SCHEMA = "factory.controller.shift_checkpoint.v1"

CANONICAL_ABSENCE = frozenset({
    "unknown", "not_applicable", "not_run", "not_measurable",
})

MISSION_STEPS = capacity.MISSION_STEPS
# The ledger owns ordinary terminal mission states.  Runtime adds only its
# explicit escalation state, so this projection cannot silently drift when the
# ledger vocabulary changes.
TERMINAL_STATES = frozenset(LEDGER_TERMINAL) | {"escalated"}

CHECKPOINT_FIELDS = (
    "schema_version", "mission_id", "project_id", "work_item_id",
    "repository", "baseline_sha", "candidate_sha", "mission_state",
    "recovery_class", "completed_steps", "next_safe_step", "safe_boundary",
    "resume_target", "idempotency_key", "operation_keys", "step_states",
    "context", "evidence", "capacity_observation", "repository_pin",
    "runtime", "lane", "work_baton", "uncertainty", "unresolved_blockers",
    "source_updated_at",
)

SAFE_RECOVERY_CLASSES = frozenset({
    "pending", "capacity_deferred", "pre_dispatch_replayable",
    "post_dispatch_recovery", "in_flight", "uncertain_dispatch",
    "terminal", "repair_required",
})

MAX_DRAIN_STEPS = 100


class ShiftRefusal(ValueError):
    """A checkpoint, recovery, or operator request that fails closed."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra

    def as_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            **self.extra,
        }


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _absent(value: Any, word: str = "unknown") -> Any:
    return word if value is None else value


def _string(value: Any, default: str = "unknown") -> str:
    return value if isinstance(value, str) and value else default


def _profiles(payload: Mapping[str, Any]) -> tuple[str, ...]:
    result = []
    for entry in payload.get("provider_candidates") or ():
        profile = entry if isinstance(entry, str) else (
            entry.get("profile") if isinstance(entry, Mapping) else None)
        if isinstance(profile, str) and profile and profile not in result:
            result.append(profile)
    return tuple(result)


def _timestamp(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _max_timestamp(values: Iterable[Any], fallback: float) -> float:
    numbers = [value for value in (_timestamp(item) for item in values)
               if value is not None]
    return max(numbers or [fallback])


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw


@dataclass(frozen=True)
class ShiftCheckpoint:
    """An immutable, content-addressed reading of one mission's state."""

    body: Mapping[str, Any]
    checkpoint_id: str
    checkpoint_hash: str

    @classmethod
    def build(cls, body: Mapping[str, Any]) -> "ShiftCheckpoint":
        value = dict(body)
        value["schema_version"] = value.get("schema_version", CHECKPOINT_SCHEMA)
        missing = sorted(set(CHECKPOINT_FIELDS) - set(value))
        if missing:
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_FIELD_MISSING",
                "checkpoint is missing fields: %s" % missing,
            )
        if value["schema_version"] != CHECKPOINT_SCHEMA:
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_SCHEMA_UNSUPPORTED",
                "unsupported shift checkpoint schema",
            )
        if value["recovery_class"] not in SAFE_RECOVERY_CLASSES:
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_RECOVERY_CLASS_INVALID",
                "unknown recovery class %r" % value["recovery_class"],
            )
        digest = _digest(value)
        return cls(value, "sc_" + digest[:32], digest)

    @classmethod
    def from_row(cls, value: Any) -> "ShiftCheckpoint":
        if not isinstance(value, Mapping):
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_MALFORMED",
                "shift checkpoint must be an object",
            )
        missing = sorted(set(CHECKPOINT_FIELDS) - set(value))
        if missing:
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_FIELD_MISSING",
                "checkpoint is missing fields: %s" % missing,
            )
        body = {key: value[key] for key in CHECKPOINT_FIELDS}
        if body["schema_version"] != CHECKPOINT_SCHEMA:
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_SCHEMA_UNSUPPORTED",
                "unsupported shift checkpoint schema",
            )
        digest = _digest(body)
        if (
            value.get("checkpoint_hash") != digest
            or value.get("checkpoint_id") != "sc_" + digest[:32]
        ):
            raise ShiftRefusal(
                "SHIFT_CHECKPOINT_FORGED",
                "checkpoint identity does not match its facts",
            )
        return cls(body, "sc_" + digest[:32], digest)

    def as_row(self) -> dict[str, Any]:
        return {
            **dict(self.body),
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_hash": self.checkpoint_hash,
            "contract_version": CONTRACT_VERSION,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_row()[key]


def validate_checkpoint(value: Any) -> dict[str, Any]:
    """Validate an untrusted checkpoint without consulting live state."""

    checkpoint = ShiftCheckpoint.from_row(value)
    row = checkpoint.as_row()
    if row["safe_boundary"] not in capacity.CHECKPOINT_BOUNDARIES:
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_BOUNDARY_UNSAFE",
            "unknown or unsafe checkpoint boundary",
        )
    if row["resume_target"] not in capacity.RESUME_TARGETS:
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_RESUME_TARGET_INVALID",
            "unknown checkpoint resume target",
        )
    boundary = row["safe_boundary"]
    effect = row["uncertainty"].get("irreversible_effect") \
        if isinstance(row["uncertainty"], Mapping) else None
    expected = {
        "pre_dispatch": ("none", "resume_next_step"),
        "post_dispatch_reconciled": ("reconciled", "resume_next_step"),
        "post_dispatch_unreconciled": ("unknown", "reconcile_uncertain_dispatch"),
    }[boundary]
    if (effect, row["resume_target"]) != expected:
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_BOUNDARY_INCONSISTENT",
            "checkpoint boundary and uncertainty do not agree",
        )
    if not isinstance(row["idempotency_key"], str) or not row["idempotency_key"]:
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_IDEMPOTENCY_INVALID",
            "checkpoint must carry the mission idempotency key",
        )
    if not isinstance(row["operation_keys"], list):
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_OPERATION_KEYS_INVALID",
            "operation_keys must be a list",
        )
    if not all(isinstance(item, str) and item for item in row["operation_keys"]):
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_OPERATION_KEYS_INVALID",
            "operation_keys must contain non-empty strings",
        )
    uncertainty = row["uncertainty"]
    if (
        not isinstance(uncertainty, Mapping)
        or uncertainty.get("irreversible_effect") not in {
            "none", "unknown", "reconciled",
        }
    ):
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_UNCERTAINTY_INVALID",
            "irreversible effect uncertainty must be explicit",
        )
    if (
        not isinstance(row["source_updated_at"], (int, float))
        or isinstance(row["source_updated_at"], bool)
    ):
        raise ShiftRefusal(
            "SHIFT_CHECKPOINT_TIMESTAMP_INVALID",
            "source_updated_at must be numeric",
        )
    return row


def _step_value(steps: Sequence[Mapping[str, Any]], name: str,
                field: str) -> tuple[Any, bool]:
    row = next((item for item in steps if item.get("name") == name), None)
    if row is None or row.get("status") != "COMPLETED":
        return None, False
    if row.get("corrupt_%s" % field):
        return None, True
    return row.get(field), False


def _context_refs(store: MissionStore, mission: Mapping[str, Any],
                  steps: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    payload = mission.get("payload") or {}
    declared = payload.get("context_manifest_hash")
    raw, corrupt = _step_value(steps, "context", "output")
    if corrupt:
        return {
            "state": "corrupt",
            "manifest_hash": "unknown",
            "corpus_identity": "unknown",
            "policy_identity": "unknown",
            "selected_refs": [],
            "unresolved_questions": [],
        }, ["CONTEXT_REF_CORRUPT"]
    if raw is None:
        return {
            "state": "not_run" if payload.get("context_request") else "not_applicable",
            "manifest_hash": _absent(declared, "not_applicable"),
            "corpus_identity": "unknown",
            "policy_identity": "unknown",
            "selected_refs": [],
            "unresolved_questions": [],
        }, []
    if not isinstance(raw, Mapping):
        return {
            "state": "corrupt",
            "manifest_hash": "unknown",
            "corpus_identity": "unknown",
            "policy_identity": "unknown",
            "selected_refs": [],
            "unresolved_questions": [],
        }, ["CONTEXT_REF_CORRUPT"]
    manifest = raw.get("manifest")
    if not isinstance(manifest, Mapping):
        return {
            "state": _string(raw.get("status"), "unknown"),
            "manifest_hash": _absent(declared, "not_applicable"),
            "corpus_identity": "unknown",
            "policy_identity": "unknown",
            "selected_refs": [],
            "unresolved_questions": [],
        }, [] if raw.get("status") == "unavailable" else ["CONTEXT_REF_MISSING"]
    selected = manifest.get("selected_refs")
    questions = manifest.get("unresolved_questions")
    selected = list(selected) if isinstance(selected, list) else []
    questions = list(questions) if isinstance(questions, list) else []
    result = {
        "state": "bound",
        "manifest_hash": _string(manifest.get("manifest_hash")),
        "corpus_identity": _string(manifest.get("corpus_identity")),
        "policy_identity": _string(manifest.get("policy_identity")),
        "selected_refs": [
            item for item in selected[:64]
            if isinstance(item, str) and item
        ],
        "unresolved_questions": [
            item for item in questions[:64]
            if isinstance(item, str) and item
        ],
    }
    measurement = raw.get("measurement")
    if isinstance(measurement, Mapping):
        result["measurement"] = {
            "head_sha": _absent(measurement.get("head_sha")),
            "repository_remote_url": _absent(
                measurement.get("repository_remote_url"),
                "not_applicable",
            ),
            "built_at": _absent(measurement.get("built_at")),
            "selected_context_bytes": _absent(
                measurement.get("selected_context_bytes"),
                "not_measurable",
            ),
            "selected_context_files": _absent(
                measurement.get("selected_context_files"),
                "not_measurable",
            ),
        }
    blockers = []
    if declared and result["manifest_hash"] != declared:
        blockers.append("CONTEXT_REF_MISMATCH")
    try:
        package = context_contract.package_from_row(dict(raw))
        if package.manifest is None or not package.manifest.intact:
            blockers.append("CONTEXT_REF_INTACTNESS_FAILED")
        elif (
            package.manifest.mission_input_hash
            != context_contract.mission_input_hash(dict(payload))
        ):
            blockers.append("CONTEXT_REF_WRONG_MISSION")
    except (TypeError, ValueError, KeyError):
        blockers.append("CONTEXT_REF_CORRUPT")
    return result, blockers


def _evidence_refs(store: MissionStore, mission: Mapping[str, Any],
                   steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw, corrupt = _step_value(steps, "evidence", "output")
    if corrupt:
        return {
            "state": "corrupt",
            "evidence_pointer": "unknown",
            "receipt_ref": "unknown",
            "measurement_ref": "unknown",
        }
    if not isinstance(raw, Mapping):
        result = mission.get("result") or {}
        raw = result.get("evidence") if isinstance(result, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "state": "accepted" if raw.get("accepted") is True else (
            "rejected" if raw else "not_run"),
        "evidence_pointer": _absent(raw.get("evidence_pointer"), "not_run"),
        "receipt_ref": _absent(raw.get("receipt_ref"), "not_applicable"),
        "measurement_ref": _absent(raw.get("measurement_ref"), "not_applicable"),
    }


def _baton_refs(store: MissionStore, mission: Mapping[str, Any],
                repository_pin: Mapping[str, Any]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    payload = mission.get("payload") or {}
    explicit = payload.get("work_baton_id") or payload.get("baton_id")
    try:
        with store.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_batons'"
            ).fetchone()
            if exists is None:
                return {"state": "not_issued", "references": []}, [], []
            if explicit:
                rows = db.execute(
                    "SELECT * FROM work_batons WHERE baton_id=?", (explicit,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM work_batons ORDER BY created_at,baton_id").fetchall()
    except sqlite3.Error:
        return {"state": "unreadable", "references": []}, ["BATON_LEDGER_UNREADABLE"], []

    references = []
    blockers = []
    raw_rows = []
    expected_project = mission.get("project_id")
    expected_ids = {mission.get("id"), mission.get("idempotency_key"),
                    payload.get("run_id"), payload.get("work_item_id")}
    for row in rows:
        encoded = _safe_json(row["payload_json"])
        if not isinstance(encoded, Mapping):
            blockers.append("BATON_CORRUPT")
            continue
        if not explicit and (
                encoded.get("project_id") != expected_project
                or not ({encoded.get("run_id"), encoded.get("idempotency_key")}
                        & expected_ids)):
            continue
        raw_rows.append((row, encoded))
        try:
            continuity.validate(dict(encoded))
        except continuity.BatonRefusal as refusal:
            blockers.append(refusal.code)
            continue
        if encoded.get("project_id") != expected_project:
            blockers.append("BATON_CROSS_PROJECT")
        if (repository_pin.get("head_sha") not in (None, "unknown")
                and encoded.get("head_sha") != repository_pin["head_sha"]):
            blockers.append("BATON_STALE_HEAD")
        references.append({
            "baton_id": row["baton_id"],
            "baton_hash": encoded.get("baton_hash", "unknown"),
            "state": row["state"],
            "project_id": encoded.get("project_id", "unknown"),
            "run_id": encoded.get("run_id", "unknown"),
            "lane_id": encoded.get("lane_id", "unknown"),
            "head_sha": encoded.get("head_sha", "unknown"),
            "safe_boundary": encoded.get("safe_boundary", "unknown"),
            "compatible_profiles": list(encoded.get("compatible_profiles") or ()),
            "consumed_by": _absent(row["consumed_by"]),
        })
    if explicit and not raw_rows:
        blockers.append("BATON_NOT_ISSUED")
    states = {item["state"] for item in references}
    state = "consumed" if "consumed" in states else (
        "issued" if "issued" in states else "not_issued")
    return {"state": state, "references": references}, sorted(set(blockers)), raw_rows


def _lane(payload: Mapping[str, Any], baton_rows: Sequence[tuple[Any, Mapping[str, Any]]]) -> dict[str, Any]:
    baton = baton_rows[-1][1] if baton_rows else {}
    return {
        "lane_id": _string(payload.get("lane_id"), _string(baton.get("lane_id"))),
        "worktree": _string(payload.get("worktree"), _string(baton.get("worktree"))),
        "branch": _string(payload.get("branch"), _string(baton.get("branch"))),
    }


def _route_facts(store: MissionStore, mission: Mapping[str, Any],
                 steps: Sequence[Mapping[str, Any]],
                 checkpoint_facts: Mapping[str, Any]) -> dict[str, Any]:
    payload = mission.get("payload") or {}
    history = store.route_history(mission["id"])
    legs = history.get("legs") or []
    selected = history.get("selected_provider_profile")
    _, dispatch = effective_dispatch_step(steps)
    pending_route = {}
    if isinstance(dispatch, Mapping) and isinstance(dispatch.get("input"), Mapping):
        pending_route = dispatch["input"].get("route") or {}
    pending_profile = pending_route.get("provider_profile")
    declared = list(_profiles(payload))
    compatible = list(checkpoint_facts.get("compatible_profiles") or ())
    if not compatible and not legs:
        compatible = declared
    return {
        "execution_mode": payload.get("execution_mode", "fixture"),
        "declared_profiles": declared,
        "compatible_profiles": compatible,
        "selected_profile": _absent(selected, "not_run"),
        "pending_profile": _absent(pending_profile, "not_applicable"),
        "last_leg": None if not legs else {
            "attempt": legs[-1].get("attempt"),
            "leg": legs[-1].get("leg"),
            "provider_profile": _absent(legs[-1].get("provider_profile")),
            "outcome": _string(legs[-1].get("outcome")),
            "process_started": legs[-1].get("process_started"),
            "idempotency_key": legs[-1].get("idempotency_key"),
        },
    }


def _recovery_class(mission: Mapping[str, Any], steps: Sequence[Mapping[str, Any]],
                    checkpoint_facts: Mapping[str, Any]) -> str:
    state = mission.get("state")
    blockers = checkpoint_facts.get("unresolved_blockers") or ()
    if any(str(item).startswith(("CONTEXT_", "BATON_", "SHIFT_"))
           for item in blockers):
        return "repair_required"
    if state in TERMINAL_STATES:
        return "terminal"
    if state == "admitted":
        return "capacity_deferred" if mission.get("deferrals", 0) else "pending"
    if state == "dispatching":
        if mission.get("lease_token"):
            return "in_flight"
        if checkpoint_facts.get("uncertainty", {}).get("irreversible_effect") == "unknown":
            return "uncertain_dispatch"
        if checkpoint_facts.get("uncertainty", {}).get("irreversible_effect") == "reconciled":
            return "post_dispatch_recovery"
        return "pre_dispatch_replayable"
    if state in {"dispatched", "candidate_verified", "evaluated", "evidence_sealed"}:
        return "in_flight" if mission.get("lease_token") else "post_dispatch_recovery"
    return "repair_required"


def _checkpoint_from_store(store: MissionStore, mission_id: str) -> ShiftCheckpoint:
    mission = store.get(mission_id)
    if mission is None:
        raise ShiftRefusal("SHIFT_MISSION_UNKNOWN", "unknown mission %s" % mission_id,
                           mission_id=mission_id)
    steps = store.step_records(mission_id)
    statuses = {row["name"]: row.get("status") for row in steps}
    dispatch_name, _ = effective_dispatch_step(steps)
    # The Owner's stage line names the stage that is live.  A mission recovered
    # from a settled dispatch refusal has that refusal row for ever, so reading
    # the literal name would report `dispatch (complete)` for a dispatch that
    # has not been attempted since -- the exact sentence SF-164 was handed.
    statuses[DISPATCH_STEP] = statuses.get(dispatch_name)
    step_states = {name: statuses.get(name) or "NOT_STARTED" for name in MISSION_STEPS
                   if name != "context" or (mission.get("payload") or {}).get("context_request")}
    for row in steps:
        if row["name"] not in step_states:
            step_states[row["name"]] = row.get("status", "UNKNOWN")
    dispatch, dispatch_corrupt = _step_value(steps, dispatch_name, "output")
    dispatch = dispatch if isinstance(dispatch, Mapping) else {}
    evidence = _evidence_refs(store, mission, steps)
    project = store.project(mission.get("project_id")) if mission.get("project_id") else None
    payload = mission.get("payload") or {}
    repository = (
        (None if project is None else project.repository)
        or payload.get("repository_remote_url")
        or payload.get("repository")
    )
    legs = store.runs(mission_id)
    reading = None
    declared = _profiles(payload)
    if declared:
        readings = store.capacity_readings()
        selected = next((leg.get("provider_profile") for leg in reversed(legs)
                         if leg.get("process_started") is not False), None)
        reading = readings.get(selected) if selected else next(
            (readings.get(profile) for profile in declared if profile in readings),
            None,
        )
    if any(row.get("corrupt_output") for row in steps):
        # ``MissionStore.capacity_checkpoint`` intentionally assumes its
        # existing projections are valid. A shift status read must instead
        # remain useful when an operator is investigating a damaged row, so
        # re-derive the shape without trusting the damaged JSON fields.
        facts = capacity.checkpoint_facts(
            mission, payload, statuses, legs, reading,
            repository=repository,
            candidate_sha=None if dispatch_corrupt else dispatch.get("candidate_sha"),
            evidence_pointer=None,
        )
    else:
        facts = store.capacity_checkpoint(
            mission_id,
            reading,
        )
    context, context_blockers = _context_refs(store, mission, steps)
    candidate = dispatch.get("candidate_sha")
    context_head = (
        (context.get("measurement") or {}).get("head_sha")
        if isinstance(context.get("measurement"), Mapping) else None
    )
    repository_pin = {
        "remote_url": _absent(repository, "not_applicable"),
        "baseline_sha": _absent(payload.get("baseline_sha")),
        "candidate_sha": _absent(candidate, "not_run"),
        "head_sha": _absent(payload.get("head_sha") or context_head,
                             "unknown"),
    }
    baton, baton_blockers, baton_rows = _baton_refs(store, mission, repository_pin)
    lane = _lane(payload, baton_rows)
    route = _route_facts(store, mission, steps, facts)
    uncertainty = dict(facts.get("uncertainty") or {})
    dispatch_status = statuses.get(dispatch_name)
    if (mission.get("state") == "dispatching"
            and dispatch_status == "STARTED"
            and not legs):
        uncertainty = {
            **uncertainty,
            "irreversible_effect": "unknown",
            "unproven_legs": 1,
            "detail": "dispatch operation started without a durable receipt",
        }
        facts = {
            **facts,
            "safe_boundary": "post_dispatch_unreconciled",
            "resume_target": "reconcile_uncertain_dispatch",
            "compatible_profiles": [],
            "uncertainty": uncertainty,
            "unresolved_blockers": sorted(set(
                list(facts.get("unresolved_blockers") or ())
                + ["UNCERTAIN_DISPATCH_LEG"])),
        }
    elif (
        dispatch_status == "COMPLETED"
        and any(leg.get("process_started") is True for leg in legs)
        and all(leg.get("process_started") is not None for leg in legs)
    ):
        # A completed dispatch step plus durable, non-ambiguous run receipts is
        # a reconciled post-dispatch boundary. The generic capacity projection
        # is intentionally conservative for baton issuance, but a shift resume
        # still needs to distinguish this known receipt from a lost call.
        facts = {
            **facts,
            "safe_boundary": "post_dispatch_reconciled",
            "resume_target": "resume_next_step",
            "uncertainty": {
                **dict(facts.get("uncertainty") or {}),
                "irreversible_effect": "reconciled",
                "unproven_legs": 0,
            },
            "unresolved_blockers": [
                blocker for blocker in facts.get("unresolved_blockers") or ()
                if blocker != "UNCERTAIN_DISPATCH_LEG"
            ],
        }
    source_times = [mission.get("created_at"), mission.get("updated_at")]
    source_times.extend(row.get("updated_at") for row in steps)
    source_times.extend(row.get("created_at") for row in legs)
    source_times.extend(event.get("created_at") for event in store.history(mission_id))
    if isinstance(facts.get("capacity_observation"), Mapping):
        source_times.append(facts["capacity_observation"].get("observed_at"))
    if dispatch_corrupt:
        context_blockers.append("SHIFT_DISPATCH_OUTPUT_CORRUPT")
    if any(row.get("corrupt_input") or row.get("corrupt_output") for row in steps):
        context_blockers.append("SHIFT_STEP_RECORD_CORRUPT")
    blockers = sorted(set(
        list(facts.get("unresolved_blockers") or ())
        + context_blockers + baton_blockers
    ))
    facts = {**facts, "unresolved_blockers": blockers}
    uncertainty = dict(facts.get("uncertainty") or uncertainty)
    operation_keys = [
        row["operation_key"] for row in sorted(
            steps,
            key=lambda item: (MISSION_STEPS.index(item["name"])
                              if item["name"] in MISSION_STEPS else len(MISSION_STEPS),
                              item["name"]),
        )
        if isinstance(row.get("operation_key"), str)
    ]
    body = {
        "schema_version": CHECKPOINT_SCHEMA,
        "mission_id": mission_id,
        "project_id": _absent(mission.get("project_id"), "not_applicable"),
        "work_item_id": _absent(payload.get("work_item_id")),
        "repository": repository_pin["remote_url"],
        "baseline_sha": repository_pin["baseline_sha"],
        "candidate_sha": repository_pin["candidate_sha"],
        "mission_state": mission.get("state"),
        "recovery_class": _recovery_class(mission, steps, facts),
        "completed_steps": list(facts.get("completed_steps") or ()),
        "next_safe_step": facts.get("next_safe_step", "not_applicable"),
        "safe_boundary": facts.get("safe_boundary"),
        "resume_target": facts.get("resume_target"),
        "idempotency_key": mission.get("idempotency_key"),
        "operation_keys": operation_keys,
        "step_states": step_states,
        "context": context,
        "evidence": evidence,
        "capacity_observation": facts.get("capacity_observation"),
        "repository_pin": repository_pin,
        "runtime": route,
        "lane": lane,
        "work_baton": baton,
        "uncertainty": uncertainty,
        "unresolved_blockers": blockers,
        "source_updated_at": _max_timestamp(source_times, mission["created_at"]),
    }
    checkpoint = ShiftCheckpoint.build(body)
    return checkpoint


class ShiftRuntime:
    """One finite shift act over the existing Controller and supervisor."""

    def __init__(self, controller, *, supervisor_plane=None) -> None:
        self.controller = controller
        self.store: MissionStore = controller.store
        self.supervisor = supervisor_plane or supervisor.OperationsSupervisor(
            controller, clock=self.store.clock)

    # -- deterministic readings ----------------------------------------- #

    def checkpoint(self, mission_id: str) -> dict[str, Any]:
        return _checkpoint_from_store(self.store, mission_id).as_row()

    def checkpoints(self, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for mission in self.store.all_missions():
            if project_id is not None and mission.get("project_id") != project_id:
                continue
            rows.append(self.checkpoint(mission["id"]))
        return rows

    def _schedule_preview(self, *, project_id: str | None = None,
                          mission_id: str | None = None) -> dict[str, Any]:
        """Return a queue preview narrowed before it crosses a scope boundary."""

        preview = self.store.schedule_preview()
        if project_id is None and mission_id is None:
            return preview
        allowed = {
            row["mission_id"] for row in preview.get("considered", ())
            if (project_id is None or row.get("project_id") == project_id)
            and (mission_id is None or row.get("mission_id") == mission_id)
        }
        narrowed = [
            row for row in preview.get("considered", ())
            if row.get("mission_id") in allowed
        ]
        selected = preview.get("selected")
        return {
            **preview,
            "selected": selected if selected in allowed else None,
            "reason": preview.get("reason")
            if selected in allowed else "NO_MISSION_IN_SCOPE",
            "considered": narrowed,
        }

    def status(self, mission_id: str | None = None,
               project_id: str | None = None) -> dict[str, Any]:
        control = self.supervisor.control()
        readings = self.store.capacity_readings()
        checkpoints = ([self.checkpoint(mission_id)] if mission_id else
                       self.checkpoints(project_id))
        if (
            mission_id
            and project_id is not None
            and checkpoints[0]["project_id"] != project_id
        ):
            raise ShiftRefusal(
                "SHIFT_CROSS_PROJECT",
                "mission is outside the requested project scope",
            )
        classes = {}
        for item in checkpoints:
            classes[item["recovery_class"]] = classes.get(item["recovery_class"], 0) + 1
        actions = [self._next_action(item, control, readings) for item in checkpoints]
        outstanding = [
            item for item in checkpoints
            if item["recovery_class"] in {
                "in_flight", "uncertain_dispatch", "post_dispatch_recovery",
            }
        ]
        return {
            "contract_version": CONTRACT_VERSION,
            "state": control["state"],
            "control": control,
            "cold_start": True,
            "conversation_state_used": False,
            "transcript_included": False,
            "checkpoint_count": len(checkpoints),
            "checkpoints": checkpoints,
            "recovery": {
                "by_class": {key: classes[key] for key in sorted(classes)},
                "outstanding_count": len(outstanding),
                "requires_reconciliation": any(
                    item["recovery_class"] in {
                        "uncertain_dispatch", "post_dispatch_recovery",
                    } for item in checkpoints),
            },
            "queue": {
                "counts": self.store.counts(),
                "schedule_preview": self._schedule_preview(
                    project_id=project_id or (
                        checkpoints[0]["project_id"] if mission_id else None
                    ),
                    mission_id=mission_id,
                ),
            },
            "capacity": {
                "readings": {
                    key: value.as_row()
                    for key, value in sorted(readings.items())
                },
                "usable_now": sorted(
                    key for key, value in readings.items() if value.usable
                ),
            },
            "next_safe_actions": actions,
            "quiescent": not outstanding and not any(
                item["recovery_class"] == "in_flight" for item in checkpoints
            ),
        }

    def _next_action(self, checkpoint: Mapping[str, Any],
                     control: Mapping[str, Any],
                     readings: Mapping[str, Any]) -> dict[str, Any]:
        recovery = checkpoint["recovery_class"]
        if recovery == "repair_required":
            action = "repair_durable_record"
        elif recovery == "uncertain_dispatch":
            action = "reconcile_uncertain_dispatch_same_runtime"
        elif recovery == "post_dispatch_recovery":
            action = "resume_same_operation_key_same_runtime"
        elif recovery == "in_flight":
            action = "settle_in_flight_without_kill"
        elif recovery == "capacity_deferred":
            action = "re_evaluate_fresh_capacity"
        elif recovery == "pending":
            action = "dispatch_when_control_and_capacity_allow"
        elif recovery == "pre_dispatch_replayable":
            action = "resume_next_step"
        else:
            action = "none"
        return {
            "mission_id": checkpoint["mission_id"],
            "project_id": checkpoint["project_id"],
            "recovery_class": recovery,
            "action": action,
            "control_state": control["state"],
            "next_safe_step": checkpoint["next_safe_step"],
            "resume_target": checkpoint["resume_target"],
            "compatible_profiles": list(
                checkpoint["runtime"].get("compatible_profiles") or ()
            ),
            "capacity_rechecked": bool(readings),
        }

    # -- recovery and bounded drain ------------------------------------- #

    def _stale_ids(self) -> list[str]:
        now = self.store.clock()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM missions WHERE lease_token IS NOT NULL"
                " AND lease_expires_at<=? AND state NOT IN"
                " ('completed','refused','failed','cancelled') ORDER BY id",
                (now,),
            ).fetchall()
        return [row["id"] for row in rows]

    def _record_checkpoint(self, reason: str,
                           checkpoints: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        detail = {
            "contract_version": CONTRACT_VERSION,
            "reason": reason,
            "control_state": self.supervisor.control()["state"],
            "checkpoint_ids": [row["checkpoint_id"] for row in checkpoints],
            "checkpoints": [dict(row) for row in checkpoints],
            "conversation_state_used": False,
        }
        self.store.coordinate(None, None, "shift", "SHIFT_CHECKPOINT", detail)
        return {
            "reason": reason,
            "checkpoint_ids": detail["checkpoint_ids"],
            "count": len(checkpoints),
        }

    def recover(self) -> dict[str, Any]:
        """Reconstruct stale work before any new dispatch is considered."""

        stale = self._stale_ids()
        count = self.store.recover_stale()
        recovered = []
        for mission_id in stale:
            checkpoint = self.checkpoint(mission_id)
            self.store.log(mission_id, "SHIFT_RECOVERY_RECONSTRUCTED", {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "recovery_class": checkpoint["recovery_class"],
                "resume_target": checkpoint["resume_target"],
            })
            recovered.append({
                "mission_id": mission_id,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "recovery_class": checkpoint["recovery_class"],
                "next_safe_step": checkpoint["next_safe_step"],
            })
        return {
            "contract_version": CONTRACT_VERSION,
            "recovered_count": count,
            "recovered": recovered,
            "fresh_dispatch_allowed": not any(
                item["recovery_class"] in {
                    "uncertain_dispatch", "post_dispatch_recovery",
                } for item in self.checkpoints()
            ),
        }

    def _enter_draining(self, *, actor: str, reason: str) -> dict[str, Any]:
        control = self.supervisor.control()
        if control["state"] in {"running", "paused"}:
            return self.supervisor.transition(
                "draining", actor=actor, reason=reason,
            )
        return control

    @staticmethod
    def _validate_limit(max_steps: int) -> None:
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= MAX_DRAIN_STEPS
        ):
            raise ShiftRefusal(
                "SHIFT_DRAIN_LIMIT_INVALID",
                "max_steps must be between 1 and %d" % MAX_DRAIN_STEPS,
            )

    def drain(self, *, worker_id: str = "shift-drain",
              max_steps: int = 8, actor: str = "owner",
              reason: str = "on-demand shift suspend") -> dict[str, Any]:
        """Perform one finite drain invocation; it never starts fresh work."""

        self._validate_limit(max_steps)
        self._enter_draining(actor=actor, reason=reason)
        before = self.checkpoints()
        recorded = self._record_checkpoint("SHIFT_DRAIN_STARTED", before)
        recovery = self.recover()
        advanced = []
        for index in range(max_steps):
            mission = self.controller.work_once(
                "%s:%d" % (worker_id, index), resume_only=True,
            )
            if mission is None:
                break
            advanced.append({
                "mission_id": mission["id"],
                "project_id": mission.get("project_id"),
                "state": mission["state"],
            })
        after = self.checkpoints()
        outstanding = [
            item for item in after
            if item["recovery_class"] in {
                "in_flight", "uncertain_dispatch", "post_dispatch_recovery",
            }
        ]
        control = self.supervisor.control()
        stopped = False
        if not outstanding and control["state"] == "draining":
            control = self.supervisor.transition(
                "stopped", actor=actor, reason="SHIFT_DRAIN_COMPLETE",
            )
            stopped = True
        after_record = self._record_checkpoint("SHIFT_DRAIN_FINISHED", after)
        return {
            "contract_version": CONTRACT_VERSION,
            "action": "drain",
            "bounded": True,
            "max_steps": max_steps,
            "fresh_claims": 0,
            "recorded_checkpoint": recorded,
            "recovery": recovery,
            "advanced": advanced,
            "stopped": stopped,
            "control": control,
            "checkpoint": after_record,
            "status": self.status(),
        }

    def suspend(self, *, worker_id: str = "shift-drain",
                max_steps: int = 8, actor: str = "owner",
                reason: str = "on-demand shift suspend") -> dict[str, Any]:
        """Suspend by entering drain, taking a checkpoint, and returning."""

        return self.drain(
            worker_id=worker_id, max_steps=max_steps,
            actor=actor, reason=reason,
        )

    # -- cold-start resume preview -------------------------------------- #

    def _current_checkpoint(self, row: Mapping[str, Any]) -> dict[str, Any]:
        current = self.checkpoint(row["mission_id"])
        immutable = (
            "project_id", "idempotency_key", "repository", "baseline_sha",
            "candidate_sha",
        )
        for key in immutable:
            old = row.get(key)
            new = current.get(key)
            if (
                old not in (None, "unknown", "not_applicable", "not_run")
                and new not in (None, "unknown", "not_applicable", "not_run")
                and old != new
            ):
                code = ("SHIFT_STALE_HEAD" if key in {
                    "candidate_sha", "baseline_sha",
                } else "SHIFT_CHECKPOINT_IDEMPOTENCY_MISMATCH")
                raise ShiftRefusal(code, "checkpoint %s differs from live ledger" % key)
        old_context = (row.get("context") or {}).get("manifest_hash")
        new_context = (current.get("context") or {}).get("manifest_hash")
        if (
            old_context not in CANONICAL_ABSENCE
            and new_context not in CANONICAL_ABSENCE
            and old_context != new_context
        ):
            raise ShiftRefusal(
                "SHIFT_CONTEXT_REF_MISMATCH",
                "checkpoint context manifest differs from live ledger",
            )
        return current

    def resume_package(self, checkpoint: Mapping[str, Any],
                       *, target_profile: str | None = None) -> dict[str, Any]:
        row = validate_checkpoint(checkpoint)
        current = self._current_checkpoint(row)
        if current["recovery_class"] == "terminal":
            raise ShiftRefusal(
                "SHIFT_MISSION_TERMINAL",
                "terminal mission has no resumable work",
                mission_id=current["mission_id"],
            )
        if current["recovery_class"] == "repair_required":
            raise ShiftRefusal(
                "SHIFT_REPAIR_REQUIRED",
                "checkpoint contains a durable reference that failed validation",
                mission_id=current["mission_id"],
            )
        runtime = current["runtime"]
        compatible = tuple(runtime.get("compatible_profiles") or ())
        selected = runtime.get("selected_profile")
        if selected in CANONICAL_ABSENCE:
            selected = runtime.get("pending_profile")
        if current["resume_target"] == "reconcile_uncertain_dispatch":
            if not selected or selected in CANONICAL_ABSENCE:
                raise ShiftRefusal(
                    "SHIFT_RUNTIME_UNRESOLVED",
                    "uncertain dispatch has no durable runtime to reconcile",
                )
            if target_profile and target_profile != selected:
                raise ShiftRefusal(
                    "SHIFT_RUNTIME_INCOMPATIBLE",
                    "uncertain dispatch must stay on its original runtime",
                )
            target_profile = selected
        elif target_profile is None and selected not in CANONICAL_ABSENCE:
            target_profile = selected
        if target_profile and target_profile not in compatible:
            raise ShiftRefusal(
                "SHIFT_RUNTIME_INCOMPATIBLE",
                "target runtime is outside the checkpoint compatible set",
            )
        readings = self.store.capacity_readings()
        if target_profile in readings and not readings[target_profile].usable:
            raise ShiftRefusal(
                "SHIFT_CAPACITY_UNUSABLE",
                "target runtime has no fresh usable capacity reading",
            )
        return {
            "contract_version": CONTRACT_VERSION,
            "checkpoint_id": current["checkpoint_id"],
            "mission_id": current["mission_id"],
            "project_id": current["project_id"],
            "work_item_id": current["work_item_id"],
            "next_safe_step": current["next_safe_step"],
            "resume_target": current["resume_target"],
            "operation_keys": list(current["operation_keys"]),
            "repository_pin": dict(current["repository_pin"]),
            "context": dict(current["context"]),
            "evidence": dict(current["evidence"]),
            "capacity_observation": dict(current["capacity_observation"]),
            "runtime": {
                "target_profile": _absent(target_profile, "not_selected"),
                "selected_profile": runtime.get("selected_profile"),
                "compatible_profiles": list(compatible),
                "execution_mode": runtime.get("execution_mode"),
            },
            "lane": dict(current["lane"]),
            "uncertainty": dict(current["uncertainty"]),
            "work_baton": dict(current["work_baton"]),
            "unresolved_blockers": list(current["unresolved_blockers"]),
            "conversation_state_used": False,
            "transcript_included": False,
        }

    def resume_preview(self, mission_id: str | None = None,
                       *, target_profile: str | None = None) -> dict[str, Any]:
        rows = ([self.checkpoint(mission_id)] if mission_id
                else self.checkpoints())
        plans = []
        for row in rows:
            if row["recovery_class"] == "terminal":
                plans.append({
                    "mission_id": row["mission_id"],
                    "action": "complete",
                    "checkpoint_id": row["checkpoint_id"],
                })
                continue
            try:
                package = self.resume_package(row, target_profile=target_profile)
                plans.append({
                    "mission_id": row["mission_id"],
                    "action": "resume",
                    "package": package,
                })
            except ShiftRefusal as refusal:
                plans.append({
                    "mission_id": row["mission_id"],
                    "action": "repair_or_wait",
                    "refused": refusal.as_row(),
                })
        return {
            "contract_version": CONTRACT_VERSION,
            "observational": True,
            "would_claim_fresh_work": False,
            "conversation_state_used": False,
            "plans": plans,
        }
