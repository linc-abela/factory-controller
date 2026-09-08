"""Owner-to-app brief intake and frozen Owner-facing states."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import owner_app, pcp
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.engine import Controller
from factory_controller.store import MissionStore
from tests.test_factory_lifecycle import FakeHost, NoopAdapter, fake_context


INVENTORY_BRIEF = (
    "Build a small inventory tracker for a household or small shop. I need to "
    "add, edit, delete and search inventory items. Each item needs a name, "
    "quantity and optional note. My inventory must still be there after I "
    "refresh or reopen the app. Keep it simple and usable on desktop and "
    "mobile, and give me a REVIEW URL to test."
)


class OwnerAppFastPathTests(unittest.TestCase):
    def test_inventory_brief_becomes_an_accepted_package(self):
        accepted = owner_app.accept_brief(
            INVENTORY_BRIEF, created_at="2026-09-08T00:00:00Z")
        intake = pcp.intake(accepted.package)
        self.assertEqual(accepted.package_id, "household-inventory")
        self.assertEqual(intake.verdict, "ACCEPTED")
        self.assertEqual(intake.mission["work_item_id"], "household-inventory:build")
        self.assertEqual(accepted.owner_state, "Accepted / waiting")
        self.assertEqual(accepted.envelope.envelope_id, "phase21-browser-local-firebase")

    def test_payments_brief_is_refused_before_acceptance(self):
        with self.assertRaises(owner_app.BriefRefusal) as raised:
            owner_app.accept_brief(
                "Build a shop with Stripe checkout and user login so customers "
                "can pay and share one inventory.",
                created_at="2026-09-08T00:00:00Z")
        self.assertEqual(raised.exception.code, "OWNER_BRIEF_UNSUPPORTED")

    def test_owner_states_do_not_call_review_ready_released(self):
        working = owner_app.owner_state_for(mission_state="dispatching")
        ready = owner_app.owner_state_for(
            mission_state="completed", review_ready=True)
        released = owner_app.owner_state_for(
            mission_state="completed", review_ready=True, released=True)
        self.assertEqual(working["owner_state"], "Working")
        self.assertEqual(ready["owner_state"], "Ready for validation")
        self.assertEqual(released["owner_state"], "Released / stopped")
        stale = owner_app.owner_state_for(
            mission_state="dispatching", stale=True)
        self.assertEqual(stale["owner_state"], "Blocked")
        self.assertEqual(stale["freshness"], "stale")
        self.assertIn("stale", stale["next_action"].lower())

    def test_factory_brief_persists_status_without_owner_admission_files(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        base = FactoryConfig.default()
        from dataclasses import replace
        config = replace(
            base,
            agents_dir=root / "LaunchAgents",
            state_dir=root / "state",
            bridge_prefix=root / "bridge",
            bridge_root=root / "bridge-source",
        )
        (config.bridge_root / "contracts").mkdir(parents=True)
        host = FakeHost(config)
        store = MissionStore(root / "missions.sqlite")
        lifecycle = FactoryLifecycle(
            Controller(store, NoopAdapter()),
            config=config, runner=host, owner=OwnerIdentity(501, "owner"),
            reports={"evidence_core": {"status": "ACCEPTED", "identity": "e"},
                     "context_broker": {"status": "ok", "identity": "c"}},
            context_builder=fake_context,
        )
        result = lifecycle.brief(INVENTORY_BRIEF)
        self.assertTrue(result.ok)
        self.assertEqual(result.details["owner_state"], "Accepted / waiting")
        self.assertIn("Accepted:", result.lines[0])
        self.assertIn("mission/status link", result.lines[1])
        status = Path(result.details["status_link"])
        self.assertTrue(status.is_file())
        self.assertIn("Accepted / waiting", status.read_text())
        package = Path(result.details["package_path"])
        self.assertTrue(package.is_file())
        self.assertNotIn("lodus-casino", package.read_text())
        contract_path = Path(result.details["contract_path"])
        self.assertTrue(contract_path.is_file())
        from factory_controller import product
        loaded = product.ProductContract.load(contract_path)
        self.assertEqual(loaded.package_id, "household-inventory")
        self.assertTrue(loaded.run_ref.startswith("owner-brief-"))
        self.assertEqual(len(result.details["baseline_sha"]), 40)
        bootstrap = Path(result.details["bootstrap_path"])
        self.assertTrue((bootstrap / "evaluate.mjs").is_file())
        self.assertIn("NOT_IMPLEMENTED", (bootstrap / "public/inventory.mjs").read_text())
        self.assertTrue((bootstrap / "capability-admission-request.json").exists()
                        or (package.parent / "capability-admission-request.json").is_file())
        pointer = owner_app.active_contract_pointer(config.state_dir)
        self.assertTrue(pointer.is_file())


class EnvelopeScaffoldTests(unittest.TestCase):
    def test_follow_on_change_reuses_the_bound_package(self):
        from factory_controller import envelope_scaffold
        self.assertEqual(
            envelope_scaffold.follow_on_package_id(
                "Add a low-stock threshold to each item and a Low Stock filter "
                "so I can quickly see what needs restocking.",
                "household-inventory"),
            "household-inventory")
        self.assertIsNone(envelope_scaffold.follow_on_package_id(
            INVENTORY_BRIEF, "household-inventory"))

    def test_revision_package_names_the_reviewed_candidate(self):
        from factory_controller import envelope_scaffold
        accepted = owner_app.accept_brief(
            INVENTORY_BRIEF, created_at="2026-09-08T00:00:00Z")
        body = envelope_scaffold.revision_package(
            accepted.package,
            "Add a low-stock threshold to each item and a Low Stock filter.",
            created_at="2026-09-08T01:00:00Z",
            predecessor_rc="household-inventory-rc-1",
            predecessor_candidate_sha="a" * 40,
            owner_validation_id="ov-household-inventory-2")
        intake = pcp.intake(body)
        self.assertEqual(intake.mission["work_item_id"],
                         "household-inventory:revision:2")
        self.assertEqual(body["revision"]["owner_decision"], "RETURN_FOR_CHANGES")

    def test_envelope_review_selects_firebase_not_loopback(self):
        from factory_controller import envelope_scaffold, google_production, product
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        accepted = owner_app.accept_brief(
            INVENTORY_BRIEF, created_at="2026-09-08T00:00:00Z")
        body = owner_app.derived_contract(
            accepted.package_id, baseline_sha="a" * 40,
            run_ref="owner-brief-household-inventory-1",
            remote=envelope_scaffold.remote_url(accepted.package_id),
            provider_profiles=product.ProductContract.load(
                Path(__file__).resolve().parents[1] / "contracts" /
                "lodus-casino-product-run-contract.json").provider_profiles)
        path = root / "contract.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        contract = product.ProductContract.load(path)
        lifecycle = FactoryLifecycle(
            Controller(MissionStore(root / "missions.sqlite"), NoopAdapter()),
            config=FactoryConfig.default(),
            review_transport=google_production.SimulatedFirebaseTransport())
        adapter, url = lifecycle._review_port(contract)
        self.assertEqual(adapter.name, google_production.ADAPTER_NAME)
        self.assertEqual(url, "https://household-inventory-review.web.app")
        lodus = product.ProductContract.load(
            Path(__file__).resolve().parents[1] / "contracts" /
            "lodus-casino-product-run-contract.json")
        legacy, legacy_url = lifecycle._review_port(lodus)
        self.assertEqual(legacy_url, lifecycle.config.review_url)
        self.assertNotEqual(getattr(legacy, "name", ""), google_production.ADAPTER_NAME)


if __name__ == "__main__":
    unittest.main()
