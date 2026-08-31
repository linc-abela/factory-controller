"""Phase-1 RC, Owner Validation, same-artifact promotion, and rollback."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from factory_controller import production, release
from factory_controller.store import MissionStore


REPOSITORY = "https://example.invalid/lodus-casino.git"


def bundle(number: int, *, artifact_char: str | None = None) -> production.ReleaseBundle:
    marker = artifact_char or "abcdef"[(number - 1) % 6]
    sha = marker * 40
    return production.ReleaseBundle.from_payload({
        "bundle_ref": "lodus-casino-release-%03d" % number,
        "project_id": "lodus-casino",
        "repository": REPOSITORY,
        "release_sha": sha,
        "mission_ref": "lodus-casino:build:%03d" % number,
        "evidence_refs": ["evidence/lodus-casino/%03d.json" % number],
        "evaluator_receipts": ["receipts/lodus-casino/%03d.json" % number],
        "artifact": {"kind": "static-bundle", "identity": "sha256:" + marker * 64},
        "env_schema": {
            "PUBLIC_ORIGIN": {
                "type": "string", "required": True,
                "description": "the target origin",
            },
        },
        "migration": {"forward_ref": "not_applicable", "reverse_ref": "not_applicable"},
        "release_policy_version": "1.0",
        "provenance": {
            "built_by": "factory-controller",
            "built_at": "2026-08-31T00:00:00Z",
            "contract_version": production.CONTRACT_VERSION,
        },
    })


def healthy() -> production.HealthRecord:
    return production.HealthRecord(
        checks_passed=3, checks_failed=0, evidence_ref="probe/lodus-casino/healthy",
        observed_at=1.0,
    )


def failed() -> production.HealthRecord:
    return production.HealthRecord(
        checks_passed=0, checks_failed=2, evidence_ref="probe/lodus-casino/failed",
        observed_at=2.0,
    )


class ReleaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MissionStore(str(Path(self.temporary.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)
        self.lifecycle = release.ReleaseLifecycle(self.store, clock=lambda: 100.0)
        self.port = production.DeterministicDeploymentAdapter()
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-review",
            project_id="lodus-casino",
            environment_class="staging",
            repository=REPOSITORY,
            service_ref="lodus-casino-review-web",
            approver_refs=("owner",),
            autonomous=True,
            policy_version="phase-1",
        ))
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-production",
            project_id="lodus-casino",
            environment_class="production",
            repository=REPOSITORY,
            service_ref="lodus-casino-production-web",
            approver_refs=("owner",),
            policy_version="phase-1",
        ))

    def validated(self, number: int, *, artifact_char: str | None = None,
                  validation_id: str | None = None) -> tuple[release.ReleaseCandidate, release.OwnerValidation]:
        candidate_bundle = bundle(number, artifact_char=artifact_char)
        rc_id = "CASINO-MVP-RC-%03d" % number
        rc = self.lifecycle.seal(
            rc_id, candidate_bundle,
            verification_refs=("verification/casino/%03d" % number,),
            qa_refs=("qa/casino/%03d" % number,),
        )
        review = self.lifecycle.deploy_review(
            rc_id,
            self.ledger,
            self.port,
            review_environment_id="lodus-casino-review",
            requested_by="factory",
            review_url="https://review.example.invalid/lodus-casino/%03d" % number,
            health=healthy(),
        )
        validation = self.lifecycle.record_owner_validation(
            validation_id or "CASINO-MVP-VALIDATION-%03d" % number,
            rc_id,
            deployment_ref=review["deployment_ref"],
            decision="VALIDATED",
            decided_by="owner",
            decided_at=101.0,
            notes="hands-on Owner Validation recorded by test",
        )
        return rc, validation

    def test_review_owner_validation_and_production_share_one_immutable_artifact(self):
        rc, validation = self.validated(1)
        promoted = self.lifecycle.promote_validated(
            rc.rc_id,
            validation.validation_id,
            self.ledger,
            self.port,
            production_environment_id="lodus-casino-production",
            requested_by="factory",
            health=healthy(),
        )

        self.assertEqual(promoted["state"], "healthy")
        self.assertTrue(promoted["same_artifact"])
        self.assertEqual(promoted["candidate_sha"], rc.candidate_sha)
        self.assertEqual(promoted["artifact_digest"], rc.artifact_digest)
        self.assertEqual(promoted["bundle_digest"], rc.bundle_digest)
        self.assertEqual(promoted["receipt"]["release_sha"], rc.candidate_sha)
        self.assertEqual(promoted["receipt"]["bundle_digest"], rc.bundle_digest)

    def test_rebuilt_artifact_cannot_reuse_owner_validation(self):
        rc, validation = self.validated(2)

        with self.assertRaises(release.ReleaseRefusal) as raised:
            self.lifecycle.promote_validated(
                rc.rc_id,
                validation.validation_id,
                self.ledger,
                self.port,
                production_environment_id="lodus-casino-production",
                requested_by="factory",
                artifact_digest="sha256:" + "f" * 64,
            )
        self.assertEqual(raised.exception.code, "ARTIFACT_IDENTITY_MISMATCH")

    def test_return_for_changes_is_not_production_authority(self):
        candidate_bundle = bundle(3)
        rc = self.lifecycle.seal(
            "CASINO-MVP-RC-003", candidate_bundle,
            verification_refs=("verification/casino/003",),
            qa_refs=("qa/casino/003",),
        )
        review = self.lifecycle.deploy_review(
            rc.rc_id,
            self.ledger,
            self.port,
            review_environment_id="lodus-casino-review",
            requested_by="factory",
            review_url="https://review.example.invalid/lodus-casino/003",
            health=healthy(),
        )
        validation = self.lifecycle.record_owner_validation(
            "CASINO-MVP-VALIDATION-003",
            rc.rc_id,
            deployment_ref=review["deployment_ref"],
            decision="RETURN_FOR_CHANGES",
            decided_by="owner",
            decided_at=102.0,
        )

        with self.assertRaises(release.ReleaseRefusal) as raised:
            self.lifecycle.promote_validated(
                rc.rc_id,
                validation.validation_id,
                self.ledger,
                self.port,
                production_environment_id="lodus-casino-production",
                requested_by="factory",
            )
        self.assertEqual(raised.exception.code, "OWNER_VALIDATION_REQUIRED")

    def test_failed_production_deployment_rolls_back_to_recorded_healthy_release(self):
        old_rc, old_validation = self.validated(4, artifact_char="d")
        old_production = self.lifecycle.promote_validated(
            old_rc.rc_id,
            old_validation.validation_id,
            self.ledger,
            self.port,
            production_environment_id="lodus-casino-production",
            requested_by="factory",
            health=healthy(),
        )
        new_rc, new_validation = self.validated(5, artifact_char="e")
        failed_production = self.lifecycle.promote_validated(
            new_rc.rc_id,
            new_validation.validation_id,
            self.ledger,
            self.port,
            production_environment_id="lodus-casino-production",
            requested_by="factory",
            health=failed(),
        )

        rolled_back = self.lifecycle.rollback_production(
            new_rc.rc_id,
            self.ledger,
            self.port,
            production_environment_id="lodus-casino-production",
        )

        self.assertEqual(failed_production["state"], "failed")
        self.assertEqual(rolled_back["state"], "recovered")
        self.assertEqual(rolled_back["receipt"]["rollback_of"], old_production["deployment_id"])
        self.assertEqual(len(self.lifecycle.events(new_rc.rc_id)), 5)

    def test_review_surface_must_be_real_or_explicit_local_and_rc_seal_is_idempotent(self):
        candidate_bundle = bundle(6)
        rc = self.lifecycle.seal(
            "CASINO-MVP-RC-006", candidate_bundle,
            verification_refs=("verification/casino/006",),
            qa_refs=("qa/casino/006",),
        )
        self.assertEqual(
            self.lifecycle.seal(
                "CASINO-MVP-RC-006", candidate_bundle,
                verification_refs=("verification/casino/006",),
                qa_refs=("qa/casino/006",),
            ),
            rc,
        )
        with self.assertRaises(release.ReleaseRefusal) as raised:
            self.lifecycle.deploy_review(
                rc.rc_id,
                self.ledger,
                self.port,
                review_environment_id="lodus-casino-review",
                requested_by="factory",
                review_url="http://review.example.invalid/casino",
            )
        self.assertEqual(raised.exception.code, "REVIEW_SURFACE_INVALID")


if __name__ == "__main__":
    unittest.main()
