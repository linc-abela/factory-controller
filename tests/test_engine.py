"""Unit tests for Controller engine execution, step resume, and retry logic."""

from __future__ import annotations

import tempfile
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
from factory_controller.store import MissionStore


class MockAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_step: str | None = None
        self.fail_mode: str | None = None

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((step, operation_key, value))
        if step == self.fail_step:
            if self.fail_mode == "retryable":
                return {"status": "retryable_error", "diagnostic": "network glitch"}
            elif self.fail_mode == "fatal":
                return {"status": "fatal_error", "diagnostic": "unrecoverable corruption"}
            elif self.fail_mode == "raise_retryable":
                raise RetryableFailure("adapter crashed retryably")
            elif self.fail_mode == "raise_fatal":
                raise NonRetryableFailure("adapter crashed fatally")
            elif self.fail_mode == "unverified":
                return {"verified": False, "diagnostic": "ancestry check failed"}
            elif self.fail_mode == "evidence_rejected":
                return {"accepted": False, "retryable": False, "diagnostic": "sha mismatch"}
            elif self.fail_mode == "evidence_retryable":
                return {"accepted": False, "retryable": True, "diagnostic": "lock busy"}

        if step == "dispatch":
            return {"status": "completed", "candidate_sha": "abc1234567890abcdef1234567890abcdef12345"}
        elif step == "verify":
            return {"verified": True, "evaluator": "test-eval"}
        elif step == "evidence":
            return {"accepted": True, "evidence_pointer": "evidence://bundle-1"}
        return {"status": "unknown"}


class ControllerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "engine.db"
        self.store = MissionStore(self.db_path)
        self.adapter = MockAdapter()
        self.controller = Controller(self.store, self.adapter, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_golden_path_execution(self) -> None:
        mission, created = self.controller.submit({"work_item_id": "M-1"}, "key-m1")
        self.assertTrue(created)
        self.assertEqual(mission["state"], "READY")

        result = self.controller.work_once("worker-1")
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(len(self.adapter.calls), 3)
        self.assertEqual([c[0] for c in self.adapter.calls], ["dispatch", "verify", "evidence"])
        self.assertEqual(result["result"]["dispatch"]["status"], "completed")
        self.assertEqual(result["result"]["verification"]["verified"], True)
        self.assertEqual(result["result"]["evidence"]["accepted"], True)

    def test_step_memoization_on_worker_crash_and_restart(self) -> None:
        # Submit mission
        mission, _ = self.controller.submit({"work_item_id": "M-CRASH"}, "key-crash")

        # First run completes dispatch, then simulate crash during verify step
        self.adapter.fail_step = "verify"
        self.adapter.fail_mode = "raise_retryable"

        res1 = self.controller.work_once("worker-1")
        self.assertIsNotNone(res1)
        self.assertEqual(res1["state"], "READY")
        self.assertEqual([c[0] for c in self.adapter.calls], ["dispatch", "verify"])

        # Wait for retry delay to elapse
        time.sleep(0.05)

        # Clear failure mode so second run can succeed
        self.adapter.fail_step = None
        self.adapter.fail_mode = None
        self.adapter.calls.clear()

        # Next run picks up the mission: dispatch MUST NOT be re-executed
        res2 = self.controller.work_once("worker-2")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["state"], "DONE")
        self.assertEqual([c[0] for c in self.adapter.calls], ["verify", "evidence"])

    def test_step_memoization_when_crash_occurs_after_verify(self) -> None:
        mission, _ = self.controller.submit({"work_item_id": "M-CRASH-2"}, "key-crash-2")

        # Simulate crash during evidence step
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "raise_retryable"

        res1 = self.controller.work_once("worker-1")
        self.assertEqual(res1["state"], "READY")
        self.assertEqual([c[0] for c in self.adapter.calls], ["dispatch", "verify", "evidence"])

        time.sleep(0.05)

        self.adapter.fail_step = None
        self.adapter.fail_mode = None
        self.adapter.calls.clear()

        # Second run picks up the mission: neither dispatch NOR verify should re-execute
        res2 = self.controller.work_once("worker-2")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["state"], "DONE")
        self.assertEqual([c[0] for c in self.adapter.calls], ["evidence"])

    def test_non_retryable_dispatch_failure(self) -> None:
        self.adapter.fail_step = "dispatch"
        self.adapter.fail_mode = "fatal"
        self.controller.submit({"work_item_id": "M-FATAL"}, "key-fatal")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "unrecoverable corruption")

    def test_verification_failure_fails_non_retryably(self) -> None:
        self.adapter.fail_step = "verify"
        self.adapter.fail_mode = "unverified"
        self.controller.submit({"work_item_id": "M-UNVERIFIED"}, "key-unverified")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "ancestry check failed")

    def test_evidence_rejection_fails_non_retryably(self) -> None:
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "evidence_rejected"
        self.controller.submit({"work_item_id": "M-REJECT"}, "key-reject")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "FAILED")
        self.assertEqual(res["terminal_reason"], "sha mismatch")

    def test_evidence_retryable_failure_retries_and_blocks_on_exhaustion(self) -> None:
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "evidence_retryable"
        self.controller.submit({"work_item_id": "M-RETRY"}, "key-retry")

        # Attempt 1
        res1 = self.controller.work_once("w1")
        self.assertEqual(res1["state"], "READY")

        # Attempt 2
        time.sleep(0.05)
        res2 = self.controller.work_once("w2")
        self.assertEqual(res2["state"], "READY")

        # Attempt 3 (max_attempts = 3)
        time.sleep(0.05)
        res3 = self.controller.work_once("w3")
        self.assertEqual(res3["state"], "BLOCKED")
        self.assertTrue("RETRIES_EXHAUSTED" in res3["terminal_reason"])


if __name__ == "__main__":
    unittest.main()
