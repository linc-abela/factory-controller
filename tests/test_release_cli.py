"""The release lifecycle, reached the way an Owner reaches it.

``release.py`` was complete and correct and no command invoked it, which is a
different failure from not having it: a lifecycle nothing can call cannot be
the thing an Owner Validation actually runs through.  These tests exercise the
whole exact-artifact chain across the process boundary the Owner types at, and
they assert the refusals as hard as the successes -- a promotion that survives
a rebuilt artifact would be the one bug this chain exists to prevent.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest

from factory_controller import cli, production
from factory_controller.store import MissionStore

from tests.test_release_lifecycle import REPOSITORY, bundle


class ReleaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = str(self.root / "controller.db")
        ledger = production.ProductionLedger(MissionStore(self.db))
        ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-review", project_id="lodus-casino",
            environment_class="staging", repository=REPOSITORY,
            service_ref="lodus-casino-review-web", approver_refs=("owner",),
            autonomous=True, policy_version="phase-1"))
        ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-production", project_id="lodus-casino",
            environment_class="production", repository=REPOSITORY,
            service_ref="lodus-casino-production-web", approver_refs=("owner",),
            policy_version="phase-1"))

    def run_cli(self, *arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["--db", self.db, *arguments])
        text = stream.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def bundle_path(self, number: int, *, artifact_char: str | None = None) -> str:
        path = self.root / ("bundle-%03d.json" % number)
        path.write_text(json.dumps(
            bundle(number, artifact_char=artifact_char).as_row()))
        return str(path)

    def seal(self, number: int = 1, *, artifact_char: str | None = None):
        return self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--bundle", self.bundle_path(number, artifact_char=artifact_char),
            "--verification-ref", "verification/casino/%03d" % number,
            "--qa-ref", "qa/casino/%03d" % number)

    def review(self, number: int = 1):
        return self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "https://review.example.invalid/casino",
            "--passed", "3")

    def validate(self, deployment_ref: str, *, decision="VALIDATED", number=1):
        return self.run_cli(
            "release", "validate", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--validation", "CASINO-MVP-VALIDATION-%03d" % number,
            "--deployment-ref", deployment_ref, "--decision", decision,
            "--actor", "owner")

    # -- the chain ------------------------------------------------------- #

    def promote(self, number: int = 1):
        return self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--validation", "CASINO-MVP-VALIDATION-%03d" % number,
            "--environment", "lodus-casino-production", "--actor", "factory",
            "--passed", "3")

    def release_through(self, number: int):
        """Seal, review, validate and promote one release from the command line."""

        code, sealed = self.seal(number)
        self.assertEqual(code, 0)
        code, deployed = self.review(number)
        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "healthy")
        self.assertEqual(deployed["artifact_digest"], sealed["artifact_digest"])
        code, validated = self.validate(deployed["deployment_ref"], number=number)
        self.assertEqual(code, 0)
        self.assertEqual(validated["decision"], "VALIDATED")
        self.assertEqual(validated["artifact_digest"], sealed["artifact_digest"])
        code, promoted = self.promote(number)
        self.assertEqual(code, 0)
        return sealed, deployed, promoted

    def test_the_whole_exact_artifact_chain_runs_from_the_command_line(self):
        sealed, _, promoted = self.release_through(1)
        self.assertTrue(sealed["artifact_digest"].startswith("sha256:"))
        self.assertEqual(promoted["artifact_digest"], sealed["artifact_digest"])
        self.assertEqual(promoted["candidate_sha"], sealed["candidate_sha"])

        code, events = self.run_cli("release", "events", "--rc", "CASINO-MVP-RC-001")
        self.assertEqual(code, 0)
        self.assertEqual([row["kind"] for row in events][:2],
                         ["rc_sealed", "review_deployed"])

    def test_a_rollback_restores_the_previous_release_and_refuses_without_one(self):
        """The first production release has nothing behind it, and says so."""

        _, _, first = self.release_through(1)
        code, refused = self.run_cli(
            "release", "rollback", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-production")
        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "ROLLBACK_TARGET_UNKNOWN")

        self.release_through(2)
        code, rolled = self.run_cli(
            "release", "rollback", "--rc", "CASINO-MVP-RC-002",
            "--environment", "lodus-casino-production")
        self.assertEqual(code, 0)
        self.assertEqual(rolled["state"], "recovered")
        self.assertEqual(rolled["receipt"]["rollback_of"], first["deployment_id"])
        self.assertIn("production_rollback",
                      [row["kind"] for row in self.run_cli(
                          "release", "events", "--rc", "CASINO-MVP-RC-002")[1]])

    # -- the refusals ---------------------------------------------------- #

    def test_a_bundle_without_an_immutable_artifact_cannot_be_sealed(self):
        body = bundle(1).as_row()
        body["artifact"] = {"kind": "static-bundle", "identity": "latest"}
        path = self.root / "mutable.json"
        path.write_text(json.dumps(body))
        code, result = self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-009", "--bundle", str(path),
            "--verification-ref", "v", "--qa-ref", "q")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_BUNDLE_INVALID")
        self.assertIn("immutable", result["refused"]["detail"])

    def test_sealing_without_independent_qa_evidence_is_refused(self):
        code, result = self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-010",
            "--bundle", self.bundle_path(1), "--verification-ref", "v")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_EVIDENCE_MISSING")

    def test_a_review_surface_that_is_not_reachable_is_refused(self):
        self.seal()
        code, result = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "file:///tmp/casino")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "REVIEW_SURFACE_INVALID")

    def test_production_cannot_stand_in_for_the_owner_review_surface(self):
        self.seal()
        code, result = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-production", "--actor", "factory",
            "--review-url", "https://review.example.invalid/casino")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "REVIEW_ENVIRONMENT_REQUIRED")

    def test_promotion_without_an_owner_validation_is_refused(self):
        self.seal()
        self.review()
        code, result = self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-001",
            "--validation", "CASINO-MVP-VALIDATION-404",
            "--environment", "lodus-casino-production", "--actor", "factory")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "OWNER_VALIDATION_NOT_FOUND")

    def test_return_for_changes_stops_the_promotion(self):
        self.seal()
        _, deployed = self.review()
        self.validate(deployed["deployment_ref"], decision="RETURN_FOR_CHANGES")
        code, result = self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-001",
            "--validation", "CASINO-MVP-VALIDATION-001",
            "--environment", "lodus-casino-production", "--actor", "factory")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "OWNER_VALIDATION_REQUIRED")

    def test_an_unknown_release_candidate_refuses_rather_than_traces(self):
        code, result = self.run_cli("release", "show", "--rc", "CASINO-MVP-RC-404")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RC_NOT_FOUND")

    def test_a_missing_argument_refuses_rather_than_traces(self):
        code, result = self.run_cli("release", "seal", "--rc", "CASINO-MVP-RC-011")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_ARGUMENTS_INVALID")


if __name__ == "__main__":
    unittest.main()
