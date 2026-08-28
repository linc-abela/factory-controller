"""CLI tests for factory-controller commands."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller.cli import main
from factory_controller import capacity, continuity
from factory_controller.store import MissionStore


class ControllerCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "cli_test.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_harness_command(self) -> None:
        rc = main(["--db", self.db_path, "harness", "--missions", "5"])
        self.assertEqual(rc, 0)
        store = MissionStore(self.db_path)
        counts = store.counts()
        self.assertEqual(counts.get("completed"), 5)

    def test_submit_and_work_once_lifecycle(self) -> None:
        # 1. Submit via JSON file
        file_path = Path(self.temp_dir.name) / "payload.json"
        file_path.write_text(json.dumps(
            {"task": "cli-test", "acceptance_gate_ids": ["cli-suite"]}))
        rc_submit = main(["--db", self.db_path, "submit", "--key", "cli-key-1", "--file", str(file_path)])
        self.assertEqual(rc_submit, 0)

        # 2. Status counts
        store = MissionStore(self.db_path)
        self.assertEqual(store.counts().get("admitted"), 1)

        # 3. Work once
        rc_work = main(["--db", self.db_path, "work-once", "--worker", "cli-worker"])
        self.assertEqual(rc_work, 0)
        self.assertEqual(store.counts().get("completed"), 1)

    def test_operator_can_inspect_durable_work_batons(self) -> None:
        observation = capacity.CapacityObservation(
            runtime_id="codex-primary", state="exhausted", observed_at=100,
            source="test", source_ref="test-observation",
            expected_reset_at=18100).as_row()
        baton = continuity.issue_payload(
            source="https://example.invalid/project.git", head_sha="a" * 40,
            project_id="project-a", run_id="run-a", lane_id="lane-a",
            worktree="/tmp/disposable-a", branch="factory/run-a",
            safe_boundary="pre_dispatch", idempotency_key="run-a:leg-1",
            required_capabilities=["prototype"],
            compatible_profiles=["codex-primary"],
            capacity_observation=observation, evaluator="test",
            uncertainty={"irreversible_effect": "none"}, issued_at=100)
        continuity.WorkBatonStore(self.db_path).issue(baton)
        self.assertEqual(main(["--db", self.db_path, "baton", "inspect",
                               "--baton-id", baton["baton_id"]]), 0)


class ProductionCLITests(unittest.TestCase):
    """The Owner's surface, exercised as an operator would during an incident."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "production.db")
        self.bundle_path = Path(self.temp_dir.name) / "bundle.json"
        self.bundle_path.write_text(json.dumps({
            "bundle_ref": "rc-cli", "project_id": "shop",
            "repository": "https://example.invalid/shop.git",
            "release_sha": "a" * 40, "mission_ref": "SF-138",
            "evidence_refs": ["evidence/cli.json"],
            "evaluator_receipts": ["receipts/cli.json"],
            "artifact": "not_applicable",
            "env_schema": {"PORT": {"type": "integer", "required": True}},
            "migration": {"forward_ref": "not_applicable",
                          "reverse_ref": "not_applicable"},
            "release_policy_version": "1.0",
            "provenance": {"built_by": "cli", "built_at": "2026-08-27T00:00:00Z",
                           "contract_version": "factory-controller/production/1.0"},
        }))

    def run_cli(self, *argv) -> int:
        return main(["--db", self.db_path, "production", *argv])

    def register(self, environment_class="production", *extra):
        argv = ["env-register", "--environment", "shop-prod", "--project", "shop",
                "--class", environment_class, "--repository",
                "https://example.invalid/shop.git", "--service", "shop-web",
                "--approver", "owner", *extra]
        return self.run_cli(*argv)

    def admit(self):
        return self.run_cli("admit", "--environment", "shop-prod",
                            "--actor", "factory", "--bundle", str(self.bundle_path))

    def deployment_id(self):
        from factory_controller import production
        from factory_controller.store import MissionStore as Store
        ledger = production.ProductionLedger(Store(self.db_path))
        return ledger.events("shop")[-1]["deployment_id"]

    def test_a_production_environment_cannot_be_registered_as_autonomous(self):
        self.assertEqual(self.register("production", "--autonomous"), 1)

    def test_the_cli_cannot_deploy_to_production_without_an_approval(self):
        self.assertEqual(self.register(), 0)
        self.assertEqual(self.admit(), 0)
        deployment = self.deployment_id()
        self.assertEqual(self.run_cli("deploy", "--deployment", deployment), 1)

    def test_an_approved_release_deploys_and_the_receipt_records_who(self):
        self.assertEqual(self.register(), 0)
        self.assertEqual(self.admit(), 0)
        deployment = self.deployment_id()
        from factory_controller import production
        digest = production.ReleaseBundle.from_payload(
            json.loads(self.bundle_path.read_text())).bundle_digest
        self.assertEqual(self.run_cli("approve", "--deployment", deployment,
                                      "--actor", "owner", "--ref", "signoff/cli",
                                      "--digest", digest), 0)
        self.assertEqual(self.run_cli("deploy", "--deployment", deployment), 0)
        self.assertEqual(self.run_cli("receipt", "--deployment", deployment), 0)

    def test_an_emergency_stop_from_the_cli_closes_the_environment(self):
        self.assertEqual(self.register("staging", "--autonomous"), 0)
        self.assertEqual(self.run_cli("stop", "--scope", "environment",
                                      "--environment", "shop-prod"), 0)
        self.assertEqual(self.admit(), 1)


if __name__ == "__main__":
    unittest.main()
