"""Hermes manager identity binds to a frozen Bridge fleet profile."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory_controller import advisor
from factory_controller.advisor import FROZEN_BRIDGE_DEPENDENCY_SHA
from tests.test_management_loop import Case


def _write_hermes(root: Path) -> Path:
    path = root / "hermes"
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then "
        "echo 'Hermes Agent v0.21.0 (test)'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _git_init(root: Path) -> str:
    env = {"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/local/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "providers.json"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "registry"], cwd=root, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
        text=True, env=env)
    return sha.stdout.strip()


class HermesFleetTests(Case):
    def fake_hermes(self) -> Path:
        return _write_hermes(Path(self.tmp.name))

    def registry(self, profile="codex-luna-max") -> tuple[Path, str]:
        root = Path(self.tmp.name) / "bridge"
        root.mkdir()
        (root / "providers.json").write_text(json.dumps({
            "schema_version": "factory.bridge.provider_profiles.v4",
            "profiles": {
                profile: {
                    "name": "codex",
                    "provider": "codex",
                    "harness": "codex",
                    "model": "gpt-5.6-luna",
                    "effort": "max",
                    "executable": "/bin/echo",
                    "argv": ["ok"],
                    "capabilities": ["prototype"],
                    "timeout_seconds": 10,
                    "priority": 1,
                }
            },
        }), encoding="utf-8")
        return root, _git_init(root)

    def test_missing_bridge_root_is_an_adapter_block(self):
        port = advisor.HermesProcessAdvisor(
            str(self.fake_hermes()), requested_profile="fleet-manager")
        report = self.cycle(manager=port)
        self.assertEqual(report["reason"], "MANAGER_PROVIDER_ADAPTER_BLOCKED")
        self.assertEqual(report["next_action"], "WAIT_MANAGER")

    def test_wrong_bridge_sha_is_an_adapter_block(self):
        root, _sha = self.registry()
        port = advisor.HermesProcessAdvisor(
            str(self.fake_hermes()), requested_profile="fleet-manager",
            bridge_root=root, expected_bridge_sha=FROZEN_BRIDGE_DEPENDENCY_SHA)
        report = self.cycle(manager=port)
        self.assertEqual(report["reason"], "MANAGER_PROVIDER_ADAPTER_BLOCKED")

    def test_default_process_advisor_pins_admitted_bridge_not_obsolete_candidate(self):
        root, sha = self.registry()
        self.assertNotEqual(sha, FROZEN_BRIDGE_DEPENDENCY_SHA)
        port = advisor.HermesProcessAdvisor(
            str(self.fake_hermes()), requested_profile="fleet-manager",
            bridge_root=root)
        self.assertEqual(
            port.expected_bridge_sha, advisor.runtime_tuple.admitted_bridge_sha())
        self.assertNotEqual(
            port.expected_bridge_sha, FROZEN_BRIDGE_DEPENDENCY_SHA)

    def test_fleet_receipt_uses_registry_identity_not_model_json(self):
        root, sha = self.registry()

        def runner(snapshot, operation_id):
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "reasoning": "Select the eligible implementer.",
                    "proposals": [],
                    "observed_identity": {"profile": "liar", "effort": "forged"},
                }),
                "observed_executable": "/bin/echo",
                "operation_id": operation_id,
            }

        port = advisor.HermesProcessAdvisor(
            str(self.fake_hermes()), requested_profile="advisory-process",
            requested_effort="unknown", bridge_root=root,
            expected_bridge_sha=sha, fleet_runner=runner)
        report = self.cycle(manager=port)
        self.assertEqual(report["outcome"], "completed")
        identities = report["export"]["execution_receipt"]["identities"]
        self.assertEqual(identities["observed_profile"], "codex-luna-max")
        self.assertEqual(identities["observed_effort"], "max")
        self.assertEqual(identities["observed_model"], "gpt-5.6-luna")
        self.assertEqual(identities["bridge_sha"], sha)
        self.assertNotEqual(identities["observed_profile"], "liar")
        self.assertTrue(identities["operation_id"])
        self.assertTrue(identities["hermes_digest"])
        self.assertTrue(identities["request_digest"])
        self.assertEqual(identities["transport"], "hermes-to-bridge-fleet")
