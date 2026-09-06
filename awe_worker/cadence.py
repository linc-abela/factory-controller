"""Live cadence continuation coordinator and certifier dispatch.

Binds candidate publication to exact-head certification tasks in Notion AWE,
wakes eligible certifiers, collects durable evidence, and reconciles the Gate
to advance the workflow without Owner coordination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .model import (
    AWEStatus,
    AWEWorkItem,
    CandidateHead,
    CertificationRecord,
    CertificationVerdict,
    ExecutionSlot,
    GateDecision,
    GateOutcome,
)
from .notion import NotionSourceOfRecord
from .reconciliation import TurnCadenceReconciler


@dataclass(frozen=True)
class CadenceAction:
    action_type: str  # "CERTIFIERS_QUEUED", "GATE_ACCEPTED", "GATE_REJECTED", "WAITING_CERTIFIERS"
    task_id: str
    head_sha: str
    queued_tasks: list[str] = field(default_factory=list)
    verdict: str = ""
    defects: list[str] = field(default_factory=list)
    activated_next_task: str | None = None
    detail: str = ""


class CadenceContinuationCoordinator:
    """Coordinates two-turn candidate freeze, certifier dispatch, and gate continuation."""

    def __init__(
        self,
        reconciler: TurnCadenceReconciler | None = None,
        source_of_record: NotionSourceOfRecord | None = None,
        required_roles: Sequence[str] = ("review", "qa"),
    ) -> None:
        self.reconciler = reconciler or TurnCadenceReconciler(required_roles=required_roles)
        self.source_of_record = source_of_record
        self.required_roles = [r.lower() for r in required_roles]

    def coordinate_candidate_cadence(
        self,
        candidate: CandidateHead,
        producer_task: AWEWorkItem,
        all_exchange_tasks: Sequence[AWEWorkItem],
        certifications: Sequence[CertificationRecord],
    ) -> CadenceAction:
        """Evaluate cadence state for a frozen candidate head and continue the workflow."""
        task_id = candidate.task_id
        head_sha = candidate.head_sha

        # 1. Look for existing certifier tasks in exchange
        certifier_tasks: dict[str, AWEWorkItem] = {}
        for t in all_exchange_tasks:
            r = t.role.lower()
            if r in self.required_roles:
                # Check if this task targets the producer task
                if task_id.lower() in t.title.lower() or task_id.lower() in t.notes.lower():
                    certifier_tasks[r] = t

        # 2. Check if we need to queue certifiers
        queued: list[str] = []
        for r in self.required_roles:
            c_task = certifier_tasks.get(r)
            if c_task and c_task.status != AWEStatus.IN_PROGRESS.value and c_task.status != AWEStatus.DONE.value:
                # Update certifier notes with exact frozen head
                if self.source_of_record:
                    notes = f"Exact-head certification for {task_id} candidate {head_sha[:7]} (PR #{candidate.pr_number or 'N/A'})."
                    self.source_of_record.client.update_page(
                        page_id=c_task.dashboard_page_id,
                        properties={
                            "Status": {"select": {"name": AWEStatus.QUEUE.value}},
                            "Notes": {
                                "rich_text": [{"type": "text", "text": {"content": notes}}]
                            },
                        },
                    )
                queued.append(c_task.task_id)

        # 3. Check existing certifications for this exact head
        valid_certs = [c for c in certifications if c.head_sha == head_sha]
        gate = self.reconciler.reconcile(
            task_id=task_id,
            frozen_head_sha=head_sha,
            current_head_sha=head_sha,
            certifications=valid_certs,
        )

        if gate.outcome == GateOutcome.ACCEPT:
            # Reconcile ACCEPT: mark producer task Done, activate conditional next task
            next_task_id = self._find_conditional_next_task(task_id, all_exchange_tasks)
            if self.source_of_record:
                self.source_of_record.complete_task(
                    producer_task,
                    verdict=f"ACCEPT — {head_sha[:7]}",
                    evidence_ref=head_sha,
                    notes=gate.detail,
                )
                if next_task_id:
                    # Activate dependent next task
                    next_task = next((t for t in all_exchange_tasks if t.task_id == next_task_id), None)
                    if next_task and next_task.dashboard_page_id:
                        self.source_of_record.client.update_page(
                            page_id=next_task.dashboard_page_id,
                            properties={"Status": {"select": {"name": AWEStatus.QUEUE.value}}},
                        )

            return CadenceAction(
                action_type="GATE_ACCEPTED",
                task_id=task_id,
                head_sha=head_sha,
                verdict="ACCEPT",
                activated_next_task=next_task_id,
                detail=gate.detail,
            )

        elif gate.outcome == GateOutcome.REJECT:
            # Reconcile REJECT: route rework back to same producer lineage, keep next task inactive
            if self.source_of_record:
                self.source_of_record.route_rework(
                    producer_task,
                    verdict="REWORK_REQUIRED",
                    defects=gate.defects,
                )
            return CadenceAction(
                action_type="GATE_REJECTED",
                task_id=task_id,
                head_sha=head_sha,
                verdict="REWORK_REQUIRED",
                defects=gate.defects,
                detail=gate.detail,
            )

        return CadenceAction(
            action_type="WAITING_CERTIFIERS",
            task_id=task_id,
            head_sha=head_sha,
            queued_tasks=queued,
            detail=f"Waiting for certifiers: {', '.join(gate.missing_roles)}",
        )

    def _find_conditional_next_task(
        self,
        task_id: str,
        all_tasks: Sequence[AWEWorkItem],
    ) -> str | None:
        """Find the next dependent task blocked on completion of this task."""
        # Check notes or dependency conventions (e.g. SF-202 waits for SF-212)
        for t in all_tasks:
            if task_id.lower() in t.notes.lower() and "wait" in t.status.lower() or "blocked" in t.status.lower():
                return t.task_id
        return None
