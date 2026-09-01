"""`./dev factory product` — the one verb where the Owner names the work.

The dogfood verb's promise is that the Owner names nothing.  This one's is the
opposite, so what is checked here is that the package the Owner named is the
only source of intent, that submitting is an act with a record over it, and
that the widening it performs is exactly one profile and one project.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from factory_controller import pcp, product
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.engine import Controller
from factory_controller.store import MissionStore

from tests.test_factory_lifecycle import (
    FakeHost, NoopAdapter, PROTOTYPE_SHA, BUG_SHA, fake_context,
)


CONTRACT = product.ProductContract.load(
    Path(__file__).resolve().parent.parent / "contracts"
    / "lodus-casino-product-run-contract.json")
CHECKOUT = "/products/lodus-casino"


class FactoryProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        base = FactoryConfig.default()
        bridge_root = root / "bridge-source"
        (bridge_root / "contracts").mkdir(parents=True)
        self.config = replace(
            base,
            agents_dir=root / "LaunchAgents",
            state_dir=root / "state",
            bridge_prefix=root / "bridge",
            bridge_root=bridge_root,
            capability_request_path=root / "dogfood-capability.json",
        )
        self.config.capability_request_path.write_text(json.dumps({
            "accepted_unknowns": [], "capability": "bug",
            "policy_ref": "vault://a#5", "profiles": ["codex-primary"],
            "projects": ["factory-bug-lab", "factory-prototype-lab"],
            "request_ref": "SF-145A-bug-capability",
            "schema_version": "factory.bridge.capability_admission_request.v1",
        }))
        (bridge_root / "contracts" / CONTRACT.capability_request).write_text(
            json.dumps({
                "accepted_unknowns": [], "capability": "development",
                "policy_ref": "vault://active/projects/lodus-casino/overview.md",
                "profiles": list(CONTRACT.provider_profiles),
                "projects": list(CONTRACT.projects),
                "request_ref": "SF-151A-development-capability",
                "schema_version":
                    "factory.bridge.capability_admission_request.v1",
            }))

        self.package_path = root / "pcp-v1.1.0.json"
        self.package_path.write_text(json.dumps(pcp.materialize_casino_pcp()))
        self.intake = pcp.intake(pcp.materialize_casino_pcp())

        self.host = FakeHost(self.config)
        self.host.checkouts["lodus-casino"] = CHECKOUT
        self.host.extra_projects = [{
            "project_id": "lodus-casino",
            "repository_remote_url":
                "https://github.com/linc-abela/lodus-casino.git",
            "resolution": "resolved", "capabilities": ["development"],
            "checkout": CHECKOUT,
        }]
        self.host.extra_profiles = [{
            "profile_id": "codex-product", "status": "available",
            "readiness": "available",
        }]
        self.lifecycle = FactoryLifecycle(
            Controller(MissionStore(root / "controller.db"), NoopAdapter()),
            config=self.config, runner=self.host,
            owner=OwnerIdentity(501, "owner"),
            remote_reachability={
                "factory-prototype-lab": (PROTOTYPE_SHA,),
                "factory-bug-lab": (BUG_SHA,),
                "lodus-casino": (CONTRACT.baseline_sha,),
            },
            reports={"evidence_core": {"status": "ACCEPTED", "identity": "e"},
                     "context_broker": {"status": "ok", "identity": "c"}},
            context_builder=fake_context)

    def ready(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        started = self.lifecycle.dispatch("start")
        self.assertTrue(started.ok, started.render())

    def submit(self, package=None):
        return self.lifecycle.dispatch(
            "product", package=str(package or self.package_path))

    def missions(self):
        store = self.lifecycle.store
        return [store.get(row["id"]) for row in store.all_missions()]

    def owner_actions(self):
        return [row for row in self.lifecycle.store.coordination(None, limit=100)
                if row.get("reason") == "FACTORY_OWNER_ACTION"]

    # -- refusals, and what they leave behind ---------------------------- #

    def test_a_product_with_no_package_named_refuses_and_submits_nothing(self):
        self.ready()
        result = self.lifecycle.dispatch("product")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_PACKAGE_REQUIRED")
        self.assertEqual(self.missions(), [])

    def test_a_product_from_a_stopped_factory_refuses_before_any_record(self):
        """No Owner act, no admission, no mission: nothing happened at all."""

        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "FACTORY_NOT_READY")
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.owner_actions(), [])
        self.assertEqual(self.host.capability_admits, 0)

    def test_a_package_for_another_product_refuses_and_submits_nothing(self):
        self.ready()
        other = pcp.materialize_casino_pcp()
        other["package_id"] = "some-other-product"
        path = Path(self.temp.name) / "other.json"
        path.write_text(json.dumps(other))
        result = self.submit(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_PACKAGE_MISMATCH")
        self.assertEqual(self.missions(), [])

    def test_a_package_that_is_not_a_package_refuses_with_its_own_code(self):
        self.ready()
        path = Path(self.temp.name) / "not-a-package.json"
        path.write_text(json.dumps({"package_id": "lodus-casino"}))
        result = self.submit(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PCP_REQUIRED_FIELD_MISSING")

    def test_an_unreadable_package_refuses_rather_than_defaulting(self):
        self.ready()
        result = self.submit(Path(self.temp.name) / "absent.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PCP_UNREADABLE")

    def test_a_product_without_containment_refuses(self):
        self.ready()
        self.host.containment = False
        result = self.submit()
        self.assertFalse(result.ok)
        self.assertIn("containment", result.render().lower())
        self.assertEqual(self.missions(), [])

    # -- the submission itself -------------------------------------------- #

    def test_the_owner_submission_admits_exactly_one_product_mission(self):
        self.ready()
        result = self.submit()

        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.state, "submitted")
        self.assertEqual(result.details["work_item_id"], "lodus-casino:build")
        self.assertEqual(result.details["package_digest"],
                         self.intake.package_digest)
        self.assertEqual(result.details["baseline_sha"], CONTRACT.baseline_sha)

        missions = self.missions()
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0]["payload"]["work_item_id"],
                         "lodus-casino:build")
        self.assertEqual(missions[0]["project_id"], "lodus-casino")
        self.assertEqual(missions[0]["state"], "admitted")

    def test_the_mission_carries_the_product_capability_and_its_own_gates(self):
        self.ready()
        self.submit()
        payload = self.missions()[0]["payload"]
        self.assertEqual(payload["capability"], "development")
        self.assertEqual(payload["acceptance_gate_ids"],
                         list(CONTRACT.acceptance_gate_ids))
        self.assertTrue(payload["stage1"]["mutates_repository"])
        self.assertEqual(payload["stage1"]["gate_commands"]["dev-evaluate"],
                         [CHECKOUT + "/dev", "evaluate"])
        self.assertEqual([entry["profile"]
                          for entry in payload["provider_candidates"]],
                         list(CONTRACT.provider_profiles))

    def test_the_provider_is_told_which_package_and_which_gate(self):
        """MISSION.md is the instruction; the brief is a bounded pointer."""

        self.ready()
        self.submit()
        brief = self.missions()[0]["payload"]["stage1"]["mission_brief"]
        self.assertIn("lodus-casino@v1", brief)
        self.assertIn("./dev evaluate", brief)
        self.assertLessEqual(len(brief), product.BRIEF_LIMIT)

    def test_the_corpus_identity_is_the_submitted_package_bytes(self):
        self.ready()
        self.submit()
        admission = json.loads(
            (self.config.mission_dir / "lodus-casino-build-admission.json")
            .read_text())
        manifest = admission["admission_evidence"]["context_manifest"]
        self.assertEqual(manifest["corpus_identity"],
                         "package://lodus-casino@v1@" + self.intake.package_digest)

    def test_the_owner_act_is_recorded_with_a_hash_over_it(self):
        self.ready()
        result = self.submit()

        recorded = json.loads(
            (self.config.mission_dir / "lodus-casino-build-owner-intake.json")
            .read_text())
        self.assertEqual(recorded["chosen_action"], "submit")
        self.assertEqual(recorded["evidence_class"], "human_authority")
        self.assertEqual(recorded["owner"], "owner")
        self.assertEqual(recorded["package_digest"], self.intake.package_digest)
        self.assertEqual(recorded["act_hash"], result.details["owner_act_hash"])

        actions = [row for row in self.owner_actions()
                   if row["detail"].get("action") == "product"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["detail"]["package_digest"],
                         self.intake.package_digest)

    def test_submitting_the_same_package_twice_admits_one_mission(self):
        self.ready()
        first = self.submit()
        second = self.submit()

        self.assertTrue(second.ok, second.render())
        self.assertEqual(second.state, "already-submitted")
        self.assertEqual(second.details["idempotency_key"],
                         first.details["idempotency_key"])
        self.assertEqual(len(self.missions()), 1)

    # -- the widening ------------------------------------------------------ #

    def test_the_submission_admits_development_and_nothing_wider(self):
        self.ready()
        admits_before = self.host.capability_admits
        self.submit()

        requests = [json.loads(text) for command, text in self.host.calls
                    if text and command[-3:-1] == ("capability", "admit")]
        self.assertEqual(self.host.capability_admits, admits_before + 1)
        self.assertEqual(requests[-1]["capability"], "development")
        self.assertEqual(requests[-1]["profiles"], ["codex-product"])
        self.assertEqual(requests[-1]["projects"], ["lodus-casino"])
        self.assertEqual(requests[-1]["authorized_by"], "owner")

    def test_a_widened_request_file_cannot_widen_past_the_contract(self):
        """The contract, not the file, bounds what an admission may name."""

        self.ready()
        path = (self.config.bridge_root / "contracts"
                / CONTRACT.capability_request)
        body = json.loads(path.read_text())
        body["projects"] = ["lodus-casino", "factory-bug-lab"]
        path.write_text(json.dumps(body))

        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "CAPABILITY_SCOPE_INVALID")
        self.assertEqual(self.missions(), [])

    def test_the_product_project_is_registered_with_its_declared_gates(self):
        self.ready()
        self.submit()
        policy = self.lifecycle.store.project("lodus-casino")
        self.assertEqual(policy.acceptance_gate_ids,
                         CONTRACT.acceptance_gate_ids)
        self.assertEqual(policy.acceptance_gate_source,
                         CONTRACT.acceptance_gate_source)
        self.assertEqual(policy.policy_version, CONTRACT.run_ref)

    def test_no_improvement_may_be_opened_over_a_product_first_build(self):
        """A second admitter would promote a candidate that does not exist."""

        self.ready()
        self.submit()
        supervisor_policy = self.lifecycle.supervisor.policy("lodus-casino")
        self.assertEqual(supervisor_policy.improvement_admissions, 0)
        self.assertEqual(supervisor_policy.work_classes, (CONTRACT.work_class,))


if __name__ == "__main__":
    unittest.main()
