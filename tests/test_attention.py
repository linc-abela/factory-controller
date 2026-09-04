"""Tests for Stage 10 / Operations Plane Owner Attention Delivery & Liveness Path.

Follows the Factory Owner Attention Budget (skills/software-factory-owner-attention).
Verifies:
1. OwnerAttention delivery contract (factory-controller/attention/1.0).
2. Classification distinguishing genuine Owner-only blockers from reversible engineering failures.
3. Zero-cost macOS Notification Center sink via osascript with deterministic testing.
4. Pluggable channel seams (recording, file, composite, external stub).
5. Deduplication and cooldown rate-limiting across supervisor cycles.
6. Changed, resolved, and reopened blocker lifecycle.
7. Supervisor liveness watchdog when service path is stalled or dead.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from factory_controller import attention
from factory_controller.engine import Controller
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.store import MissionStore


class NoopAdapter:
    def execute(self, step, operation_key, value):
        return {"status": "completed", "candidate_sha": "a" * 40}


class AttentionContractTests(unittest.TestCase):
    def test_contract_version_and_event_serialization(self):
        event = attention.AttentionEvent(
            event_id="evt-001",
            fingerprint="fp-001",
            category=attention.CATEGORY_OWNER_ACTION,
            code="AUTOPILOT_ATTENTION",
            headline="FACTORY ATTENTION: DF-1",
            message="The DF-1 validation mission needs Owner review",
            action_required="./dev factory status",
            target_ref="DF-1",
            observed_at=1000.0,
            state="active",
            severity="blocker",
            metadata={"cycle": 42},
        )
        data = event.as_dict()
        self.assertEqual(data["contract_version"], attention.CONTRACT_VERSION)
        self.assertEqual(data["event_id"], "evt-001")
        self.assertEqual(data["fingerprint"], "fp-001")
        self.assertEqual(data["category"], attention.CATEGORY_OWNER_ACTION)
        self.assertEqual(data["code"], "AUTOPILOT_ATTENTION")
        self.assertEqual(data["headline"], "FACTORY ATTENTION: DF-1")
        self.assertEqual(data["state"], "active")
        self.assertEqual(data["severity"], "blocker")
        self.assertEqual(data["metadata"]["cycle"], 42)

    def test_delivery_receipt_serialization(self):
        receipt = attention.DeliveryReceipt(
            event_id="evt-001",
            fingerprint="fp-001",
            channel="macos_notification",
            delivered=True,
            delivery_state=attention.DELIVERY_STATE_INITIAL,
            timestamp=1000.0,
            detail="Displayed banner",
        )
        data = receipt.as_dict()
        self.assertEqual(data["contract_version"], attention.CONTRACT_VERSION)
        self.assertEqual(data["channel"], "macos_notification")
        self.assertTrue(data["delivered"])
        self.assertEqual(data["delivery_state"], attention.DELIVERY_STATE_INITIAL)
        self.assertIsNone(data["error"])

    def test_fingerprint_deterministic_and_bounded(self):
        fp1 = attention.compute_fingerprint("OWNER_ACTION_REQUIRED", "AUTOPILOT_ATTENTION", "DF-1")
        fp2 = attention.compute_fingerprint("OWNER_ACTION_REQUIRED", "AUTOPILOT_ATTENTION", "DF-1")
        fp3 = attention.compute_fingerprint("OWNER_ACTION_REQUIRED", "AUTOPILOT_ATTENTION", "DF-2")
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertEqual(len(fp1), 16)


class AttentionClassificationTests(unittest.TestCase):
    def test_genuine_owner_blockers_escalate(self):
        owner_cases = [
            ("AUTOPILOT_ATTENTION", "The DF-1 mission needs Owner review before continuing"),
            ("SUPERVISOR_FAILURE", "The supervisor stopped advancing"),
            ("SUPERVISOR_ACTIVATION_UNAPPROVED", "Writing host service needs durable Owner approval"),
            ("OWNER_IDENTITY_UNAVAILABLE", "Trusted local identity missing"),
            ("OWNER_VALIDATION_REQUIRED", "Candidate sealed, awaiting Owner validation"),
            ("RELEASE_AUTHORITY_REQUIRED", "Promotion requires human release authority"),
            ("SUPERVISOR_DEAD", "Supervisor has not cycled within tolerance"),
            ("BRIDGE_PROBLEM", "Factory Bridge is unhealthy"),
        ]
        for code, detail in owner_cases:
            is_owner, cat, reason = attention.classify_attention(code, detail)
            self.assertTrue(is_owner, f"Expected {code} to be classified as Owner attention")
            self.assertIn(cat, attention.OWNER_ATTENTION_CATEGORIES)

    def test_normal_reversible_engineering_failures_do_not_escalate(self):
        # 1. Marked retryable by scheduler
        is_owner, cat, reason = attention.classify_attention(
            "PROVIDER_FAILURE", "Temporary connection timeout", retryable=True
        )
        self.assertFalse(is_owner)
        self.assertEqual(cat, attention.CATEGORY_ENGINEERING_REVERSIBLE)

        # 2. Declared non-zero gate expectation (e.g. DF-2 negative test)
        is_owner, cat, reason = attention.classify_attention(
            "ACCEPTANCE_GATE_FAILED", "Test exited non-zero as planned", expected_gate_failure=True
        )
        self.assertFalse(is_owner)
        self.assertEqual(cat, attention.CATEGORY_ENGINEERING_REVERSIBLE)

        # 3. Normal intermediate engineering failures
        is_owner, cat, reason = attention.classify_attention(
            "SYNTAX_ERROR", "SyntaxError in line 42"
        )
        self.assertFalse(is_owner)
        self.assertEqual(cat, attention.CATEGORY_ENGINEERING_REVERSIBLE)


class MacOSNotificationSinkTests(unittest.TestCase):
    def test_script_construction_and_escaping(self):
        sink = attention.MacOSNotificationSink()
        event = attention.AttentionEvent(
            event_id="evt-1",
            fingerprint="fp-1",
            category=attention.CATEGORY_OWNER_ACTION,
            code="AUTOPILOT_ATTENTION",
            headline='Alert: "Critical" Blocker',
            message='Error in file "main.py"\nLine 2',
            action_required='./dev factory "run"',
            target_ref="DF-1",
            observed_at=1000.0,
        )
        script = sink.build_script(event)
        self.assertIn('with title "Alert: \\"Critical\\" Blocker"', script)
        self.assertIn('display notification "Error in file \\"main.py\\" Line 2"', script)
        self.assertIn('subtitle "./dev factory \\"run\\""', script)
        self.assertIn('sound name "default"', script)

    def test_delivery_success_with_mock_runner(self):
        mock_result = Mock()
        mock_result.returncode = 0
        mock_runner = Mock(return_value=mock_result)

        sink = attention.MacOSNotificationSink(runner=mock_runner)
        event = attention.AttentionEvent(
            event_id="evt-1",
            fingerprint="fp-1",
            category=attention.CATEGORY_OWNER_ACTION,
            code="AUTOPILOT_ATTENTION",
            headline="FACTORY ATTENTION",
            message="DF-1 needs attention",
            action_required="./dev factory status",
            target_ref="DF-1",
            observed_at=1000.0,
        )
        receipt = sink.deliver(event)
        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.channel, "macos_notification")
        mock_runner.assert_called_once()
        cmd, kwargs = mock_runner.call_args
        self.assertEqual(cmd[0][0], "osascript")
        self.assertEqual(cmd[0][1], "-e")

    def test_delivery_failure_handles_nonzero_exit(self):
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stderr = "execution error: User canceled"
        mock_runner = Mock(return_value=mock_result)

        sink = attention.MacOSNotificationSink(runner=mock_runner)
        event = attention.AttentionEvent(
            event_id="evt-1",
            fingerprint="fp-1",
            category=attention.CATEGORY_OWNER_ACTION,
            code="AUTOPILOT_ATTENTION",
            headline="FACTORY ATTENTION",
            message="DF-1 needs attention",
            action_required="./dev factory status",
            target_ref="DF-1",
            observed_at=1000.0,
        )
        receipt = sink.deliver(event)
        self.assertFalse(receipt.delivered)
        self.assertEqual(receipt.delivery_state, "failed")
        self.assertIn("osascript returned 1", receipt.error or "")

    def test_delivery_graceful_when_no_runner(self):
        sink = attention.MacOSNotificationSink(runner=None)
        event = attention.AttentionEvent(
            event_id="evt-1",
            fingerprint="fp-1",
            category=attention.CATEGORY_OWNER_ACTION,
            code="AUTOPILOT_ATTENTION",
            headline="FACTORY ATTENTION",
            message="DF-1 needs attention",
            action_required="./dev factory status",
            target_ref="DF-1",
            observed_at=1000.0,
        )
        receipt = sink.deliver(event)
        self.assertFalse(receipt.delivered)
        self.assertEqual(receipt.delivery_state, "skipped_no_runner")


class PluggableSinkSeamTests(unittest.TestCase):
    def test_file_attention_sink_writes_durable_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "attention.json"
            sink = attention.FileAttentionSink(file_path)
            event = attention.AttentionEvent(
                event_id="evt-file-1",
                fingerprint="fp-file-1",
                category=attention.CATEGORY_OWNER_ACTION,
                code="AUTOPILOT_ATTENTION",
                headline="FACTORY ATTENTION",
                message="File test message",
                action_required="./dev factory status",
                target_ref="DF-1",
                observed_at=1000.0,
            )
            receipt = sink.deliver(event)
            self.assertTrue(receipt.delivered)
            self.assertTrue(file_path.exists())

            saved = json.loads(file_path.read_text())
            self.assertEqual(saved["contract_version"], attention.CONTRACT_VERSION)
            self.assertEqual(saved["current_event"]["event_id"], "evt-file-1")
            self.assertEqual(saved["current_event"]["message"], "File test message")

    def test_composite_sink_dispatches_to_all(self):
        recording1 = attention.RecordingAttentionSink(channel_name="rec1")
        recording2 = attention.RecordingAttentionSink(channel_name="rec2")
        composite = attention.CompositeAttentionSink([recording1, recording2])

        event = attention.AttentionEvent(
            event_id="evt-comp-1",
            fingerprint="fp-comp-1",
            category=attention.CATEGORY_SUPERVISOR_FAILURE,
            code="SUPERVISOR_FAILURE",
            headline="FACTORY ATTENTION",
            message="Supervisor stopped",
            action_required="./dev factory status",
            target_ref="supervisor",
            observed_at=1000.0,
        )
        receipt = composite.deliver(event)
        self.assertTrue(receipt.delivered)
        self.assertEqual(len(recording1.events), 1)
        self.assertEqual(len(recording2.events), 1)

    def test_external_channel_stub(self):
        stub = attention.ExternalChannelStub(endpoint_label="email_operator")
        event = attention.AttentionEvent(
            event_id="evt-stub-1",
            fingerprint="fp-stub-1",
            category=attention.CATEGORY_AUTHORITY,
            code="RELEASE_AUTHORITY_REQUIRED",
            headline="FACTORY ATTENTION",
            message="Signoff required",
            action_required="./dev factory review",
            target_ref="release",
            observed_at=1000.0,
        )
        receipt = stub.deliver(event)
        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.delivery_state, "staged_pluggable_seam")
        self.assertIn("email_operator", receipt.detail)


class DeduplicationAndLifecycleTests(unittest.TestCase):
    def test_duplicate_unresolved_events_suppressed_under_cooldown(self):
        sink = attention.RecordingAttentionSink()
        current_time = 1000.0

        router = attention.AttentionRouter(
            sink,
            clock=lambda: current_time,
            cooldown_seconds=3600.0,
        )

        # Cycle 1: initial occurrence -> delivered
        r1 = router.emit("AUTOPILOT_ATTENTION", "DF-1 validation needs attention", target_ref="DF-1")
        self.assertTrue(r1.delivered)
        self.assertEqual(r1.delivery_state, attention.DELIVERY_STATE_INITIAL)
        self.assertEqual(len(sink.events), 1)

        # Cycle 2: 5 minutes later (300s) -> suppressed
        current_time += 300.0
        r2 = router.emit("AUTOPILOT_ATTENTION", "DF-1 validation needs attention", target_ref="DF-1")
        self.assertFalse(r2.delivered)
        self.assertEqual(r2.delivery_state, attention.DELIVERY_STATE_SUPPRESSED)
        self.assertEqual(len(sink.events), 1)

        # Cycle 3: 15 minutes later (900s) -> suppressed
        current_time += 600.0
        r3 = router.emit("AUTOPILOT_ATTENTION", "DF-1 validation needs attention", target_ref="DF-1")
        self.assertFalse(r3.delivered)
        self.assertEqual(r3.delivery_state, attention.DELIVERY_STATE_SUPPRESSED)
        self.assertEqual(len(sink.events), 1)

        # Cycle 4: 65 minutes after first (3900s total, past 3600s cooldown) -> delivered as reminder
        current_time = 1000.0 + 3601.0
        r4 = router.emit("AUTOPILOT_ATTENTION", "DF-1 validation needs attention", target_ref="DF-1")
        self.assertTrue(r4.delivered)
        self.assertEqual(r4.delivery_state, attention.DELIVERY_STATE_REMINDER)
        self.assertEqual(len(sink.events), 2)

    def test_materially_changed_message_delivers_update(self):
        sink = attention.RecordingAttentionSink()
        current_time = 1000.0
        router = attention.AttentionRouter(sink, clock=lambda: current_time, cooldown_seconds=3600.0)

        # First failure
        r1 = router.emit("AUTOPILOT_ATTENTION", "DF-1 failed at stage 1", target_ref="DF-1")
        self.assertTrue(r1.delivered)
        self.assertEqual(r1.delivery_state, attention.DELIVERY_STATE_INITIAL)

        # 5 minutes later, detail changes
        current_time += 300.0
        r2 = router.emit("AUTOPILOT_ATTENTION", "DF-1 failed at stage 3 with new error", target_ref="DF-1")
        self.assertTrue(r2.delivered)
        self.assertEqual(r2.delivery_state, attention.DELIVERY_STATE_UPDATED)
        self.assertEqual(len(sink.events), 2)

    def test_resolved_and_reopened_lifecycle(self):
        sink = attention.RecordingAttentionSink()
        current_time = 1000.0
        router = attention.AttentionRouter(sink, clock=lambda: current_time, cooldown_seconds=3600.0)

        # 1. Blocker appears
        r1 = router.emit("SUPERVISOR_FAILURE", "Supervisor stopped", target_ref="supervisor")
        self.assertTrue(r1.delivered)
        fp = r1.fingerprint

        # 2. Blocker is resolved
        current_time += 600.0
        resolved = router.resolve(fp)
        self.assertTrue(resolved)

        # 3. Blocker reoccurs shortly after (within cooldown) -> MUST deliver immediately as reopened!
        current_time += 120.0
        r2 = router.emit("SUPERVISOR_FAILURE", "Supervisor stopped", target_ref="supervisor")
        self.assertTrue(r2.delivered)
        self.assertEqual(r2.delivery_state, attention.DELIVERY_STATE_REOPENED)
        self.assertEqual(len(sink.events), 2)


class LivenessWatchdogTests(unittest.TestCase):
    def test_supervisor_liveness_healthy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            receipt = Path(tmpdir) / "supervisor-runtime.json"
            receipt.write_text(json.dumps({"timestamp": 1000.0, "status": "running"}))

            healthy, detail, age = attention.check_supervisor_liveness(
                receipt, tolerance_seconds=600.0, clock=lambda: 1200.0  # 200s old < 600s
            )
            self.assertTrue(healthy)
            self.assertEqual(age, 200.0)
            self.assertIn("healthy", detail)

    def test_supervisor_liveness_stalled_beyond_tolerance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            receipt = Path(tmpdir) / "supervisor-runtime.json"
            receipt.write_text(json.dumps({"timestamp": 1000.0, "status": "running"}))

            healthy, detail, age = attention.check_supervisor_liveness(
                receipt, tolerance_seconds=600.0, clock=lambda: 1750.0  # 750s old > 600s
            )
            self.assertFalse(healthy)
            self.assertEqual(age, 750.0)
            self.assertIn("has not cycled in 750s", detail)

    def test_supervisor_liveness_absent_receipt(self):
        missing = Path("/nonexistent/supervisor-runtime.json")
        healthy, detail, age = attention.check_supervisor_liveness(missing)
        self.assertFalse(healthy)
        self.assertIn("absent", detail)

    def test_liveness_probe_delivers_attention_when_stalled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            receipt = Path(tmpdir) / "supervisor-runtime.json"
            receipt.write_text(json.dumps({"timestamp": 1000.0, "status": "running"}))
            current_time = 1800.0

            sink = attention.RecordingAttentionSink()
            router = attention.AttentionRouter(sink, clock=lambda: current_time)

            healthy, detail, _ = attention.check_supervisor_liveness(
                receipt, tolerance_seconds=600.0, clock=lambda: current_time
            )
            self.assertFalse(healthy)

            receipt_result = router.emit(
                code="SUPERVISOR_DEAD",
                detail=detail,
                target_ref="supervisor",
                headline="FACTORY ATTENTION: Supervisor Stalled",
                action_required="./dev factory start",
            )
            self.assertTrue(receipt_result.delivered)
            self.assertEqual(receipt_result.delivery_state, attention.DELIVERY_STATE_INITIAL)
            self.assertEqual(len(sink.events), 1)
            self.assertEqual(sink.events[0].headline, "FACTORY ATTENTION: Supervisor Stalled")


class LifecycleAttentionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.config = dataclasses.replace(
            FactoryConfig.default(self.root),
            state_dir=self.root / ".factory-controller",
        )
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = MissionStore(self.root / "controller.db")
        self.controller = Controller(self.store, NoopAdapter())
        self.sink = attention.RecordingAttentionSink()
        self.now = 1000.0
        self.lifecycle = FactoryLifecycle(
            self.controller,
            config=self.config,
            owner=OwnerIdentity(501, "owner"),
            clock=lambda: self.now,
            attention_sink=self.sink,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_lifecycle_attention_test_action(self):
        result = self.lifecycle.dispatch("attention", attention_action="test")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "delivered")
        self.assertEqual(len(self.sink.events), 1)
        event = self.sink.events[0]
        self.assertEqual(event.code, "AUTOPILOT_ATTENTION")
        self.assertEqual(event.headline, "FACTORY ATTENTION: Test Fixture")
        self.assertEqual(event.action_required, "./dev factory status")
        self.assertEqual(result.details["channel"], "recording")
        self.assertTrue(result.details["delivered"])

    def test_lifecycle_attention_status_and_clear(self):
        # 1. Clean status initially
        res_initial = self.lifecycle.dispatch("attention", attention_action="status")
        self.assertTrue(res_initial.ok)
        self.assertEqual(res_initial.state, "clean")
        self.assertIn("No active Owner attention blockers.", res_initial.render())

        # 2. Emit an attention result
        res_attention = self.lifecycle._attention_result(
            "AUTOPILOT_ATTENTION", "Validation failed for DF-1", mission_ref="DF-1"
        )
        self.assertFalse(res_attention.ok)
        self.assertEqual(res_attention.state, "attention")
        self.assertEqual(len(self.sink.events), 1)

        # 3. Status now reports active blocker
        res_status = self.lifecycle.dispatch("attention", attention_action="status")
        self.assertFalse(res_status.ok)
        self.assertEqual(res_status.state, "attention")
        self.assertIn("Active Owner attention blockers (1):", res_status.render())
        self.assertIn("Validation failed for DF-1", res_status.render())

        # 4. Clear active blockers
        res_clear = self.lifecycle.dispatch("attention", attention_action="clear")
        self.assertTrue(res_clear.ok)
        self.assertEqual(res_clear.state, "cleared")
        self.assertEqual(res_clear.details["cleared"], 1)

        # 5. Status is clean again
        res_clean = self.lifecycle.dispatch("attention", attention_action="status")
        self.assertTrue(res_clean.ok)
        self.assertEqual(res_clean.state, "clean")

    def test_lifecycle_liveness_probe_healthy_and_stalled(self):
        # 1. Absent receipt -> stalled -> emits attention
        res_stalled = self.lifecycle.dispatch("attention", attention_action="check-liveness")
        self.assertFalse(res_stalled.ok)
        self.assertEqual(res_stalled.state, "attention")
        self.assertEqual(res_stalled.details["code"], "SUPERVISOR_DEAD")
        self.assertEqual(len(self.sink.events), 1)
        self.assertEqual(self.sink.events[0].headline, "FACTORY ATTENTION: Supervisor Stalled")

        # 2. Write fresh receipt -> healthy
        self.config.runtime_receipt_path.write_text(
            json.dumps({"timestamp": self.now - 30.0, "status": "running"})
        )
        res_healthy = self.lifecycle.dispatch("attention", attention_action="check-liveness")
        self.assertTrue(res_healthy.ok)
        self.assertEqual(res_healthy.state, "healthy")
        self.assertEqual(len(self.sink.events), 1)  # No new event emitted

    def test_lifecycle_attention_deduplication_under_cycle(self):
        # Emit first attention event
        r1 = self.lifecycle._attention_result(
            "AUTOPILOT_ATTENTION", "Mission DF-1 failed", mission_ref="DF-1"
        )
        self.assertFalse(r1.ok)
        self.assertEqual(len(self.sink.events), 1)

        # Same event immediately in next cycle -> suppressed by router
        self.now += 60.0
        r2 = self.lifecycle._attention_result(
            "AUTOPILOT_ATTENTION", "Mission DF-1 failed", mission_ref="DF-1"
        )
        self.assertFalse(r2.ok)
        self.assertEqual(len(self.sink.events), 1)  # Suppressed!

        # Advance past cooldown (3600s) -> reminder emitted
        self.now += 3601.0
        r3 = self.lifecycle._attention_result(
            "AUTOPILOT_ATTENTION", "Mission DF-1 failed", mission_ref="DF-1"
        )
        self.assertFalse(r3.ok)
        self.assertEqual(len(self.sink.events), 2)  # Reminder delivered!


if __name__ == "__main__":
    unittest.main()
