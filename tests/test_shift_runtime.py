"""SF-144A crash-safe shift checkpoint and bounded recovery tests."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from factory_controller import capacity, continuity, portfolio, shift_runtime
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, BETA, Clock, LayerAdapter, ProcessDeath


class ShiftRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.path = Path(self.tmp.name) / "controller.db"
        self.store = MissionStore(self.path, clock=self.clock)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id="project-a",
            repository="repo://project-a",
            acceptance_gate_ids=("G",),
            acceptance_gate_source="tests/test_shift_runtime.py",
        ))
        self.adapter = LayerAdapter()
        self.controller = Controller(
            self.store, self.adapter,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            lease_seconds=1,
        )

    def payload(self, key: str, **extra):
        value = {
            "work_item_id": key,
            "project_id": "project-a",
            "repository_remote_url": "repo://project-a",
            "baseline_sha": "b" * 40,
            "execution_mode": "fixture",
            "acceptance_gate_ids": ["G"],
            "provider_candidates": [
                {"profile": ALPHA, "capabilities": ["implement"]},
                {"profile": BETA, "capabilities": ["implement"]},
            ],
        }
        value.update(extra)
        return value

    def submit(self, key: str, **extra) -> str:
        mission, _ = self.controller.submit(self.payload(key, **extra), key)
        return mission["id"]

    def runtime(self, controller=None) -> shift_runtime.ShiftRuntime:
        return shift_runtime.ShiftRuntime(controller or self.controller)

    def test_checkpoint_is_deterministic_and_resume_package_is_selective(self):
        mission_id = self.submit("checkpoint")
        plane = self.runtime()

        first = plane.checkpoint(mission_id)
        second = plane.checkpoint(mission_id)
        self.assertEqual(first, second)
        self.assertEqual(first["recovery_class"], "pending")
        self.assertEqual(first["next_safe_step"], "dispatch")
        self.assertEqual(first["repository_pin"]["baseline_sha"], "b" * 40)
        self.assertEqual(first["runtime"]["compatible_profiles"], [ALPHA, BETA])
        self.assertEqual(first["context"]["state"], "not_applicable")
        self.assertEqual(
            shift_runtime.validate_checkpoint(first)["checkpoint_id"],
            first["checkpoint_id"],
        )

        package = plane.resume_package(first, target_profile=ALPHA)
        self.assertEqual(package["mission_id"], mission_id)
        self.assertNotIn("payload", package)
        self.assertNotIn("transcript", package)
        self.assertFalse(package["conversation_state_used"])

        self.controller.work_once("worker")
        finished = plane.checkpoint(mission_id)
        self.assertEqual(finished["recovery_class"], "terminal")
        self.assertEqual(finished["evidence"]["state"], "accepted")
        self.assertEqual(
            len([key for key in finished["operation_keys"] if key.endswith(":dispatch")]),
            1,
        )
        self.assertEqual(finished["safe_boundary"], "post_dispatch_reconciled")
        self.assertEqual(
            finished["uncertainty"]["irreversible_effect"], "reconciled"
        )

    def test_cooling_capacity_is_rechecked_after_restart_and_reset(self):
        self.store.set_runtime_policy(capacity.RuntimePolicy(
            runtime_id=ALPHA, max_observation_age_seconds=20,
        ))
        self.store.observe_capacity(capacity.CapacityObservation(
            runtime_id=ALPHA, state="exhausted", observed_at=self.clock.now,
            source="test-window", source_ref="window-1",
            expected_reset_at=self.clock.now + 10,
        ))
        mission_id = self.submit("capacity")
        plane = self.runtime()

        parked = plane.checkpoint(mission_id)
        self.assertEqual(parked["capacity_observation"]["state"], "exhausted")
        self.assertEqual(plane.status(project_id="project-a")["capacity"]["usable_now"], [])
        with self.assertRaises(shift_runtime.ShiftRefusal) as held:
            plane.resume_package(parked, target_profile=ALPHA)
        self.assertEqual(held.exception.code, "SHIFT_CAPACITY_UNUSABLE")

        self.clock.advance(11)
        self.store.observe_capacity(capacity.CapacityObservation(
            runtime_id=ALPHA, state="available", observed_at=self.clock.now,
            source="test-window", source_ref="window-2",
        ))
        reopened = plane.checkpoint(mission_id)
        self.assertEqual(reopened["capacity_observation"]["state"], "available")
        self.assertNotEqual(parked["checkpoint_hash"], reopened["checkpoint_hash"])
        self.assertEqual(
            plane.resume_package(reopened, target_profile=ALPHA)["runtime"]["target_profile"],
            ALPHA,
        )

    def test_work_baton_is_selected_only_for_its_project_and_kept_selective(self):
        baton = continuity.issue_payload(
            source="repo://project-a", head_sha="b" * 40,
            project_id="project-a", run_id="run-a", lane_id="lane-a",
            worktree="/disposable/project-a", branch="factory/run-a",
            safe_boundary="pre_dispatch", idempotency_key="capacity:run-a",
            required_capabilities=["implement"],
            compatible_profiles=[ALPHA],
            capacity_observation=capacity.CapacityObservation(
                runtime_id=ALPHA, state="available", observed_at=self.clock.now,
                source="test-window", source_ref="window-3",
            ).as_row(),
            evaluator="shift-test",
            uncertainty={"irreversible_effect": "none"},
            issued_at=self.clock.now,
        )
        continuity.WorkBatonStore(self.path, clock=self.clock).issue(baton)
        mission_id = self.submit("baton", work_baton_id=baton["baton_id"])
        checkpoint = self.runtime().checkpoint(mission_id)
        self.assertEqual(checkpoint["work_baton"]["state"], "issued")
        self.assertEqual(
            checkpoint["work_baton"]["references"][0]["project_id"], "project-a"
        )
        self.assertNotIn("payload", self.runtime().resume_package(checkpoint))

    def test_corrupt_dispatch_input_is_reported_as_repair_required(self):
        mission_id = self.submit("corrupt")
        claimed = self.store.claim("worker", lease_seconds=1)
        self.store.begin_step(
            mission_id, claimed["lease_token"], "dispatch",
            {"mission": claimed["payload"], "route": {
                "provider_profile": ALPHA,
                "idempotency_key": claimed["idempotency_key"],
                "recover_only": False,
            }},
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE steps SET input_json=? WHERE mission_id=? AND name='dispatch'",
                ("{not-json", mission_id),
            )
        checkpoint = self.runtime().checkpoint(mission_id)
        self.assertEqual(checkpoint["recovery_class"], "repair_required")
        self.assertIn("SHIFT_STEP_RECORD_CORRUPT", checkpoint["unresolved_blockers"])
        with self.assertRaises(shift_runtime.ShiftRefusal) as repair:
            self.runtime().resume_package(checkpoint)
        self.assertEqual(repair.exception.code, "SHIFT_REPAIR_REQUIRED")

    def test_uncertain_dispatch_keeps_route_and_operation_key_across_restart(self):
        adapter = LayerAdapter(crash_on="dispatch")
        controller = Controller(
            self.store, adapter,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            lease_seconds=1,
        )
        mission_id = self.submit("uncertain")
        with self.assertRaises(ProcessDeath):
            controller.work_once("dead-worker")

        started = self.store.step_record(mission_id, "dispatch")
        self.assertEqual(started["status"], "STARTED")
        self.assertEqual(started["input"]["route"]["provider_profile"], ALPHA)
        operation_key = started["operation_key"]

        self.clock.advance(2)
        replacement = Controller(
            MissionStore(self.path, clock=self.clock), adapter,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            lease_seconds=1,
        )
        plane = self.runtime(replacement)
        recovery = plane.recover()
        self.assertEqual(recovery["recovered_count"], 1)
        checkpoint = plane.checkpoint(mission_id)
        self.assertEqual(checkpoint["mission_state"], "dispatching")
        self.assertEqual(checkpoint["recovery_class"], "uncertain_dispatch")
        self.assertEqual(checkpoint["resume_target"], "reconcile_uncertain_dispatch")
        self.assertEqual(checkpoint["runtime"]["pending_profile"], ALPHA)

        package = plane.resume_package(checkpoint, target_profile=ALPHA)
        self.assertEqual(package["runtime"]["target_profile"], ALPHA)
        with self.assertRaises(shift_runtime.ShiftRefusal) as wrong_runtime:
            plane.resume_package(checkpoint, target_profile=BETA)
        self.assertEqual(wrong_runtime.exception.code,
                         "SHIFT_RUNTIME_INCOMPATIBLE")
        result = replacement.work_once("replacement")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.store.step_record(mission_id, "dispatch")["operation_key"],
                         operation_key)
        self.assertEqual(len(adapter.dispatches), 1)
        self.assertTrue(adapter.dispatches[0]["recover_only"])

        self.assertEqual(plane.resume_preview(mission_id)["plans"][0]["action"],
                         "complete")

    def test_suspend_drains_only_resumable_work_and_persists_checkpoint(self):
        crashing = LayerAdapter(crash_on="verify")
        controller = Controller(
            self.store, crashing,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=1,
        )
        in_flight = self.submit("in-flight")
        self.clock.advance(1)
        fresh = self.submit("fresh")
        with self.assertRaises(ProcessDeath):
            controller.work_once("dead-worker")
        self.clock.advance(2)

        plane = self.runtime(controller)
        plane.supervisor.transition("running", actor="owner", reason="test")
        result = plane.suspend(max_steps=4, actor="owner", reason="test suspend")
        self.assertTrue(result["bounded"])
        self.assertEqual(result["fresh_claims"], 0)
        self.assertEqual(self.store.get(in_flight)["state"], "completed")
        self.assertEqual(self.store.get(fresh)["state"], "admitted")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["control"]["state"], "stopped")

        events = self.store.coordination()
        shift_events = [row for row in events if row["decision"] == "shift"]
        self.assertTrue(all(row["reason"] == "SHIFT_CHECKPOINT"
                            for row in shift_events))
        checkpoint_reasons = [row["detail"]["reason"] for row in shift_events]
        self.assertIn("SHIFT_DRAIN_STARTED", checkpoint_reasons)
        self.assertIn("SHIFT_DRAIN_FINISHED", checkpoint_reasons)

    def test_status_and_resume_preview_are_project_isolated(self):
        alpha = self.submit("alpha")
        beta_payload = self.payload("beta", project_id="project-b",
                                    repository_remote_url="repo://project-b")
        beta, _ = self.controller.submit(beta_payload, "beta")
        plane = self.runtime()

        status = plane.status(project_id="project-a")
        ids = {row["mission_id"] for row in status["checkpoints"]}
        self.assertEqual(ids, {alpha})
        self.assertNotIn(beta["id"], str(status))
        preview = plane.resume_preview()
        self.assertEqual(
            {row["mission_id"] for row in preview["plans"]},
            {alpha, beta["id"]},
        )

    def test_checkpoint_tamper_and_cross_scope_fail_closed(self):
        mission_id = self.submit("tamper")
        checkpoint = self.runtime().checkpoint(mission_id)

        missing = copy.deepcopy(checkpoint)
        del missing["uncertainty"]
        with self.assertRaises(shift_runtime.ShiftRefusal) as missing_error:
            shift_runtime.validate_checkpoint(missing)
        self.assertEqual(missing_error.exception.code,
                         "SHIFT_CHECKPOINT_FIELD_MISSING")

        forged = copy.deepcopy(checkpoint)
        forged["idempotency_key"] = "other"
        with self.assertRaises(shift_runtime.ShiftRefusal) as forged_error:
            shift_runtime.validate_checkpoint(forged)
        self.assertEqual(forged_error.exception.code, "SHIFT_CHECKPOINT_FORGED")

        with self.assertRaises(shift_runtime.ShiftRefusal) as scope_error:
            self.runtime().status(mission_id, project_id="project-b")
        self.assertEqual(scope_error.exception.code, "SHIFT_CROSS_PROJECT")

    def test_no_unresolved_recovery_allows_no_fresh_work_until_resume_settles(self):
        crashing = LayerAdapter(crash_on="verify")
        controller = Controller(
            self.store, crashing,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=1,
        )
        first = self.submit("first")
        self.clock.advance(1)
        second = self.submit("second")
        with self.assertRaises(ProcessDeath):
            controller.work_once("dead")
        self.clock.advance(2)
        replacement = Controller(
            MissionStore(self.path, clock=self.clock), crashing,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=1,
        )
        # The normal scheduler also honors the recovery gate: second cannot be
        # claimed while first is waiting past the dispatch boundary.
        replacement.store.recover_stale()
        self.assertEqual(
            replacement.store.claim("new-worker", resume_only=True)["id"], first)
        self.assertIsNone(
            replacement.store.claim("another-worker", resume_only=True))
        self.assertEqual(replacement.store.get(second)["state"], "admitted")


if __name__ == "__main__":
    unittest.main()
