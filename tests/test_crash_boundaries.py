from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore


class ProcessDeath(BaseException):
    pass


class CrashAdapter:
    def __init__(self, crash_step: str, *, before_side_effect: bool = False) -> None:
        self.crash_step = crash_step
        self.before_side_effect = before_side_effect
        self.crashed = False
        self.outputs = {}
        self.side_effects = []

    def execute(self, step, operation_key, value):
        if step == self.crash_step and self.before_side_effect and not self.crashed:
            self.crashed = True
            raise ProcessDeath(step)
        if operation_key not in self.outputs:
            self.side_effects.append(operation_key)
            self.outputs[operation_key] = {
                "dispatch": {"status": "completed", "candidate_sha": "a" * 40, "execution_id": operation_key},
                "verify": {"verified": True},
                "evaluate": {"passed": True, "gate_outcomes": [{"gate_id": "G", "passed": True}]},
                "evidence": {"accepted": True, "evidence_pointer": "b" * 64},
            }[step]
        if step == self.crash_step and not self.crashed:
            self.crashed = True
            raise ProcessDeath(step)
        return self.outputs[operation_key]


class CrashBoundaryTests(unittest.TestCase):
    def run_crash(self, crash_step, *, before=False):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "controller.db"
            adapter = CrashAdapter(crash_step, before_side_effect=before)
            controller = Controller(MissionStore(path), adapter, retry_policy=RetryPolicy(base_delay_seconds=0), lease_seconds=0.02)
            mission, _ = controller.submit({"work_item_id": crash_step}, f"crash:{crash_step}:{before}")
            with self.assertRaises(ProcessDeath):
                controller.work_once("killed")
            time.sleep(0.05)
            resumed = Controller(MissionStore(path), adapter, retry_policy=RetryPolicy(base_delay_seconds=0), lease_seconds=1)
            result = resumed.work_once("replacement")
            self.assertEqual(result["state"], "completed")
            self.assertEqual(len(adapter.side_effects), 4)
            return resumed.store.history(mission["id"])

    def test_crash_before_dispatch(self):
        self.run_crash("dispatch", before=True)

    def test_crash_during_provider_execution(self):
        self.run_crash("dispatch")

    def test_crash_after_candidate_creation(self):
        history = self.run_crash("verify", before=True)
        self.assertIn("dispatched", [event["to_state"] for event in history])

    def test_crash_during_evidence_binding(self):
        self.run_crash("evidence")

    def test_crash_after_evidence_commit_before_state_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "controller.db"
            adapter = CrashAdapter("never")
            store = MissionStore(path)
            controller = Controller(store, adapter, retry_policy=RetryPolicy(base_delay_seconds=0), lease_seconds=0.02)
            mission, _ = controller.submit({"work_item_id": "after-evidence"}, "crash:after-evidence")
            original = store.transition
            crashed = False

            def transition(mission_id, token, state, **kwargs):
                nonlocal crashed
                if state == "evidence_sealed" and not crashed:
                    crashed = True
                    raise ProcessDeath(state)
                return original(mission_id, token, state, **kwargs)

            store.transition = transition
            with self.assertRaises(ProcessDeath):
                controller.work_once("killed")
            self.assertEqual(store.get(mission["id"])["state"], "evaluated")
            time.sleep(0.05)
            resumed = Controller(MissionStore(path), adapter, retry_policy=RetryPolicy(base_delay_seconds=0), lease_seconds=1)
            result = resumed.work_once("replacement")
            self.assertEqual(result["state"], "completed")
            self.assertEqual(len([key for key in adapter.side_effects if key.endswith(":evidence")]), 1)


if __name__ == "__main__":
    unittest.main()
