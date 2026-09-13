from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from factory_v2.contracts import (
    DistributionExecutor,
    EngineeringExecutor,
    EngineeringManager,
    Verifier,
)
from factory_v2.models import MissionContext, MissionSnapshot, PCP
from factory_v2.states import MissionState
from factory_v2.store import Store


class GateError(RuntimeError):
    """Illegal lifecycle transition or missing gate evidence."""


class InvariantError(PermissionError):
    """Protected Factory invariant violated independently of prompts."""


def pcp_hash(pcp: PCP) -> str:
    body = json.dumps(pcp.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def mission_id_for(hash_: str) -> str:
    return f"msn-{hash_[:32]}"


class Controller:
    """Deterministic v2 lifecycle authority.

    Depends only on capability contracts. Harness CLI strings belong in adapters.
    External work-exchange projections are not runtime inputs.
    """

    def __init__(
        self,
        store: Store,
        manager: EngineeringManager,
        executor: EngineeringExecutor,
        verifier: Verifier,
        distributor: DistributionExecutor,
        workspace_root: str | Path,
    ):
        self.store = store
        self.manager = manager
        self.executor = executor
        self.verifier = verifier
        self.distributor = distributor
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def admit_pcp(self, pcp: PCP) -> MissionSnapshot:
        """Gate 1: an already-approved PCP becomes exactly one durable mission."""
        h = pcp_hash(pcp)
        existing = self.store.get_by_hash(h)
        if existing is not None:
            return existing
        mid = mission_id_for(h)
        snap = self.store.insert_mission(mid, h, pcp.as_dict())
        (self.workspace_root / mid).mkdir(parents=True, exist_ok=True)
        return snap

    def get(self, mission_id: str) -> MissionSnapshot:
        snap = self.store.get(mission_id)
        if snap is None:
            raise KeyError(mission_id)
        return snap

    def tick(self, mission_id: str) -> MissionSnapshot:
        snap = self.get(mission_id)
        if snap.state is MissionState.PCP_APPROVED:
            return self._engineer(snap)
        if snap.state is MissionState.ENGINEERING:
            return self._engineer(snap)
        if snap.state is MissionState.VERIFYING:
            return self._verify(snap)
        if snap.state is MissionState.VERIFIED_RC:
            return self.store.apply_state(
                snap.mission_id,
                MissionState.OWNER_VALIDATION,
                event_kind="presented_to_owner",
                payload={"artifact_id": snap.current_artifact_id},
            )
        if snap.state is MissionState.BLOCKED:
            return snap
        if snap.state in (
            MissionState.OWNER_VALIDATION,
            MissionState.DISTRIBUTION_READY,
            MissionState.DISTRIBUTED,
        ):
            return snap
        raise GateError(f"no tick from {snap.state.value}")

    def owner_decide(
        self, mission_id: str, decision: str, reason: str = ""
    ) -> MissionSnapshot:
        snap = self.get(mission_id)
        if snap.state is not MissionState.OWNER_VALIDATION:
            raise GateError(
                f"Owner Gate 2 requires OWNER_VALIDATION, not {snap.state.value}"
            )
        cand = self._current(snap)
        if cand is None or cand.review_verdict != "pass" or cand.qa_verdict != "pass":
            raise InvariantError("failed or stale candidate cannot be Owner-approved")
        decision = decision.upper()
        if decision == "REJECT":
            return self.store.apply_state(
                snap.mission_id,
                MissionState.ENGINEERING,
                owner_decision="REJECT",
                event_kind="owner_reject",
                payload={"artifact_id": cand.artifact_id, "reason": reason},
                candidate_status="rejected_by_owner",
                clear_current=True,
            )
        if decision == "APPROVE":
            return self.store.apply_state(
                snap.mission_id,
                MissionState.DISTRIBUTION_READY,
                owner_decision="APPROVE",
                approved_artifact_id=cand.artifact_id,
                event_kind="owner_approve",
                payload={"artifact_id": cand.artifact_id, "reason": reason},
                candidate_status="approved",
            )
        raise GateError(f"unknown Owner decision {decision!r}")

    def distribute(self, mission_id: str, substitute_artifact: str | None = None) -> MissionSnapshot:
        snap = self.get(mission_id)
        if snap.state is not MissionState.DISTRIBUTION_READY:
            raise GateError(
                f"Distribution requires DISTRIBUTION_READY, not {snap.state.value}"
            )
        if not snap.approved_artifact_id:
            raise InvariantError("Distribution requires an Owner-approved artifact")
        if substitute_artifact and substitute_artifact != snap.approved_artifact_id:
            raise InvariantError("Distribution cannot replace the approved candidate")
        result = self.distributor.distribute(
            snap.approved_artifact_id, snap.mission_id
        )
        if result.artifact_id != snap.approved_artifact_id:
            raise InvariantError("Distribution cannot replace the approved candidate")
        return self.store.apply_state(
            snap.mission_id,
            MissionState.DISTRIBUTED,
            event_kind="distributed",
            payload={
                "artifact_id": result.artifact_id,
                "receipt": result.receipt,
                "harness_mode": result.harness_mode,
            },
        )

    def _context(self, snap: MissionSnapshot) -> MissionContext:
        defects: list[str] = []
        for ev in snap.events:
            if ev["kind"] in {"review_fail", "qa_fail", "owner_reject"}:
                payload = ev["payload"]
                defects.extend(payload.get("defects", []))
                if payload.get("reason"):
                    defects.append(payload["reason"])
        return MissionContext(
            mission_id=snap.mission_id,
            pcp_hash=snap.pcp_hash,
            pcp=snap.pcp,
            workspace_path=str(self.workspace_root / snap.mission_id),
            defects=tuple(defects),
            current_artifact_id=snap.current_artifact_id,
        )

    def _engineer(self, snap: MissionSnapshot) -> MissionSnapshot:
        ctx = self._context(snap)
        result = self.manager.run_campaign(ctx)
        if result.blocked or not result.candidate_artifact_id:
            return self.store.apply_state(
                snap.mission_id,
                MissionState.BLOCKED,
                blocked_reason=result.reason or "engineering blocked",
                event_kind="engineering_blocked",
                payload={
                    "reason": result.reason,
                    "harness_mode": result.harness_mode,
                    "manager": result.manager_name,
                    "executor": result.executor_name,
                    "executor_called": result.executor_called,
                },
            )
        if not result.executor_called:
            raise InvariantError("Engineering Manager must delegate coding through the executor")
        seq = self.store.next_sequence(snap.mission_id)
        cid = result.candidate_id or f"cand-{seq}-{uuid4().hex[:8]}"
        self.store.record_candidate(
            snap.mission_id, cid, result.candidate_artifact_id, seq
        )
        return self.get(snap.mission_id)

    def _verify(self, snap: MissionSnapshot) -> MissionSnapshot:
        cand = self._current(snap)
        if cand is None:
            raise GateError("VERIFYING requires a bound candidate")
        ctx = self._context(snap)
        review = self.verifier.review(ctx, cand.artifact_id)
        if review.artifact_id != cand.artifact_id:
            raise InvariantError("verifier cannot substitute a different artifact")
        if review.kind != "review":
            raise InvariantError("review verdict channel required")
        if not review.passed:
            return self.store.apply_state(
                snap.mission_id,
                MissionState.ENGINEERING,
                event_kind="review_fail",
                payload={
                    "artifact_id": cand.artifact_id,
                    "defects": list(review.defects),
                    "harness_mode": review.harness_mode,
                },
                candidate_status="review_failed",
                review_verdict="fail",
                clear_current=True,
            )
        qa = self.verifier.qa(ctx, cand.artifact_id)
        if qa.artifact_id != cand.artifact_id:
            raise InvariantError("verifier cannot substitute a different artifact")
        if qa.kind != "qa":
            raise InvariantError("qa verdict channel required")
        if not qa.passed:
            return self.store.apply_state(
                snap.mission_id,
                MissionState.ENGINEERING,
                event_kind="qa_fail",
                payload={
                    "artifact_id": cand.artifact_id,
                    "defects": list(qa.defects),
                    "harness_mode": qa.harness_mode,
                },
                candidate_status="qa_failed",
                review_verdict="pass",
                qa_verdict="fail",
                clear_current=True,
            )
        return self.store.apply_state(
            snap.mission_id,
            MissionState.VERIFIED_RC,
            event_kind="verified_rc",
            payload={"artifact_id": cand.artifact_id},
            candidate_status="verified",
            review_verdict="pass",
            qa_verdict="pass",
        )

    def _current(self, snap: MissionSnapshot):
        if not snap.current_candidate_id:
            return None
        for c in snap.candidates:
            if c.candidate_id == snap.current_candidate_id:
                return c
        return None
