"""SF-142A capacity facts and restart-safe Work Baton integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factory_controller import capacity, continuity


class CapacityTests(unittest.TestCase):
    def observation(self, **overrides):
        fields = dict(runtime_id="codex-primary", state="available",
                      observed_at=100.0, source="runtime-probe",
                      source_ref="test-observation")
        fields.update(overrides)
        return capacity.CapacityObservation(**fields)

    def test_stale_unknown_and_uncertain_are_not_positive_capacity(self):
        policy = capacity.RuntimePolicy(runtime_id="codex-primary",
                                        max_observation_age_seconds=100)
        self.assertFalse(capacity.read(policy, self.observation(), now=201).usable)
        self.assertFalse(capacity.read(policy, None, now=150).usable)
        self.assertFalse(capacity.read(
            policy, self.observation(state="capacity_unmeasurable"), now=150).usable)

    def test_metered_capacity_is_never_an_automatic_fallback(self):
        reading = capacity.read(capacity.RuntimePolicy(runtime_id="codex-primary"),
                                self.observation(), now=150)
        result = capacity.plan(["codex-primary"], {"codex-primary": reading})
        self.assertEqual(result.admitted, ("codex-primary",))
        self.assertNotIn("openrouter-metered", result.admitted)


class WorkBatonTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name) / "controller.sqlite3"
        self.store = continuity.WorkBatonStore(self.path, clock=lambda: 150.0)
        self.exhausted = capacity.CapacityObservation(
            runtime_id="codex-primary", state="exhausted", observed_at=100.0,
            source="fake-5-hour-window", source_ref="window-1",
            window_started_at=100.0, expected_reset_at=18_100.0).as_row()
        self.available = capacity.read(
            capacity.RuntimePolicy(runtime_id="claude-secondary"),
            capacity.CapacityObservation(
                runtime_id="claude-secondary", state="available", observed_at=140.0,
                source="fake-5-hour-window", source_ref="window-2",
                window_started_at=140.0), now=150.0).as_row()
        self.fields = dict(
            source="https://example.invalid/project.git", head_sha="a" * 40,
            project_id="project-a", run_id="run-7", lane_id="lane-2",
            worktree="/disposable/project-a", branch="factory/run-7",
            safe_boundary="pre_dispatch", idempotency_key="mission:run-7:leg-2",
            required_capabilities=["prototype"],
            compatible_profiles=["codex-primary", "claude-secondary"],
            capacity_observation=self.exhausted, evaluator="controller-v1",
            uncertainty={"irreversible_effect": "none", "detail": "not dispatched"},
            issued_at=150.0)

    def tearDown(self):
        self.root.cleanup()

    def baton(self, **overrides):
        return continuity.issue_payload(**{**self.fields, **overrides})

    def consume(self, baton, **overrides):
        fields = dict(target_profile="claude-secondary", project_id="project-a",
                      source=self.fields["source"], head_sha="a" * 40,
                      capabilities=["prototype"],
                      capacity_reading=self.available, now=150.0)
        fields.update(overrides)
        return self.store.consume(baton, **fields)

    def test_fake_five_hour_exhaustion_checkpoint_resumes_compatibly(self):
        baton = self.baton()
        issued = self.store.issue(baton)
        self.assertEqual(issued["state"], "issued")
        consumed = self.consume(baton)
        self.assertEqual(consumed["consumed_by"], "claude-secondary")
        self.assertFalse(consumed["replayed"])

        restarted = continuity.WorkBatonStore(self.path, clock=lambda: 151.0)
        replay = restarted.consume(
            baton, target_profile="claude-secondary", project_id="project-a",
            source=self.fields["source"], head_sha="a" * 40,
            capabilities=["prototype"], capacity_reading=self.available,
            now=151.0)
        self.assertTrue(replay["replayed"])
        self.assertEqual(restarted.inspect(baton["baton_id"])["count"], 1)

    def test_duplicate_issue_and_consume_do_not_duplicate_effects(self):
        baton = self.baton()
        self.assertEqual(baton["baton_hash"], self.baton()["baton_hash"])
        self.assertFalse(self.store.issue(baton)["replayed"])
        self.assertTrue(self.store.issue(baton)["replayed"])
        self.assertFalse(self.consume(baton)["replayed"])
        self.assertTrue(self.consume(baton)["replayed"])

    def test_missing_forged_cross_project_and_stale_head_refuse(self):
        with self.assertRaises(continuity.BatonRefusal) as missing:
            continuity.issue_payload(**{key: value for key, value in self.fields.items()
                                        if key != "lane_id"})
        self.assertEqual(missing.exception.code, "BATON_FIELD_MISSING")
        baton = self.baton()
        forged = {**baton, "branch": "attacker"}
        with self.assertRaises(continuity.BatonRefusal) as caught:
            self.store.issue(forged)
        self.assertEqual(caught.exception.code, "BATON_FORGED")
        self.store.issue(baton)
        with self.assertRaises(continuity.BatonRefusal) as crossed:
            self.consume(baton, project_id="project-b")
        self.assertEqual(crossed.exception.code, "BATON_CROSS_PROJECT")
        with self.assertRaises(continuity.BatonRefusal) as stale:
            self.consume(baton, head_sha="b" * 40)
        self.assertEqual(stale.exception.code, "BATON_STALE_HEAD")

    def test_unsafe_post_dispatch_and_uncertain_effect_refuse(self):
        with self.assertRaises(continuity.BatonRefusal) as boundary:
            self.baton(safe_boundary="post_dispatch")
        self.assertEqual(boundary.exception.code, "BATON_BOUNDARY_UNSAFE")
        with self.assertRaises(continuity.BatonRefusal) as uncertain:
            self.baton(safe_boundary="post_dispatch_reconciled",
                       uncertainty={"irreversible_effect": "unknown"})
        self.assertEqual(uncertain.exception.code, "BATON_EFFECT_UNCERTAIN")

    def test_runtime_capability_capacity_and_metered_refuse(self):
        baton = self.baton()
        self.store.issue(baton)
        with self.assertRaises(continuity.BatonRefusal) as runtime:
            self.consume(baton, target_profile="other")
        self.assertEqual(runtime.exception.code, "BATON_RUNTIME_INCOMPATIBLE")
        with self.assertRaises(continuity.BatonRefusal) as capability_refusal:
            self.consume(baton, capabilities=[])
        self.assertEqual(capability_refusal.exception.code,
                         "BATON_CAPABILITY_MISSING")
        with self.assertRaises(continuity.BatonRefusal) as metered_refusal:
            self.consume(baton, target_profile="openrouter-metered")
        self.assertEqual(metered_refusal.exception.code, "BATON_RUNTIME_INCOMPATIBLE")
        forged_reading = {**self.available, "state": "capacity_unmeasurable",
                          "usable": True}
        with self.assertRaises(continuity.BatonRefusal) as unknown:
            self.consume(baton, capacity_reading=forged_reading)
        self.assertEqual(unknown.exception.code, "BATON_CAPACITY_UNUSABLE")
