"""Two projects, one store: no context, cache, budget or economics may cross.

The Controller stores every project's missions in one database, so isolation
here is a property of the join keys rather than of separate files.  Each check
below picks one thing SF-136 says must not cross and shows the key that stops
it.  The aggregate view is the same seam the later coordination stage needs:
economics are already queryable per project without a storage redesign.
"""

from __future__ import annotations

import unittest

from factory_controller import context
from tests.support import ALPHA, BETA, RouteTestCase, mission_payload
from tests.test_context_binding import BrokerAdapter, ANCHOR


ALPHA_CORPUS = "vault://project-alpha@" + "1" * 40
BETA_CORPUS = "vault://project-beta@" + "2" * 40
ALPHA_REMOTE = "git@example.com:project-alpha.git"
BETA_REMOTE = "git@example.com:project-beta.git"


class ProjectBroker(BrokerAdapter):
    """One broker serving two projects.  It answers per corpus, so a crossed
    manifest would be visible in the data rather than merely improbable."""

    SIZES = {ALPHA_CORPUS: (4000, 400), BETA_CORPUS: (9000, 3000)}

    def execute(self, step, operation_key, value):
        if step == "evidence":
            corpus = value["mission"]["context_request"]["corpus_identity"]
            return {"accepted": True, "retryable": False,
                    "evidence_pointer": context.sha256_hex(corpus)}
        return super().execute(step, operation_key, value)

    def build(self, request):
        corpus = request["corpus_identity"]
        baseline, selected = self.SIZES[corpus]
        self.selected = (ANCHOR, corpus.rsplit("@", 1)[0].rsplit("/", 1)[-1] + ".md")
        self.measurement = {
            "baseline_context_bytes": baseline, "selected_context_bytes": selected,
            "selected_context_files": len(self.selected), "cache_state": "miss",
        }
        return super().build(request)


def project_payload(corpus, remote, profile, gate, **extra):
    return mission_payload(
        work_item_id="SF-136-" + gate,
        repository_remote_url=remote,
        baseline_sha="a" * 40,
        capability="implement",
        acceptance_gate_ids=[gate],
        provider_candidates=[profile],
        context_request={"corpus_identity": corpus, "policy_identity": "SF-136",
                         "required_anchors": [ANCHOR]},
        **extra)


class ContextIsolationTests(RouteTestCase, unittest.TestCase):
    def setUp(self):
        self.adapter = ProjectBroker()
        self.controller, self.store, _ = self.build(self.adapter)
        self.alpha, _ = self.controller.submit(
            project_payload(ALPHA_CORPUS, ALPHA_REMOTE, ALPHA, "GATE-ALPHA"),
            "ctx-alpha:1")
        self.beta, _ = self.controller.submit(
            project_payload(BETA_CORPUS, BETA_REMOTE, BETA, "GATE-BETA"), "ctx-beta:1")
        self.controller.work_once("w1")
        self.controller.work_once("w1")

    def test_both_missions_completed(self):
        for mission in (self.alpha, self.beta):
            self.assertEqual(self.store.get(mission["id"])["state"], "completed")

    def test_the_two_projects_bind_different_manifests(self):
        alpha = self.store.context_history(self.alpha["id"])
        beta = self.store.context_history(self.beta["id"])
        self.assertNotEqual(alpha["context_manifest_hash"], beta["context_manifest_hash"])
        self.assertEqual(alpha["corpus_identity"], ALPHA_CORPUS)
        self.assertEqual(beta["corpus_identity"], BETA_CORPUS)

    def test_cache_identity_cannot_collide_across_projects(self):
        alpha = self.store.telemetry(self.alpha["id"])["context"]["cache_identity"]
        beta = self.store.telemetry(self.beta["id"])["context"]["cache_identity"]
        self.assertNotEqual(alpha, beta)

    def test_a_manifest_built_for_one_project_is_refused_by_the_other(self):
        """The identity check is what stops it, not the store's row scoping."""

        alpha_payload = project_payload(ALPHA_CORPUS, ALPHA_REMOTE, ALPHA, "GATE-ALPHA")
        beta_payload = project_payload(BETA_CORPUS, BETA_REMOTE, BETA, "GATE-BETA")
        beta_request = context.ContextRequest.from_payload(beta_payload)
        crossed = context.package_from_row(
            self.store.step_output(self.alpha["id"], "context"))
        self.assertEqual(context.verify(beta_request, crossed),
                         "CONTEXT_MISSION_MISMATCH")
        alpha_request = context.ContextRequest.from_payload(alpha_payload)
        self.assertIsNone(context.verify(alpha_request, crossed))

    def test_context_telemetry_never_mixes_the_two(self):
        alpha = self.store.telemetry(self.alpha["id"])["context"]
        beta = self.store.telemetry(self.beta["id"])["context"]
        self.assertEqual(alpha["selected_context_bytes"], 400)
        self.assertEqual(beta["selected_context_bytes"], 3000)
        self.assertEqual(alpha["reduction"]["reduction_ratio"], 0.9)
        self.assertEqual(beta["reduction"]["saved_bytes"], 6000)

    def test_a_context_budget_binds_only_its_own_project(self):
        """Beta's ceiling refuses beta; alpha, well under it, still runs."""

        tight, _ = self.controller.submit(
            project_payload(BETA_CORPUS, BETA_REMOTE, BETA, "GATE-BETA",
                            context_budget={"max_bytes": 1000}), "ctx-beta:2")
        refused = self.controller.work_once("w1")
        self.assertEqual(refused["id"], tight["id"])
        self.assertIn("CONTEXT_BUDGET_EXCEEDED", refused["terminal_reason"])

        roomy, _ = self.controller.submit(
            project_payload(ALPHA_CORPUS, ALPHA_REMOTE, ALPHA, "GATE-ALPHA",
                            context_budget={"max_bytes": 1000}), "ctx-alpha:2")
        ran = self.controller.work_once("w1")
        self.assertEqual(ran["id"], roomy["id"])
        self.assertEqual(ran["state"], "completed")

    def test_route_history_and_evidence_stay_with_their_own_project(self):
        alpha_route = self.store.route_history(self.alpha["id"])
        beta_route = self.store.route_history(self.beta["id"])
        self.assertEqual(alpha_route["selected_provider_profile"], ALPHA)
        self.assertEqual(beta_route["selected_provider_profile"], BETA)
        self.assertEqual({leg["idempotency_key"] for leg in alpha_route["legs"]},
                         {"ctx-alpha:1"})
        self.assertNotEqual(
            self.store.get(self.alpha["id"])["result"]["evidence"]["evidence_pointer"],
            self.store.get(self.beta["id"])["result"]["evidence"]["evidence_pointer"])


class EconomicsTests(RouteTestCase, unittest.TestCase):
    """Baseline versus selected, per project, judged by the existing evaluator."""

    def setUp(self):
        self.controller, self.store, _ = self.build(ProjectBroker())
        for index in range(2):
            self.controller.submit(
                project_payload(ALPHA_CORPUS, ALPHA_REMOTE, ALPHA, "GATE-ALPHA"),
                "econ-alpha:%d" % index)
            self.controller.submit(
                project_payload(BETA_CORPUS, BETA_REMOTE, BETA, "GATE-BETA"),
                "econ-beta:%d" % index)
        while self.controller.work_once("w1") is not None:
            pass

    def test_economics_group_per_project(self):
        report = self.store.economics()
        self.assertEqual(report["project_count"], 2)
        by_corpus = {group["corpus_identity"]: group for group in report["projects"]}
        self.assertEqual(set(by_corpus), {ALPHA_CORPUS, BETA_CORPUS})
        self.assertEqual(by_corpus[ALPHA_CORPUS]["missions"], 2)
        self.assertEqual(by_corpus[ALPHA_CORPUS]["baseline_context_bytes"], 8000)
        self.assertEqual(by_corpus[ALPHA_CORPUS]["selected_context_bytes"], 800)
        self.assertEqual(by_corpus[ALPHA_CORPUS]["reduction"]["reduction_ratio"], 0.9)
        self.assertEqual(by_corpus[BETA_CORPUS]["reduction"]["reduction_ratio"],
                         round(12000 / 18000, 6))

    def test_reduction_is_reported_beside_the_evaluator_verdict(self):
        """Cheaper context is only a result if the gates still pass."""

        for group in self.store.economics()["projects"]:
            self.assertEqual(group["gate_passed"], group["missions"])
            self.assertEqual(group["bound"], group["missions"])
            self.assertEqual(group["refused"], 0)

    def test_one_project_can_be_queried_alone(self):
        report = self.store.economics(ALPHA_CORPUS)
        self.assertEqual(report["project_count"], 1)
        self.assertEqual(report["projects"][0]["corpus_identity"], ALPHA_CORPUS)

    def test_cache_state_is_counted_not_guessed(self):
        group = self.store.economics(ALPHA_CORPUS)["projects"][0]
        self.assertEqual(group["cache_misses"], 2)
        self.assertEqual(group["cache_hits"], 0)

    def test_an_unmeasured_project_reports_not_measurable_rather_than_zero(self):
        controller, store, _ = self.build(BrokerAdapter())
        controller.submit(project_payload(ALPHA_CORPUS, ALPHA_REMOTE, ALPHA, "GATE-ALPHA"),
                          "econ-unmeasured:1")
        controller.work_once("w1")
        group = store.economics()["projects"][0]
        self.assertEqual(group["baseline_context_bytes"], "not_measurable")
        self.assertEqual(group["selected_context_bytes"], "not_measurable")
        self.assertEqual(group["reduction"]["state"], "not_measurable")
        self.assertEqual(group["measured_missions"], 0)

    def test_a_mission_with_no_context_request_is_absent_from_economics(self):
        controller, store, _ = self.build(BrokerAdapter())
        controller.submit(mission_payload(), "econ-none:1")
        controller.work_once("w1")
        self.assertEqual(store.economics()["project_count"], 0)


if __name__ == "__main__":
    unittest.main()
