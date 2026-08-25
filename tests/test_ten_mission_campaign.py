"""Ten-mission unattended campaign validation for Controller v1."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factory_controller.adapter import JsonProcessAdapter
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.safe_provider import main as safe_provider_main
from factory_controller.store import MissionStore


class TenMissionCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "campaign.db"
        self.store = MissionStore(self.db_path)
        import sys
        self.adapter = JsonProcessAdapter([sys.executable, "-m", "factory_controller.safe_provider"])
        self.controller = Controller(self.store, self.adapter, retry_policy=RetryPolicy(max_attempts=3))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ten_consecutive_unattended_missions(self) -> None:
        mission_ids: list[str] = []
        # 1. Submit 10 missions
        for i in range(1, 11):
            payload = {
                "work_item_id": f"CAMPAIGN-MISSION-{i}",
                "capability": "prototype",
                "repository": f"disposable-lab-{i}",
                "iteration": i,
            }
            m, created = self.controller.submit(payload, f"campaign:key:{i}")
            self.assertTrue(created)
            self.assertEqual(m["state"], "admitted")
            mission_ids.append(m["id"])

        self.assertEqual(len(mission_ids), 10)
        self.assertEqual(self.store.counts().get("admitted"), 10)

        # 2. Execute all 10 unattended
        completed_results = []
        for i in range(10):
            res = self.controller.work_once(f"unattended-worker-{i % 2 + 1}")
            self.assertIsNotNone(res)
            self.assertEqual(res["state"], "completed")
            completed_results.append(res)

        # 3. Verify store state after 10 missions
        counts = self.store.counts()
        self.assertEqual(counts.get("completed"), 10)
        self.assertIsNone(counts.get("admitted"))
        self.assertIsNone(counts.get("dispatching"))
        self.assertIsNone(counts.get("escalated"))
        self.assertIsNone(counts.get("failed"))

        # 4. Verify candidate SHAs, evidence pointers, and history for each
        seen_candidates = set()
        seen_evidence = set()
        for res in completed_results:
            m_id = res["id"]
            self.assertEqual(res["state"], "completed")
            self.assertEqual(res["attempt_count"], 1)

            cand_sha = res["result"]["dispatch"]["candidate_sha"]
            self.assertIsNotNone(cand_sha)
            self.assertNotIn(cand_sha, seen_candidates)
            seen_candidates.add(cand_sha)

            ev_ptr = res["result"]["evidence"]["evidence_pointer"]
            self.assertIsNotNone(ev_ptr)
            self.assertNotIn(ev_ptr, seen_evidence)
            seen_evidence.add(ev_ptr)

            history = self.store.history(m_id)
            self.assertTrue(len(history) >= 5)
            self.assertEqual(history[0]["kind"], "SUBMITTED_ADMITTED")

        # 5. Subsequent work_once finds queue empty
        self.assertIsNone(self.controller.work_once("idle-worker"))


if __name__ == "__main__":
    unittest.main()
