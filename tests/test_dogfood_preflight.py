"""The run contract, the preflight that reads it, and the gate at the far end.

The property this file exists to hold is one sentence: **an unmeasured
prerequisite is never a pass.**  The corpus has recorded that failure three
times, twice inside documents that certified a gate with every box ticked for
things that had not run.  So every check here is asked twice -- once where the
fact is present and once where it is absent -- and the absent case must come
back ``unknown`` rather than ``met``.

The second property is that the preflight *reads*.  It registers nothing,
installs nothing, promotes nothing and starts nothing; a run of it against a
store leaves that store byte-identical.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from factory_controller import dogfood, portfolio, supervisor

from tests.test_stage9_supervisor import SupervisorCase

CONTRACT_PATH = (Path(__file__).resolve().parent.parent / "contracts"
                 / "internal-dogfood-run-contract.json")

BRIDGE_DOCTOR = {
    "registry_drift": "none",
    "source": {"installed_sha": "a" * 40, "sha": "a" * 40},
    "capabilities": ["prototype"],
    "capability_admissions": {"serving": ["bug", "prototype"]},
    "provider": {"profiles": [
        {"profile_id": "codex-primary", "status": "available",
         "readiness": "available", "readiness_detail": "readiness probe exited 0"},
        {"profile_id": "claude-secondary", "status": "available",
         "readiness": "not_probed", "readiness_detail": "no probe"},
        {"profile_id": "cursor-secondary", "status": "available",
         "readiness": "unavailable", "readiness_detail": "readiness probe exited 1"},
    ]},
}

HEALTHY = {"evidence_core": {"status": "ACCEPTED", "identity": "e" * 40},
           "context_broker": {"status": "ok", "identity": "c" * 40}}


def payload(**overrides) -> dict:
    body = json.loads(CONTRACT_PATH.read_text())
    body.update(overrides)
    return body


class ContractTests(unittest.TestCase):
    def test_the_shipped_contract_loads(self):
        contract = dogfood.load_contract(str(CONTRACT_PATH))
        self.assertEqual(contract.projects,
                         ("factory-prototype-lab", "factory-bug-lab"))
        self.assertEqual(contract.environment_classes, ("local-sim", "staging"))

    def test_a_run_contract_cannot_name_production(self):
        """A second place the Stage-6 approval decision could live."""

        with self.assertRaises(dogfood.ContractError) as caught:
            dogfood.contract_from_payload(
                payload(environment_classes=["staging", "production"]))
        self.assertIn("production", str(caught.exception))

    def test_a_budget_without_a_currency_is_refused(self):
        with self.assertRaises(dogfood.ContractError):
            dogfood.contract_from_payload(payload(budget_currency=""))

    def test_half_a_window_is_refused(self):
        with self.assertRaises(dogfood.ContractError):
            dogfood.contract_from_payload(payload(window_start_hour=9))

    def test_a_contract_without_its_gate_is_refused(self):
        with self.assertRaises(dogfood.ContractError):
            dogfood.contract_from_payload(payload(productization_gate={}))

    def test_the_schema_is_pinned(self):
        with self.assertRaises(dogfood.ContractError):
            dogfood.contract_from_payload(payload(schema_version="v9"))


class PreflightCase(SupervisorCase):
    def setUp(self):
        super().setUp()
        self.contract = dogfood.load_contract(str(CONTRACT_PATH))

    def provision(self, *, gates=True, budgets=True, policies=True):
        sources = {
            "factory-prototype-lab": ("dev-check", "dev-test", "dev-evaluate"),
            "factory-bug-lab": ("dev-check", "dev-test", "dev-reproduce"),
        }
        for name, declared in sources.items():
            self.store.register_project(portfolio.ProjectPolicy(
                project_id=name, repository="https://example.invalid/%s.git" % name,
                concurrency_cap=2,
                budget_ceiling=10.0 if budgets else None,
                budget_currency="USD" if budgets else None,
                acceptance_gate_ids=declared if gates else (),
                acceptance_gate_source=("%s@baseline:dev" % name) if gates else None,
                policy_version="dogfood-1"))
            if policies:
                self.plane.set_policy(supervisor.SupervisorPolicy(
                    project_id=name, work_classes=("backlog", "maintenance",
                                                   "improvement"),
                    policy_version="dogfood-1"))

    def run_preflight(self, *, reports=None, service=None):
        return dogfood.preflight(self.contract, store=self.store,
                                 supervisor_plane=self.plane,
                                 reports=reports, service_doctor=service).as_row()

    def state(self, row, check):
        return next(item["state"] for item in row["checks"]
                    if item["check"] == check)


class PreflightTests(PreflightCase):
    def test_a_fully_provisioned_host_reduces_to_the_owner_acts(self):
        self.provision()
        row = self.run_preflight(
            reports={"bridge_doctor": BRIDGE_DOCTOR, **HEALTHY},
            service={"definition_present": True, "drift": "none",
                     "service_loaded": "unknown"})
        self.assertTrue(row["ready"], row["unmet"])

    def test_an_empty_host_is_not_ready_and_names_every_gap(self):
        row = self.run_preflight()
        self.assertFalse(row["ready"])
        self.assertEqual(self.state(row, "PROJECTS_REGISTERED"), dogfood.UNMET)

    def test_an_absent_report_is_unknown_and_never_met(self):
        """The whole point.  Nothing supplied must not read as nothing wrong."""

        self.provision()
        row = self.run_preflight(service={"definition_present": True,
                                          "drift": "none"})
        for check in ("BRIDGE_NO_DRIFT", "PROVIDER_CAPABILITIES_ADMITTED",
                      "EVIDENCE_CORE_HEALTH", "CONTEXT_BROKER_HEALTH"):
            self.assertEqual(self.state(row, check), dogfood.UNKNOWN, check)
        self.assertFalse(row["ready"])

    def test_a_check_that_cannot_be_read_is_unknown_not_vacuously_met(self):
        """An unregistered project makes the budget question unanswerable.

        The first draft of this preflight reported PROJECT_BUDGETS as met on an
        empty store, because no registered project failed the test -- which is
        the `[x]`-for-`not_run` shape one layer down.
        """

        row = self.run_preflight()
        for check in ("PROJECT_ISOLATION", "PROJECT_BUDGETS",
                      "BUDGETS_WITHIN_RUN_CEILING",
                      "WORK_CLASSES_WITHIN_CONTRACT"):
            self.assertEqual(self.state(row, check), dogfood.UNKNOWN, check)

    def test_undeclared_acceptance_gates_are_an_unmet_prerequisite(self):
        self.provision(gates=False)
        row = self.run_preflight()
        self.assertEqual(self.state(row, "ACCEPTANCE_GATES_DECLARED"), dogfood.UNMET)

    def test_declared_gates_are_reported_with_their_provenance(self):
        self.provision()
        row = self.run_preflight()
        declared = next(item["declared"] for item in row["checks"]
                        if item["check"] == "ACCEPTANCE_GATES_DECLARED")
        self.assertEqual(declared["factory-bug-lab"]["acceptance_gate_ids"],
                         ["dev-check", "dev-test", "dev-reproduce"])

    def test_a_missing_budget_is_unmet_once_the_project_exists(self):
        self.provision(budgets=False)
        row = self.run_preflight()
        self.assertEqual(self.state(row, "PROJECT_BUDGETS"), dogfood.UNMET)

    def test_an_engaged_emergency_stop_is_unmet(self):
        self.provision()
        self.store.emergency_stop(True)
        row = self.run_preflight()
        self.assertEqual(self.state(row, "EMERGENCY_STOP_CLEAR"), dogfood.UNMET)

    def test_an_unadmitted_capability_is_unmet(self):
        self.provision()
        doctor = {**BRIDGE_DOCTOR,
                  "capability_admissions": {"serving": ["prototype"]}}
        row = self.run_preflight(reports={"bridge_doctor": doctor, **HEALTHY})
        self.assertEqual(self.state(row, "PROVIDER_CAPABILITIES_ADMITTED"),
                         dogfood.UNMET)

    def test_bridge_drift_is_unmet(self):
        self.provision()
        doctor = {**BRIDGE_DOCTOR, "registry_drift": "installed digest differs"}
        row = self.run_preflight(reports={"bridge_doctor": doctor, **HEALTHY})
        self.assertEqual(self.state(row, "BRIDGE_NO_DRIFT"), dogfood.UNMET)

    def test_cursor_never_blocks_the_deterministic_preflight(self):
        """Scope 8: optional, classified accurately, and never fabricated."""

        self.provision()
        row = self.run_preflight(
            reports={"bridge_doctor": BRIDGE_DOCTOR, **HEALTHY},
            service={"definition_present": True, "drift": "none"})
        palette = next(item for item in row["checks"]
                       if item["check"] == "LIVE_PROVIDER_PALETTE")
        self.assertFalse(palette["required"])
        self.assertEqual(palette["palette"]["cursor-secondary"]["readiness"],
                         "unavailable")
        self.assertEqual(palette["palette"]["claude-secondary"]["readiness"],
                         "not_probed")
        self.assertTrue(row["ready"])

    def test_a_work_class_outside_the_contract_is_unmet(self):
        self.provision(policies=False)
        for name in self.contract.projects:
            self.plane.set_policy(supervisor.SupervisorPolicy(
                project_id=name, work_classes=("backlog", "maintenance",
                                               "improvement"),
                policy_version="dogfood-1"))
        row = self.run_preflight()
        self.assertEqual(self.state(row, "WORK_CLASSES_WITHIN_CONTRACT"),
                         dogfood.MET)

    def test_the_preflight_mutates_nothing(self):
        self.provision()
        before = Path(self.path).read_bytes()
        coordination = len(self.store.coordination())
        self.run_preflight(reports={"bridge_doctor": BRIDGE_DOCTOR, **HEALTHY})
        self.assertEqual(len(self.store.coordination()), coordination)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_every_absence_uses_the_canonical_vocabulary(self):
        row = self.run_preflight()
        classes = {item["evidence_class"] for item in row["checks"]}
        self.assertTrue(classes <= (dogfood.CANONICAL_ABSENCE
                                    | {"rederived", "reported_claim"}), classes)
        self.assertEqual(dogfood.CANONICAL_ABSENCE, supervisor.CANONICAL_ABSENCE)


class ProductizationGateTests(unittest.TestCase):
    """A gate that can be ticked without a measurement is not a gate."""

    def setUp(self):
        self.contract = dogfood.load_contract(str(CONTRACT_PATH))

    def test_with_no_evidence_every_criterion_is_not_run_and_the_verdict_holds(self):
        result = dogfood.productization_gate(self.contract)
        self.assertEqual(result["verdict"], "HOLD")
        self.assertEqual({row["observed"] for row in result["criteria"]},
                         {"not_run"})
        self.assertEqual({row["state"] for row in result["criteria"]},
                         {dogfood.UNKNOWN})
        self.assertEqual(len(result["unproven"]), len(result["criteria"]))

    def test_a_criterion_below_its_threshold_is_unmet(self):
        result = dogfood.productization_gate(
            self.contract, {"real_provider_missions_completed": 19})
        row = next(item for item in result["criteria"]
                   if item["criterion"] == "real_provider_missions_completed")
        self.assertEqual(row["state"], dogfood.UNMET)

    def test_an_at_most_criterion_reads_the_other_way(self):
        for observed, state in ((0, dogfood.MET), (1, dogfood.UNMET)):
            result = dogfood.productization_gate(
                self.contract, {"authority_bypass_events": observed})
            row = next(item for item in result["criteria"]
                       if item["criterion"] == "authority_bypass_events")
            self.assertEqual(row["state"], state, observed)

    def test_a_non_numeric_observation_of_a_numeric_threshold_is_unknown(self):
        result = dogfood.productization_gate(
            self.contract, {"unattended_cycles_real_host": "lots"})
        row = next(item for item in result["criteria"]
                   if item["criterion"] == "unattended_cycles_real_host")
        self.assertEqual(row["state"], dogfood.UNKNOWN)

    def test_every_criterion_met_proceeds(self):
        observed = {}
        for criterion in self.contract.productization_gate["criteria"]:
            observed[criterion["criterion"]] = criterion["threshold"]
        result = dogfood.productization_gate(self.contract, observed)
        self.assertEqual(result["verdict"], "PROCEED_TO_PRODUCTIZATION")
        self.assertEqual(result["unproven"], [])

    def test_the_gate_covers_every_risk_class_the_task_named(self):
        retired = {criterion["retires"] for criterion
                   in self.contract.productization_gate["criteria"]}
        for required in ("sustained unattended operation", "real-provider execution",
                         "restart and recovery", "autonomous maintenance",
                         "autonomous improvement", "cost and accounting",
                         "security and isolation", "Owner controls",
                         "zero authority bypass", "rollback and recovery"):
            self.assertIn(required, retired)

    def test_the_gate_states_why_each_number_was_chosen(self):
        rationale = self.contract.productization_gate["rationale"]
        self.assertGreater(len(rationale), 400)
        for number in ("2016", "20", "0.95"):
            self.assertIn(number, rationale)


if __name__ == "__main__":
    unittest.main()
