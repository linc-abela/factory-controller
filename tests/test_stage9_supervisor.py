"""Stage 9: what an always-on Factory may and may not cause to happen.

Written the same way as `test_stage7_maintenance.py` and
`test_stage8_improvement.py`: each test states the thing that must remain
impossible, and the happy paths exist to prove the impossible ones are not
impossible by accident.  A supervisor that refused every cycle would pass every
safety test here and be worthless, so the flows at the bottom run real backlog,
repair and experiment work end to end through the ordinary execution path.

The property this file exists to hold has three halves.

**It has to stop.**  One invocation performs one finite cycle and returns.
There is no sleep, no loop on a constant and no path from `cycle` back to
`cycle`, and those are checked structurally rather than by running it and
hoping -- a runaway is exactly the failure a timed test would not catch.

**It cannot become an author.**  Every input to selection is a durable row an
earlier stage admitted against a recorded fact.  The supervisor may call three
methods on the maintenance plane, two on the improvement plane, and *none at
all* on the production ledger; that allowlist is pinned by an AST walk, so a
later edit that reached for `approve` fails here rather than in production.

**It cannot lose work or do it twice.**  Overlapping invocations refuse rather
than queue, an abandoned cycle is settled on the next claim, and a cycle that
died with a provider process possibly still running admits nothing new until
those missions settle.
"""

from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import (improvement, maintenance, portfolio, production,
                                supervisor)
from factory_controller.cli import main as cli_main
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, BETA, Clock, LayerAdapter, ProcessDeath
from tests.test_authority_boundaries import code_text

MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "supervisor.py"

SHA = "a" * 40
NEXT_SHA = "b" * 40
PROJECT = "shop"
REPO = "https://example.invalid/shop.git"
GATES = ["G-BUILD"]
CANDIDATES = [{"profile": ALPHA, "capabilities": ["implement"]},
              {"profile": BETA, "capabilities": ["implement"]}]
SURFACES = {name: ("protected/%s/" % name,) for name in improvement.MANDATORY_SURFACES}


def staging(project_id=PROJECT, environment_id=None, repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id or (project_id + "-staging"),
        project_id=project_id, environment_class="staging",
        repository=repository, service_ref=project_id + "-web",
        approver_refs=("owner",), autonomous=True)


def gated(project_id=PROJECT, environment_id=None, repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id or (project_id + "-prod"),
        project_id=project_id, environment_class="production",
        repository=repository, service_ref=project_id + "-web",
        approver_refs=("owner", "deputy"))


def _sha(seed: str) -> str:
    """A distinct 40-hex release identity per fixture, derived not chosen."""

    return hashlib.sha1(seed.encode()).hexdigest()


def objective(project_id=PROJECT, objective_ref="OBJ-1"):
    return improvement.Objective(
        objective_ref=objective_ref, project_id=project_id,
        improvement_class="performance",
        statement="checkout should answer faster without losing tests",
        metrics=(improvement.Metric("p95_latency_ms", "decrease", "objective",
                                    min_delta_ratio=0.10),
                 improvement.Metric("passing_tests", "increase", "non_regression",
                                    tolerance_ratio=0.0)),
        objective_version="1.0")


class SupervisorCase(unittest.TestCase):
    """A store on a hand-wound clock, a Controller, and a supervisor over both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.db"
        self.clock = Clock()
        self.store = MissionStore(str(self.path), clock=self.clock)
        self.adapter = LayerAdapter()
        self.controller = Controller(
            self.store, self.adapter,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=5)
        self.ledger = production.ProductionLedger(self.store)
        self.plane = supervisor.OperationsSupervisor(self.controller, clock=self.clock)

    # -- fixtures ---------------------------------------------------------- #

    def project(self, project_id=PROJECT, *, state="enabled", priority=100,
                repository=None, **extra):
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=project_id, repository=repository or ("repo://" + project_id),
            state=state, priority=priority, concurrency_cap=4,
            acceptance_gate_ids=extra.pop("acceptance_gate_ids", ("suite", "evaluate")),
            acceptance_gate_source=extra.pop(
                "acceptance_gate_source", "repo://%s@baseline:dev" % project_id),
            policy_version="1.0", **extra))
        return project_id

    def policy(self, project_id=PROJECT, **overrides):
        values = {"project_id": project_id, "policy_version": "sp-1"}
        values.update(overrides)
        return self.plane.set_policy(supervisor.SupervisorPolicy(**values))

    def running(self):
        return self.plane.transition("running", actor="owner", reason="start")

    def backlog(self, key, project_id=PROJECT, **extra):
        payload = {"work_item_id": key, "project_id": project_id,
                   "execution_mode": "fixture", "acceptance_gate_ids": GATES,
                   "provider_candidates": CANDIDATES}
        payload.update(extra)
        mission, _ = self.controller.submit(payload, key)
        return mission["id"]

    def repair(self, project_id=PROJECT, incident_ref="INC-1", repository=None,
               concurrency=4, release_sha=None):
        """A repair admitted from a real recorded production incident.

        The release differs per incident by default.  Stage 7 makes two
        incidents on the same release *the same failure* and stops the second
        at the attempt ceiling, which is correct there and would silently
        halve every fixture here.
        """

        repository = repository or ("repo://" + project_id)
        env = staging(project_id, repository=repository)
        try:
            self.ledger.register_environment(env)
        except production.ProductionRefusal:
            pass
        maint = maintenance.MaintenancePlane(self.store, self.ledger)
        maint.set_policy(maintenance.MaintenancePolicy(
            project_id=project_id, enabled=True, cooldown_seconds=0,
            concurrency=concurrency, policy_version="mp-1"))
        self.ledger.declare_incident(
            incident_ref=incident_ref, environment_id=env.environment_id,
            declared_by="owner", incident_class="triaged_defect",
            affected_release_sha=release_sha or _sha(incident_ref),
            affected_bundle_ref="rc-000", failing_behaviour="checkout 500s",
            blast_radius="all checkout traffic")
        return maint.admit_trigger("production_incident", incident_ref)["trigger_ref"]

    def experiment(self, project_id=PROJECT, objective_ref="OBJ-1",
                   *, baseline=True, repository=None):
        """An experiment admitted against an Owner objective, baseline pinned."""

        repository = repository or ("repo://" + project_id)
        imp = improvement.ImprovementPlane(self.store, self.ledger)
        imp.set_policy(improvement.ImprovementPolicy(
            project_id=project_id, enabled=True, cooldown_seconds=0,
            protected_surfaces=SURFACES, policy_version="ip-1"))
        obj = objective(project_id, objective_ref)
        imp.register_objective(obj)
        row = imp.admit_experiment(
            obj.objective_ref, "owner_objective", obj.objective_ref,
            target_repository=repository, baseline_sha=SHA,
            isolation_ref="lane://%s/experiment-1" % project_id)
        if baseline:
            imp.record_baseline(row["experiment_ref"],
                                {"p95_latency_ms": 400.0, "passing_tests": 120})
        return row["experiment_ref"]

    def drain_cycles(self, worker="w", limit=20):
        """Run cycles until one advances nothing.  Bounded on purpose."""

        reports = []
        for index in range(limit):
            report = self.plane.cycle("%s-%d" % (worker, index))
            reports.append(report)
            if not report["advanced"] and not report["promoted"]:
                break
        return reports


# --------------------------------------------------------------------------- #
# 1. it has to stop
# --------------------------------------------------------------------------- #

def _self_calls(tree: ast.AST) -> dict[str, set[str]]:
    """For each method, the names it invokes on ``self``."""

    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = set()
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "self"):
                called.add(inner.func.attr)
        graph.setdefault(node.name, set()).update(called)
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        for called in graph.get(name, ()):
            if called not in seen:
                seen.add(called)
                stack.append(called)
    return seen


def _plane_calls(tree: ast.AST, attribute: str) -> set[str]:
    """Every method name invoked on ``self.<attribute>`` anywhere in the module."""

    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == attribute
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"):
            names.add(node.func.attr)
    return names


class TerminationTests(unittest.TestCase):
    """A cycle that could not end would make every other guarantee unprovable."""

    def setUp(self):
        self.text = MODULE.read_text()
        self.tree = ast.parse(self.text)

    def test_the_module_never_sleeps(self):
        self.assertNotIn("sleep", code_text(self.text))

    def test_no_loop_runs_on_a_constant(self):
        """`while True` is the shape a supervisor is most likely to grow."""

        for node in ast.walk(self.tree):
            if isinstance(node, ast.While):
                self.assertNotIsInstance(
                    node.test, ast.Constant,
                    "supervisor.py loops on a constant at line %d" % node.lineno)

    def test_a_cycle_cannot_reach_itself(self):
        """No recursion, direct or transitive, from the one verb that acts."""

        self.assertNotIn("cycle", _reachable(_self_calls(self.tree), "cycle"))

    def test_the_scan_would_catch_a_supervisor_that_called_itself(self):
        """A termination check that can never fire is not a check."""

        planted = ast.parse("class S:\n"
                            "    def cycle(self):\n"
                            "        self._again()\n"
                            "    def _again(self):\n"
                            "        self.cycle()\n")
        self.assertIn("cycle", _reachable(_self_calls(planted), "cycle"))

    def test_one_invocation_records_exactly_one_cycle(self):
        case = SupervisorCase("run")
        case.setUp()
        case.project()
        case.policy()
        case.running()
        case.plane.cycle("w1")
        case.plane.cycle("w2")
        self.assertEqual([row["sequence"] for row in case.plane.cycles()], [2, 1])


class AuthorityBoundaryTests(unittest.TestCase):
    """What the supervisor may call is a list, not a habit."""

    def setUp(self):
        self.tree = ast.parse(MODULE.read_text())

    def test_the_maintenance_plane_is_reached_by_exactly_three_methods(self):
        self.assertEqual(
            _plane_calls(self.tree, "_maintenance"),
            {"repairs", "create_repair_mission", "record_mission_outcome"})

    def test_the_improvement_plane_is_reached_by_exactly_two_methods(self):
        self.assertEqual(_plane_calls(self.tree, "_improvement"),
                         {"experiments", "create_candidate_mission"})

    def test_the_production_ledger_is_never_called_at_all(self):
        """The strongest form of "the supervisor cannot deploy".

        It holds a ledger only because the two planes require one at
        construction.  Nothing in this module invokes a single method on it, so
        there is no approval, admission, deployment or rollback verb to reach --
        not one that is refused, one that does not exist.
        """

        self.assertEqual(_plane_calls(self.tree, "_ledger"), set())

    def test_the_controller_is_reached_only_to_run_the_ordinary_path(self):
        self.assertEqual(_plane_calls(self.tree, "_controller"), {"work_once"})

    def test_a_cycle_cannot_reach_any_verb_that_changes_authority(self):
        """Owner verbs exist on this class; a cycle may not call them."""

        reached = _reachable(_self_calls(self.tree), "cycle")
        for verb in ("set_policy", "set_enabled", "transition", "hold",
                     "clear_health"):
            self.assertNotIn(verb, reached,
                             "a cycle can reach %s()" % verb)

    def test_the_module_declares_no_approval_or_promotion_vocabulary(self):
        text = MODULE.read_text()
        for token in ("def approve", "def deploy", "def rollback",
                      "def promote_to", "def register_environment",
                      "def widen"):
            self.assertNotIn(token, text)

    def test_every_absence_word_matches_the_other_five_layers(self):
        """Six forks across the corpus, one of them a typo inside a check."""

        for other in (production.CANONICAL_ABSENCE, maintenance.CANONICAL_ABSENCE,
                      improvement.CANONICAL_ABSENCE):
            self.assertEqual(supervisor.CANONICAL_ABSENCE, other)
        self.assertEqual(supervisor.CANONICAL_ABSENCE,
                         frozenset({"unknown", "not_applicable", "not_run",
                                    "not_measurable"}))


# --------------------------------------------------------------------------- #
# 2. control state
# --------------------------------------------------------------------------- #

class ControlStateTests(SupervisorCase):

    def test_a_new_supervisor_is_stopped_and_says_why(self):
        control = self.plane.control()
        self.assertEqual(control["state"], "stopped")
        self.assertEqual(control["actor"], "not_applicable")

    def test_a_stopped_supervisor_runs_a_recorded_cycle_that_does_nothing(self):
        self.project()
        self.policy()
        self.backlog("W1")
        report = self.plane.cycle("w1")
        self.assertEqual(report["outcome"], "idle")
        self.assertEqual(report["reason"], "SUPERVISOR_STOPPED")
        self.assertEqual(report["missions_advanced"], 0)
        self.assertEqual(self.store.get(self.backlog("W2"))["state"], "admitted")

    def test_a_transition_outside_the_table_is_refused(self):
        with self.assertRaises(supervisor.SupervisorRefusal) as raised:
            self.plane.transition("paused", actor="owner", reason="skip a step")
        self.assertEqual(raised.exception.code, "SUPERVISOR_TRANSITION_REFUSED")

    def test_a_transition_records_who_asked_and_why(self):
        self.running()
        self.plane.transition("paused", actor="owner", reason="deploy freeze",
                              evidence_ref="notes/freeze")
        row = self.plane.transitions()[-1]
        self.assertEqual((row["from_state"], row["to_state"]), ("running", "paused"))
        self.assertEqual(row["actor"], "owner")
        self.assertEqual(row["evidence_ref"], "notes/freeze")

    def test_a_transition_with_no_actor_or_reason_is_refused(self):
        for actor, reason in (("", "why"), ("owner", "")):
            with self.assertRaises(supervisor.PolicyError):
                self.plane.transition("running", actor=actor, reason=reason)

    def test_an_emergency_stop_engages_the_stage_five_portfolio_stop(self):
        """A supervisor-local flag would be a stop the other planes never read."""

        self.running()
        self.plane.transition("emergency_stopped", actor="owner", reason="incident")
        self.assertTrue(self.store.portfolio_policy().emergency_stop)

    def test_only_one_transition_leaves_an_emergency_stop(self):
        self.running()
        self.plane.transition("emergency_stopped", actor="owner", reason="incident")
        for target in ("running", "paused", "draining"):
            with self.assertRaises(supervisor.SupervisorRefusal):
                self.plane.transition(target, actor="owner", reason="resume")
        self.plane.transition("stopped", actor="owner", reason="cleared")
        self.assertFalse(self.store.portfolio_policy().emergency_stop)

    def test_an_emergency_stopped_cycle_advances_nothing(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.running()
        self.plane.transition("emergency_stopped", actor="owner", reason="incident")
        report = self.plane.cycle("w1")
        self.assertEqual(report["outcome"], "idle")
        self.assertEqual(report["missions_advanced"], 0)

    def test_control_state_survives_a_replacement_process(self):
        self.running()
        replacement = supervisor.OperationsSupervisor(
            Controller(MissionStore(str(self.path), clock=self.clock), self.adapter),
            clock=self.clock)
        self.assertEqual(replacement.control()["state"], "running")

    def test_a_hold_is_the_stage_five_project_state_and_nothing_new(self):
        self.project()
        self.plane.hold(PROJECT)
        self.assertEqual(self.store.project(PROJECT).state, "paused")
        self.plane.hold(PROJECT, held=False)
        self.assertEqual(self.store.project(PROJECT).state, "enabled")


# --------------------------------------------------------------------------- #
# 3. policy
# --------------------------------------------------------------------------- #

class PolicyTests(SupervisorCase):

    def test_an_unregistered_project_cannot_be_supervised(self):
        with self.assertRaises(supervisor.SupervisorRefusal) as raised:
            self.policy("ghost")
        self.assertEqual(raised.exception.code, "SUPERVISOR_PROJECT_UNREGISTERED")

    def test_an_empty_work_class_list_is_refused_rather_than_read_as_everything(self):
        self.project()
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, work_classes=())

    def test_an_unknown_work_class_is_refused(self):
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, work_classes=("deploy",))

    def test_a_cycle_that_may_advance_nothing_is_refused(self):
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, missions_per_cycle=0)

    def test_half_a_window_is_refused(self):
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, window_start_hour=2)

    def test_a_window_outside_the_clock_is_refused(self):
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, window_start_hour=2,
                                        window_end_hour=25)

    def test_a_suppression_threshold_below_one_is_refused(self):
        with self.assertRaises(supervisor.PolicyError):
            supervisor.SupervisorPolicy(project_id=PROJECT, failure_threshold=0)

    def test_a_policy_round_trips_through_the_database(self):
        self.project()
        self.policy(missions_per_cycle=7, window_start_hour=22, window_end_hour=6,
                    work_classes=("backlog", "maintenance"))
        stored = self.plane.policy(PROJECT)
        self.assertEqual(stored.missions_per_cycle, 7)
        self.assertEqual(stored.work_classes, ("backlog", "maintenance"))
        self.assertEqual(stored.window_start_hour, 22)

    def test_an_undeclared_window_is_open_rather_than_closed(self):
        """An undeclared constraint must never behave like a closed gate."""

        self.assertTrue(supervisor.within_window(0.0, None, None))

    def test_a_window_wraps_across_midnight(self):
        night = (22, 6)
        hours = {hour: supervisor.within_window(hour * 3600.0, *night)
                 for hour in (0, 5, 6, 12, 21, 22, 23)}
        self.assertEqual([hour for hour, inside in hours.items() if inside],
                         [0, 5, 22, 23])

    def test_a_project_outside_its_window_is_passed_over_with_a_reason(self):
        self.project()
        hour = int(self.clock.now // 3600) % 24
        self.policy(window_start_hour=(hour + 2) % 24, window_end_hour=(hour + 3) % 24)
        self.backlog("W1")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["missions_advanced"], 0)
        self.assertIn("SUPERVISOR_OUTSIDE_EXECUTION_WINDOW",
                      [row["reason"] for row in report["refused"]])


# --------------------------------------------------------------------------- #
# 4. selection: only already-authorized work
# --------------------------------------------------------------------------- #

class WorkSelectionTests(SupervisorCase):

    def test_a_cycle_advances_ordinary_backlog_through_the_existing_path(self):
        self.project()
        self.policy()
        mission_id = self.backlog("W1")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(self.store.get(mission_id)["state"], "completed")
        self.assertEqual(report["advanced"][0]["mission_id"], mission_id)
        self.assertTrue(self.adapter.dispatches)

    def test_an_admitted_repair_becomes_an_ordinary_mission(self):
        self.project()
        self.policy()
        trigger = self.repair()
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual([row["work_class"] for row in report["promoted"]],
                         ["maintenance"])
        mission = self.store.get(report["promoted"][0]["mission_ref"])
        self.assertEqual(mission["payload"]["origin"], "maintenance_trigger")
        self.assertEqual(mission["payload"]["trigger_ref"], trigger)

    def test_an_experiment_with_a_pinned_baseline_becomes_an_ordinary_mission(self):
        self.project()
        self.policy()
        ref = self.experiment()
        self.running()
        report = self.plane.cycle("w1")
        promoted = [row for row in report["promoted"] if row["work_class"] == "improvement"]
        self.assertEqual([row["work_ref"] for row in promoted], [ref])

    def test_an_experiment_without_a_baseline_is_never_promoted(self):
        """Stage 8 refuses a candidate before a baseline; so does the selector."""

        self.project()
        self.policy()
        self.experiment(baseline=False)
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual([row for row in report["promoted"]
                          if row["work_class"] == "improvement"], [])

    def test_a_closed_repair_is_never_promoted(self):
        self.project()
        self.policy()
        trigger = self.repair()
        maintenance.MaintenancePlane(self.store, self.ledger).close(
            trigger, "abandoned", reason="owner")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["promoted"], [])

    def test_a_work_class_the_project_did_not_admit_is_not_promoted(self):
        self.project()
        self.policy(work_classes=("backlog",))
        self.repair()
        self.experiment()
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["promoted"], [])

    def test_the_admission_ceiling_bounds_one_cycle(self):
        self.project()
        self.policy(maintenance_admissions=1)
        self.repair(incident_ref="INC-1")
        self.repair(incident_ref="INC-2")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(len(report["promoted"]), 1)
        self.assertIn("SUPERVISOR_CLASS_ADMISSION_CEILING",
                      [row["reason"] for row in report["refused"]])

    def test_no_method_here_takes_a_sentence_a_model_could_write(self):
        """The whole input surface of a cycle is a worker name and a lease.

        There is no argument anywhere on `cycle` into which a proposal, a
        telemetry reading, an alert or a provider's output could be poured, so
        a model cannot become a source of work by being persuasive.
        """

        import inspect
        parameters = inspect.signature(supervisor.OperationsSupervisor.cycle).parameters
        self.assertEqual(list(parameters), ["self", "worker_id", "lease_seconds"])

    def test_a_mission_belonging_to_no_project_is_out_of_scope(self):
        """No Owner priority, cap or budget -- the reason `set_policy` refuses one.

        The Stage-5 scheduler still runs it for an operator who asks by hand;
        it is only the *unattended* path that will not pick up work nobody
        placed under a policy.
        """

        self.project()
        self.policy()
        mission, _ = self.controller.submit(
            {"work_item_id": "ORPHAN", "execution_mode": "fixture",
             "acceptance_gate_ids": GATES}, "ORPHAN")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["missions_advanced"], 0)
        self.assertEqual(self.store.get(mission["id"])["state"], "admitted")
        self.assertIsNotNone(self.store.claim("by-hand"))

    def test_a_resume_survives_a_scope_its_project_is_no_longer_inside(self):
        """Half-finished work finishes even when its window has closed."""

        self.project()
        self.policy()
        self.running()
        mission_id = self.backlog("W1")
        crashing = LayerAdapter(crash_on="verify")
        crashed = Controller(self.store, crashing,
                             retry_policy=RetryPolicy(max_attempts=1,
                                                      base_delay_seconds=0),
                             lease_seconds=0)
        with self.assertRaises(ProcessDeath):
            crashed.work_once("dead-worker")
        self.store.recover_stale()
        claimed = self.store.claim("scoped", project_ids=("somewhere-else",))
        self.assertEqual(claimed["id"], mission_id)

    def test_a_paused_project_receives_no_unattended_work(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.repair()
        self.running()
        self.plane.hold(PROJECT)
        report = self.plane.cycle("w1")
        self.assertEqual(report["promoted"], [])
        self.assertEqual(report["missions_advanced"], 0)
        self.assertIn("SUPERVISOR_PROJECT_NOT_ADMITTING",
                      [row["reason"] for row in report["refused"]])

    def test_a_disabled_policy_receives_no_unattended_work(self):
        self.project()
        self.policy()
        self.plane.set_enabled(PROJECT, False)
        self.backlog("W1")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["missions_advanced"], 0)
        self.assertIn("SUPERVISOR_PROJECT_DISABLED",
                      [row["reason"] for row in report["refused"]])

    def test_a_settled_repair_mission_writes_its_outcome_back_to_the_lineage(self):
        self.project()
        self.policy()
        trigger = self.repair()
        self.running()
        self.drain_cycles()
        lineage = maintenance.MaintenancePlane(self.store, self.ledger).lineage(trigger)
        self.assertEqual(lineage["state"], "candidate_validated")
        self.assertEqual(lineage["candidate_sha"], "a" * 40)


# --------------------------------------------------------------------------- #
# 5. exactly once
# --------------------------------------------------------------------------- #

class ExactlyOnceTests(SupervisorCase):

    def test_an_overlapping_invocation_is_refused_rather_than_queued(self):
        self.project()
        self.policy()
        self.running()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_cycles (cycle_id,sequence,control_state,"
                "worker_id,lease_expires_at,started_at) VALUES ('open',99,'running',"
                "'ghost',?,?)", (self.clock.now + 100, self.clock.now))
        with self.assertRaises(supervisor.SupervisorRefusal) as raised:
            self.plane.cycle("w1")
        self.assertEqual(raised.exception.code, "SUPERVISOR_CYCLE_IN_FLIGHT")

    def test_an_abandoned_cycle_with_no_in_flight_work_is_replayable(self):
        self.project()
        self.policy()
        self.running()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_cycles (cycle_id,sequence,control_state,"
                "worker_id,lease_expires_at,started_at) VALUES ('dead',99,'running',"
                "'ghost',?,?)", (self.clock.now - 1, self.clock.now - 100))
        report = self.plane.cycle("w1")
        self.assertEqual(report["recovered"],
                         [{"cycle_id": "dead", "outcome": "recovered_replayable",
                           "prior_worker": "ghost"}])

    def test_an_abandoned_cycle_with_work_past_the_boundary_is_uncertain(self):
        """Fail closed: a provider process may still have been running."""

        self.project()
        self.policy()
        self.running()
        mission_id = self.backlog("W1")
        crashing = LayerAdapter(crash_on="verify")
        crashed = Controller(self.store, crashing,
                             retry_policy=RetryPolicy(max_attempts=1,
                                                      base_delay_seconds=0),
                             lease_seconds=0)
        with self.assertRaises(ProcessDeath):
            crashed.work_once("dead-worker")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO supervisor_cycles (cycle_id,sequence,control_state,"
                "worker_id,lease_expires_at,started_at) VALUES ('dead',99,'running',"
                "'ghost',?,?)", (self.clock.now - 1, self.clock.now - 100))
        report = self.plane.cycle("w1")
        self.assertEqual(report["recovered"][0]["outcome"], "recovered_uncertain")
        self.assertEqual(report["uncertain_missions"], [mission_id])

    def test_uncertain_work_in_flight_stops_a_cycle_promoting_anything_new(self):
        self.project()
        self.policy()
        self.repair()
        self.running()
        self.backlog("W1")
        crashing = LayerAdapter(crash_on="verify")
        crashed = Controller(self.store, crashing,
                             retry_policy=RetryPolicy(max_attempts=1,
                                                      base_delay_seconds=0),
                             lease_seconds=0)
        with self.assertRaises(ProcessDeath):
            crashed.work_once("dead-worker")
        report = self.plane.cycle("w1")
        self.assertEqual(report["promoted"], [])
        self.assertIn("SUPERVISOR_UNCERTAIN_WORK_IN_FLIGHT",
                      [row["reason"] for row in report["refused"]])

    def test_a_crash_mid_cycle_resumes_the_same_mission_and_dispatches_once(self):
        """The duplicate-irreversible-effect count, measured rather than argued."""

        self.project()
        self.policy()
        self.running()
        mission_id = self.backlog("W1")
        shared = LayerAdapter(crash_on="verify")
        crashed = Controller(self.store, shared,
                             retry_policy=RetryPolicy(max_attempts=1,
                                                      base_delay_seconds=0),
                             lease_seconds=0)
        with self.assertRaises(ProcessDeath):
            crashed.work_once("dead-worker")
        replacement = supervisor.OperationsSupervisor(
            Controller(MissionStore(str(self.path), clock=self.clock), shared,
                       retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
                       lease_seconds=5),
            clock=self.clock)
        for index in range(4):
            replacement.cycle("replacement-%d" % index)
        self.assertEqual(self.store.get(mission_id)["state"], "completed")
        # The irreversible act is the provider invocation, and there was one.
        # The recovery leg beside it is a *read* of the memoized step output --
        # `begin_step` returned the recorded result without calling the adapter
        # again -- so counting run legs would have counted the proof as a
        # duplicate of the thing it proves.
        self.assertEqual(len(shared.dispatches), 1)
        self.assertEqual([leg["selection_reason"] for leg in self.store.runs(mission_id)],
                         ["first_admissible", "recover_existing_result"])

    def test_repeated_cycles_never_promote_one_repair_twice(self):
        self.project()
        self.policy()
        trigger = self.repair()
        self.running()
        reports = self.drain_cycles()
        promoted = [row for report in reports for row in report["promoted"]
                    if row["work_ref"] == trigger]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(len([m for m in self.store.all_missions()
                              if m["id"] == promoted[0]["mission_ref"]]), 1)

    def test_the_cycle_reference_is_derived_so_a_replay_recomputes_it(self):
        first = supervisor.cycle_reference(None, 1, "w", 100.0)
        self.assertEqual(first, supervisor.cycle_reference(None, 1, "w", 100.0))
        self.assertNotEqual(first, supervisor.cycle_reference("cyc_x", 1, "w", 100.0))

    def test_the_cycle_chain_names_its_predecessor(self):
        self.project()
        self.policy()
        self.running()
        self.plane.cycle("w1")
        self.plane.cycle("w2")
        rows = sorted(self.plane.cycles(), key=lambda row: row["sequence"])
        self.assertIsNone(rows[0]["previous_cycle_id"])
        self.assertEqual(rows[1]["previous_cycle_id"], rows[0]["cycle_id"])


# --------------------------------------------------------------------------- #
# 6. drain
# --------------------------------------------------------------------------- #

class DrainTests(SupervisorCase):

    def test_a_drain_finishes_in_flight_work_and_starts_none(self):
        self.project()
        self.policy()
        self.running()
        in_flight = self.backlog("W1")
        fresh = self.backlog("W2")
        crashing = LayerAdapter(crash_on="verify")
        crashed = Controller(self.store, crashing,
                             retry_policy=RetryPolicy(max_attempts=1,
                                                      base_delay_seconds=0),
                             lease_seconds=0)
        with self.assertRaises(ProcessDeath):
            crashed.work_once("dead-worker")
        self.plane.transition("draining", actor="owner", reason="host restart")
        for index in range(4):
            self.plane.cycle("drain-%d" % index)
        self.assertEqual(self.store.get(in_flight)["state"], "completed")
        self.assertEqual(self.store.get(fresh)["state"], "admitted")

    def test_a_drain_promotes_nothing(self):
        self.project()
        self.policy()
        self.repair()
        self.running()
        self.plane.transition("draining", actor="owner", reason="host restart")
        report = self.plane.cycle("w1")
        self.assertEqual(report["promoted"], [])

    def test_a_drained_claim_reuses_the_schedulers_own_definition_of_in_flight(self):
        """A second definition would eventually abandon half-finished work."""

        self.project()
        self.policy()
        self.backlog("W1")
        self.assertIsNone(self.store.claim("w", resume_only=True))
        self.assertIsNotNone(self.store.claim("w"))

    def test_a_resume_only_claim_ignores_a_paused_project_the_same_way(self):
        self.project()
        self.backlog("W1")
        self.store.claim("w1", lease_seconds=0)
        self.store.recover_stale()
        self.store.set_project_state(PROJECT, "paused")
        self.assertIsNone(self.store.claim("w2", resume_only=True))


# --------------------------------------------------------------------------- #
# 7. provider readiness, budgets and bounded suppression
# --------------------------------------------------------------------------- #

class ReadinessTests(SupervisorCase):

    def unavailable_controller(self):
        adapter = LayerAdapter(proven_unavailable={ALPHA, BETA})
        return adapter, Controller(
            self.store, adapter,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=5)

    def test_an_unavailable_provider_is_an_infrastructure_fact_not_a_verdict(self):
        adapter, controller = self.unavailable_controller()
        plane = supervisor.OperationsSupervisor(controller, clock=self.clock)
        self.project()
        plane.set_policy(supervisor.SupervisorPolicy(project_id=PROJECT))
        plane.transition("running", actor="owner", reason="start")
        self.backlog("W1")
        report = plane.cycle("w1")
        self.assertEqual(report["advanced"][0]["classification"], "infrastructure")
        self.assertEqual(report["advanced"][0]["code"], "NO_ADMISSIBLE_PROVIDER")

    def test_repeated_infrastructure_failure_suppresses_the_project(self):
        adapter, controller = self.unavailable_controller()
        plane = supervisor.OperationsSupervisor(controller, clock=self.clock)
        self.project()
        plane.set_policy(supervisor.SupervisorPolicy(
            project_id=PROJECT, failure_threshold=2, suppression_seconds=600))
        plane.transition("running", actor="owner", reason="start")
        for index in range(3):
            self.backlog("W%d" % index)
        plane.cycle("w1")
        health = {row["project_id"]: row for row in plane.health()}[PROJECT]
        self.assertGreaterEqual(health["consecutive_failures"], 2)
        self.assertIsNotNone(health["suppressed_until"])
        report = plane.cycle("w2")
        self.assertIn("SUPERVISOR_PROJECT_SUPPRESSED",
                      [row["reason"] for row in report["refused"]])

    def test_a_suppressed_project_costs_one_skip_and_no_retry_storm(self):
        adapter, controller = self.unavailable_controller()
        plane = supervisor.OperationsSupervisor(controller, clock=self.clock)
        self.project()
        plane.set_policy(supervisor.SupervisorPolicy(
            project_id=PROJECT, failure_threshold=1, suppression_seconds=600,
            missions_per_cycle=2))
        plane.transition("running", actor="owner", reason="start")
        for index in range(6):
            self.backlog("W%d" % index)
        advanced = sum(plane.cycle("w%d" % index)["missions_advanced"]
                       for index in range(10))
        # Two missions in the first cycle, then nothing: ten invocations of a
        # project whose provider is gone cost one cycle's ceiling in total.
        self.assertEqual(advanced, 2)

    def test_progress_clears_a_partial_run_of_failures(self):
        """A single flap must not cost a project its next cycle."""

        adapter = LayerAdapter(proven_unavailable={ALPHA, BETA})
        controller = Controller(self.store, adapter,
                                retry_policy=RetryPolicy(max_attempts=1,
                                                         base_delay_seconds=0),
                                lease_seconds=5)
        plane = supervisor.OperationsSupervisor(controller, clock=self.clock)
        self.project()
        plane.set_policy(supervisor.SupervisorPolicy(
            project_id=PROJECT, failure_threshold=3, missions_per_cycle=1))
        plane.transition("running", actor="owner", reason="start")
        self.backlog("W1")
        plane.cycle("w1")
        adapter.proven_unavailable = set()
        self.backlog("W2")
        plane.cycle("w2")
        health = {row["project_id"]: row for row in plane.health()}[PROJECT]
        self.assertEqual(health["consecutive_failures"], 0)

    def test_an_escalated_project_waits_for_the_owner(self):
        adapter, controller = self.unavailable_controller()
        plane = supervisor.OperationsSupervisor(controller, clock=self.clock)
        self.project()
        plane.set_policy(supervisor.SupervisorPolicy(
            project_id=PROJECT, failure_threshold=1, suppression_seconds=0,
            missions_per_cycle=4))
        plane.transition("running", actor="owner", reason="start")
        for index in range(4):
            self.backlog("W%d" % index)
        plane.cycle("w1")
        health = {row["project_id"]: row for row in plane.health()}[PROJECT]
        self.assertTrue(health["escalated"])
        self.assertIn("SUPERVISOR_PROJECT_ESCALATED",
                      [row["reason"] for row in plane.cycle("w2")["refused"]])
        plane.clear_health(PROJECT, actor="owner")
        self.assertNotIn("SUPERVISOR_PROJECT_ESCALATED",
                         [row["reason"] for row in plane.cycle("w3")["refused"]])

    def test_a_spent_budget_is_a_policy_fact_and_never_suppresses(self):
        """Backing off from an Owner ceiling would hide it behind a timer."""

        self.assertEqual(
            supervisor.classify_outcome(
                {"state": "refused",
                 "terminal_reason": "MISSION_BUDGET_EXHAUSTED: known spend 5"}),
            ("ceiling", "MISSION_BUDGET_EXHAUSTED"))

    def test_a_retry_moves_the_counter_in_neither_direction(self):
        self.assertEqual(
            supervisor.classify_outcome({"state": "admitted",
                                         "terminal_reason": None})[0],
            "retrying")

    def test_an_exhausted_project_budget_stops_the_scheduler_not_the_supervisor(self):
        self.project(budget_ceiling=0.0, budget_currency="USD")
        self.policy()
        self.backlog("W1")
        self.running()
        report = self.plane.cycle("w1")
        self.assertEqual(report["missions_advanced"], 0)
        self.assertIn(report["refused"][-1]["reason"],
                      {"NO_ELIGIBLE_MISSION", "NO_RUNNABLE_MISSION"})


# --------------------------------------------------------------------------- #
# 8. portfolio fairness
# --------------------------------------------------------------------------- #

class FairnessTests(SupervisorCase):

    def four_projects(self, **policy):
        for name in ("alpha", "beta", "gamma", "delta"):
            self.project(name)
            self.policy(name, **policy)
        self.running()

    def test_promotion_rotates_so_one_busy_project_cannot_take_every_slot(self):
        self.four_projects(maintenance_admissions=1)
        for name in ("alpha", "beta"):
            for index in range(3):
                self.repair(name, incident_ref="INC-%s-%d" % (name, index))
        first = self.plane.cycle("w1")
        second = self.plane.cycle("w2")
        self.assertEqual({row["project_id"] for row in first["promoted"]},
                         {"alpha", "beta"})
        self.assertEqual({row["project_id"] for row in second["promoted"]},
                         {"alpha", "beta"})
        counts: dict[str, int] = {}
        for report in (first, second):
            for row in report["promoted"]:
                counts[row["project_id"]] = counts.get(row["project_id"], 0) + 1
        self.assertEqual(counts, {"alpha": 2, "beta": 2})

    def test_a_low_priority_project_is_not_starved_by_a_high_priority_one(self):
        """Stage-5 ageing, inherited rather than restated."""

        self.project("urgent", priority=1)
        self.project("patient", priority=900)
        self.policy("urgent")
        self.policy("patient", missions_per_cycle=1)
        self.running()
        patient = self.backlog("PATIENT-1", "patient")
        # 899 priority points at one step per 300s.  Stating the arithmetic
        # rather than a round number is the point: ageing is unbounded, so the
        # wait that overturns *any* gap is finite and computable.
        self.clock.advance(900 * portfolio.DEFAULT_AGING_SECONDS)
        for index in range(5):
            self.backlog("URGENT-%d" % index, "urgent")
        report = self.plane.cycle("w1")
        self.assertEqual(report["advanced"][0]["mission_id"], patient)

    def test_a_cycle_ceiling_is_the_largest_project_ceiling_not_their_sum(self):
        """Adding a project must never widen how much one cycle does."""

        self.four_projects(missions_per_cycle=2)
        for name in ("alpha", "beta", "gamma", "delta"):
            for index in range(3):
                self.backlog("%s-%d" % (name, index), name)
        report = self.plane.cycle("w1")
        self.assertEqual(report["missions_advanced"], 2)

    def test_one_projects_hold_does_not_hold_another(self):
        self.four_projects()
        for name in ("alpha", "beta"):
            self.backlog(name + "-1", name)
        self.plane.hold("alpha")
        report = self.plane.cycle("w1")
        self.assertEqual({row["project_id"] for row in report["advanced"]}, {"beta"})


# --------------------------------------------------------------------------- #
# 9. production and self-improvement containment
# --------------------------------------------------------------------------- #

class ContainmentTests(SupervisorCase):

    def test_no_cycle_creates_a_deployment_of_any_kind(self):
        self.project()
        self.policy()
        self.ledger.register_environment(staging())
        self.ledger.register_environment(gated())
        self.repair()
        self.experiment()
        self.backlog("W1")
        self.running()
        self.drain_cycles()
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) AS n FROM deployments").fetchone()["n"], 0)

    def test_a_gated_environment_never_receives_autonomous_work(self):
        """The environment is registered, reachable, and simply never used."""

        self.project()
        self.policy()
        self.ledger.register_environment(gated())
        self.experiment()
        self.running()
        self.drain_cycles()
        events = [row["kind"] for row in self.ledger.events(PROJECT)]
        self.assertNotIn("release_admitted", events)
        self.assertNotIn("release_approved", events)

    def test_a_completed_experiment_candidate_waits_for_an_owner_seal(self):
        """Sealing needs a producer identity and a change set.

        Neither is a fact this module can observe, so a supervisor that sealed
        one would be inventing the two inputs Stage 8's containment rests on.
        """

        self.project()
        self.policy()
        ref = self.experiment()
        self.running()
        self.drain_cycles()
        imp = improvement.ImprovementPlane(self.store, self.ledger)
        row = {item["experiment_ref"]: item for item in imp.experiments()}[ref]
        self.assertEqual(row["state"], "mission_created")
        self.assertIsNone(row["disposition"])

    def test_the_supervisor_never_changes_a_protected_surface_or_a_policy(self):
        self.project()
        self.policy()
        imp = improvement.ImprovementPlane(self.store, self.ledger)
        self.experiment()
        before = imp.policy(PROJECT).policy_digest
        self.running()
        self.drain_cycles()
        self.assertEqual(imp.policy(PROJECT).policy_digest, before)

    def test_a_cycle_never_changes_the_portfolio_or_project_policy(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.running()
        before = (self.store.portfolio_policy().as_row(),
                  self.store.project(PROJECT).as_row())
        self.drain_cycles()
        self.assertEqual((self.store.portfolio_policy().as_row(),
                          self.store.project(PROJECT).as_row()), before)

    def test_a_cycle_never_changes_its_own_supervisor_policy(self):
        self.project()
        self.policy(missions_per_cycle=1)
        for index in range(4):
            self.backlog("W%d" % index)
        self.running()
        self.drain_cycles()
        self.assertEqual(self.plane.policy(PROJECT).missions_per_cycle, 1)


# --------------------------------------------------------------------------- #
# 10. the Owner's surface
# --------------------------------------------------------------------------- #

class OwnerSurfaceTests(SupervisorCase):

    def test_the_brief_reads_durable_state_and_changes_nothing(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.running()
        self.plane.cycle("w1")
        before = self.store.counts()
        brief = self.plane.brief()
        self.assertEqual(self.store.counts(), before)
        self.assertEqual(brief["control"]["state"], "running")
        self.assertEqual(len(brief["recently_completed"]), 1)
        self.assertEqual(brief["cycles_recorded"], 1)

    def test_the_brief_names_what_is_waiting_and_why(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.backlog("W2")
        self.store.add_dependency(self.backlog("W3"), self.backlog("W4"))
        self.running()
        brief = self.plane.brief()
        self.assertTrue(any(row["reason"] == "DEPENDENCY_UNMET"
                            for row in brief["waiting"]))

    def test_the_brief_names_work_waiting_on_an_owner_act(self):
        self.project()
        self.policy()
        trigger = self.repair()
        self.running()
        self.drain_cycles()
        awaiting = self.plane.brief()["awaiting_owner"]
        self.assertIn(trigger, [row["ref"] for row in awaiting])

    def test_the_service_contract_describes_an_activation_it_did_not_perform(self):
        contract = self.plane.service_contract(
            invocation=["python3", "-m", "factory_controller.cli", "supervisor",
                        "cycle"])
        self.assertFalse(contract["activation"]["performed_here"])
        self.assertEqual(contract["activation"]["state"], "not_run")
        self.assertIn(contract["activation"]["state"], supervisor.CANONICAL_ABSENCE)
        self.assertEqual(contract["activation"]["performed_by"], "owner")

    def test_every_selection_a_cycle_made_is_recorded_and_append_only(self):
        self.project()
        self.policy()
        self.backlog("W1")
        self.running()
        report = self.plane.cycle("w1")
        rows = self.plane.selections(report["cycle_id"])
        self.assertTrue(rows)
        with self.store.transaction() as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE supervisor_selections SET reason='X'")
            with self.assertRaises(Exception):
                db.execute("DELETE FROM supervisor_selections")

    def test_transitions_are_append_only(self):
        self.running()
        with self.store.transaction() as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE supervisor_transitions SET actor='X'")
            with self.assertRaises(Exception):
                db.execute("DELETE FROM supervisor_transitions")


class CommandLineTests(unittest.TestCase):
    """The Owner surface, exercised the way an Owner reaches it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "controller.db")

    def run_cli(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--db", self.db, *argv])
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def test_start_configure_cycle_pause_and_brief(self):
        self.run_cli("project", "register", "--id", PROJECT,
                     "--repository", REPO)
        code, _ = self.run_cli("supervisor", "policy", "--project", PROJECT,
                               "--missions-per-cycle", "2")
        self.assertEqual(code, 0)
        code, control = self.run_cli("supervisor", "start", "--reason", "go")
        self.assertEqual(control["state"], "running")
        payload = Path(self.tmp.name) / "mission.json"
        payload.write_text(json.dumps({"work_item_id": "CLI-1",
                                       "execution_mode": "fixture",
                                       "acceptance_gate_ids": GATES}))
        self.run_cli("submit", "--key", "CLI-1", "--file", str(payload))
        code, report = self.run_cli("supervisor", "cycle", "--worker", "cli")
        self.assertEqual(code, 0)
        self.assertEqual(report["outcome"], "completed")
        code, control = self.run_cli("supervisor", "pause", "--reason", "freeze")
        self.assertEqual(control["state"], "paused")
        code, brief = self.run_cli("supervisor", "brief")
        self.assertEqual(brief["control"]["state"], "paused")

    def test_a_refusal_prints_its_code_and_exits_non_zero(self):
        code, payload = self.run_cli("supervisor", "policy", "--project", "ghost")
        self.assertEqual(code, 2)
        self.assertEqual(payload["refused"]["code"], "SUPERVISOR_PROJECT_UNREGISTERED")

    def test_the_surface_offers_no_approval_or_installation_verb(self):
        from factory_controller.cli import parser
        actions = next(
            action for action in parser()._subparsers._group_actions[0].choices[
                "supervisor"]._actions if action.dest == "action")
        for forbidden in ("approve", "install", "deploy", "promote", "widen",
                          "bootstrap"):
            self.assertNotIn(forbidden, actions.choices)

    def test_the_service_verb_describes_a_step_it_does_not_take(self):
        code, contract = self.run_cli("supervisor", "service")
        self.assertEqual(code, 0)
        self.assertFalse(contract["activation"]["performed_here"])
        self.assertIn("supervisor", contract["schedule"]["invocation"])


if __name__ == "__main__":
    unittest.main()
