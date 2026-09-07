"""Durable SQLite claim ledger and audit trail for Autonomous AWE Worker."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .model import (
    CandidateHead,
    CertificationRecord,
    CertificationVerdict,
    ClaimResult,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS awe_claims (
    task_id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    claimed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_awe_claims_state ON awe_claims(state, lease_expires_at);

CREATE TABLE IF NOT EXISTS awe_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_awe_events_task ON awe_events(task_id, sequence);

CREATE TABLE IF NOT EXISTS awe_candidate_heads (
    task_id TEXT PRIMARY KEY,
    branch_name TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    pr_number INTEGER,
    pr_url TEXT,
    frozen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS awe_certifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    role TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    verdict TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    defects_json TEXT NOT NULL DEFAULT '[]',
    certified_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_awe_certs_lookup ON awe_certifications(task_id, head_sha, role);
"""


class AWELedger:
    """Atomic state and lease manager backed by SQLite."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._mem_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", timeout=30.0, isolation_level=None)
            self._mem_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # Autocommit mode, we manage transactions explicitly
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        conn = self._get_connection()
        conn.executescript(SCHEMA)

    def claim(
        self,
        task_id: str,
        lineage_id: str,
        worker_id: str,
        slot_key: str,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> ClaimResult:
        """Atomically claim a task with lease protection."""
        ts = time.time() if now is None else now
        expires_at = ts + lease_seconds

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                "SELECT * FROM awe_claims WHERE task_id = ?;", (task_id,)
            )
            row = cursor.fetchone()

            if row is not None:
                # Existing claim exists
                current_state = row["state"]
                current_expires = float(row["lease_expires_at"])
                current_worker = row["worker_id"]
                current_slot = row["slot_key"]
                token = row["claim_token"]

                if current_state in ("done", "review"):
                    conn.execute("ROLLBACK;")
                    return ClaimResult(
                        ok=False,
                        action="refused",
                        code="TASK_ALREADY_COMPLETED",
                        detail=f"task {task_id} has already been completed",
                    )

                # Check if lease is active
                if ts < current_expires:
                    # Active lease
                    if current_worker == worker_id and current_slot == slot_key:
                        # Resumption / lease extension by the same worker & slot
                        conn.execute(
                            """
                            UPDATE awe_claims
                            SET lease_expires_at = ?, updated_at = ?
                            WHERE task_id = ?;
                            """,
                            (expires_at, ts, task_id),
                        )
                        self._log_event(
                            conn,
                            task_id,
                            "CLAIM_RESUMED",
                            current_state,
                            current_state,
                            {"worker_id": worker_id, "slot_key": slot_key, "lease_expires_at": expires_at},
                            ts,
                        )
                        conn.execute("COMMIT;")
                        return ClaimResult(
                            ok=True,
                            action="resumed",
                            token=token,
                            lease_expires_at=expires_at,
                        )
                    else:
                        conn.execute("ROLLBACK;")
                        return ClaimResult(
                            ok=False,
                            action="refused",
                            code="CLAIM_CONFLICT",
                            detail=f"task {task_id} actively claimed by worker {current_worker} ({current_slot}) until {current_expires}",
                        )

                # Lease has expired: safely reclaim only if the exact slot is free
                occupied = conn.execute(
                    """
                    SELECT task_id FROM awe_claims
                    WHERE slot_key = ?
                      AND task_id != ?
                      AND state NOT IN ('review', 'done', 'blocked')
                      AND lease_expires_at > ?;
                    """,
                    (slot_key, task_id, ts),
                ).fetchone()
                if occupied is not None:
                    conn.execute("ROLLBACK;")
                    return ClaimResult(
                        ok=False,
                        action="refused",
                        code="SLOT_ALREADY_OWNED",
                        detail=(
                            f"exact slot {slot_key} already has a live claim on "
                            f"task {occupied['task_id']}"
                        ),
                    )

                # Lease has expired: safely reclaim
                new_token = uuid.uuid4().hex
                conn.execute(
                    """
                    UPDATE awe_claims
                    SET lineage_id = ?, worker_id = ?, slot_key = ?, claim_token = ?,
                        state = 'in_progress', lease_expires_at = ?, updated_at = ?
                    WHERE task_id = ?;
                    """,
                    (lineage_id, worker_id, slot_key, new_token, expires_at, ts, task_id),
                )
                self._log_event(
                    conn,
                    task_id,
                    "CLAIM_REACQUIRED_AFTER_EXPIRY",
                    current_state,
                    "in_progress",
                    {"worker_id": worker_id, "slot_key": slot_key, "token": new_token, "prev_worker": current_worker},
                    ts,
                )
                conn.execute("COMMIT;")
                return ClaimResult(
                    ok=True,
                    action="claimed",
                    token=new_token,
                    lease_expires_at=expires_at,
                )

            # Fresh claim: one live task per exact slot
            occupied = conn.execute(
                """
                SELECT task_id FROM awe_claims
                WHERE slot_key = ?
                  AND task_id != ?
                  AND state NOT IN ('review', 'done', 'blocked')
                  AND lease_expires_at > ?;
                """,
                (slot_key, task_id, ts),
            ).fetchone()
            if occupied is not None:
                conn.execute("ROLLBACK;")
                return ClaimResult(
                    ok=False,
                    action="refused",
                    code="SLOT_ALREADY_OWNED",
                    detail=(
                        f"exact slot {slot_key} already has a live claim on "
                        f"task {occupied['task_id']}"
                    ),
                )

            # Fresh claim
            new_token = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO awe_claims (
                    task_id, lineage_id, worker_id, slot_key, claim_token,
                    state, lease_expires_at, claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?, ?);
                """,
                (task_id, lineage_id, worker_id, slot_key, new_token, expires_at, ts, ts),
            )
            self._log_event(
                conn,
                task_id,
                "CLAIM_ACQUIRED",
                "queue",
                "in_progress",
                {"worker_id": worker_id, "slot_key": slot_key, "token": new_token},
                ts,
            )
            conn.execute("COMMIT;")
            return ClaimResult(
                ok=True,
                action="claimed",
                token=new_token,
                lease_expires_at=expires_at,
            )

    def heartbeat(
        self,
        task_id: str,
        claim_token: str,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> bool:
        ts = time.time() if now is None else now
        expires_at = ts + lease_seconds
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE awe_claims
                SET lease_expires_at = ?, updated_at = ?
                WHERE task_id = ? AND claim_token = ?;
                """,
                (expires_at, ts, task_id, claim_token),
            )
            return cursor.rowcount > 0

    def complete(
        self,
        task_id: str,
        claim_token: str,
        verdict: str,
        evidence_ref: str = "",
        now: float | None = None,
    ) -> bool:
        ts = time.time() if now is None else now
        result_json = json.dumps({"verdict": verdict, "evidence_ref": evidence_ref, "completed_at": ts})
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE awe_claims
                SET state = 'review', lease_expires_at = 0, updated_at = ?, result_json = ?
                WHERE task_id = ? AND claim_token = ?;
                """,
                (ts, result_json, task_id, claim_token),
            )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    task_id,
                    "TASK_COMPLETED",
                    "in_progress",
                    "done",
                    {"verdict": verdict, "evidence_ref": evidence_ref},
                    ts,
                )
                conn.execute("COMMIT;")
                return True
            conn.execute("ROLLBACK;")
            return False

    def block(
        self,
        task_id: str,
        claim_token: str,
        reason: str,
        detail: str = "",
        now: float | None = None,
    ) -> bool:
        ts = time.time() if now is None else now
        result_json = json.dumps({"reason": reason, "detail": detail, "blocked_at": ts})
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE awe_claims
                SET state = 'blocked', lease_expires_at = 0, updated_at = ?, result_json = ?
                WHERE task_id = ? AND claim_token = ?;
                """,
                (ts, result_json, task_id, claim_token),
            )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    task_id,
                    "TASK_BLOCKED",
                    "in_progress",
                    "blocked",
                    {"reason": reason, "detail": detail},
                    ts,
                )
                conn.execute("COMMIT;")
                return True
            conn.execute("ROLLBACK;")
            return False

    def freeze_candidate(
        self,
        task_id: str,
        branch_name: str,
        head_sha: str,
        base_sha: str,
        pr_number: int | None = None,
        pr_url: str | None = None,
        now: float | None = None,
    ) -> CandidateHead:
        ts = time.time() if now is None else now
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO awe_candidate_heads (
                    task_id, branch_name, head_sha, base_sha, pr_number, pr_url, frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (task_id, branch_name, head_sha, base_sha, pr_number, pr_url, ts),
            )
            self._log_event(
                conn,
                task_id,
                "CANDIDATE_HEAD_FROZEN",
                None,
                None,
                {"branch": branch_name, "head_sha": head_sha, "base_sha": base_sha, "pr": pr_number},
                ts,
            )
        return CandidateHead(
            task_id=task_id,
            branch_name=branch_name,
            head_sha=head_sha,
            base_sha=base_sha,
            pr_number=pr_number,
            pr_url=pr_url,
            frozen_at=ts,
        )

    def get_candidate_head(self, task_id: str) -> CandidateHead | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM awe_candidate_heads WHERE task_id = ?;", (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return CandidateHead(
                task_id=row["task_id"],
                branch_name=row["branch_name"],
                head_sha=row["head_sha"],
                base_sha=row["base_sha"],
                pr_number=row["pr_number"],
                pr_url=row["pr_url"],
                frozen_at=float(row["frozen_at"]),
            )

    def record_certification(
        self,
        task_id: str,
        head_sha: str,
        role: str,
        slot_key: str,
        verdict: CertificationVerdict,
        evidence_ref: str = "",
        defects: list[str] | None = None,
        now: float | None = None,
    ) -> CertificationRecord:
        ts = time.time() if now is None else now
        defects_list = defects or []
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO awe_certifications (
                    task_id, head_sha, role, slot_key, verdict, evidence_ref, defects_json, certified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (task_id, head_sha, role.lower(), slot_key, verdict.value, evidence_ref, json.dumps(defects_list), ts),
            )
            self._log_event(
                conn,
                task_id,
                "CERTIFICATION_RECORDED",
                None,
                None,
                {"head_sha": head_sha, "role": role, "slot": slot_key, "verdict": verdict.value, "defects": defects_list},
                ts,
            )
        return CertificationRecord(
            task_id=task_id,
            head_sha=head_sha,
            role=role.lower(),
            slot_key=slot_key,
            verdict=verdict,
            evidence_ref=evidence_ref,
            defects=defects_list,
            certified_at=ts,
        )

    def get_certifications(self, task_id: str, head_sha: str) -> list[CertificationRecord]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM awe_certifications
                WHERE task_id = ? AND head_sha = ?
                ORDER BY certified_at ASC;
                """,
                (task_id, head_sha),
            )
            records = []
            for row in cursor.fetchall():
                records.append(
                    CertificationRecord(
                        task_id=row["task_id"],
                        head_sha=row["head_sha"],
                        role=row["role"],
                        slot_key=row["slot_key"],
                        verdict=CertificationVerdict(row["verdict"]),
                        evidence_ref=row["evidence_ref"],
                        defects=json.loads(row["defects_json"]),
                        certified_at=float(row["certified_at"]),
                    )
                )
            return records

    def get_claim(self, task_id: str) -> dict[str, Any] | None:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM awe_claims WHERE task_id = ?;", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def get_active_claims(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = time.time() if now is None else now
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM awe_claims
                WHERE state = 'in_progress' AND lease_expires_at > ?
                ORDER BY claimed_at ASC;
                """,
                (ts,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def clean_expired_leases(self, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE awe_claims
                SET state = 'stale_expired'
                WHERE state = 'in_progress' AND lease_expires_at <= ?;
                """,
                (ts,),
            )
            return cursor.rowcount

    def _log_event(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        kind: str,
        from_state: str | None,
        to_state: str | None,
        detail: dict[str, Any],
        ts: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO awe_events (task_id, kind, from_state, to_state, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (task_id, kind, from_state, to_state, json.dumps(detail), ts),
        )
