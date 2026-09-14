from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import (
    ContractError,
    SFV2_002_HEAD,
    contracts_dir,
    fixture_path,
    load_pcp,
    load_pcp_file,
    validate_document,
)
from factory_v2.canonical_contracts.validate_contracts import run as run_fixture_gate
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.models import CandidateIdentity
from factory_v2.states import MissionState
from factory_v2.store import Store


class Sfv2002ContractIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(root / "ledger.sqlite")
        self.executor = ScriptedGrok(["cand-A", "cand-B", "cand-C", "cand-D-red", "cand-D"])
        self.manager = ScriptedHermes(self.executor)
        self.verifier = ScriptedVerifier(
            {
                "cand-A": (False, False),
                "cand-B": (True, False),
                "cand-C": (True, True),
                "cand-D-red": (False, False),
                "cand-D": (True, True),
            }
        )
        self.distributor = ScriptedDistributor()
        self.ctl = Controller(
            self.store, self.manager, self.verifier, self.distributor, root / "sandboxes"
        )
        self.pcp = json.loads(fixture_path("valid-approved-pcp.json").read_text())

    def test_consumed_contracts_are_the_frozen_sfv2_002_head(self):
        pin = (contracts_dir() / "PIN.txt").read_text()
        self.assertIn(SFV2_002_HEAD, pin)
        self.assertEqual(0, run_fixture_gate(contracts_dir()))

    def test_canonical_valid_pcp_admitted(self):
        snap = self.ctl.admit_pcp(load_pcp_file(fixture_path("valid-approved-pcp.json")))
        self.assertEqual(snap.state, MissionState.PCP_APPROVED)
        self.assertEqual(snap.pcp["pcp"]["id"], "pcp.demo")

    def test_missing_owner_approval_rejected(self):
        raw = json.loads(fixture_path("pcp-without-owner-approval.invalid.json").read_text())
        with self.assertRaises(ContractError):
            load_pcp(raw)
        with self.assertRaises(GateError):
            self.ctl.admit_pcp(raw)
        with self.assertRaises(GateError):
            self.ctl.admit_pcp({"title": "echo", "intent": "legacy shape"})

    def test_stale_verifier_wrong_candidate_rejected(self):
        stale = json.loads(
            fixture_path("stale-verifier-pass-wrong-candidate.invalid.json").read_text()
        )
        with self.assertRaises(ContractError):
            validate_document("verification.schema.json", stale)
        wrong = CandidateIdentity(
            "candidate-a",
            "1111111111111111111111111111111111111111",
            "sha256:" + "b" * 64,
            "git+https://example.invalid/factory-v2@1111111",
        )
        self.verifier.substitute = wrong
        mid = self.ctl.admit_pcp(self.pcp).mission_id
        self.ctl.tick(mid)
        with self.assertRaises(InvariantError):
            self.ctl.tick(mid)
        self.assertEqual(self.ctl.get(mid).state, MissionState.VERIFYING)

    def test_review_and_qa_fail_rework_same_lineage(self):
        mid = self.ctl.admit_pcp(self.pcp).mission_id
        self.ctl.tick(mid)
        review = self.ctl.tick(mid)
        self.assertEqual(review.state, MissionState.ENGINEERING)
        self.assertEqual(review.lineage_id, mid)
        self.ctl.tick(mid)
        qa = self.ctl.tick(mid)
        self.assertEqual(qa.state, MissionState.ENGINEERING)
        self.assertEqual(qa.lineage_id, mid)
        self.assertEqual(qa.candidates[1].status, "qa_failed")

    def test_owner_reject_preserves_lineage_and_new_candidate(self):
        mid = self._to_verified("cand-C")
        self.ctl.tick(mid)
        rejected = self.ctl.owner_decide(mid, "REJECT", "change intent")
        self.assertEqual(rejected.mission_id, mid)
        self.assertEqual(rejected.lineage_id, mid)
        before = {c.candidate_id for c in rejected.candidates}
        self.ctl.tick(mid)
        after = self.ctl.tick(mid)
        self.assertTrue({c.candidate_id for c in after.candidates} - before)

    def test_verified_rc_validates(self):
        mid = self._to_verified("cand-C")
        path = Path(self.tmp.name) / "sandboxes" / mid / "verified-rc.json"
        document = json.loads(path.read_text())
        validate_document("verified-rc.schema.json", document)

    def test_distribution_substitution_rejected(self):
        invalid = json.loads(
            fixture_path("distribution-artifact-substitution.invalid.json").read_text()
        )
        with self.assertRaises(ContractError):
            validate_document("distribution-handoff.schema.json", invalid)
        mid = self._to_verified("cand-C")
        self.ctl.tick(mid)
        snap = self.ctl.owner_decide(mid, "APPROVE")
        fake = CandidateIdentity(
            snap.approved.candidate_id,
            snap.approved.source_revision,
            "sha256:" + "9" * 64,
            snap.approved.artifact_uri,
        )
        with self.assertRaises(InvariantError):
            self.ctl.distribute(mid, substitute=fake)
        self.distributor.replace_with = fake
        with self.assertRaises(InvariantError):
            self.ctl.distribute(mid)

    def test_restart_replay_preserves_gates(self):
        mid = self.ctl.admit_pcp(self.pcp).mission_id
        self.ctl.tick(mid)
        restarted = Controller(
            Store(self.store.path),
            self.manager,
            self.verifier,
            self.distributor,
            Path(self.tmp.name) / "sandboxes",
        )
        again = restarted.admit_pcp(self.pcp)
        self.assertEqual(again.mission_id, mid)
        self.assertEqual(again.state, MissionState.VERIFYING)
        after = restarted.tick(mid)
        self.assertEqual(after.state, MissionState.ENGINEERING)

    def _to_verified(self, label: str) -> str:
        mid = self.ctl.admit_pcp(self.pcp).mission_id
        while True:
            snap = self.ctl.tick(mid)
            if snap.state is MissionState.VERIFIED_RC:
                self.assertEqual(snap.current.candidate_id, label)
                return mid
            if snap.state is MissionState.BLOCKED:
                self.fail(snap.blocked_reason)


if __name__ == "__main__":
    unittest.main()
