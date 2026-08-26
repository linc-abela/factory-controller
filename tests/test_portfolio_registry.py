"""The project registry, the dependency graph, and the controls over both.

Everything here is about durable state that Stage 4 did not have: which
projects exist, what the Owner allows each one, what waits on what, and what
happens when a prerequisite does not arrive.
"""

from __future__ import annotations

import unittest

from factory_controller import portfolio
from factory_controller.store import ConflictError
from tests.support import LayerAdapter, PortfolioTestCase


class ProjectRegistryTests(PortfolioTestCase, unittest.TestCase):
    def test_a_project_is_identity_repository_and_an_owner_envelope(self):
        _, store, _, _ = self.portfolio_store()
        row = self.register(store, "alpha", priority=10, concurrency_cap=3,
                            budget_ceiling=25.0, budget_currency="USD",
                            context_ceiling_bytes=200_000, policy_version="SF-137:v1")
        self.assertEqual(row["repository"], "repo://alpha")
        stored = store.project("alpha")
        self.assertEqual(stored.priority, 10)
        self.assertEqual(stored.policy_version, "SF-137:v1")
        self.assertTrue(stored.admits_new_work)

    def test_a_budget_ceiling_without_a_currency_is_refused(self):
        """An unpriceable ceiling is advice, not a budget."""

        with self.assertRaises(portfolio.PolicyError):
            portfolio.ProjectPolicy(project_id="a", repository="r", budget_ceiling=10.0)

    def test_registering_twice_updates_rather_than_duplicating(self):
        _, store, _, _ = self.portfolio_store()
        self.register(store, "alpha", priority=10)
        self.register(store, "alpha", priority=1, concurrency_cap=9)
        self.assertEqual(len(store.projects()), 1)
        self.assertEqual(store.project("alpha").priority, 1)
        reasons = [row["reason"] for row in store.coordination()]
        self.assertEqual(reasons, ["PROJECT_REGISTERED", "PROJECT_UPDATED"])

    def test_every_registry_change_is_explained_in_the_ledger(self):
        _, store, _, _ = self.portfolio_store()
        self.register(store, "alpha")
        store.set_project_state("alpha", "paused")
        store.emergency_stop(True)
        rows = store.coordination()
        self.assertEqual([row["decision"] for row in rows], ["registry"] * 3)
        self.assertEqual(rows[1]["detail"]["from"], "enabled")
        self.assertEqual(rows[1]["detail"]["to"], "paused")
        self.assertTrue(rows[2]["detail"]["emergency_stop"])

    def test_pausing_touches_no_mission_row(self):
        """Durable mission state is exactly what a pause must not disturb."""

        controller, store, _, _ = self.portfolio_store()
        self.register(store, "alpha")
        mission_id = self.submit(controller, "m1", "alpha")
        before = store.get(mission_id)
        store.set_project_state("alpha", "paused")
        self.assertEqual(store.get(mission_id), before)
        self.assertIsNone(store.claim("w1"))
        store.set_project_state("alpha", "enabled")
        self.assertEqual(store.claim("w1")["id"], mission_id)

    def test_drain_reports_whether_the_project_is_quiet(self):
        controller, store, _, _ = self.portfolio_store()
        self.register(store, "alpha")
        self.submit(controller, "m1", "alpha")
        claimed = store.claim("w1")
        draining = store.set_project_state("alpha", "draining")
        self.assertEqual(draining["in_flight"], 1)
        self.assertFalse(draining["drained"])
        store.transition(claimed["id"], claimed["lease_token"], "refused",
                         reason="X", release_lease=True)
        self.assertTrue(store.set_project_state("alpha", "draining")["drained"])

    def test_an_unregistered_project_id_is_refused_not_defaulted(self):
        controller, store, _, _ = self.portfolio_store()
        self.submit(controller, "m1", "ghost")
        self.assertIsNone(store.claim("w1"))
        verdict = store.coordination()[-1]["detail"]["considered"][0]
        self.assertEqual(verdict["reason"], "PROJECT_UNREGISTERED")

    def test_a_mission_with_no_project_still_runs_under_portfolio_limits(self):
        """The Stage-4 shape keeps working; NULL means 'portfolio only'."""

        controller, store, _, _ = self.portfolio_store()
        mission_id = self.submit(controller, "m1")
        self.assertEqual(store.claim("w1")["id"], mission_id)


class EmergencyStopTests(PortfolioTestCase, unittest.TestCase):
    def test_emergency_stop_prevents_every_new_claim_across_projects(self):
        controller, store, _, _ = self.portfolio_store()
        for name in ("alpha", "beta"):
            self.register(store, name)
            self.submit(controller, "m-" + name, name)
        store.emergency_stop(True)
        self.assertIsNone(store.claim("w1"))
        reasons = {v["reason"] for v in store.coordination()[-1]["detail"]["considered"]}
        self.assertEqual(reasons, {"PORTFOLIO_EMERGENCY_STOP"})
        store.emergency_stop(False, reason="owner cleared")
        self.assertIsNotNone(store.claim("w1"))

    def test_emergency_stop_still_lets_a_dispatched_mission_be_recovered(self):
        """Orphaning a run that already had effects is the corruption, not the cure."""

        controller, store, clock, _ = self.portfolio_store()
        self.register(store, "alpha")
        mission_id = self.submit(controller, "m1", "alpha")
        claimed = store.claim("w1", lease_seconds=1)
        store.transition(claimed["id"], claimed["lease_token"], "dispatched",
                         detail={"candidate_sha": "a" * 40})
        clock.advance(5)
        store.recover_stale()
        store.emergency_stop(True)
        resumed = store.claim("w2")
        self.assertEqual(resumed["id"], mission_id)
        self.assertEqual(resumed["state"], "dispatched")


class DependencyGraphTests(PortfolioTestCase, unittest.TestCase):
    def build_chain(self):
        controller, store, clock, path = self.portfolio_store()
        self.register(store, "alpha")
        first = self.submit(controller, "m1", "alpha")
        second = self.submit(controller, "m2", "alpha")
        return controller, store, clock, first, second

    def test_a_dependency_makes_the_dependent_unrunnable_until_completion(self):
        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first)
        self.assertEqual(store.dependency_status(second)["reading"], "waiting")
        self.assertEqual(store.claim("w1")["id"], first)

    def test_a_self_edge_is_a_cycle(self):
        _, store, _, first, _ = self.build_chain()
        with self.assertRaises(portfolio.PolicyError):
            store.add_dependency(first, first)

    def test_a_cycle_is_refused_with_the_path_that_would_close_it(self):
        controller, store, _, first, second = self.build_chain()
        third = self.submit(controller, "m3", "alpha")
        store.add_dependency(second, first)
        store.add_dependency(third, second)
        with self.assertRaises(portfolio.PolicyError) as caught:
            store.add_dependency(first, third)
        self.assertIn("DEPENDENCY_CYCLE", str(caught.exception))
        row = [r for r in store.coordination() if r["reason"] == "DEPENDENCY_CYCLE"][-1]
        self.assertEqual(row["detail"]["cycle"][0], first)
        self.assertEqual(row["detail"]["cycle"][-1], first)
        self.assertEqual(store.dependency_graph().get(first), None)

    def test_cycle_detection_is_a_pure_function_over_the_edges(self):
        edges = {"c": ["b"], "b": ["a"]}
        self.assertIsNone(portfolio.cycle_path(edges, "d", "c"))
        self.assertEqual(portfolio.cycle_path(edges, "a", "c"), ("a", "c", "b", "a"))

    def test_a_dependency_releases_exactly_once(self):
        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first)
        self.run_to_completion(controller, first)
        released = [r for r in store.coordination() if r["reason"] == "DEPENDENCY_RELEASED"]
        self.assertEqual(len(released), 1)
        self.assertEqual(store.dependency_status(second)["released"], 1)
        # A second terminal write for the same mission cannot release it again;
        # the guard is `released_at IS NULL` inside the claiming transaction.
        with store.transaction() as db:
            self.assertEqual(store._release_dependencies_locked(db, first), [])
        self.assertEqual(
            len([r for r in store.coordination() if r["reason"] == "DEPENDENCY_RELEASED"]), 1)
        self.assertEqual(store.dependency_status(second)["reading"], "ready")

    def test_an_edge_added_after_the_prerequisite_finished_is_born_released(self):
        controller, store, _, first, second = self.build_chain()
        self.run_to_completion(controller, first)
        store.add_dependency(second, first)
        self.assertEqual(store.dependency_status(second)["reading"], "ready")
        self.assertEqual(store.dependency_status(second)["released"], 1)

    def test_a_failed_prerequisite_blocks_its_dependent_by_default(self):
        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first)
        self.fail_mission(store, first)
        reading = store.dependency_status(second)
        self.assertEqual(reading["reading"], "blocked")
        self.assertEqual(reading["blocking"], (first,))
        self.assertIsNone(store.claim("w1"))
        verdict = next(v for v in store.coordination()[-1]["detail"]["considered"]
                       if v["mission_id"] == second)
        self.assertEqual(verdict["reason"], "DEPENDENCY_PREREQUISITE_FAILED")

    def test_blocking_needs_no_write_because_it_is_derived(self):
        """The dependent's own row is untouched by its prerequisite failing."""

        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first)
        before = store.get(second)
        self.fail_mission(store, first)
        self.assertEqual(store.get(second), before)
        self.assertEqual(store.dependency_status(second)["reading"], "blocked")

    def test_cancel_propagation_cancels_a_dependent_that_never_started(self):
        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first, on_failure="cancel")
        self.fail_mission(store, first)
        self.assertEqual(store.get(second)["state"], "cancelled")
        self.assertIn("DEPENDENCY_FAILURE_PROPAGATED", store.get(second)["terminal_reason"])

    def test_cancel_propagation_past_the_boundary_is_a_request_not_a_kill(self):
        controller, store, _, first, second = self.build_chain()
        claimed = store.claim("w1")
        while claimed["id"] != second:
            store.renew(claimed["id"], claimed["lease_token"], 300)
            claimed = store.claim("w1")
        store.transition(second, claimed["lease_token"], "dispatched",
                         detail={"candidate_sha": "a" * 40})
        store.add_dependency(second, first, on_failure="cancel")
        self.fail_mission(store, first)
        self.assertEqual(store.get(second)["state"], "dispatched")
        self.assertTrue(store.get(second)["cancel_requested"])

    def test_ignore_lets_the_dependent_proceed(self):
        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first, on_failure="ignore")
        self.fail_mission(store, first)
        self.assertEqual(store.dependency_status(second)["reading"], "ready")
        self.assertEqual(store.claim("w1")["id"], second)

    def test_a_cancelled_prerequisite_is_a_failure_not_a_completion(self):
        """The dependent waited for an artifact; a cancelled mission made none."""

        controller, store, _, first, second = self.build_chain()
        store.add_dependency(second, first)
        store.cancel(first)
        self.assertEqual(store.dependency_status(second)["reading"], "blocked")

    def test_an_edge_to_an_unknown_mission_is_refused(self):
        _, store, _, first, _ = self.build_chain()
        with self.assertRaises(KeyError):
            store.add_dependency(first, "fm_nothing")

    # -- helpers -------------------------------------------------------- #

    def run_to_completion(self, controller, mission_id):
        for _ in range(6):
            result = controller.work_once("runner")
            if result is None:
                break
            if result["id"] == mission_id and result["state"] == "completed":
                return result
        raise AssertionError("mission %s did not complete" % mission_id)

    @staticmethod
    def fail_mission(store, mission_id):
        claimed = store.claim("failer")
        while claimed is not None and claimed["id"] != mission_id:
            store.transition(claimed["id"], claimed["lease_token"], "refused",
                             reason="OTHER", release_lease=True)
            claimed = store.claim("failer")
        assert claimed is not None, "could not claim %s" % mission_id
        store.transition(mission_id, claimed["lease_token"], "failed",
                         reason="PREREQUISITE_FAILED", release_lease=True)


if __name__ == "__main__":
    unittest.main()
