from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "validation" / "conformance_controller.py"
CONTRACTS = ROOT / "factory_v2" / "canonical_contracts"


class ConformanceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self._prev = os.environ.get("FACTORY_V2_CONTRACTS_DIR")
        os.environ["FACTORY_V2_CONTRACTS_DIR"] = str(CONTRACTS)
        self.addCleanup(self._restore_contracts)

    def _restore_contracts(self):
        if self._prev is None:
            os.environ.pop("FACTORY_V2_CONTRACTS_DIR", None)
        else:
            os.environ["FACTORY_V2_CONTRACTS_DIR"] = self._prev

    def _invoke(self, scenario: str, *, mode: str = "deterministic") -> dict:
        request = self.workspace / f"{scenario}.json"
        request.write_text(
            json.dumps(
                {
                    "protocol": "factory.v2.conformance.v1",
                    "scenario": scenario,
                    "mode": mode,
                    "contracts_dir": str(CONTRACTS),
                    "workspace": str(self.workspace / "controller-run"),
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["HOME"] = str(self.workspace / "home")
        completed = subprocess.run(
            [sys.executable, str(PROTOCOL), "--request", str(request)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertEqual(response["protocol"], "factory.v2.conformance.v1")
        self.assertEqual(response["scenario"], scenario)
        self.assertEqual(response["mode"], mode)
        return response

    def test_protocol_script_is_the_documented_entrypoint(self):
        self.assertTrue(PROTOCOL.is_file())
        source = PROTOCOL.read_text(encoding="utf-8")
        self.assertIn("factory.v2.conformance.v1", source)
        self.assertIn("from factory_v2.machine import Controller", source)

    def test_s01_admits_owner_approved_pcp(self):
        response = self._invoke("S01")
        self.assertTrue(response["accepted"])
        self.assertEqual(response["event"], "pcp_admitted")
        self.assertEqual(response["owner_approval"], "APPROVE")

    def test_s02_rejects_missing_owner_approval(self):
        response = self._invoke("S02")
        self.assertFalse(response["accepted"])
        self.assertEqual(response["failure_code"], "OWNER_APPROVAL_REQUIRED")

    def test_s03_rejects_source_identity_mismatch(self):
        response = self._invoke("S03")
        self.assertFalse(response["accepted"])
        self.assertEqual(response["failure_code"], "PCP_SOURCE_IDENTITY_INVALID")

    def test_s04_exposes_complete_candidate_tuple(self):
        response = self._invoke("S04")
        candidate = response["candidate"]
        self.assertEqual(
            response["identity_fields"],
            ["candidate_id", "source_revision", "artifact_hash", "artifact_uri"],
        )
        self.assertRegex(candidate["source_revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(candidate["artifact_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(candidate["artifact_uri"])

    def test_s05_rejects_stale_verifier_candidate(self):
        response = self._invoke("S05")
        self.assertFalse(response["accepted"])
        self.assertEqual(response["failure_code"], "STALE_VERIFIER_CANDIDATE")

    def test_s06_review_fail_returns_same_lineage(self):
        response = self._invoke("S06")
        self.assertTrue(response["returned_to_engineering"])
        self.assertTrue(response["same_mission"])
        self.assertTrue(response["same_lineage"])
        self.assertEqual(response["failed_channel"], "review")

    def test_s07_qa_fail_returns_same_lineage(self):
        response = self._invoke("S07")
        self.assertTrue(response["returned_to_engineering"])
        self.assertEqual(response["failed_channel"], "qa_e2e")

    def test_s08_repaired_candidate_is_freshly_verified(self):
        response = self._invoke("S08")
        ids = [item["candidate_id"] for item in response["candidates"]]
        verified = [item["candidate_id"] for item in response["verification_attempts"]]
        self.assertGreaterEqual(len(set(ids)), 2)
        self.assertGreaterEqual(len(set(verified)), 2)
        self.assertTrue(set(verified).issuperset(set(ids[:2])))

    def test_s09_verified_rc_requires_both_passes(self):
        response = self._invoke("S09")
        self.assertEqual(response["state"], "VERIFIED_RC")
        self.assertEqual(response["code_review"], "PASS")
        self.assertEqual(response["qa_e2e"], "PASS")
        self.assertEqual(response["owner_gate"], "AWAITING_OWNER")

    def test_s10_owner_reject_preserves_lineage(self):
        response = self._invoke("S10")
        self.assertEqual(response["owner_decision"], "REJECT")
        self.assertTrue(response["same_mission"])
        self.assertTrue(response["same_lineage"])
        self.assertGreaterEqual(len({c["candidate_id"] for c in response["candidates"]}), 2)

    def test_s11_owner_approve_creates_immutable_handoff(self):
        response = self._invoke("S11")
        self.assertEqual(response["owner_decision"], "APPROVE")
        self.assertEqual(response["state"], "DISTRIBUTION_READY")
        self.assertTrue(response["distribution_handoff"]["artifact_immutable"])
        self.assertEqual(len(response["distribution_handoff"]["candidate"]), 4)

    def test_s12_rejects_artifact_substitution(self):
        response = self._invoke("S12")
        self.assertFalse(response["accepted"])
        self.assertEqual(response["failure_code"], "ARTIFACT_SUBSTITUTION")

    def test_s13_replay_is_idempotent(self):
        response = self._invoke("S13")
        self.assertEqual(response["mission_count"], 1)
        self.assertEqual(response["admission_events"], 1)
        self.assertTrue(response["replay_idempotent"])
        self.assertTrue(response["gates_skipped_on_replay"])

    def test_s14_emits_canonical_document_references(self):
        response = self._invoke("S14")
        fixtures = {item["fixture"] for item in response["documents"]}
        self.assertIn("valid-approved-pcp.json", fixtures)
        self.assertIn("verified-rc-eligible.json", fixtures)
        self.assertIn("owner-approve-distribution.json", fixtures)

    def test_s15_runtime_truth_is_simulated_in_deterministic_mode(self):
        response = self._invoke("S15")
        self.assertTrue(response["runtime_truth"]["simulated"])


if __name__ == "__main__":
    unittest.main()
