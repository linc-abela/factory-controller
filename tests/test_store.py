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
        self.assertEqual(mission_1["state"], "READY")
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
        self.assertEqual(claimed_1["state"], "CLAIMED")
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
            self.store.transition(m_id, "bogus-token", "IN_PROGRESS")

        self.store.transition(m_id, valid_token, "IN_PROGRESS", detail={"msg": "starting"})
        updated = self.store.get(m_id)
        self.assertEqual(updated["state"], "IN_PROGRESS")

        # Complete mission to DONE
        self.store.transition(m_id, valid_token, "DONE", result={"status": "ok"}, release_lease=True)
        finished = self.store.get(m_id)
        self.assertEqual(finished["state"], "DONE")
        self.assertIsNone(finished["lease_owner"])
        self.assertEqual(finished["result"], {"status": "ok"})

    def test_renew_lease(self) -> None:
        self.store.submit({"task": "run-1"}, "key-1")
        claimed = self.store.claim("worker-A", lease_seconds=30)
        m_id, token = claimed["id"], claimed["lease_token"]

        self.clock_val += 10.0
        self.store.renew(m_id, token, lease_seconds=45)
        updated = self.store.get(m_id)
        self.assertEqual(updated["lease_expires_at"], 1055.0)

        with self.assertRaises(LeaseLostError):
            self.store.renew(m_id, "wrong-token", lease_seconds=45)

    def test_recover_stale_leases(self) -> None:
        self.store.submit({"task": "run-1"}, "key-1")
        claimed = self.store.claim("worker-A", lease_seconds=30)
        m_id = claimed["id"]

        # Before expiry: 0 recovered
        self.assertEqual(self.store.recover_stale(), 0)

        # After expiry: recovered to READY
        self.clock_val += 31.0
        self.assertEqual(self.store.recover_stale(), 1)
        recovered = self.store.get(m_id)
        self.assertEqual(recovered["state"], "READY")
        self.assertIsNone(recovered["lease_owner"])

        # Can now be claimed by worker-B
        claimed_again = self.store.claim("worker-B", lease_seconds=30)
        self.assertIsNotNone(claimed_again)
        self.assertEqual(claimed_again["id"], m_id)
        self.assertEqual(claimed_again["attempt_count"], 2)

    def test_retry_and_exhaustion(self) -> None:
        self.store.submit({"task": "flaky"}, "key-flaky", max_attempts=2)
        claimed_1 = self.store.claim("w1", lease_seconds=30)
        m_id, tok_1 = claimed_1["id"], claimed_1["lease_token"]

        # Attempt 1 failed -> schedules retry
        state_1 = self.store.retry(m_id, tok_1, "transient error", delay=10.0)
        self.assertEqual(state_1, "READY")
        self.assertEqual(self.store.get(m_id)["next_run_at"], 1010.0)

        # Before delay expires, cannot claim
        self.assertIsNone(self.store.claim("w1"))

        # After delay, claim attempt 2
        self.clock_val = 1011.0
        claimed_2 = self.store.claim("w2", lease_seconds=30)
        self.assertIsNotNone(claimed_2)
        self.assertEqual(claimed_2["attempt_count"], 2)
        tok_2 = claimed_2["lease_token"]

        # Attempt 2 failed -> max_attempts reached -> transitions to BLOCKED
        state_2 = self.store.retry(m_id, tok_2, "persistent error", delay=10.0)
        self.assertEqual(state_2, "BLOCKED")
        blocked = self.store.get(m_id)
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertTrue("RETRIES_EXHAUSTED" in blocked["terminal_reason"])

    def test_cancellation(self) -> None:
        # Cancel READY mission
        m1, _ = self.store.submit({"task": "c1"}, "k1")
        self.assertEqual(self.store.cancel(m1["id"]), "CANCELLED")
        self.assertEqual(self.store.get(m1["id"])["state"], "CANCELLED")

        # Cancel CLAIMED/IN_PROGRESS mission
        m2, _ = self.store.submit({"task": "c2"}, "k2")
        claimed = self.store.claim("w1", lease_seconds=30)
        self.assertEqual(self.store.cancel(m2["id"]), "CLAIMED")
        self.assertTrue(self.store.get(m2["id"])["cancel_requested"])

    def test_step_memoization_and_replay_protection(self) -> None:
        self.store.submit({"task": "steps"}, "k-step")
        claimed = self.store.claim("w1", lease_seconds=30)
        m_id, tok = claimed["id"], claimed["lease_token"]

        # Step 1: begin and complete
        s1 = self.store.begin_step(m_id, tok, "step1", {"arg": 1})
        self.assertEqual(s1["status"], "STARTED")
        self.store.complete_step(m_id, tok, "step1", {"out": "ok"})

        # Subsequent call with same input returns completed output
        s1_repeat = self.store.begin_step(m_id, tok, "step1", {"arg": 1})
        self.assertEqual(s1_repeat["status"], "COMPLETED")
        self.assertEqual(s1_repeat["output"], {"out": "ok"})

        # Altered input raises ConflictError
        with self.assertRaises(ConflictError):
            self.store.begin_step(m_id, tok, "step1", {"arg": 2})

    def test_counts(self) -> None:
        self.store.submit({"t": 1}, "c1")
        self.store.submit({"t": 2}, "c2")
        counts = self.store.counts()
        self.assertEqual(counts.get("READY"), 2)


if __name__ == "__main__":
    unittest.main()
