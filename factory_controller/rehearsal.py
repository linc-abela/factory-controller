"""A bounded rehearsal of the internal dogfood run, on real processes.

Every mission here goes through the ordinary execution path and every step
spawns the local safe provider as a separate process, so the thing being
rehearsed is the Controller's real dispatch seam and not a stub standing where
it would be.  What is *not* real is the provider itself: it says ``fixture`` on
every receipt and refuses a mission that declares itself real, which is what
lets this run on a host with no credential and no target repository.

Two shapes are deliberately absent, and they are the difference between a
rehearsal and a run.

**There is no loop.**  Each scenario performs a fixed number of bounded
``cycle`` invocations and stops.  A rehearsal that ran until something happened
would be the self-running Controller Stage 9 spent its whole design refusing.

**There is no shared state between scenarios.**  Each opens its own store under
the run root, so a scenario cannot pass because a previous one left the world in
a convenient shape -- and a failure names one scenario rather than a sequence.

Each scenario states what must be true, observes what happened, and records
both.  A scenario whose expectation is not met is recorded as ``failed`` and the
run says so; nothing here reports a pass it did not observe.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import improvement as improvement_plane
from . import maintenance as maintenance_plane
from . import portfolio
from . import production
from . import supervisor as supervisor_plane
from .adapter import JsonProcessAdapter
from .engine import Controller, RetryPolicy
from .store import MissionStore

CONTRACT_VERSION = "factory-controller/rehearsal/1.0"

#: Every scenario is bounded by this many cycles.  The number is a ceiling, not
#: a target: a scenario that needed more would be one whose outcome depends on
#: how long it was left running, which is the property a rehearsal must not
#: have.
CYCLE_CEILING = 8

PROTOTYPE = "factory-prototype-lab"
BUG = "factory-bug-lab"
GATES = {PROTOTYPE: ("dev-check", "dev-test", "dev-evaluate"),
         BUG: ("dev-check", "dev-test", "dev-reproduce")}
REPOSITORY = "https://github.com/linc-abela/%s.git"

#: Two ordered candidates, named neutrally.  The bridge's real profile ids are
#: vendor names and the Controller must never carry one -- it compares a profile
#: as an opaque string and `tests/test_authority_boundaries.py` holds that -- so
#: a rehearsal that hardcoded them would be rehearsing a coupling that does not
#: exist.  What is being exercised is the ordering and the pre-spawn decline,
#: and neither depends on what the profiles are called.
PRIMARY, SECONDARY = "primary", "secondary"

#: The commit each lab's `dev` targets were read at, per repository.  One
#: constant covering both would put a prototype-lab commit in a bug-lab gate
#: source, which is the invented provenance the gates exist to replace.
BASELINE = {PROTOTYPE: "229b923b050fe8a4450d5597d472157bd42c8647",
            BUG: "961a4c97d49183b5501f244ba48773d9f50953ae"}


class Harness:
    """One scenario's world: a store, a real-process adapter, and the planes."""

    def __init__(self, root: Path, name: str, *, adapter_args: tuple = (),
                 clock=None) -> None:
        self.name = name
        self.root = root / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self.store = MissionStore(str(self.root / "controller.db"), clock=self.clock)
        self.adapter = JsonProcessAdapter(
            [sys.executable, "-m", "factory_controller.safe_provider",
             *adapter_args])
        self.controller = Controller(
            self.store, self.adapter,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=30)
        self.ledger = production.ProductionLedger(self.store)
        self.maintenance = maintenance_plane.MaintenancePlane(self.store, self.ledger)
        self.improvement = improvement_plane.ImprovementPlane(self.store, self.ledger)
        self.supervisor = supervisor_plane.OperationsSupervisor(
            self.controller, clock=self.clock)

    # -- provisioning ------------------------------------------------------- #

    def project(self, project_id: str, **overrides) -> None:
        values = {"budget_ceiling": 10.0, "budget_currency": "USD",
                  "concurrency_cap": 2}
        values.update(overrides)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=project_id, repository=REPOSITORY % project_id,
            acceptance_gate_ids=GATES[project_id],
            acceptance_gate_source="%s@%s:dev" % (REPOSITORY % project_id,
                                                  BASELINE[project_id]),
            policy_version="dogfood-1", **values))
        self.supervisor.set_policy(supervisor_plane.SupervisorPolicy(
            project_id=project_id,
            work_classes=("backlog", "maintenance", "improvement"),
            missions_per_cycle=2, policy_version="dogfood-1"))

    def staging(self, project_id: str) -> production.EnvironmentPolicy:
        policy = production.EnvironmentPolicy(
            environment_id=project_id + "-staging", project_id=project_id,
            environment_class="staging", repository=REPOSITORY % project_id,
            service_ref=project_id + "-web", approver_refs=("owner",),
            autonomous=True)
        try:
            self.ledger.register_environment(policy)
        except production.ProductionRefusal:
            pass
        return policy

    def backlog(self, work_item_id: str, project_id: str = PROTOTYPE,
                **extra) -> str:
        payload = {"work_item_id": work_item_id, "project_id": project_id,
                   "execution_mode": "fixture",
                   "acceptance_gate_ids": list(GATES[project_id]),
                   "provider_candidates": [
                       {"profile": PRIMARY, "capabilities": ["prototype"]},
                       {"profile": SECONDARY, "capabilities": ["prototype"]}]}
        payload.update(extra)
        mission, _ = self.controller.submit(payload, work_item_id)
        return mission["id"]

    def incident(self, project_id: str = BUG, incident_ref="INC-1") -> str:
        environment = self.staging(project_id)
        self.maintenance.set_policy(maintenance_plane.MaintenancePolicy(
            project_id=project_id, enabled=True, cooldown_seconds=0,
            concurrency=2, policy_version="mp-1"))
        self.ledger.declare_incident(
            incident_ref=incident_ref, environment_id=environment.environment_id,
            declared_by="owner", incident_class="triaged_defect",
            affected_release_sha="a" * 40, affected_bundle_ref="rc-000",
            failing_behaviour="the lab evaluator reproduces the defect",
            blast_radius="the lab only")
        return self.maintenance.admit_trigger(
            "production_incident", incident_ref)["trigger_ref"]

    def experiment(self, project_id: str = PROTOTYPE) -> str:
        self.improvement.set_policy(improvement_plane.ImprovementPolicy(
            project_id=project_id, enabled=True, cooldown_seconds=0,
            protected_surfaces={name: ("protected/%s/" % name,) for name
                                in improvement_plane.MANDATORY_SURFACES},
            policy_version="ip-1"))
        objective = improvement_plane.Objective(
            objective_ref="OBJ-1", project_id=project_id,
            improvement_class="performance",
            statement="the lab evaluator should finish faster without losing tests",
            metrics=(improvement_plane.Metric("p95_latency_ms", "decrease",
                                              "objective", min_delta_ratio=0.10),
                     improvement_plane.Metric("passing_tests", "increase",
                                              "non_regression",
                                              tolerance_ratio=0.0)),
            objective_version="1.0")
        self.improvement.register_objective(objective)
        row = self.improvement.admit_experiment(
            objective.objective_ref, "owner_objective", objective.objective_ref,
            target_repository=REPOSITORY % project_id,
            baseline_sha=BASELINE[project_id],
            isolation_ref="lane://%s/experiment-1" % project_id)
        self.improvement.record_baseline(
            row["experiment_ref"], {"p95_latency_ms": 400.0, "passing_tests": 120})
        return row["experiment_ref"]

    # -- running ------------------------------------------------------------ #

    def running(self) -> None:
        self.supervisor.transition("running", actor="owner", reason="rehearsal")

    def cycles(self, limit: int = CYCLE_CEILING) -> list:
        """Bounded, and bounded by a constant nobody can pass in.

        The ceiling is deliberately not a parameter: a scenario that could raise
        it would be one whose outcome depends on how long it was left running.
        """

        reports = []
        for index in range(min(limit, CYCLE_CEILING)):
            report = self.supervisor.cycle("rehearsal-%d" % index)
            reports.append(report)
            if not report["advanced"] and not report["promoted"]:
                break
        return reports

    def states(self) -> dict[str, str]:
        return dict(self.store.counts())


# --------------------------------------------------------------------------- #
# the scenarios
# --------------------------------------------------------------------------- #

def _scenario(name: str, expectation: str, outcome: bool, **detail) -> dict:
    return {"scenario": name, "expects": expectation,
            "outcome": "proven" if outcome else "failed", **detail}


def normal_backlog(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    ids = [harness.backlog("DOGFOOD-BACKLOG-%d" % index) for index in range(3)]
    harness.running()
    harness.cycles()
    states = [harness.store.get(mission)["state"] for mission in ids]
    return _scenario(
        "normal_backlog",
        "already-admitted backlog missions complete through the ordinary path",
        states == ["completed"] * 3, mission_states=states,
        provider_processes=sum(len(harness.store.runs(m)) for m in ids))


def maintenance_repair(harness: Harness) -> dict:
    harness.project(BUG)
    trigger = harness.incident()
    harness.running()
    harness.cycles()
    lineage = harness.maintenance.lineage(trigger)
    mission = harness.store.get(lineage["mission_ref"]) if lineage["mission_ref"] != "not_run" else None
    gates = (mission or {}).get("payload", {}).get("acceptance_gate_ids")
    return _scenario(
        "maintenance_repair",
        "a recorded incident becomes a repair mission running the repository's "
        "own declared gates",
        bool(mission) and mission["state"] == "completed"
        and gates == list(GATES[BUG]),
        mission_state=(mission or {}).get("state", "not_run"),
        acceptance_gate_ids=gates or "not_run",
        acceptance_gate_source=(mission or {}).get("payload", {}).get(
            "acceptance_gate_source", "not_run"))


def improvement_experiment(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    experiment = harness.experiment()
    harness.running()
    harness.cycles()
    lineage = harness.improvement.lineage(experiment)
    mission = (harness.store.get(lineage["mission_ref"])
               if lineage["mission_ref"] not in ("not_run", None) else None)
    return _scenario(
        "improvement_experiment",
        "a baselined experiment becomes a candidate mission with declared gates",
        bool(mission) and mission["state"] == "completed"
        and mission["payload"]["acceptance_gate_ids"] == list(GATES[PROTOTYPE]),
        mission_state=(mission or {}).get("state", "not_run"),
        acceptance_gate_ids=(mission or {}).get("payload", {}).get(
            "acceptance_gate_ids", "not_run"))


def owner_gated_promotion(harness: Harness) -> dict:
    """The candidate stops at the Owner, and the supervisor cannot pass it."""

    harness.project(PROTOTYPE)
    experiment = harness.experiment()
    harness.running()
    harness.cycles()
    lineage = harness.improvement.lineage(experiment)
    sealed = "not_run"
    try:
        harness.improvement.stage_promotion(
            experiment, production.ReleaseBundle.from_payload({
                "bundle_ref": "rc-dogfood", "project_id": PROTOTYPE,
                "repository": REPOSITORY % PROTOTYPE, "release_sha": "b" * 40,
                "mission_ref": lineage["mission_ref"],
                "evidence_refs": ["evidence/dogfood.json"],
                "evaluator_receipts": ["receipts/evaluate.json"],
                "artifact": "not_applicable", "env_schema": {},
                "migration": {"forward_ref": "not_applicable",
                              "reverse_ref": "not_applicable"},
                "release_policy_version": "dogfood-1",
                "provenance": {"built_by": "factory-controller/rehearsal",
                               "built_at": "2026-08-27T00:00:00Z",
                               "contract_version": production.CONTRACT_VERSION}}),
            PROTOTYPE + "-staging")
        refused = None
    except (improvement_plane.ImprovementRefusal,
            production.ProductionRefusal, improvement_plane.PolicyError) as refusal:
        refused = getattr(refusal, "code", type(refusal).__name__)
    return _scenario(
        "owner_gated_promotion",
        "an unsealed candidate cannot reach an environment, and the supervisor "
        "has no verb that would seal it",
        refused is not None, refusal_code=refused or "not_run",
        sealed=sealed)


def restart_recovery(harness: Harness) -> dict:
    """A cycle abandoned mid-flight is settled, not repeated."""

    harness.project(PROTOTYPE)
    ids = [harness.backlog("DOGFOOD-RESTART-%d" % index) for index in range(2)]
    harness.running()
    with harness.store.transaction() as db:
        db.execute("INSERT INTO supervisor_cycles"
                   " (cycle_id,sequence,worker_id,control_state,started_at,"
                   "  lease_expires_at,outcome,detail_json)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   ("cyc_abandoned", 999, "crashed", "running",
                    harness.clock() - 100, harness.clock() - 1, None, "{}"))
    reports = harness.cycles()
    recovered = [entry for report in reports for entry in report.get("recovered", ())]
    states = [harness.store.get(mission)["state"] for mission in ids]
    legs = {mission: len(harness.store.runs(mission)) for mission in ids}
    return _scenario(
        "restart_recovery",
        "an abandoned cycle is settled on the next claim and no mission is "
        "dispatched to a provider twice",
        bool(recovered) and states == ["completed"] * 2
        and all(count == 1 for count in legs.values()),
        recovered=recovered, mission_states=states, provider_legs=legs)


def pause_and_drain(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    first = harness.backlog("DOGFOOD-PAUSE-1")
    harness.running()
    harness.store.set_project_state(PROTOTYPE, "paused")
    paused_reports = harness.cycles(2)
    paused_state = harness.store.get(first)["state"]
    harness.store.set_project_state(PROTOTYPE, "enabled")
    harness.cycles()
    resumed_state = harness.store.get(first)["state"]
    return _scenario(
        "pause_and_drain",
        "a paused project admits nothing new and resumes exactly where it was",
        paused_state == "admitted" and resumed_state == "completed",
        paused_state=paused_state, resumed_state=resumed_state,
        advanced_while_paused=sum(len(report["advanced"])
                                  for report in paused_reports))


def provider_outage(harness: Harness) -> dict:
    """The first profile declines before it starts; the second serves."""

    harness.project(PROTOTYPE)
    mission = harness.backlog("DOGFOOD-OUTAGE-1")
    harness.running()
    harness.cycles()
    row = harness.store.get(mission)
    runs = harness.store.runs(mission)
    started = [run["receipt"].get("process_started") for run in runs]
    return _scenario(
        "provider_outage",
        "an unavailable profile is declined before a process starts and the "
        "mission falls through to the next candidate",
        row["state"] == "completed" and False in started,
        mission_state=row["state"],
        profiles=[run["provider_profile"] for run in runs],
        process_started=started)


def budget_refusal(harness: Harness) -> dict:
    harness.project(PROTOTYPE, budget_ceiling=0.0, budget_currency="USD")
    mission = harness.backlog("DOGFOOD-BUDGET-1")
    harness.running()
    reports = harness.cycles(2)
    row = harness.store.get(mission)
    reasons = {entry["reason"] for report in reports
               for entry in report.get("refused", ())}
    return _scenario(
        "budget_refusal",
        "a project at its ceiling is not scheduled, and the mission waits "
        "rather than running unpriced",
        row["state"] == "admitted",
        mission_state=row["state"], refusals=sorted(reasons))


def acceptance_gate_failure(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    mission = harness.backlog("DOGFOOD-GATE-1")
    harness.running()
    harness.cycles()
    row = harness.store.get(mission)
    evaluation = harness.store.step_output(mission, "evaluate") or {}
    failed = [outcome["gate_id"] for outcome in evaluation.get("gate_outcomes", ())
              if outcome.get("passed") is False]
    return _scenario(
        "acceptance_gate_failure",
        "a declared gate that fails escalates the mission and names the gate",
        row["state"] == "escalated" and failed == ["dev-test"],
        mission_state=row["state"], terminal_reason=row.get("terminal_reason"),
        failed_gates=failed or "not_run")


def rollback_recovery(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    environment = harness.staging(PROTOTYPE)
    port = production.DeterministicDeploymentAdapter()
    bundle = production.ReleaseBundle.from_payload({
        "bundle_ref": "rc-dogfood-1", "project_id": PROTOTYPE,
        "repository": REPOSITORY % PROTOTYPE, "release_sha": "c" * 40,
        "mission_ref": "DOGFOOD-BACKLOG-0",
        "evidence_refs": ["evidence/dogfood.json"],
        "evaluator_receipts": ["receipts/evaluate.json"],
        "artifact": "not_applicable", "env_schema": {},
        "migration": {"forward_ref": "not_applicable",
                              "reverse_ref": "not_applicable"}, "release_policy_version": "dogfood-1",
        "provenance": {"built_by": "factory-controller/rehearsal",
                               "built_at": "2026-08-27T00:00:00Z",
                               "contract_version": production.CONTRACT_VERSION}})
    first = harness.ledger.admit_release(bundle, environment.environment_id,
                                         "rehearsal")
    harness.ledger.deploy(first, port)
    harness.ledger.record_health(first, production.HealthRecord(
        checks_passed=3, checks_failed=0, evidence_ref="probe/1", observed_at=1.0))
    second = harness.ledger.admit_release(
        production.ReleaseBundle.from_payload({**json.loads(json.dumps(
            bundle.as_row())), "bundle_ref": "rc-dogfood-2",
            "release_sha": "d" * 40}),
        environment.environment_id, "rehearsal")
    harness.ledger.deploy(second, port)
    health = harness.ledger.record_health(second, production.HealthRecord(
        checks_passed=0, checks_failed=3, evidence_ref="probe/2", observed_at=2.0))
    state = harness.ledger.rollback(second, port)
    return _scenario(
        "rollback_recovery",
        "a staging deployment that fails its health check rolls back to the "
        "last healthy release",
        health == "failed" and state == "recovered",
        health=health, state=state)


def emergency_stop(harness: Harness) -> dict:
    harness.project(PROTOTYPE)
    mission = harness.backlog("DOGFOOD-STOP-1")
    harness.running()
    harness.store.emergency_stop(True)
    reports = harness.cycles(2)
    stopped_state = harness.store.get(mission)["state"]
    harness.store.emergency_stop(False)
    harness.cycles()
    return _scenario(
        "emergency_stop",
        "the Owner's portfolio stop halts admission immediately and clearing "
        "it resumes the same mission",
        stopped_state == "admitted"
        and harness.store.get(mission)["state"] == "completed",
        stopped_state=stopped_state,
        advanced_while_stopped=sum(len(report["advanced"]) for report in reports))


SCENARIOS: tuple[tuple[str, Callable[[Harness], dict], tuple], ...] = (
    ("normal_backlog", normal_backlog, ()),
    ("maintenance_repair", maintenance_repair, ()),
    ("improvement_experiment", improvement_experiment, ()),
    ("owner_gated_promotion", owner_gated_promotion, ()),
    ("restart_recovery", restart_recovery, ()),
    ("pause_and_drain", pause_and_drain, ()),
    ("provider_outage", provider_outage,
     ("--unavailable-profiles=" + PRIMARY,)),
    ("budget_refusal", budget_refusal, ()),
    ("acceptance_gate_failure", acceptance_gate_failure,
     ("--failing-gates=dev-test",)),
    ("rollback_recovery", rollback_recovery, ()),
    ("emergency_stop", emergency_stop, ()),
)


def run(root: str | Path, *, only: tuple[str, ...] = (), clock=None) -> dict[str, Any]:
    """Every scenario, each in its own world, each bounded.  Nothing loops."""

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, scenario, adapter_args in SCENARIOS:
        if only and name not in only:
            continue
        harness = Harness(root, name, adapter_args=adapter_args, clock=clock)
        started = time.time()
        try:
            row = scenario(harness)
        except Exception as error:                        # noqa: BLE001
            row = _scenario(name, "the scenario runs at all", False,
                            error="%s: %s" % (type(error).__name__, error))
        row["duration_ms"] = int((time.time() - started) * 1000)
        row["adapter"] = list(harness.adapter.command)
        row["execution_mode"] = "fixture"
        rows.append(row)
    failed = [row["scenario"] for row in rows if row["outcome"] != "proven"]
    return {
        "contract_version": CONTRACT_VERSION,
        "root": str(root),
        "cycle_ceiling": CYCLE_CEILING,
        "execution_mode": "fixture",
        "provider": "local-safe-provider, one real process per step",
        "scenarios": rows,
        "proven": [row["scenario"] for row in rows if row["outcome"] == "proven"],
        "failed": failed,
        "outcome": "REHEARSED" if not failed else "INCOMPLETE",
    }
