"""SF-272: Controller ledger + Vault PCP are execution authority; Notion is not."""

from __future__ import annotations

import json
import os
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
from factory_controller import pcp, pcp_missions
from factory_controller.store import MissionStore


SLOT = ExecutionSlot("cursor", "grok-4.6", "high")


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
        self.assertEqual(ids["lodus-casino"].lifecycle, "IMPLEMENT")
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

    def test_rc_url_advances_lifecycle_to_owner_validation(self):
        self.plane.sync()
        casino = next(row for row in self.plane.list() if row.package_id == "lodus-casino")
        self.plane.set_rc_url(casino.mission_key, alpha="https://example.test/rc-alpha")
        row = next(item for item in self.plane.list() if item.mission_key == casino.mission_key)
        self.assertEqual(row.lifecycle, "OWNER_VALIDATION")
        self.assertEqual(row.rc_alpha_url, "https://example.test/rc-alpha")

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
        self.assertEqual(rows["lodus-kyriedachi-life"].lifecycle, "IMPLEMENT")
        self.assertFalse(rows["lodus-kyriedachi-life"].clarification)

    def test_missing_checkout_does_not_invent_a_stub_rc(self):
        rows = self.plane.advance(rc_alpha_for=lambda mission: "")
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.assertEqual(casino.lifecycle, "IMPLEMENT")
        self.assertEqual(casino.rc_alpha_url, "")

    def test_handoff_checkout_is_served_as_working_rc(self):
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
        found = pcp_missions.resolve_product_checkout(
            self.vault, casino, project_roots=(self.root / "projects",))
        self.assertEqual(found, checkout)
        url = pcp_missions.serve_product_rc(
            found, state_dir=self.root / "state", package_id=casino.package_id)
        self.addCleanup(lambda: self._stop_rc(self.root / "state", casino.package_id))
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        fetched = pcp_missions._fetch(url)
        self.assertIn("playable finite shoe", fetched)
        self.assertFalse(pcp_missions.is_stub_rc_body(fetched))

    def test_golden_path_admits_controller_mission_and_working_rc(self):
        checkout = self.root / "projects" / "lodus-casino"
        checkout.mkdir(parents=True)
        (checkout / "index.html").write_text(
            "<!doctype html><title>lodus-casino</title><p>finite-shoe higher/lower</p>\n",
            encoding="utf-8")

        def rc_alpha_for(mission):
            found = pcp_missions.resolve_product_checkout(
                self.vault, mission, project_roots=(self.root / "projects",))
            if found is None:
                return ""
            return pcp_missions.serve_product_rc(
                found, state_dir=self.root / "state", package_id=mission.package_id)

        rows = self.plane.advance(rc_alpha_for=rc_alpha_for)
        casino = next(row for row in rows if row.package_id == "lodus-casino")
        self.addCleanup(lambda: self._stop_rc(self.root / "state", "lodus-casino"))
        self.assertEqual(casino.lifecycle, "OWNER_VALIDATION")
        self.assertTrue(casino.rc_alpha_url)
        body = pcp_missions._fetch(casino.rc_alpha_url)
        self.assertIn("finite-shoe higher/lower", body)
        self.assertFalse(pcp_missions.is_stub_rc_body(body))
        mission, created = self.store.submit(
            {
                "work_item_id": "lodus-casino:build",
                "project_id": "lodus-casino",
                "source_pcp": casino.canonical_path,
                "package_digest": casino.package_digest,
                "lifecycle": "IMPLEMENT",
            },
            casino.mission_key,
        )
        self.assertFalse(created)
        self.assertEqual(mission["project_id"], "lodus-casino")


if __name__ == "__main__":
    unittest.main()
