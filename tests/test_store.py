"""Unit tests for MissionStore persistence, queue leasing, and step state."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from factory_controller.store import (
    ConflictError,
    LeaseLostError,
    MissionStore,
    canonical_json,
    payload_hash,
)


class MissionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.clock_val = 1000.0
        self.store = MissionStore(self.db_path, clock=lambda: self.clock_val)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_initialization_creates_schema_and_triggers(self) -> None:
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"missions", "attempts", "steps", "events", "schema_meta"}.issubset(tables))
            journal = db.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal.lower(), "wal")

    def test_events_table_is_append_only_via_triggers(self) -> None:
        payload = {"task": "verify"}
        mission, _ = self.store.submit(payload, "key-1")
        history = self.store.history(mission["id"])
        self.assertEqual(len(history), 1)
        event_seq = history[0]["sequence"]

        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute("UPDATE events SET kind='HACKED' WHERE sequence=?", (event_seq,))

        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute("DELETE FROM events WHERE sequence=?", (event_seq,))

    def test_idempotent_submit_and_conflict_refusal(self) -> None:
        payload_a = {"item": "A", "param": 123}
        mission_1, created_1 = self.store.submit(payload_a, "idem-1")
        self.assertTrue(created_1)
        self.assertEqual(mission_1["state"], "admitted")
        self.assertEqual(mission_1["idempotency_key"], "idem-1")

        # Duplicate submit with same payload -> returns existing record
        mission_2, created_2 = self.store.submit(payload_a, "idem-1")
        self.assertFalse(created_2)
        self.assertEqual(mission_1["id"], mission_2["id"])

        # Conflicting submit with different payload -> ConflictError
        payload_b = {"item": "A", "param": 456}
        with self.assertRaises(ConflictError):
            self.store.submit(payload_b, "idem-1")

    def test_invalid_submit_arguments_refuse(self) -> None:
        with self.assertRaises(ValueError):
            self.store.submit({"item": 1}, "")
        with self.assertRaises(ValueError):
            self.store.submit({"item": 1}, "key-valid", max_attempts=0)

    def test_claim_lease_lifecycle(self) -> None:
        self.store.submit({"task": "run-1"}, "key-1")
        self.store.submit({"task": "run-2"}, "key-2")

        claimed_1 = self.store.claim("worker-A", lease_seconds=30)
        self.assertIsNotNone(claimed_1)
        self.assertEqual(claimed_1["state"], "dispatching")
        self.assertEqual(claimed_1["attempt_count"], 1)
        self.assertEqual(claimed_1["lease_owner"], "worker-A")
        self.assertIsNotNone(claimed_1["lease_token"])
        self.assertEqual(claimed_1["lease_expires_at"], 1030.0)

        claimed_2 = self.store.claim("worker-B", lease_seconds=30)
        self.assertIsNotNone(claimed_2)
        self.assertNotEqual(claimed_1["id"], claimed_2["id"])

        claimed_none = self.store.claim("worker-C", lease_seconds=30)
        self.assertIsNone(claimed_none)

    def test_transition_with_valid_and_invalid_lease_tokens(self) -> None:
        mission, _ = self.store.submit({"task": "run-1"}, "key-1")
        claimed = self.store.claim("worker-A", lease_seconds=30)
        m_id = claimed["id"]
        valid_token = claimed["lease_token"]

        with self.assertRaises(LeaseLostError):
            self.store.transition(m_id, "bogus-token", "dispatched")

        self.store.transition(m_id, valid_token, "dispatched", detail={"msg": "dispatched"})
        updated = self.store.get(m_id)
        self.assertEqual(updated["state"], "dispatched")

        self.store.transition(m_id, valid_token, "candidate_verified")
        self.store.transition(m_id, valid_token, "evaluated")
        self.store.transition(m_id, valid_token, "evidence_sealed")
        self.store.transition(m_id, valid_token, "completed", result={"status": "ok"}, release_lease=True)
        finished = self.store.get(m_id)
        self.assertEqual(finished["state"], "completed")
        self.assertIsNone(finished["lease_owner"])
        self.assertEqual(finished["result"], {"status": "ok"})

    def test_renew_lease(self) -> None:
        self.store.submit({"task": "run-1"}, "key-1")
        claimed = self.store.claim("worker-A", lease_seconds=30)
        m_id, token = claimed["id"], claimed["lease_token"]

        self.clock_val += 10.0
        self.store.renew(m_id, token, lease_seconds=45)
        renewed = self.store.get(m_id)
        self.assertEqual(renewed["lease_expires_at"], 1010.0 + 45.0)

        with self.assertRaises(LeaseLostError):
            self.store.renew(m_id, "invalid-token", lease_seconds=45)

    def test_recover_stale_leases(self) -> None:
        self.store.submit({"task": "stale-1"}, "key-stale-1")
        claimed = self.store.claim("worker-dead", lease_seconds=20)
        m_id = claimed["id"]

        # Before lease expiry, recover_stale should not recover it
        recovered_0 = self.store.recover_stale()
        self.assertEqual(recovered_0, 0)

        # Advance clock past lease expiry
        self.clock_val += 25.0
        recovered_1 = self.store.recover_stale()
        self.assertEqual(recovered_1, 1)

        recovered = self.store.get(m_id)
        self.assertEqual(recovered["state"], "admitted")
        self.assertIsNone(recovered["lease_owner"])
        self.assertIsNone(recovered["lease_token"])

    def test_retry_and_exhaustion(self) -> None:
        self.store.submit({"task": "retry-test"}, "key-r", max_attempts=2)
        claimed = self.store.claim("worker-1", lease_seconds=20)
        m_id, token = claimed["id"], claimed["lease_token"]

        # Attempt 1 failed retryably
        state_1 = self.store.retry(m_id, token, "bridge unavailable", delay=5.0)
        self.assertEqual(state_1, "admitted")
        self.assertEqual(self.store.get(m_id)["next_run_at"], 1005.0)

        # Claim attempt 2
        self.clock_val += 6.0
        claimed_2 = self.store.claim("worker-2", lease_seconds=20)
        self.assertIsNotNone(claimed_2)
        token_2 = claimed_2["lease_token"]

        # Attempt 2 failed -> max_attempts reached -> escalated
        state_2 = self.store.retry(m_id, token_2, "bridge dead", delay=5.0)
        self.assertEqual(state_2, "escalated")
        terminal = self.store.get(m_id)
        self.assertEqual(terminal["state"], "escalated")
        self.assertIn("RETRIES_EXHAUSTED", terminal["terminal_reason"])

    def test_begin_and_complete_step(self) -> None:
        self.store.submit({"task": "step-test"}, "key-step")
        claimed = self.store.claim("worker-1", lease_seconds=20)
        m_id, token = claimed["id"], claimed["lease_token"]

        # Step not yet run
        s1 = self.store.begin_step(m_id, token, "dispatch", {"p": 1})
        self.assertEqual(s1["status"], "STARTED")

        # Step in-progress returns STARTED
        s2 = self.store.begin_step(m_id, token, "dispatch", {"p": 1})
        self.assertEqual(s2["status"], "STARTED")

        # Complete step
        self.store.complete_step(m_id, token, "dispatch", {"candidate_sha": "abc"})

        # Subsequent begin_step returns COMPLETED and cached output
        s3 = self.store.begin_step(m_id, token, "dispatch", {"p": 1})
        self.assertEqual(s3["status"], "COMPLETED")
        self.assertEqual(s3["output"], {"candidate_sha": "abc"})

    def test_cancellation(self) -> None:
        m1, _ = self.store.submit({"task": "cancel-ready"}, "key-c1")
        # Cancel ready mission
        self.assertEqual(self.store.cancel(m1["id"]), "cancelled")
        self.assertEqual(self.store.get(m1["id"])["state"], "cancelled")

        m2, _ = self.store.submit({"task": "cancel-claimed"}, "key-c2")
        claimed = self.store.claim("worker-1", lease_seconds=30)
        # Cancel claimed mission sets cancel_requested flag
        self.assertEqual(self.store.cancel(m2["id"]), "dispatching")
        self.assertTrue(self.store.get(m2["id"])["cancel_requested"])

    def test_counts(self) -> None:
        self.store.submit({"task": "1"}, "k1")
        self.store.submit({"task": "2"}, "k2")
        counts = self.store.counts()
        self.assertEqual(counts.get("admitted"), 2)


if __name__ == "__main__":
    unittest.main()
