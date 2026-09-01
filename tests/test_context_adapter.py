"""The reconciliation seam, checked against a real broker answer.

The sample below is a trimmed but unedited `factory-context-broker` build
receipt for `factory-prototype-lab` at `8155d65`, captured from the running
broker.  Testing the translation against invented JSON would only prove the
translation agrees with itself.
"""

from __future__ import annotations

import unittest

from factory_controller import context, context_adapter


HEAD = "8155d6528751e25d7e4107a9d3ec98fb23154e17"
IDENTITY = "https://github.com/linc-abela/factory-prototype-lab"
REMOTE = IDENTITY + ".git"

#: One real receipt, as the broker emits it.
ANSWER = {
    "build_latency_ms": 28.735,
    "cache_hit": False,
    "ok": True,
    "manifest_ref": {"algorithm": "sha256",
                     "digest": "7839a263c6db8d5384c7ee56f8dba6f1b060d3b08e02ed65f2c114a08f11d253",
                     "path": "/workspace/.broker-cache/manifests/sha256/78/x.json"},
    "manifest": {
        "baseline": HEAD,
        "cache_identity": "91533afdc865c51e610b113fa3cb1232ca7e80b46886f1085f6af6976ac049eb",
        "creation_time": "2026-08-25T23:42:49+08:00",
        "denied": [],
        "economics": {"full_eligible_bytes": 8044, "full_eligible_files": 12,
                      "selected_bytes": 965, "selected_files": 1,
                      "token_count": "unavailable"},
        "head": HEAD,
        "manifest_digest": "7839a263c6db8d5384c7ee56f8dba6f1b060d3b08e02ed65f2c114a08f11d253",
        "omitted": [],
        "policy_digest": "3f353f7c0d92a05ac7a3b1feb088df0dad3a6414bef2be455c7a9972cba49f77",
        "repo_identity": IDENTITY,
        "schema_version": "factory.context-manifest.v1",
        "selected": [{"digest": "cc7bb5c5", "path": "MISSION.md",
                      "reason": "always_include", "size": 965}],
        "source_identity": {"commit": HEAD, "repository": IDENTITY},
    },
}


def wire(**extra):
    payload = {
        "work_item_id": "SF-136-LIVE",
        "capability": "implement",
        "repository_remote_url": REMOTE,
        "baseline_sha": HEAD,
        "acceptance_gate_ids": ["SF-136-CONTEXT"],
        "execution_mode": "fixture",
        "context_request": {"corpus_identity": "repo://factory-prototype-lab@" + HEAD,
                            "policy_identity": "SF-136:STAGE-4-CONTEXT",
                            "required_anchors": ["MISSION.md"], **extra},
        "context_budget": {"max_bytes": 200000, "max_files": 40},
    }
    return context.ContextRequest.from_payload(payload).as_wire()


class RequestTranslationTests(unittest.TestCase):
    #: `normalize_request` in `factory_context_broker/broker.py` refuses
    #: UNKNOWN_REQUEST_FIELD for anything outside this set.
    BROKER_FIELDS = {"repo_identity", "baseline", "head", "required_anchors",
                     "requested_paths", "allowed_paths", "denied_paths",
                     "max_bytes", "max_files", "max_file_bytes", "always_include",
                     "admit_binary_paths", "admit_oversized_paths", "overview"}

    def test_only_fields_the_broker_accepts_are_ever_sent(self):
        for extra in ({}, {"allowed_paths": ["src"], "denied_paths": ["deploy"]},
                      {"max_age_seconds": 60, "purpose": "feature"}):
            sent = set(context_adapter.broker_request(wire(**extra)))
            self.assertTrue(sent <= self.BROKER_FIELDS,
                            "would be refused: %s" % (sent - self.BROKER_FIELDS))

    def test_controller_only_concepts_stay_on_this_side_of_the_seam(self):
        sent = context_adapter.broker_request(wire(max_age_seconds=60, purpose="x"))
        for name in ("max_age_seconds", "purpose", "mission_input_hash",
                     "corpus_identity", "policy_identity", "schema_version"):
            self.assertNotIn(name, sent)

    def test_the_declared_ceiling_travels_with_the_entitlement(self):
        sent = context_adapter.broker_request(wire())
        self.assertEqual(sent["max_bytes"], 200000)
        self.assertEqual(sent["max_files"], 40)

    def test_the_remote_is_normalized_the_way_the_broker_normalizes_it(self):
        self.assertEqual(context_adapter.repo_identity(REMOTE), IDENTITY)
        self.assertEqual(context_adapter.repo_identity(IDENTITY + "/"), IDENTITY)
        self.assertEqual(context_adapter.repo_identity(None), "")

    def test_context_is_read_at_the_missions_own_baseline(self):
        sent = context_adapter.broker_request(wire())
        self.assertEqual(sent["baseline"], HEAD)
        self.assertEqual(sent["head"], HEAD)

    def test_the_bounded_overview_entitlement_reaches_the_broker(self):
        sent = context_adapter.broker_request(
            wire(overview=["tests", "authoritative"]))
        self.assertEqual(sent["overview"], ["authoritative", "tests"])


class AnswerTranslationTests(unittest.TestCase):
    def setUp(self):
        self.wire = wire()
        self.package = context.ContextPackage.from_response(
            context_adapter.translate(self.wire, ANSWER, now=1000.0))

    def test_a_real_answer_translates_into_an_admissible_package(self):
        request = context.ContextRequest.from_payload({
            "work_item_id": "SF-136-LIVE", "capability": "implement",
            "repository_remote_url": REMOTE, "baseline_sha": HEAD,
            "acceptance_gate_ids": ["SF-136-CONTEXT"], "execution_mode": "fixture",
            "context_request": {"corpus_identity": "repo://factory-prototype-lab@" + HEAD,
                                "policy_identity": "SF-136:STAGE-4-CONTEXT",
                                "required_anchors": ["MISSION.md"]},
            "context_budget": {"max_bytes": 200000, "max_files": 40}})
        self.assertIsNone(context.verify(
            request, self.package, budget=context.ContextBudget(max_bytes=200000)))

    def test_the_derived_manifest_is_the_one_evidence_core_validates(self):
        self.assertTrue(self.package.manifest.intact)
        self.assertEqual(self.package.manifest.schema_version, "1.0")
        self.assertEqual(self.package.manifest.selected_refs, ("MISSION.md",))

    def test_the_brokers_own_identity_is_carried_beside_it_not_instead_of_it(self):
        row = self.package.as_row()["measurement"]
        self.assertEqual(row["broker_manifest_digest"], ANSWER["manifest_ref"]["digest"])
        self.assertNotEqual(row["broker_manifest_digest"],
                            self.package.manifest.manifest_hash)
        self.assertEqual(row["policy_digest"], ANSWER["manifest"]["policy_digest"])

    def test_measured_economics_are_carried_exactly(self):
        row = self.package.as_row()["measurement"]
        self.assertEqual(row["baseline_context_bytes"], 8044)
        self.assertEqual(row["selected_context_bytes"], 965)
        self.assertEqual(row["baseline_context_files"], 12)
        self.assertEqual(row["selected_context_files"], 1)
        self.assertEqual(self.package.measurement.reduction["reduction_ratio"], 0.880035)

    def test_the_brokers_absence_word_is_translated_at_the_seam(self):
        self.assertEqual(ANSWER["manifest"]["economics"]["token_count"], "unavailable")
        self.assertEqual(self.package.measurement.context_token_count, "not_measurable")
        self.assertIn(self.package.measurement.context_token_count,
                      context.CANONICAL_ABSENCE)

    def test_cache_state_is_read_from_the_broker_not_assumed(self):
        self.assertEqual(self.package.measurement.cache_state, "miss")
        warm = context.ContextPackage.from_response(context_adapter.translate(
            self.wire, {**ANSWER, "cache_hit": True}, now=1000.0))
        self.assertEqual(warm.measurement.cache_state, "hit")

    def test_the_bounded_overview_is_carried_from_the_broker(self):
        answer = {**ANSWER, "manifest": {
            **ANSWER["manifest"],
            "overview": {"sections": {"tests": {"refs": []}},
                          "top_level": {}}}}
        package = context.ContextPackage.from_response(
            context_adapter.translate(self.wire, answer, now=1000.0))
        self.assertEqual(package.measurement.repository_overview,
                         answer["manifest"]["overview"])

    def test_selection_order_is_the_brokers(self):
        scrambled = {**ANSWER, "manifest": {**ANSWER["manifest"], "selected": [
            {"path": "z.py"}, {"path": "a.py"}, {"path": "MISSION.md"}]}}
        package = context.ContextPackage.from_response(
            context_adapter.translate(self.wire, scrambled, now=1000.0))
        self.assertEqual(package.manifest.selected_refs, ("z.py", "a.py", "MISSION.md"))

    def test_a_repository_the_broker_disagrees_about_is_handed_back_to_refuse(self):
        crossed = {**ANSWER, "manifest": {**ANSWER["manifest"],
                                          "repo_identity": "https://example.com/other"}}
        package = context.ContextPackage.from_response(
            context_adapter.translate(self.wire, crossed, now=1000.0))
        self.assertEqual(package.measurement.repository_remote_url,
                         "https://example.com/other")
        request = context.ContextRequest.from_payload({
            "work_item_id": "SF-136-LIVE", "repository_remote_url": REMOTE,
            "baseline_sha": HEAD, "capability": "implement",
            "acceptance_gate_ids": ["SF-136-CONTEXT"], "execution_mode": "fixture",
            "context_request": {"corpus_identity": "repo://factory-prototype-lab@" + HEAD,
                                "policy_identity": "SF-136:STAGE-4-CONTEXT",
                                "required_anchors": ["MISSION.md"]}})
        self.assertEqual(context.verify(request, package),
                         "CONTEXT_REPOSITORY_MISMATCH")


class BrokerFailureTests(unittest.TestCase):
    def test_a_broker_error_becomes_a_refusal_carrying_its_own_code(self):
        refused = context_adapter.translate(wire(), {
            "ok": False, "error": {"code": "MISSING_REQUIRED_ANCHOR",
                                   "details": {"paths": ["NOT_THERE.md"]},
                                   "message": "required anchors are absent at head"}})
        package = context.ContextPackage.from_response(refused)
        self.assertEqual(package.status, "refused")
        self.assertEqual(package.refusal_code, "MISSING_REQUIRED_ANCHOR")
        self.assertIsNone(package.manifest)

    def test_an_unconfigured_broker_is_unavailable_rather_than_refused(self):
        """Unconfigured must stay retryable; a refusal would be memoized."""

        import os

        for name in (context_adapter.COMMAND_ENV, context_adapter.REPO_ENV,
                     context_adapter.CACHE_ENV):
            self.assertNotIn(name, os.environ, "test environment is contaminated")
        answer = context_adapter.build(wire())
        self.assertEqual(answer["status"], "unavailable")
        self.assertEqual(answer["refusal_code"], "CONTEXT_BROKER_UNCONFIGURED")


if __name__ == "__main__":
    unittest.main()
