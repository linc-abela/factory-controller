"""The multi-project scheduler: order, fairness, caps, budgets, and claims.

Most of this exercises `portfolio.schedule` directly, because it is a pure
function of a snapshot and that is the whole reason it was written as one --
starvation is a statement about arbitrary waits, and a pure function lets the
wait be a number rather than a sleep.  The claim-side tests then check that the
store builds the snapshot the pure function was given.
"""

from __future__ import annotations

import threading
import unittest

from factory_controller import portfolio
from tests.support import Clock, PortfolioTestCase


def candidate(mission_id, project_id="alpha", *, created_at=0.0, state="admitted",
              ready_at=0.0, priority=None, prerequisites=()):
    return portfolio.MissionCandidate(
        mission_id=mission_id, project_id=project_id, state=state,
        created_at=created_at, ready_at=ready_at, priority=priority,
        prerequisites=prerequisites)


def snapshot(candidates, projects=None, *, now=0.0, in_flight=None, portfolio_in_flight=0,
             spend=None, **policy):
    projects = projects or {"alpha": portfolio.ProjectPolicy("alpha", "repo://alpha")}
    return portfolio.Snapshot(
        portfolio=portfolio.PortfolioPolicy(**policy), projects=projects,
        candidates=candidates, in_flight=in_flight or {},
        portfolio_in_flight=portfolio_in_flight, project_spend=spend or {}, now=now)


class OrderingTests(unittest.TestCase):
    def test_lower_priority_number_runs_first(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", priority=1),
                    "b": portfolio.ProjectPolicy("b", "r", priority=50)}
        decision = portfolio.schedule(snapshot(
            [candidate("m-b", "b"), candidate("m-a", "a")], projects))
        self.assertEqual(decision.selected, "m-a")
        self.assertEqual(decision.reason, "SCHEDULED")

    def test_a_mission_priority_overrides_its_project_priority(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", priority=1),
                    "b": portfolio.ProjectPolicy("b", "r", priority=50)}
        decision = portfolio.schedule(snapshot(
            [candidate("m-b", "b", priority=0), candidate("m-a", "a")], projects))
        self.assertEqual(decision.selected, "m-b")

    def test_ties_break_on_creation_then_identity_so_two_workers_agree(self):
        first = snapshot([candidate("m2", created_at=2.0), candidate("m1", created_at=1.0)], now=9)
        second = snapshot([candidate("m1", created_at=1.0), candidate("m2", created_at=2.0)], now=9)
        self.assertEqual(portfolio.schedule(first).selected, "m1")
        self.assertEqual(portfolio.schedule(second).selected, "m1")

    def test_a_resume_outranks_every_piece_of_new_work(self):
        decision = portfolio.schedule(snapshot([
            candidate("new", priority=0),
            candidate("resuming", state="dispatched", priority=999)], now=1))
        self.assertEqual(decision.selected, "resuming")
        self.assertEqual(decision.reason, "RESUME_AFTER_BOUNDARY")

    def test_a_mission_scheduled_for_later_is_not_yet_runnable(self):
        decision = portfolio.schedule(snapshot([candidate("m", ready_at=50.0)], now=10))
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.verdicts[0].reason, "NOT_YET_RUNNABLE")

    def test_nothing_eligible_selects_nothing_and_still_explains(self):
        decision = portfolio.schedule(snapshot(
            [candidate("m", "ghost")], {"alpha": portfolio.ProjectPolicy("alpha", "r")}))
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.reason, "NO_ELIGIBLE_MISSION")
        self.assertEqual(decision.verdicts[0].reason, "PROJECT_UNREGISTERED")


class FairnessTests(unittest.TestCase):
    """Priority must not permit permanent starvation."""

    def test_a_low_priority_mission_eventually_overtakes_a_high_priority_one(self):
        projects = {"urgent": portfolio.ProjectPolicy("urgent", "r", priority=1),
                    "background": portfolio.ProjectPolicy("background", "r", priority=100)}
        starving = candidate("m-bg", "background", created_at=0.0)

        def against_a_fresh_urgent_mission(now):
            # The real starvation scenario is a *stream* of urgent work: every
            # competitor is newly created, so ageing never helps it.
            return snapshot([starving, candidate("m-urgent", "urgent", created_at=now)],
                            projects, now=now, aging_seconds=10.0)

        self.assertEqual(portfolio.schedule(against_a_fresh_urgent_mission(100.0)).selected,
                         "m-urgent")
        # The background mission overtakes once its age exceeds the priority gap
        # times the ageing interval -- (100 - 1) * 10 seconds here.  That bound
        # is finite for *any* pair of priorities, which is what makes starvation
        # impossible rather than merely unlikely.
        decision = portfolio.schedule(against_a_fresh_urgent_mission(1000.0))
        self.assertEqual(decision.selected, "m-bg")
        self.assertEqual(decision.reason, "STARVATION_PROMOTED")
        self.assertEqual(decision.verdicts[0].effective_priority, 0)

    def test_the_promotion_is_reported_only_when_ageing_actually_changed_the_order(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", priority=1),
                    "b": portfolio.ProjectPolicy("b", "r", priority=100)}
        decision = portfolio.schedule(snapshot(
            [candidate("m-a", "a", created_at=0.0), candidate("m-b", "b", created_at=0.0)],
            projects, now=50.0, aging_seconds=10.0))
        self.assertEqual(decision.selected, "m-a")
        self.assertEqual(decision.reason, "SCHEDULED")

    def test_ageing_can_be_switched_off_and_then_priority_is_absolute(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", priority=1),
                    "b": portfolio.ProjectPolicy("b", "r", priority=100)}
        decision = portfolio.schedule(snapshot(
            [candidate("m-b", "b", created_at=0.0), candidate("m-a", "a", created_at=1e6)],
            projects, now=1e9, aging_seconds=0.0))
        self.assertEqual(decision.selected, "m-a")
        self.assertEqual(decision.verdicts[0].aging_steps, 0)

    def test_the_effective_priority_is_recorded_for_every_candidate(self):
        decision = portfolio.schedule(snapshot(
            [candidate("m", created_at=0.0)], now=1000.0, aging_seconds=100.0))
        verdict = decision.verdicts[0]
        self.assertEqual(verdict.aging_steps, 10)
        self.assertEqual(verdict.effective_priority, portfolio.DEFAULT_PRIORITY - 10)
        self.assertEqual(verdict.detail["base_priority"], portfolio.DEFAULT_PRIORITY)


class CapacityTests(unittest.TestCase):
    def test_a_project_cap_defers_its_own_work_and_not_another_project(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", priority=1, concurrency_cap=1),
                    "b": portfolio.ProjectPolicy("b", "r", priority=50)}
        decision = portfolio.schedule(snapshot(
            [candidate("m-a", "a"), candidate("m-b", "b")], projects,
            in_flight={"a": 1}, portfolio_in_flight=1, portfolio_concurrency=4))
        self.assertEqual(decision.selected, "m-b")
        deferred = next(v for v in decision.verdicts if v.mission_id == "m-a")
        self.assertEqual(deferred.reason, "PROJECT_CONCURRENCY_CAP")
        self.assertEqual(deferred.detail, {"in_flight": 1, "cap": 1})

    def test_the_portfolio_cap_defers_everything(self):
        decision = portfolio.schedule(snapshot(
            [candidate("m1"), candidate("m2")], portfolio_in_flight=4, portfolio_concurrency=4))
        self.assertIsNone(decision.selected)
        self.assertEqual({v.reason for v in decision.verdicts}, {"PORTFOLIO_CONCURRENCY_CAP"})

    def test_a_cap_of_zero_admits_nothing_from_that_project(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", concurrency_cap=0)}
        decision = portfolio.schedule(snapshot([candidate("m", "a")], projects))
        self.assertIsNone(decision.selected)

    def test_a_resume_is_not_charged_against_either_cap(self):
        """Resuming is not new work, so a full portfolio must not orphan it."""

        decision = portfolio.schedule(snapshot(
            [candidate("m", state="evaluated")], portfolio_in_flight=99, portfolio_concurrency=1))
        self.assertEqual(decision.selected, "m")

    def test_a_paused_project_is_skipped_and_its_state_is_in_the_verdict(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", state="paused")}
        decision = portfolio.schedule(snapshot([candidate("m", "a")], projects))
        self.assertEqual(decision.verdicts[0].reason, "PROJECT_NOT_ADMITTING")
        self.assertEqual(decision.verdicts[0].detail["project_state"], "paused")

    def test_every_non_enabled_state_stops_new_work(self):
        for state in ("paused", "draining", "stopped"):
            projects = {"a": portfolio.ProjectPolicy("a", "r", state=state)}
            self.assertIsNone(portfolio.schedule(snapshot([candidate("m", "a")], projects)).selected)


class BudgetAdmissionTests(unittest.TestCase):
    def test_measured_spend_at_the_ceiling_refuses_the_next_dispatch(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", budget_ceiling=10.0,
                                                 budget_currency="USD")}
        under = snapshot([candidate("m", "a")], projects,
                         spend={"a": {"known_spend": 9.99, "currency": "USD"}})
        self.assertEqual(portfolio.schedule(under).selected, "m")
        at = snapshot([candidate("m", "a")], projects,
                      spend={"a": {"known_spend": 10.0, "currency": "USD", "unpriced_legs": 3}})
        decision = portfolio.schedule(at)
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.verdicts[0].reason, "PROJECT_BUDGET_EXHAUSTED")
        self.assertEqual(decision.verdicts[0].detail["unpriced_legs"], 3)

    def test_unknown_cost_is_not_counted_toward_a_ceiling(self):
        """Unknown is not zero and it is not a charge either."""

        projects = {"a": portfolio.ProjectPolicy("a", "r", budget_ceiling=1.0,
                                                 budget_currency="USD")}
        decision = portfolio.schedule(snapshot(
            [candidate("m", "a")], projects,
            spend={"a": {"known_spend": 0.0, "currency": None, "unpriced_legs": 400}}))
        self.assertEqual(decision.selected, "m")

    def test_a_foreign_currency_is_refused_rather_than_converted(self):
        projects = {"a": portfolio.ProjectPolicy("a", "r", budget_ceiling=10.0,
                                                 budget_currency="USD")}
        decision = portfolio.schedule(snapshot(
            [candidate("m", "a")], projects,
            spend={"a": {"known_spend": 1.0, "currency": "EUR"}}))
        self.assertEqual(decision.verdicts[0].reason, "PROJECT_BUDGET_CURRENCY_MISMATCH")

    def test_a_project_with_no_ceiling_is_never_budget_refused(self):
        decision = portfolio.schedule(snapshot(
            [candidate("m")], spend={"alpha": {"known_spend": 1e9, "currency": "USD"}}))
        self.assertEqual(decision.selected, "m")


class ClaimTests(PortfolioTestCase, unittest.TestCase):
    """The store must build the snapshot the pure scheduler was tested on."""

    def test_the_claim_records_one_explanation_naming_every_candidate(self):
        controller, store, clock, _ = self.portfolio_store()
        self.register(store, "alpha", priority=1)
        self.register(store, "beta", priority=90)
        first = self.submit(controller, "m-alpha", "alpha")
        clock.advance(1)
        second = self.submit(controller, "m-beta", "beta")
        claimed = store.claim("w1")
        self.assertEqual(claimed["id"], first)
        row = store.coordination()[-1]
        self.assertEqual(row["decision"], "claim")
        self.assertEqual(row["reason"], "SCHEDULED")
        self.assertEqual({v["mission_id"] for v in row["detail"]["considered"]}, {first, second})

    def test_an_idle_poll_writes_nothing(self):
        _, store, _, _ = self.portfolio_store()
        self.assertIsNone(store.claim("w1"))
        self.assertIsNone(store.claim("w1"))
        self.assertEqual(store.coordination(), [])

    def test_no_two_workers_claim_the_same_mission(self):
        controller, store, clock, _ = self.portfolio_store()
        self.register(store, "alpha", concurrency_cap=8)
        wanted = set()
        for index in range(8):
            wanted.add(self.submit(controller, "m%d" % index, "alpha"))
            clock.advance(1)
        store.set_portfolio_policy(portfolio.PortfolioPolicy(portfolio_concurrency=64))
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(name):
            while True:
                mission = store.claim(name, lease_seconds=300)
                if mission is None:
                    return
                with lock:
                    claimed.append(mission["id"])

        threads = [threading.Thread(target=worker, args=("w%d" % i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(claimed), sorted(wanted))
        self.assertEqual(len(claimed), len(set(claimed)))

    def test_caps_are_enforced_from_durable_state_after_a_restart(self):
        """A replacement process reads the caps; it does not remember them."""

        controller, store, clock, path = self.portfolio_store()
        self.register(store, "alpha", concurrency_cap=1)
        for index in range(3):
            self.submit(controller, "m%d" % index, "alpha")
            clock.advance(1)
        self.assertIsNotNone(store.claim("w1", lease_seconds=3600))
        from factory_controller.store import MissionStore
        replacement = MissionStore(path, clock=clock)
        self.assertIsNone(replacement.claim("w2"))
        self.assertEqual(replacement.coordination()[-1]["detail"]["considered"][0]["reason"],
                         "PROJECT_CONCURRENCY_CAP")
        self.assertEqual(replacement.project("alpha").concurrency_cap, 1)

    def test_schedule_preview_decides_nothing(self):
        controller, store, _, _ = self.portfolio_store()
        self.register(store, "alpha")
        mission_id = self.submit(controller, "m1", "alpha")
        preview = store.schedule_preview()
        self.assertEqual(preview["selected"], mission_id)
        self.assertIsNone(store.get(mission_id)["lease_token"])
        self.assertEqual([row["reason"] for row in store.coordination()],
                         ["PROJECT_REGISTERED"])


if __name__ == "__main__":
    unittest.main()
