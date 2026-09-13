"""SF-272: Controller ledger + Vault PCP are execution authority; Notion is not."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from awe_worker.ledger import AWELedger
from awe_worker.model import AWEWorkItem, ExecutionSlot
from awe_worker.notion import NotionAPIError
from awe_worker.observation import MemoryTaskSource
from awe_worker.scheduler import AWEScheduledRunner
from awe_worker.harness import MockHarnessAdapter
from awe_worker.worker import AWEAutonomousWorker
from factory_controller import golden_path, pcp, pcp_missions
from factory_controller.store import MissionStore


SLOT = ExecutionSlot("cursor", "grok-4.6", "high")
FAKE_HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAKE_ARCH = {"harness": "test", "model": "architect", "effort": "high"}
FAKE_DEV = {"harness": "test", "model": "developer", "effort": "medium"}


class _FakeFleet:
    def __init__(self, prototype: Path) -> None:
        self.prototype = prototype
        self.head = FAKE_HEAD
        self.e2e_queue: list[str] = []
        self.impls = 0

    def hermes(self, mission):
        return {
            "owner": "hermes",
            "routing": {
                "source": "FACTORY/roadmap/phase-2-agent-capability-mapping.md",
                "capabilities": {
                    "architecture": "architecture / technical design",
                    "implementation": "developer fleet",
                    "functional_e2e": "qa / e2e / regression / performance",
                },
                "architecture": dict(FAKE_ARCH),
                "implementation": dict(FAKE_DEV),
            },
            "plan": ["architecture", "implementation", "integration",
                     "functional_e2e", "rc_alpha"],
            "prototype_input": str(self.prototype),
        }

    def architecture(self, mission, hermes, work: Path):
        work.mkdir(parents=True, exist_ok=True)
        artifact = work / "architecture.json"
        body = {
            "prototype_reuse": "REUSE_WITH_DELTA",
            "required_delta": "post-intake implementation delta",
            "subsystems": ["ui"],
            "invariants": ["prototype URL is not RC-alpha"],
            "implementation_packages": [{"id": "core"}],
            "verification": ["page contains post-intake delta"],
            "functional_e2e": ["playable finite shoe remains"],
            "rc_alpha": "serve integrated candidate only",
        }
        artifact.write_text(json.dumps(body), encoding="utf-8")
        return {
            "artifact": str(artifact),
            **FAKE_ARCH,
            "body": body,
        }

    def implementation(self, mission, architecture, work: Path, repair=None):
        work.mkdir(parents=True, exist_ok=True)
        self.impls += 1
        if repair:
            self.head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        if self.prototype.is_dir():
            for item in self.prototype.iterdir():
                if item.is_file():
                    shutil.copy2(item, work / item.name)
        html = (work / "index.html").read_text(encoding="utf-8")
        (work / "index.html").write_text(
            html.replace("</p>", " post-intake implementation delta</p>"),
            encoding="utf-8")
        (work / golden_path.CANDIDATE_MARKER).write_text(
            json.dumps({"candidate_head": self.head}), encoding="utf-8")
        return {
            "packages": [{
                "id": "core",
                **FAKE_DEV,
                "branch": "sf/mission/impl",
                "head": self.head,
                "acceptance": "delta committed after intake",
            }],
            "head": self.head,
        }

    def integrate(self, mission, implementation, work: Path):
        return {
            "candidate_head": self.head,
            "included_heads": [self.head],
            "mission_key": mission.mission_key,
        }

    def e2e(self, mission, work: Path, candidate_head: str):
        body = (work / "index.html").read_text(encoding="utf-8")
        ok = "post-intake implementation delta" in body
        result = "PASS" if ok else "FAIL"
        if self.e2e_queue:
            result = self.e2e_queue.pop(0)
        return {
            "candidate": candidate_head,
            "scenarios": ["playable finite shoe", "post-intake delta"],
            "result": result,
            "defects": [] if result == "PASS" else ["rendered visual bar unmet"],
        }

    def deploy(self, mission, work: Path, candidate_head: str, state_dir: Path):
        sealed = state_dir / "pcp-candidates" / candidate_head[:12]
        if sealed.exists():
            shutil.rmtree(sealed)
        sealed.mkdir(parents=True)
        for item in work.iterdir():
            if item.is_file():
                shutil.copy2(item, sealed / item.name)
        (sealed / golden_path.CANDIDATE_MARKER).write_text(json.dumps({
            "candidate_head": candidate_head,
            "package_id": mission.package_id,
        }), encoding="utf-8")
        url = pcp_missions.serve_product_rc(
            sealed, state_dir=state_dir, package_id=mission.package_id,
            candidate_head=candidate_head)
        return {"url": url, "candidate_head": candidate_head, "deployment": str(sealed)}


def _task(task_id="SF-272", status="Queue"):
    return AWEWorkItem(
        task_id=task_id,
        title=task_id,
        lane="Cursor",
        role="developer",
        status=status,
        model="Grok 4.6",
        effort="High",
        sequence=272,
    )


class EmptyDispatch:
    last_errors = {"cursor": "NOTION_API_ERROR timeout"}
    last_states = {"cursor": "stale"}

    def observe_slots(self):
        return ()

    def diagnostics(self):
        return {"errors": dict(self.last_errors), "states": dict(self.last_states)}


class SF272ExecutionAuthorityTests(unittest.TestCase):
    def test_notion_unavailable_still_starts_valid_work(self):
        source = MagicMock()
        source.fetch_tasks.side_effect = NotionAPIError(503, "unavailable")
        source.projection_snapshot.side_effect = NotionAPIError(503, "unavailable")
        ledger = AWELedger(":memory:")
        adapter = MockHarnessAdapter("cursor")
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=source,
            harness_adapters={"cursor": adapter},
            target_repo=".",
        )
        worker.ledger.claim(
            task_id="SF-272", lineage_id="SF-272", worker_id="w1",
            slot_key=SLOT.key, lease_seconds=60, now=1.0,
        )
        summary = worker.run_cycle(worker_id="w1", target_slot=SLOT, now=1.0)
        self.assertEqual(summary.claimed_task, "SF-272")
        self.assertEqual([item.task_id for item in adapter.woken_tasks], ["SF-272"])

    def test_notion_timeout_does_not_roll_back_ledger_claim(self):
        task = _task()
        sor = MagicMock()
        sor.claim_task.side_effect = NotionAPIError(504, "timeout")
        adapter = MockHarnessAdapter("cursor")
        ledger = AWELedger(":memory:")
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=MemoryTaskSource([task]),
            source_of_record=sor,
            harness_adapters={"cursor": adapter},
            target_repo=".",
        )
        summary = worker.run_cycle(worker_id="w1", target_slot=SLOT)
        self.assertEqual(summary.claimed_task, "SF-272")
        self.assertIsNotNone(ledger.get_claim("SF-272"))
        self.assertEqual(ledger.get_claim("SF-272")["state"], "in_progress")
        self.assertIn("notion_projection", summary.detail)
        self.assertEqual([item.task_id for item in adapter.woken_tasks], ["SF-272"])

    def test_stale_dashboard_does_not_block_or_duplicate_claim(self):
        task = _task()
        sor = MagicMock()
        sor.claim_task.return_value = (False, "SOURCE_OF_RECORD_CONFLICT: stale Current")
        ledger = AWELedger(":memory:")
        adapter = MockHarnessAdapter("cursor")
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=MemoryTaskSource([task]),
            source_of_record=sor,
            harness_adapters={"cursor": adapter},
            target_repo=".",
        )
        first = worker.run_cycle(worker_id="w1", target_slot=SLOT)
        second = worker.run_cycle(worker_id="w2", target_slot=SLOT)
        self.assertEqual(first.claimed_task, "SF-272")
        self.assertEqual(second.health, "conflict")
        self.assertEqual(ledger.get_claim("SF-272")["worker_id"], "w1")

    def test_owner_only_gate_still_blocks_wake(self):
        task = AWEWorkItem(
            task_id="SF-OWNER", title="owner", lane="Cursor", role="Owner",
            status="Queue", model="Grok 4.6", effort="High", sequence=1,
            owner_only=True, owner_reason="owner_judgment",
        )
        adapter = MockHarnessAdapter("cursor")
        worker = AWEAutonomousWorker(
            ledger=AWELedger(":memory:"),
            source=MemoryTaskSource([task]),
            harness_adapters={"cursor": adapter},
        )
        summary = worker.run_cycle(worker_id="w1", target_slot=SLOT)
        self.assertEqual(summary.health, "escalated")
        self.assertEqual(adapter.woken_tasks, [])

    def test_empty_dispatch_falls_back_to_local_queue_work(self):
        task = _task()
        worker = AWEAutonomousWorker(
            ledger=AWELedger(":memory:"),
            source=MemoryTaskSource([task]),
            harness_adapters={"cursor": MockHarnessAdapter("cursor")},
            target_repo=".",
        )
        runner = AWEScheduledRunner(
            worker=worker, worker_id="sf272", slot_provider=EmptyDispatch(),
        )
        summaries = runner.run(interval_seconds=0, max_cycles=1)
        self.assertEqual([row.claimed_task for row in summaries], ["SF-272"])


class SF272PCPMissionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        promoted = self.vault / "PRODUCTS" / "casino"
        promoted.mkdir(parents=True)
        (promoted / "pcp-v1.1.0.json").write_text(
            json.dumps({
                **pcp.materialize_casino_pcp(),
                "g5_promotion": {"decision": "APPROVED"},
                "factory_handoff": {"product_id": "casino"},
                "product_thesis": {"statement": "lab metadata"},
                "status": "promoted_g5",
            }), encoding="utf-8")
        draft = self.vault / "PRODUCTS" / "prototypes" / "casino"
        draft.mkdir(parents=True)
        (draft / "pcp-v1.1.0-draft.json").write_text(
            json.dumps(pcp.materialize_casino_pcp()), encoding="utf-8")
        vague = dict(pcp.materialize_casino_pcp())
        vague["package_id"] = "vague-product"
        vague["decision_ledger"][0]["status"] = "open"
        vague["decision_ledger"][0].pop("resolution", None)
        vague["decision_ledger"][0]["owner_role"] = "Owner / CEO"
        vague["decision_ledger"][0]["deadline"] = "unspecified"
        other = self.vault / "PRODUCTS" / "vague-product"
        other.mkdir()
        (other / "pcp-v1.1.0.json").write_text(json.dumps(vague), encoding="utf-8")
        self.store = MissionStore(self.root / "controller.db")
        self.plane = pcp_missions.PCPMissionPlane(self.store, self.vault)

    @staticmethod
    def _stop_rc(state_dir, package_id):
        receipt = Path(state_dir) / "pcp-rc-alpha" / ("%s.json" % package_id)
        if not receipt.is_file():
            return
        try:
            pid = int(json.loads(receipt.read_text(encoding="utf-8")).get("pid") or 0)
        except (OSError, ValueError):
            return
        if pid > 1:
            try:
                os.killpg(pid, 15)
            except ProcessLookupError:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass

    def test_promoted_pcp_is_detected_without_notion(self):
        rows = self.plane.sync()
        ids = {row.package_id: row for row in rows}
        self.assertIn("lodus-casino", ids)
        self.assertEqual(ids["lodus-casino"].lifecycle, "HERMES")
        self.assertEqual(ids["vague-product"].lifecycle, "CLARITY_REQUIRED")
        self.assertTrue(ids["vague-product"].clarification)
        self.assertEqual(
            {row.canonical_path for row in rows},
            {"PRODUCTS/casino/pcp-v1.1.0.json", "PRODUCTS/vague-product/pcp-v1.1.0.json"},
        )

    def test_same_pcp_revision_is_idempotent(self):
        first = self.plane.sync()
        second = self.plane.sync()
        self.assertEqual(
            {row.mission_key for row in first},
            {row.mission_key for row in second},
        )
        self.assertEqual(len(self.plane.list()), 2)

    def test_hold_and_lease_prevent_duplicate_workers(self):
        self.plane.sync()
        casino = next(row for row in self.plane.list() if row.package_id == "lodus-casino")
        self.assertTrue(self.plane.claim(casino.mission_key, "worker-a"))
        self.assertFalse(self.plane.claim(casino.mission_key, "worker-b"))
        self.plane.set_hold(casino.mission_key, True)
        self.assertFalse(self.plane.claim(casino.mission_key, "worker-a"))

    def test_rc_url_without_provenance_is_rejected(self):
        self.plane.sync()
        casino = next(row for row in self.plane.list() if row.package_id == "lodus-casino")
        with self.assertRaises(golden_path.IncompleteChain):
            self.plane.set_rc_url(casino.mission_key, alpha="https://example.test/rc-alpha")
        row = next(item for item in self.plane.list() if item.mission_key == casino.mission_key)
        self.assertEqual(row.lifecycle, "HERMES")
        self.assertEqual(row.rc_alpha_url, "")

    def test_missing_checkout_does_not_invent_a_stub_rc(self):
        rows = self.plane.advance(process=lambda mission: {})
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.assertEqual(casino.lifecycle, "HERMES")
        self.assertEqual(casino.rc_alpha_url, "")

    def test_existing_checkout_is_not_rc_alpha(self):
        checkout = self.root / "projects" / "prototype-casino"
        checkout.mkdir(parents=True)
        (checkout / "index.html").write_text(
            "<!doctype html><title>Casino proof</title><p>playable finite shoe</p>\n",
            encoding="utf-8")
        casino = next(row for row in self.plane.sync() if row.package_id == "lodus-casino")
        pcp_path = self.vault / casino.canonical_path
        body = json.loads(pcp_path.read_text(encoding="utf-8"))
        body["factory_handoff"] = {
            "product_id": "casino",
            "source_repository": "linc-abela/prototype-casino",
        }
        pcp_path.write_text(json.dumps(body), encoding="utf-8")
        found = pcp_missions.locate_prototype(
            self.vault, casino, project_roots=(self.root / "projects",))
        self.assertEqual(found, checkout)
        url = pcp_missions.serve_product_rc(
            found, state_dir=self.root / "state", package_id=casino.package_id,
            candidate_head="deadbeef")
        self.assertEqual(url, "")
        rows = self.plane.advance(process=lambda mission: {
            "rc_alpha": {"url": "http://127.0.0.1:9/", "root": str(checkout)},
        })
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.assertEqual(casino.rc_alpha_url, "")
        self.assertNotEqual(casino.lifecycle, "OWNER_VALIDATION")

    def test_golden_path_requires_hermes_architecture_implementation(self):
        checkout = self.root / "projects" / "lodus-casino"
        checkout.mkdir(parents=True)
        (checkout / "index.html").write_text(
            "<!doctype html><title>lodus-casino</title><p>finite-shoe higher/lower</p>\n",
            encoding="utf-8")
        executors = _FakeFleet(checkout)
        def process(mission):
            return golden_path.run(
                mission, vault_root=self.vault, state_dir=self.root / "state",
                executors=executors)
        rows = self.plane.advance(process=process)
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.addCleanup(lambda: self._stop_rc(self.root / "state", "lodus-casino"))
        self.assertEqual(casino.lifecycle, "OWNER_VALIDATION")
        self.assertTrue(casino.rc_alpha_url)
        body = pcp_missions._fetch(casino.rc_alpha_url)
        self.assertIn("finite-shoe higher/lower", body)
        self.assertIn("post-intake implementation delta", body)
        self.assertFalse(pcp_missions.is_stub_rc_body(body))
        self.assertEqual(golden_path.missing_links(casino.evidence), ())
        self.assertEqual(
            casino.evidence["hermes"]["routing"]["capabilities"]["architecture"],
            "architecture / technical design")
        self.assertEqual(
            casino.evidence["architecture"]["model"], "architect")
        self.assertEqual(
            casino.evidence["implementation"]["packages"][0]["model"],
            "developer")
        mission, created = self.store.submit(
            {
                "work_item_id": "lodus-casino:build",
                "project_id": "lodus-casino",
                "source_pcp": casino.canonical_path,
                "package_digest": casino.package_digest,
                "lifecycle": "HERMES",
            },
            casino.mission_key,
        )
        self.assertFalse(created)
        self.assertEqual(mission["project_id"], "lodus-casino")

    def test_kyriedachi_extra_fields_do_not_block_factory_intake(self):
        src = Path(
            "/Users/Shared/Projects/factory-vault-SF-272/PRODUCTS/"
            "kyriedachi-life/pcp-v1.1.0.json"
        )
        dest = self.vault / "PRODUCTS" / "kyriedachi-life"
        dest.mkdir()
        dest.joinpath("pcp-v1.1.0.json").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8")
        rows = {row.package_id: row for row in self.plane.sync()}
        self.assertIn("lodus-kyriedachi-life", rows)
        self.assertEqual(rows["lodus-kyriedachi-life"].lifecycle, "HERMES")
        self.assertFalse(rows["lodus-kyriedachi-life"].clarification)

    def test_owner_reject_continues_same_mission_to_new_rc(self):
        checkout = self.root / "projects" / "lodus-casino"
        checkout.mkdir(parents=True)
        (checkout / "index.html").write_text(
            "<!doctype html><title>lodus-casino</title><p>finite-shoe higher/lower</p>\n",
            encoding="utf-8")
        executors = _FakeFleet(checkout)

        def process(mission):
            return golden_path.run(
                mission, vault_root=self.vault, state_dir=self.root / "state",
                executors=executors)

        rows = self.plane.advance(process=process)
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.addCleanup(lambda: self._stop_rc(self.root / "state", "lodus-casino"))
        first_key = casino.mission_key
        first_url = casino.rc_alpha_url
        self.assertEqual(casino.lifecycle, "OWNER_VALIDATION")
        rejected = casino.evidence["integration"]["candidate_head"]
        updated = self.plane.ingest_owner_validation(
            first_key, rejected, "REJECT",
            "map flickers; roads are crude diagonal bars")
        self.assertEqual(updated.mission_key, first_key)
        self.assertEqual(updated.rc_alpha_url, "")
        self.assertNotEqual(updated.lifecycle, "OWNER_VALIDATION")
        self.assertEqual(
            updated.evidence["owner_validation"]["candidate_head"], rejected)
        rows = self.plane.advance(process=process)
        casino = next(row for row in rows if row.mission_key == first_key)
        self.assertEqual(casino.mission_key, first_key)
        self.assertEqual(casino.lifecycle, "OWNER_VALIDATION")
        self.assertTrue(casino.rc_alpha_url)
        self.assertNotEqual(casino.rc_alpha_url, first_url)
        self.assertNotEqual(
            casino.evidence["integration"]["candidate_head"], rejected)
        self.assertIn(rejected, casino.evidence["rejected_candidates"])

    def test_e2e_fail_triggers_repair_before_rc_alpha(self):
        checkout = self.root / "projects" / "lodus-casino"
        checkout.mkdir(parents=True)
        (checkout / "index.html").write_text(
            "<!doctype html><title>lodus-casino</title><p>finite-shoe higher/lower</p>\n",
            encoding="utf-8")
        executors = _FakeFleet(checkout)
        executors.e2e_queue = ["FAIL", "PASS"]

        def process(mission):
            return golden_path.run(
                mission, vault_root=self.vault, state_dir=self.root / "state",
                executors=executors)

        rows = self.plane.advance(process=process)
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.addCleanup(lambda: self._stop_rc(self.root / "state", "lodus-casino"))
        self.assertEqual(casino.lifecycle, "OWNER_VALIDATION")
        self.assertEqual(casino.evidence["functional_e2e"]["result"], "PASS")
        self.assertTrue(casino.evidence.get("e2e_runs"))
        self.assertEqual(casino.evidence["e2e_runs"][0]["result"], "FAIL")
        self.assertNotEqual(
            casino.evidence["integration"]["candidate_head"], FAKE_HEAD)


class SF272RenderedE2ETests(unittest.TestCase):
    def test_node_test_pass_is_not_ag_e2e_pass(self):
        from factory_controller.capability_map import parse as parse_map
        from factory_controller.fleet_harness import HarnessReceipt

        catalog = parse_map("""
| Capability / role | Runner | Model | Effort / policy | Purpose |
|---|---|---|---|---|
| **QA / E2E / regression / performance** | Antigravity | **Gemini 3.8** | **Medium** | rendered functional E2E |
| **Developer Fleet** | Cursor | **Nova 2** | **High** | recovery |
""")
        calls: list[str] = []

        class Harness:
            def run(self, profile, prompt, cwd):
                calls.append(profile.key)
                return HarnessReceipt(
                    status="COMPLETED", harness=profile.harness,
                    model=profile.model, effort=profile.effort,
                    stdout_tail="node tests passed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            (work / "index.html").write_text(
                "<!doctype html><title>x</title><p>ok</p>\n", encoding="utf-8")
            (work / "package.json").write_text('{"name":"x"}', encoding="utf-8")
            golden_path._git(["init"], work)
            golden_path._git(["add", "-A"], work)
            golden_path._git(["commit", "-m", "intake"], work)
            head = golden_path._git_head(work)
            (work / golden_path.CANDIDATE_MARKER).write_text(json.dumps({
                "candidate_head": head, "package_id": "x", "mission_key": "m",
            }), encoding="utf-8")
            mission = type("M", (), {
                "mission_key": "m", "package_id": "x",
                "canonical_path": "PRODUCTS/x/pcp.json",
                "package_digest": "d", "evidence": None,
            })()
            executors = golden_path.FleetExecutors(
                vault_root=root, state_dir=root, catalog=catalog,
                harness=Harness())
            result = executors.e2e(mission, work, head)
            receipt = root / "pcp-e2e" / "x.json"
            if receipt.is_file():
                try:
                    pid = int(json.loads(receipt.read_text()).get("pid") or 0)
                    if pid > 1:
                        os.kill(pid, 15)
                except (OSError, ValueError, ProcessLookupError):
                    pass
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["capability"], golden_path.CAP_QA)
        self.assertTrue(calls)
        self.assertIn("antigravity", calls[0])
        self.assertNotEqual(result.get("harness"), "node")


if __name__ == "__main__":
    unittest.main()
