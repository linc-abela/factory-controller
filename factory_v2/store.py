from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from factory_v2.models import Candidate, MissionSnapshot
from factory_v2.states import MissionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    pcp_hash TEXT NOT NULL UNIQUE,
    pcp_json TEXT NOT NULL,
    state TEXT NOT NULL,
    current_candidate_id TEXT,
    current_artifact_id TEXT,
    approved_artifact_id TEXT,
    owner_decision TEXT,
    blocked_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    review_verdict TEXT NOT NULL DEFAULT 'none',
    qa_verdict TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite mission/evidence ledger. Sufficient for restart/replay."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def get_by_hash(self, pcp_hash: str) -> MissionSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE pcp_hash = ?", (pcp_hash,)
            ).fetchone()
            if row is None:
                return None
            return self._snapshot(conn, row)

    def get(self, mission_id: str) -> MissionSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                return None
            return self._snapshot(conn, row)

    def insert_mission(
        self, mission_id: str, pcp_hash: str, pcp: dict[str, Any]
    ) -> MissionSnapshot:
        ts = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM missions WHERE pcp_hash = ?", (pcp_hash,)
            ).fetchone()
            if existing is not None:
                return self._snapshot(conn, existing)
            conn.execute(
                """INSERT INTO missions (
                    mission_id, pcp_hash, pcp_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    mission_id,
                    pcp_hash,
                    json.dumps(pcp, sort_keys=True),
                    MissionState.PCP_APPROVED.value,
                    ts,
                    ts,
                ),
            )
            conn.execute(
                "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    mission_id,
                    "pcp_admitted",
                    json.dumps({"pcp_hash": pcp_hash}),
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return self._snapshot(conn, row)

    def record_candidate(
        self, mission_id: str, candidate_id: str, artifact_id: str, sequence: int
    ) -> None:
        ts = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO candidates (
                    candidate_id, mission_id, artifact_id, sequence, status, created_at
                ) VALUES (?, ?, ?, ?, 'produced', ?)""",
                (candidate_id, mission_id, artifact_id, sequence, ts),
            )
            conn.execute(
                """UPDATE missions SET current_candidate_id = ?, current_artifact_id = ?,
                   state = ?, blocked_reason = NULL, updated_at = ? WHERE mission_id = ?""",
                (
                    candidate_id,
                    artifact_id,
                    MissionState.VERIFYING.value,
                    ts,
                    mission_id,
                ),
            )
            conn.execute(
                "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    mission_id,
                    "candidate_produced",
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "artifact_id": artifact_id,
                            "sequence": sequence,
                        }
                    ),
                    ts,
                ),
            )

    def apply_state(
        self,
        mission_id: str,
        state: MissionState,
        *,
        blocked_reason: str | None = None,
        owner_decision: str | None = None,
        approved_artifact_id: str | None = None,
        event_kind: str,
        payload: dict[str, Any],
        candidate_status: str | None = None,
        review_verdict: str | None = None,
        qa_verdict: str | None = None,
        clear_current: bool = False,
    ) -> MissionSnapshot:
        ts = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                raise KeyError(mission_id)
            if (
                approved_artifact_id is not None
                and row["approved_artifact_id"]
                and row["approved_artifact_id"] != approved_artifact_id
            ):
                raise PermissionError("approved artifact is immutable")
            sets = ["state = ?", "updated_at = ?"]
            args: list[Any] = [state.value, ts]
            if blocked_reason is not None:
                sets.append("blocked_reason = ?")
                args.append(blocked_reason)
            elif state != MissionState.BLOCKED:
                sets.append("blocked_reason = NULL")
            if owner_decision is not None:
                sets.append("owner_decision = ?")
                args.append(owner_decision)
            if approved_artifact_id is not None:
                sets.append("approved_artifact_id = ?")
                args.append(approved_artifact_id)
            if clear_current:
                sets.append("current_candidate_id = NULL")
                sets.append("current_artifact_id = NULL")
            args.append(mission_id)
            conn.execute(
                f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = ?",
                args,
            )
            cand_id = row["current_candidate_id"]
            if cand_id and (
                candidate_status or review_verdict or qa_verdict
            ):
                csets = []
                cargs: list[Any] = []
                if candidate_status:
                    csets.append("status = ?")
                    cargs.append(candidate_status)
                if review_verdict:
                    csets.append("review_verdict = ?")
                    cargs.append(review_verdict)
                if qa_verdict:
                    csets.append("qa_verdict = ?")
                    cargs.append(qa_verdict)
                cargs.append(cand_id)
                conn.execute(
                    f"UPDATE candidates SET {', '.join(csets)} WHERE candidate_id = ?",
                    cargs,
                )
            conn.execute(
                "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (mission_id, event_kind, json.dumps(payload), ts),
            )
            updated = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return self._snapshot(conn, updated)

    def next_sequence(self, mission_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM candidates WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            return int(row["n"]) + 1

    def _snapshot(self, conn: sqlite3.Connection, row: sqlite3.Row) -> MissionSnapshot:
        cands = tuple(
            Candidate(
                candidate_id=c["candidate_id"],
                artifact_id=c["artifact_id"],
                sequence=c["sequence"],
                review_verdict=c["review_verdict"],
                qa_verdict=c["qa_verdict"],
                status=c["status"],
            )
            for c in conn.execute(
                "SELECT * FROM candidates WHERE mission_id = ? ORDER BY sequence",
                (row["mission_id"],),
            )
        )
        events = tuple(
            {
                "event_id": e["event_id"],
                "kind": e["kind"],
                "payload": json.loads(e["payload"]),
                "created_at": e["created_at"],
            }
            for e in conn.execute(
                "SELECT * FROM events WHERE mission_id = ? ORDER BY event_id",
                (row["mission_id"],),
            )
        )
        return MissionSnapshot(
            mission_id=row["mission_id"],
            pcp_hash=row["pcp_hash"],
            state=MissionState(row["state"]),
            current_candidate_id=row["current_candidate_id"],
            current_artifact_id=row["current_artifact_id"],
            approved_artifact_id=row["approved_artifact_id"],
            owner_decision=row["owner_decision"],
            blocked_reason=row["blocked_reason"],
            candidates=cands,
            events=events,
            pcp=json.loads(row["pcp_json"]),
        )
