"""Stage 6: what the Factory may and may not cause to happen in Production.

Every test here is a statement about an operated environment, so each one is
written as the thing that must remain impossible rather than the thing that
should work.  The happy paths exist to prove the impossible ones are not
impossible by accident.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import production
from factory_controller.store import MissionStore

SHA = "a" * 40
OTHER_SHA = "b" * 40


def bundle_payload(**overrides):
    payload = {
        "bundle_ref": "rc-001",
        "project_id": "shop",
        "repository": "https://example.invalid/shop.git",
        "release_sha": SHA,
        "mission_ref": "SF-138",
        "evidence_refs": ["evidence/shop/SF-138.json"],
        "evaluator_receipts": ["receipts/evaluate.json"],
        "artifact": {"kind": "image", "identity": "sha256:" + "c" * 64},
        "env_schema": {"PORT": {"type": "integer", "required": True},
                       "LOG_LEVEL": {"type": "string", "required": False}},
        "migration": {"forward_ref": "migrations/001.sql",
                      "reverse_ref": "migrations/001.down.sql"},
        "release_policy_version": "1.0",
        "provenance": {"built_by": "factory-controller",
                       "built_at": "2026-08-27T00:00:00Z",
                       "contract_version": production.CONTRACT_VERSION},
    }
    payload.update(overrides)
    return payload


def bundle(**overrides):
    return production.ReleaseBundle.from_payload(bundle_payload(**overrides))


def environment(**overrides):
    values = {
        "environment_id": "shop-prod",
        "project_id": "shop",
        "environment_class": "production",
        "repository": "https://example.invalid/shop.git",
        "service_ref": "shop-web",
        "approver_refs": ("owner", "deputy"),
    }
    values.update(overrides)
    return production.EnvironmentPolicy(**values)


def staging(**overrides):
    values = {
        "environment_id": "shop-staging",
        "environment_class": "staging",
        "autonomous": True,
        "approver_refs": ("owner",),
    }
    values.update(overrides)
    return environment(**values)


def healthy(passed=3):
    return production.HealthRecord(checks_passed=passed, checks_failed=0,
                                   evidence_ref="probe/1", observed_at=1.0)


def unhealthy():
    return production.HealthRecord(checks_passed=0, checks_failed=3,
                                   evidence_ref="probe/2", observed_at=2.0)


class LedgerCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MissionStore(str(Path(self.tmp.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)
        self.port = production.DeterministicDeploymentAdapter()

    def live(self, env=None, requested_by="factory"):
        """Admit, approve where required, deploy, and report health."""
        env = env or staging()
        self.ledger.register_environment(env)
        deployment = self.ledger.admit_release(bundle(), env.environment_id,
                                               requested_by)
        if self.ledger.deployment(deployment)["state"] == "awaiting_approval":
            self.ledger.approve(deployment, "owner", "signoff/1",
                                bundle().bundle_digest)
        self.ledger.deploy(deployment, self.port)
        self.ledger.record_health(deployment, healthy())
        return deployment


# --------------------------------------------------------------------------- #


class ReleaseBundleTests(unittest.TestCase):
    """The bundle is the whole handoff, so its shape is the first boundary."""

    def test_the_digest_is_stable_and_covers_every_field(self):
        first, second = bundle(), bundle()
        self.assertEqual(first.bundle_digest, second.bundle_digest)
        self.assertNotEqual(first.bundle_digest,
                            bundle(release_sha=OTHER_SHA).bundle_digest)
        self.assertNotEqual(first.bundle_digest,
                            bundle(release_policy_version="1.1").bundle_digest)

    def test_a_moving_tag_is_not_an_artifact_identity(self):
        with self.assertRaises(production.PolicyError):
            bundle(artifact={"kind": "image", "identity": "latest"})

    def test_a_project_with_no_artifact_says_so_in_the_absence_vocabulary(self):
        self.assertEqual(bundle(artifact="not_applicable").artifact, "not_applicable")
        with self.assertRaises(production.PolicyError):
            bundle(artifact="none")
        with self.assertRaises(production.PolicyError):
            bundle(artifact="")

    def test_the_environment_schema_has_nowhere_to_put_a_value(self):
        """Not scanned for secrets -- structurally unable to hold one."""
        for spec in ({"type": "string", "value": "hunter2"},
                     {"type": "string", "default": "hunter2"},
                     {"type": "string", "example": "hunter2"}):
            with self.assertRaises(production.PolicyError):
                bundle(env_schema={"TOKEN": spec})

    def test_a_forward_migration_with_no_reverse_is_refused_at_the_bundle(self):
        with self.assertRaises(production.PolicyError):
            bundle(migration={"forward_ref": "migrations/001.sql",
                              "reverse_ref": "unknown"})
        self.assertEqual(
            bundle(migration={"forward_ref": "not_applicable",
                              "reverse_ref": "not_applicable"}).migration,
            {"forward_ref": "not_applicable", "reverse_ref": "not_applicable"})

    def test_an_undefined_field_is_refused_rather_than_ignored(self):
        """An ignored field is how an approval arrives inside its own subject."""
        with self.assertRaises(production.PolicyError) as caught:
            bundle(approved_by="owner")
        self.assertIn("approved_by", str(caught.exception))

    def test_a_release_is_identified_by_a_commit_not_a_branch(self):
        for value in ("main", "v1.2.3", "A" * 40, "abc"):
            with self.assertRaises(production.PolicyError):
                bundle(release_sha=value)


class EnvironmentRegistryTests(LedgerCase):

    def test_a_production_environment_cannot_be_autonomous(self):
        with self.assertRaises(production.PolicyError) as caught:
            environment(autonomous=True)
        self.assertIn("approved by a person", str(caught.exception))

    def test_a_production_environment_must_name_an_approver(self):
        with self.assertRaises(production.PolicyError):
            environment(approver_refs=())

    def test_a_secret_reference_must_be_this_projects_own_name(self):
        """The pattern is not the boundary; the namespace and the source are.

        A lower-case token matches the pattern perfectly well, so the pattern
        alone would be a check that reads stronger than it is.  What a foreign
        value cannot do is belong to this project's namespace.
        """
        environment(secret_refs=("shop.database", "shop.mail"))
        for value in ("sk-live-0123456789abcdef", "mail.relay", "A" * 80,
                      "Bearer xyz", ""):
            with self.assertRaises(production.PolicyError):
                environment(secret_refs=(value,))

    def test_an_environment_cannot_change_class_under_the_same_id(self):
        self.ledger.register_environment(environment())
        with self.assertRaises(production.PolicyError):
            self.ledger.register_environment(
                environment(environment_class="staging", autonomous=True))

    def test_registration_survives_a_reread_intact(self):
        self.ledger.register_environment(environment(secret_refs=("shop.database",)))
        stored = self.ledger.environment("shop-prod")
        self.assertEqual(stored.approver_refs, ("owner", "deputy"))
        self.assertEqual(stored.secret_refs, ("shop.database",))
        self.assertFalse(stored.autonomous)


class ProductionAuthorityTests(LedgerCase):
    """The criterion the whole stage exists for."""

    def setUp(self):
        super().setUp()
        self.ledger.register_environment(environment())
        self.deployment = self.ledger.admit_release(bundle(), "shop-prod", "factory")

    def test_an_admitted_release_waits_for_a_person(self):
        self.assertEqual(self.ledger.deployment(self.deployment)["state"],
                         "awaiting_approval")

    def test_the_factory_cannot_deploy_to_production_on_its_own(self):
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.deploy(self.deployment, self.port)
        self.assertEqual(caught.exception.code, "PRODUCTION_APPROVAL_REQUIRED")
        self.assertEqual(self.port.calls, [])

    def test_an_actor_the_owner_never_listed_cannot_approve(self):
        for actor in ("factory", "an-agent", "alerting", "deployment-adapter"):
            with self.assertRaises(production.ProductionRefusal) as caught:
                self.ledger.approve(self.deployment, actor, "ref",
                                    bundle().bundle_digest)
            self.assertEqual(caught.exception.code,
                             "PRODUCTION_APPROVAL_UNAUTHORIZED")

    def test_the_requester_cannot_approve_its_own_release(self):
        self.ledger.register_environment(
            environment(environment_id="shop-prod-b",
                        approver_refs=("factory", "owner")))
        deployment = self.ledger.admit_release(bundle(), "shop-prod-b", "factory")
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.approve(deployment, "factory", "ref", bundle().bundle_digest)
        self.assertEqual(caught.exception.code, "PRODUCTION_APPROVAL_SELF")

    def test_an_approval_cannot_be_carried_onto_different_bytes(self):
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.approve(self.deployment, "owner", "signoff/1",
                                bundle(release_sha=OTHER_SHA).bundle_digest)
        self.assertEqual(caught.exception.code, "PRODUCTION_APPROVAL_BUNDLE_MISMATCH")

    def test_health_evidence_can_never_produce_approval(self):
        """Telemetry moves a deployment; it does not authorise one."""
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.record_health(self.deployment, healthy())
        self.assertEqual(caught.exception.code, "DEPLOYMENT_TRANSITION_INVALID")
        self.assertEqual(self.ledger.deployment(self.deployment)["state"],
                         "awaiting_approval")

    def test_no_state_in_the_machine_reaches_approved_except_approval(self):
        reaching = {source for source, targets in
                    production.ALLOWED_TRANSITIONS.items() if "approved" in targets}
        self.assertEqual(reaching, {"admitted", "awaiting_approval"})

    def test_an_approved_release_deploys_and_the_approval_is_on_the_record(self):
        self.ledger.approve(self.deployment, "owner", "signoff/1",
                            bundle().bundle_digest)
        self.assertEqual(self.ledger.deploy(self.deployment, self.port), "verifying")
        receipt = self.ledger.receipt(self.deployment)
        self.assertEqual(receipt["approved_by"], "owner")
        self.assertEqual(receipt["approval_ref"], "signoff/1")
        self.assertEqual(receipt["environment_class"], "production")


class UnattendedStagingTests(LedgerCase):
    """Staging and local simulation run without a person, when policy says so."""

    def test_a_declared_autonomous_environment_needs_no_approval(self):
        self.ledger.register_environment(staging())
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.assertEqual(self.ledger.deployment(deployment)["state"], "approved")
        self.assertEqual(self.ledger.deploy(deployment, self.port), "verifying")
        self.assertEqual(self.ledger.record_health(deployment, healthy()), "healthy")

    def test_a_staging_environment_left_gated_still_waits(self):
        """Autonomy is declared, not implied by the class."""
        self.ledger.register_environment(staging(autonomous=False))
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.assertEqual(self.ledger.deployment(deployment)["state"],
                         "awaiting_approval")

    def test_bounded_rollback_runs_unattended_after_a_failed_release(self):
        self.live()
        second = self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                           "shop-staging", "factory")
        self.ledger.deploy(second, self.port)
        self.assertEqual(self.ledger.record_health(second, unhealthy()), "failed")
        self.assertEqual(self.ledger.rollback(second, self.port), "recovered")
        self.assertEqual(self.ledger.receipt(second)["rollback_attempts"], 1)

    def test_rollback_is_bounded_by_declared_policy(self):
        self.ledger.register_environment(staging(max_rollback_attempts=0))
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, self.port)
        self.ledger.record_health(deployment, unhealthy())
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.rollback(deployment, self.port)
        self.assertEqual(caught.exception.code, "ROLLBACK_ATTEMPTS_EXHAUSTED")

    def test_rollback_with_no_healthy_predecessor_escalates_rather_than_guesses(self):
        self.ledger.register_environment(staging())
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, self.port)
        self.ledger.record_health(deployment, unhealthy())
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.rollback(deployment, self.port)
        self.assertEqual(caught.exception.code, "ROLLBACK_TARGET_UNKNOWN")
        self.assertEqual(self.ledger.escalate(deployment, "no target"), "escalated")

    def test_a_rollback_target_is_looked_up_and_never_built(self):
        first = self.live()
        second = self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                           "shop-staging", "factory")
        self.ledger.deploy(second, self.port)
        self.ledger.record_health(second, unhealthy())
        self.ledger.rollback(second, self.port)
        self.assertEqual(self.ledger.deployment(second)["rollback_of"], first)


class ExactlyOnceTests(LedgerCase):

    def setUp(self):
        super().setUp()
        self.ledger.register_environment(staging())

    def test_admitting_the_same_bundle_twice_yields_one_deployment(self):
        first = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        second = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.assertEqual(first, second)

    def test_a_second_deploy_call_cannot_mint_a_second_operation(self):
        """Exactly-once is the state machine, not a duplicate-key rescue."""
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, self.port)
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.deploy(deployment, self.port)
        self.assertEqual(caught.exception.code, "DEPLOYMENT_STATE_INVALID")
        self.assertEqual([call for call in self.port.calls if call[0] == "deploy"],
                         [("deploy", production.operation_key(deployment, "deploy", 0))])

    def test_no_transition_returns_a_deployment_to_a_state_a_claim_starts_from(self):
        """Why the above holds for every path, not only the one it walks."""
        returning = {source for source, targets in
                     production.ALLOWED_TRANSITIONS.items()
                     if "approved" in targets and source not in
                     ("admitted", "awaiting_approval")}
        self.assertEqual(returning, set())

    def test_an_adapter_that_cannot_say_produces_uncertain_not_a_guess(self):
        silent = production.DeterministicDeploymentAdapter(reached=None)
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.assertEqual(self.ledger.deploy(deployment, silent), "uncertain")

    def test_an_adapter_that_raises_produces_uncertain_not_a_failure(self):
        class Exploding:
            name = "exploding"

            def deploy(self, *args):
                raise RuntimeError("connection reset")

            def rollback(self, *args):
                raise RuntimeError("connection reset")

        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.assertEqual(self.ledger.deploy(deployment, Exploding()), "uncertain")

    def test_an_unresolved_uncertain_deployment_closes_the_environment(self):
        """The blind-duplicate criterion, stated as the refusal that prevents it."""
        silent = production.DeterministicDeploymentAdapter(reached=None)
        first = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(first, silent)
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                      "shop-staging", "factory")
        self.assertEqual(caught.exception.code, "DEPLOYMENT_UNCERTAIN_UNRESOLVED")
        self.assertEqual(caught.exception.deployment_id, first)

    def test_uncertain_is_left_only_by_reconciliation_against_an_observation(self):
        silent = production.DeterministicDeploymentAdapter(reached=None)
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, silent)
        with self.assertRaises(production.PolicyError):
            self.ledger.reconcile(deployment, "approved", "probe/9")
        self.assertEqual(self.ledger.reconcile(deployment, "healthy", "probe/9"),
                         "healthy")
        self.assertTrue(any(
            event["kind"] == "deployment_state"
            and json.loads(event["detail_json"]).get("evidence_ref") == "probe/9"
            for event in self.ledger.events("shop")))

    def test_a_restart_turns_every_in_flight_operation_into_uncertain(self):
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, self.port)
        self.assertEqual(self.ledger.deployment(deployment)["state"], "verifying")
        self.assertEqual(self.ledger.reconcile_on_restart(), (deployment,))
        self.assertEqual(self.ledger.deployment(deployment)["state"], "uncertain")
        # And a restart never re-dispatches: the adapter was called once.
        self.assertEqual(len(self.port.calls), 1)


class ContainmentTests(LedgerCase):

    def setUp(self):
        super().setUp()
        self.deployment = self.live()
        self.ledger.declare_incident(
            incident_ref="INC-1", environment_id="shop-staging",
            declared_by="owner", incident_class="outage",
            affected_release_sha=SHA, affected_bundle_ref="rc-001",
            failing_behaviour="checkout returns 500",
            blast_radius="all checkout traffic")

    def test_only_a_person_the_owner_listed_declares_an_incident(self):
        for actor in ("alerting", "factory", "an-adapter"):
            with self.assertRaises(production.ProductionRefusal) as caught:
                self.ledger.declare_incident(
                    incident_ref="INC-%s" % actor,
                    environment_id="shop-staging", declared_by=actor,
                    incident_class="outage", affected_release_sha=SHA,
                    affected_bundle_ref="rc-001", failing_behaviour="x",
                    blast_radius="y")
            self.assertEqual(caught.exception.code,
                             "INCIDENT_DECLARATION_UNAUTHORIZED")

    def test_an_incident_reference_is_never_reused(self):
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.declare_incident(
                incident_ref="INC-1", environment_id="shop-staging",
                declared_by="owner", incident_class="outage",
                affected_release_sha=SHA, affected_bundle_ref="rc-001",
                failing_behaviour="x", blast_radius="y")
        self.assertEqual(caught.exception.code, "INCIDENT_REF_REUSED")

    def test_a_blast_radius_statement_is_required(self):
        with self.assertRaises(production.PolicyError):
            self.ledger.declare_incident(
                incident_ref="INC-2", environment_id="shop-staging",
                declared_by="owner", incident_class="outage",
                affected_release_sha=SHA, affected_bundle_ref="rc-001",
                failing_behaviour="x", blast_radius="")

    def test_there_is_no_containment_action_that_changes_source(self):
        self.assertEqual(production.CONTAINMENT_ACTIONS,
                         ("rollback", "traffic_stop", "safe_stop"))
        for attempted in ("hotfix", "patch", "edit", "redeploy_from_source"):
            with self.assertRaises(production.PolicyError):
                self.ledger.contain("INC-1", attempted)

    def test_a_confirmed_defect_leaves_as_a_bug_mission_for_the_factory(self):
        self.ledger.contain("INC-1", "rollback")
        mission = self.ledger.route_defect("INC-1", "SF-200", "checkout 500s")
        self.assertEqual(mission["capability"], "bug")
        self.assertEqual(mission["baseline_sha"], SHA)
        self.assertEqual(mission["incident_ref"], "INC-1")
        self.assertEqual(mission["origin"], "production_incident")
        self.assertEqual(mission["repository"], "https://example.invalid/shop.git")

    def test_an_incident_is_closed_by_a_person_with_evidence(self):
        self.ledger.contain("INC-1", "rollback")
        with self.assertRaises(production.ProductionRefusal):
            self.ledger.close_incident("INC-1", "alerting", "probe/3")
        self.ledger.close_incident("INC-1", "owner", "probe/3")

    def test_a_release_and_its_incident_correlate_by_the_identity_both_carry(self):
        linked = self.ledger.correlate(SHA)
        self.assertEqual([row["incident_ref"] for row in linked["incidents"]], ["INC-1"])
        self.assertIn(self.deployment, [row["id"] for row in linked["deployments"]])

    def test_an_emergency_stop_refuses_admission_and_execution(self):
        self.ledger.emergency_stop("environment", environment_id="shop-staging")
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                      "shop-staging", "factory")
        self.assertEqual(caught.exception.code, "EMERGENCY_STOP_ENGAGED")

    def test_a_safe_stop_admits_nothing_new_and_disturbs_nothing_running(self):
        self.ledger.set_environment_state("shop-staging", "draining")
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                      "shop-staging", "factory")
        self.assertEqual(caught.exception.code, "ENVIRONMENT_NOT_ADMITTING")
        self.assertEqual(self.ledger.deployment(self.deployment)["state"], "healthy")


class MultiProjectTests(LedgerCase):
    """Two projects on one host, under concurrent release work."""

    def setUp(self):
        super().setUp()
        self.ledger.register_environment(staging())
        self.ledger.register_environment(production.EnvironmentPolicy(
            environment_id="mail-staging", project_id="mail",
            environment_class="staging", repository="https://example.invalid/mail.git",
            service_ref="mail-api", approver_refs=("owner",), autonomous=True,
            secret_refs=("mail.relay",)))

    def test_a_bundle_cannot_reach_another_projects_environment(self):
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(bundle(), "mail-staging", "factory")
        self.assertEqual(caught.exception.code, "ENVIRONMENT_PROJECT_MISMATCH")

    def test_a_bundle_cannot_reach_an_environment_bound_to_another_repository(self):
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(
                bundle(project_id="mail"), "mail-staging", "factory")
        self.assertEqual(caught.exception.code, "ENVIRONMENT_REPOSITORY_MISMATCH")

    def test_listing_environments_is_scoped_to_one_project(self):
        self.assertEqual([env.environment_id for env in self.ledger.environments("shop")],
                         ["shop-staging"])
        self.assertEqual([env.environment_id for env in self.ledger.environments("mail")],
                         ["mail-staging"])

    def test_one_projects_events_never_carry_another_projects_facts(self):
        self.live()
        self.ledger.admit_release(
            bundle(project_id="mail", repository="https://example.invalid/mail.git",
                   bundle_ref="rc-mail"), "mail-staging", "factory")
        shop = json.dumps([dict(event) for event in self.ledger.events("shop")])
        mail = json.dumps([dict(event) for event in self.ledger.events("mail")])
        self.assertNotIn("mail", shop)
        self.assertNotIn("shop", mail)

    def test_an_emergency_stop_reaches_exactly_its_declared_scope(self):
        self.assertEqual(self.ledger.emergency_stop("project", project_id="shop"),
                         ("shop-staging",))
        self.ledger.admit_release(
            bundle(project_id="mail", repository="https://example.invalid/mail.git"),
            "mail-staging", "factory")
        self.assertEqual(
            self.ledger.emergency_stop("portfolio"),
            ("mail-staging", "shop-staging"))

    def test_concurrency_is_counted_per_environment(self):
        self.ledger.register_environment(staging(deployment_concurrency=1))
        self.ledger.admit_release(bundle(), "shop-staging", "factory")
        with self.assertRaises(production.ProductionRefusal) as caught:
            self.ledger.admit_release(bundle(release_sha=OTHER_SHA),
                                      "shop-staging", "factory")
        self.assertEqual(caught.exception.code, "ENVIRONMENT_CONCURRENCY_EXCEEDED")
        # The other project is untouched by its neighbour's saturation.
        self.ledger.admit_release(
            bundle(project_id="mail", repository="https://example.invalid/mail.git"),
            "mail-staging", "factory")


class BoundaryTests(LedgerCase):
    """Facts that must never appear, and facts that must never be missing."""

    def test_a_receipt_spells_what_did_not_happen(self):
        self.ledger.register_environment(staging())
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        receipt = self.ledger.receipt(deployment)
        self.assertEqual(receipt["operation_ref"], "not_run")
        self.assertEqual(receipt["health_outcome"], "not_run")
        self.assertEqual(receipt["approved_by"], "not_applicable")
        self.assertEqual(receipt["rollback_of"], "not_applicable")
        for key in ("operation_ref", "health_outcome", "approved_by", "rollback_of"):
            self.assertIn(receipt[key], production.CANONICAL_ABSENCE)
        self.assertNotEqual(receipt["rollback_attempts"], "not_run")

    def test_the_absence_vocabulary_is_the_corpus_vocabulary_unchanged(self):
        from factory_controller import routing, store as store_module
        self.assertEqual(production.CANONICAL_ABSENCE, routing.CANONICAL_ABSENCE)
        self.assertEqual(production.CANONICAL_ABSENCE, store_module.CANONICAL_ABSENCE)

    def test_a_secret_value_cannot_reach_durable_state_through_any_field(self):
        planted = "sk-live-0123456789abcdef"
        with self.assertRaises(production.PolicyError):
            environment(secret_refs=(planted,))
        with self.assertRaises(production.PolicyError):
            bundle(env_schema={"API_TOKEN": {"type": "string", "value": planted}})
        # And there is no field on the bundle in which one could arrive: the
        # only surface that accepts a reference at all is registration.
        with self.assertRaises(production.PolicyError):
            bundle(secret_refs=[planted])
        self.ledger.register_environment(staging(secret_refs=("shop.database",)))
        deployment = self.ledger.admit_release(bundle(), "shop-staging", "factory")
        self.ledger.deploy(deployment, self.port)
        dumped = json.dumps([self.ledger.receipt(deployment),
                             self.ledger.deployment(deployment),
                             [dict(event) for event in self.ledger.events("shop")]])
        self.assertNotIn(planted, dumped)
        self.assertNotIn("sk-live", dumped)

    def test_an_slo_event_is_recorded_and_marked_as_no_authority(self):
        self.ledger.register_environment(staging())
        self.ledger.record_slo_event("shop-staging", "error_rate", 0.02, 5.0)
        events = [event for event in self.ledger.events("shop")
                  if event["kind"] == "slo_event"]
        self.assertEqual(len(events), 1)
        detail = json.loads(events[0]["detail_json"])
        self.assertEqual(detail["authority"], "not_applicable")
        self.assertIsNone(events[0]["deployment_id"])

    def test_the_event_log_is_append_only(self):
        import sqlite3
        self.ledger.register_environment(staging())
        with self.store.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM production_events")
        with self.store.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE production_events SET kind='x'")

    def test_the_ledger_shares_the_mission_store_rather_than_a_second_database(self):
        second = production.ProductionLedger(self.store)
        second.register_environment(staging())
        self.assertEqual(self.ledger.environment("shop-staging").project_id, "shop")


if __name__ == "__main__":
    unittest.main()
