from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from factory_v2.canonical import now_iso
from factory_v2.models import Candidate, CandidateIdentity, MissionSnapshot
from factory_v2.states import MissionState

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    pcp_hash TEXT NOT NULL UNIQUE,
    pcp_json TEXT NOT NULL,
    state TEXT NOT NULL,
    current_candidate_json TEXT,
    approved_candidate_json TEXT,
    rc_id TEXT,
    owner_decision TEXT,
    blocked_reason TEXT,
    hermes_session_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    rework_sequence INTEGER NOT NULL DEFAULT 0,
    owner_history_json TEXT NOT NULL DEFAULT '[]',
    rework_history_json TEXT NOT NULL DEFAULT '[]',
    active_stage TEXT,
    current_work_item_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    hermes_session_id TEXT,
    grok_session_ref TEXT,
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


def _identity(data: dict[str, str] | None) -> CandidateIdentity | None:
    if not data:
        return None
    return CandidateIdentity(
        candidate_id=data["candidate_id"],
        source_revision=data["source_revision"],
        artifact_hash=data["artifact_hash"],
        artifact_uri=data["artifact_uri"],
    )


class Store:
    """SQLite mission/evidence ledger. Sufficient for restart/replay."""

    def __init__(self, path):
        from pathlib import Path

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
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(missions)").fetchall()}
            if "active_stage" not in cols:
                conn.execute("ALTER TABLE missions ADD COLUMN active_stage TEXT")
            if "current_work_item_json" not in cols:
                conn.execute("ALTER TABLE missions ADD COLUMN current_work_item_json TEXT")

    def get_by_hash(self, pcp_hash: str) -> MissionSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE pcp_hash = ?", (pcp_hash,)
            ).fetchone()
            return None if row is None else self._snapshot(conn, row)

    def get(self, mission_id: str) -> MissionSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return None if row is None else self._snapshot(conn, row)

    def list_missions(self) -> tuple[MissionSnapshot, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM missions ORDER BY created_at, mission_id"
            ).fetchall()
            return tuple(self._snapshot(conn, row) for row in rows)

    def insert_mission(
        self, mission_id: str, lineage_id: str, pcp_hash: str, pcp: dict[str, Any]
    ) -> MissionSnapshot:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM missions WHERE pcp_hash = ?", (pcp_hash,)
            ).fetchone()
            if existing is not None:
                return self._snapshot(conn, existing)
            conn.execute(
                """INSERT INTO missions (
                    mission_id, lineage_id, pcp_hash, pcp_json, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mission_id,
                    lineage_id,
                    pcp_hash,
                    json.dumps(pcp, sort_keys=True),
                    MissionState.PCP_APPROVED.value,
                    ts,
                    ts,
                ),
            )
            conn.execute(
                "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (mission_id, "pcp_admitted", json.dumps({"pcp_hash": pcp_hash}), ts),
            )
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return self._snapshot(conn, row)

    def record_candidate(
        self,
        mission_id: str,
        identity: CandidateIdentity,
        sequence: int,
        *,
        attempt_id: str,
        hermes_session_id: str,
        grok_session_ref: str,
    ) -> MissionSnapshot:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO candidates (
                    candidate_id, mission_id, source_revision, artifact_hash, artifact_uri,
                    sequence, attempt_id, hermes_session_id, grok_session_ref, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'produced', ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    source_revision = excluded.source_revision,
                    artifact_hash = excluded.artifact_hash,
                    artifact_uri = excluded.artifact_uri,
                    sequence = excluded.sequence,
                    attempt_id = excluded.attempt_id,
                    hermes_session_id = excluded.hermes_session_id,
                    grok_session_ref = excluded.grok_session_ref,
                    status = 'produced',
                    review_verdict = 'none',
                    qa_verdict = 'none',
                    created_at = excluded.created_at""",
                (
                    identity.candidate_id,
                    mission_id,
                    identity.source_revision,
                    identity.artifact_hash,
                    identity.artifact_uri,
                    sequence,
                    attempt_id,
                    hermes_session_id,
                    grok_session_ref,
                    ts,
                ),
            )
            conn.execute(
                """UPDATE missions SET current_candidate_json = ?, state = ?,
                   blocked_reason = NULL, hermes_session_id = ?, attempt_number = ?,
                   active_stage = 'verifying', current_work_item_json = NULL,
                   updated_at = ? WHERE mission_id = ?""",
                (
                    json.dumps(identity.as_dict()),
                    MissionState.VERIFYING.value,
                    hermes_session_id,
                    sequence,
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
                            "candidate": identity.as_dict(),
                            "sequence": sequence,
                            "hermes_session_id": hermes_session_id,
                            "grok_session_ref": grok_session_ref,
                        }
                    ),
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return self._snapshot(conn, row)

    def record_progress(
        self,
        mission_id: str,
        *,
        active_stage: str,
        current_work_item: dict[str, Any] | None = None,
        event_kind: str = "engineering_progress",
        payload: dict[str, Any] | None = None,
    ) -> MissionSnapshot:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sets = ["active_stage = ?", "updated_at = ?"]
            args: list[Any] = [active_stage, ts]
            if current_work_item is not None:
                sets.append("current_work_item_json = ?")
                args.append(json.dumps(current_work_item))
            args.append(mission_id)
            conn.execute(f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = ?", args)
            ev_payload = dict(payload) if payload else {}
            ev_payload["active_stage"] = active_stage
            if current_work_item:
                ev_payload["work_item"] = current_work_item
            conn.execute(
                "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (mission_id, event_kind, json.dumps(ev_payload), ts),
            )
            updated = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            return self._snapshot(conn, updated)

    def apply_state(
        self,
        mission_id: str,
        state: MissionState,
        *,
        blocked_reason: str | None = None,
        owner_decision: str | None = None,
        approved: CandidateIdentity | None = None,
        rc_id: str | None = None,
        event_kind: str,
        payload: dict[str, Any],
        candidate_status: str | None = None,
        review_verdict: str | None = None,
        qa_verdict: str | None = None,
        clear_current: bool = False,
        hermes_session_id: str | None = None,
        owner_entry: dict[str, Any] | None = None,
        rework_entry: dict[str, Any] | None = None,
        attempt_number: int | None = None,
        rework_sequence: int | None = None,
        active_stage: str | None = None,
        current_work_item: dict[str, Any] | None = None,
        clear_work_item: bool = False,
    ) -> MissionSnapshot:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                raise KeyError(mission_id)
            if approved is not None and row["approved_candidate_json"]:
                existing = json.loads(row["approved_candidate_json"])
                if existing != approved.as_dict():
                    raise PermissionError("approved candidate tuple is immutable")
            sets = ["state = ?", "updated_at = ?"]
            args: list[Any] = [state.value, ts]
            if active_stage is not None:
                sets.append("active_stage = ?")
                args.append(active_stage)
            else:
                sets.append("active_stage = ?")
                args.append(state.value.lower())
            if current_work_item is not None:
                sets.append("current_work_item_json = ?")
                args.append(json.dumps(current_work_item))
            elif clear_work_item:
                sets.append("current_work_item_json = NULL")
            if blocked_reason is not None:
                sets.append("blocked_reason = ?")
                args.append(blocked_reason)
            elif state != MissionState.BLOCKED:
                sets.append("blocked_reason = NULL")
            if owner_decision is not None:
                sets.append("owner_decision = ?")
                args.append(owner_decision)
            if approved is not None:
                sets.append("approved_candidate_json = ?")
                args.append(json.dumps(approved.as_dict()))
            if rc_id is not None:
                sets.append("rc_id = ?")
                args.append(rc_id)
            if hermes_session_id is not None:
                sets.append("hermes_session_id = ?")
                args.append(hermes_session_id)
            if attempt_number is not None:
                sets.append("attempt_number = ?")
                args.append(attempt_number)
            if rework_sequence is not None:
                sets.append("rework_sequence = ?")
                args.append(rework_sequence)
            owner_history = json.loads(row["owner_history_json"])
            rework_history = json.loads(row["rework_history_json"])
            if owner_entry is not None:
                owner_history.append(owner_entry)
                sets.append("owner_history_json = ?")
                args.append(json.dumps(owner_history))
            if rework_entry is not None:
                rework_history.append(rework_entry)
                sets.append("rework_history_json = ?")
                args.append(json.dumps(rework_history))
            if clear_current:
                sets.append("current_candidate_json = NULL")
            args.append(mission_id)
            conn.execute(
                f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = ?",
                args,
            )
            current = json.loads(row["current_candidate_json"] or "null")
            cand_id = None if not current else current["candidate_id"]
            if cand_id and (candidate_status or review_verdict or qa_verdict):
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
                identity=CandidateIdentity(
                    candidate_id=c["candidate_id"],
                    source_revision=c["source_revision"],
                    artifact_hash=c["artifact_hash"],
                    artifact_uri=c["artifact_uri"],
                ),
                sequence=c["sequence"],
                attempt_id=c["attempt_id"],
                hermes_session_id=c["hermes_session_id"] or "",
                grok_session_ref=c["grok_session_ref"] or "",
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
        current = json.loads(row["current_candidate_json"] or "null")
        approved = json.loads(row["approved_candidate_json"] or "null")
        active_stage = row["active_stage"] if "active_stage" in row.keys() else None
        cwi_raw = row["current_work_item_json"] if "current_work_item_json" in row.keys() else None
        current_work_item = json.loads(cwi_raw) if cwi_raw else None
        return MissionSnapshot(
            mission_id=row["mission_id"],
            lineage_id=row["lineage_id"],
            pcp_hash=row["pcp_hash"],
            state=MissionState(row["state"]),
            current=_identity(current),
            approved=_identity(approved),
            rc_id=row["rc_id"],
            owner_decision=row["owner_decision"],
            blocked_reason=row["blocked_reason"],
            hermes_session_id=row["hermes_session_id"],
            attempt_number=row["attempt_number"],
            rework_sequence=row["rework_sequence"],
            owner_history=tuple(json.loads(row["owner_history_json"])),
            rework_history=tuple(json.loads(row["rework_history_json"])),
            candidates=cands,
            events=events,
            pcp=json.loads(row["pcp_json"]),
            active_stage=active_stage,
            current_work_item=current_work_item,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
