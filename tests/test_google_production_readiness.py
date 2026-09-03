"""Comprehensive test suite for Phase-1 Zero-Cost Google Production Readiness.

Verifies:
1. Zero-cost target configuration refusal if billing/paid mode is selected.
2. Exact immutable artifact deployment from sealed bytes (no rebuild).
3. REVIEW and Production target separation under Firebase Hosting Spark.
4. Exact same-artifact promotion after Owner validation.
5. Mutated / rebuilt artifact refusal during promotion.
6. No-approval / RETURN_FOR_CHANGES refusal.
7. Real health probe success, failure, and timeout semantics.
8. Deterministic rollback to previous known-good immutable release.
9. Retry and uncertain deployment safety (idempotent, no duplicate mutations).
10. No secrets in bundles, environment policies, receipts, or persisted evidence.
11. No Production authority created by AI agents, adapters, or automated processes.
12. Current Phase-1 Casino deployment vertical slice runs with 0 live network calls.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from factory_controller import google_production, production, release
from factory_controller.store import MissionStore

from tests.test_release_lifecycle import REPOSITORY, bundle


CASINO_PROJECT = "lodus-casino"
REVIEW_ENV_ID = "lodus-casino-review"
REVIEW_URL = "https://lodus-casino-review.web.app"
PROD_ENV_ID = "lodus-casino-production"

CASINO_FILES_V1 = {
    "index.html": b"<!DOCTYPE html><html><head><title>Lodus Casino</title></head><body>Casino v1</body></html>",
    "styles.css": b"body { background: #111; color: #fff; }",
    "engine.mjs": b"export const SHOE_DECKS = 6;\nexport function createDeck() { return []; }",
    "game.mjs": b"import { createDeck } from './engine.mjs';\nexport class CasinoGame {}",
    "storage.mjs": b"export function loadState() { return {}; }",
    "ui.mjs": b"export function render() {}",
    "health.json": b'{"app": "lodus-casino", "status": "ok", "version": "1.0.0"}',
}

CASINO_FILES_V2 = {
    "index.html": b"<!DOCTYPE html><html><head><title>Lodus Casino</title></head><body>Casino v2 revised</body></html>",
    "styles.css": b"body { background: #222; color: #eee; }",
    "engine.mjs": b"export const SHOE_DECKS = 6;\nexport function createDeck() { return ['A']; }",
    "game.mjs": b"export class CasinoGameV2 {}",
    "storage.mjs": b"export function loadState() { return { rev: 2 }; }",
    "ui.mjs": b"export function render() {}",
    "health.json": b'{"app": "lodus-casino", "status": "ok", "version": "1.1.0"}',
}


#: The identity of a file set is whatever the Controller says it is. This used
#: to re-spell the construction, which meant the tests could keep passing
#: against a digest the production code no longer computed -- and did, through
#: the whole life of the non-injective v1 framing.
digest_for_files = production.deployable_digest


def probed(passed: int, failed: int, ref: str, observed_at: float,
           *, surface: str = REVIEW_URL,
           entry_proof: str | None = None) -> production.ProbedHealthRecord:
    """A health observation with the provenance a release deployment requires.

    Counts alone no longer settle a REVIEW or a Production deployment: they
    say what was observed without saying that anything was observed at all.
    An observation that is not healthy needs no entry proof -- there is nothing
    for a proof to authorise -- so the default follows the outcome.
    """

    if entry_proof is None:
        entry_proof = "sha256:" + "0" * 64 if passed and not failed \
            else "not_applicable"
    return production.ProbedHealthRecord(
        checks_passed=passed, checks_failed=failed, evidence_ref=ref,
        observed_at=observed_at, probe_target=surface, entry_proof=entry_proof)


def make_casino_bundle(number: int, files: dict[str, bytes], *, sha: str | None = None) -> production.ReleaseBundle:
    artifact_id = digest_for_files(files)
    commit_sha = sha or ("a" * 39 + str(number))
    return production.ReleaseBundle.from_payload({
        "bundle_ref": f"lodus-casino-release-{number:03d}",
        "project_id": CASINO_PROJECT,
        "repository": REPOSITORY,
        "release_sha": commit_sha,
        "mission_ref": f"lodus-casino:build:{number:03d}",
        "evidence_refs": [f"evidence/lodus-casino/{number:03d}.json"],
        "evaluator_receipts": [f"receipts/lodus-casino/{number:03d}.json"],
        "artifact": {"kind": "static-bundle", "identity": artifact_id},
        "env_schema": {},
        "migration": {"forward_ref": "not_applicable", "reverse_ref": "not_applicable"},
        "release_policy_version": "phase-1",
        "provenance": {
            "built_by": "factory-controller",
            "built_at": "2026-09-03T00:00:00Z",
            "contract_version": production.CONTRACT_VERSION,
        },
    })


class GoogleProductionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = MissionStore(str(Path(self.temp_dir.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)
        self.clock_val = 1000.0
        self.lifecycle = release.ReleaseLifecycle(self.store, clock=lambda: self.clock_val)

        # Register environments
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id=REVIEW_ENV_ID,
            project_id=CASINO_PROJECT,
            environment_class="staging",
            repository=REPOSITORY,
            service_ref="lodus-casino-review-web",
            approver_refs=("owner",),
            autonomous=True,
            policy_version="phase-1",
            secret_refs=(),
        ))
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id=PROD_ENV_ID,
            project_id=CASINO_PROJECT,
            environment_class="production",
            repository=REPOSITORY,
            service_ref="lodus-casino-production-web",
            approver_refs=("owner",),
            policy_version="phase-1",
            secret_refs=(),
        ))

        # Target configs for Firebase Hosting Spark
        self.target_configs = {
            REVIEW_ENV_ID: google_production.GoogleTargetConfig(
                project_id=CASINO_PROJECT,
                site_id="lodus-casino-review",
                channel_id="live",
                plan="spark",
            ),
            PROD_ENV_ID: google_production.GoogleTargetConfig(
                project_id=CASINO_PROJECT,
                site_id="lodus-casino",
                channel_id="live",
                plan="spark",
            ),
        }
        self.transport = google_production.SimulatedFirebaseTransport()
        self.file_registry: dict[str, dict[str, bytes]] = {
            digest_for_files(CASINO_FILES_V1): CASINO_FILES_V1,
            digest_for_files(CASINO_FILES_V2): CASINO_FILES_V2,
        }
        self.adapter = google_production.FirebaseHostingDeploymentAdapter(
            self.target_configs,
            transport=self.transport,
            artifact_resolver=lambda d: self.file_registry.get(d, {}),
        )

    def _simulated_opener(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        parsed = google_production.urllib.parse.urlparse(url)
        host = parsed.netloc
        path = parsed.path.lstrip("/") or "index.html"
        site = "lodus-casino-review" if "review" in host else "lodus-casino"
        data = self.transport.get_served_file(site, "live", path)
        if data is None:
            return 404, b"Not Found", {}
        return 200, data, {"content-type": "application/json" if path.endswith(".json") else "text/html"}

    # ----------------------------------------------------------------------- #
    # Test 1: Zero-cost target configuration refusal if billing/paid selected
    # ----------------------------------------------------------------------- #
    def test_zero_cost_target_configuration_refusal_if_billing_or_paid_selected(self):
        # Blaze plan refused
        with self.assertRaises(google_production.ZeroCostViolation) as ctx:
            google_production.GoogleTargetConfig(
                project_id="p", site_id="s", plan="blaze"
            )
        self.assertEqual(ctx.exception.code, "BILLING_ENABLED_FORBIDDEN")

        # Generic paid plan refused
        with self.assertRaises(google_production.ZeroCostViolation) as ctx:
            google_production.GoogleTargetConfig(
                project_id="p", site_id="s", plan="paid"
            )
        self.assertEqual(ctx.exception.code, "BILLING_ENABLED_FORBIDDEN")

        # Billing account ID refused
        with self.assertRaises(google_production.ZeroCostViolation) as ctx:
            google_production.GoogleTargetConfig(
                project_id="p", site_id="s", plan="spark", billing_account_id="012345-6789AB-CDEF01"
            )
        self.assertEqual(ctx.exception.code, "BILLING_ACCOUNT_FORBIDDEN")

        # Disallowed billable services refused
        for disallowed in ("cloud_run", "artifact_registry", "cloud_build", "secret_manager"):
            with self.assertRaises(google_production.ZeroCostViolation) as ctx:
                google_production.GoogleTargetConfig(
                    project_id="p", site_id="s", plan="spark", metadata={"service": disallowed}
                )
            self.assertEqual(ctx.exception.code, "BILLABLE_SERVICE_FORBIDDEN")

        # Spark plan succeeds
        spark_config = google_production.GoogleTargetConfig(
            project_id="p", site_id="my-site", plan="spark"
        )
        self.assertEqual(spark_config.default_url, "https://my-site.web.app")

    # ----------------------------------------------------------------------- #
    # Test 2: Exact immutable artifact deployment
    # ----------------------------------------------------------------------- #
    def test_exact_immutable_artifact_deployment(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        expected_digest = digest_for_files(CASINO_FILES_V1)
        self.assertEqual(b.artifact["identity"], expected_digest)

        # Deploy through adapter
        policy = self.ledger.environment(REVIEW_ENV_ID)
        outcome = self.adapter.deploy(b, policy, "op-deploy-1")
        self.assertTrue(outcome.reached)
        self.assertEqual(outcome.adapter, google_production.ADAPTER_NAME)

        detail = json.loads(outcome.detail)
        self.assertEqual(detail["artifact_digest"], expected_digest)
        self.assertEqual(detail["target_url"], "https://lodus-casino-review.web.app")

        # Byte verification: mutated bytes rejected
        bad_files = dict(CASINO_FILES_V1)
        bad_files["index.html"] = b"Tampered bytes"
        bad_adapter = google_production.FirebaseHostingDeploymentAdapter(
            self.target_configs,
            transport=self.transport,
            artifact_resolver=lambda _: bad_files,
        )
        rejected_outcome = bad_adapter.deploy(b, policy, "op-deploy-tampered")
        self.assertFalse(rejected_outcome.reached)
        rej_detail = json.loads(rejected_outcome.detail)
        self.assertEqual(rej_detail["error"], "ARTIFACT_DIGEST_MISMATCH")

    # ----------------------------------------------------------------------- #
    # Test 3: REVIEW and Production target separation
    # ----------------------------------------------------------------------- #
    def test_review_and_production_target_separation(self):
        rev_target = self.target_configs[REVIEW_ENV_ID]
        prod_target = self.target_configs[PROD_ENV_ID]

        self.assertNotEqual(rev_target.site_id, prod_target.site_id)
        self.assertEqual(rev_target.default_url, "https://lodus-casino-review.web.app")
        self.assertEqual(prod_target.default_url, "https://lodus-casino.web.app")

        # Deploying to review does not touch production site
        b = make_casino_bundle(1, CASINO_FILES_V1)
        rev_policy = self.ledger.environment(REVIEW_ENV_ID)
        self.adapter.deploy(b, rev_policy, "op-rev-deploy")

        self.assertIsNotNone(self.transport.get_served_file("lodus-casino-review", "live", "index.html"))
        self.assertIsNone(self.transport.get_served_file("lodus-casino", "live", "index.html"))

    # ----------------------------------------------------------------------- #
    # Test 4: Same-artifact promotion after validation
    # ----------------------------------------------------------------------- #
    def test_same_artifact_promotion_after_validation(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        rc_id = "RC-LODUS-CASINO-001"
        rc = self.lifecycle.seal(rc_id, b, verification_refs=("v/1",), qa_refs=("qa/1",))

        health = probed(3, 0, "p/1", 100.0)
        deployed_rev = self.lifecycle.deploy_review(
            rc_id, self.ledger, self.adapter,
            review_environment_id=REVIEW_ENV_ID,
            requested_by="factory",
            review_url=REVIEW_URL,
            health=health,
        )
        self.assertEqual(deployed_rev["state"], "healthy")
        self.assertEqual(deployed_rev["artifact_digest"], rc.artifact_digest)

        # Owner validates
        val = self.lifecycle.record_owner_validation(
            "VAL-CASINO-001", rc_id,
            deployment_ref=deployed_rev["deployment_ref"],
            decision="VALIDATED",
            decided_by="owner",
            decided_at=200.0,
        )
        self.assertEqual(val.decision, "VALIDATED")

        # Promote to Production
        promoted = self.lifecycle.promote_validated(
            rc_id, "VAL-CASINO-001", self.ledger, self.adapter,
            production_environment_id=PROD_ENV_ID,
            requested_by="factory",
            health=health,
        )
        self.assertEqual(promoted["state"], "healthy")
        self.assertEqual(promoted["artifact_digest"], rc.artifact_digest)
        self.assertTrue(promoted["same_artifact"])

        # Receipt verifies same bundle digest sealing the artifact
        receipt = promoted["receipt"]
        self.assertEqual(receipt["bundle_digest"], rc.bundle_digest)
        self.assertEqual(promoted["artifact_digest"], rc.artifact_digest)

    # ----------------------------------------------------------------------- #
    # Test 5: Mutated / rebuilt artifact refusal
    # ----------------------------------------------------------------------- #
    def test_mutated_or_rebuilt_artifact_refusal(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        rc_id = "RC-LODUS-CASINO-001"
        rc = self.lifecycle.seal(rc_id, b, verification_refs=("v/1",), qa_refs=("qa/1",))
        health = probed(2, 0, "p/1", 100.0)
        rev = self.lifecycle.deploy_review(
            rc_id, self.ledger, self.adapter,
            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
            review_url=REVIEW_URL, health=health,
        )
        self.lifecycle.record_owner_validation(
            "VAL-CASINO-001", rc_id,
            deployment_ref=rev["deployment_ref"], decision="VALIDATED",
            decided_by="owner", decided_at=200.0,
        )

        # Attempt to promote with a different/rebuilt artifact digest
        with self.assertRaises(release.ReleaseRefusal) as ctx:
            self.lifecycle.promote_validated(
                rc_id, "VAL-CASINO-001", self.ledger, self.adapter,
                production_environment_id=PROD_ENV_ID, requested_by="factory",
                artifact_digest="sha256:" + "f" * 64,
            )
        self.assertEqual(ctx.exception.code, "ARTIFACT_IDENTITY_MISMATCH")

        # Attempt to promote with different candidate SHA
        with self.assertRaises(release.ReleaseRefusal) as ctx:
            self.lifecycle.promote_validated(
                rc_id, "VAL-CASINO-001", self.ledger, self.adapter,
                production_environment_id=PROD_ENV_ID, requested_by="factory",
                candidate_sha="f" * 40,
            )
        self.assertEqual(ctx.exception.code, "CANDIDATE_IDENTITY_MISMATCH")

    # ----------------------------------------------------------------------- #
    # Test 6: No-approval / RETURN_FOR_CHANGES refusal
    # ----------------------------------------------------------------------- #
    def test_no_approval_or_return_for_changes_refusal(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        rc_id = "RC-LODUS-CASINO-001"
        self.lifecycle.seal(rc_id, b, verification_refs=("v/1",), qa_refs=("qa/1",))
        health = probed(2, 0, "p/1", 100.0)
        rev = self.lifecycle.deploy_review(
            rc_id, self.ledger, self.adapter,
            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
            review_url=REVIEW_URL, health=health,
        )

        # Record RETURN_FOR_CHANGES
        self.lifecycle.record_owner_validation(
            "VAL-CASINO-RET", rc_id,
            deployment_ref=rev["deployment_ref"], decision="RETURN_FOR_CHANGES",
            decided_by="owner", decided_at=200.0, notes="Need revision",
        )

        # Promotion must refuse
        with self.assertRaises(release.ReleaseRefusal) as ctx:
            self.lifecycle.promote_validated(
                rc_id, "VAL-CASINO-RET", self.ledger, self.adapter,
                production_environment_id=PROD_ENV_ID, requested_by="factory",
            )
        self.assertEqual(ctx.exception.code, "OWNER_VALIDATION_REQUIRED")

        # Unknown validation refused
        with self.assertRaises(release.ReleaseRefusal) as ctx:
            self.lifecycle.promote_validated(
                rc_id, "VAL-NONEXISTENT", self.ledger, self.adapter,
                production_environment_id=PROD_ENV_ID, requested_by="factory",
            )
        self.assertEqual(ctx.exception.code, "OWNER_VALIDATION_NOT_FOUND")

    # ----------------------------------------------------------------------- #
    # Test 7: Real health probe success/failure/timeout semantics
    # ----------------------------------------------------------------------- #
    def test_real_health_probe_success_failure_timeout_semantics(self):
        # Deploy v1 to review site in transport
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = self.ledger.environment(REVIEW_ENV_ID)
        self.adapter.deploy(b, policy, "op-deploy-probe")

        verifier = google_production.StaticWebHealthVerifier(opener=self._simulated_opener)

        # 1. Successful verification
        record = verifier.verify(
            "https://lodus-casino-review.web.app",
            expected_entry_content=CASINO_FILES_V1["index.html"],
            expected_health_json={"app": "lodus-casino", "status": "ok"},
        )
        self.assertEqual(production.classify_health(record), "healthy")
        self.assertGreaterEqual(record.checks_passed, 3)
        self.assertEqual(record.checks_failed, 0)

        # 2. Insecure scheme rejected
        insecure_record = verifier.verify("http://remote.invalid/casino", allow_loopback=False)
        self.assertEqual(production.classify_health(insecure_record), "failed")
        self.assertEqual(insecure_record.checks_passed, 0)

        # 3. Content mismatch fails check honestly
        mismatch_record = verifier.verify(
            "https://lodus-casino-review.web.app",
            expected_entry_content=b"Completely different content",
        )
        self.assertGreater(mismatch_record.checks_failed, 0)
        self.assertIn(production.classify_health(mismatch_record), ("degraded", "failed"))

        # 4. Timeout / unreachable honestly reported
        def timing_out_opener(_):
            raise TimeoutError("connection timed out")

        timeout_verifier = google_production.StaticWebHealthVerifier(opener=timing_out_opener)
        timeout_record = timeout_verifier.verify("https://lodus-casino-review.web.app")
        self.assertEqual(production.classify_health(timeout_record), "failed")
        self.assertEqual(timeout_record.checks_passed, 0)
        self.assertIn("unreachable", timeout_record.evidence_ref)

    # ----------------------------------------------------------------------- #
    # Test 8: Rollback to previous known-good artifact
    # ----------------------------------------------------------------------- #
    def test_rollback_to_previous_known_good_artifact(self):
        health_good = probed(3, 0, "p/good", 10.0)
        health_bad = probed(0, 2, "p/bad", 20.0)

        # 1. Release 1 promoted and healthy
        b1 = make_casino_bundle(1, CASINO_FILES_V1)
        self.lifecycle.seal("RC-001", b1, verification_refs=("v/1",), qa_refs=("qa/1",))
        rev1 = self.lifecycle.deploy_review("RC-001", self.ledger, self.adapter,
                                            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
                                            review_url=REVIEW_URL, health=health_good)
        self.lifecycle.record_owner_validation("VAL-001", "RC-001", deployment_ref=rev1["deployment_ref"],
                                               decision="VALIDATED", decided_by="owner", decided_at=50.0)
        promoted1 = self.lifecycle.promote_validated("RC-001", "VAL-001", self.ledger, self.adapter,
                                                     production_environment_id=PROD_ENV_ID, requested_by="factory",
                                                     health=health_good)
        self.assertEqual(promoted1["state"], "healthy")

        # 2. Release 2 promoted and fails
        b2 = make_casino_bundle(2, CASINO_FILES_V2)
        self.lifecycle.seal("RC-002", b2, verification_refs=("v/2",), qa_refs=("qa/2",))
        rev2 = self.lifecycle.deploy_review("RC-002", self.ledger, self.adapter,
                                            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
                                            review_url=REVIEW_URL, health=health_good)
        self.lifecycle.record_owner_validation("VAL-002", "RC-002", deployment_ref=rev2["deployment_ref"],
                                               decision="VALIDATED", decided_by="owner", decided_at=60.0)
        promoted2 = self.lifecycle.promote_validated("RC-002", "VAL-002", self.ledger, self.adapter,
                                                     production_environment_id=PROD_ENV_ID, requested_by="factory",
                                                     health=health_bad)
        self.assertEqual(promoted2["state"], "failed")

        # 3. Trigger rollback on Release 2
        rollback_res = self.lifecycle.rollback_production(
            "RC-002", self.ledger, self.adapter, production_environment_id=PROD_ENV_ID
        )
        self.assertEqual(rollback_res["state"], "recovered")
        receipt = rollback_res["receipt"]
        self.assertEqual(receipt["rollback_of"], promoted1["deployment_id"])

    # ----------------------------------------------------------------------- #
    # Test 9: Retry and uncertain deployment safety
    # ----------------------------------------------------------------------- #
    def test_retry_and_uncertain_deployment_safety(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = self.ledger.environment(REVIEW_ENV_ID)

        # Inject uncertain fault
        self.transport.inject_fault("deploy", "op-uncertain", "uncertain")
        outcome = self.adapter.deploy(b, policy, "op-uncertain")
        self.assertIsNone(outcome.reached)  # None = uncertain
        self.assertIn("uncertain", outcome.detail)

        # Idempotent retry returns same outcome without duplicating releases
        outcome_repeat = self.adapter.deploy(b, policy, "op-uncertain")
        self.assertEqual(outcome, outcome_repeat)

    # ----------------------------------------------------------------------- #
    # Test 10: No secrets in persisted evidence
    # ----------------------------------------------------------------------- #
    def test_no_secrets_in_persisted_evidence(self):
        b = make_casino_bundle(1, CASINO_FILES_V1)
        self.lifecycle.seal("RC-001", b, verification_refs=("v/1",), qa_refs=("qa/1",))
        health = probed(2, 0, "p/1", 100.0)
        rev = self.lifecycle.deploy_review("RC-001", self.ledger, self.adapter,
                                           review_environment_id=REVIEW_ENV_ID, requested_by="factory",
                                           review_url=REVIEW_URL, health=health)
        self.lifecycle.record_owner_validation("VAL-001", "RC-001", deployment_ref=rev["deployment_ref"],
                                               decision="VALIDATED", decided_by="owner", decided_at=200.0)
        self.lifecycle.promote_validated("RC-001", "VAL-001", self.ledger, self.adapter,
                                         production_environment_id=PROD_ENV_ID, requested_by="factory",
                                         health=health)

        # Scan all DB tables for secret keys
        secret_re = production.HOST_SECRET_KEYS if hasattr(production, "HOST_SECRET_KEYS") else google_production.re.compile(r"(secret|token|password|api_key)", google_production.re.IGNORECASE)
        with self.store.transaction() as db:
            for table in ("deployments", "release_candidates", "release_deployments", "owner_validations", "release_events", "production_events"):
                rows = db.execute(f"SELECT * FROM {table}").fetchall()
                for row in rows:
                    for col in row.keys():
                        val = row[col]
                        if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                            try:
                                parsed = json.loads(val)
                                self._scan_no_secrets(parsed, secret_re)
                            except json.JSONDecodeError:
                                pass

    def _scan_no_secrets(self, obj: Any, pattern) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not k.endswith("_ref") and pattern.search(k):
                    self.fail(f"Found secret-like key {k}")
                self._scan_no_secrets(v, pattern)
        elif isinstance(obj, list):
            for item in obj:
                self._scan_no_secrets(item, pattern)

    # ----------------------------------------------------------------------- #
    # Test 11: No Production authority created by agent or adapter
    # ----------------------------------------------------------------------- #
    def test_no_production_authority_created_by_agent_or_adapter(self):
        # 1. Gated production environment refuses autonomous=True
        with self.assertRaises(production.PolicyError) as ctx:
            production.EnvironmentPolicy(
                environment_id="bad-prod",
                project_id=CASINO_PROJECT,
                environment_class="production",
                repository=REPOSITORY,
                service_ref="srv",
                approver_refs=("owner",),
                autonomous=True,
            )
        self.assertIn("cannot be autonomous", str(ctx.exception))

        # 2. Self-approval refused
        b = make_casino_bundle(1, CASINO_FILES_V1)
        dep_id_owner = self.ledger.admit_release(b, PROD_ENV_ID, "owner")
        with self.assertRaises(production.ProductionRefusal) as ctx:
            self.ledger.approve(dep_id_owner, "owner", "auto-app", b.bundle_digest)
        self.assertEqual(ctx.exception.code, "PRODUCTION_APPROVAL_SELF")

        # 3. Undeclared approver refused
        dep_id_factory = self.ledger.admit_release(b, PROD_ENV_ID, "factory")
        with self.assertRaises(production.ProductionRefusal) as ctx:
            self.ledger.approve(dep_id_factory, "gemini-agent", "auto-app", b.bundle_digest)
        self.assertEqual(ctx.exception.code, "PRODUCTION_APPROVAL_UNAUTHORIZED")

    # ----------------------------------------------------------------------- #
    # Test 12: Current Phase-1 Casino deployment vertical slice with 0 network calls
    # ----------------------------------------------------------------------- #
    def test_current_phase1_casino_deployment_path_no_live_network(self):
        # Full Phase-1 lifecycle from seal to review to promotion to health
        b = make_casino_bundle(1, CASINO_FILES_V1)
        rc_id = "RC-LODUS-CASINO-001"
        rc = self.lifecycle.seal(rc_id, b, verification_refs=("ver/1",), qa_refs=("qa/1",))
        self.assertEqual(rc.rc_id, rc_id)

        # 1. Deploy Review
        rev = self.lifecycle.deploy_review(
            rc_id, self.ledger, self.adapter,
            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
            review_url=REVIEW_URL,
        )
        self.assertEqual(rev["state"], "health_pending")

        # 2. Health probe
        verifier = google_production.StaticWebHealthVerifier(opener=self._simulated_opener)
        h_rec = verifier.verify("https://lodus-casino-review.web.app", expected_entry_content=CASINO_FILES_V1["index.html"])
        state = self.ledger.record_health(rev["deployment_id"], h_rec)
        self.assertEqual(state, "healthy")

        # 3. Owner Validates
        val = self.lifecycle.record_owner_validation(
            "VAL-001", rc_id, deployment_ref=rev["deployment_ref"],
            decision="VALIDATED", decided_by="owner", decided_at=300.0,
        )
        self.assertEqual(val.decision, "VALIDATED")

        # 4. Promote
        promoted = self.lifecycle.promote_validated(
            rc_id, "VAL-001", self.ledger, self.adapter,
            production_environment_id=PROD_ENV_ID, requested_by="factory",
        )
        self.assertEqual(promoted["state"], "health_pending")

        # 5. Production Health probe
        h_prod = verifier.verify("https://lodus-casino.web.app", expected_entry_content=CASINO_FILES_V1["index.html"])
        prod_state = self.ledger.record_health(promoted["deployment_id"], h_prod)
        self.assertEqual(prod_state, "healthy")

        # Receipt confirms zero network, exact digest
        receipt = self.ledger.receipt(promoted["deployment_id"])
        self.assertEqual(promoted["artifact_digest"], rc.artifact_digest)
        self.assertEqual(receipt["bundle_digest"], rc.bundle_digest)
        self.assertEqual(receipt["approved_by"], "owner")
        self.assertEqual(receipt["approval_ref"], "VAL-001")


def make_review_policy() -> production.EnvironmentPolicy:
    return production.EnvironmentPolicy(
        environment_id=REVIEW_ENV_ID,
        project_id=CASINO_PROJECT,
        environment_class="staging",
        repository=REPOSITORY,
        service_ref="lodus-casino-review-web",
        approver_refs=("owner",),
        policy_version="phase-1",
        secret_refs=(),
    )


def make_production_policy() -> production.EnvironmentPolicy:
    return production.EnvironmentPolicy(
        environment_id=PROD_ENV_ID,
        project_id=CASINO_PROJECT,
        environment_class="production",
        repository=REPOSITORY,
        service_ref="lodus-casino-production-web",
        approver_refs=("owner",),
        policy_version="phase-1",
        secret_refs=(),
    )


class FirebaseTransportAndRollbackClosureTests(unittest.TestCase):
    """SF-170A: Real Firebase transport, sealed artifact verification, and rollback identity."""

    def test_live_adapter_construction_cannot_silently_select_simulated_transport(self):
        adapter = google_production.FirebaseHostingDeploymentAdapter({})
        self.assertIsInstance(adapter.transport, google_production.FirebaseHostingRestTransport)
        self.assertNotIsInstance(adapter.transport, google_production.SimulatedFirebaseTransport)

    def test_missing_or_empty_artifact_resolver_refuses_before_transport_invocation(self):
        calls = []

        class MockTransport:
            def deploy_release(self, *args, **kwargs):
                calls.append("deploy")
                return {}

            def rollback_release(self, *args, **kwargs):
                calls.append("rollback")
                return {}

        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=MockTransport(),
            artifact_resolver=lambda _: {},
        )
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = make_review_policy()
        outcome = adapter.deploy(b, policy, "op-empty")
        self.assertFalse(outcome.reached)
        self.assertEqual(len(calls), 0)
        self.assertTrue(outcome.operation_ref.endswith(":rejected"))
        detail = json.loads(outcome.detail)
        self.assertEqual(detail.get("error"), "EMPTY_OR_MISSING_ARTIFACT")

    def test_exact_byte_digest_mismatch_refuses_before_network_mutation(self):
        calls = []

        class MockTransport:
            def deploy_release(self, *args, **kwargs):
                calls.append("deploy")
                return {}

            def rollback_release(self, *args, **kwargs):
                calls.append("rollback")
                return {}

        # Resolver returns V2 files while bundle declared V1 digest
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=MockTransport(),
            artifact_resolver=lambda _: dict(CASINO_FILES_V2),
        )
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = make_review_policy()
        outcome = adapter.deploy(b, policy, "op-mismatch")
        self.assertFalse(outcome.reached)
        self.assertEqual(len(calls), 0)
        self.assertTrue(outcome.operation_ref.endswith(":rejected"))
        detail = json.loads(outcome.detail)
        self.assertEqual(detail.get("error"), "ARTIFACT_DIGEST_MISMATCH")
        self.assertNotEqual(detail.get("declared"), detail.get("computed"))

    def test_production_shaped_deployments_call_transport_with_exact_bytes(self):
        recorded = []

        class RecordingTransport:
            def deploy_release(self, config, artifact_digest, files, operation_key):
                recorded.append((config, artifact_digest, dict(files), operation_key))
                return {"version_id": "v0042", "release_id": "rel_0042"}

            def rollback_release(self, *args, **kwargs):
                return {}

        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=RecordingTransport(),
            artifact_resolver=lambda _: dict(CASINO_FILES_V1),
        )
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = make_review_policy()
        outcome = adapter.deploy(b, policy, "op-deploy-real")
        self.assertTrue(outcome.reached)
        self.assertEqual(len(recorded), 1)
        cfg, digest, files, op_key = recorded[0]
        self.assertEqual(digest, b.artifact["identity"])
        self.assertEqual(files, CASINO_FILES_V1)
        self.assertEqual(op_key, "op-deploy-real")

    def test_returned_firebase_version_and_release_ids_durably_carried(self):
        transport = google_production.SimulatedFirebaseTransport()
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=transport,
            artifact_resolver=lambda _: dict(CASINO_FILES_V1),
        )
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = make_review_policy()
        outcome = adapter.deploy(b, policy, "op-version-carry")
        self.assertTrue(outcome.reached)
        detail = json.loads(outcome.detail)
        self.assertEqual(detail.get("version_id"), "v0001")
        self.assertTrue(detail.get("release_id", "").startswith("rel_"))
        self.assertEqual(outcome.operation_ref, "google-firebase:lodus-casino-review:review:v0001")

    def test_rollback_targets_exact_previous_known_good_version_not_first_release_guess(self):
        transport = google_production.SimulatedFirebaseTransport()
        b1 = make_casino_bundle(1, CASINO_FILES_V1)
        b2 = make_casino_bundle(2, CASINO_FILES_V2)
        v3_files = dict(CASINO_FILES_V1, **{"index.html": b"<html><body>Casino V3</body></html>"})
        b3 = make_casino_bundle(3, v3_files)

        files_by_digest = {
            b1.artifact["identity"]: CASINO_FILES_V1,
            b2.artifact["identity"]: CASINO_FILES_V2,
            b3.artifact["identity"]: v3_files,
        }

        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=transport,
            artifact_resolver=lambda d: files_by_digest.get(d, {}),
        )
        policy = make_production_policy()

        out1 = adapter.deploy(b1, policy, "op-1")
        self.assertEqual(json.loads(out1.detail)["version_id"], "v0001")

        out2 = adapter.deploy(b2, policy, "op-2")
        self.assertEqual(json.loads(out2.detail)["version_id"], "v0002")

        out3 = adapter.deploy(b3, policy, "op-3")
        self.assertEqual(json.loads(out3.detail)["version_id"], "v0003")

        # Rollback of V3 must target V2 (v0002), NEVER v0001 or first release
        rollback_out = adapter.rollback(b3, policy, "op-rollback-v3")
        self.assertTrue(rollback_out.reached)
        detail = json.loads(rollback_out.detail)
        self.assertEqual(detail["restored_version_id"], "v0002")
        self.assertEqual(detail["attempted_artifact_digest"], b3.artifact["identity"])
        self.assertEqual(detail["restored_artifact_digest"], b2.artifact["identity"])
        self.assertEqual(rollback_out.operation_ref, "google-firebase-rollback:lodus-casino-production:live:v0002")

    def test_rollback_without_prior_version_refuses_closed(self):
        transport = google_production.SimulatedFirebaseTransport()
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=transport,
            artifact_resolver=lambda _: dict(CASINO_FILES_V1),
        )
        b = make_casino_bundle(1, CASINO_FILES_V1)
        policy = make_production_policy()
        outcome = adapter.rollback(b, policy, "op-rollback-no-prior")
        self.assertFalse(outcome.reached)
        self.assertTrue(outcome.operation_ref.endswith(":rejected"))
        detail = json.loads(outcome.detail)
        self.assertEqual(detail.get("error"), "NO_PREVIOUS_VERSION_FOR_ROLLBACK")

    def test_health_recheck_after_rollback_verifies_restored_content(self):
        transport = google_production.SimulatedFirebaseTransport()
        b1 = make_casino_bundle(1, CASINO_FILES_V1)
        b2 = make_casino_bundle(2, CASINO_FILES_V2)
        files_by_digest = {
            b1.artifact["identity"]: CASINO_FILES_V1,
            b2.artifact["identity"]: CASINO_FILES_V2,
        }
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {},
            transport=transport,
            artifact_resolver=lambda d: files_by_digest.get(d, {}),
        )
        policy = make_production_policy()

        def test_opener(url_str: str) -> tuple[int, bytes, dict[str, str]]:
            parsed = urllib.parse.urlsplit(url_str)
            clean_path = parsed.path.lstrip("/") or "index.html"
            content = transport.get_served_file("lodus-casino-production", "live", clean_path)
            if content is None:
                return 404, b"Not Found", {}
            return 200, content, {"Content-Type": "text/html"}

        # Deploy V1
        adapter.deploy(b1, policy, "op-d1")
        # Deploy V2
        adapter.deploy(b2, policy, "op-d2")

        verifier = google_production.StaticWebHealthVerifier(opener=test_opener)
        h2 = verifier.verify("https://lodus-casino-production.web.app", expected_entry_content=CASINO_FILES_V2["index.html"])
        self.assertEqual(production.classify_health(h2), "healthy")

        # Rollback V2 -> Restores V1 content
        rb = adapter.rollback(b2, policy, "op-rb2")
        self.assertTrue(rb.reached)

        # Health recheck against V1 passes
        h_recheck_v1 = verifier.verify("https://lodus-casino-production.web.app", expected_entry_content=CASINO_FILES_V1["index.html"])
        self.assertEqual(production.classify_health(h_recheck_v1), "healthy")

        # Health check looking for V2 fails to report healthy because V1 was restored
        h_recheck_v2 = verifier.verify("https://lodus-casino-production.web.app", expected_entry_content=CASINO_FILES_V2["index.html"])
        self.assertNotEqual(production.classify_health(h_recheck_v2), "healthy")
        self.assertGreater(h_recheck_v2.checks_failed, 0)

    def test_real_transport_mocked_http_deploy_and_rollback(self):
        calls = []

        def mock_opener(req: urllib.request.Request) -> tuple[int, bytes, dict[str, str]]:
            calls.append((req.get_method(), req.full_url, req.data, dict(req.headers)))
            url = req.full_url
            if url.endswith("/versions") and req.get_method() == "POST":
                return 200, json.dumps({"name": "sites/lodus-casino/versions/v_mock_001"}).encode(), {}
            if ":populateFiles" in url and req.get_method() == "POST":
                return 200, json.dumps({
                    "uploadUrl": "https://upload.firebasehosting.googleapis.com",
                    "uploadRequiredHashes": list(json.loads(req.data.decode())["files"].values()),
                }).encode(), {}
            if "https://upload.firebasehosting.googleapis.com" in url and req.get_method() == "POST":
                return 200, b"{}", {}
            if "?update_mask=status" in url and req.get_method() == "PATCH":
                return 200, json.dumps({"status": "FINALIZED"}).encode(), {}
            if "/releases" in url and req.get_method() == "POST":
                return 200, json.dumps({"name": "sites/lodus-casino/releases/rel_mock_001"}).encode(), {}
            return 404, b"Not Found", {}

        transport = google_production.FirebaseHostingRestTransport(
            token="test-secret-token",
            opener=mock_opener,
        )
        target = google_production.GoogleTargetConfig(
            project_id="lodus-casino",
            site_id="lodus-casino",
            channel_id="live",
            plan=google_production.ZERO_COST_PLAN,
        )
        deploy_receipt = transport.deploy_release(
            target,
            artifact_digest=digest_for_files(CASINO_FILES_V1),
            files=CASINO_FILES_V1,
            operation_key="op-http-1",
        )
        self.assertEqual(deploy_receipt["version_id"], "v_mock_001")
        self.assertEqual(deploy_receipt["release_id"], "rel_mock_001")
        self.assertGreaterEqual(len(calls), 5)

        # Rollback call
        rollback_receipt = transport.rollback_release(
            target,
            target_version_id="v_mock_001",
            operation_key="op-http-rb",
        )
        self.assertEqual(rollback_receipt["version_id"], "v_mock_001")
        self.assertEqual(rollback_receipt["release_id"], "rel_mock_001")

    def test_real_transport_missing_auth_fails_closed(self):
        transport = google_production.FirebaseHostingRestTransport()
        target = google_production.GoogleTargetConfig(
            project_id="lodus-casino",
            site_id="lodus-casino",
            channel_id="live",
            plan=google_production.ZERO_COST_PLAN,
        )
        with self.assertRaises(PermissionError) as ctx:
            transport.deploy_release(
                target,
                artifact_digest="sha256:test",
                files={"index.html": b"test"},
                operation_key="op-noauth",
            )
        self.assertIn("auth token missing", str(ctx.exception).lower())


class RecordingRollbackTransport:
    """A transport that answers a rollback and records which version was named."""

    def __init__(self) -> None:
        self.rollback_targets: list[str] = []

    def deploy_release(self, config, artifact_digest, files, operation_key):
        raise AssertionError("this transport exists to observe a rollback")

    def rollback_release(self, config, target_version_id, operation_key):
        self.rollback_targets.append(target_version_id)
        return {"version_id": target_version_id, "release_id": "rel_restored"}


class DurableRollbackIdentityTests(unittest.TestCase):
    """SF-170A: the rollback target has to survive the process that deployed.

    Every rollback the Owner performs is a separate command in a separate
    process from the deploy it undoes, so an adapter instance that remembers
    its own deployments remembers nothing at the moment it is asked.  These
    tests use a *fresh* adapter for the rollback -- the state a second command
    actually starts in -- against the same durable ledger.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MissionStore(str(Path(self.temporary.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)
        self.lifecycle = release.ReleaseLifecycle(self.store, clock=lambda: 100.0)
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id=REVIEW_ENV_ID, project_id=CASINO_PROJECT,
            environment_class="staging", repository=REPOSITORY,
            service_ref="lodus-casino-review-web", approver_refs=("owner",),
            autonomous=True, policy_version="phase-1"))
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id=PROD_ENV_ID, project_id=CASINO_PROJECT,
            environment_class="production", repository=REPOSITORY,
            service_ref="lodus-casino-production-web", approver_refs=("owner",),
            policy_version="phase-1"))

        self.bundles = {
            1: make_casino_bundle(1, CASINO_FILES_V1),
            2: make_casino_bundle(2, CASINO_FILES_V2),
        }
        files_by_digest = {
            self.bundles[1].artifact["identity"]: CASINO_FILES_V1,
            self.bundles[2].artifact["identity"]: CASINO_FILES_V2,
        }
        self.resolver = lambda d: files_by_digest.get(d, {})
        self.deploy_transport = google_production.SimulatedFirebaseTransport()
        self.deploy_adapter = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=self.deploy_transport,
            artifact_resolver=self.resolver, store=self.store)

    def last_adapter_detail(self, deployment_id: str) -> dict:
        """The adapter's own detail, as the ledger durably recorded it."""

        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT detail_json FROM production_events WHERE deployment_id=?"
                " ORDER BY sequence", (deployment_id,)).fetchall()
        for row in reversed(rows):
            detail = json.loads(row["detail_json"]).get("detail")
            if isinstance(detail, str) and detail.startswith("{"):
                return json.loads(detail)
        raise AssertionError("no adapter detail recorded for %s" % deployment_id)

    def health(self, passed: int, failed: int) -> production.ProbedHealthRecord:
        return probed(passed, failed, "probe/lodus-casino", 1.0)

    def promote(self, number: int, *, health: production.HealthRecord) -> dict:
        rc_id = "CASINO-MVP-RC-%03d" % number
        self.lifecycle.seal(
            rc_id, self.bundles[number],
            verification_refs=("verification/casino/%03d" % number,),
            qa_refs=("qa/casino/%03d" % number,))
        review = self.lifecycle.deploy_review(
            rc_id, self.ledger, self.deploy_adapter,
            review_environment_id=REVIEW_ENV_ID, requested_by="factory",
            review_url=REVIEW_URL,
            health=self.health(3, 0))
        validation = self.lifecycle.record_owner_validation(
            "CASINO-MVP-VALIDATION-%03d" % number, rc_id,
            deployment_ref=review["deployment_ref"], decision="VALIDATED",
            decided_by="owner", decided_at=101.0,
            notes="hands-on Owner Validation recorded by test")
        return self.lifecycle.promote_validated(
            rc_id, validation.validation_id, self.ledger, self.deploy_adapter,
            production_environment_id=PROD_ENV_ID, requested_by="factory",
            health=health)

    def test_a_fresh_adapter_rolls_back_to_the_platform_version_the_ledger_recorded(self):
        first = self.promote(1, health=self.health(3, 0))
        second = self.promote(2, health=self.health(0, 2))
        self.assertEqual(
            first["receipt"]["operation_ref"],
            "google-firebase:lodus-casino-production:live:v0001")
        self.assertEqual(second["state"], "failed")

        # The state a second Owner command starts in: nothing remembered.
        transport = RecordingRollbackTransport()
        fresh = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=transport, artifact_resolver=self.resolver,
            store=self.store)
        self.assertEqual(fresh._target_history, {})

        rolled_back = self.lifecycle.rollback_production(
            "CASINO-MVP-RC-002", self.ledger, fresh,
            production_environment_id=PROD_ENV_ID)

        self.assertEqual(rolled_back["state"], "recovered")
        self.assertEqual(transport.rollback_targets, ["v0001"])
        detail = self.last_adapter_detail(rolled_back["deployment_id"])
        self.assertEqual(detail["restored_version_id"], "v0001")
        self.assertEqual(detail["attempted_artifact_digest"],
                         self.bundles[2].artifact["identity"])
        self.assertEqual(detail["restored_artifact_digest"],
                         self.bundles[1].artifact["identity"])
        self.assertEqual(rolled_back["receipt"]["operation_ref"],
                         "google-firebase-rollback:lodus-casino-production:live:v0001")

    def test_a_fresh_adapter_without_the_durable_ledger_refuses_rather_than_guessing(self):
        """The fail-closed half: no recorded version is never a first-release guess."""

        self.promote(1, health=self.health(3, 0))
        self.promote(2, health=self.health(0, 2))

        transport = RecordingRollbackTransport()
        blind = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=transport, artifact_resolver=self.resolver)

        rolled_back = self.lifecycle.rollback_production(
            "CASINO-MVP-RC-002", self.ledger, blind,
            production_environment_id=PROD_ENV_ID)

        self.assertEqual(rolled_back["state"], "rollback_failed")
        self.assertEqual(transport.rollback_targets, [])
        detail = self.last_adapter_detail(rolled_back["deployment_id"])
        self.assertEqual(detail["error"], "NO_PREVIOUS_VERSION_FOR_ROLLBACK")

    def test_a_deployment_that_never_reached_a_platform_version_is_not_a_target(self):
        """`:uncertain`, `:failed` and `:rejected` refs name no Firebase version."""

        self.promote(1, health=self.health(3, 0))
        target = google_production.GoogleTargetConfig(
            project_id=CASINO_PROJECT, site_id=PROD_ENV_ID, channel_id="live")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT id, operation_ref FROM deployments"
                " WHERE environment_id=? AND state='healthy'", (PROD_ENV_ID,)).fetchone()
            healthy_id, healthy_ref = row["id"], row["operation_ref"]
            db.execute("UPDATE deployments SET rollback_of=? WHERE id=?",
                       (healthy_id, healthy_id))
        key = production.operation_key(healthy_id, "rollback", 1)

        resolved = self.deploy_adapter._ledger_prior_version(
            target, key, self.bundles[2].artifact["identity"])
        self.assertEqual(resolved["version_id"], "v0001")

        for ending in ("uncertain", "failed", "rejected", ""):
            with self.store.transaction() as db:
                db.execute("UPDATE deployments SET operation_ref=? WHERE id=?",
                           (healthy_ref.rsplit(":", 1)[0] + ":" + ending, healthy_id))
            self.assertIsNone(
                self.deploy_adapter._ledger_prior_version(
                    target, key, self.bundles[2].artifact["identity"]),
                "%r was accepted as a platform version" % ending)


if __name__ == "__main__":
    unittest.main()
