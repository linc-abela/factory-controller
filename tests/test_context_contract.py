"""The Context Request/Manifest contract, and the refusal matrix over it.

Two things are being proved here.  First, that the manifest shape the Controller
verifies is the one `factory-evidence-core` already validates -- the digest rule
is re-derived from that repository's own algorithm, not paraphrased.  Second,
that every failure mode SF-136 names has a distinct refusal code and that each
one actually fires, because a fail-closed matrix nobody can make fire is a
description of safety rather than safety.
"""

from __future__ import annotations

import unittest

from factory_controller import context
from factory_controller.context import (
    ContextBudget, ContextError, ContextManifest, ContextPackage, ContextRequest,
)


CORPUS = "vault://software-factory@800f2563ca52f01f2be807fa733d3c8c70dd8a47"
POLICY = "SF-136:STAGE-4-CONTEXT"
REMOTE = "git@example.com:project-alpha.git"
HEAD = "a" * 40


def payload(**extra):
    value = {
        "work_item_id": "SF-136",
        "capability": "implement",
        "repository_remote_url": REMOTE,
        "baseline_sha": HEAD,
        "acceptance_gate_ids": ["G"],
        "execution_mode": "fixture",
        "context_request": {
            "corpus_identity": CORPUS,
            "policy_identity": POLICY,
            "required_anchors": ["MISSION.md"],
        },
    }
    value.update(extra)
    return value


def build_manifest(request: ContextRequest, *, selected=("MISSION.md",),
                   corpus=None, policy=None, mission_input_hash=None,
                   questions=()) -> ContextManifest:
    """Mint a manifest the way a broker must: hash the six, then attach it."""

    unhashed = {
        "schema_version": context.CONTEXT_SCHEMA_VERSION,
        "mission_input_hash": mission_input_hash or request.mission_input_hash,
        "corpus_identity": corpus or request.corpus_identity,
        "policy_identity": policy or request.policy_identity,
        "selected_refs": list(selected),
        "unresolved_questions": list(questions),
    }
    return ContextManifest(manifest_hash=context.sha256_hex(unhashed),
                           selected_refs=tuple(selected),
                           unresolved_questions=tuple(questions),
                           **{key: value for key, value in unhashed.items()
                              if key not in ("selected_refs", "unresolved_questions")})


def package(manifest: ContextManifest, **measurement) -> ContextPackage:
    row = {
        "status": "built",
        "manifest": {**manifest.unhashed(), "manifest_hash": manifest.manifest_hash},
        "receipt": {"context_manifest_hash": manifest.manifest_hash,
                    "selected_refs": list(manifest.selected_refs), "excluded_refs": []},
        "measurement": {"repository_remote_url": REMOTE, "head_sha": HEAD,
                        "cache_state": "miss", **measurement},
    }
    return ContextPackage.from_response(row)


class RequestTests(unittest.TestCase):
    def test_a_mission_without_a_context_request_declares_none(self):
        self.assertIsNone(ContextRequest.from_payload({"work_item_id": "X"}))
        self.assertIsNone(ContextRequest.from_payload(None))

    def test_identity_and_policy_are_required(self):
        for missing in ("corpus_identity", "policy_identity"):
            raw = dict(payload()["context_request"])
            raw.pop(missing)
            with self.assertRaises(ContextError):
                ContextRequest.from_payload({"context_request": raw})

    def test_the_request_inherits_repository_identity_from_the_mission(self):
        request = ContextRequest.from_payload(payload())
        self.assertEqual(request.repository_remote_url, REMOTE)
        self.assertEqual(request.baseline_sha, HEAD)

    def test_mission_input_hash_is_derived_from_mission_identity_alone(self):
        """Retry, budget and provider policy must not orphan a valid manifest."""

        base = context.mission_input_hash(payload())
        self.assertEqual(base, context.mission_input_hash(payload(
            execution_policy={"budget_ceiling": 5, "budget_currency": "USD"},
            provider_candidates=["p"])))
        self.assertNotEqual(base, context.mission_input_hash(payload(work_item_id="SF-137")))
        self.assertNotEqual(base, context.mission_input_hash(payload(baseline_sha="b" * 40)))

    def test_gate_order_does_not_change_mission_identity(self):
        self.assertEqual(context.mission_input_hash(payload(acceptance_gate_ids=["A", "B"])),
                         context.mission_input_hash(payload(acceptance_gate_ids=["B", "A"])))

    def test_the_wire_request_carries_no_mission_state(self):
        wire = ContextRequest.from_payload(payload()).as_wire()
        for forbidden in ("idempotency_key", "attempt", "lease_token", "state",
                          "provider_candidates", "execution_policy"):
            self.assertNotIn(forbidden, wire)


class ManifestIntegrityTests(unittest.TestCase):
    def test_the_digest_rule_is_evidence_cores_own(self):
        """`src/evidence/validation.py` re-derives exactly this, so a manifest
        this module accepts is one Evidence Core will accept."""

        frozen = ContextManifest(
            schema_version="1.0",
            mission_input_hash="3682faeec54a3302f2e2cf3fa6c7d9b4904a6b944bd5e704e9ec956f7c87d587",
            manifest_hash="000f1c5aac3bc8e109b635b4068ae9fddee66ed95458fef7a658f1ea38ec3390",
            corpus_identity="vault://software-factory@800f2563ca52f01f2be807fa733d3c8c70dd8a47",
            policy_identity="FLM-1:STAGE-1-FIRST-LIVE",
            selected_refs=("MISSION.md",),
            unresolved_questions=(),
        )
        self.assertEqual(frozen.derived_hash, frozen.manifest_hash)
        self.assertTrue(frozen.intact)

    def test_the_controller_canonical_form_is_not_the_stores(self):
        """A digest computed under the store's rule would never match."""

        from factory_controller.store import canonical_json
        value = {"ref": "café"}
        self.assertNotEqual(canonical_json(value).encode(), context.canonical_bytes(value))

    def test_a_duplicated_ref_is_not_an_intact_manifest(self):
        request = ContextRequest.from_payload(payload())
        doubled = build_manifest(request, selected=("MISSION.md", "MISSION.md"))
        self.assertFalse(doubled.intact)


class VerificationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.request = ContextRequest.from_payload(payload())
        self.manifest = build_manifest(self.request)
        self.good = package(self.manifest)

    def refusal(self, pkg, **kwargs):
        return context.verify(self.request, pkg, **kwargs)

    def test_a_correct_package_is_admitted(self):
        self.assertIsNone(self.refusal(self.good))

    def test_a_tampered_manifest_is_refused_before_anything_else_is_read(self):
        row = self.good.as_row()
        row["manifest"]["selected_refs"] = ["MISSION.md", "secrets.env"]
        self.assertEqual(self.refusal(ContextPackage.from_response(row)),
                         "INVALID_CONTEXT_MANIFEST")

    def test_a_manifest_for_another_mission_is_refused(self):
        other = build_manifest(self.request, mission_input_hash="f" * 64)
        self.assertEqual(self.refusal(package(other)), "CONTEXT_MISSION_MISMATCH")

    def test_a_manifest_from_the_wrong_repository_is_refused(self):
        other = build_manifest(self.request, corpus="vault://other@" + "c" * 40)
        self.assertEqual(self.refusal(package(other)), "CONTEXT_REPOSITORY_MISMATCH")

    def test_a_manifest_under_another_policy_is_refused(self):
        other = build_manifest(self.request, policy="SOMETHING-ELSE")
        self.assertEqual(self.refusal(package(other)), "CONTEXT_POLICY_MISMATCH")

    def test_a_measured_remote_that_is_not_the_missions_is_refused(self):
        pkg = package(self.manifest, repository_remote_url="git@example.com:other.git")
        self.assertEqual(self.refusal(pkg), "CONTEXT_REPOSITORY_MISMATCH")

    def test_a_manifest_built_against_another_head_is_refused(self):
        self.assertEqual(self.refusal(package(self.manifest, head_sha="b" * 40)),
                         "CONTEXT_HEAD_MISMATCH")

    def test_a_manifest_that_is_not_the_declared_one_is_refused(self):
        """This is the whole replay guarantee, in one equality."""

        self.assertEqual(self.refusal(self.good, declared_manifest_hash="d" * 64),
                         "CONTEXT_HASH_MISMATCH")
        self.assertIsNone(self.refusal(
            self.good, declared_manifest_hash=self.manifest.manifest_hash))

    def test_a_receipt_pointing_at_a_different_manifest_is_refused(self):
        row = self.good.as_row()
        row["receipt"]["context_manifest_hash"] = "e" * 64
        self.assertEqual(self.refusal(ContextPackage.from_response(row)),
                         "CONTEXT_RECEIPT_MISMATCH")

    def test_a_missing_required_anchor_is_refused(self):
        empty = build_manifest(self.request, selected=())
        self.assertEqual(self.refusal(package(empty)), "CONTEXT_ANCHOR_MISSING")

    def test_a_denied_path_in_the_selection_is_refused(self):
        request = ContextRequest.from_payload(payload(context_request={
            "corpus_identity": CORPUS, "policy_identity": POLICY,
            "required_anchors": ["MISSION.md"], "denied_paths": ["deploy"]}))
        manifest = build_manifest(request, selected=("MISSION.md", "deploy/keys.json"))
        self.assertEqual(context.verify(request, package(manifest)),
                         "CONTEXT_DENIED_PATH_SELECTED")

    def test_a_path_outside_the_allowlist_is_refused(self):
        request = ContextRequest.from_payload(payload(context_request={
            "corpus_identity": CORPUS, "policy_identity": POLICY,
            "required_anchors": ["src/a.py"], "allowed_paths": ["src"]}))
        manifest = build_manifest(request, selected=("src/a.py", "elsewhere/b.py"))
        self.assertEqual(context.verify(request, package(manifest)),
                         "CONTEXT_PATH_NOT_ADMITTED")

    def test_a_denied_prefix_does_not_catch_a_sibling_directory(self):
        request = ContextRequest.from_payload(payload(context_request={
            "corpus_identity": CORPUS, "policy_identity": POLICY,
            "required_anchors": ["deployment/notes.md"], "denied_paths": ["deploy"]}))
        manifest = build_manifest(request, selected=("deployment/notes.md",))
        self.assertIsNone(context.verify(request, package(manifest)))

    def test_an_unavailable_broker_is_its_own_refusal(self):
        pkg = ContextPackage.from_response({"status": "unavailable"})
        self.assertEqual(self.refusal(pkg), "CONTEXT_BROKER_UNAVAILABLE")

    def test_a_broker_refusal_is_carried_through_verbatim(self):
        pkg = ContextPackage.from_response(
            {"status": "refused", "refusal_code": "SYMLINK_ESCAPE"})
        self.assertEqual(self.refusal(pkg), "SYMLINK_ESCAPE")

    def test_an_unreadable_broker_answer_is_never_read_as_success(self):
        for raw in (None, "built", [], {"status": "built"}, {"status": "built", "manifest": {}}):
            self.assertNotEqual(ContextPackage.from_response(raw).status, "built")

    def test_an_unsupported_schema_version_is_refused(self):
        row = self.good.as_row()
        row["manifest"]["schema_version"] = "2.0"
        pkg = ContextPackage.from_response(row)
        self.assertEqual(self.refusal(pkg), "CONTEXT_SCHEMA_UNSUPPORTED")


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.request = ContextRequest.from_payload(payload(context_request={
            "corpus_identity": CORPUS, "policy_identity": POLICY,
            "required_anchors": ["MISSION.md"], "max_age_seconds": 60}))
        self.manifest = build_manifest(self.request)

    def test_a_fresh_manifest_passes(self):
        pkg = package(self.manifest, built_at=1000.0)
        self.assertIsNone(context.verify(self.request, pkg, now=1030.0))

    def test_a_stale_manifest_is_refused(self):
        pkg = package(self.manifest, built_at=1000.0)
        self.assertEqual(context.verify(self.request, pkg, now=1100.0),
                         "CONTEXT_MANIFEST_STALE")

    def test_a_broker_that_will_not_say_when_it_built_cannot_be_called_fresh(self):
        pkg = package(self.manifest)
        self.assertEqual(context.verify(self.request, pkg, now=1000.0),
                         "CONTEXT_FRESHNESS_UNPROVEN")

    def test_staleness_is_not_evaluated_once_the_boundary_is_crossed(self):
        """After dispatch the execution stays bound to the manifest it ran on."""

        pkg = package(self.manifest, built_at=1000.0)
        self.assertIsNone(context.verify(self.request, pkg, now=None))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.request = ContextRequest.from_payload(payload())
        self.manifest = build_manifest(self.request)

    def test_a_ceiling_with_no_measurement_behind_it_refuses(self):
        pkg = package(self.manifest)
        self.assertEqual(
            context.verify(self.request, pkg, budget=ContextBudget(max_bytes=100)),
            "CONTEXT_BUDGET_UNMEASURED")

    def test_measured_bytes_over_the_ceiling_refuse(self):
        pkg = package(self.manifest, selected_context_bytes=101)
        self.assertEqual(
            context.verify(self.request, pkg, budget=ContextBudget(max_bytes=100)),
            "CONTEXT_BUDGET_EXCEEDED")

    def test_measured_bytes_at_the_ceiling_are_admitted(self):
        pkg = package(self.manifest, selected_context_bytes=100)
        self.assertIsNone(
            context.verify(self.request, pkg, budget=ContextBudget(max_bytes=100)))

    def test_the_file_ceiling_is_separate_from_the_byte_ceiling(self):
        pkg = package(self.manifest, selected_context_files=9, selected_context_bytes=1)
        self.assertEqual(
            context.verify(self.request, pkg,
                           budget=ContextBudget(max_bytes=10, max_files=8)),
            "CONTEXT_FILE_BUDGET_EXCEEDED")

    def test_a_token_ceiling_never_fires_on_an_unreported_count(self):
        budget = ContextBudget(max_reported_input_tokens=10)
        for absent in ("unknown", "not_applicable", "not_run", "not_measurable", None):
            self.assertIsNone(context.reported_token_refusal(budget, absent))

    def test_a_token_ceiling_fires_only_on_a_reported_count(self):
        budget = ContextBudget(max_reported_input_tokens=10)
        self.assertIsNone(context.reported_token_refusal(budget, {"total": 10}))
        self.assertEqual(context.reported_token_refusal(budget, {"total": 11}),
                         "CONTEXT_TOKEN_BUDGET_EXCEEDED")

    def test_a_non_positive_ceiling_is_an_unusable_mission(self):
        for name in ("max_bytes", "max_files", "max_reported_input_tokens"):
            with self.assertRaises(ContextError):
                ContextBudget.from_payload({"context_budget": {name: 0}})


class MeasurementTests(unittest.TestCase):
    def test_reduction_is_not_measurable_without_both_sides(self):
        self.assertEqual(context.ContextMeasurement().reduction["state"], "not_measurable")
        self.assertEqual(
            context.ContextMeasurement(baseline_context_bytes=10).reduction["state"],
            "not_measurable")

    def test_reduction_is_exact_when_both_sides_are_measured(self):
        value = context.ContextMeasurement(baseline_context_bytes=1000,
                                           selected_context_bytes=250).reduction
        self.assertEqual(value["state"], "measured")
        self.assertEqual(value["saved_bytes"], 750)
        self.assertEqual(value["reduction_ratio"], 0.75)

    def test_an_empty_baseline_is_not_a_hundred_percent_saving(self):
        self.assertEqual(
            context.ContextMeasurement(baseline_context_bytes=0,
                                       selected_context_bytes=0).reduction["state"],
            "not_applicable")

    def test_a_byte_count_never_becomes_a_token_count(self):
        """The Controller has no tokenizer, so an unstated count stays absent."""

        row = context.ContextPackage.from_response({
            "status": "refused",
            "measurement": {"selected_context_bytes": 123456},
        }).as_row()["measurement"]
        self.assertEqual(row["selected_context_bytes"], 123456)
        self.assertEqual(row["context_token_count"], "unknown")

    def test_a_foreign_absence_word_is_translated_not_propagated(self):
        """`factory-context-broker` reports `unavailable`, which is not in the
        vocabulary `src/contracts/replay.py` owns.  Translate at the seam."""

        self.assertNotIn("unavailable", context.CANONICAL_ABSENCE)
        self.assertEqual(context.canonical_absence("unavailable"), "not_measurable")
        for word in context.CANONICAL_ABSENCE:
            self.assertEqual(context.canonical_absence(word), word)
        self.assertEqual(context.canonical_absence(0), 0)
        self.assertEqual(context.canonical_absence(True), "unknown")

    def test_the_brokers_own_reference_is_carried_and_never_parsed(self):
        row = context.ContextPackage.from_response({
            "status": "refused",
            "measurement": {"broker_manifest_digest": "7839a263", "policy_digest": "3f353f"},
        }).as_row()["measurement"]
        self.assertEqual(row["broker_manifest_digest"], "7839a263")
        self.assertEqual(row["policy_digest"], "3f353f")

    def test_a_negative_or_boolean_measurement_is_absent_rather_than_wrong(self):
        pkg = ContextPackage.from_response({"status": "refused", "measurement": {
            "selected_context_bytes": -1, "selected_context_files": True,
            "cache_state": "warm"}})
        self.assertIsNone(pkg.measurement.selected_context_bytes)
        self.assertIsNone(pkg.measurement.selected_context_files)
        self.assertEqual(pkg.measurement.cache_state, "unknown")


class ExplainTests(unittest.TestCase):
    def test_a_mission_with_no_context_request_says_not_applicable(self):
        self.assertEqual(context.explain(None, None)["context_state"], "not_applicable")

    def test_a_mission_that_never_reached_the_broker_says_not_run(self):
        request = ContextRequest.from_payload(payload())
        self.assertEqual(context.explain(request, None)["context_state"], "not_run")

    def test_a_bound_mission_explains_what_was_used_and_how_big_it_was(self):
        request = ContextRequest.from_payload(payload())
        manifest = build_manifest(request)
        view = context.explain(request, package(
            manifest, baseline_context_bytes=1000, selected_context_bytes=100))
        self.assertEqual(view["context_state"], "bound")
        self.assertEqual(view["context_manifest_hash"], manifest.manifest_hash)
        self.assertEqual(view["selected_refs"], ["MISSION.md"])
        self.assertTrue(view["required_anchors_covered"])
        self.assertEqual(view["reduction"]["reduction_ratio"], 0.9)


if __name__ == "__main__":
    unittest.main()
