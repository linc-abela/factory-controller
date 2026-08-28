"""The act between a green preflight and live work, and its bounds.

Every test here is about one of two properties.

**Authority does not widen by accident.**  A host that got healthier, a
capacity window that reopened, a service that got installed, an approval
written for a different act -- none of these may produce a grant, and none may
revive one that ended.  The corpus records the opposite failure four times
under different names, always as a check that passed because there was nothing
to check.

**An activation ends.**  Every grant carries a mission ceiling, an expiry and a
budget, all mandatory; the state machine is derived rather than stored so there
is no record anyone can edit into ``active``; and the twelve drain reasons are
readings of durable state rather than flags.  A shift that could be extended by
resuming it, or that reported ``off`` while a provider process was still alive,
would be finite only on paper.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import (activation, advisor, capacity, dogfood,
                                improvement, production, shift, supervisor)

from tests.test_stage9_supervisor import SupervisorCase

CONTRACT_PATH = (Path(__file__).resolve().parent.parent / "contracts"
                 / "internal-dogfood-run-contract.json")
PORTFOLIO_PATH = (Path(__file__).resolve().parent.parent / "contracts"
                  / "first-dogfood-mission-portfolio.json")

CONTRACT = dogfood.load_contract(str(CONTRACT_PATH))
PORTFOLIO = shift.load_portfolio(str(PORTFOLIO_PATH))

REACHABLE = {
    "factory-prototype-lab": ["229b923b050fe8a4450d5597d472157bd42c8647"],
    "factory-bug-lab": ["4072bfd7c008d3b227e2e164ecbe6f58013c2733",
                        "961a4c97d49183b5501f244ba48773d9f50953ae"],
}

DECLARED_GATES = {
    "factory-prototype-lab": {
        "acceptance_gate_ids": ["dev-check", "dev-test", "dev-evaluate"],
        "source": "https://github.com/linc-abela/factory-prototype-lab.git"
                  "@229b923b050fe8a4450d5597d472157bd42c8647:dev"},
    "factory-bug-lab": {
        "acceptance_gate_ids": ["dev-check", "dev-test", "dev-reproduce"],
        "source": "https://github.com/linc-abela/factory-bug-lab.git"
                  "@961a4c97d49183b5501f244ba48773d9f50953ae:dev"},
}


def ready_preflight(**overrides) -> dict:
    """A preflight row with every check met, so the gate's own checks show.

    Synthetic on purpose: ``tests/test_dogfood_preflight.py`` already holds the
    preflight's own behaviour, and a test that rebuilt a whole healthy host to
    reach the shift gate would be testing the fixture.
    """

    checks = [{"check": name, "state": dogfood.MET, "detail": "fixture",
               "evidence_class": "rederived", "required": True}
              for name in ("PROJECTS_REGISTERED", "ACCEPTANCE_GATES_DECLARED",
                           "SUPERVISOR_CONTROL_STATE", "BRIDGE_NO_DRIFT")]
    row = {"run_ref": CONTRACT.run_ref, "ready": True, "unmet": [],
           "checks": checks}
    row.update(overrides)
    return row


def request(**overrides) -> shift.ActivationRequest:
    values = {"request_ref": "SF-144-shift-1", "run_ref": CONTRACT.run_ref,
              "portfolio_ref": PORTFOLIO.portfolio_ref, "mission_ceiling": 4,
              "duration_seconds": 3600.0, "budget_ceiling": 10.0,
              "budget_currency": "USD"}
    values.update(overrides)
    return shift.ActivationRequest(**values)


def facts(**overrides) -> shift.GateFacts:
    values = {
        "preflight": ready_preflight(), "portfolio": PORTFOLIO,
        "request": request(),
        "contract_projects": CONTRACT.projects,
        "contract_work_classes": CONTRACT.work_classes,
        "contract_environment_classes": CONTRACT.environment_classes,
        "contract_budget_ceiling": CONTRACT.budget_ceiling,
        "contract_budget_currency": CONTRACT.budget_currency,
        "declared_gates": DECLARED_GATES,
        "capacity_readings": {name: {"state": "available", "usable": True}
                              for name in CONTRACT.provider_profiles},
        "eligible_profiles": CONTRACT.provider_profiles,
        "fetchable_shas": REACHABLE,
    }
    values.update(overrides)
    return shift.GateFacts(**values)


class VocabularyTests(unittest.TestCase):
    """The words this module shares with its neighbours, checked not copied."""

    def test_the_absence_vocabulary_is_the_one_the_corpus_owns(self):
        self.assertEqual((shift.MET, shift.UNMET, shift.UNKNOWN),
                         (dogfood.MET, dogfood.UNMET, dogfood.UNKNOWN))
        self.assertEqual(dogfood.CANONICAL_ABSENCE,
                         frozenset({"unknown", "not_applicable", "not_run",
                                    "not_measurable"}))

    def test_the_admitting_states_match_the_plane_that_owns_them(self):
        """A copied set that drifted would authorize work the plane refuses."""

        self.assertEqual(shift.ADMITTING_CONTROL_STATES, supervisor.ADMITTING)

    def test_every_state_and_drain_reason_is_reachable_by_name(self):
        self.assertEqual(set(shift.SHIFT_STATES),
                         {"off", "preparing", "active", "draining", "suspended"})
        self.assertEqual(len(set(shift.DRAIN_REASONS)), 12)

    def test_the_advisory_seam_cannot_propose_a_shift_act(self):
        """Hermes-class advice may explain; it may not activate.

        Checked against the forbidden list rather than the allowlist, because
        an omission from an allowlist is an oversight and a name on this list
        is a decision.
        """

        for kind in ("activate_shift", "revoke_shift", "resume_shift",
                     "clear_blocker", "assert_readiness", "assert_capacity",
                     "widen_capability"):
            self.assertIn(kind, advisor.FORBIDDEN_KINDS)
            self.assertNotIn(kind, advisor.PROPOSAL_KINDS)

    def test_a_refusal_code_does_not_collide_with_a_neighbour(self):
        """The bridge owns the bare names; every code here carries its layer."""

        codes = [refusal.code for refusal in (
            shift.ShiftRefusal("SHIFT_GATE_UNMET", "x"),
            shift.ShiftRefusal("SHIFT_ALREADY_ACTIVE", "x"),
            shift.ShiftRefusal("SHIFT_DRAIN_REQUIRED", "x"))]
        for code in codes:
            self.assertTrue(code.startswith("SHIFT_"), code)


class PortfolioTests(unittest.TestCase):
    def test_the_shipped_portfolio_loads_and_is_ordered(self):
        self.assertEqual([mission.mission_ref for mission in PORTFOLIO.missions],
                         ["DF-1", "DF-2", "DF-3", "DF-4"])
        self.assertEqual([mission.order for mission in PORTFOLIO.missions],
                         [1, 2, 3, 4])

    def test_the_first_mission_mutates_nothing(self):
        self.assertFalse(PORTFOLIO.missions[0].mutates_repository)
        self.assertFalse(PORTFOLIO.missions[1].mutates_repository)

    def test_the_sequence_is_serial(self):
        """Mission two is not offered until mission one has settled."""

        self.assertEqual(PORTFOLIO.next_mission({}).mission_ref, "DF-1")
        self.assertEqual(PORTFOLIO.next_mission({"DF-1": "running"}).mission_ref,
                         "DF-1")
        self.assertEqual(PORTFOLIO.next_mission({"DF-1": "completed"}).mission_ref,
                         "DF-2")

    def test_a_settled_portfolio_offers_nothing_and_says_it_is_complete(self):
        done = {mission.mission_ref: "completed" for mission in PORTFOLIO.missions}
        self.assertIsNone(PORTFOLIO.next_mission(done))
        self.assertTrue(PORTFOLIO.complete(done))

    def test_a_refused_mission_counts_as_settled(self):
        """Otherwise one refusal would stall the portfolio forever."""

        self.assertEqual(
            PORTFOLIO.next_mission({"DF-1": "refused"}).mission_ref, "DF-2")

    def test_a_portfolio_missing_a_required_field_is_refused(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        for key in ("baseline_sha", "rollback_boundary", "evidence_required",
                    "stop_conditions", "acceptance_gate_ids"):
            broken = json.loads(json.dumps(body))
            broken["missions"][0].pop(key)
            with self.assertRaises(shift.ShiftError):
                shift.portfolio_from_payload(broken)

    def test_two_missions_may_not_share_a_reference(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][1]["mission_ref"] = "DF-1"
        with self.assertRaises(shift.ShiftError):
            shift.portfolio_from_payload(body)

    def test_a_portfolio_of_the_wrong_schema_is_not_read_as_one(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["schema_version"] = "factory.controller.something_else.v1"
        with self.assertRaises(shift.ShiftError):
            shift.portfolio_from_payload(body)

    def test_a_missing_portfolio_file_is_a_refusal_not_a_crash(self):
        with self.assertRaises(shift.ShiftError):
            shift.load_portfolio("/nonexistent/portfolio.json")


class RequestTests(unittest.TestCase):
    """Finiteness, checked at the only place a request can be constructed."""

    def test_the_shipped_defaults_are_finite(self):
        entry = request()
        self.assertLessEqual(entry.duration_seconds, shift.MAX_SHIFT_SECONDS)
        self.assertLessEqual(entry.mission_ceiling, shift.MAX_MISSION_CEILING)

    def test_an_unbounded_request_cannot_be_expressed(self):
        for bad in ({"duration_seconds": 0},
                    {"duration_seconds": shift.MAX_SHIFT_SECONDS + 1},
                    {"duration_seconds": float("inf")},
                    {"mission_ceiling": 0},
                    {"mission_ceiling": shift.MAX_MISSION_CEILING + 1},
                    {"budget_ceiling": 0.0},
                    {"budget_ceiling": -1.0},
                    {"request_ref": ""}):
            with self.assertRaises(shift.ShiftError, msg=bad):
                request(**bad)

    def test_a_mission_ceiling_is_a_count_not_a_flag(self):
        with self.assertRaises(shift.ShiftError):
            request(mission_ceiling=True)


class GateTests(unittest.TestCase):
    """Composition, not a second authority."""

    def test_a_ready_host_and_a_lawful_portfolio_pass(self):
        reading = shift.gate(facts())
        self.assertTrue(reading["ready"], reading["blockers"])
        self.assertEqual(reading["gate"], "DOGFOOD-ACTIVATION-GATE")

    def test_every_preflight_check_is_carried_through_unchanged(self):
        pre = ready_preflight()
        reading = shift.gate(facts(preflight=pre))
        carried = {row["check"] for row in reading["checks"]
                   if row.get("source") == "preflight"}
        self.assertEqual(carried, {row["check"] for row in pre["checks"]})

    def test_an_upstream_unmet_check_blocks_the_shift(self):
        """Host readiness is not re-decided here; it is inherited."""

        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNMET
        reading = shift.gate(facts(preflight=pre))
        self.assertFalse(reading["ready"])
        self.assertIn("PROJECTS_REGISTERED",
                      [row["check"] for row in reading["blockers"]])

    def test_an_upstream_unknown_check_is_not_a_pass(self):
        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNKNOWN
        self.assertFalse(shift.gate(facts(preflight=pre))["ready"])

    def test_a_portfolio_naming_an_unadmitted_project_is_refused(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][0]["project_id"] = "some-other-repository"
        reading = shift.gate(facts(portfolio=shift.portfolio_from_payload(body)))
        self.assertIn("PORTFOLIO_WITHIN_RUN_CONTRACT",
                      [row["check"] for row in reading["blockers"]])

    def test_a_portfolio_naming_production_is_refused(self):
        """``production`` is absent from the run contract's environments."""

        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][0]["environment_class"] = "production"
        reading = shift.gate(facts(portfolio=shift.portfolio_from_payload(body)))
        self.assertFalse(reading["ready"])

    def test_a_capability_the_run_contract_never_admitted_is_refused(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][0]["work_class"] = "security_review"
        reading = shift.gate(facts(portfolio=shift.portfolio_from_payload(body)))
        self.assertIn("PORTFOLIO_WITHIN_RUN_CONTRACT",
                      [row["check"] for row in reading["blockers"]])

    def test_a_gate_id_the_registry_never_declared_is_refused(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][0]["acceptance_gate_ids"] = ["dev-check", "dev-invented"]
        reading = shift.gate(facts(portfolio=shift.portfolio_from_payload(body)))
        self.assertIn("PORTFOLIO_GATES_DECLARED",
                      [row["check"] for row in reading["blockers"]])

    def test_no_gate_declarations_reads_unknown_not_met(self):
        reading = shift.gate(facts(declared_gates={}))
        row = [item for item in reading["checks"]
               if item["check"] == "PORTFOLIO_GATES_DECLARED"][0]
        self.assertEqual(row["state"], dogfood.UNKNOWN)
        self.assertEqual(row["evidence_class"], "not_run")
        self.assertFalse(reading["ready"])

    def test_a_first_mutating_mission_is_refused(self):
        body = json.loads(PORTFOLIO_PATH.read_text())
        body["missions"][0]["mutates_repository"] = True
        reading = shift.gate(facts(portfolio=shift.portfolio_from_payload(body)))
        self.assertIn("PORTFOLIO_STARTS_NON_MUTATING",
                      [row["check"] for row in reading["blockers"]])

    def test_a_commit_only_this_host_holds_is_refused(self):
        """The measured case: the bug lab's declared gate source is local-only."""

        reachable = {"factory-prototype-lab": REACHABLE["factory-prototype-lab"],
                     "factory-bug-lab": [
                         "4072bfd7c008d3b227e2e164ecbe6f58013c2733"]}
        reading = shift.gate(facts(fetchable_shas=reachable))
        blocked = [row for row in reading["blockers"]
                   if row["check"] == "PORTFOLIO_SOURCES_FETCHABLE"]
        self.assertTrue(blocked)
        self.assertIn("961a4c97", blocked[0]["detail"])

    def test_no_reachability_reading_is_unknown_not_met(self):
        reading = shift.gate(facts(fetchable_shas=None))
        row = [item for item in reading["checks"]
               if item["check"] == "PORTFOLIO_SOURCES_FETCHABLE"][0]
        self.assertEqual(row["state"], dogfood.UNKNOWN)
        self.assertFalse(reading["ready"])

    def test_a_budget_above_the_run_ceiling_is_refused(self):
        reading = shift.gate(facts(request=request(budget_ceiling=1000.0)))
        self.assertIn("BUDGET_WITHIN_RUN_CONTRACT",
                      [row["check"] for row in reading["blockers"]])

    def test_a_request_for_another_portfolio_is_refused(self):
        reading = shift.gate(facts(request=request(portfolio_ref="something-else")))
        self.assertIn("PORTFOLIO_MATCHES_REQUEST",
                      [row["check"] for row in reading["blockers"]])

    def test_zero_usable_runtimes_blocks_the_gate(self):
        reading = shift.gate(facts(
            capacity_readings={name: {"state": "exhausted", "usable": False}
                               for name in CONTRACT.provider_profiles},
            eligible_profiles=()))
        self.assertIn("RUNTIME_ELIGIBILITY",
                      [row["check"] for row in reading["blockers"]])

    def test_no_capacity_reading_is_unknown_not_met(self):
        reading = shift.gate(facts(capacity_readings={}, eligible_profiles=()))
        row = [item for item in reading["checks"]
               if item["check"] == "RUNTIME_ELIGIBILITY"][0]
        self.assertEqual(row["state"], dogfood.UNKNOWN)

    def test_the_digest_moves_only_when_a_check_state_moves(self):
        first = shift.gate(facts())
        self.assertEqual(first["digest"], shift.gate(facts())["digest"])
        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNMET
        self.assertNotEqual(first["digest"],
                            shift.gate(facts(preflight=pre))["digest"])


class EligibilityTests(unittest.TestCase):
    """Capacity narrows.  Nothing here can add a profile nobody declared."""

    def test_an_unmeasured_runtime_is_kept(self):
        """Switching capacity on for the first time must not stop the Factory."""

        self.assertEqual(shift.eligible(("a", "b"), {}), ("a", "b"))

    def test_an_unusable_runtime_is_dropped(self):
        self.assertEqual(
            shift.eligible(("a", "b"), {"a": {"usable": False}}), ("b",))

    def test_a_denied_runtime_is_dropped_even_when_available(self):
        self.assertEqual(
            shift.eligible(("a", "b"), {"a": {"usable": True}}, denied=("a",)),
            ("b",))

    def test_a_runtime_nobody_declared_is_never_added(self):
        """A reading is not an admission; the declared list is the ceiling."""

        self.assertEqual(
            shift.eligible(("a",), {"a": {"usable": True},
                                    "z": {"usable": True}}), ("a",))

    def test_a_real_reading_object_is_accepted_too(self):
        reading = capacity.RuntimeReading(runtime_id="a", state="available",
                                          usable=True, reason="RUNTIME_AVAILABLE")
        self.assertEqual(shift.eligible(("a",), {"a": reading}), ("a",))
        cooling = capacity.RuntimeReading(runtime_id="a", state="cooling",
                                          usable=False, reason="RUNTIME_COOLING")
        self.assertEqual(shift.eligible(("a",), {"a": cooling}), ())


class _GrantFixture:
    """A grant and a set of readings, shared by the two classes that need them.

    A mixin rather than a base class: inheriting the state tests into the
    admission tests would have run them twice and inflated the count, which is
    the kind of number this corpus has had to re-derive by grep before.
    """

    def grant(self, **overrides) -> shift.Grant:
        values = {"request_ref": "r", "run_ref": "run", "portfolio_ref": "p",
                  "approved_by": "owner", "approval_ref": "notion://SF-144",
                  "gate_digest": "d", "mission_ceiling": 4,
                  "budget_ceiling": 10.0, "budget_currency": "USD",
                  "granted_at": 0.0, "expires_at": 3600.0}
        values.update(overrides)
        return shift.Grant(**values)

    def facts(self, **overrides) -> shift.ShiftFacts:
        values = {"gate_ready": True, "control_state": "running",
                  "eligible_profiles": ("a",), "capacity_measured": True}
        values.update(overrides)
        return shift.ShiftFacts(**values)


class StateTests(_GrantFixture, unittest.TestCase):
    """The transition table, as arithmetic over facts nobody can edit."""

    def test_no_grant_is_off_however_healthy_the_host_is(self):
        """Host preparation is not mission authority, stated as a test."""

        self.assertEqual(shift.state(None, self.facts(), 0.0), "off")

    def test_a_grant_whose_gate_is_unready_is_preparing(self):
        self.assertEqual(
            shift.state(self.grant(), self.facts(gate_ready=False), 0.0),
            "preparing")

    def test_a_grant_whose_control_plane_is_stopped_is_preparing(self):
        self.assertEqual(
            shift.state(self.grant(), self.facts(control_state="stopped"), 0.0),
            "preparing")

    def test_a_paused_control_plane_does_not_admit(self):
        self.assertEqual(
            shift.state(self.grant(), self.facts(control_state="paused"), 0.0),
            "preparing")

    def test_a_ready_gate_and_a_running_plane_is_active(self):
        self.assertEqual(shift.state(self.grant(), self.facts(), 0.0), "active")

    def test_a_revoked_grant_with_work_running_is_draining_not_off(self):
        """Reporting ``off`` over a live provider process is how a duplicate
        irreversible effect happens on the next start."""

        state = shift.state(self.grant(revoked_at=10.0),
                            self.facts(missions_in_flight=1), 20.0)
        self.assertEqual(state, "draining")

    def test_a_revoked_grant_with_nothing_running_is_off(self):
        self.assertEqual(
            shift.state(self.grant(revoked_at=10.0), self.facts(), 20.0), "off")

    def test_an_expired_grant_stops_admitting(self):
        self.assertEqual(shift.state(self.grant(), self.facts(), 3600.0), "off")

    def test_a_suspended_grant_is_suspended_not_off(self):
        self.assertEqual(
            shift.state(self.grant(suspended_at=5.0), self.facts(), 10.0),
            "suspended")

    def test_the_ceiling_ends_the_shift(self):
        self.assertEqual(
            shift.state(self.grant(), self.facts(missions_admitted=4), 0.0),
            "off")

    def test_every_drain_reason_can_actually_fire(self):
        """A stop condition that can never fire is not a stop condition."""

        cases = {
            "OWNER_STOP": (self.grant(revoked_at=1.0), self.facts(), 2.0),
            "EMERGENCY_STOP": (self.grant(), self.facts(emergency_stop=True), 0.0),
            "PORTFOLIO_COMPLETE": (self.grant(),
                                   self.facts(portfolio_complete=True), 0.0),
            "MISSION_CEILING_REACHED": (self.grant(),
                                        self.facts(missions_admitted=4), 0.0),
            "SHIFT_WINDOW_EXPIRED": (self.grant(), self.facts(), 4000.0),
            "BUDGET_CEILING_REACHED": (self.grant(), self.facts(spend=10.0), 0.0),
            "READINESS_LOST": (self.grant(),
                               self.facts(gate_ready=False, missions_in_flight=1),
                               0.0),
            "CAPACITY_EXHAUSTED_NO_ELIGIBLE_RUNTIME": (
                self.grant(), self.facts(eligible_profiles=()), 0.0),
            "PROTECTED_SURFACE_CONFLICT": (
                self.grant(), self.facts(protected_surface_conflict=True), 0.0),
            "PROVIDER_UNCERTAINTY_UNRESOLVED": (
                self.grant(), self.facts(unresolved_uncertain_dispatches=1), 0.0),
            "REPEATED_MISSION_FAILURE": (
                self.grant(), self.facts(consecutive_failures=3), 0.0),
            "ACCEPTANCE_GATE_UNAVAILABLE": (
                self.grant(), self.facts(acceptance_gate_available=False), 0.0),
        }
        self.assertEqual(set(cases), set(shift.DRAIN_REASONS))
        for reason, (grant, state_facts, now) in cases.items():
            self.assertIn(reason, shift.drain_reasons(grant, state_facts, now),
                          reason)

    def test_a_healthy_active_shift_has_no_drain_reason(self):
        """The mirror of the test above: the scan must also be able to stay quiet."""

        self.assertEqual(shift.drain_reasons(self.grant(), self.facts(), 0.0), ())

    def test_unmeasured_capacity_does_not_drain(self):
        """No reading is not the same fact as no capacity."""

        quiet = self.facts(eligible_profiles=(), capacity_measured=False)
        self.assertNotIn("CAPACITY_EXHAUSTED_NO_ELIGIBLE_RUNTIME",
                         shift.drain_reasons(self.grant(), quiet, 0.0))

    def test_restored_capacity_cannot_revive_a_revoked_grant(self):
        """Capacity narrows and never widens, checked in the dangerous direction."""

        revoked = self.grant(revoked_at=1.0)
        healthy = self.facts(eligible_profiles=("a", "b"))
        self.assertEqual(shift.state(revoked, healthy, 2.0), "off")

    def test_restored_capacity_cannot_revive_an_expired_grant(self):
        healthy = self.facts(eligible_profiles=("a", "b"))
        self.assertEqual(shift.state(self.grant(), healthy, 9999.0), "off")


class AdmissionTests(_GrantFixture, unittest.TestCase):
    """The one place a reading becomes an admission."""

    def test_an_active_shift_offers_the_first_mission(self):
        result = shift.admission(self.grant(), PORTFOLIO, self.facts(), {}, 0.0)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["mission"]["mission_ref"], "DF-1")

    def test_a_preparing_shift_offers_nothing(self):
        result = shift.admission(self.grant(), PORTFOLIO,
                                 self.facts(gate_ready=False), {}, 0.0)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["code"], "SHIFT_NOT_ACTIVE")

    def test_no_grant_offers_nothing(self):
        result = shift.admission(None, PORTFOLIO, self.facts(), {}, 0.0)
        self.assertFalse(result["admitted"])

    def test_a_draining_shift_reports_why(self):
        result = shift.admission(self.grant(), PORTFOLIO,
                                 self.facts(emergency_stop=True,
                                            missions_in_flight=1), {}, 0.0)
        self.assertEqual(result["code"], "SHIFT_DRAINING")
        self.assertIn("EMERGENCY_STOP", result["drain_reasons"])

    def test_a_second_mission_is_not_offered_while_one_runs(self):
        result = shift.admission(self.grant(), PORTFOLIO,
                                 self.facts(missions_in_flight=1),
                                 {"DF-1": "running"}, 0.0)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["code"], "SHIFT_MISSION_IN_FLIGHT")

    def test_admission_walks_the_portfolio_in_order(self):
        outcomes = {"DF-1": "completed", "DF-2": "completed"}
        result = shift.admission(self.grant(), PORTFOLIO, self.facts(),
                                 outcomes, 0.0)
        self.assertEqual(result["mission"]["mission_ref"], "DF-3")
        self.assertEqual(result["remaining_missions"], 4)


class PlaneCase(SupervisorCase):
    def setUp(self):
        super().setUp()
        self.shift = shift.ShiftPlane(self.store, clock=self.clock)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def approval(self, request_ref="SF-144-shift-1", **overrides) -> dict:
        body = {"schema_version": shift.APPROVAL_SCHEMA,
                "request_ref": request_ref, "approved": True,
                "approved_by": "owner", "approval_ref": "notion://SF-144"}
        body.update(overrides)
        path = Path(self.tmpdir.name) / ("approval-%s.json" % len(
            list(Path(self.tmpdir.name).iterdir())))
        path.write_text(json.dumps(body))
        return shift.approval_record(str(path), request_ref=request_ref)


class ApprovalTests(PlaneCase):
    def test_no_record_is_absence_not_a_refusal(self):
        record = shift.approval_record(None, request_ref="r")
        self.assertEqual(record["state"], "not_run")
        self.assertFalse(record["approved"])

    def test_a_missing_file_is_absence(self):
        record = shift.approval_record("/nonexistent.json", request_ref="r")
        self.assertEqual(record["state"], "not_run")

    def test_a_supervisor_service_approval_is_not_a_shift_approval(self):
        """The two records exist so that one act cannot stand in for the other."""

        path = Path(self.tmpdir.name) / "service.json"
        path.write_text(json.dumps({
            "schema_version": activation.APPROVAL_SCHEMA,
            "label": activation.DEFAULT_LABEL, "approved": True,
            "approved_by": "owner", "approval_ref": "notion://SF-142"}))
        record = shift.approval_record(str(path), request_ref="SF-144-shift-1")
        self.assertFalse(record["approved"])

    def test_a_record_for_another_request_does_not_apply(self):
        record = self.approval(request_ref="SF-144-shift-9")
        self.assertTrue(record["approved"])
        again = shift.approval_record(record["source"],
                                      request_ref="SF-144-shift-1")
        self.assertEqual(again["state"], "not_applicable")
        self.assertFalse(again["approved"])

    def test_a_record_without_a_named_approver_is_not_granted(self):
        record = self.approval(approved_by="")
        self.assertFalse(record["approved"])

    def test_the_supervisor_reader_still_behaves_as_it_did(self):
        """Widening the reader must not change the call it already had."""

        path = Path(self.tmpdir.name) / "svc.json"
        path.write_text(json.dumps({
            "schema_version": activation.APPROVAL_SCHEMA,
            "label": activation.DEFAULT_LABEL, "approved": True,
            "approved_by": "owner", "approval_ref": "notion://SF-142"}))
        granted = activation.approval(str(path))
        self.assertTrue(granted["approved"])
        self.assertEqual(granted["label"], activation.DEFAULT_LABEL)


class PreviewApplyRevokeTests(PlaneCase):
    def test_preview_writes_nothing_and_shows_the_effects(self):
        result = self.shift.preview(facts(), approval=self.approval())
        self.assertTrue(result["ready"])
        self.assertIsNone(self.shift.grant("SF-144-shift-1"))
        effects = result["would_authorize"]
        self.assertEqual(effects["mission_ceiling"], 4)
        self.assertEqual(effects["repository_mutating_missions"],
                         ["DF-3", "DF-4"])
        self.assertEqual(sorted(effects["environment_classes"]),
                         ["local-sim", "staging"])

    def test_preview_names_what_would_not_be_authorized(self):
        result = self.shift.preview(facts())
        joined = " ".join(result["would_not_authorize"])
        self.assertIn("host service", joined)
        self.assertIn("production", joined)

    def test_preview_shows_every_blocker_before_an_apply_is_attempted(self):
        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNMET
        result = self.shift.preview(facts(preflight=pre, fetchable_shas=None))
        self.assertFalse(result["ready"])
        self.assertEqual({row["check"] for row in result["blockers"]},
                         {"PROJECTS_REGISTERED", "PORTFOLIO_SOURCES_FETCHABLE"})

    def test_apply_creates_one_grant(self):
        result = self.shift.apply(facts(), self.approval())
        self.assertTrue(result["created"])
        grant = self.shift.grant("SF-144-shift-1")
        self.assertEqual(grant.approved_by, "owner")
        self.assertEqual(grant.approval_ref, "notion://SF-144")
        self.assertEqual(grant.expires_at, grant.granted_at + 3600.0)

    def test_apply_is_idempotent(self):
        first = self.shift.apply(facts(), self.approval())
        second = self.shift.apply(facts(), self.approval())
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["grant"], second["grant"])
        self.assertEqual(len(self.shift.grants()), 1)

    def test_apply_without_an_approval_is_refused(self):
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.apply(facts(), shift.approval_record(
                None, request_ref="SF-144-shift-1"))
        self.assertEqual(caught.exception.code, "SHIFT_UNAPPROVED")
        self.assertIsNone(self.shift.grant("SF-144-shift-1"))

    def test_apply_against_an_unmet_gate_is_refused(self):
        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNMET
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.apply(facts(preflight=pre), self.approval())
        self.assertEqual(caught.exception.code, "SHIFT_GATE_UNMET")

    def test_the_gate_is_checked_before_the_approval(self):
        """An approval cannot be offered in place of a missing prerequisite."""

        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNKNOWN
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.apply(facts(preflight=pre), self.approval())
        self.assertEqual(caught.exception.code, "SHIFT_GATE_UNMET")

    def test_a_second_overlapping_shift_is_refused(self):
        self.shift.apply(facts(), self.approval())
        other = facts(request=request(request_ref="SF-144-shift-2"))
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.apply(other, self.approval("SF-144-shift-2"))
        self.assertEqual(caught.exception.code, "SHIFT_ALREADY_ACTIVE")

    def test_revoke_is_idempotent_and_records_the_reason(self):
        self.shift.apply(facts(), self.approval())
        first = self.shift.revoke("SF-144-shift-1", reason="owner stopped it")
        second = self.shift.revoke("SF-144-shift-1", reason="again")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(self.shift.grant("SF-144-shift-1").revoke_reason,
                         "owner stopped it")

    def test_a_revoked_request_cannot_be_re_applied(self):
        """Reversible means a new decision, not an undo."""

        self.shift.apply(facts(), self.approval())
        self.shift.revoke("SF-144-shift-1", reason="stop")
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.apply(facts(), self.approval())
        self.assertEqual(caught.exception.code, "SHIFT_GRANT_REVOKED")

    def test_a_new_request_ref_may_open_a_new_shift_after_a_revocation(self):
        self.shift.apply(facts(), self.approval())
        self.shift.revoke("SF-144-shift-1", reason="stop")
        result = self.shift.apply(
            facts(request=request(request_ref="SF-144-shift-2")),
            self.approval("SF-144-shift-2"))
        self.assertTrue(result["created"])

    def test_revoking_an_unknown_request_is_a_named_refusal(self):
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.revoke("nobody", reason="x")
        self.assertEqual(caught.exception.code, "SHIFT_GRANT_UNKNOWN")

    def test_every_owner_act_is_recorded_append_only(self):
        self.shift.apply(facts(), self.approval())
        self.shift.revoke("SF-144-shift-1", reason="stop")
        events = self.shift.events("SF-144-shift-1")
        self.assertEqual([row["event"] for row in events],
                         ["revoked", "granted"])
        with self.assertRaises(Exception):
            with self.store.transaction() as db:
                db.execute("DELETE FROM shift_events")


class SuspendResumeTests(PlaneCase):
    """Shutdown hands over durable state, or it is not a shutdown."""

    def test_suspend_requires_a_drained_shift(self):
        self.shift.apply(facts(), self.approval())
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.suspend("SF-144-shift-1", resume_ref="vault://x",
                               missions_in_flight=2)
        self.assertEqual(caught.exception.code, "SHIFT_DRAIN_REQUIRED")

    def test_suspend_requires_somewhere_to_resume_from(self):
        self.shift.apply(facts(), self.approval())
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.suspend("SF-144-shift-1", resume_ref="",
                               missions_in_flight=0)
        self.assertEqual(caught.exception.code, "SHIFT_RESUME_REF_REQUIRED")

    def test_suspend_then_resume_keeps_the_original_expiry(self):
        """A suspension that extended the window would make the ceiling advisory."""

        self.shift.apply(facts(), self.approval())
        before = self.shift.grant("SF-144-shift-1").expires_at
        self.shift.suspend("SF-144-shift-1", resume_ref="vault://SF-144/checkpoint",
                           missions_in_flight=0)
        self.clock.advance(1800)
        self.shift.resume("SF-144-shift-1")
        after = self.shift.grant("SF-144-shift-1")
        self.assertEqual(after.expires_at, before)
        self.assertIsNone(after.suspended_at)

    def test_suspend_and_resume_are_both_idempotent(self):
        self.shift.apply(facts(), self.approval())
        self.shift.suspend("SF-144-shift-1", resume_ref="vault://x",
                           missions_in_flight=0)
        self.assertFalse(self.shift.suspend(
            "SF-144-shift-1", resume_ref="vault://x",
            missions_in_flight=0)["changed"])
        self.shift.resume("SF-144-shift-1")
        self.assertFalse(self.shift.resume("SF-144-shift-1")["changed"])

    def test_a_revoked_grant_cannot_be_resumed(self):
        self.shift.apply(facts(), self.approval())
        self.shift.revoke("SF-144-shift-1", reason="stop")
        with self.assertRaises(shift.ShiftRefusal) as caught:
            self.shift.resume("SF-144-shift-1")
        self.assertEqual(caught.exception.code, "SHIFT_GRANT_REVOKED")

    def test_a_restart_reconstructs_the_shift_from_the_store_alone(self):
        """No conversational memory: a second plane over the same file agrees."""

        self.shift.apply(facts(), self.approval())
        self.shift.suspend("SF-144-shift-1", resume_ref="vault://SF-144/cp",
                           missions_in_flight=0)
        revived = shift.ShiftPlane(self.store, clock=self.clock)
        grant = revived.grant("SF-144-shift-1")
        self.assertEqual(grant.resume_ref, "vault://SF-144/cp")
        self.assertEqual(
            shift.state(grant, shift.ShiftFacts(gate_ready=True,
                                                control_state="running"),
                        self.clock()),
            "suspended")
        self.assertEqual([row["event"] for row in revived.events()],
                         ["suspended", "granted"])


class ObservationTests(PlaneCase):
    """Facts read from the planes that own them, not from a cached copy."""

    def test_an_empty_store_has_nothing_in_flight_and_nothing_admitted(self):
        observed = self.shift.observe(PORTFOLIO, control_state="running",
                                      gate_ready=True)
        self.assertEqual((observed.missions_in_flight, observed.missions_admitted),
                         (0, 0))
        self.assertFalse(observed.portfolio_complete)

    def test_a_submitted_portfolio_mission_counts_as_in_flight(self):
        self.project("factory-prototype-lab")
        self.controller.submit({"work_item_id": "DF-1",
                                "project_id": "factory-prototype-lab",
                                "execution_mode": "fixture",
                                "acceptance_gate_ids": ["dev-check"]}, "DF-1")
        observed = self.shift.observe(PORTFOLIO, control_state="running",
                                      gate_ready=True)
        self.assertEqual(observed.missions_in_flight, 1)
        self.assertEqual(observed.missions_admitted, 1)

    def test_unmeasured_capacity_is_reported_as_unmeasured(self):
        observed = self.shift.observe(PORTFOLIO, control_state="running",
                                      gate_ready=True,
                                      profiles=CONTRACT.provider_profiles)
        self.assertFalse(observed.capacity_measured)
        self.assertEqual(tuple(observed.eligible_profiles),
                         tuple(CONTRACT.provider_profiles))

    def test_a_cooling_runtime_narrows_eligibility(self):
        name = CONTRACT.provider_profiles[0]
        self.store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=name))
        self.store.observe_capacity(capacity.CapacityObservation(
            runtime_id=name, state="cooling", source="operator",
            source_ref="test", observed_at=self.clock()))
        observed = self.shift.observe(
            PORTFOLIO, control_state="running", gate_ready=True,
            capacity_readings=self.store.capacity_readings(),
            profiles=CONTRACT.provider_profiles)
        self.assertTrue(observed.capacity_measured)
        self.assertNotIn(name, observed.eligible_profiles)


class PreflightExtensionTests(SupervisorCase):
    """The two facts the composed gate added to the preflight itself."""

    def setUp(self):
        super().setUp()
        self.ledger_ = production.ProductionLedger(self.store)
        self.improvement = improvement.ImprovementPlane(self.store, self.ledger_)

    def preflight(self, **kwargs):
        return dogfood.preflight(CONTRACT, store=self.store,
                                 supervisor_plane=self.plane, **kwargs)

    def check(self, name, **kwargs):
        rows = [row for row in self.preflight(**kwargs).checks
                if row["check"] == name]
        self.assertEqual(len(rows), 1, name)
        return rows[0]

    def test_capacity_with_no_readings_is_unknown_not_met(self):
        row = self.check("PROVIDER_CAPACITY")
        self.assertEqual(row["state"], dogfood.UNKNOWN)
        self.assertEqual(row["evidence_class"], "not_run")

    def test_capacity_is_met_when_a_declared_runtime_is_usable(self):
        name = CONTRACT.provider_profiles[0]
        self.store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=name))
        self.store.observe_capacity(capacity.CapacityObservation(
            runtime_id=name, state="available", source="operator",
            source_ref="test", observed_at=self.clock()))
        self.assertEqual(self.check("PROVIDER_CAPACITY")["state"], dogfood.MET)

    def test_capacity_is_unmet_when_every_declared_runtime_is_exhausted(self):
        for name in CONTRACT.provider_profiles:
            self.store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=name))
            self.store.observe_capacity(capacity.CapacityObservation(
                runtime_id=name, state="exhausted", source="operator",
                source_ref="test", observed_at=self.clock()))
        self.assertEqual(self.check("PROVIDER_CAPACITY")["state"], dogfood.UNMET)

    def test_protected_surfaces_without_a_plane_are_unknown(self):
        row = self.check("PROTECTED_SURFACES_DECLARED")
        self.assertEqual(row["state"], dogfood.UNKNOWN)
        self.assertEqual(row["evidence_class"], "not_run")

    def test_a_project_with_no_protected_surface_is_refused(self):
        """A project where the Stage-8 check can never fire is not protected."""

        row = self.check("PROTECTED_SURFACES_DECLARED",
                         improvement_plane=self.improvement)
        self.assertEqual(row["state"], dogfood.UNMET)
        self.assertEqual(sorted(row["declared"]), sorted(CONTRACT.projects))

    def test_the_preflight_still_mutates_nothing(self):
        before = self.store.counts()
        self.preflight(improvement_plane=self.improvement)
        self.assertEqual(self.store.counts(), before)


class BriefTests(PlaneCase):
    """The eleven answers, and the one act that follows them."""

    def brief(self, **overrides):
        gate_facts = facts(**overrides)
        reading = shift.gate(gate_facts)
        observed = self.shift.observe(
            PORTFOLIO, control_state="running", gate_ready=reading["ready"],
            capacity_readings=gate_facts.capacity_readings,
            profiles=CONTRACT.provider_profiles)
        return shift.brief(self.shift.grant(), observed, reading, PORTFOLIO,
                           self.shift.outcomes(PORTFOLIO), self.clock())

    def test_a_blocked_brief_names_the_blocker_as_the_next_act(self):
        pre = ready_preflight()
        pre["checks"][0]["state"] = dogfood.UNMET
        result = self.brief(preflight=pre)
        self.assertEqual(result["shift_state"], "off")
        self.assertIn("PROJECTS_REGISTERED", result["next_owner_action"]["act"])

    def test_a_ready_brief_with_no_grant_asks_for_the_decision(self):
        result = self.brief()
        self.assertTrue(result["activation_readiness"]["ready"])
        self.assertIn("approval", result["next_owner_action"]["act"])
        self.assertIsNone(result["grant"])

    def test_the_brief_answers_all_eleven_questions(self):
        result = self.brief()
        for key in ("phase", "activation_readiness", "unresolved_owner_actions",
                    "admitted_projects", "admitted_capabilities",
                    "usable_runtimes", "capacity", "proposed_missions", "risk",
                    "checkpoints", "next_owner_action"):
            self.assertIn(key, result)

    def test_the_brief_marks_unrun_missions_with_an_absence_word(self):
        result = self.brief()
        outcomes = {row["mission_ref"]: row["outcome"]
                    for row in result["proposed_missions"]}
        self.assertEqual(set(outcomes.values()), {"not_run"})
        self.assertIn("not_run", dogfood.CANONICAL_ABSENCE)

    def test_the_brief_arms_every_stop_condition(self):
        self.assertEqual(set(self.brief()["risk"]["stop_conditions_armed"]),
                         set(shift.DRAIN_REASONS))


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
