from __future__ import annotations

from pathlib import Path

from factory_v2.canonical import (
    ContractError,
    load_pcp,
    mission_id_for,
    pcp_hash,
)
from factory_v2.contracts import (
    DistributionExecutor,
    EngineeringManager,
    Verifier,
)
from factory_v2.emit import (
    emit_distribution_handoff,
    emit_engineering_mission,
    emit_verification,
    emit_verified_rc,
    owner_history_entry,
)
from factory_v2.models import CandidateIdentity, MissionContext, MissionSnapshot, Verdict
from factory_v2.states import MissionState
from factory_v2.store import Store

AUTONOMOUS_DRIVE_STATES = frozenset(
    {
        MissionState.PCP_APPROVED,
        MissionState.ENGINEERING,
        MissionState.VERIFYING,
        MissionState.VERIFIED_RC,
    }
)
TERMINAL_STATES = frozenset({MissionState.DISTRIBUTED})


class GateError(RuntimeError):
    """Illegal lifecycle transition or missing gate evidence."""

    def __init__(self, message: str, *, code: str = "GATE_ERROR"):
        super().__init__(message)
        self.code = code


class InvariantError(PermissionError):
    """Protected Factory invariant violated independently of prompts."""

    def __init__(self, message: str, *, code: str = "INVARIANT"):
        super().__init__(message)
        self.code = code


class Controller:
    """Deterministic v2 lifecycle authority.

    Depends only on capability contracts. Harness CLI strings belong in adapters.
    External work-exchange projections are not runtime inputs.
    Controller does not call the EngineeringExecutor; Hermes coordinates it.
    """

    def __init__(
        self,
        store: Store,
        manager: EngineeringManager,
        verifier: Verifier,
        distributor: DistributionExecutor,
        workspace_root: str | Path,
    ):
        self.store = store
        self.manager = manager
        self.verifier = verifier
        self.distributor = distributor
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def admit_pcp(self, pcp: dict) -> MissionSnapshot:
        """Gate 1: schema-valid Owner-APPROVE PCP becomes exactly one durable mission."""
        try:
            admitted = load_pcp(pcp)
        except ContractError as exc:
            raise GateError(str(exc), code=getattr(exc, "code", "PCP_MALFORMED")) from exc
        h = pcp_hash(admitted)
        existing = self.store.get_by_hash(h)
        if existing is not None:
            return existing
        mid = mission_id_for(h)
        snap = self.store.insert_mission(mid, mid, h, admitted)
        (self.workspace_root / mid).mkdir(parents=True, exist_ok=True)
        return snap

    def submit_pcp(self, pcp: dict) -> MissionSnapshot:
        """Event-driven intake: admit once, then start Engineering without a manual tick."""
        snap = self.admit_pcp(pcp)
        return self.drive_until_pause(snap.mission_id)

    def drive_until_pause(self, mission_id: str, *, max_steps: int = 32) -> MissionSnapshot:
        """Advance until Owner Gate 2, BLOCKED, or a terminal state."""
        snap = self.get(mission_id)
        for _ in range(max_steps):
            if snap.state not in AUTONOMOUS_DRIVE_STATES:
                return snap
            snap = self.tick(mission_id)
        raise InvariantError(
            f"autonomous drive exceeded {max_steps} steps for {mission_id}"
        )

    def resume_incomplete(self) -> tuple[MissionSnapshot, ...]:
        """Restart recovery: resume non-terminal admitted work; never rerun terminal missions."""
        resumed: list[MissionSnapshot] = []
        for snap in self.store.list_missions():
            if snap.state in TERMINAL_STATES:
                continue
            if snap.state not in AUTONOMOUS_DRIVE_STATES:
                continue
            resumed.append(self.drive_until_pause(snap.mission_id))
        return tuple(resumed)

    def list_missions(self) -> tuple[MissionSnapshot, ...]:
        return self.store.list_missions()

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
                payload={"candidate": None if snap.current is None else snap.current.as_dict()},
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

    def owner_decide(self, mission_id: str, decision: str, reason: str = "") -> MissionSnapshot:
        snap = self.get(mission_id)
        if snap.state is not MissionState.OWNER_VALIDATION:
            raise GateError(
                f"Owner Gate 2 requires OWNER_VALIDATION, not {snap.state.value}"
            )
        cand = self._current(snap)
        if cand is None or cand.review_verdict != "pass" or cand.qa_verdict != "pass":
            raise InvariantError("failed or stale candidate cannot be Owner-approved")
        decision = decision.upper()
        identity = cand.identity
        if decision == "REJECT":
            nxt = snap.attempt_number + 1
            snap = self.store.apply_state(
                snap.mission_id,
                MissionState.ENGINEERING,
                owner_decision="REJECT",
                event_kind="owner_reject",
                payload={"candidate": identity.as_dict(), "reason": reason},
                candidate_status="rejected_by_owner",
                clear_current=True,
                owner_entry=owner_history_entry(identity, "REJECT", reason),
                rework_entry={
                    "from_attempt": snap.attempt_number,
                    "to_attempt": nxt,
                    "trigger": "OWNER_REJECT",
                    "same_mission": True,
                    "same_lineage": True,
                    "feedback": reason or "Owner rejected the verified RC",
                },
                attempt_number=nxt,
                rework_sequence=snap.rework_sequence + 1,
            )
            emit_engineering_mission(self._ws(snap), snap)
            return snap
        if decision == "APPROVE":
            rc_id = snap.rc_id or f"rc-{identity.candidate_id}"
            snap = self.store.apply_state(
                snap.mission_id,
                MissionState.DISTRIBUTION_READY,
                owner_decision="APPROVE",
                approved=identity,
                rc_id=rc_id,
                event_kind="owner_approve",
                payload={"candidate": identity.as_dict(), "reason": reason},
                candidate_status="approved",
                owner_entry=owner_history_entry(identity, "APPROVE", reason),
            )
            emit_engineering_mission(self._ws(snap), snap)
            emit_distribution_handoff(self._ws(snap), snap, identity, rc_id)
            return snap
        raise GateError(f"unknown Owner decision {decision!r}")

    def distribute(
        self,
        mission_id: str,
        substitute: CandidateIdentity | None = None,
    ) -> MissionSnapshot:
        snap = self.get(mission_id)
        if snap.state is not MissionState.DISTRIBUTION_READY:
            raise GateError(
                f"Distribution requires DISTRIBUTION_READY, not {snap.state.value}"
            )
        if snap.approved is None:
            raise InvariantError("Distribution requires an Owner-approved candidate tuple")
        if substitute is not None and substitute.key() != snap.approved.key():
            raise InvariantError(
                "Distribution cannot replace the approved candidate",
                code="ARTIFACT_SUBSTITUTION",
            )
        result = self.distributor.distribute(snap.approved, snap.mission_id)
        if result.candidate.key() != snap.approved.key():
            raise InvariantError(
                "Distribution cannot replace the approved candidate",
                code="ARTIFACT_SUBSTITUTION",
            )
        return self.store.apply_state(
            snap.mission_id,
            MissionState.DISTRIBUTED,
            event_kind="distributed",
            payload={
                "candidate": result.candidate.as_dict(),
                "receipt": result.receipt,
                "harness_mode": result.harness_mode,
            },
        )

    def _ws(self, snap: MissionSnapshot) -> Path:
        path = self.workspace_root / snap.mission_id
        path.mkdir(parents=True, exist_ok=True)
        return path

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
            lineage_id=snap.lineage_id,
            pcp_hash=snap.pcp_hash,
            pcp=snap.pcp,
            workspace_path=str(self._ws(snap)),
            attempt_number=snap.attempt_number,
            rework_sequence=snap.rework_sequence,
            hermes_session_id=snap.hermes_session_id,
            defects=tuple(defects),
            current=snap.current,
            progress_callback=lambda stage, item: self.store.record_progress(
                snap.mission_id, active_stage=stage, current_work_item=item
            ),
        )

    def _engineer(self, snap: MissionSnapshot) -> MissionSnapshot:
        # Crucial: record ENGINEERING state in durable ledger before launching Hermes/executor
        if snap.state is not MissionState.ENGINEERING:
            snap = self.store.apply_state(
                snap.mission_id,
                MissionState.ENGINEERING,
                active_stage="engineering",
                event_kind="engineering_started",
                payload={
                    "attempt_number": snap.attempt_number,
                    "rework_sequence": snap.rework_sequence,
                },
            )
            emit_engineering_mission(self._ws(snap), snap)
        ctx = self._context(snap)
        result = self.manager.run_campaign(ctx)
        if result.blocked or result.candidate is None:
            snap = self.store.apply_state(
                snap.mission_id,
                MissionState.BLOCKED,
                active_stage="blocked",
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
            emit_engineering_mission(self._ws(snap), snap)
            return snap
        if not result.executor_called:
            raise InvariantError(
                "Engineering Manager must delegate coding through the EngineeringExecutor"
            )
        seq = self.store.next_sequence(snap.mission_id)
        snap = self.store.record_candidate(
            snap.mission_id,
            result.candidate,
            seq,
            attempt_id=f"att-{seq}",
            hermes_session_id=result.hermes_session_id or f"hermes-{snap.mission_id}",
            grok_session_ref=result.grok_session_ref or f"grok-{result.candidate.candidate_id}",
        )
        emit_engineering_mission(self._ws(snap), snap)
        return snap

    def _verify(self, snap: MissionSnapshot) -> MissionSnapshot:
        cand = self._current(snap)
        if cand is None:
            raise GateError("VERIFYING requires a bound candidate")
        ctx = self._context(snap)
        review = self.verifier.review(ctx, cand.identity)
        self._bound(review, cand.identity, "review")
        if not review.passed:
            emit_verification(self._ws(snap), snap, cand.identity, review, None)
            snap = self._rework(
                snap,
                "review_fail",
                review,
                candidate_status="review_failed",
                review_verdict="fail",
            )
            emit_engineering_mission(self._ws(snap), snap)
            return snap
        qa = self.verifier.qa(ctx, cand.identity)
        self._bound(qa, cand.identity, "qa")
        emit_verification(self._ws(snap), snap, cand.identity, review, qa)
        if not qa.passed:
            snap = self._rework(
                snap,
                "qa_fail",
                qa,
                candidate_status="qa_failed",
                review_verdict="pass",
                qa_verdict="fail",
            )
            emit_engineering_mission(self._ws(snap), snap)
            return snap
        rc = emit_verified_rc(
            self._ws(snap),
            snap,
            cand.identity,
            review,
            qa,
            (),
        )
        snap = self.store.apply_state(
            snap.mission_id,
            MissionState.VERIFIED_RC,
            event_kind="verified_rc",
            payload={"candidate": cand.identity.as_dict(), "rc_id": rc["rc"]["rc_id"]},
            candidate_status="verified",
            review_verdict="pass",
            qa_verdict="pass",
            rc_id=rc["rc"]["rc_id"],
        )
        emit_engineering_mission(self._ws(snap), snap)
        return snap

    def _rework(
        self,
        snap: MissionSnapshot,
        kind: str,
        verdict: Verdict,
        **kwargs,
    ) -> MissionSnapshot:
        nxt = snap.attempt_number + 1
        return self.store.apply_state(
            snap.mission_id,
            MissionState.ENGINEERING,
            active_stage="engineering",
            clear_work_item=True,
            event_kind=kind,
            payload={
                "candidate": verdict.candidate.as_dict(),
                "defects": list(verdict.defects),
                "harness_mode": verdict.harness_mode,
            },
            clear_current=True,
            rework_entry={
                "from_attempt": snap.attempt_number,
                "to_attempt": nxt,
                "trigger": "VERIFIER_REJECT",
                "same_mission": True,
                "same_lineage": True,
                "feedback": "; ".join(verdict.defects) or kind,
            },
            attempt_number=nxt,
            rework_sequence=snap.rework_sequence + 1,
            **kwargs,
        )

    def _bound(self, verdict: Verdict, expected: CandidateIdentity, channel: str) -> None:
        if verdict.candidate.key() != expected.key():
            raise InvariantError(
                f"{channel} verifier cannot substitute a different candidate",
                code="STALE_VERIFIER_CANDIDATE",
            )
        if verdict.kind not in {channel, "review", "qa"}:
            raise InvariantError(f"{channel} verdict channel required")
        if channel == "review" and verdict.kind != "review":
            raise InvariantError("review verdict channel required")
        if channel == "qa" and verdict.kind != "qa":
            raise InvariantError("qa verdict channel required")

    def _current(self, snap: MissionSnapshot):
        if snap.current is None:
            return None
        for c in snap.candidates:
            if c.identity.key() == snap.current.key():
                return c
        return None
