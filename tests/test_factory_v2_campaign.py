from __future__ import annotations

import copy
import json
import stat
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.adapters.hermes import derive_campaign_plan
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import fixture_path, identity_for, pcp_hash
from factory_v2.machine import Controller
from factory_v2.models import (
    CandidateIdentity,
    ExecutorResult,
    MissionContext,
    WorkItem,
)
from factory_v2.serve import make_server
from factory_v2.states import MissionState
from factory_v2.store import Store

BASE_PCP = json.loads(fixture_path("valid-approved-pcp.json").read_text(encoding="utf-8"))

MULTI_ITEM_PCP = copy.deepcopy(BASE_PCP)
MULTI_ITEM_PCP["acceptance"]["functional"] = [
    {
        "id": "fn-step1",
        "statement": "Step 1: world foundation and landing scene",
        "verification": "verify world",
    },
    {
        "id": "fn-step2",
        "statement": "Step 2: character creator and resident controls",
        "verification": "verify creator",
    },
    {
        "id": "fn-step3",
        "statement": "Step 3: player ownership and switching",
        "verification": "verify player control",
    },
    {
        "id": "fn-step4",
        "statement": "Step 4: direct interactions and animations",
        "verification": "verify interactions",
    },
]

KYRIEDACHI_PCP = copy.deepcopy(BASE_PCP)
KYRIEDACHI_PCP["pcp"]["id"] = "lodus-kyriedachi-life"
KYRIEDACHI_PCP["product"]["objective"] = (
    "Build Kyriedachi Life MVP-1 as a daughter-ready polished playable experience"
)
KYRIEDACHI_PCP["acceptance"]["functional"] = [
    {
        "id": "mvp-world",
        "statement": "Opening the product immediately shows the inhabited Kyriedachi world",
        "verification": "inspect world",
    },
    {
        "id": "mvp-rich-creator",
        "statement": "Kyrie and Zeke can each be independently customized",
        "verification": "creator matrix",
    },
    {
        "id": "mvp-player-control",
        "statement": "Kyrie can directly play Kyrie and Zeke can separately play Zeke",
        "verification": "player switch verify",
    },
]


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class RecordingMultiItemExecutor:
    """Mock executor tracking calls, work items, and states."""

    name = "Recording Multi Executor"
    harness_mode = "simulated"

    def __init__(self, ctl_ref: list[Controller]):
        self.ctl_ref = ctl_ref
        self.work_items_received: list[WorkItem] = []
        self.states_at_invocation: list[MissionState] = []
        self.verifier_called_during_engineering = False

    def credentials_available(self) -> bool:
        return True

    def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
        self.work_items_received.append(work)
        if self.ctl_ref:
            snap = self.ctl_ref[0].get(ctx.mission_id)
            self.states_at_invocation.append(snap.state)
        cid = f"cand-{work.item_id}"
        cand = identity_for(cid, ctx.workspace_path)
        return ExecutorResult(
            candidate=cand,
            grok_session_ref=f"sess-{work.item_id}",
            harness_mode="simulated",
            simulated=True,
        )


class FactoryV2CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "ledger.sqlite")
        self.sandboxes = self.root / "sandboxes"
        self.sandboxes.mkdir(parents=True, exist_ok=True)

    def test_01_multi_item_campaign_runs_all_items_before_verification(self):
        """Multi-item PCP requiring >= 3 items runs all items sequentially before verification."""
        plan = derive_campaign_plan(
            MissionContext(
                mission_id="msn-plan-test",
                lineage_id="lin-plan-test",
                pcp_hash="hash",
                pcp=MULTI_ITEM_PCP,
                workspace_path=str(self.sandboxes / "msn-plan-test"),
            )
        )
        self.assertGreaterEqual(len(plan), 3)
        self.assertEqual(len(plan), 4)

        ctl_ref: list[Controller] = []
        executor = RecordingMultiItemExecutor(ctl_ref)
        manager = ScriptedHermes(executor)

        verifier_calls: list[str] = []

        class TrackingVerifier:
            name = "Antigravity"
            harness_mode = "simulated"

            def review(self, ctx: MissionContext, candidate: CandidateIdentity):
                verifier_calls.append("review")
                if len(executor.work_items_received) < 4:
                    executor.verifier_called_during_engineering = True
                from factory_v2.models import Verdict
                return Verdict(
                    kind="review",
                    candidate=candidate,
                    passed=True,
                    harness_mode="simulated",
                    verifier_identity="antigravity:reviewer-1",
                )

            def qa(self, ctx: MissionContext, candidate: CandidateIdentity):
                verifier_calls.append("qa")
                from factory_v2.models import Verdict
                return Verdict(
                    kind="qa",
                    candidate=candidate,
                    passed=True,
                    harness_mode="simulated",
                    verifier_identity="antigravity:qa-1",
                )

        verifier = TrackingVerifier()
        distributor = ScriptedDistributor()
        ctl = Controller(
            self.store,
            manager,
            verifier,
            distributor,
            self.sandboxes,
        )
        ctl_ref.append(ctl)

        snap = ctl.submit_pcp(MULTI_ITEM_PCP)

        self.assertEqual(len(executor.work_items_received), 4)
        indices = [w.index for w in executor.work_items_received]
        totals = [w.total for w in executor.work_items_received]
        self.assertEqual(indices, [1, 2, 3, 4])
        self.assertEqual(totals, [4, 4, 4, 4])

        self.assertFalse(executor.verifier_called_during_engineering)
        self.assertIn("review", verifier_calls)
        self.assertIn("qa", verifier_calls)

        final_snap = ctl.get(snap.mission_id)
        self.assertEqual(final_snap.state, MissionState.OWNER_VALIDATION)

    def test_02_resume_state_preserves_work_between_items(self):
        """Work item N+1 continues in same sandbox and preserves all work from item N."""
        class WorkspaceModifyingExecutor:
            name = "Workspace Modifying Executor"
            harness_mode = "simulated"

            def credentials_available(self) -> bool:
                return True

            def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
                w_path = Path(ctx.workspace_path)
                item_file = w_path / f"output-{work.index}.txt"
                item_file.write_text(f"content-{work.index}", encoding="utf-8")

                if work.index > 1:
                    prev_file = w_path / f"output-{work.index - 1}.txt"
                    if not prev_file.exists() or prev_file.read_text(encoding="utf-8") != f"content-{work.index - 1}":
                        raise RuntimeError(f"Sandbox state corrupted between item {work.index - 1} and {work.index}")

                cand = identity_for(f"cand-{work.index}", str(w_path))
                return ExecutorResult(
                    candidate=cand,
                    grok_session_ref=f"sess-{work.index}",
                    harness_mode="simulated",
                    simulated=True,
                )

        executor = WorkspaceModifyingExecutor()
        manager = ScriptedHermes(executor)
        verifier = ScriptedVerifier({"cand-4": (True, True)})
        distributor = ScriptedDistributor()

        ctl = Controller(self.store, manager, verifier, distributor, self.sandboxes)
        snap = ctl.submit_pcp(MULTI_ITEM_PCP)
        ws = self.sandboxes / snap.mission_id

        for i in range(1, 5):
            f = ws / f"output-{i}.txt"
            self.assertTrue(f.exists(), f"output-{i}.txt missing in sandbox")
            self.assertEqual(f.read_text(encoding="utf-8"), f"content-{i}")

    def test_03_stalled_executor_hang_detection_and_safe_termination(self):
        """CursorCLIExecutor detects stalled executor hang early, kills it safely, and updates provenance."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        ws = self.sandboxes / "msn-hang-test"
        ws.mkdir(parents=True, exist_ok=True)

        (ws / "existing_work.js").write_text("console.log('intact');", encoding="utf-8")

        _script(
            bin_dir,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Logged in as test'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            "while true; do sleep 1; done\n",
        )

        env = {
            "PATH": str(bin_dir),
            "HOME": str(self.root / "home"),
            "FACTORY_V2_EXECUTOR_NO_PROGRESS_TIMEOUT": "0.3",
            "FACTORY_V2_EXECUTOR_POLL_INTERVAL": "0.05",
        }
        (self.root / "home").mkdir(parents=True, exist_ok=True)

        executor = CursorCLIExecutor(env=env, binary="agent")
        ctx = MissionContext(
            mission_id="msn-hang-test",
            lineage_id="lin-hang-test",
            pcp_hash="h" * 64,
            pcp=MULTI_ITEM_PCP,
            workspace_path=str(ws),
        )
        work = WorkItem(objective="Hang test item", item_id="item-hang", index=1, total=3)

        start = time.time()
        result = executor.implement(ctx, work)
        duration = time.time() - start

        self.assertTrue(result.blocked)
        self.assertLess(duration, 5.0)
        self.assertTrue("stalled" in result.reason.lower() or "progress" in result.reason.lower())

        self.assertEqual(
            (ws / "existing_work.js").read_text(encoding="utf-8"),
            "console.log('intact');",
        )

        prov_file = ws / "cursor-executor-provenance.json"
        self.assertTrue(prov_file.exists())
        prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
        self.assertEqual(prov_data.get("status"), "stalled")
        self.assertEqual(prov_data.get("resumable"), True)
        self.assertEqual(prov_data.get("mission_id"), "msn-hang-test")

        hb_file = ws / "cursor-executor-heartbeat.json"
        self.assertTrue(hb_file.exists())
        hb_data = json.loads(hb_file.read_text(encoding="utf-8"))
        self.assertIn(hb_data.get("status"), {"stalled", "running"})

    def test_04_status_visibility_during_engineering(self):
        """Status endpoint and snapshot expose active_stage and current_work_item during engineering."""
        observed_statuses: list[dict] = []

        class StatusCheckingExecutor:
            name = "Status Checking Executor"
            harness_mode = "simulated"

            def __init__(self, ctl_holder: list[Controller]):
                self.ctl_holder = ctl_holder

            def credentials_available(self) -> bool:
                return True

            def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
                if self.ctl_holder:
                    snap = self.ctl_holder[0].get(ctx.mission_id)
                    observed_statuses.append(snap.as_status_dict())
                cand = identity_for(f"cand-{work.index}", ctx.workspace_path)
                return ExecutorResult(
                    candidate=cand,
                    grok_session_ref=f"sess-{work.index}",
                    harness_mode="simulated",
                    simulated=True,
                )

        ctl_holder: list[Controller] = []
        executor = StatusCheckingExecutor(ctl_holder)
        manager = ScriptedHermes(executor)
        verifier = ScriptedVerifier({"cand-4": (True, True)})
        distributor = ScriptedDistributor()

        ctl = Controller(self.store, manager, verifier, distributor, self.sandboxes)
        ctl_holder.append(ctl)

        server = make_server(ctl, host="127.0.0.1", port=0)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        snap = ctl.submit_pcp(MULTI_ITEM_PCP)

        self.assertGreaterEqual(len(observed_statuses), 1)
        for st in observed_statuses:
            self.assertEqual(st.get("state"), "ENGINEERING")
            self.assertTrue(str(st.get("active_stage", "")).startswith("engineering"))
            cwi = st.get("current_work_item")
            self.assertIsNotNone(cwi)
            self.assertIn("index", cwi)
            self.assertIn("total", cwi)
            self.assertIn("objective", cwi)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/status", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("missions", data)
            self.assertEqual(len(data["missions"]), 1)
            self.assertEqual(data["missions"][0]["mission_id"], snap.mission_id)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/status/{snap.mission_id}", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["mission_id"], snap.mission_id)
            self.assertEqual(data["state"], "OWNER_VALIDATION")

    def test_05_kyriedachi_pcp_derives_full_five_item_delivery_slice(self):
        """Kyriedachi MVP-1 PCP derives all 5 required work items covering the complete delivery slice."""
        ctx = MissionContext(
            mission_id="msn-kyriedachi-test",
            lineage_id="lin-kyriedachi-test",
            pcp_hash="k" * 64,
            pcp=KYRIEDACHI_PCP,
            workspace_path=str(self.sandboxes / "msn-kyriedachi-test"),
        )
        plan = derive_campaign_plan(ctx)
        self.assertEqual(len(plan), 5)

        expected_ids = [
            "item-world-foundation",
            "item-rich-creator",
            "item-player-control",
            "item-interactions-animation",
            "item-social-mechanics",
        ]
        actual_ids = [item.item_id for item in plan]
        self.assertEqual(actual_ids, expected_ids)

        for i, item in enumerate(plan, 1):
            self.assertEqual(item.index, i)
            self.assertEqual(item.total, 5)
            self.assertTrue(len(item.objective) > 10)


if __name__ == "__main__":
    unittest.main()
