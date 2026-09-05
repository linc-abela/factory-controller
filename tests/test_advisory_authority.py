"""What an advisor may propose, and what happens when it does not answer.

Two claims are under test.  The advisor cannot reach past the Owner's policy
into admission, budgets, evidence, or execution.  And the Factory does not
depend on it: the same missions, scheduled with a good advisor, a broken one,
and none at all, come out in the same order.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory_controller import advisor, portfolio
from tests.support import PortfolioTestCase


def policy(**extra):
    base = {"enabled": True,
            "allowed_kinds": list(advisor.PROPOSAL_KINDS),
            "priority_min": 1, "priority_max": 100,
            "allowed_profiles": ["reviewer"], "max_proposals": 8}
    base.update(extra)
    return advisor.AdvisorPolicy.from_payload({"advisor_policy": base})


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.facts = advisor.Facts(projects=("alpha", "beta"), missions=("m1", "m2"), edges={})

    def review(self, *proposals, **extra):
        return advisor.review(list(proposals), policy(**extra), self.facts)

    def test_a_forbidden_kind_is_refused_before_the_allowlist_is_consulted(self):
        """These are the boundary, not omissions from a list."""

        for kind in advisor.FORBIDDEN_KINDS:
            verdict = self.review({"kind": kind}, allowed_kinds=list(advisor.FORBIDDEN_KINDS))[0]
            self.assertFalse(verdict.accepted)
            self.assertEqual(verdict.code, "ADVISOR_AUTHORITY_BOUNDARY")

    def test_a_disabled_advisor_can_propose_nothing_at_all(self):
        verdicts = advisor.review([{"kind": "next_mission", "mission_id": "m1"}],
                                  advisor.AdvisorPolicy(), self.facts)
        self.assertEqual(verdicts[0].code, "ADVISOR_DISABLED")

    def test_a_kind_outside_the_grant_is_refused(self):
        verdict = self.review({"kind": "project_priority", "project_id": "alpha", "priority": 5},
                              allowed_kinds=["next_mission"])[0]
        self.assertEqual(verdict.code, "ADVISOR_KIND_NOT_PERMITTED")

    def test_a_priority_outside_the_owner_band_is_refused(self):
        for value in (0, 101, -5):
            verdict = self.review({"kind": "project_priority", "project_id": "alpha",
                                   "priority": value})[0]
            self.assertEqual(verdict.code, "ADVISOR_PRIORITY_OUT_OF_BOUNDS")
            self.assertEqual(verdict.detail["bounds"], [1, 100])

    def test_an_unregistered_project_is_a_creation_attempt(self):
        verdict = self.review({"kind": "project_priority", "project_id": "gamma",
                               "priority": 5})[0]
        self.assertEqual(verdict.code, "ADVISOR_UNKNOWN_PROJECT")

    def test_a_specialist_profile_must_already_be_admitted(self):
        """The agent palette stays lean because an advisor cannot widen it."""

        self.assertTrue(self.review({"kind": "specialist_profile", "mission_id": "m1",
                                     "profile": "reviewer"})[0].accepted)
        self.assertEqual(self.review({"kind": "specialist_profile", "mission_id": "m1",
                                      "profile": "novel-specialist"})[0].code,
                         "ADVISOR_PROFILE_NOT_ALLOWLISTED")

    def test_a_decomposition_carrying_its_own_admission_is_refused_whole(self):
        for field in ("execution_mode", "acceptance_gate_ids", "context_manifest_hash",
                      "idempotency_key", "gateway_policy", "advisor_policy"):
            verdict = self.review({"kind": "decompose", "children": [
                {"work_item_id": "child", field: "anything"}]})[0]
            self.assertEqual(verdict.code, "ADVISOR_ADMISSION_FIELD_FORBIDDEN")
            self.assertEqual(verdict.detail["field"], field)

    def test_a_decomposition_naming_an_unregistered_project_is_refused(self):
        verdict = self.review({"kind": "decompose", "children": [
            {"work_item_id": "child", "project_id": "gamma"}]})[0]
        self.assertEqual(verdict.code, "ADVISOR_UNKNOWN_PROJECT")

    def test_a_cycle_cannot_be_split_across_two_proposals(self):
        """Each accepted edge joins the graph the next proposal is checked against."""

        verdicts = self.review({"kind": "dependency_edge", "mission_id": "m1", "depends_on": "m2"},
                               {"kind": "dependency_edge", "mission_id": "m2", "depends_on": "m1"})
        self.assertTrue(verdicts[0].accepted)
        self.assertEqual(verdicts[1].code, "ADVISOR_DEPENDENCY_CYCLE")
        self.assertEqual(verdicts[1].detail["cycle"], ["m2", "m1", "m2"])

    def test_proposals_past_the_limit_are_refused_rather_than_truncated(self):
        proposals = [{"kind": "next_mission", "mission_id": "m1"} for _ in range(4)]
        verdicts = self.review(*proposals, max_proposals=2)
        self.assertEqual([v.accepted for v in verdicts], [True, True, False, False])
        self.assertEqual(verdicts[3].code, "ADVISOR_PROPOSAL_LIMIT_EXCEEDED")

    def test_malformed_shapes_are_refused_individually(self):
        verdicts = self.review("not an object", {"no_kind": 1},
                               {"kind": "dependency_edge", "mission_id": "m1",
                                "depends_on": "m2", "on_failure": "explode"})
        self.assertEqual([v.code for v in verdicts],
                         ["ADVISOR_MALFORMED_PROPOSAL"] * 3)

    def test_a_response_that_is_not_a_list_of_proposals_is_one_refusal(self):
        verdicts = advisor.review({"proposals": "yes"}, policy(), self.facts)
        self.assertEqual(verdicts[0].code, "ADVISOR_MALFORMED_RESPONSE")


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        self.facts = advisor.Facts(projects=("alpha",), missions=("m1",), edges={})

    def test_silence_is_an_outcome_not_a_failure(self):
        outcome = advisor.consult(advisor.StaticAdvisor(None), {}, policy(), self.facts)
        self.assertEqual(outcome.status, "silent")
        self.assertEqual(outcome.refusal_code, "ADVISOR_SILENT")

    def test_an_exception_from_the_advisor_never_escapes(self):
        class Exploding:
            def advise(self, request):
                raise RuntimeError("connection refused")

        outcome = advisor.consult(Exploding(), {}, policy(), self.facts)
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.refusal_code, "ADVISOR_UNAVAILABLE")
        self.assertEqual(outcome.detail["error"], "RuntimeError")

    def test_a_non_object_response_is_malformed(self):
        outcome = advisor.consult(advisor.StaticAdvisor([1, 2, 3]), {}, policy(), self.facts)
        self.assertEqual(outcome.status, "malformed")

    def test_an_absent_port_is_distinguished_from_a_disabled_policy(self):
        self.assertEqual(advisor.consult(None, {}, policy(), self.facts).refusal_code,
                         "ADVISOR_ABSENT")
        self.assertEqual(advisor.consult(advisor.StaticAdvisor({}), {},
                                         advisor.AdvisorPolicy(), self.facts).refusal_code,
                         "ADVISOR_DISABLED")

    def test_a_disabled_advisor_is_never_even_asked(self):
        port = advisor.StaticAdvisor({"proposals": []})
        advisor.consult(port, {}, advisor.AdvisorPolicy(), self.facts)
        self.assertEqual(port.requests, [])


class ApplicationTests(PortfolioTestCase, unittest.TestCase):
    def build(self):
        controller, store, clock, path = self.portfolio_store()
        self.register(store, "alpha", priority=50)
        first = self.submit(controller, "m1", "alpha")
        clock.advance(1)
        second = self.submit(controller, "m2", "alpha")
        return controller, store, first, second

    def test_the_two_applicable_kinds_move_state_and_the_rest_do_not(self):
        controller, store, first, second = self.build()
        port = advisor.StaticAdvisor({"proposals": [
            {"kind": "dependency_edge", "mission_id": second, "depends_on": first},
            {"kind": "project_priority", "project_id": "alpha", "priority": 7},
            {"kind": "next_mission", "mission_id": second},
            {"kind": "specialist_profile", "mission_id": first, "profile": "reviewer"},
            {"kind": "decompose", "children": [{"work_item_id": "child"}]},
        ]})
        row = advisor.coordinate(store, port, {
            "enabled": True, "allowed_kinds": list(advisor.PROPOSAL_KINDS),
            "priority_min": 1, "priority_max": 100, "allowed_profiles": ["reviewer"]})
        effects = [item["effect"] for item in row["applied"]]
        self.assertEqual(effects, ["edge_added", "priority_set",
                                   "recorded_only", "recorded_only", "recorded_only"])
        self.assertEqual(store.project("alpha").priority, 7)
        self.assertEqual(store.dependency_status(second)["reading"], "waiting")
        self.assertEqual(len(store.all_missions()), 2, "no child mission was created")

    def test_the_whole_consultation_lands_in_the_coordination_ledger(self):
        controller, store, first, _ = self.build()
        advisor.coordinate(store, advisor.StaticAdvisor(None), {"enabled": True})
        row = store.coordination()[-1]
        self.assertEqual(row["decision"], "advisor")
        self.assertEqual(row["reason"], "ADVISOR_SILENT")

    def test_an_accepted_proposal_can_still_lose_to_the_store(self):
        """Review is not a lock; facts may move between review and application."""

        controller, store, first, second = self.build()
        store.add_dependency(first, second)

        class StaleView:
            """A store whose graph moved after the advisor was handed the facts."""

            def __init__(self, real):
                self.real = real

            def dependency_graph(self):
                return {}

            def __getattr__(self, name):
                return getattr(self.real, name)

        row = advisor.coordinate(StaleView(store), advisor.StaticAdvisor({"proposals": [
            {"kind": "dependency_edge", "mission_id": second, "depends_on": first}]}), {
            "enabled": True, "allowed_kinds": ["dependency_edge"]})
        self.assertEqual(row["applied"], [])
        self.assertEqual(row["application_refused"][0]["code"], "ADVISOR_APPLICATION_REFUSED")

    def test_the_schedule_is_identical_with_a_good_advisor_a_broken_one_and_none(self):
        """The strongest form of 'the advisor is optional'."""

        class Broken:
            def advise(self, request):
                raise OSError("no route to host")

        orders = []
        for port in (None, Broken(), advisor.StaticAdvisor({"proposals": [
                {"kind": "next_mission", "mission_id": "does-not-exist"}]})):
            controller, store, clock, _ = self.portfolio_store()
            self.register(store, "alpha", priority=5, concurrency_cap=4)
            self.register(store, "beta", priority=60, concurrency_cap=4)
            for index in range(4):
                self.submit(controller, "m%d" % index, "alpha" if index % 2 else "beta")
                clock.advance(1)
            advisor.coordinate(store, port, {"enabled": True,
                                             "allowed_kinds": ["next_mission"]})
            order = []
            while True:
                claimed = store.claim("w", lease_seconds=3600)
                if claimed is None:
                    break
                order.append(claimed["payload"]["work_item_id"])
            orders.append(order)
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[1], orders[2])
        self.assertEqual(len(orders[0]), 4)


class LiveEndpointTests(unittest.TestCase):
    """Measured against whatever is actually on this host, and never required."""

    def test_the_probe_reports_absence_without_raising(self):
        port = advisor.endpoint_advisor("http://127.0.0.1:1")
        result = port.probe()
        self.assertFalse(result["present"])
        self.assertEqual(result["reason"], "ADVISOR_ENDPOINT_UNREACHABLE")

    def test_consulting_without_a_credential_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(advisor.Path, "home", return_value=Path(tmp)):
                port = advisor.endpoint_advisor()
                outcome = advisor.consult(port, {}, policy(), advisor.Facts())
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.detail["detail"], "ADVISOR_CREDENTIAL_ABSENT")

    def test_the_probe_never_returns_a_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(advisor.Path, "home", return_value=Path(tmp)):
                self.assertNotIn("token", advisor.endpoint_advisor(token=None).probe())


class SessionGrantTests(unittest.TestCase):
    def _home(self, body):
        root = Path(tempfile.mkdtemp())
        store = root / ".hermes"
        store.mkdir()
        (store / "auth.json").write_text(json.dumps(body))
        return root

    def test_a_provider_model_key_is_not_an_http_session(self):
        home = self._home({
            "providers": {},
            "pool": {"lmstudio": [{"auth_type": "api_key", "source": "env"}]},
        })
        with patch.object(advisor.Path, "home", return_value=home):
            self.assertIsNone(advisor.runtime_session())

    def test_a_local_http_session_grant_is_used(self):
        home = self._home({
            "providers": {
                "local": {
                    "auth_type": "session",
                    "base_url": "http://127.0.0.1:9119",
                    "session": "fixture-session",
                }
            }
        })
        with patch.object(advisor.Path, "home", return_value=home):
            self.assertEqual(advisor.runtime_session(), "fixture-session")
            self.assertIsNotNone(advisor.HermesAdvisor().token)

    def test_an_explicit_session_wins_over_the_store(self):
        home = self._home({
            "providers": {
                "local": {
                    "auth_type": "session",
                    "base_url": "http://127.0.0.1:9119",
                    "session": "stored-session",
                }
            }
        })
        with patch.object(advisor.Path, "home", return_value=home):
            self.assertEqual(advisor.runtime_session("cli-session"), "cli-session")


class RecordingOpener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.get_full_url())
        payload = self.payloads.pop(0)

        class Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return json.dumps(payload).encode()

        return Resp()


class KanbanJudgeTests(unittest.TestCase):
    def test_decompose_failure_is_missing_model_not_a_judgment(self):
        opener = RecordingOpener([
            {"task": {"id": "t_1"}},
            {"ok": False, "reason": "LLM error: RuntimeError", "child_ids": []},
        ])
        port = advisor.HermesAdvisor(token="fixture-session", opener=opener)
        with self.assertRaises(PermissionError) as raised:
            port.judge({"work_items": [{"work_item_id": "factory-maintenance:SF-202"}]})
        self.assertEqual(str(raised.exception), "ADVISOR_MODEL_ABSENT")
        self.assertTrue(opener.urls[0].endswith("/api/plugins/kanban/tasks"))
        self.assertTrue(opener.urls[1].endswith("/api/plugins/kanban/tasks/t_1/decompose"))

    def test_a_successful_decompose_is_mapped_into_reviewable_proposals(self):
        opener = RecordingOpener([
            {"task": {"id": "t_2"}},
            {"ok": True, "reason": "split implementer from reviewer",
             "fanout": True, "child_ids": ["c1", "c2"]},
        ])
        port = advisor.HermesAdvisor(token="fixture-session", opener=opener)
        body = port.judge({"mission_id": "fm_1"})
        self.assertEqual(body["reasoning"], "split implementer from reviewer")
        self.assertEqual(body["proposals"][0]["kind"], "decompose")
        self.assertEqual(
            [row["work_item_id"] for row in body["proposals"][0]["children"]],
            ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
