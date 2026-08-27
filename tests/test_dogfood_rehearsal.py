"""The rehearsal, run.

A harness that is never executed in the suite is a harness nobody knows works,
and the corpus has recorded that shape twice under a different name: a green
runner standing in for the thing it was meant to prove.  So this file runs the
whole rehearsal -- eleven scenarios, each in its own store, each dispatching to
a real provider process -- and asserts every one is proven rather than trusting
the summary line.

It also holds the two properties that make it a rehearsal rather than a run:
it terminates by construction, and it cannot reach production authority.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from factory_controller import rehearsal

MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "rehearsal.py"


class RehearsalRunTests(unittest.TestCase):
    """One run, shared across the assertions: it spawns real processes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.result = rehearsal.run(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scenario(self, name):
        return next(row for row in self.result["scenarios"]
                    if row["scenario"] == name)

    def test_every_scenario_is_proven(self):
        self.assertEqual(self.result["failed"], [])
        self.assertEqual(self.result["outcome"], "REHEARSED")
        self.assertEqual(len(self.result["proven"]), len(rehearsal.SCENARIOS))

    def test_the_scenarios_cover_what_the_task_named(self):
        covered = {row["scenario"] for row in self.result["scenarios"]}
        self.assertEqual(covered, {
            "normal_backlog", "maintenance_repair", "improvement_experiment",
            "owner_gated_promotion", "restart_recovery", "pause_and_drain",
            "provider_outage", "budget_refusal", "acceptance_gate_failure",
            "rollback_recovery", "emergency_stop"})

    def test_a_repair_ran_the_repository_s_own_declared_gates(self):
        row = self.scenario("maintenance_repair")
        self.assertEqual(row["acceptance_gate_ids"],
                         ["dev-check", "dev-test", "dev-reproduce"])
        self.assertIn("factory-bug-lab", row["acceptance_gate_source"])
        self.assertIn(rehearsal.BASELINE["factory-bug-lab"],
                      row["acceptance_gate_source"])

    def test_the_outage_was_declined_before_a_process_started(self):
        """A proof, not a claim: the profile is checked before anything spawns.

        The profiles are named neutrally because the Controller never learns a
        vendor's name; the bridge owns the real profile ids.
        """

        row = self.scenario("provider_outage")
        self.assertEqual(row["process_started"], [False, True])
        self.assertEqual(row["profiles"],
                         [rehearsal.PRIMARY, rehearsal.SECONDARY])

    def test_a_failing_gate_escalates_and_names_the_gate(self):
        row = self.scenario("acceptance_gate_failure")
        self.assertEqual(row["mission_state"], "escalated")
        self.assertEqual(row["failed_gates"], ["dev-test"])
        self.assertIn("dev-test", row["terminal_reason"])

    def test_an_abandoned_cycle_recovers_without_a_second_dispatch(self):
        row = self.scenario("restart_recovery")
        self.assertEqual(set(row["provider_legs"].values()), {1})
        self.assertEqual([entry["outcome"] for entry in row["recovered"]],
                         ["recovered_replayable"])

    def test_pause_and_emergency_stop_advance_nothing(self):
        self.assertEqual(self.scenario("pause_and_drain")["advanced_while_paused"], 0)
        self.assertEqual(self.scenario("emergency_stop")["advanced_while_stopped"], 0)

    def test_the_candidate_stopped_at_the_owner(self):
        self.assertEqual(self.scenario("owner_gated_promotion")["refusal_code"],
                         "IMPROVEMENT_NOT_DEMONSTRATED")

    def test_every_receipt_says_fixture(self):
        """The rehearsal is real processes and a fixture provider, and says so."""

        self.assertEqual(self.result["execution_mode"], "fixture")
        for row in self.result["scenarios"]:
            self.assertEqual(row["execution_mode"], "fixture")
            self.assertIn("factory_controller.safe_provider", row["adapter"])


class TerminationTests(unittest.TestCase):
    """Checked structurally: the test that waits for a runaway to stop hangs."""

    SOURCE = MODULE.read_text()

    def test_the_harness_never_waits(self):
        code = [node for node in ast.walk(ast.parse(self.SOURCE))
                if isinstance(node, ast.Attribute) and node.attr == "sleep"]
        self.assertEqual(code, [])

    def test_no_loop_runs_on_a_constant(self):
        for node in ast.walk(ast.parse(self.SOURCE)):
            if isinstance(node, ast.While):
                self.assertNotIsInstance(node.test, ast.Constant)

    def test_the_cycle_ceiling_cannot_be_raised_by_a_caller(self):
        """`cycles` clamps to the module constant whatever it is handed."""

        source = ast.parse(self.SOURCE)
        cycles = next(node for node in ast.walk(source)
                      if isinstance(node, ast.FunctionDef) and node.name == "cycles")
        self.assertIn("CYCLE_CEILING", ast.dump(cycles))

    def test_no_scenario_reaches_a_production_deployment_verb(self):
        """The one deployment in the rehearsal is a staging environment.

        `rollback_recovery` deploys, which is the point of it -- so the check
        that matters is that no environment it builds is a production one.
        """

        self.assertNotIn('"production"', self.SOURCE)
        self.assertIn('environment_class="staging"', self.SOURCE)


class NeutralityRegressionTests(unittest.TestCase):
    """The advisory service and the metered gateway are optional, and absent.

    Scope 11 asks for a regression rather than an assertion: the whole dogfood
    path must work with both switched off.  It does, and the reason is
    structural -- neither reaches this path at all, so there is no configuration
    under which one of them becomes required.
    """

    PACKAGE = MODULE.parent

    def imported(self, name):
        tree = ast.parse((self.PACKAGE / name).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is None:
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name.split(".")[-1] for alias in node.names)
        return names

    def test_the_dogfood_path_imports_neither_seam(self):
        for module in ("rehearsal.py", "dogfood.py", "activation.py"):
            imported = self.imported(module)
            self.assertNotIn("advisor", imported, module)
            self.assertNotIn("gateway", imported, module)

    def test_no_vendor_name_reaches_the_run_contract_or_the_preflight(self):
        """A provider profile is an opaque string on both sides of the seam."""

        from factory_controller import dogfood
        source = (self.PACKAGE / "dogfood.py").read_text().lower()
        for token in ("anthropic", "openai", "claude", "codex", "cursor",
                      "gemini", "hermes", "openrouter"):
            self.assertNotIn(token, source, token)
        self.assertNotIn("hermes", str(dogfood.CONTRACT_SCHEMA).lower())

    def test_the_rehearsal_runs_with_no_advisor_and_no_gateway_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = rehearsal.run(tmp, only=("normal_backlog",
                                              "maintenance_repair"))
        self.assertEqual(result["failed"], [])

    def test_no_evidence_artifact_carries_a_secret_shaped_value(self):
        """A leak scan over what this task actually wrote to disk."""

        import re
        evidence = MODULE.parent.parent / "evidence" / "SF-142"
        patterns = (re.compile(r"sk-[A-Za-z0-9]{16,}"),
                    re.compile(r"(?i)(api[_-]?key|password|bearer)\s*[\"':=]\s*\S{8,}"),
                    re.compile(r"gh[pous]_[A-Za-z0-9]{20,}"))
        for path in sorted(evidence.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
            for pattern in patterns:
                self.assertIsNone(pattern.search(text),
                                  "%s carries a secret-shaped value" % path.name)


class OperatorSurfaceTests(unittest.TestCase):
    def test_a_rehearsal_without_a_root_is_refused(self):
        from factory_controller.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                main(["--db", str(Path(tmp) / "c.db"), "dogfood", "rehearse"]), 2)

    def test_one_named_scenario_runs_alone(self):
        from factory_controller.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main([
                "--db", str(Path(tmp) / "c.db"), "dogfood", "rehearse",
                "--root", str(Path(tmp) / "run"),
                "--scenario", "budget_refusal"]), 0)


if __name__ == "__main__":
    unittest.main()
