"""Two missions, two registered repositories, one store: nothing may cross.

This is a correctness seam for the later coordination stage, not a scheduler.
It proves the join keys already separate everything a multi-project Controller
would otherwise have to separate later: route history, gate outcomes,
idempotency identity, candidate and evidence pointers, and budget accounting.
"""

from __future__ import annotations

import unittest

from tests.support import ALPHA, BETA, LayerAdapter, RouteTestCase, mission_payload


ALPHA_REPO = "git@example.com:project-alpha.git"
BETA_REPO = "git@example.com:project-beta.git"


class ProjectAdapter(LayerAdapter):
    """Answers differently per repository, so a mix-up is visible in the data."""

    PROFILES = {ALPHA_REPO: ALPHA, BETA_REPO: BETA}

    def execute(self, step, operation_key, value):
        mission = value.get("mission", {})
        repo = mission.get("repository_remote_url")
        if step == "dispatch":
            response = super().execute(step, operation_key, value)
            if response["status"] == "completed":
                response["candidate_sha"] = _sha(repo)
                response["receipt"]["usage"] = {
                    "cost_amount": 1.0 if repo == ALPHA_REPO else 7.0,
                    "cost_currency": "USD", "cost_state": "reported"}
            return response
        if step == "evaluate":
            return {"passed": True,
                    "gate_outcomes": [{"gate_id": gate, "passed": True, "detail": repo}
                                      for gate in mission["acceptance_gate_ids"]]}
        if step == "evidence":
            return {"accepted": True, "evidence_pointer": _sha(repo) * 2}
        return super().execute(step, operation_key, value)


def _sha(repo: str) -> str:
    return ("a" if repo == ALPHA_REPO else "b") * 40


def _payload(repo: str, gate: str, profile: str) -> dict:
    return mission_payload(
        repository_remote_url=repo,
        acceptance_gate_ids=[gate],
        provider_candidates=[profile],
        execution_policy={"budget_ceiling": 5, "budget_currency": "USD"},
    )


class MultiProjectIsolationTests(RouteTestCase, unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ProjectAdapter()
        self.controller, self.store, _ = self.build(self.adapter)
        self.alpha, _ = self.controller.submit(
            _payload(ALPHA_REPO, "GATE-ALPHA", ALPHA), "project-alpha:1")
        self.beta, _ = self.controller.submit(
            _payload(BETA_REPO, "GATE-BETA", BETA), "project-beta:1")

    def _run_both(self):
        first = self.controller.work_once("w1")
        second = self.controller.work_once("w1")
        return first, second

    def test_the_two_missions_have_distinct_identities(self):
        self.assertNotEqual(self.alpha["id"], self.beta["id"])
        self.assertNotEqual(self.alpha["idempotency_key"], self.beta["idempotency_key"])

    def test_one_projects_key_cannot_be_reused_by_the_other(self):
        with self.assertRaises(Exception):
            self.controller.submit(_payload(BETA_REPO, "GATE-BETA", BETA), "project-alpha:1")

    def test_route_history_never_mixes_across_projects(self):
        self._run_both()
        alpha_route = self.store.route_history(self.alpha["id"])
        beta_route = self.store.route_history(self.beta["id"])
        self.assertEqual(alpha_route["selected_provider_profile"], ALPHA)
        self.assertEqual(beta_route["selected_provider_profile"], BETA)
        self.assertEqual({leg["idempotency_key"] for leg in alpha_route["legs"]},
                         {"project-alpha:1"})
        self.assertEqual({leg["idempotency_key"] for leg in beta_route["legs"]},
                         {"project-beta:1"})

    def test_gate_outcomes_stay_with_their_own_repository(self):
        self._run_both()
        for mission, repo, gate in ((self.alpha, ALPHA_REPO, "GATE-ALPHA"),
                                    (self.beta, BETA_REPO, "GATE-BETA")):
            outcomes = self.store.get(mission["id"])["result"]["evaluation"]["gate_outcomes"]
            self.assertEqual([item["gate_id"] for item in outcomes], [gate])
            self.assertEqual(outcomes[0]["detail"], repo)

    def test_candidate_and_evidence_pointers_stay_separate(self):
        self._run_both()
        alpha_result = self.store.get(self.alpha["id"])["result"]
        beta_result = self.store.get(self.beta["id"])["result"]
        self.assertEqual(alpha_result["dispatch"]["candidate_sha"], "a" * 40)
        self.assertEqual(beta_result["dispatch"]["candidate_sha"], "b" * 40)
        self.assertNotEqual(alpha_result["evidence"]["evidence_pointer"],
                            beta_result["evidence"]["evidence_pointer"])

    def test_budget_accounting_is_per_mission(self):
        """Beta spends 7 of a 5 ceiling; alpha spends 1 and is untouched by it."""

        self._run_both()
        alpha_cost = self.store.telemetry(self.alpha["id"])["reported_cost"]
        beta_cost = self.store.telemetry(self.beta["id"])["reported_cost"]
        self.assertEqual(alpha_cost["amount"], 1.0)
        self.assertEqual(beta_cost["amount"], 7.0)

    def test_an_exhausted_project_does_not_block_the_other(self):
        self._run_both()
        # Beta is over its ceiling; a fresh beta mission refuses while a fresh
        # alpha mission still runs.
        exhausted, _ = self.controller.submit(
            _payload(BETA_REPO, "GATE-BETA", BETA), "project-beta:2")
        # A provider that declined but still billed for the tokens it read before
        # declining: nothing ran, and the spend is real.
        self.store.record_run(
            exhausted["id"], 0, {"reason": "seed", "considered": []},
            {"profile": BETA, "classification": "provider_unavailable", "process_started": False,
             "provider": None, "selection_reason": "seed", "fallback_chain": [],
             "duration_ms": None, "refusal_code": None, "execution_mode": "fixture",
             "idempotency_key": "project-beta:2", "evidence_class": "reported_claim",
             "usage": {"input_tokens": None, "output_tokens": None, "cost_amount": 9.0,
                       "cost_currency": "USD", "cost_state": "reported"}},
            "project-beta:2")
        blocked = self.controller.work_once("w1")
        self.assertEqual(blocked["id"], exhausted["id"])
        self.assertIn("MISSION_BUDGET_EXHAUSTED", blocked["terminal_reason"])

        fresh, _ = self.controller.submit(
            _payload(ALPHA_REPO, "GATE-ALPHA", ALPHA), "project-alpha:2")
        ran = self.controller.work_once("w1")
        self.assertEqual(ran["id"], fresh["id"])
        self.assertEqual(ran["state"], "completed")

    def test_a_mission_query_returns_only_its_own_rows(self):
        self._run_both()
        alpha_ids = {leg["mission_id"] for leg in self.store.runs(self.alpha["id"])}
        beta_ids = {leg["mission_id"] for leg in self.store.runs(self.beta["id"])}
        self.assertEqual(alpha_ids, {self.alpha["id"]})
        self.assertEqual(beta_ids, {self.beta["id"]})
        self.assertEqual(alpha_ids & beta_ids, set())
        alpha_events = {event["mission_id"] for event in self.store.history(self.alpha["id"])}
        self.assertEqual(alpha_events, {self.alpha["id"]})


if __name__ == "__main__":
    unittest.main()
