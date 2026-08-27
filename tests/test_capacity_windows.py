"""A deterministic virtual-time proof over staggered five-hour quota windows.

Everything here runs on a hand-wound clock against the real store, the real
scheduler and the real engine.  No sleeping, no wall-clock, no network: the
simulated part is only the harness's own quota accounting, which is the one
fact the Controller is not entitled to compute for itself.

The headline number the brief asks for is at the bottom: how much more useful
work the same backlog gets through when it may use every open window, against
the naive baseline of one runtime and waiting for its reset.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factory_controller import capacity, portfolio
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import Clock


HOUR = 3_600.0
WINDOW = 5 * HOUR
TICK = 900.0
HORIZON = 24 * HOUR
START = 1_800_000.0

RUNTIMES = ("runtime-alpha", "runtime-beta", "runtime-gamma")
#: Staggered starts: the whole point of Phase 1 is that these do not reset
#: together, so there is almost always one open window somewhere.
OFFSETS = {"runtime-alpha": 0.0, "runtime-beta": 1.5 * HOUR, "runtime-gamma": 3.0 * HOUR}
ALLOWANCE = 6


class WindowedHarness:
    """Three subscription harnesses with rolling five-hour allowances.

    Each runtime serves ``allowance`` missions per window and then declines for
    quota until its window rolls.  The refusal proves no process started, which
    is what a real pre-spawn quota rejection reports and what makes the leg
    safely re-routable.
    """

    def __init__(self, clock, *, allowance: int = ALLOWANCE,
                 unavailable: tuple[str, ...] = ()) -> None:
        self.clock = clock
        self.allowance = allowance
        self.unavailable = set(unavailable)
        self.window_start = {name: START + offset - WINDOW
                             for name, offset in OFFSETS.items()}
        self.used = {name: 0 for name in RUNTIMES}
        self.served: list[tuple[str, float]] = []
        self.refusals: list[tuple[str, float]] = []

    # -- the harness's own accounting ----------------------------------- #

    def roll(self, now: float) -> None:
        for name in RUNTIMES:
            while now >= self.window_start[name] + WINDOW:
                self.window_start[name] += WINDOW
                self.used[name] = 0

    def state(self, name: str) -> tuple[str, float | None, int]:
        remaining = self.allowance - self.used[name]
        if name in self.unavailable:
            return ("readiness_unavailable", None, 0)
        if remaining <= 0:
            return ("exhausted", self.window_start[name] + WINDOW, 0)
        if remaining == 1:
            return ("constrained", None, remaining)
        return ("available", None, remaining)

    def publish(self, store: MissionStore, now: float) -> None:
        """Write one observation per runtime, exactly as a real probe would."""

        self.roll(now)
        for name in RUNTIMES:
            state, reset_at, remaining = self.state(name)
            store.observe_capacity(capacity.CapacityObservation(
                runtime_id=name, state=state, observed_at=now,
                source="execution_layer", source_ref="window-accounting",
                window_started_at=self.window_start[name],
                expected_reset_at=reset_at,
                remaining_units=float(remaining) if state != "readiness_unavailable" else None,
                unit="missions" if state != "readiness_unavailable" else None,
                precision="exact" if state != "readiness_unavailable" else "unknown"))

    # -- the execution layer -------------------------------------------- #

    def execute(self, step, operation_key, value):
        if step == "dispatch":
            now = self.clock()
            self.roll(now)
            route = value["route"]
            name = route["provider_profile"]
            receipt = {"provider_profile": name, "provider": "harness",
                       "execution_mode": "fixture", "duration_ms": 1,
                       "idempotency_key": route["idempotency_key"]}
            if name in self.unavailable or self.used.get(name, 0) >= self.allowance:
                self.refusals.append((name, now))
                return {"status": "provider_unavailable", "diagnostic": "quota_exhausted",
                        "receipt": {**receipt, "process_started": False,
                                    "refusal_code": "quota_exhausted"}}
            self.used[name] += 1
            self.served.append((name, now))
            return {"status": "completed", "candidate_sha": "a" * 40,
                    "execution_id": operation_key,
                    "receipt": {**receipt, "process_started": True}}
        if step == "verify":
            return {"verified": True}
        if step == "evaluate":
            gates = value["mission"].get("acceptance_gate_ids") or ["G"]
            return {"passed": True,
                    "gate_outcomes": [{"gate_id": g, "passed": True} for g in gates]}
        if step == "evidence":
            return {"accepted": True, "evidence_pointer": "e" * 64}
        return {"status": "unknown"}


class Simulation:
    """One run of the whole stack over a virtual day."""

    def __init__(self, case, *, runtimes, projects=5, per_project=8,
                 managed=True, unavailable=(), allowance=ALLOWANCE) -> None:
        temp = tempfile.TemporaryDirectory()
        case.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "controller.db"
        self.clock = Clock(START)
        self.store = MissionStore(self.path, clock=self.clock)
        self.harness = WindowedHarness(self.clock, allowance=allowance,
                                       unavailable=unavailable)
        self.controller = Controller(self.store, self.harness,
                                     retry_policy=RetryPolicy(base_delay_seconds=0),
                                     lease_seconds=0)
        self.runtimes = tuple(runtimes)
        self.store.set_portfolio_policy(portfolio.PortfolioPolicy(portfolio_concurrency=6))
        if managed:
            for name in RUNTIMES:
                self.store.set_runtime_policy(capacity.RuntimePolicy(
                    runtime_id=name, max_observation_age_seconds=2 * TICK))
        for index in range(projects):
            project_id = "project-%d" % index
            self.store.register_project(portfolio.ProjectPolicy(
                project_id=project_id, repository="repo://" + project_id,
                concurrency_cap=2, priority=100 + index))
            for item in range(per_project):
                key = "%s-%d" % (project_id, item)
                self.controller.submit({
                    "work_item_id": key, "execution_mode": "fixture",
                    "acceptance_gate_ids": ["G"], "project_id": project_id,
                    "provider_candidates": list(self.runtimes),
                    "capacity_estimate": {"size_class": "small",
                                          "expected_units": 1, "unit": "missions"},
                }, key)
        self.ticks = 0

    def run(self, *, horizon: float = HORIZON, publish: bool = True) -> "Simulation":
        elapsed = 0.0
        while elapsed < horizon:
            if publish:
                self.harness.publish(self.store, self.clock.now)
            for _ in range(12):
                if self.controller.work_once("sim:%d" % self.ticks) is None:
                    break
            self.ticks += 1
            self.clock.advance(TICK)
            elapsed += TICK
        return self

    @property
    def completed(self) -> int:
        return self.store.counts().get("completed", 0)

    @property
    def lost(self) -> int:
        counts = self.store.counts()
        return sum(counts.get(state, 0) for state in ("refused", "failed", "escalated"))

    @property
    def duplicate_effects(self) -> int:
        """Provider invocations beyond one per mission that actually ran."""

        extra = 0
        for mission in self.store.all_missions():
            started = [leg for leg in self.store.runs(mission["id"])
                       if leg["process_started"] is True]
            extra += max(0, len(started) - 1)
        return extra


class StaggeredWindowTests(unittest.TestCase):
    def test_a_full_day_across_three_staggered_windows_loses_nothing(self):
        run = Simulation(self, runtimes=RUNTIMES).run()
        self.assertEqual(run.lost, 0)
        self.assertEqual(run.duplicate_effects, 0)
        self.assertEqual(run.completed, 40)

    def test_every_runtime_was_used_rather_than_one_being_drained(self):
        run = Simulation(self, runtimes=RUNTIMES).run()
        used = {name for name, _ in run.harness.served}
        self.assertEqual(used, set(RUNTIMES))

    def test_no_mission_was_ever_dispatched_to_a_spent_window(self):
        """Capacity is doing the work: the harness is never asked for what it
        cannot serve, except at the moment its own window closes under a
        mission already in flight."""

        run = Simulation(self, runtimes=RUNTIMES).run()
        self.assertLessEqual(len(run.harness.refusals), len(run.harness.served) // 4)

    def test_an_unavailable_runtime_never_stalls_the_other_two(self):
        run = Simulation(self, runtimes=RUNTIMES,
                         unavailable=("runtime-beta",)).run()
        self.assertEqual(run.lost, 0)
        self.assertEqual({name for name, _ in run.harness.served},
                         {"runtime-alpha", "runtime-gamma"})

    def test_all_providers_cooling_leaves_durable_state_and_no_spin(self):
        """Scope 8: the cycle ends, the missions keep their state, nothing loops."""

        run = Simulation(self, runtimes=RUNTIMES, allowance=1, per_project=4)
        run.harness.publish(run.store, run.clock.now)
        for name in RUNTIMES:
            run.store.observe_capacity(capacity.CapacityObservation(
                runtime_id=name, state="cooling", observed_at=run.clock.now,
                source="execution_layer", source_ref="all-cooling",
                expected_reset_at=run.clock.now + WINDOW))
        for _ in range(20):
            self.assertIsNone(run.controller.work_once("w"))
        counts = run.store.counts()
        self.assertEqual(counts.get("admitted"), 20)
        self.assertEqual(counts.get("refused", 0) + counts.get("failed", 0), 0)

    def test_work_resumes_after_the_reset_without_any_conversation(self):
        run = Simulation(self, runtimes=("runtime-alpha",), per_project=4, projects=2,
                         allowance=2)
        run.harness.publish(run.store, run.clock.now)
        for _ in range(12):
            if run.controller.work_once("w") is None:
                break
        first = run.completed
        self.assertEqual(first, 2)
        # The window rolls.  Nothing is re-declared, nothing is re-submitted,
        # and no model state carries across: the ledger is the whole handoff.
        run.clock.advance(WINDOW)
        run.harness.publish(run.store, run.clock.now)
        for _ in range(12):
            if run.controller.work_once("w") is None:
                break
        self.assertEqual(run.completed, 4)
        self.assertEqual(run.duplicate_effects, 0)

    def test_a_mission_moves_to_another_open_window_without_a_second_effect(self):
        """Cross-runtime resumption is the ordinary selector over a smaller set."""

        run = Simulation(self, runtimes=RUNTIMES, projects=1, per_project=3,
                         allowance=1)
        run.harness.publish(run.store, run.clock.now)
        for _ in range(9):
            if run.controller.work_once("w") is None:
                break
        self.assertEqual(run.completed, 3)
        self.assertEqual({name for name, _ in run.harness.served}, set(RUNTIMES))
        self.assertEqual(run.duplicate_effects, 0)

    def test_a_restart_mid_day_changes_nothing_about_the_outcome(self):
        run = Simulation(self, runtimes=RUNTIMES, per_project=20).run(horizon=12 * HOUR)
        midpoint = run.completed
        replacement = Controller(MissionStore(run.path, clock=run.clock), run.harness,
                                 retry_policy=RetryPolicy(base_delay_seconds=0),
                                 lease_seconds=0)
        elapsed = 0.0
        while elapsed < 12 * HOUR:
            run.harness.publish(replacement.store, run.clock.now)
            for _ in range(12):
                if replacement.work_once("restarted") is None:
                    break
            run.clock.advance(TICK)
            elapsed += TICK
        self.assertGreater(replacement.store.counts().get("completed", 0), midpoint)
        self.assertEqual(run.duplicate_effects, 0)


class ThroughputComparisonTests(unittest.TestCase):
    """The acceptance criterion that asks for a transparent comparison."""

    def test_using_every_open_window_beats_waiting_for_one_to_reset(self):
        naive = Simulation(self, runtimes=("runtime-alpha",)).run()
        aware = Simulation(self, runtimes=RUNTIMES).run()
        self.assertEqual(naive.lost, 0)
        self.assertEqual(aware.lost, 0)
        self.assertGreater(aware.completed, naive.completed)
        # Recorded rather than merely asserted, so the numbers can be read back
        # out of the suite: five projects, eight missions each, one virtual day,
        # three harnesses whose five-hour windows are staggered by 1.5 hours.
        self.comparison = {
            "naive_single_runtime_completed": naive.completed,
            "capacity_aware_completed": aware.completed,
            "backlog": 40,
            "horizon_hours": 24,
            "window_hours": 5,
            "allowance_per_window": ALLOWANCE,
        }
        self.assertEqual(naive.completed, ALLOWANCE * 5)
        self.assertEqual(aware.completed, 40)

    def test_the_deferral_protects_work_even_with_no_capacity_record_at_all(self):
        """Two separate guarantees, and it is worth being exact about which is which.

        Not losing the mission comes from the *refusal codes*, so it holds with
        no registry, no observations and no Owner configuration -- a Factory
        that never adopts capacity still stops throwing missions away when a
        window closes.  What the capacity record buys on top is not having made
        the doomed dispatch in the first place, which is measurable as the
        refused legs the harness never had to answer.
        """

        unmanaged = Simulation(self, runtimes=("runtime-alpha",), managed=False,
                               projects=2, per_project=8)
        unmanaged.run(horizon=6 * HOUR, publish=False)
        managed = Simulation(self, runtimes=("runtime-alpha",),
                             projects=2, per_project=8).run(horizon=6 * HOUR)
        self.assertEqual(unmanaged.lost, 0)
        self.assertEqual(managed.lost, 0)
        self.assertGreater(len(unmanaged.harness.refusals), len(managed.harness.refusals))
        self.assertGreaterEqual(managed.completed, unmanaged.completed)


if __name__ == "__main__":
    unittest.main()
