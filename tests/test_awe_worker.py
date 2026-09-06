"""Comprehensive adversarial and behavioral test suite for Autonomous AWE Worker.

Covers:
1. Queue observation and slot-exact filtering
2. Atomic claim, concurrency race, lease expiry, and crash recovery
3. Harness wake adapters & truthful HARNESS_WAKE_PATH_UNAVAILABLE:cursor proof
4. Completion detection and anti-forgery guards
5. Turn cadence certification gate, candidate freeze, and anti-drift protection
6. Owner escalation boundaries
7. Context Broker grounding, freshness verification, context volume reduction, and git fallback
8. Controlled autonomous cycle demonstration
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from awe_worker.detector import CompletionDetector
from awe_worker.escalation import OwnerEscalationGate
from awe_worker.grounding import ContextBrokerGrounder
from awe_worker.harness import (
    AntigravityHarnessAdapter,
    CodexHarnessAdapter,
    CursorHarnessAdapter,
    MockHarnessAdapter,
    get_adapter_for_harness,
)
from awe_worker.ledger import AWELedger
from awe_worker.model import (
    AWEStatus,
    AWEWorkItem,
    CertificationRecord,
    CertificationVerdict,
    ExecutionSlot,
    GateOutcome,
)
from awe_worker.observation import (
    AWEObservationService,
    DirectoryTaskSource,
    MemoryTaskSource,
)
from awe_worker.reconciliation import TurnCadenceReconciler
from awe_worker.worker import AWEAutonomousWorker


class QueueObservationTests(unittest.TestCase):
    def setUp(self):
        self.t1 = AWEWorkItem(
            task_id="SF-212",
            title="SF-212 — Sandbox-Only Harness",
            lane="Cursor",
            role="Main Developer",
            status="Queue",
            model="Grok 4.6",
            effort="High",
            sequence=212,
        )
        self.t2 = AWEWorkItem(
            task_id="SF-216",
            title="SF-216 — Phase-2 Core Closure Ledger",
            lane="Codex",
            role="Review",
            status="Queue",
            model="GPT-5.6 Luna",
            effort="Max",
            sequence=216,
        )
        self.t3 = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 — Autonomous AWE Worker",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
        )
        self.t4 = AWEWorkItem(
            task_id="SF-218",
            title="SF-218 — Next Antigravity High Task",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=218,
        )
        self.source = MemoryTaskSource([self.t4, self.t2, self.t1, self.t3])
        self.service = AWEObservationService(self.source)

    def test_filter_exact_slot(self):
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        eligible = self.service.filter_eligible(slot=slot)
        self.assertEqual(len(eligible), 2)
        self.assertEqual(eligible[0].task_id, "SF-217")
        self.assertEqual(eligible[1].task_id, "SF-218")

    def test_slot_mismatch_rejection(self):
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="medium")
        eligible = self.service.filter_eligible(slot=slot)
        self.assertEqual(len(eligible), 0)

    def test_resumption_priority(self):
        in_progress_task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 in progress",
            lane="Antigravity",
            role="Parallel Developer",
            status="In Progress",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
        )
        source = MemoryTaskSource([self.t4, in_progress_task])
        service = AWEObservationService(source)
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        eligible = service.filter_eligible(slot=slot)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].task_id, "SF-217")
        self.assertEqual(eligible[0].status, "In Progress")


class AtomicClaimAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.ledger = AWELedger(":memory:")
        self.slot = "antigravity/gemini-3.8-flash/high"

    def test_single_worker_claim_success(self):
        res = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60)
        self.assertTrue(res.ok)
        self.assertEqual(res.action, "claimed")
        self.assertIsNotNone(res.token)

    def test_duplicate_claim_conflict(self):
        res1 = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60)
        self.assertTrue(res1.ok)

        # Worker 2 tries to claim the same task before lease expires
        res2 = self.ledger.claim("SF-217", "SF-217", "worker-2", self.slot, lease_seconds=60)
        self.assertFalse(res2.ok)
        self.assertEqual(res2.code, "CLAIM_CONFLICT")
        self.assertEqual(res2.action, "refused")

    def test_worker_restart_and_resumption(self):
        t0 = 1000.0
        res1 = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60, now=t0)
        self.assertTrue(res1.ok)

        # Worker crashes and restarts at t0 + 10s (same worker identity & slot)
        res2 = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60, now=t0 + 10.0)
        self.assertTrue(res2.ok)
        self.assertEqual(res2.action, "resumed")
        self.assertEqual(res2.token, res1.token)

    def test_expired_lease_takeover(self):
        t0 = 1000.0
        res1 = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=30, now=t0)
        self.assertTrue(res1.ok)

        # Worker 2 arrives at t0 + 35s (lease expired)
        res2 = self.ledger.claim("SF-217", "SF-217", "worker-2", self.slot, lease_seconds=30, now=t0 + 35.0)
        self.assertTrue(res2.ok)
        self.assertEqual(res2.action, "claimed")
        self.assertNotEqual(res2.token, res1.token)

    def test_completed_task_refuses_claim(self):
        res = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60)
        self.assertTrue(res.ok)
        self.ledger.complete("SF-217", res.token, "ACCEPT")

        # Attempt to claim completed task
        res_after = self.ledger.claim("SF-217", "SF-217", "worker-2", self.slot, lease_seconds=60)
        self.assertFalse(res_after.ok)
        self.assertEqual(res_after.code, "TASK_ALREADY_COMPLETED")


class HarnessAdapterTests(unittest.TestCase):
    def test_cursor_truthful_refusal(self):
        adapter = CursorHarnessAdapter()
        avail, code, detail = adapter.check_availability()
        # On this host, Cursor CLI status is 'Not logged in' or binary missing in test env
        self.assertFalse(avail)
        self.assertIn("HARNESS_WAKE_PATH_UNAVAILABLE:cursor", code)

        task = AWEWorkItem(
            task_id="SF-212",
            title="SF-212",
            lane="Cursor",
            role="developer",
            status="Queue",
            model="Grok 4.6",
            effort="High",
            sequence=212,
        )
        receipt = adapter.wake(task)
        self.assertFalse(receipt.success)
        self.assertEqual(receipt.harness, "cursor")
        self.assertEqual(receipt.error_code, "HARNESS_WAKE_PATH_UNAVAILABLE:cursor")

    def test_antigravity_adapter_dry_run(self):
        adapter = AntigravityHarnessAdapter()
        task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
            lane="Antigravity",
            role="developer",
            status="Queue",
            model="gemini-3.8-flash-high",
            effort="high",
            sequence=217,
        )
        with patch.object(adapter, "check_availability", return_value=(True, "HARNESS_READY", "ok")):
            receipt = adapter.wake(task, dry_run=True)
            self.assertTrue(receipt.success)
            self.assertIn("--model", receipt.command)
            self.assertIn("gemini-3.8-flash-high", receipt.command)

    def test_codex_adapter_dry_run(self):
        adapter = CodexHarnessAdapter()
        task = AWEWorkItem(
            task_id="SF-216",
            title="SF-216",
            lane="Codex",
            role="review",
            status="Queue",
            model="gpt-5.6-luna",
            effort="max",
            sequence=216,
        )
        with patch.object(adapter, "check_availability", return_value=(True, "HARNESS_READY", "ok")):
            receipt = adapter.wake(task, dry_run=True)
            self.assertTrue(receipt.success)
            self.assertIn("exec", receipt.command)
            self.assertTrue(any("model_reasoning_effort=max" in arg for arg in receipt.command))


class TurnCadenceReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.reconciler = TurnCadenceReconciler(required_roles=("review", "qa"))
        self.head = "1111222233334444555566667777888899990000"

    def test_missing_certifier_is_pending(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="review",
                slot_key="codex/gpt-5.6-luna/max",
                verdict=CertificationVerdict.ACCEPT,
            )
        ]
        decision = self.reconciler.reconcile("SF-212", self.head, self.head, certs)
        self.assertEqual(decision.outcome, GateOutcome.PENDING)
        self.assertEqual(decision.missing_roles, ["qa"])

    def test_both_accept_integrates_and_activates_next_task(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="review",
                slot_key="codex/gpt-5.6-luna/max",
                verdict=CertificationVerdict.ACCEPT,
            ),
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="qa",
                slot_key="antigravity/gemini-3.8-flash/high",
                verdict=CertificationVerdict.ACCEPT,
            ),
        ]
        decision = self.reconciler.reconcile(
            "SF-212", self.head, self.head, certs, real_post_merge_main_sha="postmerge_sha"
        )
        self.assertEqual(decision.outcome, GateOutcome.ACCEPT)
        self.assertEqual(decision.next_action, "INTEGRATE_AND_ACTIVATE_NEXT_TASK")
        self.assertEqual(decision.new_main_sha, "postmerge_sha")

    def test_qa_reject_routes_rework_only_and_blocks_next_task(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="review",
                slot_key="codex/gpt-5.6-luna/max",
                verdict=CertificationVerdict.ACCEPT,
            ),
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="qa",
                slot_key="antigravity/gemini-3.8-flash/high",
                verdict=CertificationVerdict.REJECT,
                defects=["Uncontained host socket leak discovered"],
            ),
        ]
        decision = self.reconciler.reconcile("SF-212", self.head, self.head, certs)
        self.assertEqual(decision.outcome, GateOutcome.REJECT)
        self.assertEqual(decision.next_action, "ROUTER_REWORK_SAME_LINEAGE")
        self.assertIn("Uncontained host socket leak discovered", decision.defects)

    def test_stale_head_mutation_during_review_rejects(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="review",
                slot_key="codex/gpt-5.6-luna/max",
                verdict=CertificationVerdict.ACCEPT,
            ),
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.head,
                role="qa",
                slot_key="antigravity/gemini-3.8-flash/high",
                verdict=CertificationVerdict.ACCEPT,
            ),
        ]
        new_head = "9999888877776666555544443333222211110000"
        decision = self.reconciler.reconcile("SF-212", self.head, new_head, certs)
        self.assertEqual(decision.outcome, GateOutcome.REJECT)
        self.assertIn("STALE_PR_HEAD_DETECTED", decision.defects[0])


class CompletionDetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = CompletionDetector()

    def test_forged_done_when_no_branch_exists(self):
        task = AWEWorkItem(
            task_id="SF-999",
            title="SF-999",
            lane="Antigravity",
            role="developer",
            status="Done",
            model="gemini",
            effort="high",
            sequence=999,
            verdict="ACCEPT",
        )
        report = self.detector.detect_state(task, repo_path=".", agent_reported_done=True)
        self.assertEqual(report.state, "FORGED_DONE")
        self.assertIn("EVIDENCE_FORGERY_DETECTED", report.detail)


class OwnerEscalationTests(unittest.TestCase):
    def setUp(self):
        self.gate = OwnerEscalationGate()

    def test_explicit_owner_only_task(self):
        task = AWEWorkItem(
            task_id="SF-208",
            title="SF-208 — Production candidate",
            lane="Cursor",
            role="Specialist",
            status="Queue",
            model="Opus",
            effort="High",
            sequence=208,
            owner_only=True,
            owner_reason="production_promotion",
        )
        report = self.gate.check_escalation(task)
        self.assertTrue(report.escalated)
        self.assertEqual(report.reason_code, "production_promotion")

    def test_nondelegable_keyword_triggers_escalation(self):
        task = AWEWorkItem(
            task_id="SF-299",
            title="SF-299 — Wipe database and reset credentials",
            lane="Antigravity",
            role="Developer",
            status="Queue",
            model="Gemini",
            effort="High",
            sequence=299,
        )
        report = self.gate.check_escalation(task)
        self.assertTrue(report.escalated)
        self.assertEqual(report.reason_code, "destructive_history")

    def test_autonomous_task_not_escalated(self):
        task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 — Autonomous AWE Worker + Context-Broker Grounding v1",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
        )
        report = self.gate.check_escalation(task)
        self.assertFalse(report.escalated)


class ContextBrokerGroundingTests(unittest.TestCase):
    def setUp(self):
        self.grounder = ContextBrokerGrounder()

    def test_direct_git_fallback_when_broker_unavailable(self):
        task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
            lane="Antigravity",
            role="developer",
            status="Queue",
            model="Gemini",
            effort="high",
            sequence=217,
        )
        res = self.grounder.ground_task(repo_path=".", task=task, force_fallback=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.source, "direct_git_fallback")
        self.assertTrue(len(res.selected_paths) > 0)
        self.assertIn("README.md", res.selected_paths)

    def test_broker_grounding_measures_context_reduction(self):
        task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
            lane="Antigravity",
            role="developer",
            status="Queue",
            model="Gemini",
            effort="high",
            sequence=217,
        )
        # Mock broker receipt
        mock_res = {
            "manifest_digest": "sha256:abc123456789",
            "manifest_ref": {"digest": "abc"},
            "selected_paths": ["README.md", "dev"],
            "overview": {"authoritative": ["README.md"]},
            "full_eligible_bytes": 100000,
            "selected_bytes": 5000,
        }
        with patch.object(self.grounder, "_try_broker", return_value=mock_res):
            res = self.grounder.ground_task(repo_path=".", task=task)
            self.assertTrue(res.ok)
            self.assertEqual(res.source, "broker")
            self.assertEqual(res.manifest_digest, "sha256:abc123456789")
            self.assertEqual(res.full_eligible_bytes, 100000)
            self.assertEqual(res.selected_bytes, 5000)
            self.assertAlmostEqual(res.reduction_ratio, 0.95, places=2)


class ControlledAutonomousCycleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = AWELedger(":memory:")
        self.task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 — Autonomous AWE Worker",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
        )
        self.source = MemoryTaskSource([self.task])
        self.mock_adapter = MockHarnessAdapter("antigravity")
        self.worker = AWEAutonomousWorker(
            ledger=self.ledger,
            source=self.source,
            harness_adapters={"antigravity": self.mock_adapter},
            target_repo=".",
        )

    def test_autonomous_intake_claim_ground_and_wake(self):
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        summary = self.worker.run_cycle(worker_id="test-worker", target_slot=slot, dry_run=True)

        self.assertEqual(summary.claimed_task, "SF-217")
        self.assertIsNotNone(summary.wake_receipt)
        self.assertTrue(summary.wake_receipt.success)
        self.assertEqual(len(self.mock_adapter.woken_tasks), 1)
        self.assertEqual(self.mock_adapter.woken_tasks[0].task_id, "SF-217")

        # Verify claim exists in ledger
        claim = self.ledger.get_claim("SF-217")
        self.assertIsNotNone(claim)
        self.assertEqual(claim["state"], "in_progress")
        self.assertEqual(claim["worker_id"], "test-worker")


if __name__ == "__main__":
    unittest.main()
