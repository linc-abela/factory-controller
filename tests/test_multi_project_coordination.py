"""Three projects on one host, and what survives a restart.

The acceptance question for Stage 5 is not whether one mission works -- Stage 4
answered that -- but whether several projects can share a host without leaking
state, budget, or ordering into each other, and whether a crash at any point in
the coordination path leaves the durable record still true.
"""

from __future__ import annotations

import unittest

from factory_controller import advisor, portfolio
from factory_controller.store import MissionStore
from tests.support import Clock, LayerAdapter, PortfolioTestCase, ProcessDeath


PRICED = {"cost_amount": 1.0, "cost_currency": "USD", "input_tokens": 100}


class ThreeProjectTests(PortfolioTestCase, unittest.TestCase):
    """Dependency chains, independent work, mixed priorities, one failure."""

    def build_portfolio(self, adapter=None):
        controller, store, clock, path = self.portfolio_store(
            adapter, portfolio_concurrency=2, aging_seconds=60.0)
        self.register(store, "urgent", priority=1, concurrency_cap=1)
        self.register(store, "steady", priority=10, concurrency_cap=2)
        self.register(store, "background", priority=90, concurrency_cap=1)
        ids = {}
        for name, project in (("u-design", "urgent"), ("u-build", "urgent"),
                              ("s-solo", "steady"), ("s-second", "steady"),
                              ("b-chore", "background")):
            ids[name] = self.submit(controller, name, project)
            clock.advance(1)
        # urgent's build waits on its own design; nothing else waits on anything.
        store.add_dependency(ids["u-build"], ids["u-design"])
        return controller, store, clock, ids, path

    def drain(self, controller, limit=40):
        seen = []
        for _ in range(limit):
            result = controller.work_once("w1")
            if result is None:
                break
            seen.append(result["payload"]["work_item_id"])
        return seen

    def test_three_projects_coexist_and_each_keeps_its_own_state(self):
        controller, store, _, ids, _ = self.build_portfolio()
        self.drain(controller)
        states = {name: store.get(mission_id)["state"] for name, mission_id in ids.items()}
        self.assertEqual(set(states.values()), {"completed"})
        economics = store.portfolio_economics()
        self.assertEqual(economics["project_count"], 3)
        by_id = {group["project_id"]: group for group in economics["projects"]}
        self.assertEqual(by_id["urgent"]["missions"], 2)
        self.assertEqual(by_id["steady"]["missions"], 2)
        self.assertEqual(by_id["background"]["missions"], 1)
        self.assertEqual(economics["portfolio"]["missions"], 5)
        self.assertEqual(economics["portfolio"]["completed"], 5)

    def test_a_prerequisite_runs_before_its_dependent_whatever_the_priorities(self):
        controller, store, _, ids, _ = self.build_portfolio()
        order = self.drain(controller)
        self.assertLess(order.index("u-design"), order.index("u-build"))

    def test_priority_orders_the_independent_work_and_ageing_still_reaches_the_last(self):
        controller, store, _, ids, _ = self.build_portfolio()
        order = self.drain(controller)
        self.assertEqual(order[0], "u-design")
        self.assertLess(order.index("s-solo"), order.index("b-chore"))
        self.assertIn("b-chore", order)

    def test_one_failing_prerequisite_blocks_only_its_own_dependent(self):
        controller, store, _, ids, _ = self.build_portfolio(
            LayerAdapter(gates_pass=False))
        self.drain(controller)
        self.assertEqual(store.get(ids["u-design"])["state"], "escalated")
        self.assertEqual(store.dependency_status(ids["u-build"])["reading"], "waiting")
        self.assertEqual(store.get(ids["u-build"])["state"], "admitted")
        # Every other project finished; the failure did not spread.
        for name in ("s-solo", "s-second", "b-chore"):
            self.assertIn(store.get(ids[name])["state"], {"escalated", "completed"})

    def test_a_paused_project_stops_only_itself(self):
        controller, store, _, ids, _ = self.build_portfolio()
        store.set_project_state("urgent", "paused")
        order = self.drain(controller)
        self.assertNotIn("u-design", order)
        self.assertIn("s-solo", order)
        self.assertIn("b-chore", order)
        self.assertEqual(store.get(ids["u-design"])["state"], "admitted")

    def test_a_project_budget_stops_that_project_and_no_other(self):
        controller, store, _, ids, _ = self.build_portfolio(LayerAdapter(usage=PRICED))
        store.register_project(portfolio.ProjectPolicy(
            "steady", "repo://steady", priority=10, concurrency_cap=2,
            budget_ceiling=1.0, budget_currency="USD"))
        self.drain(controller)
        steady = [store.get(ids[name])["state"] for name in ("s-solo", "s-second")]
        self.assertIn("admitted", steady, "the second steady mission should be held")
        self.assertEqual(store.get(ids["b-chore"])["state"], "completed")
        refusals = [row for row in store.coordination()
                    if any(v["reason"] == "PROJECT_BUDGET_EXHAUSTED"
                           for v in row["detail"].get("considered", []))]
        self.assertTrue(refusals)

    def test_portfolio_economics_never_estimates_what_nobody_measured(self):
        controller, store, _, ids, _ = self.build_portfolio(LayerAdapter(usage=PRICED))
        self.drain(controller)
        economics = store.portfolio_economics()
        by_id = {group["project_id"]: group for group in economics["projects"]}
        self.assertEqual(by_id["urgent"]["provider_spend"]["known_spend"], 2.0)
        self.assertEqual(by_id["urgent"]["provider_spend"]["currency"], "USD")
        self.assertEqual(by_id["urgent"]["provider_spend"]["evidence_class"], "reported_claim")
        # No context request was declared, so context bytes are not measurable
        # -- not zero, and not confused with the spend that was measured.
        self.assertEqual(by_id["urgent"]["baseline_context_bytes"], "not_measurable")
        self.assertEqual(economics["portfolio"]["known_spend"], 5.0)

    def test_unpriced_legs_are_counted_but_never_summed(self):
        controller, store, _, ids, _ = self.build_portfolio()
        self.drain(controller)
        economics = store.portfolio_economics("urgent")["projects"][0]
        self.assertEqual(economics["provider_spend"]["known_spend"], "not_measurable")
        self.assertEqual(economics["provider_spend"]["currency"], "not_applicable")
        self.assertEqual(economics["provider_spend"]["unpriced_legs"], 2)

    def test_two_currencies_are_reported_rather_than_converted(self):
        controller, store, clock, _ = self.portfolio_store(LayerAdapter(usage=PRICED))
        self.register(store, "usd")
        self.submit(controller, "m-usd", "usd")
        controller.work_once("w1")
        controller.adapter = LayerAdapter(usage={"cost_amount": 2.0, "cost_currency": "EUR"})
        self.register(store, "eur")
        clock.advance(1)
        self.submit(controller, "m-eur", "eur")
        controller.work_once("w1")
        economics = store.portfolio_economics()
        self.assertEqual(economics["portfolio"]["known_spend"], "not_measurable")
        self.assertEqual(sorted(economics["portfolio"]["currencies"]), ["EUR", "USD"])


class CrashRecoveryTests(PortfolioTestCase, unittest.TestCase):
    """A crash at each coordination point, then a replacement process."""

    def two_projects(self, adapter):
        controller, store, clock, path = self.portfolio_store(adapter)
        self.register(store, "alpha", concurrency_cap=2)
        self.register(store, "beta", concurrency_cap=2)
        first = self.submit(controller, "m1", "alpha")
        clock.advance(1)
        second = self.submit(controller, "m2", "beta")
        return controller, store, clock, path, first, second

    def test_a_crash_before_dependency_release_leaves_the_dependent_waiting(self):
        adapter = LayerAdapter(crash_on="evidence")
        controller, store, clock, path, first, second = self.two_projects(adapter)
        store.add_dependency(second, first)
        with self.assertRaises(ProcessDeath):
            controller.work_once("w1")
        self.assertEqual(store.dependency_status(second)["reading"], "waiting")
        self.assertEqual(store.dependency_status(second)["released"], 0)
        clock.advance(120)
        replacement = self.reopen(path, LayerAdapter(), lease_seconds=0)
        replacement.store.clock = clock
        while replacement.work_once("w2") is not None:
            pass
        self.assertEqual(store.dependency_status(second)["reading"], "ready")
        self.assertEqual(store.dependency_status(second)["released"], 1)
        self.assertEqual(
            len([r for r in store.coordination() if r["reason"] == "DEPENDENCY_RELEASED"]), 1)

    def test_a_crash_after_dispatch_resumes_on_the_same_provider_under_a_pause(self):
        adapter = LayerAdapter(crash_on="verify")
        controller, store, clock, path, first, _ = self.two_projects(adapter)
        with self.assertRaises(ProcessDeath):
            controller.work_once("w1")
        self.assertEqual(store.get(first)["state"], "dispatched")
        store.set_project_state("alpha", "paused")
        clock.advance(120)
        replacement = self.reopen(path, LayerAdapter(), lease_seconds=0)
        replacement.store.clock = clock
        resumed = replacement.work_once("w2")
        self.assertEqual(resumed["id"], first)
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(len(store.runs(first)), 2)
        self.assertEqual({leg["provider_profile"] for leg in store.runs(first)}, {None})

    def test_a_lost_lease_returns_a_pre_dispatch_mission_to_the_queue(self):
        controller, store, clock, path, first, _ = self.two_projects(LayerAdapter())
        claimed = store.claim("w1", lease_seconds=1)
        self.assertEqual(claimed["state"], "dispatching")
        clock.advance(10)
        self.assertEqual(store.recover_stale(), 1)
        self.assertEqual(store.get(first)["state"], "admitted")
        self.assertEqual(store.claim("w2")["id"], first)

    def test_a_restart_re_reads_the_portfolio_policy_rather_than_remembering_it(self):
        controller, store, clock, path, first, second = self.two_projects(LayerAdapter())
        store.set_portfolio_policy(portfolio.PortfolioPolicy(
            portfolio_concurrency=1, policy_version="SF-137:v2"))
        store.claim("w1", lease_seconds=3600)
        replacement = MissionStore(path, clock=clock)
        self.assertEqual(replacement.portfolio_policy().policy_version, "SF-137:v2")
        self.assertIsNone(replacement.claim("w2"))
        self.assertEqual(replacement.coordination()[-1]["detail"]["considered"][0]["reason"],
                         "PORTFOLIO_CONCURRENCY_CAP")

    def test_an_emergency_stop_survives_a_restart(self):
        controller, store, clock, path, _, _ = self.two_projects(LayerAdapter())
        store.emergency_stop(True)
        replacement = MissionStore(path, clock=clock)
        self.assertTrue(replacement.portfolio_policy().emergency_stop)
        self.assertIsNone(replacement.claim("w2"))

    def test_an_advisor_consulted_before_a_crash_leaves_its_edges_behind(self):
        adapter = LayerAdapter(crash_on="dispatch")
        controller, store, clock, path, first, second = self.two_projects(adapter)
        advisor.coordinate(store, advisor.StaticAdvisor({"proposals": [
            {"kind": "dependency_edge", "mission_id": second, "depends_on": first}]}),
            {"enabled": True, "allowed_kinds": ["dependency_edge"]})
        with self.assertRaises(ProcessDeath):
            controller.work_once("w1")
        clock.advance(120)
        replacement = MissionStore(path, clock=clock)
        self.assertEqual(replacement.dependency_status(second)["reading"], "waiting")
        self.assertEqual([row["decision"] for row in replacement.coordination()
                          if row["decision"] == "advisor"], ["advisor"])

    def test_the_coordination_ledger_cannot_be_rewritten(self):
        _, store, _, _, _, _ = self.two_projects(LayerAdapter())
        store.coordinate("m", "alpha", "claim", "SCHEDULED", {})
        with store.transaction() as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE coordination SET reason='X'")
            with self.assertRaises(Exception):
                db.execute("DELETE FROM coordination")


class NoRuntimeStateOutsideTheDatabaseTests(PortfolioTestCase, unittest.TestCase):
    """Notion is the human work exchange.  No scheduler state may depend on it."""

    def test_no_runtime_module_names_a_work_exchange_or_a_chat_surface(self):
        import ast
        from pathlib import Path
        package = Path(__file__).resolve().parent.parent / "factory_controller"
        for path in sorted(package.glob("*.py")):
            lowered = path.read_text().lower()
            for token in ("notion", "slack", "discord", "chat.", "webhook"):
                self.assertNotIn(token, lowered, "%s names %r" % (path.name, token))
            ast.parse(path.read_text())

    def test_the_whole_coordination_path_runs_against_the_database_alone(self):
        """Registry, graph, schedule, ledger and economics, with no network."""

        import socket
        controller, store, clock, path = self.portfolio_store()
        self.register(store, "alpha")
        first = self.submit(controller, "m1", "alpha")
        clock.advance(1)
        second = self.submit(controller, "m2", "alpha")
        store.add_dependency(second, first)

        real_socket = socket.socket

        def refuse(*args, **kwargs):
            raise AssertionError("the coordination path opened a socket")

        socket.socket = refuse
        try:
            self.assertEqual(store.schedule_preview()["selected"], first)
            self.assertEqual(store.dependency_status(second)["reading"], "waiting")
            self.assertEqual(store.claim("w1")["id"], first)
            self.assertEqual(store.portfolio_economics()["project_count"], 1)
            self.assertTrue(store.coordination())
            advisor.coordinate(store, None, {"enabled": True})
        finally:
            socket.socket = real_socket

    def test_a_replacement_process_needs_only_the_database_file(self):
        controller, store, clock, path = self.portfolio_store()
        self.register(store, "alpha", priority=3, budget_ceiling=5.0, budget_currency="USD")
        mission_id = self.submit(controller, "m1", "alpha")
        replacement = MissionStore(path, clock=Clock())
        self.assertEqual(replacement.project("alpha").priority, 3)
        self.assertEqual(replacement.project("alpha").budget_ceiling, 5.0)
        self.assertEqual(replacement.schedule_preview()["selected"], mission_id)


if __name__ == "__main__":
    unittest.main()
