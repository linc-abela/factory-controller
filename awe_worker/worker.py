"""Autonomous AWE Worker core cycle, coordination, and lifecycle management."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .cadence import CadenceAction, CadenceContinuationCoordinator
from .detector import CompletionDetector
from .escalation import OwnerEscalationGate
from .grounding import ContextBrokerGrounder
from .harness import (
    AntigravityHarnessAdapter,
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    CursorHarnessAdapter,
    HarnessAdapter,
    MockHarnessAdapter,
)
from .ledger import AWELedger
from .model import (
    AWEStatus,
    AWEWorkItem,
    ClaimResult,
    CompletionReport,
    CycleSummary,
    EscalationReport,
    ExecutionSlot,
    GateDecision,
    GateOutcome,
    GroundingResult,
    WakeReceipt,
)
from .notion import NotionClient, NotionSourceOfRecord
from .observation import AWEObservationService, MemoryTaskSource, TaskSource
from .reconciliation import TurnCadenceReconciler


class AWEAutonomousWorker:
    """End-to-end Autonomous Worker for Agent Work Exchange queue consumption."""

    def __init__(
        self,
        ledger: AWELedger,
        source: TaskSource,
        grounder: ContextBrokerGrounder | None = None,
        detector: CompletionDetector | None = None,
        reconciler: TurnCadenceReconciler | None = None,
        escalation_gate: OwnerEscalationGate | None = None,
        harness_adapters: Mapping[str, HarnessAdapter] | None = None,
        target_repo: str | Path | None = None,
        source_of_record: NotionSourceOfRecord | None = None,
        cadence_coordinator: CadenceContinuationCoordinator | None = None,
    ) -> None:
        self.ledger = ledger
        self.observation = AWEObservationService(source)
        self.grounder = grounder or ContextBrokerGrounder()
        self.detector = detector or CompletionDetector(default_repo=target_repo)
        self.reconciler = reconciler or TurnCadenceReconciler()
        self.escalation = escalation_gate or OwnerEscalationGate()
        self.target_repo = Path(target_repo) if target_repo else Path(".")

        # Auto-initialize source of record if Notion credentials are present
        if source_of_record is None:
            nc = NotionClient()
            if nc.is_configured:
                source_of_record = NotionSourceOfRecord(client=nc)
        self.source_of_record = source_of_record

        self.cadence = cadence_coordinator or CadenceContinuationCoordinator(
            reconciler=self.reconciler,
            source_of_record=self.source_of_record,
        )

        default_adapters: dict[str, HarnessAdapter] = {
            "antigravity": AntigravityHarnessAdapter(),
            "codex": CodexHarnessAdapter(),
            "cursor": CursorHarnessAdapter(),
            "claude": ClaudeHarnessAdapter(),
        }
        if harness_adapters:
            default_adapters.update(harness_adapters)
        self.adapters = default_adapters

    def run_cycle(
        self,
        worker_id: str,
        target_slot: ExecutionSlot | None = None,
        dry_run: bool = False,
        now: float | None = None,
    ) -> CycleSummary:
        """Execute one bounded autonomous intake, dispatch, detection, and reconciliation cycle."""
        ts = time.time() if now is None else now
        cycle_id = f"cyc_{uuid.uuid4().hex[:12]}"

        if target_slot is None:
            return CycleSummary(
                worker_id=worker_id,
                cycle_id=cycle_id,
                observed_tasks=0,
                claimed_task=None,
                grounding_source=None,
                wake_receipt=None,
                completion_report=None,
                gate_decision=None,
                escalation=None,
                health="refused",
                detail="TARGET_SLOT_REQUIRED: exact (harness, model, effort) is mandatory for execution",
            )

        # 1. Clean up expired leases to recover stale tasks
        self.ledger.clean_expired_leases(now=ts)

        # 2. Observe all tasks
        all_tasks = self.observation.observe_all()
        eligible_tasks = self.observation.filter_eligible(slot=target_slot, tasks=all_tasks)

        if not eligible_tasks:
            return CycleSummary(
                worker_id=worker_id,
                cycle_id=cycle_id,
                observed_tasks=len(all_tasks),
                claimed_task=None,
                grounding_source=None,
                wake_receipt=None,
                completion_report=None,
                gate_decision=None,
                escalation=None,
                health="idle",
                detail="No eligible tasks found for slot in Queue/In Progress",
            )

        task = eligible_tasks[0]

        if dry_run:
            grounding = self.grounder.ground_task(repo_path=self.target_repo, task=task)
            adapter = self.adapters.get(task.slot.harness.lower())
            if adapter is None:
                wake_receipt = WakeReceipt(
                    success=False,
                    harness=task.slot.harness,
                    slot_key=task.slot.key,
                    error_code=f"HARNESS_WAKE_PATH_UNAVAILABLE:{task.slot.harness}",
                    detail=f"No harness adapter configured for '{task.slot.harness}'",
                )
            else:
                wake_receipt = adapter.wake(task, grounding=grounding, dry_run=True)
            return CycleSummary(
                worker_id=worker_id,
                cycle_id=cycle_id,
                observed_tasks=len(all_tasks),
                claimed_task=None,
                grounding_source=grounding.source,
                wake_receipt=wake_receipt,
                completion_report=None,
                gate_decision=None,
                escalation=None,
                health="dry_run",
                detail=f"DRY_RUN: observed {task.task_id} for slot {target_slot.key}; no claim/lifecycle mutation",
            )

        # 3. Check Owner escalation triggers
        escalation = self.escalation.check_escalation(task)
        if escalation.escalated:
            claim_res = self.ledger.claim(
                task_id=task.task_id,
                lineage_id=task.task_id,
                worker_id=worker_id,
                slot_key=task.slot.key,
                lease_seconds=300.0,
                now=ts,
            )
            if claim_res.ok and claim_res.token:
                self.ledger.block(
                    task_id=task.task_id,
                    claim_token=claim_res.token,
                    reason=escalation.reason_code,
                    detail=escalation.detail,
                    now=ts,
                )
            if self.source_of_record:
                self.source_of_record.block_task(
                    task=task,
                    reason=escalation.reason_code,
                    detail=escalation.detail,
                )
            return CycleSummary(
                worker_id=worker_id,
                cycle_id=cycle_id,
                observed_tasks=len(all_tasks),
                claimed_task=task.task_id,
                grounding_source=None,
                wake_receipt=None,
                completion_report=None,
                gate_decision=None,
                escalation=escalation,
                health="escalated",
                detail=f"Task {task.task_id} requires Owner authority ({escalation.reason_code}); blocked autonomously",
            )

        # 4. Atomic claim in SQLite ledger
        claim_res = self.ledger.claim(
            task_id=task.task_id,
            lineage_id=task.task_id,
            worker_id=worker_id,
            slot_key=task.slot.key,
            lease_seconds=120.0,
            now=ts,
        )

        if not claim_res.ok:
            return CycleSummary(
                worker_id=worker_id,
                cycle_id=cycle_id,
                observed_tasks=len(all_tasks),
                claimed_task=task.task_id,
                grounding_source=None,
                wake_receipt=None,
                completion_report=None,
                gate_decision=None,
                escalation=None,
                health="conflict",
                detail=f"Claim refused: {claim_res.code} - {claim_res.detail}",
            )

        # 4b. Fail-closed physical AWE + dashboard reconciliation (not Notion CAS)
        if self.source_of_record and task.status == AWEStatus.QUEUE.value:
            sor_ok, sor_err = self.source_of_record.claim_task(
                task=task,
                worker_id=worker_id,
                slot_key=task.slot.key,
                lease_seconds=120.0,
            )
            if not sor_ok:
                # Fencing conflict on Notion source of record! Roll back local lease
                if claim_res.token:
                    self.ledger.block(
                        task.task_id,
                        claim_res.token,
                        "SOURCE_OF_RECORD_CONFLICT",
                        sor_err,
                        now=ts,
                    )
                return CycleSummary(
                    worker_id=worker_id,
                    cycle_id=cycle_id,
                    observed_tasks=len(all_tasks),
                    claimed_task=task.task_id,
                    grounding_source=None,
                    wake_receipt=None,
                    completion_report=None,
                    gate_decision=None,
                    escalation=None,
                    health="conflict",
                    detail=f"Source-of-record claim conflict: {sor_err}",
                )

        # 5. Context Broker Grounding (Preferred fast bounded path)
        grounding = self.grounder.ground_task(
            repo_path=self.target_repo,
            task=task,
        )

        # 6. Harness Wake / Dispatch
        adapter = self.adapters.get(task.slot.harness.lower())
        if adapter is None:
            wake_receipt = WakeReceipt(
                success=False,
                harness=task.slot.harness,
                slot_key=task.slot.key,
                error_code=f"HARNESS_WAKE_PATH_UNAVAILABLE:{task.slot.harness}",
                detail=f"No harness adapter configured for '{task.slot.harness}'",
            )
        else:
            wake_receipt = adapter.wake(task, grounding=grounding, dry_run=dry_run)

        # 7. Check completion if task is already in progress
        completion = None
        gate_decision = None
        if task.status == AWEStatus.IN_PROGRESS.value or claim_res.action == "resumed":
            completion = self.detector.detect_state(
                task=task,
                repo_path=self.target_repo,
                agent_reported_done=(task.verdict != ""),
            )
            if completion.state == "DONE" and completion.head_sha:
                # Freeze candidate head in ledger
                candidate = self.ledger.freeze_candidate(
                    task_id=task.task_id,
                    branch_name=completion.branch_name,
                    head_sha=completion.head_sha,
                    base_sha=completion.base_sha,
                    pr_number=completion.pr_number,
                    now=ts,
                )
                certs = self.ledger.get_certifications(task.task_id, candidate.head_sha)
                gate_decision = self.reconciler.reconcile(
                    task_id=task.task_id,
                    frozen_head_sha=candidate.head_sha,
                    current_head_sha=completion.head_sha,
                    certifications=certs,
                )

                # Execute live cadence coordination and write-back
                cadence_action = self.cadence.coordinate_candidate_cadence(
                    candidate=candidate,
                    producer_task=task,
                    all_exchange_tasks=all_tasks,
                    certifications=certs,
                )

                if gate_decision.outcome == GateOutcome.ACCEPT and claim_res.token:
                    self.ledger.complete(
                        task_id=task.task_id,
                        claim_token=claim_res.token,
                        verdict="ACCEPT",
                        evidence_ref=completion.head_sha,
                        now=ts,
                    )
                elif gate_decision.outcome == GateOutcome.REJECT:
                    if claim_res.token:
                        self.ledger.block(
                            task_id=task.task_id,
                            claim_token=claim_res.token,
                            reason="GATE_REJECTED",
                            detail="; ".join(gate_decision.defects),
                            now=ts,
                        )

        return CycleSummary(
            worker_id=worker_id,
            cycle_id=cycle_id,
            observed_tasks=len(all_tasks),
            claimed_task=task.task_id,
            grounding_source=grounding.source,
            wake_receipt=wake_receipt,
            completion_report=completion,
            gate_decision=gate_decision,
            escalation=escalation,
            health="healthy" if wake_receipt.success else "harness_unavailable",
            detail=f"Task {task.task_id} {claim_res.action}; grounding: {grounding.source}; wake: {wake_receipt.error_code or 'ok'}",
        )
