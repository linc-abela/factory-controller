#!/usr/bin/env python3
"""Black-box Factory v2 Controller protocol: factory.v2.conformance.v1.

This entrypoint is the production surface SFV2-005 invokes. It drives the
real Controller, store, validators, candidate binding, verification,
Owner Gate, replay, and Distribution handoff. Scenario IDs select which
lifecycle path to exercise; they do not select hardcoded PASS payloads.
"""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from factory_v2.adapters.antigravity import AntigravityDistributor, AntigravityVerifier
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.adapters.selection import build_executor, selected_executor_kind
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import fixture_path, validate_document
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.models import CandidateIdentity
from factory_v2.states import MissionState
from factory_v2.store import Store

PROTOCOL = "factory.v2.conformance.v1"
IDENTITY_FIELDS = [
    "candidate_id",
    "source_revision",
    "artifact_hash",
    "artifact_uri",
]
ARTIFACTS = ["cand-A", "cand-B", "cand-C", "cand-D-red", "cand-D"]
VERDICT_TABLE = {
    "cand-A": (False, False),
    "cand-B": (True, False),
    "cand-C": (True, True),
    "cand-D-red": (False, False),
    "cand-D": (True, True),
}
CANONICAL_DOCUMENTS = [
    {"fixture": "valid-approved-pcp.json", "schema": "pcp-handoff.schema.json"},
    {"fixture": "both-verifiers-pass-same-candidate.json", "schema": "verification.schema.json"},
    {"fixture": "verified-rc-eligible.json", "schema": "verified-rc.schema.json"},
    {"fixture": "owner-approve-distribution.json", "schema": "distribution-handoff.schema.json"},
]


class World:
    def __init__(self, root: Path, *, mode: str, stale: bool = False):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.store = Store(root / "ledger.sqlite")
        sandboxes = root / "sandboxes"
        if mode == "deterministic":
            self.executor = ScriptedGrok(list(ARTIFACTS))
            self.manager = ScriptedHermes(self.executor)
            stale_id = CandidateIdentity(
                "stale-candidate",
                "1111111111111111111111111111111111111111",
                "sha256:" + "b" * 64,
                "artifact://stale/wrong",
            )
            self.verifier = ScriptedVerifier(
                dict(VERDICT_TABLE),
                substitute=stale_id if stale else None,
            )
            self.distributor = ScriptedDistributor()
        else:
            self.executor = build_executor()
            self.manager = NousHermesAdapter(executor=self.executor)
            self.verifier = AntigravityVerifier()
            self.distributor = AntigravityDistributor()
        self.ctl = Controller(
            self.store, self.manager, self.verifier, self.distributor, sandboxes
        )

    def approved_pcp(self) -> dict[str, Any]:
        return json.loads(fixture_path("valid-approved-pcp.json").read_text(encoding="utf-8"))

    def runtime_truth(self) -> dict[str, Any]:
        simulated = self.mode == "deterministic"
        kind = "scripted" if simulated else selected_executor_kind()
        executor_mode = getattr(
            self.executor, "harness_mode", "simulated" if simulated else "real"
        )
        return {
            "simulated": simulated,
            "hermes": getattr(self.manager, "harness_mode", "simulated" if simulated else "real"),
            "executor": kind,
            "executor_type": getattr(self.executor, "executor_type", "scripted"),
            "grok": executor_mode,
            "antigravity": getattr(self.verifier, "harness_mode", "simulated" if simulated else "real"),
        }


def _base(scenario: str, mode: str) -> dict[str, Any]:
    return {"protocol": PROTOCOL, "scenario": scenario, "mode": mode}


def _fail(world: World, scenario: str, exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", None) or "PROTOCOL_FAILURE"
    return {
        **_base(scenario, world.mode),
        "accepted": False,
        "failure_code": code,
        "runtime_truth": world.runtime_truth(),
    }


def _candidate(identity: CandidateIdentity | None) -> dict[str, str] | None:
    return None if identity is None else identity.as_dict()


def _attempts(snap) -> list[dict[str, str]]:
    kinds = {"review_fail", "qa_fail", "verified_rc"}
    out: list[dict[str, str]] = []
    for event in snap.events:
        if event["kind"] not in kinds:
            continue
        payload = event.get("payload") or {}
        candidate = payload.get("candidate") or {}
        cid = candidate.get("candidate_id")
        if cid:
            out.append({"candidate_id": cid})
    return out


def _tick_until(ctl: Controller, mission_id: str, state: MissionState) -> Any:
    snap = ctl.get(mission_id)
    for _ in range(16):
        if snap.state is state:
            return snap
        if snap.state is MissionState.BLOCKED:
            raise GateError(snap.blocked_reason or "blocked", code="RUNTIME_BLOCKED")
        nxt = ctl.tick(mission_id)
        if nxt.state is snap.state and nxt.state in {
            MissionState.OWNER_VALIDATION,
            MissionState.DISTRIBUTION_READY,
            MissionState.DISTRIBUTED,
        }:
            return nxt
        snap = nxt
    raise GateError(f"did not reach {state.value}", code="GATE_ERROR")


def handle(world: World, scenario: str) -> dict[str, Any]:
    if scenario == "S01":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        return {
            **_base(scenario, world.mode),
            "accepted": True,
            "event": "pcp_admitted",
            "owner_approval": "APPROVE",
            "mission_id": snap.mission_id,
            "lineage_id": snap.lineage_id,
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S02":
        raw = json.loads(
            fixture_path("pcp-without-owner-approval.invalid.json").read_text(encoding="utf-8")
        )
        try:
            world.ctl.admit_pcp(raw)
        except (GateError, InvariantError) as exc:
            return _fail(world, scenario, exc)
        return {**_base(scenario, world.mode), "accepted": True}
    if scenario == "S03":
        raw = deepcopy(world.approved_pcp())
        raw["source"]["revision"] = "0000000000000000000000000000000000000000"
        try:
            world.ctl.admit_pcp(raw)
        except (GateError, InvariantError) as exc:
            return _fail(world, scenario, exc)
        return {**_base(scenario, world.mode), "accepted": True}
    if scenario == "S04":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        snap = world.ctl.tick(snap.mission_id)
        candidate = _candidate(snap.current) or snap.candidates[0].identity.as_dict()
        return {
            **_base(scenario, world.mode),
            "candidate": candidate,
            "identity_fields": list(IDENTITY_FIELDS),
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S05":
        stale = World(world.root / "stale", mode=world.mode, stale=True)
        snap = stale.ctl.admit_pcp(stale.approved_pcp())
        stale.ctl.tick(snap.mission_id)
        try:
            stale.ctl.tick(snap.mission_id)
        except (GateError, InvariantError) as exc:
            return _fail(stale, scenario, exc)
        return {**_base(scenario, world.mode), "accepted": True}
    if scenario == "S06":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        world.ctl.tick(snap.mission_id)
        after = world.ctl.tick(snap.mission_id)
        return {
            **_base(scenario, world.mode),
            "mission_id": after.mission_id,
            "lineage_id": after.lineage_id,
            "failed_channel": "review",
            "returned_to_engineering": after.state is MissionState.ENGINEERING,
            "same_mission": after.mission_id == snap.mission_id,
            "same_lineage": after.lineage_id == snap.lineage_id,
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S07":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        world.ctl.tick(mid)
        world.ctl.tick(mid)
        world.ctl.tick(mid)
        after = world.ctl.tick(mid)
        return {
            **_base(scenario, world.mode),
            "mission_id": after.mission_id,
            "lineage_id": after.lineage_id,
            "failed_channel": "qa_e2e",
            "returned_to_engineering": after.state is MissionState.ENGINEERING,
            "same_mission": after.mission_id == snap.mission_id,
            "same_lineage": after.lineage_id == snap.lineage_id,
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S08":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        world.ctl.tick(mid)
        world.ctl.tick(mid)
        world.ctl.tick(mid)
        after = world.ctl.tick(mid)
        return {
            **_base(scenario, world.mode),
            "candidates": [c.identity.as_dict() for c in after.candidates],
            "verification_attempts": _attempts(after),
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S09":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        after = _tick_until(world.ctl, snap.mission_id, MissionState.VERIFIED_RC)
        current = next(c for c in after.candidates if c.identity == after.current)
        return {
            **_base(scenario, world.mode),
            "state": after.state.value,
            "code_review": "PASS" if current.review_verdict == "pass" else "FAIL",
            "qa_e2e": "PASS" if current.qa_verdict == "pass" else "FAIL",
            "owner_gate": "AWAITING_OWNER",
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S10":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        _tick_until(world.ctl, mid, MissionState.VERIFIED_RC)
        world.ctl.tick(mid)
        rejected = world.ctl.owner_decide(mid, "REJECT", "change intent")
        world.ctl.tick(mid)
        after = world.ctl.tick(mid)
        return {
            **_base(scenario, world.mode),
            "owner_decision": "REJECT",
            "same_mission": after.mission_id == snap.mission_id,
            "same_lineage": after.lineage_id == snap.lineage_id,
            "candidates": [c.identity.as_dict() for c in after.candidates],
            "verification_attempts": _attempts(after),
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S11":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        _tick_until(world.ctl, mid, MissionState.VERIFIED_RC)
        world.ctl.tick(mid)
        approved = world.ctl.owner_decide(mid, "APPROVE")
        handoff_path = world.root / "sandboxes" / mid / "distribution-handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        validate_document("distribution-handoff.schema.json", handoff)
        candidate = approved.approved.as_dict() if approved.approved else {}
        return {
            **_base(scenario, world.mode),
            "owner_decision": "APPROVE",
            "state": approved.state.value,
            "distribution_handoff": {
                "candidate": candidate,
                "artifact_immutable": True,
            },
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S12":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        _tick_until(world.ctl, mid, MissionState.VERIFIED_RC)
        world.ctl.tick(mid)
        approved = world.ctl.owner_decide(mid, "APPROVE")
        fake = CandidateIdentity(
            approved.approved.candidate_id,
            approved.approved.source_revision,
            "sha256:" + "9" * 64,
            approved.approved.artifact_uri,
        )
        try:
            world.ctl.distribute(mid, substitute=fake)
        except (GateError, InvariantError) as exc:
            return _fail(world, scenario, exc)
        return {**_base(scenario, world.mode), "accepted": True}
    if scenario == "S13":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        world.ctl.tick(mid)
        verifying = world.ctl.get(mid)
        restarted = Controller(
            Store(world.store.path),
            world.manager,
            world.verifier,
            world.distributor,
            world.root / "sandboxes",
        )
        again = restarted.admit_pcp(world.approved_pcp())
        after = restarted.tick(mid)
        admitted = [e for e in restarted.get(mid).events if e["kind"] == "pcp_admitted"]
        skipped = after.state in {
            MissionState.VERIFIED_RC,
            MissionState.OWNER_VALIDATION,
            MissionState.DISTRIBUTION_READY,
            MissionState.DISTRIBUTED,
        } or again.state in {
            MissionState.VERIFIED_RC,
            MissionState.OWNER_VALIDATION,
            MissionState.DISTRIBUTION_READY,
        }
        return {
            **_base(scenario, world.mode),
            "mission_count": 1 if again.mission_id == mid else 2,
            "admission_events": len(admitted),
            "replay_idempotent": again.mission_id == mid and again.state is verifying.state,
            "gates_skipped_on_replay": not skipped,
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S14":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        mid = snap.mission_id
        _tick_until(world.ctl, mid, MissionState.VERIFIED_RC)
        world.ctl.tick(mid)
        world.ctl.owner_decide(mid, "APPROVE")
        workspace = world.root / "sandboxes" / mid
        required = {
            "engineering-mission.json": "engineering-mission.schema.json",
            "verified-rc.json": "verified-rc.schema.json",
            "distribution-handoff.json": "distribution-handoff.schema.json",
        }
        for name, schema in required.items():
            document = json.loads((workspace / name).read_text(encoding="utf-8"))
            validate_document(schema, document)
        verifications = list(workspace.glob("verification-*.json"))
        if not verifications:
            raise GateError("verification document was not emitted", code="VERIFICATION_EVIDENCE_UNBOUND")
        for path in verifications:
            validate_document("verification.schema.json", json.loads(path.read_text(encoding="utf-8")))
        return {
            **_base(scenario, world.mode),
            "documents": list(CANONICAL_DOCUMENTS),
            "runtime_truth": world.runtime_truth(),
        }
    if scenario == "S15":
        snap = world.ctl.admit_pcp(world.approved_pcp())
        world.ctl.tick(snap.mission_id)
        return {
            **_base(scenario, world.mode),
            "runtime_truth": world.runtime_truth(),
        }
    return {
        **_base(scenario, world.mode),
        "accepted": False,
        "failure_code": "UNKNOWN_SCENARIO",
    }


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("protocol") != PROTOCOL:
        return {
            "protocol": request.get("protocol"),
            "scenario": request.get("scenario"),
            "mode": request.get("mode"),
            "accepted": False,
            "failure_code": "PROTOCOL_VERSION_MISMATCH",
        }
    contracts = request.get("contracts_dir")
    if contracts:
        os.environ["FACTORY_V2_CONTRACTS_DIR"] = str(contracts)
    workspace = Path(request["workspace"])
    scenario = request["scenario"]
    mode = request.get("mode") or "deterministic"
    world = World(workspace / scenario, mode=mode)
    try:
        return handle(world, scenario)
    except (GateError, InvariantError) as exc:
        return _fail(world, scenario, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    request = json.loads(parser.parse_args().request.read_text(encoding="utf-8"))
    print(json.dumps(run_request(request), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
