"""SF-186: the reachable Phase-1 release/Production safety defects, closed.

SF-182 reproduced nine blockers on the exact frozen heads and fixed none of
them, because it was a read-only closure review.  This module is the other
half: every one of them reproduced here first as a failing behaviour, then
asserted closed, plus the composed run across the whole path they sit on --
immutable artifact identity, RC seal, REVIEW health proof, Owner Validation
binding, same-artifact Production admission, post-deploy health, and rollback
target and version fidelity.

They are one system and they are tested as one.  Every defect below was
individually plausible and collectively fatal in the same direction: each let
a release advance on a fact nobody had established.  B09 is the ninth and
lives in ``factory-bridge`` (``tests/test_candidate_artifact.py``), because the
candidate source boundary is the execution layer's.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import tempfile
import threading
from pathlib import Path
import unittest

from factory_controller import cli, google_production, production, release
from factory_controller.store import MissionStore

from tests.test_release_lifecycle import REPOSITORY, bundle


HEALTH_OK = b'{"app": "lodus-casino", "status": "ok"}'
HEALTH_DOWN = b'{"app": "lodus-casino", "status": "down"}'


def files_for(marker: str) -> dict[str, bytes]:
    return {"index.html": b"<!doctype html><title>casino %s</title>" % marker.encode(),
            "health.json": HEALTH_OK}


def real_bundle(number: int, marker: str) -> tuple[production.ReleaseBundle,
                                                   dict[str, bytes]]:
    """A bundle whose artifact identity is the real digest of real bytes."""

    files = files_for(marker)
    payload = bundle(number, artifact_char=marker).as_row()
    payload["artifact"] = {"kind": "static-bundle",
                           "identity": production.deployable_digest(files)}
    return production.ReleaseBundle.from_payload(payload), files


def probed(passed: int, failed: int, surface: str, *,
           entry_proof: str | None = None) -> production.ProbedHealthRecord:
    if entry_proof is None:
        entry_proof = "sha256:" + "0" * 64 if passed and not failed \
            else "not_applicable"
    return production.ProbedHealthRecord(
        checks_passed=passed, checks_failed=failed,
        evidence_ref="probe/lodus-casino", observed_at=1.0,
        probe_target=surface, entry_proof=entry_proof)


class ReleaseFixture(unittest.TestCase):
    """One project, one review environment, one production environment."""

    REVIEW = "https://review.example.invalid/casino"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = str(self.root / "controller.db")
        self.store = MissionStore(self.db)
        self.ledger = production.ProductionLedger(self.store)
        self.lifecycle = release.ReleaseLifecycle(self.store)
        self.port = production.DeterministicDeploymentAdapter()
        self.environment("lodus-casino-review", "staging", autonomous=True)
        self.environment("lodus-casino-production", "production",
                         autonomous=False)

    def environment(self, environment_id: str, klass: str, *,
                    autonomous: bool, project_id: str = "lodus-casino") -> None:
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id=environment_id, project_id=project_id,
            environment_class=klass, repository=REPOSITORY,
            service_ref=environment_id + "-web", approver_refs=("owner",),
            autonomous=autonomous, policy_version="phase-1"))

    def seal(self, number: int = 1, marker: str = "a"):
        candidate, files = real_bundle(number, marker)
        rc = self.lifecycle.seal("CASINO-MVP-RC-%03d" % number, candidate,
                                 verification_refs=["verification/%03d" % number],
                                 qa_refs=["qa/%03d" % number])
        return rc, files

    def review(self, number: int = 1, *, environment_id="lodus-casino-review",
               url: str | None = None, health=None):
        url = url or self.REVIEW
        return self.lifecycle.deploy_review(
            "CASINO-MVP-RC-%03d" % number, self.ledger, self.port,
            review_environment_id=environment_id, requested_by="factory",
            review_url=url,
            health=probed(3, 0, url) if health is None else health)

    def validate(self, number: int, deployment_ref: str, *,
                 decision="VALIDATED", validation_id=None):
        return self.lifecycle.record_owner_validation(
            validation_id or "CASINO-MVP-VALIDATION-%03d" % number,
            "CASINO-MVP-RC-%03d" % number, deployment_ref=deployment_ref,
            decision=decision, decided_by="owner", decided_at=100.0)

    def promote(self, number: int, validation_id=None, *, health=None,
                port=None):
        return self.lifecycle.promote_validated(
            "CASINO-MVP-RC-%03d" % number,
            validation_id or "CASINO-MVP-VALIDATION-%03d" % number,
            self.ledger, port or self.port,
            production_environment_id="lodus-casino-production",
            requested_by="factory",
            health=probed(3, 0, self.REVIEW) if health is None else health)


# --------------------------------------------------------------------------- #
# B01 -- a REVIEW is settled by a probe, or it is not settled healthy
# --------------------------------------------------------------------------- #


class ReviewHealthProofTests(ReleaseFixture):
    """SF-186 B01.

    ``release deploy-review --passed 3`` with no server settled the review
    ``healthy``.  Owner Validation and Production promotion both consume only
    the resulting ledger state, so the entire exact-artifact chain could run
    against a surface that served nothing.
    """

    def test_operator_counts_cannot_settle_a_review(self):
        self.seal()

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.review(health=production.HealthRecord(
                checks_passed=3, checks_failed=0,
                evidence_ref="typed-by-hand", observed_at=1.0))

        self.assertEqual(caught.exception.code, "REVIEW_HEALTH_UNPROVEN")
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM release_deployments"
                                         ).fetchone())

    def test_a_probe_of_a_different_surface_is_not_this_reviews_evidence(self):
        self.seal()

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.review(health=probed(3, 0, "https://somewhere.else.invalid/"))

        self.assertEqual(caught.exception.code, "REVIEW_HEALTH_SURFACE_MISMATCH")

    def test_a_probe_that_compared_nothing_cannot_report_healthy(self):
        """Reaching a surface is not proof that it serves the sealed bytes."""

        self.seal()

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.review(health=probed(3, 0, self.REVIEW,
                                      entry_proof="not_applicable"))

        self.assertEqual(caught.exception.code, "REVIEW_HEALTH_ENTRY_UNPROVEN")

    def test_a_probe_that_found_the_surface_broken_is_still_recorded(self):
        """Fail closed on the failure -- do not discard it for lack of proof."""

        self.seal()

        deployed = self.review(health=probed(0, 2, self.REVIEW))

        self.assertEqual(deployed["state"], "failed")

    def test_an_unproven_review_leaves_owner_validation_unreachable(self):
        self.seal()

        deployed = self.review(health=probed(0, 0, self.REVIEW))

        # An observation of nothing is `unknown`, one of the four absence
        # words, and it advances nothing: the deployment stays in `verifying`.
        self.assertEqual(deployed["state"], "unknown")
        self.assertEqual(
            self.ledger.deployment(deployed["deployment_id"])["state"],
            "verifying")
        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.validate(1, deployed["deployment_ref"])
        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")

    def test_production_health_needs_the_same_proof(self):
        """The same defect one environment class over.

        A falsely healthy Production deployment is not only a wrong status: a
        healthy row is what rollback target selection reads, so counts alone
        could nominate bytes nobody observed serving.
        """

        self.seal()
        deployed = self.review()
        self.validate(1, deployed["deployment_ref"])

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.promote(1, health=production.HealthRecord(
                checks_passed=3, checks_failed=0,
                evidence_ref="typed-by-hand", observed_at=1.0))

        self.assertEqual(caught.exception.code, "PRODUCTION_HEALTH_UNPROVEN")


# --------------------------------------------------------------------------- #
# B02 / B03 -- promotion is bound to the exact validated deployment, atomically
# --------------------------------------------------------------------------- #


class ValidatedDeploymentBindingTests(ReleaseFixture):
    """SF-186 B02 and B03."""

    A = "https://review-a.example.invalid/casino"
    B = "https://review-b.example.invalid/casino"

    def setUp(self):
        super().setUp()
        self.environment("review-a", "staging", autonomous=True)
        self.environment("review-b", "staging", autonomous=True)

    def two_reviews(self):
        self.seal()
        a = self.review(1, environment_id="review-a", url=self.A,
                        health=probed(3, 0, self.A))
        b = self.review(1, environment_id="review-b", url=self.B,
                        health=probed(3, 0, self.B))
        return a, b

    def test_promotion_reads_the_validated_deployment_not_any_healthy_one(self):
        """One RC, two staging environments, one Owner decision.

        Promotion selected an arbitrary staging row for the RC. Validating
        ``review-b`` and then watching it fail left ``review-a`` healthy, and
        that was enough: Production was admitted while the deployment the
        Owner actually looked at was recorded broken.
        """

        a, b = self.two_reviews()
        self.validate(1, b["deployment_ref"])
        self.ledger.record_health(b["deployment_id"], probed(0, 2, self.B))
        self.assertEqual(self.ledger.deployment(a["deployment_id"])["state"],
                         "healthy")

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.promote(1)

        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")
        self.assertIn(b["deployment_ref"], caught.exception.detail)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM deployments WHERE environment_id=?",
                ("lodus-casino-production",)).fetchone())

    def test_the_validated_review_being_healthy_still_promotes(self):
        """The rule is exactness, not refusal: the good path is unchanged."""

        a, b = self.two_reviews()
        self.validate(1, b["deployment_ref"])
        self.ledger.record_health(a["deployment_id"], probed(0, 2, self.A))

        promoted = self.promote(1)

        self.assertEqual(promoted["state"], "healthy")
        self.assertTrue(promoted["same_artifact"])

    def test_a_review_failing_inside_the_promotion_window_refuses(self):
        """SF-186 B03: the window between the health read and the admission.

        The pre-check and ``admit_release`` used to run in two transactions.
        A concurrent health writer committing between them meant Production
        was admitted on a fact that had already stopped being true. The
        interleave fires after the pre-check has committed, which is exactly
        the window; the refusal therefore comes from the admission's own
        transaction and not from the earlier read.
        """

        self.seal()
        deployed = self.review()
        self.validate(1, deployed["deployment_ref"])

        original = self.ledger.environment
        fired = []

        def interleave(environment_id):
            if not fired:
                fired.append(True)
                self.ledger.record_health(deployed["deployment_id"],
                                          probed(0, 2, self.REVIEW))
            return original(environment_id)

        self.ledger.environment = interleave
        self.addCleanup(setattr, self.ledger, "environment", original)

        with self.assertRaises(production.ProductionRefusal) as caught:
            self.promote(1)

        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")
        self.assertIn("at the moment of Production admission",
                      caught.exception.detail)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM deployments WHERE environment_id=?",
                ("lodus-casino-production",)).fetchone())
            refusals = db.execute(
                "SELECT detail_json FROM production_events WHERE kind='release_refused'"
            ).fetchall()
        self.assertTrue(any("REVIEW_NOT_HEALTHY" in row["detail_json"]
                            for row in refusals))


# --------------------------------------------------------------------------- #
# B04 -- the deployable identity is injective
# --------------------------------------------------------------------------- #


class DeployableIdentityTests(unittest.TestCase):
    """SF-186 B04.

    v1 concatenated each sorted name and then its bytes with no framing, so
    the input was not uniquely decodable and two different file sets reached
    one digest.  The resolver and the adapter both *compare* that digest
    immediately before serving, so a different file set could satisfy the
    same-artifact check and be served as the sealed artifact.
    """

    COLLIDING = {
        "engine.mjs": b"E", "game.mjs": b"G", "health.json": b"H",
        "index.html": b"<html>", "styles.css": b"S", "ui.mjs": b"U",
    }

    def test_the_exact_reported_collision_no_longer_collides(self):
        set_a = {**self.COLLIDING, "a": b"b"}
        set_b = {**self.COLLIDING, "ab": b""}

        self.assertNotEqual(production.deployable_digest(set_a),
                            production.deployable_digest(set_b))

    def test_the_v1_construction_produced_that_collision(self):
        """The defect is real, not a misreading of the report."""

        import hashlib

        def v1(files):
            hasher = hashlib.sha256()
            for name in sorted(files):
                hasher.update(name.encode("utf-8"))
                hasher.update(files[name])
            return "sha256:" + hasher.hexdigest()

        self.assertEqual(v1({**self.COLLIDING, "a": b"b"}),
                         v1({**self.COLLIDING, "ab": b""}))

    def test_a_boundary_shifted_by_one_byte_is_a_different_identity(self):
        """The whole family the collision came from, not one pair of it."""

        seen = {}
        for split in range(1, 8):
            name, body = "x" * split, b"y" * (8 - split)
            seen.setdefault(production.deployable_digest({name: body}),
                            []).append(name)
        self.assertEqual(sorted(len(v) for v in seen.values()), [1] * 7)

    def test_the_same_bytes_under_different_names_differ(self):
        self.assertNotEqual(production.deployable_digest({"a.html": b"x"}),
                            production.deployable_digest({"b.html": b"x"}))

    def test_a_file_count_alone_changes_the_identity(self):
        self.assertNotEqual(production.deployable_digest({"a": b""}),
                            production.deployable_digest({"a": b"", "b": b""}))

    def test_the_framing_is_named_so_a_change_to_it_is_visible(self):
        self.assertEqual(production.DEPLOYABLE_SET_SCHEMA,
                         "factory.controller.deployable_file_set.v2")

    def test_the_resolver_and_the_adapter_agree_with_the_seal(self):
        """Three sites derive this identity; one construction answers all."""

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "artifact"
            directory.mkdir()
            files = files_for("a")
            for name, body in files.items():
                (directory / name).write_bytes(body)
            digest = production.deployable_digest(files)

            resolved = google_production.file_system_artifact_resolver(
                digest, base_dirs=[str(directory)])

        self.assertEqual(resolved, files)
        self.assertEqual(production.deployable_digest(resolved), digest)


# --------------------------------------------------------------------------- #
# B05 -- the health document's own answer
# --------------------------------------------------------------------------- #


class HealthSemanticsTests(unittest.TestCase):
    """SF-186 B05.

    ``/health.json`` exists to make an assertion.  Counting only its HTTP
    status made "the app says it is down" indistinguishable from "the app says
    it is fine", and the REVIEW was settled healthy on the first.
    """

    ENTRY = b"<!doctype html><title>casino</title>"

    def verify(self, health_body, *, entry=None, status=200):
        def opener(url):
            if url.endswith("/health.json"):
                return status, health_body, {}
            return 200, self.ENTRY if entry is None else entry, {}

        verifier = google_production.StaticWebHealthVerifier(
            opener=opener, clock=lambda: 1.0)
        return verifier.verify("https://review.example.invalid/casino",
                               expected_entry_content=self.ENTRY)

    def test_a_health_document_reporting_down_is_not_healthy(self):
        record = self.verify(HEALTH_DOWN)

        self.assertNotEqual(production.classify_health(record), "healthy")
        self.assertTrue(record.checks_failed)

    def test_the_casino_health_document_is_healthy(self):
        record = self.verify(HEALTH_OK)

        self.assertEqual(production.classify_health(record), "healthy")
        self.assertEqual(record.checks_failed, 0)

    def test_an_unreadable_health_document_is_not_a_pass(self):
        for body in (b"not json at all", b"[]", b"null", b"\xff\xfe"):
            with self.subTest(body=body):
                self.assertNotEqual(
                    production.classify_health(self.verify(body)), "healthy")

    def test_a_document_making_no_health_claim_is_not_a_pass(self):
        """Absence of an assertion is `unknown`, and unknown is not a pass."""

        self.assertFalse(google_production.health_body_ok(
            b'{"app": "lodus-casino"}'))

    def test_the_accepted_words_are_listed_rather_than_inferred(self):
        for word in sorted(google_production.HEALTHY_STATUS_VALUES):
            with self.subTest(word=word):
                self.assertTrue(google_production.health_body_ok(
                    json.dumps({"status": word}).encode()))
        for word in ("down", "degraded", "failing", "", "okish", "notok"):
            with self.subTest(word=word):
                self.assertFalse(google_production.health_body_ok(
                    json.dumps({"status": word}).encode()))

    def test_a_boolean_health_flag_is_read_as_written(self):
        self.assertTrue(google_production.health_body_ok(b'{"healthy": true}'))
        self.assertFalse(google_production.health_body_ok(b'{"healthy": false}'))
        self.assertFalse(google_production.health_body_ok(b'{"ok": false}'))

    def test_a_declared_expected_body_is_the_answer_when_given(self):
        self.assertTrue(google_production.health_body_ok(
            HEALTH_OK, {"app": "lodus-casino"}))
        self.assertFalse(google_production.health_body_ok(
            HEALTH_OK, {"app": "someone-else"}))

    def test_a_non_200_health_endpoint_still_fails(self):
        self.assertNotEqual(
            production.classify_health(self.verify(HEALTH_OK, status=503)),
            "healthy")

    def test_the_wrong_entry_document_is_not_healthy_however_ok_health_is(self):
        record = self.verify(HEALTH_OK, entry=b"<html>somebody else</html>")

        self.assertNotEqual(production.classify_health(record), "healthy")

    def test_the_probe_records_what_it_observed_and_compared(self):
        record = self.verify(HEALTH_OK)

        self.assertIsInstance(record, production.ProbedHealthRecord)
        self.assertEqual(record.probe_target,
                         "https://review.example.invalid/casino")
        self.assertTrue(record.entry_proven)
        self.assertIn("entry_proof", record.as_row())


# --------------------------------------------------------------------------- #
# B06 / B07 / B08 -- rollback legality, target, and version fidelity
# --------------------------------------------------------------------------- #


class RollbackSafetyTests(ReleaseFixture):
    """SF-186 B06, B07 and B08."""

    def production_release(self, marker: str, health_passed: bool):
        candidate, files = real_bundle(1, marker)
        self.files_by_digest[candidate.artifact["identity"]] = files
        deployment = self.ledger.admit_release(candidate,
                                               "lodus-casino-production",
                                               "factory")
        self.ledger.approve(deployment, "owner", "val-" + marker,
                            candidate.bundle_digest)
        state = self.ledger.deploy(deployment, self.adapter)
        self.assertEqual(state, "verifying",
                         self.ledger.receipt(deployment).get("operation_ref"))
        self.ledger.record_health(
            deployment,
            probed(3, 0, self.REVIEW) if health_passed
            else probed(0, 2, self.REVIEW))
        return deployment

    def setUp(self):
        super().setUp()
        self.files_by_digest: dict[str, dict[str, bytes]] = {}
        self.transport = google_production.SimulatedFirebaseTransport()
        self.adapter = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=self.transport,
            artifact_resolver=lambda digest: self.files_by_digest.get(digest, {}),
            store=self.store)

    def served(self) -> bytes | None:
        return self.transport.get_served_file("lodus-casino-production",
                                              "live", "index.html")

    def test_a_reused_adapter_never_restores_a_known_failed_version(self):
        """SF-186 B06: one adapter, two rollbacks, one known-failed version.

        The ledger chose the healthy predecessor both times.  The adapter
        consulted its own in-process history first and took the last version
        it happened to deploy -- which after the first rollback was the
        version the ledger had already recorded failed.  The environment was
        then serving a release the Factory knew was broken while the ledger
        recorded a successful recovery.
        """

        good = self.production_release("a", True)
        first_bad = self.production_release("b", False)
        self.ledger.rollback(first_bad, self.adapter)
        second_bad = self.production_release("c", False)

        self.ledger.rollback(second_bad, self.adapter)

        self.assertEqual(self.ledger.deployment(second_bad)["rollback_of"], good)
        self.assertEqual(self.served(), files_for("a")["index.html"])

    def test_the_adapter_refuses_when_the_ledger_names_no_target(self):
        """A ledger that names nothing is an answer, not a gap to fill."""

        only = self.production_release("a", False)
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.rollback(only, self.adapter)

        self.assertEqual(caught.exception.code, "ROLLBACK_TARGET_UNKNOWN")
        self.assertEqual(self.served(), files_for("a")["index.html"])

    def test_a_rollback_cannot_start_from_a_release_nobody_approved(self):
        """SF-186 B07: `awaiting_approval` reached the adapter.

        The transition to ``rolling_back`` was written straight to the row, so
        it was the one state change that never consulted the state machine.
        An unapproved, undeployed release could therefore be sent to the
        adapter and mutate the live environment.
        """

        self.production_release("a", True)
        candidate, files = real_bundle(1, "b")
        self.files_by_digest[candidate.artifact["identity"]] = files
        unapproved = self.ledger.admit_release(candidate,
                                               "lodus-casino-production",
                                               "factory")
        self.assertEqual(self.ledger.deployment(unapproved)["state"],
                         "awaiting_approval")

        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.rollback(unapproved, self.adapter)

        self.assertEqual(caught.exception.code, "ROLLBACK_STATE_INVALID")
        self.assertEqual(self.ledger.deployment(unapproved)["state"],
                         "awaiting_approval")
        self.assertEqual(self.served(), files_for("a")["index.html"])

    def test_every_state_that_never_reached_the_environment_refuses(self):
        """The rule is the state machine, not a list of examples."""

        never_reached = {"admitted", "awaiting_approval", "approved",
                         "deploying", "verifying", "cancelled", "recovered",
                         "rollback_failed", "escalated"}
        for state in never_reached:
            with self.subTest(state=state):
                self.assertNotIn(
                    "rolling_back",
                    production.ALLOWED_TRANSITIONS.get(state, frozenset()))
        for state in ("healthy", "degraded", "failed", "uncertain"):
            with self.subTest(state=state):
                self.assertIn("rolling_back",
                              production.ALLOWED_TRANSITIONS[state])

    def test_the_rollback_target_predates_the_failure(self):
        """SF-186 B08: `previous` is temporal, not `most recently touched`.

        Ordering healthy rows by ``updated_at`` answered "which healthy
        deployment was touched last", and a release deployed *after* the
        failure satisfies that.  Rolling back then moved the environment
        forward onto bytes it had never run before.
        """

        first = self.production_release("a", True)
        broken = self.production_release("b", False)
        later = self.production_release("c", True)

        self.ledger.rollback(broken, self.adapter)

        self.assertEqual(self.ledger.deployment(broken)["rollback_of"], first)
        self.assertNotEqual(self.ledger.deployment(broken)["rollback_of"], later)
        self.assertEqual(self.served(), files_for("a")["index.html"])

    def test_admission_order_decides_and_not_a_mutable_timestamp(self):
        """`updated_at` moves; the append-only admission sequence does not."""

        first = self.production_release("a", True)
        broken = self.production_release("b", False)
        later = self.production_release("c", True)
        with self.store.transaction() as db:
            db.execute("UPDATE deployments SET updated_at=? WHERE id=?",
                       (10.0, later))
            db.execute("UPDATE deployments SET updated_at=? WHERE id=?",
                       (9.0, first))

        self.ledger.rollback(broken, self.adapter)

        self.assertEqual(self.ledger.deployment(broken)["rollback_of"], first)

    def test_the_restored_version_survives_a_new_process(self):
        """The ledger, not the adapter's memory, is what a restart reads."""

        good = self.production_release("a", True)
        broken = self.production_release("b", False)
        fresh = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=self.transport,
            artifact_resolver=lambda digest: self.files_by_digest.get(digest, {}),
            store=MissionStore(self.db))

        state = self.ledger.rollback(broken, fresh)

        self.assertEqual(state, "recovered")
        self.assertEqual(self.ledger.deployment(broken)["rollback_of"], good)
        self.assertEqual(self.served(), files_for("a")["index.html"])


# --------------------------------------------------------------------------- #
# the composed path
# --------------------------------------------------------------------------- #


def serve(directory: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_address[1], server


class ComposedReleaseLifecycleTests(unittest.TestCase):
    """One run across every seam SF-182 rejected, through the Owner's command.

    Immutable artifact identity, RC seal, a REVIEW health proof taken from a
    surface that really serves the sealed bytes, Owner Validation bound to
    that exact deployment, same-artifact Production admission, post-deploy
    health, and a rollback whose target and served version are both the
    healthy predecessor.  Each of the nine defects would have broken a
    different step of this; none of them breaks it now.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = str(self.root / "controller.db")
        ledger = production.ProductionLedger(MissionStore(self.db))
        for environment_id, klass, autonomous in (
                ("lodus-casino-review", "staging", True),
                ("lodus-casino-production", "production", False)):
            ledger.register_environment(production.EnvironmentPolicy(
                environment_id=environment_id, project_id="lodus-casino",
                environment_class=klass, repository=REPOSITORY,
                service_ref=environment_id + "-web", approver_refs=("owner",),
                autonomous=autonomous, policy_version="phase-1"))

    def run_cli(self, *arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["--db", self.db, *arguments])
        text = stream.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def release(self, number: int, marker: str):
        directory = self.root / ("artifact-%s" % marker)
        directory.mkdir(exist_ok=True)
        for name, body in files_for(marker).items():
            (directory / name).write_bytes(body)
        candidate, _ = real_bundle(number, marker)
        path = self.root / ("bundle-%03d.json" % number)
        path.write_text(json.dumps(candidate.as_row()))
        url, server = serve(directory)
        self.addCleanup(server.shutdown)
        return directory, path, url

    def through(self, number: int, marker: str):
        directory, path, url = self.release(number, marker)
        rc_id = "CASINO-MVP-RC-%03d" % number
        validation_id = "CASINO-MVP-VALIDATION-%03d" % number

        code, sealed = self.run_cli(
            "release", "seal", "--rc", rc_id, "--bundle", str(path),
            "--verification-ref", "v/%d" % number, "--qa-ref", "q/%d" % number)
        self.assertEqual(code, 0, sealed)

        code, deployed = self.run_cli(
            "release", "deploy-review", "--rc", rc_id,
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", url, "--probe", "--artifact-dir", str(directory))
        self.assertEqual(code, 0, deployed)
        self.assertEqual(deployed["state"], "healthy")

        code, validated = self.run_cli(
            "release", "validate", "--rc", rc_id, "--validation", validation_id,
            "--deployment-ref", deployed["deployment_ref"],
            "--decision", "VALIDATED", "--actor", "owner")
        self.assertEqual(code, 0, validated)

        code, promoted = self.run_cli(
            "release", "promote", "--rc", rc_id, "--validation", validation_id,
            "--environment", "lodus-casino-production", "--actor", "factory",
            "--review-url", url, "--probe", "--artifact-dir", str(directory))
        self.assertEqual(code, 0, promoted)
        return sealed, deployed, validated, promoted

    def test_the_whole_path_runs_and_carries_one_identity(self):
        sealed, deployed, validated, promoted = self.through(1, "a")

        for row in (deployed, validated, promoted):
            self.assertEqual(row["artifact_digest"], sealed["artifact_digest"])
        self.assertEqual(promoted["candidate_sha"], sealed["candidate_sha"])
        self.assertTrue(promoted["same_artifact"])
        self.assertEqual(promoted["state"], "healthy")

    def test_the_review_that_settled_it_was_really_served(self):
        """The proof is a comparison against the sealed bytes, recorded."""

        _, deployed, _, _ = self.through(1, "a")

        with MissionStore(self.db).transaction() as db:
            rows = db.execute(
                "SELECT detail_json FROM production_events"
                " WHERE deployment_id=? AND kind='deployment_state'"
                " ORDER BY sequence", (deployed["deployment_id"],)).fetchall()
        health = next(json.loads(row["detail_json"])["health"] for row in rows
                      if "health" in json.loads(row["detail_json"]))

        self.assertEqual(health["probe_target"], deployed["validation_surface"])
        self.assertTrue(health["entry_proof"].startswith("sha256:"))
        self.assertEqual(health["checks_failed"], 0)
        self.assertEqual(deployed["receipt"]["health_outcome"], "healthy")

    def test_a_surface_serving_the_wrong_bytes_stops_the_chain(self):
        directory, path, url = self.release(2, "b")
        self.run_cli("release", "seal", "--rc", "CASINO-MVP-RC-002",
                     "--bundle", str(path), "--verification-ref", "v",
                     "--qa-ref", "q")
        (directory / "index.html").write_bytes(b"<html>something else</html>")

        code, deployed = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-002",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", url, "--probe", "--artifact-dir", str(directory))

        # The resolver cannot even produce the sealed bytes from a mutated
        # directory, so the probe refuses before it contacts anything.
        self.assertEqual(code, 1)
        self.assertEqual(deployed["refused"]["code"],
                         "REVIEW_ARTIFACT_UNAVAILABLE")

    def test_a_surface_reporting_itself_down_stops_the_chain(self):
        directory, path, url = self.release(3, "c")
        self.run_cli("release", "seal", "--rc", "CASINO-MVP-RC-003",
                     "--bundle", str(path), "--verification-ref", "v",
                     "--qa-ref", "q")
        served = self.root / "artifact-c-down"
        served.mkdir()
        for name, body in files_for("c").items():
            (served / name).write_bytes(body)
        (served / "health.json").write_bytes(HEALTH_DOWN)
        down_url, server = serve(served)
        self.addCleanup(server.shutdown)

        code, deployed = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-003",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", down_url, "--probe",
            "--artifact-dir", str(directory))

        self.assertEqual(code, 0, deployed)
        self.assertNotEqual(deployed["state"], "healthy")
        code, refused = self.run_cli(
            "release", "validate", "--rc", "CASINO-MVP-RC-003",
            "--validation", "CASINO-MVP-VALIDATION-003",
            "--deployment-ref", deployed["deployment_ref"],
            "--decision", "VALIDATED", "--actor", "owner")
        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "REVIEW_NOT_HEALTHY")

    def test_the_rollback_at_the_end_restores_the_predecessor(self):
        _, _, _, first = self.through(1, "a")
        _, _, _, second = self.through(2, "b")

        code, rolled = self.run_cli(
            "release", "rollback", "--rc", "CASINO-MVP-RC-002",
            "--environment", "lodus-casino-production")

        self.assertEqual(code, 0, rolled)
        self.assertEqual(rolled["state"], "recovered")
        self.assertEqual(rolled["receipt"]["rollback_of"],
                         first["deployment_id"])

    def test_the_ledger_reads_the_same_after_reopening_it(self):
        """Release and Production state are durable, not process-local."""

        _, deployed, _, promoted = self.through(1, "a")

        store = MissionStore(self.db)
        lifecycle = release.ReleaseLifecycle(store)
        ledger = production.ProductionLedger(store)
        candidate = lifecycle.candidate("CASINO-MVP-RC-001")
        validation = lifecycle.owner_validation("CASINO-MVP-VALIDATION-001")

        self.assertEqual(candidate.artifact_digest, promoted["artifact_digest"])
        self.assertEqual(validation.deployment_ref, deployed["deployment_ref"])
        self.assertEqual(ledger.deployment(promoted["deployment_id"])["state"],
                         "healthy")
        self.assertEqual(
            [row["kind"] for row in lifecycle.events("CASINO-MVP-RC-001")],
            ["rc_sealed", "review_deployed", "owner_validation_recorded",
             "production_promoted"])

class ProducerSiblingTests(ReleaseFixture):
    """SF-186 B01, reached through the branches beside the one it names.

    ``deploy_review`` is not the only writer that can settle a review
    deployment: the generic ``production health`` command reaches the same row
    with operator-supplied counts, and refusing the record at one producer
    leaves every other one open.  So the evidence is read where it is
    *consumed* -- Owner Validation and Production admission -- which answers
    for producers that do not exist yet.
    """

    def run_cli(self, *arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["--db", self.db, *arguments])
        text = stream.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def pending_review(self):
        self.seal()
        deployed = self.review(health=probed(0, 0, self.REVIEW))
        self.assertEqual(
            self.ledger.deployment(deployed["deployment_id"])["state"],
            "verifying")
        return deployed

    def test_the_generic_health_command_cannot_settle_a_review(self):
        deployed = self.pending_review()

        code, health = self.run_cli(
            "production", "health", "--deployment", deployed["deployment_id"],
            "--passed", "3", "--ref", "typed-by-hand")

        # The ledger records what it was told -- it is a ledger.
        self.assertEqual(code, 0)
        self.assertEqual(health["state"], "healthy")
        # The release path does not accept it as this review's evidence.
        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.validate(1, deployed["deployment_ref"])
        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")
        self.assertIn("unproven_health", caught.exception.detail)

    def test_an_unproven_healthy_review_cannot_be_promoted_either(self):
        """The validation may predate the tampering; admission re-reads it."""

        self.seal()
        deployed = self.review()
        self.validate(1, deployed["deployment_ref"])
        # Settle it again from the generic surface, overwriting the probe's
        # observation with counts.
        self.ledger.record_health(deployed["deployment_id"],
                                  production.HealthRecord(
                                      checks_passed=3, checks_failed=0,
                                      evidence_ref="typed-by-hand",
                                      observed_at=2.0))

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.promote(1)

        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")

    def test_a_probe_of_another_surface_is_not_this_reviews_evidence(self):
        deployed = self.pending_review()
        self.ledger.record_health(
            deployed["deployment_id"],
            probed(3, 0, "https://somewhere.else.invalid/"))

        with self.assertRaises(release.ReleaseRefusal) as caught:
            self.validate(1, deployed["deployment_ref"])

        self.assertEqual(caught.exception.code, "REVIEW_NOT_HEALTHY")

    def test_a_real_probe_recorded_through_the_ledger_is_accepted(self):
        """The rule is evidence, not the command that carried it."""

        deployed = self.pending_review()
        self.ledger.record_health(deployed["deployment_id"],
                                  probed(3, 0, self.REVIEW))

        validation = self.validate(1, deployed["deployment_ref"])

        self.assertEqual(validation.decision, "VALIDATED")
        self.assertEqual(self.promote(1)["state"], "healthy")

if __name__ == "__main__":
    unittest.main()
