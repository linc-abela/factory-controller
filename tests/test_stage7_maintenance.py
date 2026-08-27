"""Stage 7: what autonomous maintenance may and may not cause to happen.

Written the same way as `test_production_authority.py`: each test states the
thing that must remain impossible, and the happy paths exist to prove the
impossible ones are not impossible by accident.  A suite where every repair is
refused would pass every safety test here and be worthless, so the end-to-end
flow at the bottom runs a real repair all the way to a staged recovery.

The property this file exists to hold is narrow and load-bearing: **a repair
loop cannot run away.**  Four independent bounds land on the same terminal
disposition -- attempt ceiling, repeated-failure suppression, repair budget,
concurrency -- and none of them defers a repair to a later tick, because there
is no later tick.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from factory_controller import maintenance, portfolio, production
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import ALPHA, BETA, LayerAdapter
from tests.test_authority_boundaries import code_text

SHA = "a" * 40
OTHER_SHA = "b" * 40
PROJECT = "shop"
REPO = "https://example.invalid/shop.git"
GATES = ["G-BUILD"]
CANDIDATES = [{"profile": ALPHA, "capabilities": ["implement"]},
              {"profile": BETA, "capabilities": ["implement"]}]


def bundle_payload(**overrides):
    payload = {
        "bundle_ref": "rc-repair-001",
        "project_id": PROJECT,
        "repository": REPO,
        "release_sha": OTHER_SHA,
        "mission_ref": "SF-139",
        "evidence_refs": ["evidence/shop/SF-139.json"],
        "evaluator_receipts": ["receipts/evaluate.json"],
        "artifact": {"kind": "image", "identity": "sha256:" + "c" * 64},
        "env_schema": {"PORT": {"type": "integer", "required": True,
                                "description": "service port"}},
        "migration": {"forward_ref": "migrations/002.sql",
                      "reverse_ref": "migrations/002.down.sql"},
        "release_policy_version": "1.0",
        "provenance": {"built_by": "factory-controller",
                       "built_at": "2026-08-27T00:00:00Z",
                       "contract_version": production.CONTRACT_VERSION},
    }
    payload.update(overrides)
    return payload


def bundle(**overrides):
    return production.ReleaseBundle.from_payload(bundle_payload(**overrides))


def staging(project_id=PROJECT, environment_id="shop-staging", repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id, project_id=project_id,
        environment_class="staging", repository=repository,
        service_ref="shop-web", approver_refs=("owner",), autonomous=True)


def gated(project_id=PROJECT, environment_id="shop-prod", repository=REPO):
    return production.EnvironmentPolicy(
        environment_id=environment_id, project_id=project_id,
        environment_class="production", repository=repository,
        service_ref="shop-web", approver_refs=("owner", "deputy"))


def unhealthy():
    return production.HealthRecord(checks_passed=0, checks_failed=3,
                                   evidence_ref="probe/2", observed_at=2.0)


class PlaneCase(unittest.TestCase):
    """A store, a production ledger, a maintenance plane and a real Controller."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.db"
        self.store = MissionStore(str(self.path))
        self.ledger = production.ProductionLedger(self.store)
        self.plane = maintenance.MaintenancePlane(self.store, self.ledger)
        self.port = production.DeterministicDeploymentAdapter()
        self.register_project()

    def register_project(self, project_id=PROJECT, repository=REPO, state="enabled"):
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=project_id, repository=repository, state=state,
            concurrency_cap=4, policy_version="1.0"))

    def policy(self, **overrides):
        values = {"project_id": PROJECT, "enabled": True, "cooldown_seconds": 0,
                  "policy_version": "mp-1"}
        values.update(overrides)
        return self.plane.set_policy(maintenance.MaintenancePolicy(**values))

    def controller(self, adapter=None):
        return Controller(self.store, adapter or LayerAdapter(),
                          retry_policy=RetryPolicy(max_attempts=1,
                                                   base_delay_seconds=0),
                          lease_seconds=5)

    # -- production facts to trigger from -------------------------------- #

    def incident(self, incident_ref="INC-1", env=None, behaviour="checkout 500s"):
        env = env or staging()
        try:
            self.ledger.register_environment(env)
        except production.PolicyError:
            raise
        except Exception:                                          # noqa: BLE001
            pass
        self.ledger.declare_incident(
            incident_ref=incident_ref, environment_id=env.environment_id,
            declared_by="owner", incident_class="triaged_defect",
            affected_release_sha=SHA, affected_bundle_ref="rc-000",
            failing_behaviour=behaviour, blast_radius="all checkout traffic")
        return incident_ref

    def failed_deployment(self, env=None):
        """A deployment this ledger itself settled as failed."""
        env = env or staging()
        self.ledger.register_environment(env)
        adapter = production.DeterministicDeploymentAdapter(reached=False)
        deployment = self.ledger.admit_release(bundle(release_sha=SHA),
                                               env.environment_id, "factory")
        self.ledger.deploy(deployment, adapter)
        return deployment


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

class PolicyTests(unittest.TestCase):

    def test_a_production_class_cannot_be_scoped_for_autonomous_repair(self):
        with self.assertRaises(maintenance.PolicyError) as raised:
            maintenance.MaintenancePolicy(
                project_id=PROJECT,
                environment_classes=("staging", "production"))
        self.assertIn("approved by a person", str(raised.exception))

    def test_maintenance_is_off_until_the_owner_turns_it_on(self):
        self.assertFalse(maintenance.MaintenancePolicy(project_id=PROJECT).enabled)

    def test_an_empty_scope_is_refused_rather_than_admitting_everything(self):
        with self.assertRaises(maintenance.PolicyError):
            maintenance.MaintenancePolicy(project_id=PROJECT, environment_classes=())

    def test_an_attempt_ceiling_below_one_is_refused(self):
        with self.assertRaises(maintenance.PolicyError):
            maintenance.MaintenancePolicy(project_id=PROJECT, attempt_ceiling=0)

    def test_an_unknown_trigger_class_cannot_be_declared(self):
        with self.assertRaises(maintenance.PolicyError):
            maintenance.MaintenancePolicy(project_id=PROJECT,
                                          trigger_classes=("external_event",))

    def test_an_unknown_execution_mode_is_refused(self):
        with self.assertRaises(maintenance.PolicyError):
            maintenance.MaintenancePolicy(project_id=PROJECT, execution_mode="live")


class PolicyStorageTests(PlaneCase):

    def test_a_policy_round_trips_through_the_database(self):
        self.policy(repair_budget=3, attempt_ceiling=2, suppression_threshold=2)
        stored = self.plane.policy(PROJECT)
        self.assertEqual(stored.repair_budget, 3)
        self.assertEqual(stored.environment_classes, ("local-sim", "staging"))
        self.assertTrue(stored.enabled)

    def test_maintenance_can_be_paused_without_losing_the_envelope(self):
        self.policy(repair_budget=5)
        self.plane.set_enabled(PROJECT, False)
        stored = self.plane.policy(PROJECT)
        self.assertFalse(stored.enabled)
        self.assertEqual(stored.repair_budget, 5)


# --------------------------------------------------------------------------- #
# trigger admission: only a recorded production fact
# --------------------------------------------------------------------------- #

class TriggerAdmissionTests(PlaneCase):

    def test_a_declared_incident_opens_exactly_one_repair(self):
        self.policy()
        row = self.plane.admit_trigger("production_incident", self.incident())
        self.assertEqual(row["state"], "admitted")
        self.assertEqual(row["repository"], REPO)
        self.assertEqual(row["baseline_sha"], SHA)

    def test_a_deployment_this_ledger_settled_as_failed_opens_a_repair(self):
        self.policy()
        deployment = self.failed_deployment()
        row = self.plane.admit_trigger("deployment_health_failure", deployment)
        self.assertEqual(row["trigger_class"], "deployment_health_failure")
        self.assertEqual(row["baseline_sha"], SHA)

    def test_a_reference_to_nothing_is_refused(self):
        self.policy()
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", "INC-does-not-exist")
        self.assertEqual(raised.exception.code, "MAINTENANCE_SOURCE_NOT_RECORDED")

    def test_a_healthy_deployment_is_not_a_repair_trigger(self):
        self.policy()
        env = staging()
        self.ledger.register_environment(env)
        deployment = self.ledger.admit_release(bundle(release_sha=SHA),
                                               env.environment_id, "factory")
        self.ledger.deploy(deployment, self.port)
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("deployment_health_failure", deployment)
        self.assertEqual(raised.exception.code, "MAINTENANCE_SOURCE_NOT_REPAIRABLE")

    def test_an_uncertain_deployment_is_not_a_repair_trigger(self):
        """Nobody knows what happened, so nobody knows what to repair."""
        self.policy()
        env = staging()
        self.ledger.register_environment(env)
        deployment = self.ledger.admit_release(bundle(release_sha=SHA),
                                               env.environment_id, "factory")
        self.ledger.deploy(deployment,
                           production.DeterministicDeploymentAdapter(reached=None))
        self.assertEqual(self.ledger.deployment(deployment)["state"], "uncertain")
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("deployment_health_failure", deployment)
        self.assertEqual(raised.exception.code, "MAINTENANCE_SOURCE_NOT_REPAIRABLE")

    def test_a_trigger_class_outside_the_contract_is_refused(self):
        self.policy()
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("model_suggestion", "anything")
        self.assertEqual(raised.exception.code, "MAINTENANCE_TRIGGER_CLASS_UNKNOWN")

    def test_admission_takes_a_reference_and_nothing_a_model_could_write(self):
        """The refusal of free text is structural, not a validation rule.

        `admit_trigger` has exactly two parameters and neither of them can
        carry a sentence into the repair.  A prompt, an advisory diagnosis or a
        gateway payload has nowhere to go, which is why no test here needs to
        assert that one is rejected.
        """
        signature = inspect_signature(maintenance.MaintenancePlane.admit_trigger)
        self.assertEqual(signature, ["self", "trigger_class", "source_ref"])

    def test_maintenance_that_is_off_admits_nothing(self):
        self.policy(enabled=False)
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", self.incident())
        self.assertEqual(raised.exception.code, "MAINTENANCE_DISABLED")

    def test_a_project_with_no_policy_admits_nothing(self):
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", self.incident())
        self.assertEqual(raised.exception.code, "MAINTENANCE_DISABLED")

    def test_a_gated_environment_is_out_of_scope_even_when_it_fails(self):
        self.policy()
        incident = self.incident("INC-PROD", env=gated())
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", incident)
        self.assertEqual(raised.exception.code,
                         "MAINTENANCE_ENVIRONMENT_OUT_OF_SCOPE")

    def test_a_trigger_class_the_project_did_not_admit_is_refused(self):
        self.policy(trigger_classes=("production_incident",))
        deployment = self.failed_deployment()
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("deployment_health_failure", deployment)
        self.assertEqual(raised.exception.code,
                         "MAINTENANCE_TRIGGER_CLASS_NOT_ADMITTED")

    def test_a_paused_project_does_not_receive_autonomous_work(self):
        self.policy()
        incident = self.incident()
        self.store.set_project_state(PROJECT, "paused")
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", incident)
        self.assertEqual(raised.exception.code, "MAINTENANCE_PROJECT_NOT_ADMITTING")

    def test_an_emergency_stop_stops_maintenance_too(self):
        self.policy()
        incident = self.incident()
        self.store.emergency_stop(True)
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", incident)
        self.assertEqual(raised.exception.code, "MAINTENANCE_EMERGENCY_STOP")


# --------------------------------------------------------------------------- #
# idempotency and restart
# --------------------------------------------------------------------------- #

class IdempotencyTests(PlaneCase):

    def test_one_incident_admitted_twice_is_one_repair(self):
        self.policy()
        incident = self.incident()
        first = self.plane.admit_trigger("production_incident", incident)
        second = self.plane.admit_trigger("production_incident", incident)
        self.assertEqual(first["trigger_ref"], second["trigger_ref"])
        self.assertEqual(len(self.plane.repairs(PROJECT)), 1)

    def test_the_trigger_reference_is_derived_so_a_restart_recomputes_it(self):
        self.policy()
        incident = self.incident()
        row = self.plane.admit_trigger("production_incident", incident)
        self.assertEqual(row["trigger_ref"],
                         maintenance.trigger_reference("incidents", incident))

    def test_a_replay_after_a_restart_finds_the_same_repair(self):
        self.policy()
        incident = self.incident()
        first = self.plane.admit_trigger("production_incident", incident)
        reopened_store = MissionStore(str(self.path))
        reopened_ledger = production.ProductionLedger(reopened_store)
        reopened = maintenance.MaintenancePlane(reopened_store, reopened_ledger)
        again = reopened.admit_trigger("production_incident", incident)
        self.assertEqual(again["trigger_ref"], first["trigger_ref"])
        self.assertEqual(len(reopened.repairs(PROJECT)), 1)

    def test_a_repair_mission_is_submitted_once_across_a_restart(self):
        self.policy()
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        mission, created = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        self.assertTrue(created)

        reopened_store = MissionStore(str(self.path))
        reopened = maintenance.MaintenancePlane(
            reopened_store, production.ProductionLedger(reopened_store))
        again, created_again = reopened.create_repair_mission(
            trigger["trigger_ref"],
            Controller(reopened_store, LayerAdapter(),
                       retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0)),
            acceptance_gate_ids=GATES, provider_candidates=CANDIDATES)
        self.assertFalse(created_again)
        self.assertEqual(again["id"], mission["id"])
        self.assertEqual(self.store.counts().get("admitted", 0), 1)

    def test_a_crash_between_submission_and_the_record_does_not_duplicate(self):
        """The store's own idempotency key is the second, independent guard."""
        self.policy()
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        payload = self.plane.repair_payload(
            trigger["trigger_ref"], acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        key = self.plane.repair_idempotency_key(payload)
        direct, created = controller.submit(payload, key)   # the lost submission
        self.assertTrue(created)
        recovered, created_again = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        self.assertFalse(created_again)
        self.assertEqual(recovered["id"], direct["id"])

    def test_a_real_repair_derives_the_key_evidence_core_will_accept(self):
        self.policy(execution_mode="real")
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        payload = self.plane.repair_payload(
            trigger["trigger_ref"], acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES, context_manifest_hash="f" * 64)
        self.assertEqual(self.plane.repair_idempotency_key(payload),
                         "%s:%s" % (trigger["trigger_ref"], "f" * 64))


# --------------------------------------------------------------------------- #
# boundedness: four independent stops, none of them a delay
# --------------------------------------------------------------------------- #

class BoundedRepairTests(PlaneCase):

    def repeat(self, behaviour="checkout 500s", count=1, start=1):
        """Distinct incidents describing the identical unfixed failure."""
        refs = []
        for index in range(start, start + count):
            refs.append(self.incident("INC-%d" % index, behaviour=behaviour))
        return refs

    def test_the_attempt_ceiling_stops_a_failure_that_keeps_coming_back(self):
        self.policy(attempt_ceiling=2, suppression_threshold=9, concurrency=9)
        for index, ref in enumerate(self.repeat(count=3), start=1):
            if index <= 2:
                row = self.plane.admit_trigger("production_incident", ref)
                self.assertEqual(row["attempt"], index)
                self.plane.close(row["trigger_ref"], "escalated", reason="test")
            else:
                with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
                    self.plane.admit_trigger("production_incident", ref)
                self.assertEqual(raised.exception.code,
                                 "MAINTENANCE_ATTEMPT_CEILING_REACHED")

    def test_repeated_identical_failure_is_suppressed(self):
        self.policy(attempt_ceiling=9, suppression_threshold=2, concurrency=9)
        for index, ref in enumerate(self.repeat(count=3), start=1):
            if index <= 2:
                row = self.plane.admit_trigger("production_incident", ref)
                self.plane.close(row["trigger_ref"], "escalated", reason="test")
            else:
                with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
                    self.plane.admit_trigger("production_incident", ref)
                self.assertEqual(raised.exception.code,
                                 "MAINTENANCE_SIGNATURE_SUPPRESSED")

    def test_a_recovered_repair_does_not_count_towards_suppression(self):
        """Suppression is for failures that keep failing, not for busy projects."""
        self.policy(attempt_ceiling=9, suppression_threshold=1, concurrency=9)
        first, second = self.repeat(count=2)
        row = self.plane.admit_trigger("production_incident", first)
        self.plane.close(row["trigger_ref"], "recovered", reason="fixed")
        again = self.plane.admit_trigger("production_incident", second)
        self.assertEqual(again["attempt"], 2)

    def test_a_different_failure_is_not_suppressed_by_another_ones_history(self):
        self.policy(attempt_ceiling=1, suppression_threshold=1, concurrency=9)
        first = self.incident("INC-A", behaviour="checkout 500s")
        row = self.plane.admit_trigger("production_incident", first)
        self.plane.close(row["trigger_ref"], "escalated", reason="test")
        other = self.incident("INC-B", behaviour="search times out")
        admitted = self.plane.admit_trigger("production_incident", other)
        self.assertEqual(admitted["attempt"], 1)

    def test_the_repair_budget_is_spent_per_policy_version(self):
        self.policy(repair_budget=1, concurrency=9, attempt_ceiling=9,
                    suppression_threshold=9)
        first, second = self.repeat(count=2)
        row = self.plane.admit_trigger("production_incident", first)
        self.plane.close(row["trigger_ref"], "recovered", reason="fixed")
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", second)
        self.assertEqual(raised.exception.code, "MAINTENANCE_BUDGET_EXHAUSTED")

    def test_a_new_policy_version_is_the_only_thing_that_restores_a_budget(self):
        self.policy(repair_budget=1, concurrency=9, attempt_ceiling=9,
                    suppression_threshold=9)
        first, second = self.repeat(count=2)
        row = self.plane.admit_trigger("production_incident", first)
        self.plane.close(row["trigger_ref"], "recovered", reason="fixed")
        self.policy(repair_budget=1, concurrency=9, attempt_ceiling=9,
                    suppression_threshold=9, policy_version="mp-2")
        admitted = self.plane.admit_trigger("production_incident", second)
        self.assertEqual(admitted["policy_version"], "mp-2")

    def test_concurrency_bounds_how_many_repairs_are_open_at_once(self):
        self.policy(concurrency=1, attempt_ceiling=9, suppression_threshold=9)
        first, second = self.repeat(count=2)
        self.plane.admit_trigger("production_incident", first)
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", second)
        self.assertEqual(raised.exception.code, "MAINTENANCE_CONCURRENCY_EXCEEDED")

    def test_a_cooldown_holds_the_next_attempt_at_the_same_failure(self):
        self.policy(cooldown_seconds=3600, concurrency=9, attempt_ceiling=9,
                    suppression_threshold=9)
        first, second = self.repeat(count=2)
        row = self.plane.admit_trigger("production_incident", first)
        self.plane.close(row["trigger_ref"], "escalated", reason="test")
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", second)
        self.assertEqual(raised.exception.code, "MAINTENANCE_COOLDOWN_ACTIVE")

    def test_every_bound_ends_at_a_terminal_disposition_not_a_retry(self):
        """There is no code path in this module that schedules another attempt.

        A bound that deferred rather than stopped would be an unbounded loop
        with a delay in it, which is the failure this whole module is shaped to
        make unrepresentable.
        """
        source = (Path(maintenance.__file__)).read_text()
        tree = ast.parse(source)
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("admit_trigger", called)
        self.assertNotIn("create_repair_mission", called)
        self.assertNotIn("sleep", called)


# --------------------------------------------------------------------------- #
# the source-authority boundary
# --------------------------------------------------------------------------- #

class SourceAuthorityTests(PlaneCase):

    def test_a_repair_is_an_ordinary_mission_the_controller_admits(self):
        self.policy()
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        payload = self.plane.repair_payload(
            trigger["trigger_ref"], acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        # The same admission check every other mission passes, unmodified.
        self.assertEqual(Controller.validate(
            payload, self.plane.repair_idempotency_key(payload)), "fixture")
        mission, created = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        self.assertTrue(created)
        self.assertEqual(mission["state"], "admitted")
        self.assertEqual(mission["project_id"], PROJECT)

    def test_the_repair_payload_carries_the_ledgers_repository_not_a_callers(self):
        self.policy()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        payload = self.plane.repair_payload(
            trigger["trigger_ref"], acceptance_gate_ids=GATES)
        self.assertEqual(payload["repository"], REPO)
        self.assertEqual(payload["baseline_sha"], SHA)
        self.assertEqual(payload["origin"], "maintenance_trigger")

    def test_the_maintenance_plane_has_no_verb_that_changes_source(self):
        verbs = [name for name in dir(maintenance.MaintenancePlane)
                 if not name.startswith("_")]
        for forbidden in ("hotfix", "patch", "edit", "apply", "commit", "push",
                          "write_source", "approve", "deploy"):
            self.assertNotIn(forbidden, verbs)

    def test_maintenance_cannot_approve_its_own_recovery(self):
        self.assertFalse(hasattr(maintenance.MaintenancePlane, "approve"))

    def test_maintenance_never_stages_into_a_gated_environment(self):
        self.policy()
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        mission, _ = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        controller.work_once("w-1")
        self.plane.record_mission_outcome(trigger["trigger_ref"],
                                          self.store.get(mission["id"]))
        self.ledger.register_environment(gated())
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.stage_recovery(trigger["trigger_ref"], bundle(), "shop-prod")
        self.assertEqual(raised.exception.code,
                         "MAINTENANCE_PRODUCTION_AUTHORITY_REQUIRED")

    def test_an_unvalidated_candidate_never_reaches_an_environment(self):
        self.policy()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.stage_recovery(trigger["trigger_ref"], bundle(),
                                      "shop-staging")
        self.assertEqual(raised.exception.code,
                         "MAINTENANCE_CANDIDATE_UNVALIDATED")

    def test_the_module_never_reaches_an_advisory_service(self):
        """Deterministic maintenance must work with the advisor absent."""
        tree = ast.parse(Path(maintenance.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("advisor", imported)
        self.assertNotIn("gateway", imported)


# --------------------------------------------------------------------------- #
# multi-project isolation
# --------------------------------------------------------------------------- #

class MultiProjectTests(PlaneCase):

    OTHER = "warehouse"
    OTHER_REPO = "https://example.invalid/warehouse.git"

    def setUp(self):
        super().setUp()
        self.register_project(self.OTHER, self.OTHER_REPO)
        self.other_env = production.EnvironmentPolicy(
            environment_id="warehouse-staging", project_id=self.OTHER,
            environment_class="staging", repository=self.OTHER_REPO,
            service_ref="warehouse-api", approver_refs=("owner",), autonomous=True)

    def other_incident(self, ref="INC-W1"):
        self.ledger.register_environment(self.other_env)
        self.ledger.declare_incident(
            incident_ref=ref, environment_id="warehouse-staging",
            declared_by="owner", incident_class="triaged_defect",
            affected_release_sha=OTHER_SHA, affected_bundle_ref="rc-w0",
            failing_behaviour="picking API 500s", blast_radius="warehouse")
        return ref

    def test_one_projects_budget_does_not_bound_another(self):
        self.policy(repair_budget=1, concurrency=9)
        self.plane.set_policy(maintenance.MaintenancePolicy(
            project_id=self.OTHER, enabled=True, cooldown_seconds=0,
            repair_budget=1, concurrency=9, policy_version="mp-w1"))
        self.plane.admit_trigger("production_incident", self.incident())
        row = self.plane.admit_trigger("production_incident", self.other_incident())
        self.assertEqual(row["project_id"], self.OTHER)

    def test_one_projects_concurrency_does_not_bound_another(self):
        self.policy(concurrency=1)
        self.plane.set_policy(maintenance.MaintenancePolicy(
            project_id=self.OTHER, enabled=True, cooldown_seconds=0,
            concurrency=1, policy_version="mp-w1"))
        self.plane.admit_trigger("production_incident", self.incident())
        self.plane.admit_trigger("production_incident", self.other_incident())
        self.assertEqual(len(self.plane.repairs(PROJECT)), 1)
        self.assertEqual(len(self.plane.repairs(self.OTHER)), 1)

    def test_maintenance_enabled_for_one_project_does_not_enable_another(self):
        self.policy()
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.admit_trigger("production_incident", self.other_incident())
        self.assertEqual(raised.exception.code, "MAINTENANCE_DISABLED")

    def test_a_repair_cannot_be_staged_into_another_projects_environment(self):
        self.policy()
        self.plane.set_policy(maintenance.MaintenancePolicy(
            project_id=self.OTHER, enabled=True, cooldown_seconds=0,
            policy_version="mp-w1"))
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        mission, _ = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        controller.work_once("w-1")
        self.plane.record_mission_outcome(trigger["trigger_ref"],
                                          self.store.get(mission["id"]))
        self.ledger.register_environment(self.other_env)
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.stage_recovery(trigger["trigger_ref"], bundle(),
                                      "warehouse-staging")
        self.assertEqual(raised.exception.code, "MAINTENANCE_PROJECT_ISOLATION")

    def test_a_repairs_events_are_only_its_own_projects(self):
        self.policy()
        self.plane.set_policy(maintenance.MaintenancePolicy(
            project_id=self.OTHER, enabled=True, cooldown_seconds=0,
            policy_version="mp-w1"))
        self.plane.admit_trigger("production_incident", self.incident())
        self.plane.admit_trigger("production_incident", self.other_incident())
        for event in self.plane.events(PROJECT):
            self.assertEqual(event["project_id"], PROJECT)
        self.assertTrue(self.plane.events(self.OTHER))


# --------------------------------------------------------------------------- #
# lineage
# --------------------------------------------------------------------------- #

class LineageTests(PlaneCase):

    def test_an_unstarted_repair_records_absences_not_zeros(self):
        self.policy()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        lineage = self.plane.lineage(trigger["trigger_ref"])
        self.assertEqual(lineage["mission_ref"], "not_run")
        self.assertEqual(lineage["candidate_sha"], "not_run")
        self.assertEqual(lineage["bundle_ref"], "not_applicable")
        self.assertEqual(lineage["disposition"], "unknown")

    def test_every_absence_is_one_of_the_four_canonical_words(self):
        self.policy()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        lineage = self.plane.lineage(trigger["trigger_ref"])
        absences = {value for value in lineage.values()
                    if isinstance(value, str)
                    and value.startswith(("not_", "unknown"))}
        self.assertTrue(absences <= maintenance.CANONICAL_ABSENCE, absences)

    def test_a_mission_that_never_produced_a_candidate_records_not_run(self):
        self.policy()
        controller = self.controller(LayerAdapter(verified=False))
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        mission, _ = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        controller.work_once("w-1")
        lineage = self.plane.record_mission_outcome(
            trigger["trigger_ref"], self.store.get(mission["id"]))
        self.assertEqual(lineage["evaluator_result"], "not_run")
        self.assertNotEqual(lineage["state"], "candidate_validated")

    def test_lineage_names_the_originating_production_fact(self):
        self.policy()
        incident = self.incident()
        trigger = self.plane.admit_trigger("production_incident", incident)
        lineage = self.plane.lineage(trigger["trigger_ref"])
        self.assertEqual(lineage["source_ref"], incident)
        self.assertEqual(lineage["source_kind"], "incidents")
        self.assertEqual(lineage["environment_id"], "shop-staging")


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #

class EndToEndTests(PlaneCase):

    def run_repair(self, adapter=None, incident_ref="INC-1"):
        controller = self.controller(adapter)
        trigger = self.plane.admit_trigger("production_incident",
                                           self.incident(incident_ref))
        ref = trigger["trigger_ref"]
        mission, _ = self.plane.create_repair_mission(
            ref, controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        controller.work_once("maintenance-worker")
        lineage = self.plane.record_mission_outcome(ref, self.store.get(mission["id"]))
        return ref, lineage

    def test_incident_to_repair_to_staged_recovery_to_closure(self):
        self.policy()
        ref, lineage = self.run_repair()
        self.assertEqual(lineage["evaluator_result"], "passed")
        self.assertEqual(lineage["state"], "candidate_validated")
        self.assertNotEqual(lineage["candidate_sha"], "not_run")

        self.ledger.register_environment(staging())
        deployment = self.plane.stage_recovery(ref, bundle(), "shop-staging")
        # Ungated: the ledger admits it approved, and no person is involved.
        self.assertEqual(self.ledger.deployment(deployment)["state"], "approved")
        self.ledger.deploy(deployment, self.port)
        self.ledger.record_health(
            deployment, production.HealthRecord(checks_passed=3, checks_failed=0,
                                                evidence_ref="probe/ok",
                                                observed_at=3.0))
        self.assertEqual(self.ledger.deployment(deployment)["state"], "healthy")

        closed = self.plane.close(ref, "recovered", reason="staging is healthy",
                                  recovery_outcome="healthy")
        self.assertEqual(closed["disposition"], "recovered")
        self.assertEqual(closed["recovery_deployment_id"], deployment)
        self.assertEqual(closed["recovery_outcome"], "healthy")
        kinds = [event["kind"] for event in closed["transitions"]]
        self.assertEqual(kinds, ["trigger_admitted", "repair_mission_created",
                                 "repair_mission_outcome", "recovery_staged",
                                 "repair_closed"])

    def test_a_failed_repair_stops_safely_and_stages_nothing(self):
        self.policy()
        ref, lineage = self.run_repair(LayerAdapter(gates_pass=False))
        self.assertEqual(lineage["evaluator_result"], "failed")
        self.ledger.register_environment(staging())
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.stage_recovery(ref, bundle(), "shop-staging")
        self.assertEqual(raised.exception.code, "MAINTENANCE_CANDIDATE_UNVALIDATED")
        closed = self.plane.close(ref, "escalated", reason="acceptance gate failed")
        self.assertEqual(closed["disposition"], "escalated")
        self.assertEqual(closed["recovery_deployment_id"], "not_applicable")

    def test_a_closed_repair_cannot_be_reopened_or_restaged(self):
        self.policy()
        ref, _ = self.run_repair()
        self.plane.close(ref, "escalated", reason="operator stopped it")
        self.ledger.register_environment(staging())
        for call in (lambda: self.plane.close(ref, "recovered", reason="again"),
                     lambda: self.plane.stage_recovery(ref, bundle(), "shop-staging")):
            with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
                call()
            self.assertEqual(raised.exception.code, "MAINTENANCE_REPAIR_CLOSED")

    def test_staging_a_recovery_twice_admits_one_deployment(self):
        self.policy()
        ref, _ = self.run_repair()
        self.ledger.register_environment(staging())
        first = self.plane.stage_recovery(ref, bundle(), "shop-staging")
        second = self.plane.stage_recovery(ref, bundle(), "shop-staging")
        self.assertEqual(first, second)

    def test_a_restart_after_staging_recovers_the_same_lineage(self):
        self.policy()
        ref, _ = self.run_repair()
        self.ledger.register_environment(staging())
        deployment = self.plane.stage_recovery(ref, bundle(), "shop-staging")
        reopened_store = MissionStore(str(self.path))
        reopened = maintenance.MaintenancePlane(
            reopened_store, production.ProductionLedger(reopened_store))
        lineage = reopened.lineage(ref)
        self.assertEqual(lineage["recovery_deployment_id"], deployment)
        self.assertEqual(lineage["state"], "recovery_staged")


def inspect_signature(function) -> list[str]:
    import inspect
    return list(inspect.signature(function).parameters)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# the operator surface
# --------------------------------------------------------------------------- #

class MaintenanceCLITests(unittest.TestCase):
    """The Owner's own surface, including that it has no `run` verb.

    Every command is one act.  Nothing here starts something that keeps going
    after the command returns, which is the same property the module holds and
    is worth checking at the surface an operator actually types at.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "cli.db")
        self.out = []

    def run_cli(self, *argv):
        import contextlib
        import io
        import json
        from factory_controller.cli import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--db", self.db, *argv])
        text = buffer.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def prepare(self):
        self.run_cli("project", "register", "--id", PROJECT,
                     "--repository", REPO, "--policy-version", "1.0")
        self.run_cli("production", "env-register", "--environment", "shop-staging",
                     "--project", PROJECT, "--class", "staging",
                     "--repository", REPO, "--service", "shop-web",
                     "--approver", "owner", "--autonomous",
                     "--policy-version", "p1")
        self.run_cli("production", "incident", "--incident", "INC-1",
                     "--environment", "shop-staging", "--actor", "owner",
                     "--incident-class", "triaged_defect", "--release-sha", SHA,
                     "--ref", "rc-000", "--behaviour", "checkout 500s",
                     "--blast-radius", "all checkout traffic")

    def test_a_refusal_prints_its_code_and_exits_non_zero(self):
        self.prepare()
        code, result = self.run_cli("maintenance", "trigger", "--source", "INC-1")
        self.assertEqual(code, 2)
        self.assertEqual(result["refused"]["code"], "MAINTENANCE_DISABLED")

    def test_policy_then_trigger_then_repair_then_close(self):
        self.prepare()
        code, policy = self.run_cli("maintenance", "policy", "--project", PROJECT,
                                    "--env-class", "staging", "--cooldown", "0",
                                    "--policy-version", "mp-1")
        self.assertEqual((code, policy["enabled"]), (0, True))
        code, trigger = self.run_cli("maintenance", "trigger", "--source", "INC-1")
        self.assertEqual(code, 0)
        ref = trigger["trigger_ref"]

        code, again = self.run_cli("maintenance", "trigger", "--source", "INC-1")
        self.assertEqual(again["trigger_ref"], ref)
        _, listed = self.run_cli("maintenance", "list", "--project", PROJECT)
        self.assertEqual(len(listed), 1)

        code, repair = self.run_cli("maintenance", "repair", "--trigger", ref,
                                    "--gate", "G-BUILD")
        self.assertEqual((code, repair["created"]), (0, True))
        _, repeat = self.run_cli("maintenance", "repair", "--trigger", ref,
                                 "--gate", "G-BUILD")
        self.assertFalse(repeat["created"])

        _, lineage = self.run_cli("maintenance", "lineage", "--trigger", ref)
        self.assertEqual(lineage["candidate_sha"], "not_run")
        code, closed = self.run_cli("maintenance", "close", "--trigger", ref,
                                    "--disposition", "escalated",
                                    "--reason", "operator stopped it")
        self.assertEqual((code, closed["disposition"]), (0, "escalated"))
        code, refused = self.run_cli("maintenance", "close", "--trigger", ref,
                                     "--disposition", "recovered")
        self.assertEqual(code, 2)
        self.assertEqual(refused["refused"]["code"], "MAINTENANCE_REPAIR_CLOSED")

    def test_a_production_class_cannot_be_scoped_from_the_command_line(self):
        self.prepare()
        code, result = self.run_cli("maintenance", "policy", "--project", PROJECT,
                                    "--env-class", "production",
                                    "--policy-version", "mp-1")
        self.assertEqual(code, 2)
        self.assertEqual(result["refused"]["code"], "MAINTENANCE_POLICY_INVALID")

    def test_the_operator_surface_offers_nothing_that_keeps_running(self):
        from factory_controller.cli import parser
        actions = [action for action in parser()._subparsers._group_actions[0]
                   .choices["maintenance"]._actions if action.dest == "action"]
        self.assertEqual(len(actions), 1)
        for forbidden in ("run", "worker", "loop", "watch", "start"):
            self.assertNotIn(forbidden, actions[0].choices)


# --------------------------------------------------------------------------- #
# what maintenance reuses rather than restates
# --------------------------------------------------------------------------- #

class Stage5ReuseTests(PlaneCase):
    """A repair is an ordinary mission, so Stage 5 governs it unmodified.

    These are the tests that would fail if maintenance had grown a scheduler of
    its own.  Nothing in `maintenance.py` mentions fairness, dependencies,
    budgets, drain or emergency stop; the portfolio scheduler already inside
    `MissionStore.claim` does all of it, and these prove the repair mission is
    genuinely subject to it rather than routed around it.
    """

    OTHER = "warehouse"
    OTHER_REPO = "https://example.invalid/warehouse.git"

    def open_repair(self, controller):
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        mission, _ = self.plane.create_repair_mission(
            trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        return trigger["trigger_ref"], mission

    def test_a_project_paused_after_admission_still_stops_the_repair_running(self):
        self.policy()
        controller = self.controller()
        _, mission = self.open_repair(controller)
        self.store.set_project_state(PROJECT, "paused")
        self.assertIsNone(controller.work_once("w-1"))
        self.assertEqual(self.store.get(mission["id"])["state"], "admitted")

    def test_an_emergency_stop_after_admission_stops_the_repair_running(self):
        self.policy()
        controller = self.controller()
        _, mission = self.open_repair(controller)
        self.store.emergency_stop(True)
        self.assertIsNone(controller.work_once("w-1"))
        self.assertEqual(self.store.get(mission["id"])["state"], "admitted")

    def test_a_repair_mission_carries_its_own_project_into_the_scheduler(self):
        self.policy()
        controller = self.controller()
        _, mission = self.open_repair(controller)
        self.assertEqual(self.store.get(mission["id"])["project_id"], PROJECT)
        decision = self.store.schedule_preview()
        self.assertEqual(decision["selected"], mission["id"])

    def test_two_projects_repairs_are_scheduled_independently(self):
        self.policy()
        self.register_project(self.OTHER, self.OTHER_REPO)
        self.plane.set_policy(maintenance.MaintenancePolicy(
            project_id=self.OTHER, enabled=True, cooldown_seconds=0,
            policy_version="mp-w1"))
        other_env = production.EnvironmentPolicy(
            environment_id="warehouse-staging", project_id=self.OTHER,
            environment_class="staging", repository=self.OTHER_REPO,
            service_ref="warehouse-api", approver_refs=("owner",), autonomous=True)
        self.ledger.register_environment(other_env)
        self.ledger.declare_incident(
            incident_ref="INC-W1", environment_id="warehouse-staging",
            declared_by="owner", incident_class="triaged_defect",
            affected_release_sha=OTHER_SHA, affected_bundle_ref="rc-w0",
            failing_behaviour="picking API 500s", blast_radius="warehouse")
        controller = self.controller()
        self.open_repair(controller)
        other = self.plane.admit_trigger("production_incident", "INC-W1")
        self.plane.create_repair_mission(
            other["trigger_ref"], controller, acceptance_gate_ids=GATES,
            provider_candidates=CANDIDATES)
        while controller.work_once("w-1") is not None:
            pass
        self.assertEqual(self.store.counts().get("completed"), 2)
        for row in self.plane.repairs():
            self.assertEqual(self.store.get(row["mission_ref"])["project_id"],
                             row["project_id"])

    def test_a_closed_repair_submits_no_mission(self):
        self.policy()
        controller = self.controller()
        trigger = self.plane.admit_trigger("production_incident", self.incident())
        self.plane.close(trigger["trigger_ref"], "abandoned", reason="operator")
        with self.assertRaises(maintenance.MaintenanceRefusal) as raised:
            self.plane.create_repair_mission(
                trigger["trigger_ref"], controller, acceptance_gate_ids=GATES,
                provider_candidates=CANDIDATES)
        self.assertEqual(raised.exception.code, "MAINTENANCE_REPAIR_CLOSED")
        self.assertEqual(self.store.counts(), {})

    def test_maintenance_names_no_runtime_and_no_model(self):
        """Logical roles stay provider-agnostic; the palette is the caller's.

        `provider_candidates` is an argument, never a value this module holds,
        so a repair runs on whatever the Owner's palette offers and the module
        works unchanged when a runtime is added or removed.

        Scanned as *code* rather than as text, with the package's own scanner:
        a docstring here says what the module refuses to contain, and a check
        that cannot tell that apart from a real identifier is a check that
        punishes the module for documenting its own boundary.
        """
        text = code_text(Path(maintenance.__file__).read_text())
        for token in ("openrouter", "gateway", "advisor", "model",
                      "provider_profile", "profile_id"):
            self.assertNotIn(token, text, "maintenance.py names %r" % token)

    def test_that_scan_would_actually_catch_a_runtime_reaching_the_module(self):
        planted = code_text("def go(profile_id):\n    return profile_id\n")
        self.assertIn("profile_id", planted)
