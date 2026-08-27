"""Where an acceptance gate identifier is allowed to come from.

SF-141 found the Stage-9 supervisor promoting repairs and experiments with a
literal ``["ACCEPTANCE"]``.  Nothing declares that gate.  The stage-1 adapter
runs the *declared* command for each declared gate and returns ``not_run`` when
there is none, ``not_run`` is a failure, so every unattended repair was on a
path to escalate for a reason that said nothing about the work -- and a
repository that happened to declare a gate by that name would have had it run
without ever admitting it.

The rule this file holds: a gate identifier for work nobody typed comes from the
project registry or the work is not promoted at all.  There is no default, no
fallback, and no literal anywhere in the package that can become one.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from factory_controller import maintenance, portfolio, safe_provider
from factory_controller import store as store_module
from factory_controller.cli import main as cli_main

from tests.test_stage9_supervisor import PROJECT, SupervisorCase

PACKAGE = Path(__file__).resolve().parent.parent / "factory_controller"

DECLARED = ("dev-test", "dev-evaluate")
SOURCE = "repo://shop@" + "a" * 40 + ":dev"


class DeclarationTests(unittest.TestCase):
    """A declaration with no provenance is the invented gate, spelled longer."""

    def test_gates_require_a_source_and_a_source_requires_gates(self):
        for gates, source in ((DECLARED, None), ((), SOURCE)):
            with self.assertRaises(portfolio.PolicyError):
                portfolio.ProjectPolicy("p", "r", acceptance_gate_ids=gates,
                                        acceptance_gate_source=source)

    def test_a_gate_is_a_non_empty_string_declared_once(self):
        for gates in (("a", "a"), ("",), ("  ",), (None,), (1,)):
            with self.assertRaises(portfolio.PolicyError):
                portfolio.ProjectPolicy("p", "r", acceptance_gate_ids=gates,
                                        acceptance_gate_source=SOURCE)

    def test_declaring_nothing_is_lawful_and_is_not_a_pass(self):
        """A project may decline to declare gates.  It then promotes nothing."""

        policy = portfolio.ProjectPolicy("p", "r")
        self.assertEqual(policy.acceptance_gate_ids, ())
        self.assertEqual(policy.as_row()["acceptance_gate_source"], "not_applicable")


class ResolutionTests(SupervisorCase):
    """The store owns the resolution, so every caller reads the same answer."""

    def declared_project(self, project_id=PROJECT, **extra):
        return self.project(project_id, acceptance_gate_ids=DECLARED,
                            acceptance_gate_source=SOURCE, **extra)

    def test_the_declared_gates_are_returned_with_their_provenance(self):
        self.declared_project()
        gates, source = self.store.declared_acceptance_gates(
            PROJECT, "repo://" + PROJECT)
        self.assertEqual((tuple(gates), source), (DECLARED, SOURCE))

    def test_an_undeclared_project_refuses_rather_than_defaulting(self):
        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        with self.assertRaises(portfolio.GateProvenanceError) as caught:
            self.store.declared_acceptance_gates(PROJECT)
        self.assertEqual(caught.exception.code, "ACCEPTANCE_GATES_UNDECLARED")
        self.assertEqual(caught.exception.detail["acceptance_gate_ids"],
                         "not_applicable")

    def test_an_unregistered_project_refuses(self):
        with self.assertRaises(portfolio.GateProvenanceError) as caught:
            self.store.declared_acceptance_gates("nobody")
        self.assertEqual(caught.exception.code,
                         "ACCEPTANCE_GATE_PROJECT_UNREGISTERED")

    def test_gates_declared_for_one_repository_do_not_govern_another(self):
        self.declared_project()
        with self.assertRaises(portfolio.GateProvenanceError) as caught:
            self.store.declared_acceptance_gates(PROJECT, "repo://somewhere-else")
        self.assertEqual(caught.exception.code,
                         "ACCEPTANCE_GATE_REPOSITORY_NOT_ADMITTED")
        self.assertEqual(caught.exception.detail["declared_for"], "repo://" + PROJECT)

    def test_the_absence_vocabulary_is_used_for_the_absent_source(self):
        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        row = self.store.project(PROJECT).as_row()
        self.assertIn(row["acceptance_gate_source"], store_module.CANONICAL_ABSENCE)


def promotion_refusals(report) -> list[str]:
    """Only the promotion pass.  `NO_RUNNABLE_MISSION` from the advance pass is
    an ordinary empty-queue fact and says nothing about gate provenance."""

    return [item["reason"] for item in report["refused"]
            if item["work_class"] in ("maintenance", "improvement")]


class PromotionTests(SupervisorCase):
    """What the supervisor does with the two answers."""

    def declared_project(self, project_id=PROJECT, **extra):
        return self.project(project_id, acceptance_gate_ids=DECLARED,
                            acceptance_gate_source=SOURCE, **extra)

    def test_a_promoted_repair_carries_the_declared_gates(self):
        self.declared_project()
        self.policy()
        self.repair()
        self.running()
        report = self.plane.cycle("w")
        self.assertEqual(len(report["promoted"]), 1)
        promoted = report["promoted"][0]
        self.assertEqual(promoted["acceptance_gate_ids"], list(DECLARED))
        self.assertEqual(promoted["acceptance_gate_source"], SOURCE)
        payload = self.store.get(promoted["mission_ref"])["payload"]
        self.assertEqual(payload["acceptance_gate_ids"], list(DECLARED))
        self.assertEqual(payload["acceptance_gate_source"], SOURCE)
        self.assertNotIn("ACCEPTANCE", payload["acceptance_gate_ids"])

    def test_a_promoted_experiment_carries_the_declared_gates(self):
        self.declared_project()
        self.policy()
        self.experiment()
        self.running()
        report = self.plane.cycle("w")
        promoted = [item for item in report["promoted"]
                    if item["work_class"] == "improvement"]
        self.assertEqual(len(promoted), 1)
        payload = self.store.get(promoted[0]["mission_ref"])["payload"]
        self.assertEqual(payload["acceptance_gate_ids"], list(DECLARED))

    def test_an_undeclared_project_promotes_nothing_and_says_why(self):
        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        self.policy()
        self.repair()
        self.experiment()
        self.running()
        report = self.plane.cycle("w")
        self.assertEqual(report["promoted"], [])
        self.assertEqual(promotion_refusals(report), ["ACCEPTANCE_GATES_UNDECLARED"] * 2)
        self.assertEqual(self.store.counts().get("admitted"), None)

    def test_provenance_is_refused_before_the_admission_ceiling(self):
        """The more fundamental condition decides.

        Whether the project declared gates is true independently of how many
        items this cycle already promoted, so a ceiling of zero must not be the
        reason a caller is told about.
        """

        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        self.policy(maintenance_admissions=0)
        self.repair()
        self.running()
        report = self.plane.cycle("w")
        self.assertEqual(promotion_refusals(report), ["ACCEPTANCE_GATES_UNDECLARED"])

    def test_a_repair_targeting_another_repository_is_refused(self):
        self.declared_project()
        self.policy()
        self.repair(repository="repo://not-this-one")
        self.running()
        report = self.plane.cycle("w")
        self.assertEqual(report["promoted"], [])
        self.assertEqual(promotion_refusals(report),
                         ["ACCEPTANCE_GATE_REPOSITORY_NOT_ADMITTED"])

    def test_the_admitted_mission_keeps_its_gates_when_the_registry_moves(self):
        """Immutable for the admitted mission, which is the point of a gate."""

        self.declared_project()
        self.policy()
        self.repair()
        self.running()
        mission_ref = self.plane.cycle("w")["promoted"][0]["mission_ref"]
        self.project(acceptance_gate_ids=("something-else",),
                     acceptance_gate_source="repo://shop@" + "b" * 40 + ":dev")
        self.assertEqual(
            self.store.get(mission_ref)["payload"]["acceptance_gate_ids"],
            list(DECLARED))


class OperatorSurfaceTests(SupervisorCase):
    """The CLI reads the same declaration, and labels a typed gate as typed."""

    def cli(self, *argv):
        return cli_main(["--db", str(self.path), *argv])

    def test_the_cli_refuses_a_repair_with_no_declared_gate(self):
        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        trigger = self.repair()
        self.assertEqual(self.cli("maintenance", "repair", "--trigger", trigger), 2)

    def test_a_typed_gate_is_recorded_as_the_operator_s_own(self):
        self.project(acceptance_gate_ids=(), acceptance_gate_source=None)
        trigger = self.repair()
        self.assertEqual(
            self.cli("maintenance", "repair", "--trigger", trigger,
                     "--gate", "dev-test"), 0)
        plane = maintenance.MaintenancePlane(self.store, self.ledger)
        mission = self.store.get(plane.lineage(trigger)["mission_ref"])
        self.assertEqual(mission["payload"]["acceptance_gate_ids"], ["dev-test"])
        self.assertEqual(mission["payload"]["acceptance_gate_source"], "operator")


class HarnessEvaluatorTests(unittest.TestCase):
    """The local fixture evaluator used to invent a gate too, and pass it."""

    def test_an_undeclared_gate_list_is_a_failure_not_a_pass(self):
        result = _evaluate(None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["diagnostic"], "ACCEPTANCE_GATE_UNDECLARED")
        self.assertEqual(result["gate_outcomes"], [])

    def test_exactly_the_declared_gates_are_reported(self):
        result = _evaluate(["a", "b"])
        self.assertTrue(result["passed"])
        self.assertEqual([item["gate_id"] for item in result["gate_outcomes"]],
                         ["a", "b"])


def _evaluate(declared):
    """Run the harness evaluator step in-process over one mission payload."""

    import io
    import contextlib
    mission = {} if declared is None else {"acceptance_gate_ids": declared}
    request = {"step": "evaluate", "operation_key": "k",
               "input": {"mission": mission}}
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        safe_provider.main_with(request)
    return json.loads(buffer.getvalue())


class NoLiteralGateTests(unittest.TestCase):
    """Nothing in the package may hand a gate list it wrote itself.

    A structural check rather than a grep for the word ``ACCEPTANCE``: the
    defect was a literal *list* passed as ``acceptance_gate_ids``, and any other
    literal would be the same defect under a different name.
    """

    @staticmethod
    def literal_gate_arguments(text: str) -> list[str]:
        found = []
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "acceptance_gate_ids":
                    continue
                if isinstance(keyword.value, (ast.List, ast.Tuple)) and all(
                        isinstance(item, ast.Constant) for item in keyword.value.elts):
                    found.append(ast.dump(keyword.value))
        return found

    def test_no_module_passes_a_literal_gate_list(self):
        for path in sorted(PACKAGE.glob("*.py")):
            self.assertEqual(self.literal_gate_arguments(path.read_text()), [],
                             "%s invents an acceptance gate" % path.name)

    def test_the_scan_would_actually_catch_the_defect_it_replaced(self):
        planted = 'create_repair_mission(ref, c, acceptance_gate_ids=["ACCEPTANCE"])'
        self.assertTrue(self.literal_gate_arguments(planted))
        allowed = "create_repair_mission(ref, c, acceptance_gate_ids=gates)"
        self.assertFalse(self.literal_gate_arguments(allowed))


if __name__ == "__main__":
    unittest.main()
