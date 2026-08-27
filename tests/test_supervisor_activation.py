"""Installing the Stage-9 host service, and the step this package will not take.

SF-141 delivered a supervisor that *can* run and is not running.  The gap
between those two was one Owner act with no package around it: no job
definition, no receipt, no drift report, no way back.  This file holds the
package and, more importantly, its edges.

The three properties, each an absence:

* **nothing here loads a service** -- no verb, and no name of the loader
  anywhere in the module, so it is missing rather than refused;
* **nothing writes by accident** -- an import, a test, a plan, a doctor and an
  uninstall without ``--apply`` all leave the host exactly as they found it;
* **an apply is gated on a durable Owner approval**, which nothing in this
  package can produce.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import activation, supervisor

from tests.test_stage9_supervisor import SupervisorCase

MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "activation.py"

INVOCATION = ("/usr/bin/python3", "-m", "factory_controller.cli",
              "--db", "/tmp/controller.db", "supervisor", "cycle")


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agents = str(Path(self.tmp.name) / "LaunchAgents")
        self.state = str(Path(self.tmp.name) / "state")

    def plan(self, **overrides):
        values = {"label": activation.DEFAULT_LABEL, "invocation": INVOCATION,
                  "interval_seconds": 300, "agents_dir": self.agents,
                  "state_dir": self.state, "working_dir": self.tmp.name}
        values.update(overrides)
        return activation.ServicePlan(**values)


class PlanTests(PlanCase):
    def test_a_relative_location_is_refused(self):
        with self.assertRaises(activation.ActivationError):
            self.plan(agents_dir="LaunchAgents")

    def test_an_interval_below_the_floor_is_refused(self):
        """Not a preference: a too-short interval hides real refusals.

        An overlapping cycle refuses rather than corrupting anything, so the
        damage from a one-second interval is a stream of recorded overlap
        refusals that buries the ones that mean something.
        """

        for seconds in (0, 30, 59, 86401):
            with self.assertRaises(activation.ActivationError):
                self.plan(interval_seconds=seconds)

    def test_a_label_with_a_path_separator_is_refused(self):
        with self.assertRaises(activation.ActivationError):
            self.plan(label="com.softwarefactory/supervisor")

    def test_the_digest_moves_with_every_field_that_reaches_the_host(self):
        base = self.plan().digest
        self.assertNotEqual(base, self.plan(interval_seconds=600).digest)
        self.assertNotEqual(base, self.plan(label="com.other.supervisor").digest)
        self.assertNotEqual(
            base, self.plan(invocation=INVOCATION[:-1] + ("cycle", "-x")).digest)
        self.assertEqual(base, self.plan().digest)

    def test_the_plan_is_built_from_the_supervisor_s_own_contract(self):
        """One owner for the invocation, so a job cannot name a command that
        does not exist."""

        contract = {"schedule": {"invocation": list(INVOCATION),
                                 "interval_seconds": 900}}
        plan = activation.from_contract(contract, agents_dir=self.agents,
                                        state_dir=self.state,
                                        working_dir=self.tmp.name)
        self.assertEqual(plan.invocation, INVOCATION)
        self.assertEqual(plan.interval_seconds, 900)

    def test_a_contract_with_no_invocation_is_refused(self):
        with self.assertRaises(activation.ActivationError):
            activation.from_contract({}, agents_dir=self.agents,
                                     state_dir=self.state,
                                     working_dir=self.tmp.name)


class DefinitionTests(PlanCase):
    def test_the_job_is_an_interval_and_never_a_running_process(self):
        body = activation.definition(self.plan())
        self.assertIn("<key>StartInterval</key><integer>300</integer>", body)
        self.assertIn("<key>RunAtLoad</key><false/>", body)
        self.assertNotIn("KeepAlive", body)

    def test_the_definition_is_byte_identical_for_one_plan(self):
        self.assertEqual(activation.definition(self.plan()),
                         activation.definition(self.plan()))

    def test_markup_in_a_path_cannot_break_out_of_the_definition(self):
        body = activation.definition(self.plan(working_dir="/tmp/a<b&c"))
        self.assertIn("/tmp/a&lt;b&amp;c", body)
        self.assertNotIn("/tmp/a<b&c", body)


class WriteTests(PlanCase):
    def test_a_plan_writes_nothing(self):
        result = activation.install(self.plan())
        self.assertFalse(result["applied"])
        self.assertEqual(result["outcome"], "planned")
        self.assertFalse(Path(self.agents).exists())
        self.assertFalse(Path(self.state).exists())

    def test_an_apply_writes_the_definition_and_a_receipt(self):
        result = activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        self.assertTrue(result["applied"])
        self.assertEqual(result["outcome"], "installed")
        self.assertTrue(Path(result["definition_path"]).is_file())
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual(receipt["plan_digest"], self.plan().digest)

    def test_installing_the_same_plan_twice_writes_once(self):
        first = activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        second = activation.install(self.plan(), apply=True, clock=lambda: 2.0)
        self.assertEqual(second["outcome"], "unchanged")
        self.assertFalse(second["applied"])
        receipt = json.loads(Path(first["receipt_path"]).read_text())
        self.assertEqual(receipt["installed_at"], 1.0)

    def test_a_changed_plan_reinstalls_and_reports_the_previous_digest(self):
        activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        result = activation.install(self.plan(interval_seconds=600), apply=True,
                                    clock=lambda: 2.0)
        self.assertEqual(result["outcome"], "reinstalled")
        self.assertEqual(result["previous_digest"], self.plan().digest)

    def test_uninstall_removes_exactly_what_the_receipt_records(self):
        installed = activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        planned = activation.uninstall(self.plan())
        self.assertFalse(planned["applied"])
        self.assertTrue(Path(installed["definition_path"]).is_file())
        removed = activation.uninstall(self.plan(), apply=True)
        self.assertEqual(sorted(removed["removed"]),
                         sorted([installed["definition_path"],
                                 installed["receipt_path"]]))
        self.assertFalse(Path(installed["definition_path"]).exists())

    def test_uninstall_says_plainly_that_it_unloads_nothing(self):
        self.assertIn("bootout", activation.uninstall(self.plan())["deactivate"])


class DoctorTests(PlanCase):
    def test_an_uninstalled_host_reports_absence_in_the_shared_vocabulary(self):
        report = activation.doctor(self.plan())
        self.assertFalse(report["definition_present"])
        self.assertEqual(report["installed_digest"], "not_run")
        self.assertEqual(report["drift"], "not_applicable")

    def test_a_matching_install_reports_no_drift(self):
        activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        self.assertEqual(activation.doctor(self.plan())["drift"], "none")

    def test_a_plan_that_moved_since_the_install_is_drift(self):
        activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        report = activation.doctor(self.plan(interval_seconds=600))
        self.assertIn("differs from the current plan", report["drift"])

    def test_a_receipt_whose_definition_was_deleted_is_drift(self):
        installed = activation.install(self.plan(), apply=True, clock=lambda: 1.0)
        Path(installed["definition_path"]).unlink()
        self.assertIn("absent", activation.doctor(self.plan())["drift"])

    def test_whether_the_host_holds_the_job_is_unknown_and_not_guessed(self):
        """Reporting false because no file said otherwise is fabricated readiness."""

        report = activation.doctor(self.plan())
        self.assertEqual(report["service_loaded"], "unknown")
        self.assertIn(report["service_loaded"], supervisor.CANONICAL_ABSENCE)


class ApprovalTests(PlanCase):
    def record(self, **overrides):
        body = {"schema_version": activation.APPROVAL_SCHEMA,
                "label": activation.DEFAULT_LABEL, "approved": True,
                "approved_by": "owner", "approval_ref": "notion://SF-142"}
        body.update(overrides)
        path = Path(self.tmp.name) / "approval.json"
        path.write_text(json.dumps(body))
        return str(path)

    def test_no_record_named_is_not_run_and_not_a_refusal(self):
        self.assertEqual(activation.approval(None)["state"], "not_run")

    def test_an_absent_record_is_not_run(self):
        result = activation.approval(str(Path(self.tmp.name) / "absent.json"))
        self.assertEqual(result["state"], "not_run")
        self.assertFalse(result["approved"])

    def test_a_complete_record_grants(self):
        result = activation.approval(self.record())
        self.assertEqual(result["state"], "granted")
        self.assertTrue(result["approved"])

    def test_a_record_for_another_label_does_not_grant_this_one(self):
        result = activation.approval(self.record(label="com.other.thing"))
        self.assertEqual(result["state"], "not_applicable")
        self.assertFalse(result["approved"])

    def test_a_record_that_names_nobody_does_not_grant(self):
        for overrides in ({"approved_by": ""}, {"approval_ref": ""},
                          {"approved": "yes"}, {"schema_version": "v9"}):
            self.assertFalse(activation.approval(self.record(**overrides))
                             ["approved"], overrides)

    def test_every_absence_uses_the_canonical_vocabulary(self):
        states = {activation.approval(None)["state"],
                  activation.approval(self.record(label="x"))["state"],
                  activation.approval(self.record(approved_by=""))["state"]}
        self.assertTrue(states <= supervisor.CANONICAL_ABSENCE, states)


class AbsentVerbTests(unittest.TestCase):
    """The loader is missing from this module, not refused by it."""

    SOURCE = MODULE.read_text()

    def test_the_module_names_no_loader_call_of_its_own(self):
        """The one place the loader's name may appear is the text handed back
        to the Owner, and it is a value, never a call."""

        tree = ast.parse(self.SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                self.assertNotIn("subprocess", names)
                self.assertNotIn("os", [getattr(node, "module", None), *names])

    def test_the_module_starts_nothing(self):
        for token in ("Popen", "run(", "system(", "execv", "spawn"):
            self.assertNotIn(token, self.SOURCE, token)

    def test_the_activation_step_is_returned_as_data_and_marked_not_taken(self):
        plan = activation.ServicePlan(
            label=activation.DEFAULT_LABEL, invocation=INVOCATION,
            interval_seconds=300, agents_dir="/tmp/a", state_dir="/tmp/b",
            working_dir="/tmp/c")
        step = activation.activation_command(plan)
        self.assertFalse(step["performed_here"])
        self.assertEqual(step["state"], "not_run")
        self.assertTrue(step["activate"].startswith("launchctl bootstrap"))
        self.assertEqual(step["verify"][2].split()[-4:],
                         ["supervisor", "cycles", "--limit", "5"])


class OperatorSurfaceTests(SupervisorCase):
    """The Owner gate, where it can be checked rather than promised."""

    def setUp(self):
        super().setUp()
        self.agents = str(Path(self.tmp.name) / "LaunchAgents")
        self.state = str(Path(self.tmp.name) / "state")

    def cli(self, *argv):
        from factory_controller.cli import main
        return main(["--db", str(self.path), "supervisor", *argv,
                     "--agents-dir", self.agents, "--state-dir", self.state,
                     "--working-dir", self.tmp.name])

    def test_an_apply_without_an_approval_is_refused_and_writes_nothing(self):
        self.assertEqual(self.cli("service-install", "--apply"), 2)
        self.assertFalse(Path(self.agents).exists())

    def test_a_plan_needs_no_approval_and_still_writes_nothing(self):
        self.assertEqual(self.cli("service-plan"), 0)
        self.assertFalse(Path(self.agents).exists())

    def test_an_approved_apply_writes_the_definition_and_loads_nothing(self):
        record = Path(self.tmp.name) / "approval.json"
        record.write_text(json.dumps({
            "schema_version": activation.APPROVAL_SCHEMA,
            "label": activation.DEFAULT_LABEL, "approved": True,
            "approved_by": "owner", "approval_ref": "notion://SF-142"}))
        self.assertEqual(
            self.cli("service-install", "--apply", "--approval", str(record)), 0)
        definition = Path(self.agents) / (activation.DEFAULT_LABEL + ".plist")
        self.assertTrue(definition.is_file())
        self.assertEqual(self.cli("service-doctor",
                                  "--approval", str(record)), 0)

    def test_the_doctor_runs_on_a_host_with_nothing_installed(self):
        self.assertEqual(self.cli("service-doctor"), 0)


if __name__ == "__main__":
    unittest.main()
