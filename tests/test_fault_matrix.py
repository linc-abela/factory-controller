"""Comprehensive fault-injection suite for Controller v1."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from factory_controller.adapter import JsonProcessAdapter
from factory_controller.engine import (
    Controller,
    NonRetryableFailure,
    RetryPolicy,
    RetryableFailure,
)
from factory_controller.store import (
    ConflictError,
    LeaseLostError,
    MissionStore,
)


class FaultHarnessAdapter:
    def __init__(self) -> None:
        self.faults: dict[str, str] = {}
        self.call_history: list[str] = []

    def set_fault(self, step: str, fault: str) -> None:
        self.faults[step] = fault

    def clear_faults(self) -> None:
        self.faults.clear()

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        self.call_history.append(step)
        fault = self.faults.get(step)

        if fault == "bridge_unavailable":
            raise RetryableFailure("ADAPTER_UNAVAILABLE: Connection refused")
        elif fault == "provider_timeout":
            raise RetryableFailure("ADAPTER_TIMEOUT: Provider execution timed out after 300s")
        elif fault == "provider_nonzero_exit":
            raise RetryableFailure("ADAPTER_EXIT_1: Fatal error in provider process")
        elif fault == "no_candidate":
            return {"status": "completed", "candidate_sha": None}
        elif fault == "invalid_candidate":
            return {"verified": False, "diagnostic": "CANDIDATE_OUTSIDE_BASELINE_HISTORY"}
        elif fault == "evaluator_failure":
            return {"verified": False, "diagnostic": "EVALUATOR_ASSERTION_FAILED"}
        elif fault == "evidence_rejection":
            return {"accepted": False, "retryable": False, "diagnostic": "EVIDENCE_ROOT_REJECTED"}

        # Default green step responses
        if step == "dispatch":
            return {"status": "completed", "candidate_sha": "d00d1234567890abcdef1234567890abcdef1234"}
        elif step == "verify":
            return {"verified": True, "evaluator": "standard-test"}
        elif step == "evidence":
            return {"accepted": True, "evidence_pointer": "evidence://bundle-test"}
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
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01),
            lease_seconds=0.05,  # Short lease for timeout tests
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # Fault 1: Stale lease recovery
    def test_fault_01_stale_lease_recovered_and_completed_by_next_worker(self) -> None:
        self.controller.submit({"item": "stale"}, "key-stale")
        # Worker 1 claims but disappears (simulating killed process)
        m1 = self.store.claim("dead-worker", lease_seconds=0.02)
        self.assertEqual(m1["state"], "CLAIMED")

        time.sleep(0.04)  # Lease expires
        # Worker 2 calls work_once -> automatically recovers stale lease and completes
        res = self.controller.work_once("live-worker")
        self.assertIsNotNone(res)
        self.assertEqual(res["state"], "DONE")
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
        self.assertEqual(res1["state"], "READY")
        self.assertEqual(res1["attempt_count"], 1)

        # Bridge becomes available
        time.sleep(0.03)
        self.adapter.clear_faults()
        res2 = self.controller.work_once("w2")
        self.assertEqual(res2["state"], "DONE")
        self.assertEqual(res2["attempt_count"], 2)

    # Fault 6: Provider timeout / failure
    def test_fault_06_provider_timeout_retries_and_recovers(self) -> None:
        self.controller.submit({"item": "timeout"}, "key-timeout")
        self.adapter.set_fault("dispatch", "provider_timeout")

        res1 = self.controller.work_once("w1")
        self.assertEqual(res1["state"], "READY")

        time.sleep(0.03)
        self.adapter.clear_faults()
        res2 = self.controller.work_once("w2")
        self.assertEqual(res2["state"], "DONE")

    # Fault 7: No candidate produced
    def test_fault_07_no_candidate_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "no-cand"}, "key-nocand")
        self.adapter.set_fault("dispatch", "no_candidate")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "DISPATCH_REFUSED")

    # Fault 8: Invalid candidate SHA
    def test_fault_08_invalid_candidate_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "inv-cand"}, "key-invcand")
        self.adapter.set_fault("verify", "invalid_candidate")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "CANDIDATE_OUTSIDE_BASELINE_HISTORY")

    # Fault 9: Evaluator failure
    def test_fault_09_evaluator_failure_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "eval-fail"}, "key-evalfail")
        self.adapter.set_fault("verify", "evaluator_failure")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "EVALUATOR_ASSERTION_FAILED")

    # Fault 10: Evidence Core rejection
    def test_fault_10_evidence_rejection_fails_non_retryably(self) -> None:
        self.controller.submit({"item": "ev-reject"}, "key-evreject")
        self.adapter.set_fault("evidence", "evidence_rejection")

        res = self.controller.work_once("w1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "EVIDENCE_ROOT_REJECTED")

    # Fault 11: Database reopen / crash across separate process connections
    def test_fault_11_database_reopen_preserves_full_state_and_history(self) -> None:
        m, _ = self.controller.submit({"item": "reopen"}, "key-reopen")
        self.controller.work_once("w1")

        # Completely close and reopen store from disk
        new_store = MissionStore(self.db_path)
        reopened = new_store.get(m["id"])
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["state"], "DONE")
        self.assertEqual(reopened["result"]["verification"]["verified"], True)
        history = new_store.history(m["id"])
        self.assertTrue(len(history) >= 4)

    # Fault 12: Incomplete attempt resumption (killed between steps)
    def test_fault_12_incomplete_attempt_resumes_without_duplicate_dispatch(self) -> None:
        m, _ = self.controller.submit({"item": "step-crash"}, "key-sc1")
        self.adapter.set_fault("verify", "bridge_unavailable")  # Fail during verify

        self.controller.work_once("w1")
        self.adapter.call_history.clear()
        self.adapter.clear_faults()

        time.sleep(0.03)
        res = self.controller.work_once("w2")
        self.assertEqual(res["state"], "DONE")
        # verify and evidence executed, but dispatch was NOT repeated
        self.assertEqual(self.adapter.call_history, ["verify", "evidence"])

    # Fault 13: Cancellation of queued mission
    def test_fault_13_cancellation_of_queued_mission(self) -> None:
        m, _ = self.controller.submit({"item": "cancel-q"}, "key-cq")
        self.store.cancel(m["id"])
        self.assertEqual(self.store.get(m["id"])["state"], "CANCELLED")
        # Cannot be claimed
        self.assertIsNone(self.controller.work_once("w1"))

    # Fault 14: Cancellation of running mission
    def test_fault_14_cancellation_of_running_mission(self) -> None:
        m, _ = self.controller.submit({"item": "cancel-r"}, "key-cr")
        claimed = self.store.claim("w1", lease_seconds=0.02)
        self.assertIsNotNone(claimed)
        # Cancel requested while in flight
        self.store.cancel(m["id"])
        self.assertTrue(self.store.get(m["id"])["cancel_requested"])

        # When lease expires and recover_stale runs, cancelled state is recognized
        time.sleep(0.04)
        self.store.recover_stale()
        self.assertEqual(self.store.get(m["id"])["state"], "CANCELLED")

    # Fault 15: Retry exhaustion -> transitions to BLOCKED
    def test_fault_15_retry_exhaustion_transitions_to_blocked(self) -> None:
        self.controller.submit({"item": "exhaust"}, "key-ex")
        self.adapter.set_fault("dispatch", "bridge_unavailable")

        # Attempt 1
        self.controller.work_once("w1")
        # Attempt 2
        time.sleep(0.03)
        self.controller.work_once("w2")
        # Attempt 3 (exhausted)
        time.sleep(0.03)
        res = self.controller.work_once("w3")
        self.assertEqual(res["state"], "BLOCKED")
        self.assertTrue("RETRIES_EXHAUSTED" in res["terminal_reason"])


if __name__ == "__main__":
    unittest.main()
