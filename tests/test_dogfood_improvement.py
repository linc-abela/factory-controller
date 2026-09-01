"""The frozen portfolio's improvement slot, joined to the Stage-8 plane.

`test_stage8_improvement.py` holds what recursive self-improvement may never
cause to happen.  This file holds the seam that finally calls it, and every
test here is about that seam not becoming a second authority: the measurements
come from the project's own declared gates, the baseline is pinned before the
candidate exists, the producer is not the evaluator, and a promotion is the
production ledger's decision rather than this seam's.

The happy path at the bottom is deliberate.  A suite where every candidate is
refused would pass every safety test above it and prove nothing, which is the
`CB-3` shape the corpus already records: a green harness standing in for the
thing it was supposed to prove.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import (
    dogfood_improvement, improvement, portfolio, production,
)
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, LayerAdapter


ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_PATH = ROOT / "contracts" / "first-dogfood-improvement-objective.json"
PROJECT = "factory-prototype-lab"
REPO = "https://github.com/linc-abela/factory-prototype-lab.git"
BASELINE = "229b923b050fe8a4450d5597d472157bd42c8647"
CANDIDATE = "ee816e5c45a1eed7714c998060792a643c5dd4cb"
GATES = ["dev-check", "dev-test", "dev-evaluate"]

#: What the lab's own gates print.  Copied from a real run of them rather than
#: composed, because the readers exist to read exactly this and a fixture that
#: drifted from it would test the fixture.
UNITTEST_OK = ("test_a (t.T) ... ok\n\n"
               "----------------------------------------------------------\n"
               "Ran 2 tests in 0.001s\n\nOK\n")
UNITTEST_FAILED = ("test_a (t.T) ... FAIL\n\n"
                   "----------------------------------------------------\n"
                   "Ran 3 tests in 0.002s\n\nFAILED (failures=1)\n")
EVALUATE_JSON = json.dumps(
    {"correct": 5, "false_matches": 0, "proceed": True, "total": 5,
     "results": []}, indent=2, sort_keys=True)


def gate(gate_id, *, stdout="", stderr="", exit_code=0):
    return {"gate_id": gate_id, "passed": exit_code == 0,
            "exit_code": exit_code, "evidence_class": "rederived",
            "stdout_tail": stdout or "not_applicable",
            "stderr_tail": stderr or "not_applicable"}


def baseline_gates(*, tests=UNITTEST_OK, evaluate=EVALUATE_JSON):
    return [gate("dev-check"),
            gate("dev-test", stderr=tests),
            gate("dev-evaluate", stdout=evaluate)]


def candidate_gates(*, ran=4, correct=5, false_matches=0):
    tests = ("Ran %d tests in 0.003s\n\nOK\n" % ran) if ran else ""
    evaluate = json.dumps({"correct": correct, "false_matches": false_matches,
                           "proceed": correct >= 4 and false_matches <= 1,
                           "total": 5}, sort_keys=True)
    return [gate("dev-check"),
            gate("dev-test", stderr="\n" + tests),
            gate("dev-evaluate", stdout=evaluate)]


def contract(**overrides):
    body = json.loads(OBJECTIVE_PATH.read_text(encoding="utf-8"))
    body.update(overrides)
    return dogfood_improvement.contract_from_payload(body)


class ObjectiveContractTests(unittest.TestCase):
    """What the Controller will and will not accept as an Owner objective."""

    def test_the_shipped_objective_loads_and_binds_the_dogfood_project(self):
        loaded = dogfood_improvement.load(OBJECTIVE_PATH)

        self.assertEqual(loaded.project_id, PROJECT)
        self.assertEqual(loaded.objective.authority, "owner")
        self.assertEqual(loaded.trigger_class, "owner_objective")
        self.assertEqual(loaded.environment, "factory-prototype-lab-staging")
        self.assertEqual(loaded.gate_ids, ("dev-test", "dev-evaluate"))
        self.assertEqual(len(loaded.contract_digest), 64)

    def test_its_statement_fits_the_channel_that_carries_it(self):
        """The brief is one bounded metadata value on the bridge request.

        Reproduced from `factory_bridge/provider.py` MISSION_BRIEF_LIMIT and
        `src/cli/first_live.py`, both of which refuse a longer one -- so an
        objective the provider could never be told is refused here, where it
        is an Owner's editing mistake rather than a mission's refusal.
        """

        self.assertLessEqual(
            len(dogfood_improvement.load(OBJECTIVE_PATH).objective.statement), 256)

    def test_every_declared_gate_is_one_the_frozen_portfolio_runs(self):
        loaded = dogfood_improvement.load(OBJECTIVE_PATH)
        frozen = json.loads(
            (ROOT / "contracts" / "first-dogfood-mission-portfolio.json")
            .read_text(encoding="utf-8"))
        slot = next(mission for mission in frozen["missions"]
                    if mission["work_class"] == dogfood_improvement.WORK_CLASS)

        self.assertEqual(slot["project_id"], loaded.project_id)
        self.assertEqual(slot["baseline_sha"], BASELINE)
        for gate_id in loaded.gate_ids:
            self.assertIn(gate_id, slot["acceptance_gate_ids"])

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(dogfood_improvement.ObjectiveError):
            contract(schema_version="something.else.v1")

    def test_a_metric_nothing_reads_is_refused(self):
        body = json.loads(OBJECTIVE_PATH.read_text(encoding="utf-8"))
        body["measurement"]["readings"] = body["measurement"]["readings"][:1]

        with self.assertRaises(dogfood_improvement.ObjectiveError) as caught:
            dogfood_improvement.contract_from_payload(body)
        self.assertIn("unread", str(caught.exception))

    def test_a_reading_nothing_judges_is_refused(self):
        body = json.loads(OBJECTIVE_PATH.read_text(encoding="utf-8"))
        body["measurement"]["readings"].append(
            {"metric_id": "invented", "gate_id": "dev-test",
             "reader": "unittest_ran"})

        with self.assertRaises(dogfood_improvement.ObjectiveError) as caught:
            dogfood_improvement.contract_from_payload(body)
        self.assertIn("unjudged", str(caught.exception))

    def test_a_reader_this_module_does_not_implement_is_refused(self):
        body = json.loads(OBJECTIVE_PATH.read_text(encoding="utf-8"))
        body["measurement"]["readings"][0]["reader"] = "ask_the_provider"

        with self.assertRaises(dogfood_improvement.ObjectiveError):
            dogfood_improvement.contract_from_payload(body)

    def test_an_objective_that_names_no_environment_is_refused(self):
        with self.assertRaises(dogfood_improvement.ObjectiveError):
            contract(promotion={})

    def test_the_absent_objective_is_an_absence_and_not_a_crash(self):
        with self.assertRaises(dogfood_improvement.ObjectiveError):
            dogfood_improvement.load(ROOT / "contracts" / "not-a-file.json")

    def test_the_project_surfaces_widen_the_controller_surfaces(self):
        merged = dogfood_improvement.merged_surfaces(
            {"evaluator_independence": ("tests/test_authority_boundaries.py",)},
            contract().protected_surfaces)

        self.assertEqual(
            merged["evaluator_independence"],
            ("tests/test_authority_boundaries.py", "lab/evaluate.py", "fixtures/"))

    def test_a_retry_is_a_different_experiment_and_the_same_objective(self):
        """`experiment_reference` is derived, so attempt 2 needs its own ref."""

        loaded = contract()
        first = dogfood_improvement.attempt_objective(loaded, 1)
        second = dogfood_improvement.attempt_objective(loaded, 2)

        self.assertIs(first, loaded.objective)
        self.assertEqual(second.objective_ref, first.objective_ref + "#2")
        self.assertEqual(second.statement, first.statement)
        self.assertEqual(second.metrics, first.metrics)
        self.assertNotEqual(
            improvement.experiment_reference(second.objective_ref, 1, BASELINE),
            improvement.experiment_reference(first.objective_ref, 1, BASELINE))


class MeasurementReaderTests(unittest.TestCase):
    """Every metric comes out of a gate's own output, or comes out absent."""

    def setUp(self):
        self.contract = contract()

    def test_the_lab_baseline_reads_as_the_numbers_it_actually_prints(self):
        self.assertEqual(
            dogfood_improvement.measurements(self.contract, baseline_gates()),
            {"passing_tests": 2, "evaluate_correct": 5,
             "evaluate_false_matches": 0})

    def test_tests_that_ran_are_not_tests_that_passed(self):
        values = dogfood_improvement.measurements(
            self.contract, baseline_gates(tests=UNITTEST_FAILED))

        self.assertEqual(values["passing_tests"], "not_measurable")
        self.assertEqual(values["evaluate_correct"], 5)

    def test_a_gate_that_did_not_run_is_not_measurable(self):
        values = dogfood_improvement.measurements(self.contract, [gate("dev-check")])

        self.assertEqual(set(values.values()), {"not_measurable"})
        self.assertIn("not_measurable", improvement.CANONICAL_ABSENCE)

    def test_an_unparseable_evaluator_reading_is_never_a_zero(self):
        """A regression that read as zero would be recorded as an improvement."""

        for stdout in ("", "not_applicable", "{broken", "[]", "{}"):
            with self.subTest(stdout=stdout):
                values = dogfood_improvement.measurements(
                    self.contract, baseline_gates(evaluate=stdout))
                self.assertEqual(values["evaluate_false_matches"], "not_measurable")

    def test_a_truncated_document_is_absent_rather_than_half_read(self):
        values = dogfood_improvement.measurements(
            self.contract, baseline_gates(evaluate=EVALUATE_JSON[40:]))

        self.assertEqual(values["evaluate_correct"], "not_measurable")

    def test_a_boolean_is_not_a_measurement(self):
        body = json.loads(OBJECTIVE_PATH.read_text(encoding="utf-8"))
        for reading in body["measurement"]["readings"]:
            if reading["metric_id"] == "evaluate_correct":
                reading["field"] = "proceed"
        values = dogfood_improvement.measurements(
            dogfood_improvement.contract_from_payload(body), baseline_gates())

        self.assertEqual(values["evaluate_correct"], "not_measurable")

    def test_the_reader_uses_the_stream_each_tool_actually_writes_to(self):
        """`unittest` counts on stderr; the evaluator prints JSON on stdout."""

        swapped = [gate("dev-test", stdout=UNITTEST_OK),
                   gate("dev-evaluate", stderr=EVALUATE_JSON)]
        values = dogfood_improvement.measurements(self.contract, swapped)

        self.assertEqual(values["passing_tests"], 2)
        self.assertEqual(values["evaluate_correct"], "not_measurable")


class _LabExecution(LayerAdapter):
    """A layer that returns one lab's real candidate and its real gate output."""

    def __init__(self, *, candidate_sha, gate_outcomes, refuse=False, **kwargs):
        super().__init__(**kwargs)
        self.candidate_sha = candidate_sha
        self.gate_outcomes = gate_outcomes
        self.refuse = refuse

    def _dispatch(self, operation_key, value):
        if self.refuse:
            return {"status": "refused", "diagnostic": "EXECUTION_MODE_UNPROVEN",
                    "receipt": {"provider_profile": ALPHA,
                                "process_started": True}}
        result = super()._dispatch(operation_key, value)
        if result.get("status") == "completed":
            result["candidate_sha"] = self.candidate_sha
        return result

    def execute(self, step, operation_key, value):
        if step == "verify":
            return {"verified": True, "diagnostic": None}
        if step == "evaluate":
            return {"passed": all(item.get("passed")
                                  for item in self.gate_outcomes),
                    "target": "candidate", "target_sha": self.candidate_sha,
                    "gate_outcomes": self.gate_outcomes}
        return super().execute(step, operation_key, value)


class SeamCase(unittest.TestCase):
    """A real store, ledger, plane and Controller behind the seam."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MissionStore(str(Path(self.tmp.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)
        self.plane = improvement.ImprovementPlane(self.store, self.ledger)
        self.contract = contract()
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=PROJECT, repository=REPO, concurrency_cap=4,
            acceptance_gate_ids=tuple(GATES),
            acceptance_gate_source="%s@%s:dev" % (REPO, BASELINE),
            policy_version="run-1"))
        self.plane.set_policy(improvement.ImprovementPolicy(
            project_id=PROJECT, enabled=True, cooldown_seconds=0,
            protected_surfaces=dogfood_improvement.merged_surfaces(
                {name: ("protected/%s/" % name,)
                 for name in improvement.MANDATORY_SURFACES},
                self.contract.protected_surfaces),
            policy_version="run-1"))
        dogfood_improvement.ensure_environment(
            self.ledger, self.contract, repository=REPO, policy_version="run-1")
        self.controller = Controller(
            self.store, LayerAdapter(),
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=5)

    # -- helpers --------------------------------------------------------- #

    def open_slot(self, *, baseline=None, attempt=1):
        return dogfood_improvement.open_experiment(
            self.plane, self.contract, repository=REPO, baseline_sha=BASELINE,
            isolation_ref="lane://%s/DF-4#%d" % (PROJECT, attempt),
            baseline=(dogfood_improvement.measurements(
                self.contract, baseline_gates()) if baseline is None else baseline),
            attempt=attempt)

    def dispatch(self, experiment_ref, *, candidate=CANDIDATE, refuse=False,
                 outcomes=None):
        """Run the candidate mission to settlement through the real Controller.

        The gate evidence is what the execution layer returned, because that is
        what the post-change measurement is read from in the live seam -- a
        harness that wrote the evaluation step itself would be measuring its
        own fixture rather than the mission's own answer.
        """

        adapter = _LabExecution(
            candidate_sha=candidate,
            gate_outcomes=candidate_gates() if outcomes is None else outcomes,
            refuse=refuse)
        controller = Controller(
            self.store, adapter,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=5)
        mission, _ = self.plane.create_candidate_mission(
            experiment_ref, controller, acceptance_gate_ids=GATES,
            extra={"work_item_id": "DF-4", "project_id": PROJECT,
                   "execution_mode": "fixture", "baseline_sha": BASELINE,
                   "provider_candidates": [{"profile": ALPHA,
                                            "capabilities": ["prototype"]}]})
        while controller.work_once("worker-1") is not None:
            pass
        settled = self.store.get(mission["id"])
        evaluation = self.store.step_output(mission["id"], "evaluate") or {
            "passed": False, "gate_outcomes": []}
        return settled, evaluation

    def settle(self, experiment_ref, mission, evaluation, *,
               producer="provider:codex-primary", changed=("lab/prototype.py",
                                                           "tests/test_match.py")):
        return dogfood_improvement.settle(
            self.plane, self.ledger, self.contract,
            experiment_ref=experiment_ref, mission=mission,
            producer_identity=producer,
            evaluator_identity="factory-controller/dogfood-improvement",
            changed_paths=changed,
            candidate=dogfood_improvement.measurements(
                self.contract, evaluation["gate_outcomes"]),
            approval_ref="factory-owner-501-shift-1",
            release_policy_version="run-1",
            provenance_at="2026-09-01T00:00:00Z")


class OrderingTests(SeamCase):
    """The baseline is pinned before the candidate exists, or nothing runs."""

    def test_the_baseline_is_recorded_before_any_mission(self):
        row = self.open_slot()
        lineage = self.plane.lineage(row["experiment_ref"])

        self.assertEqual(lineage["state"], "baseline_measured")
        self.assertEqual(lineage["mission_ref"], "not_run")
        self.assertEqual(lineage["baseline"],
                         {"passing_tests": 2, "evaluate_correct": 5,
                          "evaluate_false_matches": 0})

    def test_a_candidate_cannot_be_created_without_one(self):
        self.plane.register_objective(self.contract.objective)
        row = self.plane.admit_experiment(
            self.contract.objective.objective_ref, "owner_objective",
            self.contract.objective.objective_ref, target_repository=REPO,
            baseline_sha=BASELINE, isolation_ref="lane://x/1")

        with self.assertRaises(improvement.ImprovementRefusal) as caught:
            self.plane.create_candidate_mission(
                row["experiment_ref"], self.controller, acceptance_gate_ids=GATES)
        self.assertEqual(caught.exception.code, "IMPROVEMENT_BASELINE_REQUIRED")

    def test_a_baseline_nothing_could_measure_refuses_the_experiment(self):
        """An unknown starting point cannot later become an improvement."""

        with self.assertRaises(improvement.ImprovementRefusal) as caught:
            self.open_slot(baseline={"passing_tests": "not_measurable",
                                     "evaluate_correct": 5,
                                     "evaluate_false_matches": 0})
        self.assertEqual(caught.exception.code,
                         "IMPROVEMENT_BASELINE_NOT_MEASURABLE")

    def test_the_baseline_cannot_be_re_measured_after_the_candidate_ran(self):
        row = self.open_slot()
        self.dispatch(row["experiment_ref"])

        with self.assertRaises(improvement.ImprovementRefusal) as caught:
            self.plane.record_baseline(row["experiment_ref"],
                                       {"passing_tests": 1,
                                        "evaluate_correct": 5,
                                        "evaluate_false_matches": 0})
        self.assertEqual(caught.exception.code, "IMPROVEMENT_BASELINE_SEALED")

    def test_a_replayed_slot_opens_one_experiment_and_one_mission(self):
        first = self.open_slot()
        mission, _ = self.dispatch(first["experiment_ref"])
        second = self.open_slot()

        self.assertEqual(second["experiment_ref"], first["experiment_ref"])
        again, created = self.plane.create_candidate_mission(
            first["experiment_ref"], self.controller, acceptance_gate_ids=GATES)
        self.assertFalse(created)
        self.assertEqual(again["id"], mission["id"])
        self.assertEqual(len(self.plane.experiments(PROJECT)), 1)

    def test_a_spent_attempt_is_abandoned_so_the_next_one_has_somewhere_to_go(self):
        first = self.open_slot()
        self.dispatch(first["experiment_ref"], refuse=True)

        abandoned = dogfood_improvement.abandon_spent(
            self.plane, self.contract, self.store)
        second = self.open_slot(attempt=2)

        self.assertEqual(abandoned, first["experiment_ref"])
        self.assertEqual(
            self.plane.lineage(first["experiment_ref"])["disposition"], "abandoned")
        self.assertNotEqual(second["experiment_ref"], first["experiment_ref"])
        self.assertEqual(second["state"], "baseline_measured")

    def test_a_completed_attempt_is_never_abandoned_by_the_retry_path(self):
        row = self.open_slot()
        self.dispatch(row["experiment_ref"])

        self.assertIsNone(dogfood_improvement.abandon_spent(
            self.plane, self.contract, self.store))


class ContainmentTests(SeamCase):
    """Each of DF-4's stop conditions, as a refusal that actually fires."""

    def test_a_candidate_that_touched_the_evaluator_is_never_promoted(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        outcome = self.settle(row["experiment_ref"], mission, evaluation,
                              changed=("lab/prototype.py", "lab/evaluate.py"))

        self.assertEqual(outcome["stopped_at"], "seal")
        self.assertEqual(outcome["refusal_code"],
                         "IMPROVEMENT_PROTECTED_SURFACE_TOUCHED")
        self.assertNotIn("deployment_id", outcome)
        self.assertEqual([event["kind"] for event in self.ledger.events(PROJECT)],
                         ["environment_registered"])

    def test_a_candidate_that_touched_the_fixture_is_never_promoted(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        outcome = self.settle(row["experiment_ref"], mission, evaluation,
                              changed=("fixtures/cases.json",))

        self.assertEqual(outcome["refusal_code"],
                         "IMPROVEMENT_PROTECTED_SURFACE_TOUCHED")

    def test_an_unknown_change_set_is_refused_rather_than_assumed_harmless(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        outcome = self.settle(row["experiment_ref"], mission, evaluation,
                              changed="unknown")

        self.assertEqual(outcome["changed_paths"], [])
        self.assertEqual(outcome["refusal_code"], "IMPROVEMENT_CHANGE_SET_UNKNOWN")

    def test_an_empty_candidate_cannot_be_sealed(self):
        """The first live DF-4 returned exactly this and called itself done."""

        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        outcome = self.settle(row["experiment_ref"], mission, evaluation,
                              changed=())

        self.assertEqual(outcome["refusal_code"], "IMPROVEMENT_CHANGE_SET_UNKNOWN")
        self.assertEqual(outcome["disposition"], "abandoned")

    def test_the_producer_cannot_also_be_the_judge(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        outcome = self.settle(
            row["experiment_ref"], mission, evaluation,
            producer="factory-controller/dogfood-improvement")

        self.assertEqual(outcome["stopped_at"], "evaluate")
        self.assertEqual(outcome["refusal_code"],
                         "IMPROVEMENT_EVALUATOR_NOT_INDEPENDENT")

    def test_a_candidate_whose_gates_failed_is_never_compared(self):
        row = self.open_slot()
        failing = candidate_gates()
        failing[2] = gate("dev-evaluate", stdout=EVALUATE_JSON, exit_code=1)
        mission, evaluation = self.dispatch(row["experiment_ref"],
                                            outcomes=failing)

        outcome = self.settle(row["experiment_ref"], mission, evaluation)

        self.assertEqual(outcome["refusal_code"],
                         "IMPROVEMENT_ACCEPTANCE_GATES_UNMET")

    def test_losing_the_decision_boundary_is_a_regression_not_a_trade(self):
        """More tests bought with a worse evaluator reading is still a loss."""

        row = self.open_slot()
        mission, evaluation = self.dispatch(
            row["experiment_ref"], outcomes=candidate_gates(ran=9, correct=4))

        outcome = self.settle(row["experiment_ref"], mission, evaluation)

        self.assertEqual(outcome["verdict"], "regressed")
        self.assertEqual(outcome["comparison"]["regressed"], ["evaluate_correct"])
        self.assertEqual(outcome["disposition"], "rejected")
        self.assertNotIn("deployment_id", outcome)

    def test_a_false_match_against_a_zero_baseline_is_a_regression(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(
            row["experiment_ref"], outcomes=candidate_gates(ran=9, false_matches=1))

        outcome = self.settle(row["experiment_ref"], mission, evaluation)

        self.assertEqual(outcome["verdict"], "regressed")
        self.assertEqual(outcome["comparison"]["regressed"],
                         ["evaluate_false_matches"])

    def test_a_candidate_that_added_no_test_is_not_an_improvement(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(
            row["experiment_ref"], outcomes=candidate_gates(ran=2))

        outcome = self.settle(row["experiment_ref"], mission, evaluation)

        self.assertEqual(outcome["verdict"], "not_improved")
        self.assertEqual(outcome["disposition"], "rejected")
        self.assertNotIn("deployment_id", outcome)

    def test_an_unmeasurable_candidate_is_never_read_as_improvement(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(
            row["experiment_ref"], outcomes=candidate_gates(ran=0))

        outcome = self.settle(row["experiment_ref"], mission, evaluation)

        self.assertEqual(outcome["verdict"], "not_measurable")
        self.assertEqual(outcome["comparison"]["unmeasured"], ["passing_tests"])
        self.assertNotIn("deployment_id", outcome)

    def test_a_gated_environment_has_no_autonomous_path(self):
        gated = production.EnvironmentPolicy(
            environment_id="factory-prototype-lab-prod", project_id=PROJECT,
            environment_class="production", repository=REPO,
            service_ref="lab", approver_refs=("owner",))
        self.ledger.register_environment(gated)
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])

        promoted = contract(promotion={
            "environment_id": "factory-prototype-lab-prod",
            "environment_class": "production", "service_ref": "lab",
            "approver_refs": ["owner"], "autonomous": False})
        outcome = dogfood_improvement.settle(
            self.plane, self.ledger, promoted,
            experiment_ref=row["experiment_ref"], mission=mission,
            producer_identity="provider:codex-primary",
            evaluator_identity="factory-controller/dogfood-improvement",
            changed_paths=("lab/prototype.py",),
            candidate=dogfood_improvement.measurements(
                self.contract, evaluation["gate_outcomes"]),
            approval_ref="factory-owner-501-shift-1",
            release_policy_version="run-1",
            provenance_at="2026-09-01T00:00:00Z")

        self.assertEqual(outcome["stopped_at"], "promote")
        self.assertEqual(outcome["refusal_code"],
                         "IMPROVEMENT_PRODUCTION_AUTHORITY_REQUIRED")


class PromotionTests(SeamCase):
    """The path that must work, and what it leaves behind when it does."""

    def promoted(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(row["experiment_ref"])
        return row["experiment_ref"], self.settle(
            row["experiment_ref"], mission, evaluation)

    def test_an_improved_candidate_is_staged_and_the_experiment_accepted(self):
        experiment_ref, outcome = self.promoted()

        self.assertEqual(outcome["verdict"], "improved")
        self.assertEqual(outcome["disposition"], "accepted")
        self.assertTrue(outcome["deployment_id"].startswith("dep_"))
        self.assertEqual(outcome["approval_ref"], "factory-owner-501-shift-1")
        lineage = self.plane.lineage(experiment_ref)
        self.assertEqual(lineage["promotion_environment_id"],
                         "factory-prototype-lab-staging")
        self.assertEqual(lineage["candidate_sha"], CANDIDATE)
        self.assertEqual(lineage["producer_identity"], "provider:codex-primary")
        self.assertEqual(lineage["evaluator_identity"],
                         "factory-controller/dogfood-improvement")

    def test_the_evidence_df4_asks_for_is_all_of_it_present(self):
        """DF-4's own `evidence_required`, item by item."""

        experiment_ref, outcome = self.promoted()
        lineage = self.plane.lineage(experiment_ref)

        self.assertEqual(lineage["baseline"]["passing_tests"], 2)
        self.assertEqual(lineage["candidate_measurements"]["passing_tests"], 4)
        self.assertEqual(
            sorted(lineage["baseline"]), sorted(lineage["candidate_measurements"]))
        self.assertEqual(outcome["deployment"]["state"], "approved")
        self.assertEqual(outcome["approval_ref"], "factory-owner-501-shift-1")

    def test_the_release_bundle_invents_no_artifact_identity(self):
        _, outcome = self.promoted()
        bundle = json.loads(
            self.ledger.deployment(outcome["deployment_id"])["bundle_json"])

        self.assertEqual(bundle["artifact"], "not_applicable")
        self.assertEqual(bundle["release_sha"], CANDIDATE)
        self.assertIn("not_applicable", bundle["migration"].values())

    def test_the_promotion_is_the_ledger_decision_and_carries_the_candidate(self):
        _, outcome = self.promoted()
        kinds = [event["kind"] for event in self.ledger.events(PROJECT)]

        self.assertIn("release_admitted", kinds)
        self.assertEqual(outcome["deployment"]["release_sha"], CANDIDATE)

    def test_rollback_returns_to_the_baseline_pinned_before_anything_ran(self):
        """Demote first, then delete the branch: the record and the code together."""

        experiment_ref, outcome = self.promoted()
        before = self.plane.lineage(experiment_ref)
        self.assertEqual(before["reverted_to"], "not_applicable")

        reverted = self.plane.revert(experiment_ref, reason="dogfood rollback drill")

        self.assertEqual(reverted["reverted_to"], BASELINE)
        self.assertEqual(reverted["rollback_target"], before["baseline_sha"])
        self.assertEqual(
            [event["kind"] for event in self.plane.events(PROJECT)][-1],
            "promotion_reverted")

    def test_nothing_can_be_reverted_that_was_never_promoted(self):
        row = self.open_slot()
        mission, evaluation = self.dispatch(
            row["experiment_ref"], outcomes=candidate_gates(ran=2))
        self.settle(row["experiment_ref"], mission, evaluation)

        with self.assertRaises(improvement.ImprovementRefusal) as caught:
            self.plane.revert(row["experiment_ref"], reason="nothing there")
        self.assertEqual(caught.exception.code, "IMPROVEMENT_NOTHING_PROMOTED")

    def test_settling_twice_stages_one_promotion(self):
        experiment_ref, first = self.promoted()
        mission = self.store.get(
            self.plane.lineage(experiment_ref)["mission_ref"])
        evaluation = self.store.step_output(mission["id"], "evaluate")

        second = self.settle(experiment_ref, mission, evaluation)

        self.assertEqual(second["refusal_code"], "IMPROVEMENT_EXPERIMENT_CLOSED")
        self.assertEqual(
            len([event for event in self.ledger.events(PROJECT)
                 if event["kind"] == "release_admitted"]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
