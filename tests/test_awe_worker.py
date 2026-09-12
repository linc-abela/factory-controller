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

from awe_worker.cadence import CadenceContinuationCoordinator
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
    CandidateHead,
    CertificationRecord,
    CertificationVerdict,
    ExecutionSlot,
    GateOutcome,
)
from awe_worker.notion import NotionAPIError, NotionSourceOfRecord
from awe_worker.observation import (
    AWEObservationService,
    DirectoryTaskSource,
    MemoryTaskSource,
)
from awe_worker.reconciliation import TurnCadenceReconciler
from awe_worker.scheduler import AWEScheduledRunner
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
        first = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60)
        second = self.ledger.claim("SF-217", "SF-217", "worker-2", self.slot, lease_seconds=60)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "CLAIM_CONFLICT")

    def test_exact_slot_ownership_is_unique(self):
        first = self.ledger.claim("SF-217", "SF-217", "worker-1", self.slot, lease_seconds=60)
        second = self.ledger.claim("SF-219", "SF-219", "worker-2", self.slot, lease_seconds=60)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "SLOT_ALREADY_OWNED")

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
            self.assertEqual(receipt.command[-1], "Process your Queue.")
            self.assertFalse(any("SF-217" in str(arg) for arg in receipt.command))

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
            self.assertEqual(receipt.command[-1], "Process your Queue.")
            self.assertFalse(any("SF-216" in str(arg) for arg in receipt.command))


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

    def test_both_accept_proposes_canonical_integration(self):
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
        self.assertEqual(decision.next_action, "PROPOSE_CANONICAL_INTEGRATION")
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
        self.assertGreater(res.full_eligible_bytes, 0)
        self.assertGreaterEqual(res.full_eligible_bytes, res.selected_bytes)
        self.assertNotEqual(res.full_eligible_bytes, res.selected_bytes * 5)
        self.assertGreaterEqual(res.reduction_ratio, 0.0)

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
        summary = self.worker.run_cycle(worker_id="test-worker", target_slot=slot, dry_run=False)

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

    def test_dry_run_never_mutates_claim_or_lifecycle(self):
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        summary = self.worker.run_cycle(worker_id="test-worker", target_slot=slot, dry_run=True)
        self.assertIsNone(summary.claimed_task)
        self.assertEqual(summary.health, "dry_run")
        self.assertIsNone(self.ledger.get_claim("SF-217"))

    def test_missing_target_slot_refuses_execution(self):
        summary = self.worker.run_cycle(worker_id="test-worker", target_slot=None, dry_run=False)
        self.assertEqual(summary.health, "refused")
        self.assertIn("TARGET_SLOT_REQUIRED", summary.detail)
        self.assertIsNone(self.ledger.get_claim("SF-217"))


class NotionSourceOfRecordTests(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.sor = NotionSourceOfRecord(client=self.mock_client)
        self.task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 — Autonomous AWE Worker",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
            task_page_id="task-page-123",
            dashboard_page_id="dash-page-123",
        )

    def test_claim_task_reconciles_physical_move_and_dashboard(self):
        self.mock_client.is_configured = True
        # Page retrieval returns physical parent in Queue
        queue_folder_id = "3c5690f6-eb14-815c-a767-d6952b58f0de"
        self.mock_client.retrieve_page.side_effect = [
            {"id": "task-page-123", "parent": {"type": "page_id", "page_id": queue_folder_id}},
            {"id": "dash-page-123", "properties": {"Status": {"select": {"name": "Queue"}}}},
        ]
        ok, err = self.sor.claim_task(self.task, worker_id="w1", slot_key="antigravity/flash/high")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        # Verify physical move called with in_progress folder
        self.mock_client.move_page.assert_called_once()
        move_args, move_kwargs = self.mock_client.move_page.call_args
        self.assertEqual(move_args[0], "task-page-123")
        self.assertEqual(move_kwargs["parent"]["page_id"], "3c5690f6-eb14-817e-ba22-f57ea996fec0")
        # Verify dashboard update called
        self.mock_client.update_page.assert_called_once()
        up_args, up_kwargs = self.mock_client.update_page.call_args
        self.assertEqual(up_kwargs["page_id"], "dash-page-123")
        self.assertEqual(up_kwargs["properties"]["Status"]["select"]["name"], "In Progress")

    def test_claim_task_physical_ancestry_conflict(self):
        self.mock_client.is_configured = True
        # Physical parent is already Done folder, not Queue
        done_folder_id = "3c5690f6-eb14-81b7-b578-c9fad99c2e6a"
        self.mock_client.retrieve_page.return_value = {
            "id": "task-page-123",
            "parent": {"type": "page_id", "page_id": done_folder_id},
        }
        ok, err = self.sor.claim_task(self.task, worker_id="w2", slot_key="antigravity/flash/high")
        self.assertFalse(ok)
        self.assertIn("PHYSICAL_ANCESTRY_CONFLICT", err)
        self.mock_client.move_page.assert_not_called()
        self.mock_client.update_page.assert_not_called()

    def test_claim_task_dashboard_conflict(self):
        self.mock_client.is_configured = True
        queue_folder_id = "3c5690f6-eb14-815c-a767-d6952b58f0de"
        self.mock_client.retrieve_page.side_effect = [
            {"id": "task-page-123", "parent": {"type": "page_id", "page_id": queue_folder_id}},
            {"id": "dash-page-123", "properties": {"Status": {"select": {"name": "In Progress"}}, "Notes": {"rich_text": [{"plain_text": "Claimed by other"}]}}},
        ]
        ok, err = self.sor.claim_task(self.task, worker_id="w2", slot_key="antigravity/flash/high")
        self.assertFalse(ok)
        self.assertIn("SOURCE_OF_RECORD_CONFLICT", err)
        self.mock_client.move_page.assert_not_called()
        self.mock_client.update_page.assert_not_called()

    def test_complete_task_moves_physical_page_to_done(self):
        self.mock_client.is_configured = True
        res = self.sor.complete_task(self.task, verdict="ACCEPT — abc1234", evidence_ref="abc1234", notes="All green")
        self.assertTrue(res)
        self.mock_client.move_page.assert_called_once()
        move_args, move_kwargs = self.mock_client.move_page.call_args
        self.assertEqual(move_args[0], "task-page-123")
        self.assertEqual(move_kwargs["parent"]["page_id"], "3c5690f6-eb14-81b7-b578-c9fad99c2e6a")
        self.mock_client.update_page.assert_called_once()
        up_args, up_kwargs = self.mock_client.update_page.call_args
        self.assertEqual(up_kwargs["properties"]["Status"]["select"]["name"], "Review")

    def test_block_task_moves_physical_page_to_blocked(self):
        self.mock_client.is_configured = True
        res = self.sor.block_task(self.task, reason="NEEDS_OWNER", detail="destructive operation")
        self.assertTrue(res)
        self.mock_client.move_page.assert_called_once()
        move_args, move_kwargs = self.mock_client.move_page.call_args
        self.assertEqual(move_kwargs["parent"]["page_id"], "3c5690f6-eb14-8180-8d85-e791738e1d45")
        self.mock_client.update_page.assert_called_once()
        up_args, up_kwargs = self.mock_client.update_page.call_args
        self.assertEqual(up_kwargs["properties"]["Status"]["select"]["name"], "Blocked")

    def test_route_rework_moves_physical_page_to_queue(self):
        self.mock_client.is_configured = True
        res = self.sor.route_rework(self.task, verdict="REWORK_REQUIRED", defects=["Test failed"])
        self.assertTrue(res)
        self.mock_client.move_page.assert_called_once()
        move_args, move_kwargs = self.mock_client.move_page.call_args
        self.assertEqual(move_kwargs["parent"]["page_id"], "3c5690f6-eb14-815c-a767-d6952b58f0de")
        self.mock_client.update_page.assert_called_once()
        up_args, up_kwargs = self.mock_client.update_page.call_args
        self.assertEqual(up_kwargs["properties"]["Status"]["select"]["name"], "Queue")

    def test_complete_task_fail_closed_when_physical_move_fails(self):
        self.mock_client.is_configured = True
        self.mock_client.move_page.side_effect = NotionAPIError(503, "unavailable")
        res = self.sor.complete_task(self.task, verdict="ACCEPT", evidence_ref="abc")
        self.assertFalse(res)
        self.mock_client.update_page.assert_not_called()

    def test_block_task_fail_closed_when_physical_move_fails(self):
        self.mock_client.is_configured = True
        self.mock_client.move_page.side_effect = NotionAPIError(500, "move failed")
        res = self.sor.block_task(self.task, reason="NEEDS_OWNER", detail="x")
        self.assertFalse(res)
        self.mock_client.update_page.assert_not_called()

    def test_route_rework_fail_closed_when_physical_move_fails(self):
        self.mock_client.is_configured = True
        self.mock_client.move_page.side_effect = NotionAPIError(500, "move failed")
        res = self.sor.route_rework(self.task, verdict="REWORK_REQUIRED", defects=["x"])
        self.assertFalse(res)
        self.mock_client.update_page.assert_not_called()


class TwoTierClaimFencingTests(unittest.TestCase):
    def setUp(self):
        self.ledger = AWELedger(":memory:")
        self.task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
            dashboard_page_id="page-123",
        )
        self.source = MemoryTaskSource([self.task])
        self.mock_client = MagicMock()
        self.mock_client.is_configured = True
        self.sor = NotionSourceOfRecord(client=self.mock_client)
        self.mock_adapter = MockHarnessAdapter("antigravity")

    def test_source_of_record_conflict_rolls_back_sqlite_claim(self):
        # Notion returns that task was already Done in source of record
        self.mock_client.retrieve_page.return_value = {
            "properties": {"Status": {"select": {"name": "Done"}}}
        }
        worker = AWEAutonomousWorker(
            ledger=self.ledger,
            source=self.source,
            source_of_record=self.sor,
            harness_adapters={"antigravity": self.mock_adapter},
            target_repo=".",
        )
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        summary = worker.run_cycle(worker_id="w1", target_slot=slot, dry_run=False)

        self.assertEqual(summary.health, "conflict")
        self.assertIn("Source-of-record claim conflict", summary.detail)
        # Verify SQLite lease was marked blocked/released
        claim = self.ledger.get_claim("SF-217")
        self.assertEqual(claim["state"], "blocked")


class AWEScheduledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = AWELedger(":memory:")
        self.task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
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

    def test_scheduled_runner_bounded_cycles_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hb_file = Path(tmpdir) / "heartbeat.json"
            runner = AWEScheduledRunner(
                worker=self.worker,
                worker_id="runner-1",
                heartbeat_file=hb_file,
            )
            slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
            summaries = runner.run(
                interval_seconds=0.01,
                max_cycles=3,
                target_slot=slot,
                dry_run=True,
            )
            self.assertEqual(len(summaries), 3)
            self.assertTrue(hb_file.is_file())
            hb_data = json.loads(hb_file.read_text(encoding="utf-8"))
            self.assertEqual(hb_data["worker_id"], "runner-1")
            self.assertEqual(hb_data["cycles_completed"], 3)

            # Check liveness table
            liveness = runner.get_liveness_status()
            self.assertEqual(len(liveness), 1)
            self.assertEqual(liveness[0].worker_id, "runner-1")
            self.assertEqual(liveness[0].cycles_completed, 3)

    def test_crashed_worker_recovery(self):
        runner1 = AWEScheduledRunner(
            worker=self.worker,
            worker_id="crashed-worker",
            stale_threshold_seconds=10.0,
        )
        # Register crashed worker at old time
        old_time = time.time() - 200.0
        runner1.record_heartbeat(cycles_completed=1, status="running", now=old_time)

        # Runner 2 starts up
        runner2 = AWEScheduledRunner(
            worker=self.worker,
            worker_id="recovering-worker",
            stale_threshold_seconds=10.0,
        )
        recovered = runner2.recover_crashed_workers(now=time.time())
        self.assertIn("crashed-worker", recovered)

        # Verify status is crashed
        status_list = runner2.get_liveness_status()
        crashed_entry = next(r for r in status_list if r.worker_id == "crashed-worker")
        self.assertEqual(crashed_entry.status, "crashed")


class CadenceContinuationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.is_configured = True
        self.sor = NotionSourceOfRecord(client=self.mock_client)
        self.reconciler = TurnCadenceReconciler(required_roles=["review", "qa"])
        self.coordinator = CadenceContinuationCoordinator(
            reconciler=self.reconciler,
            source_of_record=self.sor,
            required_roles=["review", "qa"],
        )
        self.producer = AWEWorkItem(
            task_id="SF-212",
            title="SF-212 — Sandbox-Only Harness",
            lane="Cursor",
            role="Main Developer",
            status="In Progress",
            model="Grok 4.6",
            effort="High",
            sequence=212,
            dashboard_page_id="page-sf-212",
        )
        self.review_task = AWEWorkItem(
            task_id="SF-213",
            title="SF-213 — SF-212 Security Review",
            lane="Codex",
            role="Review",
            status="Queue",
            model="GPT-5.6 Luna",
            effort="Max",
            sequence=213,
            dashboard_page_id="page-sf-213",
        )
        self.qa_task = AWEWorkItem(
            task_id="SF-214",
            title="SF-214 — SF-212 QA",
            lane="Antigravity",
            role="QA",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=214,
            dashboard_page_id="page-sf-214",
        )
        self.next_task = AWEWorkItem(
            task_id="SF-202",
            title="SF-202 — Hermes Core Runtime Manager",
            lane="Cursor",
            role="Main Developer",
            status="Blocked",
            model="Grok 4.6",
            effort="High",
            sequence=202,
            dashboard_page_id="page-sf-202",
            notes="WAIT_MANAGER / BRIDGE_DEPENDENCY: waits for SF-212",
        )
        self.all_tasks = [self.producer, self.review_task, self.qa_task, self.next_task]
        self.candidate = CandidateHead(
            task_id="SF-212",
            branch_name="sf/SF-212/sandbox-enforcement",
            head_sha="e726e79331fdc7811b1e4b98a5ea8062657c6a76",
            base_sha="48dbd88878a450fb84485af82ae3d9691ff267cd",
            pr_number=2,
            frozen_at=time.time(),
        )

    def test_waiting_certifiers_queues_both(self):
        action = self.coordinator.coordinate_candidate_cadence(
            candidate=self.candidate,
            producer_task=self.producer,
            all_exchange_tasks=self.all_tasks,
            certifications=[],
        )
        self.assertEqual(action.action_type, "WAITING_CERTIFIERS")
        self.assertIn("SF-213", action.queued_tasks)
        self.assertIn("SF-214", action.queued_tasks)

    def test_both_accept_marks_done_and_withholds_merge_authority(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.candidate.head_sha,
                role="review",
                slot_key="codex/luna/max",
                verdict=CertificationVerdict.ACCEPT,
            ),
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.candidate.head_sha,
                role="qa",
                slot_key="antigravity/flash/high",
                verdict=CertificationVerdict.ACCEPT,
            ),
        ]
        action = self.coordinator.coordinate_candidate_cadence(
            candidate=self.candidate,
            producer_task=self.producer,
            all_exchange_tasks=self.all_tasks,
            certifications=certs,
        )
        self.assertEqual(action.action_type, "GATE_ACCEPTED")
        # Integration authority remains external: worker does NOT activate next task
        self.assertIsNone(action.activated_next_task)
        self.assertIn("withheld for canonical integration authority", action.detail)

    def test_reject_routes_rework_and_does_not_activate_next_task(self):
        certs = [
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.candidate.head_sha,
                role="review",
                slot_key="codex/luna/max",
                verdict=CertificationVerdict.REJECT,
                defects=["Host escape flaw detected"],
            ),
            CertificationRecord(
                task_id="SF-212",
                head_sha=self.candidate.head_sha,
                role="qa",
                slot_key="antigravity/flash/high",
                verdict=CertificationVerdict.ACCEPT,
            ),
        ]
        action = self.coordinator.coordinate_candidate_cadence(
            candidate=self.candidate,
            producer_task=self.producer,
            all_exchange_tasks=self.all_tasks,
            certifications=certs,
        )
        self.assertEqual(action.action_type, "GATE_REJECTED")
        self.assertIsNone(action.activated_next_task)
        self.assertIn("Host escape flaw detected", action.defects)


class PhysicalAncestryResolutionTests(unittest.TestCase):
    def test_resolve_physical_folder_status(self):
        from awe_worker.notion import (
            AWE_LANE_FOLDERS,
            AWE_PROCESSED_PAGE_ID,
            resolve_physical_folder_status,
        )
        # Queue
        antigravity_queue = AWE_LANE_FOLDERS["antigravity"]["queue"]
        self.assertEqual(resolve_physical_folder_status(antigravity_queue, "antigravity"), "Queue")

        # In Progress
        antigravity_in_prog = AWE_LANE_FOLDERS["antigravity"]["in_progress"]
        self.assertEqual(resolve_physical_folder_status(antigravity_in_prog, "antigravity"), "In Progress")

        # Blocked
        antigravity_blocked = AWE_LANE_FOLDERS["antigravity"]["blocked"]
        self.assertEqual(resolve_physical_folder_status(antigravity_blocked, "antigravity"), "Blocked")

        # Done
        antigravity_done = AWE_LANE_FOLDERS["antigravity"]["done"]
        self.assertEqual(resolve_physical_folder_status(antigravity_done, "antigravity"), "Review")

        # Processed
        self.assertEqual(resolve_physical_folder_status(AWE_PROCESSED_PAGE_ID), "Processed")

    def test_verify_physical_status_overrides_dashboard_drift(self):
        from awe_worker.notion import LiveNotionTaskSource
        mock_client = MagicMock()
        mock_client.is_configured = True
        # Physical page in In Progress folder, while dashboard row had claimed Queue
        mock_client.retrieve_page.return_value = {
            "id": "task-page-drift",
            "parent": {"type": "page_id", "page_id": "3c5690f6-eb14-817e-ba22-f57ea996fec0"},
        }
        source = LiveNotionTaskSource(client=mock_client)
        task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",  # Drift: dashboard had Queue
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
            task_page_id="task-page-drift",
        )
        verified_task = source.verify_physical_status(task)
        # Physical ancestry overrides dashboard drift!
        self.assertEqual(verified_task.status, "In Progress")


class ConcurrentClaimSingleWinnerTests(unittest.TestCase):
    def test_concurrent_threads_single_winner(self):
        import threading
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "concurrent_test.db"
            ledger = AWELedger(db_path)

            task_id = "SF-CONCURRENT-1"
            results = []
            barrier = threading.Barrier(5)

            def try_claim(worker_num):
                worker_id = f"worker-{worker_num}"
                barrier.wait()
                res = ledger.claim(
                    task_id=task_id,
                    lineage_id=task_id,
                    worker_id=worker_id,
                    slot_key="antigravity/flash/high",
                    lease_seconds=60.0,
                )
                results.append((worker_id, res.ok, res.code))

            threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly one winner!
            winners = [r for r in results if r[1] is True]
            losers = [r for r in results if r[1] is False]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(losers), 4)
            for l in losers:
                self.assertEqual(l[2], "CLAIM_CONFLICT")


class NoCredentialScavengingTests(unittest.TestCase):
    def test_resolve_notion_token_zero_file_scavenging(self):
        from awe_worker.notion import resolve_notion_token
        # 1. Argument takes precedence
        self.assertEqual(resolve_notion_token("explicit-token"), "explicit-token")

        # 2. Environment variable
        with patch.dict(os.environ, {"NOTION_TOKEN": "env-token"}):
            self.assertEqual(resolve_notion_token(), "env-token")

        # 3. If neither provided, returns empty string without reading files
        from awe_worker.credentials import MemorySecretStore, set_default_secret_store
        set_default_secret_store(MemorySecretStore())
        try:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_notion_token(), "")
        finally:
            set_default_secret_store(None)


class CLIContractTests(unittest.TestCase):
    def test_cli_live_and_dry_run_flags(self):
        import argparse
        from awe_worker.cli import _add_dry_run_arguments
        p = argparse.ArgumentParser()
        _add_dry_run_arguments(p)

        # Default is dry_run=True
        args_default = p.parse_args([])
        self.assertTrue(args_default.dry_run)

        # --live makes dry_run=False
        args_live = p.parse_args(["--live"])
        self.assertFalse(args_live.dry_run)

        # --no-dry-run makes dry_run=False
        args_no_dry = p.parse_args(["--no-dry-run"])
        self.assertFalse(args_no_dry.dry_run)

        # --dry-run explicitly keeps dry_run=True
        args_dry = p.parse_args(["--dry-run"])
        self.assertTrue(args_dry.dry_run)


class LiveDemonstrationRegressionTests(unittest.TestCase):
    def test_controlled_demonstration_dry_run(self):
        from validation.awe_worker_live_demo import run_live_demonstration
        results = run_live_demonstration(live=False)
        self.assertTrue(results["passed"])
        self.assertEqual(results["mode"], "MOCK_DRY_RUN")
        self.assertEqual(results["steps"]["step2_observe"], "PASSED")
        self.assertEqual(results["steps"]["step3_claim_concurrency"], "PASSED")
        self.assertEqual(results["steps"]["step4_grounding"], "PASSED")
        self.assertEqual(results["steps"]["step5_harness_wake"], "PASSED")
        self.assertEqual(results["steps"]["step6_terminal_completion"], "PASSED")
        self.assertEqual(results["steps"]["step7_recovery"], "PASSED")
        self.assertEqual(results["steps"]["step8_teardown"], "PASSED")


class DeterministicDispatchTests(unittest.TestCase):
    def setUp(self):
        from awe_worker.dispatch import (
            DISPATCH_PAGE_IDS,
            DISPATCH_STALE,
            NO_EXECUTABLE_TASK,
            DispatchMaintainer,
            DispatchPointer,
            HeadlessDispatchResolver,
            format_pointer_text,
            parse_pointers,
        )
        self.DISPATCH_STALE = DISPATCH_STALE
        self.NO_EXECUTABLE_TASK = NO_EXECUTABLE_TASK
        self.DISPATCH_PAGE_IDS = DISPATCH_PAGE_IDS
        self.DispatchMaintainer = DispatchMaintainer
        self.DispatchPointer = DispatchPointer
        self.HeadlessDispatchResolver = HeadlessDispatchResolver
        self.format_pointer_text = format_pointer_text
        self.parse_pointers = parse_pointers
        self.client = MagicMock()
        self.client.is_configured = True
        self.client.search = MagicMock(side_effect=AssertionError("workspace search is forbidden"))
        self.client.query_database = MagicMock(side_effect=AssertionError("workspace search is forbidden"))
        self.ag_queue = "3c5690f6-eb14-815c-a767-d6952b58f0de"
        self.ag_in_progress = "3c5690f6-eb14-817e-ba22-f57ea996fec0"
        self.task = AWEWorkItem(
            task_id="SF-217",
            title="SF-217 — Autonomous AWE Worker",
            lane="Antigravity",
            role="Parallel Developer",
            status="Queue",
            model="Gemini 3.8 Flash",
            effort="High",
            sequence=217,
            task_page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            task_page_url="https://app.notion.com/p/aaaaaaaabbbbccccddddeeeeeeeeeeee",
            dashboard_page_id="dash-page-123",
        )

    def _children(self, text: str) -> dict:
        return {
            "results": [
                {
                    "id": f"blk-{i}",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": line}]},
                }
                for i, line in enumerate(text.splitlines())
            ]
        }

    def test_claim_updates_dispatch_in_same_pass(self):
        self.client.retrieve_page.side_effect = [
            {"id": self.task.task_page_id, "parent": {"type": "page_id", "page_id": self.ag_queue}},
            {"id": "dash-page-123", "properties": {"Status": {"select": {"name": "Queue"}}}},
        ]
        self.client.retrieve_block_children.return_value = {"results": []}
        sor = NotionSourceOfRecord(client=self.client)
        ok, err = sor.claim_task(self.task, worker_id="w1", slot_key="antigravity/gemini-3.8-flash/high")
        self.assertTrue(ok, err)
        self.client.move_page.assert_called_once()
        self.client.append_block_children.assert_called_once()
        args, kwargs = self.client.append_block_children.call_args
        self.assertEqual(args[0], self.DISPATCH_PAGE_IDS["antigravity"])
        rendered = " ".join(
            b["paragraph"]["rich_text"][0]["text"]["content"] for b in args[1]
        )
        self.assertIn("SF-217", rendered)
        self.assertIn("In Progress", rendered)
        self.assertIn("EXECUTABLE", rendered)

    def test_missing_pointer_is_stale(self):
        self.client.retrieve_block_children.return_value = {"results": []}
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, self.DISPATCH_STALE)
        self.client.search.assert_not_called()
        self.client.query_database.assert_not_called()

    def test_lifecycle_mismatch_is_stale(self):
        pointer_text = self.format_pointer_text(
            self.DispatchPointer(
                task_id="SF-217",
                execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                expected_lifecycle_state="Queue",
                task_page_url=self.task.task_page_url,
                dispatch_state="EXECUTABLE",
                harness="antigravity",
            )
        )
        self.client.retrieve_block_children.return_value = self._children(pointer_text)
        self.client.retrieve_page.return_value = {
            "id": self.task.task_page_id,
            "parent": {"type": "page_id", "page_id": self.ag_in_progress},
        }
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, self.DISPATCH_STALE)
        self.assertIn("physical ancestry", result.detail)

    def test_duplicate_slot_without_identity_fails_closed(self):
        text = "\n".join(
            [
                self.format_pointer_text(
                    self.DispatchPointer(
                        task_id="SF-217",
                        execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                        expected_lifecycle_state="Queue",
                        task_page_url=self.task.task_page_url,
                        dispatch_state="EXECUTABLE",
                        harness="antigravity",
                    )
                ),
                self.format_pointer_text(
                    self.DispatchPointer(
                        task_id="SF-219",
                        execution_profile="Antigravity -> Gemini 3.8 Flash / Medium",
                        expected_lifecycle_state="Queue",
                        task_page_url="https://app.notion.com/p/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        dispatch_state="EXECUTABLE",
                        harness="antigravity",
                    )
                ),
            ]
        )
        self.client.retrieve_block_children.return_value = self._children(text)
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, self.DISPATCH_STALE)
        self.assertIn("duplicate-slot", result.detail)

    def test_duplicate_slot_with_identity_selects_exact_pointer(self):
        text = "\n".join(
            [
                self.format_pointer_text(
                    self.DispatchPointer(
                        task_id="SF-217",
                        execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                        expected_lifecycle_state="Queue",
                        task_page_url=self.task.task_page_url,
                        dispatch_state="EXECUTABLE",
                        harness="antigravity",
                    )
                ),
                self.format_pointer_text(
                    self.DispatchPointer(
                        task_id="SF-219",
                        execution_profile="Antigravity -> Gemini 3.8 Flash / Medium",
                        expected_lifecycle_state="Queue",
                        task_page_url="https://app.notion.com/p/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        dispatch_state="EXECUTABLE",
                        harness="antigravity",
                    )
                ),
            ]
        )
        self.client.retrieve_block_children.return_value = self._children(text)
        self.client.retrieve_page.return_value = {
            "id": self.task.task_page_id,
            "parent": {"type": "page_id", "page_id": self.ag_queue},
        }
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.8-flash", effort="high")
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity", slot=slot)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.code, "OK")
        self.assertEqual(result.pointer.task_id, "SF-217")
        self.client.search.assert_not_called()
        self.client.query_database.assert_not_called()
        self.client.retrieve_page.assert_called_once_with(self.task.task_page_id)

    def test_no_executable_task_stops_without_search(self):
        pointer_text = self.format_pointer_text(
            self.DispatchPointer(
                task_id="",
                execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                expected_lifecycle_state="Done",
                task_page_url="",
                dispatch_state="NO_EXECUTABLE_TASK",
                harness="antigravity",
            )
        )
        self.client.retrieve_block_children.return_value = self._children(pointer_text)
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, self.NO_EXECUTABLE_TASK)
        self.client.retrieve_page.assert_not_called()
        self.client.search.assert_not_called()

    def test_restart_reconciles_from_dispatch_pointer(self):
        pointer_text = self.format_pointer_text(
            self.DispatchPointer(
                task_id="SF-217",
                execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                expected_lifecycle_state="In Progress",
                task_page_url=self.task.task_page_url,
                dispatch_state="EXECUTABLE",
                harness="antigravity",
            )
        )
        self.client.retrieve_block_children.return_value = self._children(pointer_text)
        self.client.retrieve_page.return_value = {
            "id": self.task.task_page_id,
            "parent": {"type": "page_id", "page_id": self.ag_in_progress},
        }
        first = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        second = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertEqual(first.code, "OK")
        self.assertEqual(second.code, "OK")
        self.assertEqual(first.pointer.task_id, second.pointer.task_id)
        self.assertEqual(self.client.retrieve_block_children.call_count, 2)
        self.client.search.assert_not_called()
        self.client.query_database.assert_not_called()

    def test_native_mention_task_page_parses(self):
        text = (
            "## Current pointer\n"
            "- Task ID: `SF-217`\n"
            "- Execution profile: **Cursor → Grok 4.6 / High**\n"
            "- Expected lifecycle state: `Queue`\n"
            "- Task page: <mention-page url=\"https://app.notion.com/p/aaaaaaaabbbbccccddddeeeeeeeeeeee\"/>\n"
            "- Dispatch state: `EXECUTABLE`\n"
        )
        pointers = self.parse_pointers(text, harness="cursor")
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0].task_id, "SF-217")
        self.assertEqual(pointers[0].task_page_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertNotIn("<mention-page", pointers[0].task_page_url)

    def test_unknown_physical_ancestry_is_stale(self):
        pointer_text = self.format_pointer_text(
            self.DispatchPointer(
                task_id="SF-217",
                execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                expected_lifecycle_state="Queue",
                task_page_url=self.task.task_page_url,
                dispatch_state="EXECUTABLE",
                harness="antigravity",
            )
        )
        self.client.retrieve_block_children.return_value = self._children(pointer_text)
        self.client.retrieve_page.return_value = {
            "id": self.task.task_page_id,
            "parent": {"type": "page_id", "page_id": "00000000-0000-0000-0000-000000000000"},
        }
        result = self.HeadlessDispatchResolver(self.client).resolve("antigravity")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, self.DISPATCH_STALE)
        self.assertIn("UNKNOWN_PHYSICAL_ANCESTRY", result.detail)

    def test_stale_pointer_deletion_failure_does_not_append(self):
        from awe_worker.notion import NotionAPIError
        existing = self._children("- Task ID: `SF-OLD`\n- Dispatch state: `EXECUTABLE`")
        self.client.retrieve_block_children.return_value = existing
        self.client.delete_block.side_effect = NotionAPIError(500, "delete failed")
        maintainer = self.DispatchMaintainer(self.client)
        err = maintainer.write_pointer(
            self.DispatchPointer(
                task_id="SF-217",
                execution_profile="Antigravity -> Gemini 3.8 Flash / High",
                expected_lifecycle_state="Queue",
                task_page_url=self.task.task_page_url,
                dispatch_state="EXECUTABLE",
                harness="antigravity",
            )
        )
        self.assertIn(self.DISPATCH_STALE, err)
        self.client.append_block_children.assert_not_called()


if __name__ == "__main__":
    unittest.main()

