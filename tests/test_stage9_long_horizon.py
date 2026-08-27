"""Stage 9: seventy-two virtual hours of an unattended Factory, four projects.

Every other Stage-9 test states one thing that must remain impossible.  This
file runs the whole thing for three virtual days and asks a different question:
with real work arriving, providers going away, budgets running out, incidents
landing, a staging deployment failing, a quiet window closing, the Owner pausing
and the host restarting -- does it keep doing useful work, and does it stay
inside every bound at once?

The clock is hand-wound and every event is scheduled, so the run is a fixture
rather than a soak test: the same 288 cycles happen in the same order every
time, and a failure names a virtual hour.  Nothing here sleeps or waits.

What the run is evidence *for* is stated as assertions at the bottom, and they
are the acceptance criteria of the task rather than a summary of what happened:
one cycle per invocation, forward progress in all four projects, zero duplicate
irreversible effects, zero deployments the supervisor caused, zero
cross-project leakage, no hot loop, and a pause the Owner can actually rely on.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path

from factory_controller import (improvement, maintenance, portfolio, production,
                                supervisor)
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, BETA, Clock

HOURS = 72
TICKS_PER_HOUR = 4
TICK_SECONDS = 3600 // TICKS_PER_HOUR
TICKS = HOURS * TICKS_PER_HOUR

#: Four projects with genuinely different Owner policy, so a bound that leaked
#: from one to another would change an assertion rather than pass unnoticed.
PROJECTS = {
    "alpha": {"priority": 10, "cap": 2},
    "beta": {"priority": 100, "cap": 2},
    "gamma": {"priority": 200, "cap": 1},
    "delta": {"priority": 300, "cap": 1, "budget_ceiling": 0.05,
              "budget_currency": "USD"},
}

#: `gamma` only runs during the working day.  Chosen so the simulation crosses
#: the boundary in both directions several times.
QUIET_WINDOW = (8, 20)

#: The execution layer is gone for these *elapsed* hours of the run, for every
#: project.  Elapsed rather than wall-clock: the run is three days long, so an
#: outage stated as a clock hour would either recur daily or -- for a number
#: above 23 -- never fire at all, which is how the first version of this
#: constant silently tested two thirds of what it claimed.
OUTAGE_HOURS = frozenset({14, 15, 16, 40, 41})

PAUSE_TICK = 30 * TICKS_PER_HOUR
RESUME_TICK = 34 * TICKS_PER_HOUR
RESTART_TICK = 50 * TICKS_PER_HOUR
INCIDENT_TICKS = {20 * TICKS_PER_HOUR: "alpha", 45 * TICKS_PER_HOUR: "beta"}
STAGING_FAILURE_TICK = 26 * TICKS_PER_HOUR
EXPERIMENT_TICK = 12 * TICKS_PER_HOUR

GATES = ["G-BUILD"]
CANDIDATES = [{"profile": ALPHA, "capabilities": ["implement"]},
              {"profile": BETA, "capabilities": ["implement"]}]
SURFACES = {name: ("protected/%s/" % name,)
            for name in improvement.MANDATORY_SURFACES}

#: Anything that would mean a secret had reached durable state.  Scanned over
#: every row the supervisor wrote, not over the source, which the authority
#: boundary test already covers.
LEAK_PATTERN = re.compile(
    r"(sk-[a-z0-9]{8,})|(ghp_[A-Za-z0-9]{8,})|(bearer\s)|(api[_-]?key)",
    re.IGNORECASE)


def repository(project_id: str) -> str:
    return "https://example.invalid/%s.git" % project_id


def release_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()


class SimulationLayer:
    """The execution layer, with an outage schedule and a per-project price.

    Modelled on `tests.support.LayerAdapter` and kept separate rather than
    parameterised into it: this one needs to change its answer as virtual time
    moves, which the route tests deliberately do not.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.start = clock.now
        self.invocations: list[str] = []
        self.dispatch_keys: list[str] = []
        self.gate_failures: set[str] = set()

    @property
    def elapsed_hour(self) -> int:
        return int((self.clock.now - self.start) // 3600)

    @property
    def available(self) -> bool:
        return self.elapsed_hour not in OUTAGE_HOURS

    def execute(self, step: str, operation_key: str, value: dict) -> dict:
        self.invocations.append("%s:%s" % (step, operation_key))
        if step == "dispatch":
            return self._dispatch(operation_key, value)
        if step == "verify":
            return {"verified": True}
        if step == "evaluate":
            work_item = value["mission"].get("work_item_id", "")
            passed = work_item not in self.gate_failures
            return {"passed": passed,
                    "gate_outcomes": [{"gate_id": gate, "passed": passed,
                                       "detail": "simulation"}
                                      for gate in value["mission"].get(
                                          "acceptance_gate_ids") or GATES]}
        if step == "evidence":
            return {"accepted": True, "retryable": False,
                    "evidence_pointer": "e" * 64}
        return {"status": "unknown"}

    def _dispatch(self, operation_key: str, value: dict) -> dict:
        route = value["route"]
        profile = route["provider_profile"]
        receipt = {"provider_profile": profile,
                   "provider": None if profile is None else profile + "/v1",
                   "execution_mode": "fixture", "duration_ms": 900,
                   "idempotency_key": route["idempotency_key"],
                   "usage": self._usage(value["mission"])}
        if not self.available:
            # Proven not to have started, which is what lets the Controller
            # fall back and what makes the outage an infrastructure fact.
            return {"status": "provider_unavailable",
                    "diagnostic": "PROFILE_UNAVAILABLE",
                    "receipt": {**receipt, "process_started": False,
                                "refusal_code": "PROFILE_UNAVAILABLE"}}
        self.dispatch_keys.append(operation_key)
        return {"status": "completed",
                "candidate_sha": release_sha(operation_key),
                "execution_id": operation_key,
                "receipt": {**receipt, "process_started": True}}

    @staticmethod
    def _usage(payload: dict) -> dict:
        if payload.get("project_id") != "delta":
            return {"cost_state": "not_applicable"}
        return {"cost_state": "reported", "cost_amount": 0.02,
                "cost_currency": "USD", "input_tokens": 1000,
                "output_tokens": 200}


class LongHorizonSimulation(unittest.TestCase):
    """Seventy-two virtual hours, run once in `setUpClass` and asserted below."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "controller.db"
        cls.clock = Clock(now=1_800_000_000.0)
        cls.layer = SimulationLayer(cls.clock)
        cls.trace: list[dict] = []
        cls.owner_deployments: list[str] = []
        cls.simulate()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- the run ------------------------------------------------------------ #

    @classmethod
    def open_host(cls):
        """A replacement process over the same durable database file."""

        store = MissionStore(str(cls.path), clock=cls.clock)
        controller = Controller(
            store, cls.layer,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            lease_seconds=120)
        ledger = production.ProductionLedger(store)
        return store, controller, ledger, supervisor.OperationsSupervisor(
            controller, clock=cls.clock)

    @classmethod
    def simulate(cls):
        store, controller, ledger, plane = cls.open_host()
        cls.setup_portfolio(store, ledger, plane)
        plane.transition("running", actor="owner", reason="stage-9 simulation")

        # No advisory service and no model gateway exist in this run, and the
        # deterministic path must not merely tolerate that -- it must never
        # reach for one.  A socket opened anywhere below fails the whole run.
        real_socket = socket.socket

        def refuse(*args, **kwargs):
            raise AssertionError("the unattended path opened a socket")

        socket.socket = refuse
        try:
            for tick in range(TICKS):
                if tick == RESTART_TICK:
                    store, controller, ledger, plane = cls.open_host()
                cls.owner_events(tick, store, controller, ledger, plane)
                report = plane.cycle("host-%d" % tick)
                cls.trace.append({
                    "tick": tick, "hour": tick // TICKS_PER_HOUR,
                    "clock_hour": int(cls.clock.now // 3600) % 24,
                    "control_state": report["control_state"],
                    "outcome": report["outcome"],
                    "advanced": report["advanced"],
                    "promoted": report["promoted"],
                    "refused": [row["reason"] for row in report["refused"]],
                    "cycle_id": report["cycle_id"],
                    "sequence": report["sequence"],
                })
                cls.clock.advance(TICK_SECONDS)
        finally:
            socket.socket = real_socket
        cls.store, cls.controller, cls.ledger, cls.plane = store, controller, ledger, plane
        cls.final_brief = plane.brief()

    @classmethod
    def setup_portfolio(cls, store, ledger, plane):
        store.set_portfolio_policy(portfolio.PortfolioPolicy(
            portfolio_concurrency=3, aging_seconds=1800.0, policy_version="pf-1"))
        for project_id, values in PROJECTS.items():
            store.register_project(portfolio.ProjectPolicy(
                project_id=project_id, repository=repository(project_id),
                priority=values["priority"], concurrency_cap=values["cap"],
                budget_ceiling=values.get("budget_ceiling"),
                budget_currency=values.get("budget_currency"),
                policy_version="1.0"))
            window = QUIET_WINDOW if project_id == "gamma" else (None, None)
            plane.set_policy(supervisor.SupervisorPolicy(
                project_id=project_id, missions_per_cycle=2,
                maintenance_admissions=1, improvement_admissions=1,
                window_start_hour=window[0], window_end_hour=window[1],
                failure_threshold=4, suppression_seconds=7200.0,
                policy_version="sp-1"))
            ledger.register_environment(production.EnvironmentPolicy(
                environment_id=project_id + "-staging", project_id=project_id,
                environment_class="staging", repository=repository(project_id),
                service_ref=project_id + "-web", approver_refs=("owner",),
                autonomous=True))
            ledger.register_environment(production.EnvironmentPolicy(
                environment_id=project_id + "-prod", project_id=project_id,
                environment_class="production", repository=repository(project_id),
                service_ref=project_id + "-web",
                approver_refs=("owner", "deputy")))
            maintenance.MaintenancePlane(store, ledger).set_policy(
                maintenance.MaintenancePolicy(
                    project_id=project_id, enabled=True, cooldown_seconds=0,
                    concurrency=2, policy_version="mp-1"))
            improvement.ImprovementPlane(store, ledger).set_policy(
                improvement.ImprovementPolicy(
                    project_id=project_id, enabled=True, cooldown_seconds=0,
                    protected_surfaces=SURFACES, policy_version="ip-1"))

    @classmethod
    def owner_events(cls, tick, store, controller, ledger, plane):
        """Everything an Owner or the outside world does, on a fixed schedule.

        All of it is authorization: submitting a work item, declaring an
        incident, deploying to staging, pausing.  None of it is something the
        supervisor could have done for itself, which is the point of listing it
        here rather than inside the cycle.
        """

        hour = tick // TICKS_PER_HOUR
        if tick % (3 * TICKS_PER_HOUR) == 0:
            for index, project_id in enumerate(PROJECTS):
                kind = "bug" if (hour + index) % 3 == 0 else "feature"
                work_item = "%s-%s-H%d" % (project_id.upper(), kind.upper(), hour)
                if kind == "bug" and hour % 12 == 0:
                    cls.layer.gate_failures.add(work_item)
                controller.submit({
                    "work_item_id": work_item, "project_id": project_id,
                    "repository": repository(project_id), "capability": kind,
                    "execution_mode": "fixture", "acceptance_gate_ids": GATES,
                    "provider_candidates": CANDIDATES}, work_item)
        if tick == PAUSE_TICK:
            plane.transition("paused", actor="owner", reason="owner away")
        if tick == RESUME_TICK:
            plane.transition("running", actor="owner", reason="owner back")
        if tick == EXPERIMENT_TICK:
            cls.open_experiment(store, ledger)
        if tick == STAGING_FAILURE_TICK:
            cls.fail_a_staging_deployment(store, ledger)
        if tick in INCIDENT_TICKS:
            cls.declare_incident(store, ledger, INCIDENT_TICKS[tick], hour)

    @classmethod
    def open_experiment(cls, store, ledger):
        plane = improvement.ImprovementPlane(store, ledger)
        objective = improvement.Objective(
            objective_ref="OBJ-gamma", project_id="gamma",
            improvement_class="performance",
            statement="gamma should answer faster without losing tests",
            metrics=(improvement.Metric("p95_latency_ms", "decrease", "objective",
                                        min_delta_ratio=0.10),
                     improvement.Metric("passing_tests", "increase",
                                        "non_regression", tolerance_ratio=0.0)),
            objective_version="1.0")
        plane.register_objective(objective)
        row = plane.admit_experiment(
            "OBJ-gamma", "owner_objective", "OBJ-gamma",
            target_repository=repository("gamma"),
            baseline_sha=release_sha("gamma-baseline"),
            isolation_ref="lane://gamma/experiment-1")
        plane.record_baseline(row["experiment_ref"],
                              {"p95_latency_ms": 420.0, "passing_tests": 130})
        cls.experiment_ref = row["experiment_ref"]

    @classmethod
    def fail_a_staging_deployment(cls, store, ledger):
        """An Owner-driven staging deployment that the ledger settles as failed."""

        bundle = production.ReleaseBundle.from_payload({
            "bundle_ref": "rc-delta-001", "project_id": "delta",
            "repository": repository("delta"),
            "release_sha": release_sha("delta-release"),
            "mission_ref": "SF-141", "evidence_refs": ["evidence/delta.json"],
            "evaluator_receipts": ["receipts/evaluate.json"],
            "artifact": {"kind": "image", "identity": "sha256:" + "c" * 64},
            "env_schema": {"PORT": {"type": "integer", "required": True,
                                    "description": "service port"}},
            "migration": {"forward_ref": "migrations/001.sql",
                          "reverse_ref": "migrations/001.down.sql"},
            "release_policy_version": "1.0",
            "provenance": {"built_by": "owner",
                           "built_at": "2026-08-27T00:00:00Z",
                           "contract_version": production.CONTRACT_VERSION}})
        deployment = ledger.admit_release(bundle, "delta-staging", "owner")
        ledger.deploy(deployment,
                      production.DeterministicDeploymentAdapter(reached=False))
        cls.owner_deployments.append(deployment)
        maintenance.MaintenancePlane(store, ledger).admit_trigger(
            "deployment_health_failure", deployment)

    @classmethod
    def declare_incident(cls, store, ledger, project_id, hour):
        incident_ref = "INC-%s-H%d" % (project_id, hour)
        ledger.declare_incident(
            incident_ref=incident_ref,
            environment_id=project_id + "-staging", declared_by="owner",
            incident_class="triaged_defect",
            affected_release_sha=release_sha(incident_ref),
            affected_bundle_ref="rc-000",
            failing_behaviour="checkout fails at hour %d" % hour,
            blast_radius="all traffic")
        maintenance.MaintenancePlane(store, ledger).admit_trigger(
            "production_incident", incident_ref)

    # -- readings ------------------------------------------------------------ #

    def missions(self):
        return [row for row in self.plane._mission_lines()]

    def completed(self):
        return [row for row in self.missions() if row["state"] == "completed"]

    # -- acceptance ---------------------------------------------------------- #

    def test_the_run_covered_seventy_two_virtual_hours(self):
        self.assertEqual(len(self.trace), TICKS)
        self.assertGreaterEqual(self.trace[-1]["hour"] + 1, HOURS)

    def test_one_invocation_produced_exactly_one_cycle(self):
        """No runaway, no recursion, and nothing left open across a restart."""

        rows = sorted(self.plane.cycles(limit=1_000_000),
                      key=lambda row: row["sequence"])
        self.assertEqual(len(rows), TICKS)
        self.assertEqual([row["sequence"] for row in rows],
                         list(range(1, TICKS + 1)))
        self.assertEqual([row for row in rows if row["ended_at"] is None], [])

    def test_the_cycle_chain_is_unbroken_across_the_restart(self):
        rows = sorted(self.plane.cycles(limit=1_000_000),
                      key=lambda row: row["sequence"])
        for previous, current in zip(rows, rows[1:]):
            self.assertEqual(current["previous_cycle_id"], previous["cycle_id"])

    def test_all_four_projects_made_forward_progress(self):
        """Starvation is the failure a fair-looking scheduler hides best."""

        by_project: dict[str, int] = {}
        for row in self.completed():
            by_project[row["project_id"]] = by_project.get(row["project_id"], 0) + 1
        self.assertEqual(set(by_project), set(PROJECTS))
        for project_id, count in by_project.items():
            self.assertGreater(count, 0, project_id)

    def test_the_lowest_priority_project_was_not_starved_by_the_highest(self):
        counts = {project_id: 0 for project_id in PROJECTS}
        for row in self.completed():
            counts[row["project_id"]] += 1
        self.assertGreater(counts["delta"], 0)
        self.assertGreater(counts["gamma"], 0)

    def test_zero_duplicate_irreversible_effects(self):
        """The whole run's provider invocations, counted by operation key."""

        keys = self.layer.dispatch_keys
        duplicates = {key for key in keys if keys.count(key) > 1}
        self.assertEqual(duplicates, set())
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreater(len(keys), 40, "the run has to have done real work")

    def test_no_mission_ran_a_dispatch_step_twice(self):
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT mission_id, COUNT(*) AS n FROM steps WHERE name='dispatch'"
                " GROUP BY mission_id HAVING n > 1").fetchall()
        self.assertEqual([dict(row) for row in rows], [])

    def test_the_supervisor_caused_no_deployment_of_any_kind(self):
        """Every deployment in the ledger is one the Owner made in this file."""

        with self.store.transaction() as db:
            rows = db.execute("SELECT id FROM deployments").fetchall()
        self.assertEqual(sorted(row["id"] for row in rows),
                         sorted(self.owner_deployments))

    def test_no_release_reached_a_gated_environment(self):
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT d.id FROM deployments d JOIN environments e"
                " ON e.environment_id = d.environment_id"
                " WHERE e.environment_class = 'production'").fetchall()
        self.assertEqual([dict(row) for row in rows], [])

    def test_no_experiment_was_promoted_sealed_or_accepted_unattended(self):
        plane = improvement.ImprovementPlane(self.store, self.ledger)
        rows = plane.experiments()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["state"], {"admitted", "baseline_measured",
                                         "mission_created"})
            self.assertIsNone(row["disposition"])

    def test_no_cross_project_leakage(self):
        """A mission's repository always matches its own project's repository."""

        for row in self.missions():
            payload = row["payload"]
            if not payload.get("repository"):
                continue
            self.assertEqual(payload["repository"],
                             repository(row["project_id"]),
                             row["work_item_id"] if "work_item_id" in row else row["id"])
        for selection in self.plane.selections():
            if selection["project_id"] and selection["mission_ref"]:
                mission = self.store.get(selection["mission_ref"])
                self.assertEqual(mission["project_id"], selection["project_id"])

    def test_nothing_secret_shaped_reached_durable_supervisor_state(self):
        with self.store.transaction() as db:
            tables = [row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE 'supervisor_%'")]
            for table in tables:
                for row in db.execute("SELECT * FROM %s" % table):
                    text = json.dumps({key: row[key] for key in row.keys()},
                                      default=str)
                    self.assertIsNone(LEAK_PATTERN.search(text),
                                      "%s holds a secret-shaped value" % table)

    def test_no_cycle_exceeded_its_own_ceiling(self):
        """The hot-loop check: a ceiling that only holds on average is not one."""

        for entry in self.trace:
            self.assertLessEqual(len(entry["advanced"]), 2,
                                 "hour %d advanced %d missions"
                                 % (entry["hour"], len(entry["advanced"])))
            self.assertLessEqual(len(entry["promoted"]), len(PROJECTS) * 2)

    def test_the_owner_pause_held_for_its_whole_window(self):
        paused = [entry for entry in self.trace
                  if PAUSE_TICK <= entry["tick"] < RESUME_TICK]
        self.assertTrue(paused)
        for entry in paused:
            self.assertEqual(entry["control_state"], "paused")
            self.assertEqual(entry["outcome"], "idle")
            self.assertEqual(entry["advanced"], [])
            self.assertEqual(entry["promoted"], [])

    def test_work_resumed_after_the_pause(self):
        after = [entry for entry in self.trace if entry["tick"] >= RESUME_TICK]
        self.assertTrue(any(entry["advanced"] for entry in after))

    def test_the_quiet_window_held_for_the_project_that_declared_one(self):
        for entry in self.trace:
            inside = QUIET_WINDOW[0] <= entry["clock_hour"] < QUIET_WINDOW[1]
            if inside:
                continue
            for row in entry["advanced"] + entry["promoted"]:
                self.assertNotEqual(row.get("project_id"), "gamma",
                                    "gamma ran at hour %d" % entry["clock_hour"])

    def test_the_quiet_window_did_not_simply_stop_that_project(self):
        """A window that closed forever would pass the test above."""

        self.assertTrue(any(row["project_id"] == "gamma"
                            for entry in self.trace for row in entry["advanced"]))

    def test_the_provider_outage_was_recorded_and_did_not_spin(self):
        outage = [entry for entry in self.trace
                  if entry["hour"] in OUTAGE_HOURS]
        self.assertTrue(outage)
        infrastructure = [row for entry in outage for row in entry["advanced"]
                          if row["classification"] == "infrastructure"]
        self.assertTrue(infrastructure)
        for entry in outage:
            self.assertLessEqual(len(entry["advanced"]), 2)

    def test_work_continued_after_the_outage_ended(self):
        after = [entry for entry in self.trace
                 if entry["hour"] == max(OUTAGE_HOURS) + 1]
        self.assertTrue(any(row["classification"] == "progressed"
                            for entry in after for row in entry["advanced"]))

    def test_budget_pressure_stopped_the_project_it_applied_to(self):
        """`delta` alone has a ceiling, and it alone is refused for spend."""

        spend = self.store.portfolio_economics("delta")["projects"][0]["provider_spend"]
        self.assertGreaterEqual(spend["known_spend"], 0.05)
        self.assertEqual(spend["currency"], "USD")
        verdicts = {verdict["reason"]
                    for row in self.store.coordination(limit=1_000_000)
                    for verdict in row["detail"].get("considered", [])}
        self.assertIn("PROJECT_BUDGET_EXHAUSTED", verdicts)
        for project_id in ("alpha", "beta", "gamma"):
            group = self.store.portfolio_economics(project_id)["projects"][0]
            # Nothing was priced for these, so nothing is summed.  An unpriced
            # leg stays unpriced rather than becoming a zero.
            self.assertEqual(group["provider_spend"]["known_spend"], "not_measurable")

    def test_incidents_became_ordinary_repair_missions(self):
        plane = maintenance.MaintenancePlane(self.store, self.ledger)
        repairs = plane.repairs()
        self.assertGreaterEqual(len(repairs), len(INCIDENT_TICKS))
        promoted = [row for row in repairs if row["mission_ref"]]
        self.assertTrue(promoted)
        for row in promoted:
            mission = self.store.get(row["mission_ref"])
            self.assertEqual(mission["payload"]["origin"], "maintenance_trigger")
            self.assertEqual(mission["payload"]["capability"], "bug")

    def test_a_failed_staging_deployment_became_a_repair_too(self):
        plane = maintenance.MaintenancePlane(self.store, self.ledger)
        kinds = {row["trigger_class"] for row in plane.repairs()}
        self.assertIn("deployment_health_failure", kinds)
        self.assertIn("production_incident", kinds)

    def test_a_failing_acceptance_gate_escalated_instead_of_retrying_forever(self):
        escalated = [row for row in self.missions() if row["state"] == "escalated"]
        self.assertTrue(escalated, "the run has to exercise a failing gate")
        for row in escalated:
            self.assertEqual(self.store.get(row["id"])["attempt_count"], 1)

    def test_the_final_brief_reads_the_whole_run_from_durable_state(self):
        brief = self.final_brief
        self.assertEqual(brief["control"]["state"], "running")
        self.assertEqual(brief["cycles_recorded"], TICKS)
        self.assertEqual(len(brief["policies"]), len(PROJECTS))
        self.assertTrue(brief["recently_completed"])

    def test_the_whole_run_happened_without_an_advisory_service_or_a_gateway(self):
        """Asserted during the run by a socket that refuses; restated here.

        The absence is structural rather than configured: no mission declared a
        gateway candidate and no advisor was ever constructed, so "deterministic
        operation works with both absent" is the only mode this run had.
        """

        for row in self.missions():
            self.assertNotIn("model_gateway", row["payload"])
        self.assertTrue(self.layer.dispatch_keys)


if __name__ == "__main__":
    unittest.main()
