"""Turn cadence reconciliation and certification gate enforcement.

Implements the canonical two-turn certification cadence from:
`skills/software-factory-turn-cadence/SKILL.md`

Rules:
1. Candidate Freeze: both certifiers (Review and QA) must bind their evidence to the exact candidate head SHA Hn.
2. If the candidate changes after certification begins, evidence is stale.
3. Gate ACCEPT requires BOTH independent certifiers to ACCEPT the exact head.
4. On ACCEPT: propose canonical expected-head integration. The worker does not merge PRs, advance `main`, or activate dependent work.
5. On REJECT: consolidated defects route back to the SAME producer lineage as rework; the dependent next task remains INACTIVE.
6. Forbidden anti-drift path `rework A + start dependent B in one turn` is strictly rejected.
"""

from __future__ import annotations

from typing import Sequence

from .model import (
    CertificationRecord,
    CertificationVerdict,
    GateDecision,
    GateOutcome,
)


class TurnCadenceReconciler:
    """Reconciles independent Review and QA certifications against a frozen candidate head."""

    def __init__(self, required_roles: Sequence[str] = ("review", "qa")) -> None:
        self.required_roles = [r.lower() for r in required_roles]

    def reconcile(
        self,
        task_id: str,
        frozen_head_sha: str,
        current_head_sha: str,
        certifications: Sequence[CertificationRecord],
        real_post_merge_main_sha: str | None = None,
    ) -> GateDecision:
        """Reconcile the certification gate for a frozen candidate head."""
        # 1. Stale head check: has candidate branch mutated since freeze?
        if current_head_sha != frozen_head_sha:
            return GateDecision(
                outcome=GateOutcome.REJECT,
                task_id=task_id,
                head_sha=current_head_sha,
                next_action="ROUTER_REWORK_SAME_LINEAGE",
                defects=[
                    f"STALE_PR_HEAD_DETECTED: candidate head moved from {frozen_head_sha} to {current_head_sha} during certification"
                ],
                detail="Candidate mutated during active certification; certifications invalidated",
            )

        # 2. Filter certifications strictly matching frozen head SHA
        valid_certs = [c for c in certifications if c.head_sha == frozen_head_sha]

        # Check coverage of required roles
        recorded_roles = {c.role.lower(): c for c in valid_certs}
        missing_roles = [r for r in self.required_roles if r not in recorded_roles]

        if missing_roles:
            return GateDecision(
                outcome=GateOutcome.PENDING,
                task_id=task_id,
                head_sha=frozen_head_sha,
                next_action="WAIT_CERTIFICATION",
                missing_roles=missing_roles,
                detail=f"Waiting for certifications from required roles: {', '.join(missing_roles)}",
            )

        # 3. Collect verdicts and defects
        consolidated_defects: list[str] = []
        any_rejected = False

        for role in self.required_roles:
            cert = recorded_roles[role]
            if cert.verdict == CertificationVerdict.REJECT:
                any_rejected = True
                consolidated_defects.extend(cert.defects)
                if not cert.defects:
                    consolidated_defects.append(f"{role.upper()} rejected head {frozen_head_sha}")

        # 4. Gate Fork: REJECT vs ACCEPT
        if any_rejected:
            return GateDecision(
                outcome=GateOutcome.REJECT,
                task_id=task_id,
                head_sha=frozen_head_sha,
                next_action="ROUTER_REWORK_SAME_LINEAGE",
                defects=consolidated_defects,
                detail=f"Gate REJECTED by independent certification: {len(consolidated_defects)} defects routed to same producer lineage as rework only. Dependent next task remains inactive.",
            )

        # All required certifiers ACCEPTED
        post_merge = real_post_merge_main_sha or "PENDING_REAL_POST_MERGE_INTEGRATION"
        return GateDecision(
            outcome=GateOutcome.ACCEPT,
            task_id=task_id,
            head_sha=frozen_head_sha,
            next_action="PROPOSE_CANONICAL_INTEGRATION",
            new_main_sha=post_merge,
            detail=(
                f"Gate ACCEPTED: Review and QA certified exact head {frozen_head_sha}. "
                "Propose canonical expected-head integration; worker does not merge PR "
                "or activate dependent work."
            ),
        )
