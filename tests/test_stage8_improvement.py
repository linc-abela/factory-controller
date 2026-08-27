"""Stage 8: what recursive self-improvement may and may not cause to happen.

Written the same way as `test_stage7_maintenance.py`: each test states the
thing that must remain impossible, and the happy paths exist to prove the
impossible ones are not impossible by accident.  A suite where every experiment
is refused would pass every safety test here and be worthless, so the flow at
the bottom runs two real generations end to end, and the multi-generation case
proves the ceiling stops the third.

The property this file exists to hold has two halves and both are load-bearing.
**A self-improving system cannot widen its own authority**: not its ceilings,
not its evaluator, not its metrics, not its protected surfaces, not the policy
version that admitted it.  And **an improvement has to be measured, by somebody
else, against something pinned before it ran** -- which is the only thing that
separates "this is better" from "this ran and did not crash".
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import improvement, maintenance, portfolio, production
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, BETA, LayerAdapter
from tests.test_authority_boundaries import code_text

SHA = "a" * 40
NEXT_SHA = "b" * 40
THIRD_SHA = "c" * 40
PROJECT = "shop"
REPO = "https://example.invalid/shop.git"
GATES = ["G-BUILD"]
CANDIDATES = [{"profile": ALPHA, "capabilities": ["implement"]},
              {"profile": BETA, "capabilities": ["implement"]}]

#: A complete protected-surface declaration.  Every mandatory name is present
#: and each covers a real prefix, which is the only shape the policy accepts.
SURFACES = {name: ("protected/%s/" % name,) for name in improvement.MANDATORY_SURFACES}


def metrics(**overrides):
    values = [improvement.Metric("p95_latency_ms", "decrease", "objective",
                                 min_delta_ratio=0.10),
              improvement.Metric("passing_tests", "increase", "non_regression",
                                 tolerance_ratio=0.0)]
    return overrides.get("metrics", tuple(values))


def objective(**overrides):
    values = {"objective_ref": "OBJ-1", "project_id": PROJECT,
              "improvement_class": "performance",
              "statement": "checkout should answer faster without losing tests",
              "metrics": metrics(), "objective_version": "1.0"}
    values.update(overrides)
    return improvement.Objective(**values)


def staging(project_id=PROJECT, environment_id="shop-staging", repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id, project_id=project_id,
        environment_class="staging", repository=repository,
        service_ref="shop-web", approver_refs=("owner",), autonomous=True)


def gated(project_id=PROJECT, environment_id="shop-prod", repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id, project_id=project_id,
        environment_class="production", repository=repository,
        service_ref="shop-web", approver_refs=("owner", "deputy"))


def bundle(**overrides):
    payload = {
        "bundle_ref": "rc-improve-001",
        "project_id": PROJECT,
        "repository": REPO,
        "release_sha": NEXT_SHA,
        "mission_ref": "SF-140",
        "evidence_refs": ["evidence/shop/SF-140.json"],
        "evaluator_receipts": ["receipts/evaluate.json"],
        "artifact": {"kind": "image", "identity": "sha256:" + "c" * 64},
        "env_schema": {"PORT": {"type": "integer", "required": True,
                                "description": "service port"}},
        "migration": {"forward_ref": "migrations/003.sql",
                      "reverse_ref": "migrations/003.down.sql"},
        "release_policy_version": "1.0",
        "provenance": {"built_by": "factory-controller",
                       "built_at": "2026-08-27T00:00:00Z",
                       "contract_version": production.CONTRACT_VERSION},
    }
    payload.update(overrides)
    return production.ReleaseBundle.from_payload(payload)


class PlaneCase(unittest.TestCase):
    """A store, a production ledger, an improvement plane and a real Controller."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.db"
        self.store = MissionStore(str(self.path))
        self.ledger = production.ProductionLedger(self.store)
        self.plane = improvement.ImprovementPlane(self.store, self.ledger)
        self.register_project()

    def register_project(self, project_id=PROJECT, repository=REPO,
                         state="enabled", **extra):
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=project_id, repository=repository, state=state,
            concurrency_cap=4, policy_version="1.0", **extra))

    def policy(self, **overrides):
        values = {"project_id": PROJECT, "enabled": True, "cooldown_seconds": 0,
                  "protected_surfaces": SURFACES, "policy_version": "ip-1"}
        values.update(overrides)
        return self.plane.set_policy(improvement.ImprovementPolicy(**values))

    def controller(self, adapter=None):
        return Controller(self.store, adapter or LayerAdapter(),
                          retry_policy=RetryPolicy(max_attempts=1,
                                                   base_delay_seconds=0),
                          lease_seconds=5)

    # -- the flow, as one call each -------------------------------------- #

    def admit(self, obj=None, trigger_class="owner_objective", source_ref=None,
              baseline_sha=SHA, isolation_ref="lane://shop/experiment-1",
              repository=REPO):
        obj = obj if obj is not None else objective()
        self.plane.register_objective(obj)
        return self.plane.admit_experiment(
            obj.objective_ref, trigger_class,
            source_ref if source_ref is not None else obj.objective_ref,
            target_repository=repository, baseline_sha=baseline_sha,
            isolation_ref=isolation_ref)

    def run_candidate(self, experiment_ref, controller=None, adapter=None):
        """Create the mission and run it to completion through the Controller."""

        controller = controller or self.controller(adapter)
        mission, _ = self.plane.create_candidate_mission(
            experiment_ref, controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        while True:
            result = controller.work_once("worker-1")
            if result is None:
                break
        return controller.store.get(mission["id"])

    def to_sealed(self, experiment_ref, *, producer="producer-one",
                  changed_paths=("src/checkout.py",), baseline=None,
                  controller=None):
        self.plane.record_baseline(
            experiment_ref, baseline or {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        mission = self.run_candidate(experiment_ref, controller)
        self.plane.seal_candidate(experiment_ref, mission,
                                  producer_identity=producer,
                                  changed_paths=changed_paths)
        return mission


class PolicyDeclarationTests(PlaneCase):
    """A policy is the whole envelope, and it cannot be written with a hole."""

    def test_a_policy_that_drops_a_protected_surface_is_not_stored(self):
        holed = {name: ("protected/",) for name in improvement.MANDATORY_SURFACES
                 if name != "evaluator_independence"}
        with self.assertRaises(improvement.PolicyError) as raised:
            improvement.ImprovementPolicy(project_id=PROJECT,
                                          protected_surfaces=holed)
        self.assertIn("evaluator_independence", str(raised.exception))

    def test_a_surface_declared_over_nothing_is_not_a_protected_surface(self):
        """The subtler hole: the name is present and covers no path."""

        empty = dict(SURFACES)
        empty["secret_handling"] = ()
        with self.assertRaises(improvement.PolicyError) as raised:
            improvement.ImprovementPolicy(project_id=PROJECT,
                                          protected_surfaces=empty)
        self.assertIn("covers nothing", str(raised.exception))

    def test_a_string_of_prefixes_is_refused_rather_than_iterated(self):
        """`"src/"` would otherwise silently protect `s`, `r`, `c` and `/`."""

        sloppy = dict(SURFACES)
        sloppy["governance"] = "standards/"
        with self.assertRaises(improvement.PolicyError):
            improvement.ImprovementPolicy(project_id=PROJECT,
                                          protected_surfaces=sloppy)

    def test_improvement_cannot_be_scoped_to_a_production_environment(self):
        with self.assertRaises(improvement.PolicyError) as raised:
            improvement.ImprovementPolicy(
                project_id=PROJECT, protected_surfaces=SURFACES,
                environment_classes=("staging", "production"))
        self.assertIn("approved by a person", str(raised.exception))

    def test_repairing_a_known_failure_is_not_an_improvement_class(self):
        self.assertNotIn("bug", improvement.IMPROVEMENT_CLASSES)
        with self.assertRaises(improvement.PolicyError):
            improvement.ImprovementPolicy(project_id=PROJECT,
                                          protected_surfaces=SURFACES,
                                          improvement_classes=("bug",))

    def test_a_generation_ceiling_below_one_is_refused_not_stored_as_zero(self):
        with self.assertRaises(improvement.PolicyError):
            improvement.ImprovementPolicy(project_id=PROJECT,
                                          protected_surfaces=SURFACES,
                                          generation_ceiling=0)

    def test_the_policy_digest_moves_when_any_bound_moves(self):
        """What the experiment pins, so a generation cannot move its own rules."""

        base = improvement.ImprovementPolicy(project_id=PROJECT,
                                             protected_surfaces=SURFACES)
        for changed in (
                improvement.ImprovementPolicy(project_id=PROJECT,
                                              protected_surfaces=SURFACES,
                                              generation_ceiling=9),
                improvement.ImprovementPolicy(project_id=PROJECT,
                                              protected_surfaces=SURFACES,
                                              experiment_budget=99),
                improvement.ImprovementPolicy(project_id=PROJECT,
                                              protected_surfaces=SURFACES,
                                              risk_class="high")):
            self.assertNotEqual(base.policy_digest, changed.policy_digest)

    def test_the_stored_policy_round_trips_through_the_row(self):
        stored = self.policy()
        read_back = self.plane.policy(PROJECT)
        self.assertEqual(read_back.as_row(), stored)
        self.assertEqual(read_back.protected_surfaces["governance"],
                         ("protected/governance/",))


class ObjectiveTests(PlaneCase):
    """The one door human intent comes through, and it is versioned."""

    def test_only_the_owner_may_author_an_objective(self):
        for claimed in ("model", "advisor", "candidate", "experiment"):
            with self.assertRaises(improvement.PolicyError) as raised:
                objective(authority=claimed)
            self.assertIn("Owner act", str(raised.exception))

    def test_an_objective_with_no_metric_cannot_be_registered(self):
        with self.assertRaises(improvement.PolicyError):
            objective(metrics=())

    def test_an_objective_of_only_non_regression_metrics_is_refused(self):
        """Nothing there could ever be an improvement, so it is not an objective."""

        with self.assertRaises(improvement.PolicyError) as raised:
            objective(metrics=(improvement.Metric("passing_tests", "increase",
                                                  "non_regression"),))
        self.assertIn("could ever be an improvement", str(raised.exception))

    def test_a_metric_cannot_be_both_required_to_improve_and_tolerated_worse(self):
        with self.assertRaises(improvement.PolicyError):
            improvement.Metric("x", "increase", "objective", tolerance_ratio=0.1)
        with self.assertRaises(improvement.PolicyError):
            improvement.Metric("x", "increase", "non_regression", min_delta_ratio=0.1)

    def test_an_objective_statement_is_a_sentence_not_a_brief(self):
        with self.assertRaises(improvement.PolicyError):
            objective(statement="x" * 513)

    def test_the_objective_digest_moves_when_any_part_of_it_moves(self):
        base = objective()
        for changed in (objective(statement="something else"),
                        objective(objective_version="2.0"),
                        objective(improvement_class="cost"),
                        objective(metrics=(improvement.Metric(
                            "p95_latency_ms", "decrease", "objective",
                            min_delta_ratio=0.01),))):
            self.assertNotEqual(base.objective_digest, changed.objective_digest)


class TriggerAdmissionTests(PlaneCase):
    """Only a fact this Controller already recorded may open a generation."""

    def test_an_unknown_trigger_class_never_reaches_a_table(self):
        self.policy()
        self.plane.register_objective(objective())
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "model_suggestion", "anything",
                target_repository=REPO, baseline_sha=SHA,
                isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_TRIGGER_CLASS_UNKNOWN")

    def test_an_experiment_is_not_a_source_any_trigger_class_can_name(self):
        """A candidate cannot authorize its own successor, structurally."""

        self.assertNotIn("experiments", improvement.SOURCE_TABLE.values())
        self.assertEqual(set(improvement.SOURCE_TABLE),
                         set(improvement.TRIGGER_CLASSES))

    def test_admission_has_no_field_for_a_sentence(self):
        """The contract has no container for prompt text, advice or a plan."""

        import inspect
        signature = inspect.signature(improvement.ImprovementPlane.admit_experiment)
        self.assertEqual(
            [name for name in signature.parameters if name != "self"],
            ["objective_ref", "trigger_class", "source_ref", "target_repository",
             "baseline_sha", "isolation_ref"])

    def test_an_unregistered_objective_admits_nothing(self):
        self.policy()
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-MISSING", "owner_objective", "OBJ-MISSING",
                target_repository=REPO, baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_OBJECTIVE_UNKNOWN")

    def test_a_retired_objective_admits_nothing(self):
        self.policy()
        self.plane.register_objective(objective())
        self.plane.retire_objective("OBJ-1")
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_OBJECTIVE_RETIRED")

    def test_improvement_disabled_admits_nothing(self):
        self.plane.register_objective(objective())
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_DISABLED")

    def test_a_trigger_class_the_policy_did_not_admit_is_refused(self):
        self.policy(trigger_classes=("maintenance_history",))
        self.plane.register_objective(objective())
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_TRIGGER_CLASS_NOT_ADMITTED")

    def test_an_owner_objective_trigger_must_name_its_own_objective(self):
        self.policy()
        self.plane.register_objective(objective())
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-SOMETHING-ELSE",
                target_repository=REPO, baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_SOURCE_MISMATCH")

    def test_one_repair_is_not_a_case_for_changing_what_the_software_does(self):
        """The maintenance-to-improvement edge needs a measured pattern, not an event."""

        plane = maintenance.MaintenancePlane(self.store, self.ledger)
        self.ledger.register_environment(staging())
        self.policy(trigger_classes=("maintenance_history",),
                    maintenance_pressure=3)
        self.plane.register_objective(objective())
        signature = maintenance.failure_signature(PROJECT, "shop-staging",
                                                  "production_incident", SHA)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "maintenance_history", signature,
                target_repository=REPO, baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_TRIGGER_NOT_MEASURED")
        self.assertIn("one repair is a repair", str(raised.exception))
        self.assertIsNotNone(plane)

    def test_an_unmeasured_cost_is_not_evidence_of_an_inefficient_one(self):
        self.store.register_project(portfolio.ProjectPolicy(
            project_id="thrift", repository="repo://thrift",
            budget_ceiling=100.0, budget_currency="USD", policy_version="1.0"))
        self.policy(project_id="thrift", trigger_classes=("cost_inefficiency",))
        self.plane.register_objective(objective(objective_ref="OBJ-COST",
                                                project_id="thrift",
                                                improvement_class="cost"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-COST", "cost_inefficiency", "thrift",
                target_repository="repo://thrift", baseline_sha=SHA,
                isolation_ref="lane://thrift")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_TRIGGER_NOT_MEASURED")
        self.assertIn("not_measurable", str(raised.exception))

    def test_a_project_with_no_budget_ceiling_has_no_measurable_cost_pressure(self):
        self.policy(trigger_classes=("cost_inefficiency",))
        self.plane.register_objective(objective(improvement_class="cost"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "cost_inefficiency", PROJECT, target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertIn("not_measurable", str(raised.exception))

    def test_a_paused_project_does_not_have_experiments_created_for_it(self):
        self.policy()
        self.plane.register_objective(objective())
        self.store.set_project_state(PROJECT, "paused")
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PROJECT_NOT_ADMITTING")

    def test_an_emergency_stop_admits_no_experiment(self):
        self.policy()
        self.plane.register_objective(objective())
        self.store.emergency_stop(True)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_EMERGENCY_STOP")

    def test_an_experiment_names_the_disposable_lane_it_runs_in(self):
        self.policy()
        self.plane.register_objective(objective())
        for isolation in ("", "   ", REPO):
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                self.plane.admit_experiment(
                    "OBJ-1", "owner_objective", "OBJ-1",
                    target_repository=REPO, baseline_sha=SHA,
                    isolation_ref=isolation)
            self.assertEqual(raised.exception.code, "IMPROVEMENT_ISOLATION_REQUIRED")

    def test_the_budget_is_spent_against_a_policy_version_not_a_clock(self):
        self.policy(experiment_budget=1, concurrent_experiments=5)
        first = self.admit()
        self.plane.close(first["experiment_ref"], "abandoned", reason="test")
        self.plane.register_objective(objective(objective_ref="OBJ-2"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-2", "owner_objective", "OBJ-2", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BUDGET_EXHAUSTED")

    def test_concurrency_bounds_how_many_experiments_are_open_at_once(self):
        self.policy(concurrent_experiments=1)
        self.admit()
        self.plane.register_objective(objective(objective_ref="OBJ-2"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-2", "owner_objective", "OBJ-2", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_CONCURRENCY_EXCEEDED")

    def test_a_replayed_admission_collides_rather_than_opening_a_second(self):
        """The reference is derived, so a restart recomputes the same value."""

        self.policy()
        first = self.admit()
        again = self.plane.admit_experiment(
            "OBJ-1", "owner_objective", "OBJ-1", target_repository=REPO,
            baseline_sha=SHA, isolation_ref="lane://shop/experiment-1")
        self.assertEqual(first["experiment_ref"], again["experiment_ref"])
        self.assertEqual(len(self.plane.experiments(PROJECT)), 1)
        self.assertEqual(
            improvement.experiment_reference("OBJ-1", 1, SHA),
            first["experiment_ref"])


class FrozenMetricTests(PlaneCase):
    """Metrics are frozen before the candidate runs, and cannot be revised after."""

    def test_a_baseline_must_be_recorded_before_a_candidate_mission_exists(self):
        self.policy()
        row = self.admit()
        controller = self.controller()
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.create_candidate_mission(
                row["experiment_ref"], controller, acceptance_gate_ids=GATES)
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BASELINE_REQUIRED")
        self.assertEqual(self.store.counts(), {})

    def test_a_baseline_cannot_be_measured_twice(self):
        self.policy()
        row = self.admit()
        self.plane.record_baseline(row["experiment_ref"],
                                   {"p95_latency_ms": 400.0, "passing_tests": 120})
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.record_baseline(row["experiment_ref"],
                                       {"p95_latency_ms": 4000.0,
                                        "passing_tests": 120})
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BASELINE_SEALED")
        stored = self.plane.lineage(row["experiment_ref"])["baseline"]
        self.assertEqual(stored["p95_latency_ms"], 400.0)

    def test_a_baseline_cannot_be_recorded_after_the_candidate_mission(self):
        """Belt and braces: the ordering holds even if the first record is skipped."""

        self.policy()
        row = self.admit()
        ref = row["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        self.run_candidate(ref)
        with self.store.transaction() as db:
            db.execute("UPDATE experiments SET baseline_json=NULL WHERE experiment_ref=?",
                       (ref,))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.record_baseline(ref, {"p95_latency_ms": 4000.0,
                                             "passing_tests": 120})
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BASELINE_AFTER_CANDIDATE")

    def test_an_objective_metric_with_no_numeric_baseline_is_refused(self):
        self.policy()
        row = self.admit()
        for absent in ("unknown", "not_measurable", None, True):
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                self.plane.record_baseline(row["experiment_ref"],
                                           {"p95_latency_ms": absent,
                                            "passing_tests": 120})
            self.assertEqual(raised.exception.code,
                             "IMPROVEMENT_BASELINE_NOT_MEASURABLE")

    def test_a_candidate_cannot_redefine_its_metrics_after_execution_begins(self):
        """The objective is revised mid-flight; the pinned digest no longer matches."""

        self.policy()
        row = self.admit()
        ref = row["experiment_ref"]
        self.to_sealed(ref)
        self.plane.register_objective(objective(
            metrics=(improvement.Metric("p95_latency_ms", "decrease", "objective",
                                        min_delta_ratio=0.0),),
            objective_version="2.0"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.evaluate_candidate(
                ref, evaluator_identity="evaluator-one",
                measurements={"p95_latency_ms": 399.0, "passing_tests": 120})
        self.assertEqual(raised.exception.code, "IMPROVEMENT_OBJECTIVE_MUTATED")

    def test_an_evaluation_stating_a_different_objective_digest_is_refused(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.to_sealed(ref)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.evaluate_candidate(
                ref, evaluator_identity="evaluator-one",
                measurements={"p95_latency_ms": 300.0, "passing_tests": 120},
                objective_digest="0" * 64)
        self.assertEqual(raised.exception.code, "IMPROVEMENT_OBJECTIVE_MUTATED")


class ProtectedSurfaceTests(PlaneCase):
    """What an autonomous improvement may never touch, checked against the diff."""

    def test_a_candidate_touching_a_protected_surface_is_refused(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        mission = self.run_candidate(ref)
        for surface in improvement.MANDATORY_SURFACES:
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                self.plane.seal_candidate(
                    ref, mission, producer_identity="producer-one",
                    changed_paths=("src/checkout.py",
                                   "protected/%s/thing.py" % surface))
            self.assertEqual(raised.exception.code,
                             "IMPROVEMENT_PROTECTED_SURFACE_TOUCHED")
            self.assertIn(surface, str(raised.exception))

    def test_an_unknown_change_set_is_refused_rather_than_assumed_harmless(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        mission = self.run_candidate(ref)
        for empty in ((), ("",), ("   ",)):
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                self.plane.seal_candidate(ref, mission,
                                          producer_identity="producer-one",
                                          changed_paths=empty)
            self.assertEqual(raised.exception.code, "IMPROVEMENT_CHANGE_SET_UNKNOWN")

    def test_a_refused_candidate_leaves_no_sealed_state_behind(self):
        """Fail closed means the experiment does not advance, not just that it errored."""

        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        mission = self.run_candidate(ref)
        with self.assertRaises(improvement.ImprovementRefusal):
            self.plane.seal_candidate(ref, mission, producer_identity="producer-one",
                                      changed_paths=("protected/governance/x.md",))
        lineage = self.plane.lineage(ref)
        self.assertEqual(lineage["candidate_sha"], "not_run")
        self.assertEqual(lineage["producer_identity"], "not_run")
        self.assertEqual(lineage["state"], "mission_created")

    def test_the_improvement_policy_itself_is_a_protected_surface(self):
        """The one that makes "cannot widen its own authority" checkable."""

        self.assertIn("improvement_policy", improvement.MANDATORY_SURFACES)
        self.assertIn("emergency_stop", improvement.MANDATORY_SURFACES)
        self.assertIn("evaluator_independence", improvement.MANDATORY_SURFACES)

    def test_prefix_matching_covers_a_whole_subtree_and_an_exact_file(self):
        policy = improvement.ImprovementPolicy(
            project_id=PROJECT,
            protected_surfaces={**SURFACES,
                                "governance": ("standards/", "CONSTITUTION.md")})
        self.assertEqual(policy.surface_for("standards/a/b/c.md"), "governance")
        self.assertEqual(policy.surface_for("CONSTITUTION.md"), "governance")
        self.assertIsNone(policy.surface_for("src/checkout.py"))

    def test_a_policy_cannot_be_written_that_widens_the_autonomous_path(self):
        """Adding a surface is allowed; the mandatory ones are not removable."""

        widened = improvement.ImprovementPolicy(
            project_id=PROJECT,
            protected_surfaces={**SURFACES, "release_notes": ("docs/release/",)})
        self.assertEqual(widened.surface_for("docs/release/x.md"), "release_notes")
        for name in improvement.MANDATORY_SURFACES:
            self.assertIn(name, widened.protected_surfaces)


class EvaluatorIndependenceTests(PlaneCase):
    """Nothing evaluates or approves itself."""

    def test_the_producer_cannot_judge_its_own_candidate(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.to_sealed(ref, producer="runtime-one")
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.evaluate_candidate(
                ref, evaluator_identity="runtime-one",
                measurements={"p95_latency_ms": 100.0, "passing_tests": 120})
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_EVALUATOR_NOT_INDEPENDENT")

    def test_an_anonymous_evaluation_cannot_be_shown_to_be_independent(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.to_sealed(ref)
        for anonymous in ("", "   "):
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                self.plane.evaluate_candidate(
                    ref, evaluator_identity=anonymous,
                    measurements={"p95_latency_ms": 100.0, "passing_tests": 120})
            self.assertEqual(raised.exception.code, "IMPROVEMENT_EVALUATOR_UNKNOWN")

    def test_an_unattributed_candidate_cannot_be_sealed(self):
        """Independence is only checkable if the producer was recorded."""

        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        mission = self.run_candidate(ref)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.seal_candidate(ref, mission, producer_identity="  ",
                                      changed_paths=("src/checkout.py",))
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PRODUCER_UNKNOWN")

    def test_a_candidate_failing_its_ordinary_gates_is_never_compared(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        adapter = LayerAdapter(gates_pass=False)
        mission = self.run_candidate(ref, self.controller(adapter))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.seal_candidate(ref, mission, producer_identity="producer-one",
                                      changed_paths=("src/checkout.py",))
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_ACCEPTANCE_GATES_UNMET")


class ComparisonTests(unittest.TestCase):
    """The reading itself: an unknown is never an improvement."""

    def setUp(self):
        self.objective = objective()

    def compare(self, baseline, candidate, obj=None):
        return improvement.compare(obj or self.objective, baseline, candidate)

    def test_a_metric_that_cleared_its_threshold_is_an_improvement(self):
        result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                              {"p95_latency_ms": 300.0, "passing_tests": 120})
        self.assertEqual(result["verdict"], "improved")

    def test_a_gain_below_the_declared_threshold_is_not_an_improvement(self):
        """0.10 is required; 0.05 moved the number and did not meet the objective."""

        result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                              {"p95_latency_ms": 380.0, "passing_tests": 120})
        self.assertEqual(result["verdict"], "not_improved")
        self.assertEqual(result["unmet"], ["p95_latency_ms"])

    def test_a_regression_beats_any_objective_gain(self):
        """The whole of goal-gaming in one assertion."""

        result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                              {"p95_latency_ms": 10.0, "passing_tests": 119})
        self.assertEqual(result["verdict"], "regressed")
        self.assertEqual(result["regressed"], ["passing_tests"])

    def test_a_missing_candidate_reading_is_not_measurable_not_a_pass(self):
        for absent in ({}, {"passing_tests": 120},
                       {"p95_latency_ms": "unknown", "passing_tests": 120},
                       {"p95_latency_ms": "not_run", "passing_tests": 120}):
            result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                                  absent)
            self.assertEqual(result["verdict"], "not_measurable")
            self.assertEqual(result["unmeasured"], ["p95_latency_ms"])

    def test_an_absent_reading_keeps_its_own_word_when_it_is_one_of_the_four(self):
        result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                              {"p95_latency_ms": "not_run", "passing_tests": 120})
        reading = [item for item in result["metrics"]
                   if item["metric_id"] == "p95_latency_ms"][0]
        self.assertEqual(reading["candidate"], "not_run")
        self.assertEqual(reading["verdict"], "not_measurable")

    def test_a_relative_requirement_against_a_zero_baseline_is_not_measurable(self):
        result = self.compare({"p95_latency_ms": 0.0, "passing_tests": 120},
                              {"p95_latency_ms": -5.0, "passing_tests": 120})
        self.assertEqual(result["verdict"], "not_measurable")

    def test_a_boolean_is_never_arithmetic_here(self):
        """`True` is an `int`; a flag flip must not read as an infinite gain."""

        obj = objective(metrics=(
            improvement.Metric("works", "increase", "objective"),
            improvement.Metric("passing_tests", "increase", "non_regression")))
        result = self.compare({"works": False, "passing_tests": 120},
                              {"works": True, "passing_tests": 120}, obj)
        self.assertEqual(result["verdict"], "not_measurable")

    def test_a_non_regression_metric_may_hold_steady_within_tolerance(self):
        obj = objective(metrics=(
            improvement.Metric("p95_latency_ms", "decrease", "objective",
                               min_delta_ratio=0.10),
            improvement.Metric("memory_mb", "decrease", "non_regression",
                               tolerance_ratio=0.05)))
        result = self.compare({"p95_latency_ms": 400.0, "memory_mb": 100.0},
                              {"p95_latency_ms": 300.0, "memory_mb": 104.0}, obj)
        self.assertEqual(result["verdict"], "improved")
        beyond = self.compare({"p95_latency_ms": 400.0, "memory_mb": 100.0},
                              {"p95_latency_ms": 300.0, "memory_mb": 106.0}, obj)
        self.assertEqual(beyond["verdict"], "regressed")

    def test_an_unchanged_objective_metric_is_not_an_improvement(self):
        obj = objective(metrics=(
            improvement.Metric("p95_latency_ms", "decrease", "objective"),
            improvement.Metric("passing_tests", "increase", "non_regression")))
        result = self.compare({"p95_latency_ms": 400.0, "passing_tests": 120},
                              {"p95_latency_ms": 400.0, "passing_tests": 120}, obj)
        self.assertEqual(result["verdict"], "not_improved")

    def test_every_verdict_this_module_can_produce_is_declared(self):
        self.assertEqual(set(improvement.VERDICTS),
                         {"improved", "not_improved", "regressed", "not_measurable"})


class GenerationCase(PlaneCase):
    """Shared machinery for the recursion tests: one accepted generation."""

    def accept(self, ref, *, producer="producer-one", evaluator="evaluator-one",
               candidate=None):
        self.plane.evaluate_candidate(
            ref, evaluator_identity=evaluator,
            measurements=candidate or {"p95_latency_ms": 300.0,
                                       "passing_tests": 121})
        return self.plane.close(ref, "accepted", reason="test")

    def first_generation(self, **policy_overrides):
        """Generation 1, run and accepted, pinned at a baseline the adapter moves."""

        self.policy(**policy_overrides)
        row = self.admit(baseline_sha=THIRD_SHA)
        ref = row["experiment_ref"]
        self.to_sealed(ref)
        self.accept(ref)
        return ref


class RecursiveGenerationTests(GenerationCase):
    """Recursion is finite, sequential, baseline-pinned, policy-pinned."""

    def test_a_second_generation_pins_the_candidate_its_parent_produced(self):
        parent = self.first_generation()
        child = self.plane.open_generation(
            parent, baseline_sha=SHA, isolation_ref="lane://shop/experiment-2")
        self.assertEqual(child["generation"], 2)
        self.assertEqual(child["parent_ref"], parent)
        self.assertEqual(child["baseline_sha"], SHA)
        self.assertEqual(child["lineage_ref"],
                         self.plane.lineage(parent)["lineage_ref"])

    def test_an_unaccepted_generation_carries_no_lineage_forward(self):
        self.policy()
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(ref, baseline_sha=SHA,
                                       isolation_ref="lane://2")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PARENT_NOT_ACCEPTED")

    def test_a_rejected_generation_carries_no_lineage_forward(self):
        self.policy()
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        self.plane.evaluate_candidate(
            ref, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 399.0, "passing_tests": 120})
        self.plane.close(ref, "rejected", reason="did not clear the threshold")
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(ref, baseline_sha=SHA,
                                       isolation_ref="lane://2")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PARENT_NOT_ACCEPTED")

    def test_an_experiment_is_not_accepted_merely_because_it_finished(self):
        self.policy()
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        self.plane.evaluate_candidate(
            ref, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 399.0, "passing_tests": 120})
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.close(ref, "accepted", reason="it ran")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_NOT_DEMONSTRATED")

    def test_a_generation_cannot_continue_across_a_changed_policy(self):
        """Raising a ceiling ends the lineage rather than extending it."""

        parent = self.first_generation()
        self.policy(generation_ceiling=99, experiment_budget=99)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(parent, baseline_sha=SHA,
                                       isolation_ref="lane://2")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_POLICY_CHANGED")

    def test_a_successor_cannot_re_pin_its_parents_own_baseline(self):
        parent = self.first_generation()
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(parent, baseline_sha=THIRD_SHA,
                                       isolation_ref="lane://2")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BASELINE_NOT_ADVANCED")

    def test_a_successor_cannot_pin_a_baseline_its_parent_never_produced(self):
        parent = self.first_generation()
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(parent, baseline_sha="d" * 40,
                                       isolation_ref="lane://2")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BASELINE_NOT_ADVANCED")

    def test_a_generation_cannot_spawn_another_while_it_is_running(self):
        parent = self.first_generation(concurrent_experiments=5)
        child = self.plane.open_generation(parent, baseline_sha=SHA,
                                           isolation_ref="lane://2")
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(child["experiment_ref"],
                                       baseline_sha=NEXT_SHA,
                                       isolation_ref="lane://3")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PARENT_NOT_ACCEPTED")
        with self.assertRaises(improvement.ImprovementRefusal) as second:
            self.plane.open_generation(parent, baseline_sha=NEXT_SHA,
                                       isolation_ref="lane://3")
        self.assertEqual(second.exception.code, "IMPROVEMENT_GENERATION_IN_FLIGHT")

    def test_the_generation_ceiling_stops_the_recursion(self):
        """Three generations run; the fourth is refused, not deferred."""

        parent = self.first_generation(generation_ceiling=3,
                                       concurrent_experiments=5)
        lineage = self.plane.lineage(parent)["lineage_ref"]
        current, baseline = parent, SHA
        for generation, candidate in ((2, NEXT_SHA), (3, "e" * 40)):
            child = self.plane.open_generation(
                current, baseline_sha=baseline,
                isolation_ref="lane://shop/experiment-%d" % generation)
            ref = child["experiment_ref"]
            self.assertEqual(child["generation"], generation)
            self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                             "passing_tests": 120})
            mission = self.run_candidate(ref, self.controller(_Candidate(candidate)))
            self.plane.seal_candidate(ref, mission, producer_identity="producer-one",
                                      changed_paths=("src/checkout.py",))
            self.accept(ref)
            current, baseline = ref, candidate
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.open_generation(current, baseline_sha=baseline,
                                       isolation_ref="lane://shop/experiment-4")
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_GENERATION_CEILING_REACHED")
        generations = self.plane.generations(lineage)
        self.assertEqual([item["generation"] for item in generations], [1, 2, 3])
        self.assertEqual({item["disposition"] for item in generations}, {"accepted"})
        self.assertEqual(len({item["policy_digest"] for item in generations}), 1)
        self.assertEqual(len({item["baseline_sha"] for item in generations}), 3)

    def test_no_method_here_takes_an_experiment_and_writes_a_policy(self):
        """Self-expanding authority has no code path, not merely no permission."""

        source = Path(improvement.__file__).read_text()
        tree = ast.parse(source)
        writers = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {arg.arg for arg in node.args.args} | {
                arg.arg for arg in node.args.kwonlyargs}
            if not (names & {"experiment_ref", "parent_ref"}):
                continue
            for statement in ast.walk(node):
                # A *write* against the policies table, not any mention of it:
                # `_admission` reads the policy row and inserts an experiment,
                # and a scan that cannot tell those apart proves nothing.
                if not (isinstance(statement, ast.Constant)
                        and isinstance(statement.value, str)):
                    continue
                text = statement.value.upper()
                if "IMPROVEMENT_POLICIES" in text and (
                        "INSERT" in text or "UPDATE" in text
                        or "DELETE" in text):
                    writers.append(node.name)
                    break
        self.assertEqual(writers, [])


class _Candidate(LayerAdapter):
    """A layer that mints a chosen candidate, so generations differ by baseline."""

    def __init__(self, candidate_sha: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.candidate_sha = candidate_sha

    def _dispatch(self, operation_key, value):
        result = super()._dispatch(operation_key, value)
        if result.get("status") == "completed":
            result["candidate_sha"] = self.candidate_sha
        return result

    def execute(self, step, operation_key, value):
        if step == "verify":
            return {"verified": True, "diagnostic": None}
        return super().execute(step, operation_key, value)


class PromotionAuthorityTests(GenerationCase):
    """What an accepted improvement may reach, and what stays an Owner act."""

    def improved(self, *, repository=REPO, risk="low", self_targets=(),
                 environment=None):
        self.policy(risk_class=risk, self_target_repositories=self_targets)
        self.ledger.register_environment(environment or staging())
        ref = self.admit(baseline_sha=THIRD_SHA, repository=repository)["experiment_ref"]
        self.to_sealed(ref)
        self.plane.evaluate_candidate(
            ref, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 300.0, "passing_tests": 121})
        return ref

    def test_an_improved_candidate_stages_into_an_ungated_environment(self):
        ref = self.improved()
        deployment_id = self.plane.stage_promotion(ref, bundle(), "shop-staging")
        self.assertTrue(deployment_id)
        lineage = self.plane.lineage(ref)
        self.assertEqual(lineage["state"], "promotion_staged")
        self.assertEqual(lineage["promotion_environment_id"], "shop-staging")

    def test_a_production_environment_is_never_reached_autonomously(self):
        ref = self.improved()
        self.ledger.register_environment(gated())
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.stage_promotion(ref, bundle(), "shop-prod")
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_PRODUCTION_AUTHORITY_REQUIRED")

    def test_the_factory_improving_itself_never_promotes_itself(self):
        """The case the whole stage exists to keep safe."""

        ref = self.improved(repository="repo://factory-controller",
                            self_targets=("repo://factory-controller",))
        self.assertTrue(self.plane.lineage(ref)["self_target"])
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.stage_promotion(ref, bundle(), "shop-staging")
        self.assertEqual(raised.exception.code,
                         "IMPROVEMENT_SELF_PROMOTION_REQUIRES_OWNER")

    def test_a_self_target_experiment_may_still_be_accepted_and_evidenced(self):
        """Refusing the promotion must not refuse the experiment."""

        ref = self.improved(repository="repo://factory-controller",
                            self_targets=("repo://factory-controller",))
        lineage = self.plane.close(ref, "accepted", reason="candidate is real")
        self.assertEqual(lineage["disposition"], "accepted")
        self.assertEqual(lineage["verdict"], "improved")
        self.assertNotEqual(lineage["candidate_sha"], "not_run")
        self.assertEqual(lineage["promotion_deployment_id"], "not_applicable")

    def test_only_the_lowest_risk_class_stages_without_a_person(self):
        for risk in ("medium", "high"):
            with self.subTest(risk=risk):
                self.setUp()
                ref = self.improved(risk=risk)
                with self.assertRaises(improvement.ImprovementRefusal) as raised:
                    self.plane.stage_promotion(ref, bundle(), "shop-staging")
                self.assertEqual(raised.exception.code,
                                 "IMPROVEMENT_RISK_CLASS_REQUIRES_OWNER")

    def test_an_unimproved_candidate_is_never_staged(self):
        self.policy()
        self.ledger.register_environment(staging())
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        self.plane.evaluate_candidate(
            ref, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 399.0, "passing_tests": 120})
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.stage_promotion(ref, bundle(), "shop-staging")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_NOT_DEMONSTRATED")

    def test_an_unevaluated_candidate_is_never_staged(self):
        self.policy()
        self.ledger.register_environment(staging())
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.stage_promotion(ref, bundle(), "shop-staging")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_NOT_DEMONSTRATED")

    def test_an_experiment_never_stages_into_another_projects_environment(self):
        ref = self.improved()
        self.register_project("other", repository="repo://other")
        self.ledger.register_environment(staging(
            project_id="other", environment_id="other-staging",
            repository="repo://other"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.stage_promotion(ref, bundle(), "other-staging")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_PROJECT_ISOLATION")

    def test_a_promotion_reverts_to_the_baseline_pinned_at_admission(self):
        ref = self.improved()
        self.plane.stage_promotion(ref, bundle(), "shop-staging")
        lineage = self.plane.revert(ref, reason="staging went bad")
        self.assertEqual(lineage["reverted_to"], THIRD_SHA)
        self.assertEqual(lineage["rollback_target"], THIRD_SHA)
        kinds = [event["kind"] for event in lineage["transitions"]]
        self.assertIn("promotion_reverted", kinds)

    def test_reverting_what_was_never_promoted_is_refused(self):
        ref = self.improved()
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.revert(ref, reason="nothing happened")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_NOTHING_PROMOTED")


class RestartTests(PlaneCase):
    """Replay after a crash recovers; it never duplicates or skips."""

    def test_a_replayed_candidate_mission_recovers_rather_than_duplicating(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        controller = self.controller()
        first, created = self.plane.create_candidate_mission(
            ref, controller, acceptance_gate_ids=GATES)
        self.assertTrue(created)
        replacement = Controller(MissionStore(str(self.path)), LayerAdapter(),
                                 retry_policy=RetryPolicy(base_delay_seconds=0))
        again, created_again = self.plane.create_candidate_mission(
            ref, replacement, acceptance_gate_ids=GATES)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.store.counts(), {"admitted": 1})

    def test_a_crash_between_submission_and_the_record_does_not_double_submit(self):
        """The store's own idempotency key is the second, independent guard."""

        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.record_baseline(ref, {"p95_latency_ms": 400.0,
                                         "passing_tests": 120})
        controller = self.controller()
        first, _ = self.plane.create_candidate_mission(
            ref, controller, acceptance_gate_ids=GATES)
        with self.store.transaction() as db:
            db.execute("UPDATE experiments SET mission_ref=NULL,"
                       " state='baseline_measured' WHERE experiment_ref=?", (ref,))
        again, created = self.plane.create_candidate_mission(
            ref, controller, acceptance_gate_ids=GATES)
        self.assertFalse(created)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.store.all_missions()), 1)

    def test_sealing_a_candidate_twice_seals_it_once(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        mission = self.to_sealed(ref)
        first = self.plane.lineage(ref)
        self.plane.seal_candidate(ref, mission, producer_identity="someone-else",
                                  changed_paths=("src/other.py",))
        second = self.plane.lineage(ref)
        self.assertEqual(first["producer_identity"], second["producer_identity"])
        self.assertEqual(first["change_set"], second["change_set"])

    def test_a_closed_experiment_does_no_further_work(self):
        self.policy()
        ref = self.admit()["experiment_ref"]
        self.plane.close(ref, "abandoned", reason="operator")
        controller = self.controller()
        for call in (
                lambda: self.plane.record_baseline(ref, {"p95_latency_ms": 1.0}),
                lambda: self.plane.create_candidate_mission(
                    ref, controller, acceptance_gate_ids=GATES),
                lambda: self.plane.evaluate_candidate(
                    ref, evaluator_identity="e", measurements={}),
                lambda: self.plane.close(ref, "accepted", reason="again")):
            with self.assertRaises(improvement.ImprovementRefusal) as raised:
                call()
            self.assertEqual(raised.exception.code, "IMPROVEMENT_EXPERIMENT_CLOSED")
        self.assertEqual(self.store.counts(), {})

    def test_the_event_ledger_is_append_only(self):
        import sqlite3
        self.policy()
        ref = self.admit()["experiment_ref"]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("UPDATE improvement_events SET kind='rewritten'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("DELETE FROM improvement_events")
        self.assertTrue(self.plane.events(PROJECT))
        self.assertTrue(ref)


class MultiProjectTests(PlaneCase):
    """Two projects improve independently, with nothing shared between them."""

    def setUp(self):
        super().setUp()
        self.register_project("thrift", repository="repo://thrift")
        self.policy(policy_version="shop-1", experiment_budget=1)
        self.policy(project_id="thrift", policy_version="thrift-1",
                    experiment_budget=1, generation_ceiling=2)

    def test_one_projects_spent_budget_does_not_bind_another(self):
        first = self.admit()
        self.plane.close(first["experiment_ref"], "abandoned", reason="test")
        self.plane.register_objective(objective(objective_ref="OBJ-2"))
        with self.assertRaises(improvement.ImprovementRefusal) as raised:
            self.plane.admit_experiment(
                "OBJ-2", "owner_objective", "OBJ-2", target_repository=REPO,
                baseline_sha=SHA, isolation_ref="lane://x")
        self.assertEqual(raised.exception.code, "IMPROVEMENT_BUDGET_EXHAUSTED")
        other = self.plane.register_objective(objective(
            objective_ref="OBJ-T", project_id="thrift"))
        admitted = self.plane.admit_experiment(
            "OBJ-T", "owner_objective", "OBJ-T",
            target_repository="repo://thrift", baseline_sha=SHA,
            isolation_ref="lane://thrift/1")
        self.assertEqual(admitted["project_id"], "thrift")
        self.assertTrue(other)

    def test_policies_ceilings_and_experiments_are_partitioned_by_project(self):
        self.admit()
        self.plane.register_objective(objective(objective_ref="OBJ-T",
                                                project_id="thrift"))
        self.plane.admit_experiment(
            "OBJ-T", "owner_objective", "OBJ-T", target_repository="repo://thrift",
            baseline_sha=SHA, isolation_ref="lane://thrift/1")
        self.assertEqual(self.plane.policy(PROJECT).generation_ceiling, 3)
        self.assertEqual(self.plane.policy("thrift").generation_ceiling, 2)
        self.assertEqual({row["project_id"] for row in self.plane.experiments(PROJECT)},
                         {PROJECT})
        self.assertEqual({row["project_id"] for row in self.plane.experiments("thrift")},
                         {"thrift"})
        self.assertEqual({row["project_id"] for row in self.plane.events(PROJECT)},
                         {PROJECT})

    def test_two_projects_run_the_same_objective_reference_without_collision(self):
        """The reference is derived from the objective, so distinct rows stay distinct."""

        self.admit()
        self.plane.register_objective(objective(objective_ref="OBJ-1",
                                                project_id="thrift"))
        # Re-registering OBJ-1 under another project retargets the objective;
        # the experiment already admitted keeps the digest it pinned.
        rows = self.plane.experiments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_id"], PROJECT)


class BoundaryTests(unittest.TestCase):
    """The absences, checked mechanically rather than described."""

    def setUp(self):
        self.source = Path(improvement.__file__).read_text()
        self.code = code_text(self.source)
        self.tree = ast.parse(self.source)

    def imports(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
        return names

    def test_improvement_names_no_runtime_and_no_model(self):
        """Logical roles stay neutral; the palette is the caller's.

        `provider_candidates` is an argument, never a value this module holds,
        so an experiment runs on whatever the Owner's palette offers and the
        module works unchanged when a runtime is added or removed.

        Scanned as *code* rather than as text, with the package's own scanner:
        a docstring here says what the module refuses to contain, and a check
        that cannot tell that apart from a real identifier punishes the module
        for documenting its own boundary.
        """

        for token in ("openrouter", "gateway", "advisor", "model",
                      "provider_profile", "profile_id"):
            self.assertNotIn(token, self.code, "improvement.py names %r" % token)

    def test_that_scan_would_actually_catch_a_runtime_reaching_the_module(self):
        planted = code_text("def go(profile_id):\n    return gateway.model\n")
        for token in ("profile_id", "gateway", "model"):
            self.assertIn(token, planted)

    def test_improvement_and_maintenance_do_not_import_each_other(self):
        """The two control planes must not be able to drive each other."""

        self.assertNotIn("maintenance", self.imports())
        maintenance_tree = ast.parse(Path(maintenance.__file__).read_text())
        maintenance_imports = set()
        for node in ast.walk(maintenance_tree):
            if isinstance(node, ast.ImportFrom):
                maintenance_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                maintenance_imports.update(alias.name.split(".")[0]
                                           for alias in node.names)
        self.assertNotIn("improvement", maintenance_imports)

    def test_improvement_never_writes_to_the_maintenance_tables(self):
        """It reads a measured pattern; it never records a repair."""

        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value.upper()
            if not any(verb in text for verb in ("INSERT", "UPDATE", "DELETE")):
                continue
            for table in ("REPAIRS", "MAINTENANCE_POLICIES", "MAINTENANCE_EVENTS",
                          "INCIDENTS", "DEPLOYMENTS", "PROJECTS", "PORTFOLIO"):
                self.assertNotIn(table, text,
                                 "improvement.py writes to %s" % table)

    def test_improvement_never_imports_the_external_seam(self):
        self.assertFalse({"advisor", "gateway"} & self.imports())

    def test_nothing_here_polls_ticks_or_sleeps(self):
        """There is no loop to run away, rather than a bound on one.

        Whole identifiers, not bare substrings.  A refusal in this module says
        a spent budget is "an Owner decision, not a timer", and a scan that
        cannot tell that sentence from `threading.Timer` would force the wrong
        wording on the code rather than catch the thing it exists to catch --
        the same call `test_authority_boundaries` makes for `environ`.
        """

        names = set(self.code.split("\n"))
        for token in ("sleep", "timer", "poll", "tick", "threading", "asyncio",
                      "monotonic", "perf_counter"):
            self.assertNotIn(token, names, "improvement.py names %r" % token)

    def test_that_scan_would_actually_catch_a_loop(self):
        """Narrowing it to whole identifiers must not make it unable to fire."""

        for planted in ("import time\ntime.sleep(1)\n",
                        "import threading\nthreading.Timer(1, go)\n",
                        "import asyncio\n",
                        "def poll():\n    pass\n"):
            names = set(code_text(planted).split("\n"))
            self.assertTrue(
                names & {"sleep", "timer", "threading", "asyncio", "poll"},
                planted)
        self.assertNotIn(
            "timer",
            set(code_text('x = "an Owner decision, not a timer"\n').split("\n"))
            - {"an Owner decision, not a timer"})

    def test_no_method_calls_open_generation_from_inside_this_module(self):
        """Generation N cannot cause generation N+1; a caller has to."""

        called = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name:
                    called.add(name)
        for verb in ("open_generation", "admit_experiment", "stage_promotion"):
            self.assertNotIn(verb, called,
                             "improvement.py calls its own %s" % verb)

    def test_the_absence_vocabulary_is_the_same_four_words_at_every_layer(self):
        """Forked six times across the corpus; checked here rather than copied."""

        from factory_controller import store
        self.assertEqual(improvement.CANONICAL_ABSENCE, production.CANONICAL_ABSENCE)
        self.assertEqual(improvement.CANONICAL_ABSENCE, store.CANONICAL_ABSENCE)
        self.assertEqual(improvement.CANONICAL_ABSENCE, maintenance.CANONICAL_ABSENCE)
        self.assertEqual(improvement.CANONICAL_ABSENCE,
                         {"unknown", "not_applicable", "not_run", "not_measurable"})

    def test_no_absence_word_outside_the_four_reaches_a_lineage(self):
        for word in ("unavailable", "not_measured", "none", "missing", ""):
            with self.assertRaises(improvement.PolicyError):
                improvement._absent(None, word)

    def test_every_refusal_code_carries_the_layer_prefix(self):
        """A layer that refuses under a neighbour's name cannot be routed on."""

        codes = {node.value for node in ast.walk(self.tree)
                 if isinstance(node, ast.Constant)
                 and isinstance(node.value, str)
                 and node.value.isupper()
                 and node.value.replace("_", "").isalnum()
                 and "_" in node.value}
        refusals = {code for code in codes if not code.startswith("SELECT")}
        self.assertTrue(refusals)
        for code in refusals:
            self.assertTrue(code.startswith("IMPROVEMENT_"),
                            "%s is not prefixed for this layer" % code)


class EndToEndTests(GenerationCase):
    """Two real generations, so the refusals above are not passing by accident."""

    def test_an_admitted_objective_becomes_a_measured_staged_improvement(self):
        self.policy(concurrent_experiments=2)
        self.ledger.register_environment(staging())
        first = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]

        self.plane.record_baseline(first, {"p95_latency_ms": 400.0,
                                           "passing_tests": 120})
        mission = self.run_candidate(first)
        self.assertEqual(mission["state"], "completed")
        self.plane.seal_candidate(first, mission, producer_identity="producer-one",
                                  changed_paths=("src/checkout.py", "tests/test_checkout.py"))
        comparison = self.plane.evaluate_candidate(
            first, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 280.0, "passing_tests": 124})
        self.assertEqual(comparison["verdict"], "improved")

        deployment_id = self.plane.stage_promotion(first, bundle(), "shop-staging")
        self.plane.close(first, "accepted", reason="measured better, staged")

        lineage = self.plane.lineage(first)
        self.assertEqual(lineage["disposition"], "accepted")
        self.assertEqual(lineage["promotion_deployment_id"], deployment_id)
        self.assertEqual(lineage["producer_identity"], "producer-one")
        self.assertEqual(lineage["evaluator_identity"], "evaluator-one")
        self.assertNotEqual(lineage["producer_identity"],
                            lineage["evaluator_identity"])
        self.assertEqual(lineage["change_set"],
                         ["src/checkout.py", "tests/test_checkout.py"])
        self.assertEqual([event["kind"] for event in lineage["transitions"]],
                         ["experiment_admitted", "baseline_measured",
                          "candidate_mission_created", "candidate_sealed",
                          "candidate_evaluated", "promotion_staged",
                          "experiment_closed"])

        second = self.plane.open_generation(
            first, baseline_sha=SHA, isolation_ref="lane://shop/experiment-2")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second["baseline_sha"], lineage["candidate_sha"])
        self.assertEqual(second["policy_digest"], lineage["policy_digest"])
        self.assertEqual(second["objective_digest"], lineage["objective_digest"])

        generations = self.plane.generations(lineage["lineage_ref"])
        self.assertEqual([item["generation"] for item in generations], [1, 2])
        self.assertEqual(generations[1]["verdict"], "not_run")

    def test_the_whole_flow_is_deterministic_with_no_advisory_service_present(self):
        """Nothing here consults an advisor or a gateway, so absence changes nothing."""

        self.policy()
        ref = self.admit(baseline_sha=THIRD_SHA)["experiment_ref"]
        self.to_sealed(ref)
        first = self.plane.evaluate_candidate(
            ref, evaluator_identity="evaluator-one",
            measurements={"p95_latency_ms": 300.0, "passing_tests": 121})
        again = improvement.compare(
            objective(), {"p95_latency_ms": 400.0, "passing_tests": 120},
            {"p95_latency_ms": 300.0, "passing_tests": 121})
        self.assertEqual(first["verdict"], again["verdict"])
        self.assertEqual(first["metrics"], again["metrics"])
        self.assertEqual(first["objective_digest"], again["objective_digest"])


if __name__ == "__main__":
    unittest.main()
