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
        elif step == "evaluate":
            return {"passed": True, "gate_outcomes": [{"gate_id": "TEST", "passed": True}]}
        elif step == "evidence":
            return {"accepted": True, "evidence_pointer": "evidence://bundle-1"}
        return {"status": "unknown"}


class ControllerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "engine.db"
        self.store = MissionStore(self.db_path)
        self.adapter = MockAdapter()
        self.controller = Controller(self.store, self.adapter, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.001))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_golden_path_execution(self) -> None:
        mission, created = self.controller.submit({"work_item_id": "M-1"}, "key-m1")
        self.assertTrue(created)
        self.assertEqual(mission["state"], "admitted")

        result = self.controller.work_once("worker-1")
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(self.adapter.calls), 4)
        self.assertEqual([c[0] for c in self.adapter.calls], ["dispatch", "verify", "evaluate", "evidence"])
        self.assertEqual(result["result"]["dispatch"]["status"], "completed")
        self.assertEqual(result["result"]["verification"]["verified"], True)
        self.assertEqual(result["result"]["evidence"]["accepted"], True)

    def test_step_memoization_on_worker_crash_and_restart(self) -> None:
        # Submit mission
        mission, _ = self.controller.submit({"work_item_id": "M-CRASH"}, "key-crash")

        # Worker 1 claims and executes dispatch, then process is killed
        claimed = self.store.claim("worker-1", lease_seconds=0.02)
        token = claimed["lease_token"]
        self.store.transition(claimed["id"], token, "dispatched")
        self.store.begin_step(claimed["id"], token, "dispatch", {"mission": claimed["payload"]})
        self.store.complete_step(claimed["id"], token, "dispatch", {"status": "completed", "candidate_sha": "sha-dispatch-1"})

        # Worker 1 dies; wait for lease expiry
        time.sleep(0.04)

        # Worker 2 calls work_once: stale lease recovered to admitted, claimed, and skips dispatch!
        self.adapter.calls.clear()
        res2 = self.controller.work_once("worker-2")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["state"], "completed")
        self.assertEqual([c[0] for c in self.adapter.calls], ["verify", "evaluate", "evidence"])

    def test_step_memoization_when_crash_occurs_after_verify(self) -> None:
        mission, _ = self.controller.submit({"work_item_id": "M-CRASH-2"}, "key-crash-2")

        # Simulate crash during evidence step -> retry after side effect escalates
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "raise_retryable"

        res1 = self.controller.work_once("worker-1")
        self.assertEqual(res1["state"], "escalated")
        self.assertEqual([c[0] for c in self.adapter.calls], ["dispatch", "verify", "evaluate", "evidence"])

        time.sleep(0.05)

        self.adapter.fail_step = None
        self.adapter.fail_mode = None
        self.adapter.calls.clear()

        # Second run picks up the mission: escalated mission cannot be re-run
        res2 = self.controller.work_once("worker-2")
        self.assertIsNone(res2)
        self.assertEqual(self.adapter.calls, [])

    def test_non_retryable_dispatch_failure(self) -> None:
        self.adapter.fail_step = "dispatch"
        self.adapter.fail_mode = "fatal"
        self.controller.submit({"work_item_id": "M-FATAL"}, "key-fatal")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "refused")
        self.assertEqual(res["terminal_reason"], "unrecoverable corruption")

    def test_verification_failure_fails_non_retryably(self) -> None:
        self.adapter.fail_step = "verify"
        self.adapter.fail_mode = "unverified"
        self.controller.submit({"work_item_id": "M-UNVERIFIED"}, "key-unverified")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["terminal_reason"], "ancestry check failed")

    def test_evidence_rejection_fails_non_retryably(self) -> None:
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "evidence_rejected"
        self.controller.submit({"work_item_id": "M-REJECT"}, "key-reject")

        res = self.controller.work_once("worker-1")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["terminal_reason"], "sha mismatch")

    def test_evidence_retryable_failure_retries_and_blocks_on_exhaustion(self) -> None:
        self.adapter.fail_step = "evidence"
        self.adapter.fail_mode = "evidence_retryable"
        mission, _ = self.controller.submit({"work_item_id": "M-RETRY"}, "key-retry")

        res1 = self.controller.work_once("w1")
        self.assertEqual(res1["state"], "escalated")
        self.assertIn("RETRY_AFTER_SIDE_EFFECT", self.store.get(mission["id"])["terminal_reason"])


if __name__ == "__main__":
    unittest.main()
