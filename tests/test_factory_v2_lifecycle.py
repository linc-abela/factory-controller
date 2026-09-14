from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

import factory_v2.machine as machine_mod
from factory_v2.adapters.grok_build import GrokBuildAdapter
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import fixture_path, pcp_hash
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.models import CandidateIdentity
from factory_v2.states import MissionState
from factory_v2.store import Store

PCP = json.loads(fixture_path("valid-approved-pcp.json").read_text(encoding="utf-8"))


class World:
    def __init__(
        self,
        artifacts=None,
        table=None,
        executor=None,
        replace_with=None,
        substitute=None,
    ):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "ledger.sqlite")
        self.executor = executor or ScriptedGrok(
            artifacts or ["cand-A", "cand-B", "cand-C", "cand-D-red", "cand-D"]
        )
        self.manager = ScriptedHermes(self.executor)
        self.verifier = ScriptedVerifier(
            table
            or {
                "cand-A": (False, False),
                "cand-B": (True, False),
                "cand-C": (True, True),
                "cand-D-red": (False, False),
                "cand-D": (True, True),
            },
            substitute=substitute,
        )
        self.distributor = ScriptedDistributor(replace_with=replace_with)
        self.ctl = Controller(
            self.store,
            self.manager,
            self.verifier,
            self.distributor,
            root / "sandboxes",
        )

    def close(self):
        self.tmp.cleanup()


class FactoryV2LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)

    def _admit(self):
        return self.world.ctl.admit_pcp(PCP)

    def _engineer_and_verify(self):
        self.world.ctl.tick(self._admit().mission_id)
        return self.world.ctl.tick(self._admit().mission_id)

    def test_111_approved_pcp_creates_exactly_one_durable_mission(self):
        a = self.world.ctl.admit_pcp(PCP)
        b = self.world.ctl.admit_pcp(PCP)
        self.assertEqual(a.mission_id, b.mission_id)
        self.assertEqual(a.lineage_id, a.mission_id)
        self.assertEqual(a.state, MissionState.PCP_APPROVED)
        self.assertEqual(a.pcp_hash, pcp_hash(PCP))
        admitted = [e for e in self.world.ctl.get(a.mission_id).events if e["kind"] == "pcp_admitted"]
        self.assertEqual(len(admitted), 1)

    def test_112_mission_proceeds_without_notion_awe_dispatch(self):
        src = inspect.getsource(machine_mod)
        self.assertNotIn("import notion", src)
        self.assertNotIn("from notion", src)
        params = inspect.signature(Controller.__init__).parameters
        self.assertNotIn("notion", params)
        self.assertNotIn("awe", params)
        self.assertNotIn("dispatch", params)
        snap = self._engineer_and_verify()
        self.assertIn(
            snap.state,
            {MissionState.ENGINEERING, MissionState.VERIFYING, MissionState.VERIFIED_RC},
        )
        self.assertTrue(self.world.manager.calls)

    def test_113_controller_delegates_through_nous_hermes_adapter(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        self.assertEqual(self.world.manager.name, "Nous Hermes Agent")
        self.assertEqual(len(self.world.manager.calls), 1)
        self.assertEqual(self.world.manager.calls[0].mission_id, mid)

    def test_114_hermes_delegates_coding_through_grok_executor(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        self.assertEqual(self.world.executor.name, "Grok Build")
        self.assertEqual(len(self.world.executor.calls), 1)
        self.assertEqual(self.world.executor.calls[0][0], mid)
        src = inspect.getsource(machine_mod.Controller._engineer)
        self.assertNotIn("executor.implement", src)
        self.assertNotIn("self.executor", src)

    def test_115_candidate_a_review_fail_returns_same_mission_to_engineering(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        snap = self.world.ctl.tick(mid)
        self.assertEqual(snap.mission_id, mid)
        self.assertEqual(snap.lineage_id, mid)
        self.assertEqual(snap.state, MissionState.ENGINEERING)
        self.assertEqual(snap.candidates[0].artifact_id, "cand-A")
        self.assertEqual(len(snap.candidates[0].identity.key()), 4)
        self.assertEqual(snap.candidates[0].review_verdict, "fail")
        self.assertEqual(snap.candidates[0].status, "review_failed")
        self.assertEqual(self.world.verifier.qa_calls, [])
        self.assertEqual(snap.rework_history[-1]["trigger"], "VERIFIER_REJECT")

    def test_116_candidate_b_qa_fail_returns_same_mission_to_engineering(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        snap = self.world.ctl.tick(mid)
        self.assertEqual(snap.mission_id, mid)
        self.assertEqual(snap.state, MissionState.ENGINEERING)
        b = [c for c in snap.candidates if c.artifact_id == "cand-B"][0]
        self.assertEqual(b.review_verdict, "pass")
        self.assertEqual(b.qa_verdict, "fail")
        self.assertEqual(b.status, "qa_failed")

    def test_117_candidate_c_both_pass_reaches_verified_rc(self):
        mid = self._to_c(self._admit().mission_id)
        snap = self.world.ctl.get(mid)
        self.assertEqual(snap.state, MissionState.VERIFIED_RC)
        c = [x for x in snap.candidates if x.artifact_id == "cand-C"][0]
        self.assertEqual(c.review_verdict, "pass")
        self.assertEqual(c.qa_verdict, "pass")
        self.assertEqual(c.status, "verified")
        rc = json.loads((Path(self.world.tmp.name) / "sandboxes" / mid / "verified-rc.json").read_text())
        self.assertEqual(rc["candidate"]["candidate_id"], "cand-C")

    def test_118_failed_a_and_b_cannot_be_owner_approved_or_distributed(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        with self.assertRaises(GateError):
            self.world.ctl.owner_decide(mid, "APPROVE")
        with self.assertRaises(GateError):
            self.world.ctl.distribute(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        with self.assertRaises(GateError):
            self.world.ctl.owner_decide(mid, "APPROVE")
        with self.assertRaises(GateError):
            self.world.ctl.distribute(mid)
        self.assertTrue(
            all(c.status in {"review_failed", "qa_failed"} for c in self.world.ctl.get(mid).candidates)
        )

    def test_119_owner_reject_c_same_mission_new_candidate_full_reverify(self):
        mid = self._to_c(self._admit().mission_id)
        self.world.ctl.tick(mid)
        snap = self.world.ctl.owner_decide(mid, "REJECT", "change intent")
        self.assertEqual(snap.mission_id, mid)
        self.assertEqual(snap.lineage_id, mid)
        self.assertEqual(snap.state, MissionState.ENGINEERING)
        self.assertEqual(snap.owner_history[-1]["decision"], "REJECT")
        before = {c.candidate_id for c in snap.candidates}
        self.world.ctl.tick(mid)
        after = self.world.ctl.tick(mid)
        self.assertEqual(after.mission_id, mid)
        self.assertGreater(len(after.candidates), len(before))
        new = [c for c in after.candidates if c.candidate_id not in before][0]
        self.assertEqual(new.review_verdict, "fail")
        self.assertIn("review_fail", [e["kind"] for e in after.events])

    def test_120_only_fully_green_d_returns_to_owner(self):
        mid = self._to_c(self._admit().mission_id)
        self.world.ctl.tick(mid)
        self.world.ctl.owner_decide(mid, "REJECT", "rework")
        self.world.ctl.tick(mid)
        red = self.world.ctl.tick(mid)
        self.assertEqual(red.state, MissionState.ENGINEERING)
        with self.assertRaises(GateError):
            self.world.ctl.owner_decide(mid, "APPROVE")
        self.world.ctl.tick(mid)
        green = self.world.ctl.tick(mid)
        self.assertEqual(green.state, MissionState.VERIFIED_RC)
        presented = self.world.ctl.tick(mid)
        self.assertEqual(presented.state, MissionState.OWNER_VALIDATION)
        d = [c for c in presented.candidates if c.artifact_id == "cand-D"][0]
        self.assertEqual((d.review_verdict, d.qa_verdict), ("pass", "pass"))

    def test_121_owner_approve_immutable_distribution_handoff(self):
        mid = self._green_d()
        snap = self.world.ctl.owner_decide(mid, "APPROVE")
        self.assertEqual(snap.state, MissionState.DISTRIBUTION_READY)
        self.assertEqual(snap.approved_artifact_id, "cand-D")
        self.assertEqual(snap.owner_decision, "APPROVE")
        other = [c for c in snap.candidates if c.candidate_id == "cand-C"][0].identity
        with self.assertRaises(PermissionError):
            self.world.store.apply_state(
                mid,
                MissionState.DISTRIBUTION_READY,
                approved=other,
                event_kind="tamper",
                payload={},
            )
        handoff = json.loads(
            (Path(self.world.tmp.name) / "sandboxes" / mid / "distribution-handoff.json").read_text()
        )
        self.assertEqual(handoff["deployment_artifact"]["candidate_id"], "cand-D")

    def test_122_distribution_cannot_replace_approved_candidate(self):
        mid = self._green_d()
        snap = self.world.ctl.owner_decide(mid, "APPROVE")
        approved = snap.approved
        fake = CandidateIdentity(
            approved.candidate_id,
            approved.source_revision,
            "sha256:" + "9" * 64,
            approved.artifact_uri,
        )
        with self.assertRaises(InvariantError):
            self.world.ctl.distribute(mid, substitute=fake)
        self.world.distributor.replace_with = fake
        with self.assertRaises(InvariantError):
            self.world.ctl.distribute(mid)
        self.world.distributor.replace_with = None
        done = self.world.ctl.distribute(mid)
        self.assertEqual(done.state, MissionState.DISTRIBUTED)
        self.assertEqual(self.world.distributor.calls[0][0].candidate_id, "cand-D")
        self.assertEqual(self.world.distributor.calls[0][0].key(), approved.key())

    def test_123_restart_replay_does_not_duplicate_mission_or_skip_gate(self):
        mid = self._admit().mission_id
        self.world.ctl.tick(mid)
        verifying = self.world.ctl.get(mid)
        self.assertEqual(verifying.state, MissionState.VERIFYING)
        restarted = Controller(
            Store(self.world.store.path),
            self.world.manager,
            self.world.verifier,
            self.world.distributor,
            Path(self.world.tmp.name) / "sandboxes",
        )
        again = restarted.admit_pcp(PCP)
        self.assertEqual(again.mission_id, mid)
        self.assertEqual(again.state, MissionState.VERIFYING)
        after = restarted.tick(mid)
        self.assertEqual(after.state, MissionState.ENGINEERING)
        self.assertNotEqual(after.state, MissionState.VERIFIED_RC)
        self.assertNotEqual(after.state, MissionState.DISTRIBUTION_READY)
        admitted = [e for e in restarted.get(mid).events if e["kind"] == "pcp_admitted"]
        self.assertEqual(len(admitted), 1)

    def test_124_missing_real_grok_credentials_fail_closed_not_simulated_success(self):
        home = Path(self.world.tmp.name) / "empty-home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in {"XAI_API_KEY", "GROK_DEPLOYMENT_KEY"}}
        env.pop("XAI_API_KEY", None)
        env.pop("GROK_DEPLOYMENT_KEY", None)
        real = GrokBuildAdapter(env=env, home=home)
        self.assertEqual(real.harness_mode, "real")
        self.assertFalse(real.credentials_available())
        self.world.close()
        self.world = World(executor=real)
        self.addCleanup(self.world.close)
        mid = self.world.ctl.admit_pcp(PCP).mission_id
        snap = self.world.ctl.tick(mid)
        self.assertEqual(snap.state, MissionState.BLOCKED)
        self.assertIn("credentials", (snap.blocked_reason or "").lower())
        self.assertEqual(snap.candidates, ())
        kinds = [e["kind"] for e in snap.events]
        self.assertIn("engineering_blocked", kinds)
        self.assertNotIn("verified_rc", kinds)
        payload = [e["payload"] for e in snap.events if e["kind"] == "engineering_blocked"][0]
        self.assertEqual(payload["harness_mode"], "real")
        self.assertTrue(payload["executor_called"])

    def test_machine_has_no_vendor_cli_strings(self):
        src = inspect.getsource(machine_mod)
        for needle in ("hermes chat", "grok -p", "antigravity review", "notion"):
            self.assertNotIn(needle, src.lower() if needle == "notion" else src)

    def _to_c(self, mid: str) -> str:
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        snap = self.world.ctl.tick(mid)
        self.assertEqual(snap.state, MissionState.VERIFIED_RC)
        return mid

    def _green_d(self) -> str:
        mid = self._to_c(self._admit().mission_id)
        self.world.ctl.tick(mid)
        self.world.ctl.owner_decide(mid, "REJECT", "rework")
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        self.world.ctl.tick(mid)
        presented = self.world.ctl.tick(mid)
        self.assertEqual(presented.state, MissionState.OWNER_VALIDATION)
        return mid


    def test_record_candidate_upsert_on_same_candidate_id(self) -> None:
        mid = self._admit().mission_id
        identity1 = CandidateIdentity("cand-same", "rev-1", "hash-1", "uri-1")
        identity2 = CandidateIdentity("cand-same", "rev-2", "hash-2", "uri-2")
        self.world.store.record_candidate(
            mid,
            identity1,
            1,
            attempt_id="att-1",
            hermes_session_id="h-1",
            grok_session_ref="g-1",
        )
        snap = self.world.store.record_candidate(
            mid,
            identity2,
            2,
            attempt_id="att-2",
            hermes_session_id="h-2",
            grok_session_ref="g-2",
        )
        self.assertIsNotNone(snap.current)
        self.assertEqual(snap.current.candidate_id, "cand-same")
        self.assertEqual(snap.current.source_revision, "rev-2")
        self.assertEqual(snap.current.artifact_hash, "hash-2")
        cands = snap.candidates
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].identity.source_revision, "rev-2")


if __name__ == "__main__":
    unittest.main()
