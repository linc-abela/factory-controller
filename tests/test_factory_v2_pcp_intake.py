from __future__ import annotations

import inspect
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import factory_v2.cli as cli_mod
import factory_v2.machine as machine_mod
import factory_v2.serve as serve_mod
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import fixture_path
from factory_v2.machine import Controller, GateError
from factory_v2.serve import make_server
from factory_v2.states import MissionState
from factory_v2.store import Store

PCP = json.loads(fixture_path("valid-approved-pcp.json").read_text(encoding="utf-8"))
UNAPPROVED = json.loads(
    fixture_path("pcp-without-owner-approval.invalid.json").read_text(encoding="utf-8")
)


class World:
    def __init__(self, artifacts=None, table=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "ledger.sqlite")
        self.executor = ScriptedGrok(
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
            }
        )
        self.distributor = ScriptedDistributor()
        self.ctl = Controller(
            self.store,
            self.manager,
            self.verifier,
            self.distributor,
            root / "sandboxes",
        )

    def close(self):
        self.tmp.cleanup()

    def reopen(self) -> Controller:
        return Controller(
            Store(self.store.path),
            self.manager,
            self.verifier,
            self.distributor,
            Path(self.tmp.name) / "sandboxes",
        )


def _post(port: int, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/pcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class FactoryV2PcpIntakeTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)

    def test_01_valid_approved_pcp_creates_exactly_one_mission(self):
        snap = self.world.ctl.submit_pcp(PCP)
        self.assertEqual(snap.lineage_id, snap.mission_id)
        self.assertEqual(len(self.world.store.list_missions()), 1)
        admitted = [e for e in snap.events if e["kind"] == "pcp_admitted"]
        self.assertEqual(len(admitted), 1)

    def test_02_valid_submission_starts_engineering_without_tick(self):
        snap = self.world.ctl.submit_pcp(PCP)
        self.assertTrue(self.world.manager.calls)
        self.assertNotEqual(snap.state, MissionState.PCP_APPROVED)
        self.assertEqual(snap.state, MissionState.OWNER_VALIDATION)

    def test_03_invalid_owner_approval_creates_no_mission(self):
        with self.assertRaises(GateError):
            self.world.ctl.submit_pcp(UNAPPROVED)
        self.assertEqual(self.world.store.list_missions(), ())
        self.assertEqual(self.world.manager.calls, [])

    def test_04_duplicate_submission_is_idempotent(self):
        first = self.world.ctl.submit_pcp(PCP)
        calls = len(self.world.manager.calls)
        second = self.world.ctl.submit_pcp(PCP)
        self.assertEqual(first.mission_id, second.mission_id)
        self.assertEqual(len(self.world.store.list_missions()), 1)
        self.assertEqual(len(self.world.manager.calls), calls)
        admitted = [e for e in second.events if e["kind"] == "pcp_admitted"]
        self.assertEqual(len(admitted), 1)

    def test_05_replay_after_restart_remains_idempotent(self):
        first = self.world.ctl.submit_pcp(PCP)
        calls = len(self.world.manager.calls)
        restarted = self.world.reopen()
        again = restarted.submit_pcp(PCP)
        self.assertEqual(again.mission_id, first.mission_id)
        self.assertEqual(again.state, first.state)
        self.assertEqual(len(self.world.manager.calls), calls)

    def test_06_nonterminal_mission_resumes_without_new_owner_action(self):
        admitted = self.world.ctl.admit_pcp(PCP)
        self.assertEqual(admitted.state, MissionState.PCP_APPROVED)
        self.assertEqual(self.world.manager.calls, [])
        restarted = self.world.reopen()
        resumed = restarted.resume_incomplete()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0].mission_id, admitted.mission_id)
        self.assertTrue(self.world.manager.calls)
        self.assertEqual(resumed[0].state, MissionState.OWNER_VALIDATION)

    def test_07_terminal_mission_is_not_spuriously_restarted(self):
        snap = self.world.ctl.submit_pcp(PCP)
        self.world.ctl.owner_decide(snap.mission_id, "APPROVE")
        done = self.world.ctl.distribute(snap.mission_id)
        self.assertEqual(done.state, MissionState.DISTRIBUTED)
        calls = len(self.world.manager.calls)
        restarted = self.world.reopen()
        self.assertEqual(restarted.resume_incomplete(), ())
        self.assertEqual(restarted.get(snap.mission_id).state, MissionState.DISTRIBUTED)
        self.assertEqual(len(self.world.manager.calls), calls)

    def test_08_intake_path_has_no_notion_awe_dispatch_or_watcher(self):
        for mod in (serve_mod, machine_mod, cli_mod):
            src = inspect.getsource(mod)
            self.assertNotIn("import notion", src)
            self.assertNotIn("from notion", src)
            self.assertNotIn("import awe", src)
            self.assertNotIn("from awe", src)
            self.assertNotIn("watchdog", src.lower())
        params = inspect.signature(Controller.__init__).parameters
        self.assertNotIn("notion", params)
        self.assertNotIn("awe", params)
        self.assertNotIn("dispatch", params)

    def test_09_intake_is_event_driven_not_polling_or_cron(self):
        src = inspect.getsource(serve_mod) + inspect.getsource(machine_mod.Controller.submit_pcp)
        self.assertNotIn("time.sleep", src)
        self.assertNotIn("sched.", src)
        self.assertNotIn("crontab", src.lower())
        self.assertNotIn("polling", src.lower())
        self.assertNotIn("FileSystemEvent", src)
        self.assertNotIn("Observer(", src)
        self.assertIn("POST /v1/pcp", inspect.getsource(serve_mod))

    def test_10_manual_cli_remains_diagnostic_normal_path_is_serve(self):
        readme = (Path(__file__).resolve().parents[1] / "factory_v2" / "README.md").read_text()
        self.assertIn("python3 -m factory_v2 serve", readme)
        self.assertIn("POST /v1/pcp", readme)
        self.assertIn("Diagnostic/recovery CLI remains available", readme)
        self.assertIn("admit-pcp", inspect.getsource(cli_mod))
        self.assertIn("tick", inspect.getsource(cli_mod))
        snap = self.world.ctl.admit_pcp(PCP)
        self.assertEqual(snap.state, MissionState.PCP_APPROVED)
        self.assertEqual(self.world.manager.calls, [])

    def test_http_post_v1_pcp_is_the_intake_boundary(self):
        httpd = make_server(self.world.ctl, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        status, body = _post(port, PCP)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], MissionState.OWNER_VALIDATION.value)
        self.assertTrue(self.world.manager.calls)
        again_status, again = _post(port, PCP)
        self.assertEqual(again_status, 200)
        self.assertEqual(again["mission_id"], body["mission_id"])
        denied, payload = _post(port, UNAPPROVED)
        self.assertEqual(denied, 400)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
