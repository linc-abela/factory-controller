"""Comprehensive fault-injection test matrix for Controller v1."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from factory_controller.engine import (
    Controller,
    NonRetryableFailure,
    RetryPolicy,
    RetryableFailure,
)
from factory_controller.store import ConflictError, MissionStore


class FaultHarnessAdapter:
    def __init__(self) -> None:
        self.faults: dict[str, str] = {}
        self.call_history: list[str] = []

    def set_fault(self, step: str, fault_type: str) -> None:
        self.faults[step] = fault_type

    def clear_faults(self) -> None:
        self.faults.clear()

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        self.call_history.append(step)
        fault = self.faults.get(step)

        if fault == "bridge_unavailable":
            return {"status": "retryable_error", "diagnostic": "BRIDGE_UNAVAILABLE"}
        elif fault == "provider_timeout":
            return {"status": "retryable_error", "diagnostic": "PROVIDER_TIMEOUT"}
        elif fault == "no_candidate":
            return {"status": "no_candidate", "diagnostic": "DISPATCH_REFUSED"}
        elif fault == "invalid_candidate":
            return {"verified": False, "diagnostic": "CANDIDATE_OUTSIDE_BASELINE_HISTORY"}
        elif fault == "evaluator_failure":
            return {"verified": False, "diagnostic": "EVALUATOR_ASSERTION_FAILED"}
        elif fault == "evidence_rejection":
            return {"accepted": False, "retryable": False, "diagnostic": "EVIDENCE_ROOT_REJECTED"}

        if step == "dispatch":
            return {"status": "completed", "candidate_sha": f"cand_{operation_key[:8]}"}
        elif step == "verify":
            return {"verified": True, "evaluator": "local-safe-evaluator"}
        elif step == "evaluate":
            return {"passed": True, "gate_outcomes": [{"gate_id": "TEST-GATE", "passed": True}]}
        elif step == "evidence":
            return {"accepted": True, "evidence_pointer": f"evidence://{operation_key[:8]}"}
        return {"status": "unknown"}


class ControllerFaultMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "faults.db"
        self.store = MissionStore(self.db_path)
        self.adapter = FaultHarnessAdapter()
        self.controller = Controller(
            self.store,
            self.adapter,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.001),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # Fault 1: Stale lease recovery
    def test_fault_01_stale_lease_recovered_and_completed_by_next_worker(self) -> None:
        self.controller.submit({"item": "stale"}, "key-stale")
        # Worker 1 claims but disappears (simulating killed process)
        m1 = self.store.claim("dead-worker", lease_seconds=0.02)
        self.assertEqual(m1["state"], "dispatching")

        time.sleep(0.04)  # Lease expires
        # Worker 2 calls work_once -> automatically recovers stale lease and completes
        res = self.controller.work_once("live-worker")
        self.assertIsNotNone(res)
        self.assertEqual(res["state"], "completed")
        self.assertEqual(res["attempt_count"], 2)

    # Fault 2: Duplicate worker claim race (bounded single-claim concurrency)
    def test_fault_02_duplicate_worker_concurrency_race(self) -> None:
        self.controller.submit({"item": "race"}, "key-race")
        # Worker A claims the mission
        claimed_a = self.store.claim("worker-A", lease_seconds=30)
        self.assertIsNotNone(claimed_a)
        # Concurrent Worker B attempts to claim the same mission
        claimed_b = self.store.claim("worker-B", lease_seconds=30)
        self.assertIsNone(claimed_b)

    # Fault 3: Duplicate submission (idempotent replay)
    def test_fault_03_duplicate_submission_returns_identical_record(self) -> None:
        payload = {"item": "idem", "args": [1, 2, 3]}
        m1, created1 = self.controller.submit(payload, "key-idem")
        self.assertTrue(created1)
        m2, created2 = self.controller.submit(payload, "key-idem")
        self.assertFalse(created2)
        self.assertEqual(m1["id"], m2["id"])

    # Fault 4: Replay conflict (same idempotency key with tampered payload)
    def test_fault_04_replay_conflict_refuses_before_execution(self) -> None:
        self.controller.submit({"item": "original"}, "key-conflict")
        with self.assertRaises(ConflictError):
            self.controller.submit({"item": "tampered"}, "key-conflict")

    # Fault 5: Bridge unavailable (retries with backoff)
    def test_fault_05_bridge_unavailable_retries(self) -> None:
        self.controller.submit({"item": "bridge-fail"}, "key-bfail")
        self.adapter.set_fault("dispatch", "bridge_unavailable")

        res1 = self.controller.work_once("w1")
        self.assertEqual(res1["state"], "admitted")
        self.assertEqual(res1["attempt_count"], 1)

        # Bridge becomes available
        time.sleep(0.08)
        self.adapter.clear_faults()
        res2 = self.controller.work_once("w2")
        self.assertEqual(res2["state"], "completed")
        self.assertEqual(res2["attempt_count"], 2)

    # Fault 6: Provider timeout / failure
    def test_fault_06_provider_timeout_retries_and_recovers(self) -> None:
        self.controller.submit({"item": "timeout"}, "key-timeout")
        self.adapter.set_fault("dispatch", "provider_timeout")

        res1 = self.controller.work_once("w1")
        self.assertEqual(res1["state"], "admitted")

        time.sleep(0.08)
        self.adapter.clear_faults()
        res2 = self.controller.work_once("w2")
        self.assertEqual(res2["state"], "completed")

    # Fault 7: No candidate produced
    def test_fault_07_no_candidate_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "no-cand"}, "key-nocand")
        self.adapter.set_fault("dispatch", "no_candidate")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "refused")
        self.assertEqual(res["terminal_reason"], "DISPATCH_REFUSED")

    # Fault 8: Invalid candidate SHA
    def test_fault_08_invalid_candidate_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "inv-cand"}, "key-invcand")
        self.adapter.set_fault("verify", "invalid_candidate")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["terminal_reason"], "CANDIDATE_OUTSIDE_BASELINE_HISTORY")

    # Fault 9: Evaluator failure
    def test_fault_09_evaluator_failure_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "eval-fail"}, "key-evalfail")
        self.adapter.set_fault("verify", "evaluator_failure")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["terminal_reason"], "EVALUATOR_ASSERTION_FAILED")

    # Fault 10: Evidence Core rejection
    def test_fault_10_evidence_rejection_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "ev-reject"}, "key-evreject")
        self.adapter.set_fault("evidence", "evidence_rejection")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["terminal_reason"], "EVIDENCE_ROOT_REJECTED")

    # Fault 11: Database reopen / crash across separate process connections
    def test_fault_11_database_reopen_preserves_full_state_and_history(self) -> None:
        m, _ = self.controller.submit({"item": "reopen"}, "key-reopen")
        self.controller.work_once("w1")

        # Completely close and reopen store from disk
        new_store = MissionStore(self.db_path)
        reopened = new_store.get(m["id"])
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["state"], "completed")
        self.assertEqual(reopened["result"]["verification"]["verified"], True)
        history = new_store.history(m["id"])
        self.assertTrue(len(history) >= 4)

    # Fault 12: Incomplete attempt resumption (killed between steps)
    def test_fault_12_incomplete_attempt_resumes_without_duplicate_dispatch(self) -> None:
        m, _ = self.controller.submit({"item": "step-crash"}, "key-sc1")

        # Worker 1 completes dispatch and then crashes/dies holding lease
        claimed = self.store.claim("w1", lease_seconds=0.02)
        token = claimed["lease_token"]
        self.store.transition(m["id"], token, "dispatched")
        self.store.begin_step(m["id"], token, "dispatch", {"mission": claimed["payload"]})
        self.store.complete_step(m["id"], token, "dispatch", {"status": "completed", "candidate_sha": "cand_memo"})

        time.sleep(0.04)  # Lease expires
        self.adapter.call_history.clear()

        # Worker 2 picks it up and runs to completion
        res = self.controller.work_once("w2")
        self.assertIsNotNone(res)
        self.assertEqual(res["state"], "completed")
        # verify, evaluate, and evidence executed, but dispatch was NOT repeated
        self.assertEqual(self.adapter.call_history, ["verify", "evaluate", "evidence"])

    # Fault 13: Cancellation of queued mission
    def test_fault_13_cancellation_of_queued_mission(self) -> None:
        m, _ = self.controller.submit({"item": "cancel-q"}, "key-cq")
        self.store.cancel(m["id"])
        self.assertEqual(self.store.get(m["id"])["state"], "cancelled")
        # Cannot be claimed
        self.assertIsNone(self.controller.work_once("w1"))

    # Fault 14: Cancellation of running mission
    def test_fault_14_cancellation_of_running_mission(self) -> None:
        m, _ = self.controller.submit({"item": "cancel-r"}, "key-cr")
        claimed = self.store.claim("w1", lease_seconds=0.02)
        self.assertIsNotNone(claimed)
        # Cancel requested while in flight
        state = self.store.cancel(m["id"])
        self.assertEqual(state, "dispatching")
        self.assertTrue(self.store.get(m["id"])["cancel_requested"])

        # When lease expires and recover_stale runs, cancelled state is recognized
        time.sleep(0.08)
        self.store.recover_stale()
        self.assertEqual(self.store.get(m["id"])["state"], "cancelled")

    # Fault 15: Retry exhaustion -> transitions to escalated
    def test_fault_15_retry_exhaustion_transitions_to_blocked(self) -> None:
        self.controller.submit({"item": "exhaust"}, "key-ex")
        self.adapter.set_fault("dispatch", "bridge_unavailable")

        # Attempt 1
        self.controller.work_once("w1")
        # Attempt 2
        time.sleep(0.02)
        self.controller.work_once("w2")
        # Attempt 3 (exhausted)
        time.sleep(0.02)
        res = self.controller.work_once("w3")
        self.assertEqual(res["state"], "escalated")
        self.assertTrue("RETRIES_EXHAUSTED" in res["terminal_reason"])

    # Fault 16: a long provider call is protected by heartbeat renewal
    def test_fault_16_provider_call_renews_lease_until_completion(self) -> None:
        class SlowAdapter(FaultHarnessAdapter):
            def execute(inner_self, step, operation_key, value):
                if step == "dispatch":
                    time.sleep(0.12)
                return super(SlowAdapter, inner_self).execute(step, operation_key, value)

        controller = Controller(
            self.store,
            SlowAdapter(),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            lease_seconds=0.05,
        )
        controller.submit({"item": "slow"}, "key-slow")
        result_box = []
        worker = threading.Thread(target=lambda: result_box.append(controller.work_once("slow-worker")))
        worker.start()
        time.sleep(0.08)
        self.assertEqual(self.store.recover_stale(), 0)
        self.assertIsNone(self.store.claim("duplicate-worker"))
        worker.join()
        self.assertEqual(result_box[0]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
