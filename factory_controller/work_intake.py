"""Admit Factory-maintenance packets into Controller-legal work, then stop.

The always-on supervisor cannot invent missions.  This plane is the missing
join: it observes a work source, claims one packet exclusively, and either
submits it through ``Controller.submit`` or surfaces a named Owner stop.
Done and Blocked are readings of that ledger plus the mission it opened.

A bounded ``cycle`` exists so a host scheduler can invoke this plane without
a person typing a wake-up command.  The cycle is finite: it never sleeps,
never calls itself, and never creates a second execution path.  Selected work
becomes or resumes an ordinary mission through ``Controller.work_once``.

Five absences carry the safety properties:

* **No chat surface.**  Adapters produce ``WorkPacket`` values.  This module
  does not name, fetch, or write an external work exchange.
* **No duplicate claim.**  Claim is a guarded ``UPDATE`` inside
  ``BEGIN IMMEDIATE``.  A second worker either loses the write or sees the
  first worker's token.
* **No duplicate execution.**  Admission uses the packet's work-item identity
  as the mission idempotency key for fixture work (and the Bridge-derived key
  for real work).  A retry collides with the existing row.
* **No new task for a blocked lineage.**  Blocked-as-resumable keeps the same
  ``work_item_id`` / ``lineage_id``.  Unblocking queues that row; it does not
  mint a successor identity.
* **No production authority.**  ``owner_only`` packets are recorded as
  ``owner_required`` and never submitted.  Nothing here can approve, deploy,
  or promote.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import portfolio
from . import routing
from .store import canonical_json, payload_hash
from .work_source import WorkPacket, WorkSource


CONTRACT_VERSION = "factory-controller/work-intake/1.0"

ITEM_STATES = ("queued", "claimed", "admitted", "in_progress", "done",
               "blocked", "owner_required")
RUNNABLE = frozenset({"queued"})
SETTLED = frozenset({"done", "owner_required"})
MISSION_DONE = "completed"
MISSION_BLOCKED = frozenset({"escalated", "failed", "refused", "cancelled"})
MISSION_IN_FLIGHT = frozenset({
    "admitted", "dispatching", "dispatched", "candidate_verified",
    "evaluated", "evidence_sealed",
})
DEFAULT_CYCLE_LEASE_SECONDS = 30.0
DEFAULT_CLAIM_LEASE_SECONDS = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
  work_item_id TEXT PRIMARY KEY,
  lineage_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  owner_only INTEGER NOT NULL,
  owner_reason TEXT NOT NULL,
  state TEXT NOT NULL,
  claim_token TEXT,
  claimed_by TEXT,
  claim_expires_at REAL,
  mission_ref TEXT,
  idempotency_key TEXT,
  blocked_reason TEXT NOT NULL,
  admitted_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS work_items_by_sequence
  ON work_items(sequence, work_item_id);
CREATE INDEX IF NOT EXISTS work_items_by_lineage
  ON work_items(lineage_id, created_at);
CREATE TABLE IF NOT EXISTS work_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS work_events_no_update
BEFORE UPDATE ON work_events
BEGIN SELECT RAISE(ABORT, 'work events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS work_events_no_delete
BEFORE DELETE ON work_events
BEGIN SELECT RAISE(ABORT, 'work events are append-only'); END;
CREATE TABLE IF NOT EXISTS work_intake_cycles (
  cycle_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE,
  previous_cycle_id TEXT,
  worker_id TEXT NOT NULL,
  lease_token TEXT,
  lease_expires_at REAL NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  outcome TEXT,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS work_intake_cycles_open
  ON work_intake_cycles(ended_at, lease_expires_at);
"""


class PolicyError(ValueError):
    """A work-intake declaration the Controller will not store."""


class WorkIntakeRefusal(Exception):
    """A bounded stop, carrying the code and why."""

    def __init__(self, code: str, detail: str, *,
                 work_item_id: str | None = None,
                 cycle_id: str | None = None) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.work_item_id = work_item_id
        self.cycle_id = cycle_id

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "work_item_id": self.work_item_id, "cycle_id": self.cycle_id}


def cycle_reference(previous: str | None, sequence: int, worker_id: str,
                    started_at: float) -> str:
    return "wic_%s" % payload_hash({
        "previous": previous or "none", "sequence": sequence,
        "worker_id": worker_id, "started_at": started_at,
    })[:24]


@dataclass
class CycleReport:
    """Everything one intake cycle did, and everything it declined to do."""

    cycle_id: str
    sequence: int
    outcome: str
    started_at: float
    ended_at: float
    reason: str = "CYCLE_COMPLETED"
    observed: list[str] = field(default_factory=list)
    claimed: str | None = None
    admitted: str | None = None
    advanced: list[dict[str, Any]] = field(default_factory=list)
    owner_required: list[dict[str, Any]] = field(default_factory=list)
    next_action: str = "not_applicable"
    recovered: list[dict[str, Any]] = field(default_factory=list)
    lease_token: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {"cycle_id": self.cycle_id, "sequence": self.sequence,
                "outcome": self.outcome, "reason": self.reason,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "observed": list(self.observed), "claimed": self.claimed,
                "admitted": self.admitted, "advanced": list(self.advanced),
                "owner_required": list(self.owner_required),
                "next_action": self.next_action,
                "recovered": list(self.recovered),
                "contract_version": CONTRACT_VERSION}


class WorkIntakePlane:
    """Durable Factory-maintenance intake, on the mission store's connection."""

    def __init__(self, store, *, clock=None) -> None:
        self._store = store
        self.clock = clock or store.clock
        with store.transaction() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(work_intake_cycles)")}
            if "lease_token" not in columns:
                db.execute(
                    "ALTER TABLE work_intake_cycles ADD COLUMN lease_token TEXT")

    def observe(self, source: WorkSource) -> tuple[dict[str, Any], ...]:
        """Fold packets into the ledger without claiming or submitting."""

        packets = tuple(source.packets())
        seen: set[str] = set()
        for packet in packets:
            if packet.work_item_id in seen:
                raise WorkIntakeRefusal(
                    "WORK_INTAKE_DUPLICATE_IDENTITY",
                    packet.work_item_id, work_item_id=packet.work_item_id)
            seen.add(packet.work_item_id)
        changed: list[dict[str, Any]] = []
        for packet in packets:
            changed.append(self._upsert(packet))
        return tuple(item for item in changed if item.get("changed"))

    def items(self, state: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            if state is None:
                rows = db.execute(
                    "SELECT * FROM work_items ORDER BY sequence, work_item_id"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM work_items WHERE state=? "
                    "ORDER BY sequence, work_item_id",
                    (state,)).fetchall()
        return tuple(self._row(row) for row in rows)

    def item(self, work_item_id: str) -> dict[str, Any] | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (work_item_id,)).fetchone()
        return None if row is None else self._row(row)

    def eligible(self) -> tuple[dict[str, Any], ...]:
        """Lowest-numbered packets a cycle may claim."""

        now = self.clock()
        found: list[dict[str, Any]] = []
        for row in self.items():
            if row["state"] == "queued":
                found.append(row)
            elif (row["state"] == "claimed"
                  and row["claim_expires_at"] is not None
                  and row["claim_expires_at"] <= now):
                found.append(row)
        found.sort(key=lambda item: (item["sequence"], item["work_item_id"]))
        return tuple(found)

    def claim(self, work_item_id: str, worker_id: str, *,
              lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
              ) -> dict[str, Any] | None:
        """Take exclusive ownership of one queued (or expired) item.

        Same-worker retry of a live claim returns the existing row.  A second
        worker loses the guarded update and gets ``None``.
        """

        if not worker_id or lease_seconds <= 0:
            raise PolicyError("a claim names a worker and a positive lease")
        now = self.clock()
        token = str(uuid.uuid4())
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (work_item_id,)).fetchone()
            if row is None:
                return None
            live = (row["state"] == "claimed"
                    and row["claim_expires_at"] is not None
                    and row["claim_expires_at"] > now)
            if live and row["claimed_by"] == worker_id:
                return self._row(row)
            if live:
                return None
            if row["state"] not in RUNNABLE and not (
                    row["state"] == "claimed"
                    and row["claim_expires_at"] is not None
                    and row["claim_expires_at"] <= now):
                return None
            changed = db.execute(
                "UPDATE work_items SET state=?, claim_token=?, claimed_by=?,"
                " claim_expires_at=?, updated_at=? WHERE work_item_id=? AND"
                " (state='queued' OR (state='claimed' AND"
                "  claim_expires_at IS NOT NULL AND claim_expires_at<=?))",
                ("claimed", token, worker_id, now + lease_seconds, now,
                 work_item_id, now)).rowcount
            if changed != 1:
                return None
            self._event(db, work_item_id, "CLAIMED", row["state"], "claimed",
                        {"worker_id": worker_id, "claim_token": token})
            fresh = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (work_item_id,)).fetchone()
        return self._row(fresh)

    def admit(self, work_item_id: str, controller) -> dict[str, Any]:
        """Submit a claimed packet through the ordinary Controller path."""

        row = self.item(work_item_id)
        if row is None:
            raise WorkIntakeRefusal(
                "WORK_INTAKE_UNKNOWN_ITEM", work_item_id,
                work_item_id=work_item_id)
        if row["owner_only"]:
            self._mark(work_item_id, "owner_required",
                       blocked_reason=row["owner_reason"],
                       kind="OWNER_REQUIRED")
            raise WorkIntakeRefusal(
                "WORK_INTAKE_OWNER_REQUIRED",
                row["owner_reason"], work_item_id=work_item_id)
        if row["state"] == "done":
            return {"created": False, "item": self.item(work_item_id),
                    "mission": self._mission(controller, row)}
        if row["state"] not in {"claimed", "admitted", "in_progress", "blocked"}:
            raise WorkIntakeRefusal(
                "WORK_INTAKE_NOT_CLAIMED",
                "item %s is %s" % (work_item_id, row["state"]),
                work_item_id=work_item_id)
        if row["state"] in {"admitted", "in_progress", "blocked"} and row["mission_ref"]:
            mission = controller.store.get(row["mission_ref"])
            return {"created": False, "item": row, "mission": mission}
        payload = dict(row["payload"])
        self._refuse_if_unadmissible(payload, work_item_id)
        key = self._idempotency_key(payload, work_item_id)
        mission, created = controller.submit(payload, key)
        now = self.clock()
        with self._store.transaction() as db:
            db.execute(
                "UPDATE work_items SET state=?, mission_ref=?, idempotency_key=?,"
                " admitted_at=COALESCE(admitted_at, ?), updated_at=?,"
                " blocked_reason=? WHERE work_item_id=?",
                ("admitted", mission["id"], key, now, now, "not_applicable",
                 work_item_id))
            self._event(db, work_item_id, "ADMITTED", row["state"], "admitted",
                        {"mission_ref": mission["id"], "created": created,
                         "idempotency_key": key})
        return {"created": created, "item": self.item(work_item_id),
                "mission": mission}

    def reconcile(self, work_item_id: str, controller) -> dict[str, Any]:
        """Map the bound mission's durable state onto Done/Blocked."""

        row = self.item(work_item_id)
        if row is None:
            raise WorkIntakeRefusal(
                "WORK_INTAKE_UNKNOWN_ITEM", work_item_id,
                work_item_id=work_item_id)
        if row["state"] in SETTLED:
            return row
        mission_ref = row["mission_ref"]
        if not mission_ref:
            return row
        mission = controller.store.get(mission_ref)
        if mission is None:
            return row
        state = mission.get("state")
        if state == MISSION_DONE:
            return self._mark(work_item_id, "done", kind="DONE",
                              detail={"mission_ref": mission_ref,
                                      "mission_state": state})
        if state in MISSION_BLOCKED:
            return self._mark(work_item_id, "blocked",
                              blocked_reason=str(mission.get("terminal_reason")
                                                 or state),
                              kind="BLOCKED",
                              detail={"mission_ref": mission_ref,
                                      "mission_state": state})
        if state in MISSION_IN_FLIGHT and state != "admitted":
            return self._mark(work_item_id, "in_progress", kind="IN_PROGRESS",
                              detail={"mission_ref": mission_ref,
                                      "mission_state": state})
        return row

    def cycle(self, worker_id: str, *, source: WorkSource | None = None,
              controller=None,
              lease_seconds: float = DEFAULT_CYCLE_LEASE_SECONDS,
              execute: bool = True) -> dict[str, Any]:
        """Observe, claim one, admit or escalate, optionally run work_once."""

        claim = self._claim_cycle(worker_id, lease_seconds)
        report = CycleReport(
            cycle_id=claim["cycle_id"], sequence=claim["sequence"],
            outcome="completed", started_at=claim["started_at"],
            ended_at=claim["started_at"], recovered=claim["recovered"],
            lease_token=claim["lease_token"])
        try:
            if controller is None:
                raise WorkIntakeRefusal(
                    "WORK_INTAKE_CONTROLLER_MISSING",
                    "a cycle admits through a Controller",
                    cycle_id=report.cycle_id)
            if source is not None:
                observed = self.observe(source)
                report.observed = [item["work_item_id"] for item in observed]
            for item in self.items():
                if item["state"] in {"admitted", "in_progress", "claimed"}:
                    self.reconcile(item["work_item_id"], controller)
            nxt = self.eligible()
            if not nxt:
                report.outcome = "idle"
                report.reason = "WORK_INTAKE_EMPTY"
                report.next_action = "not_applicable"
                return self._close_cycle(report)
            target = nxt[0]
            claimed = self.claim(target["work_item_id"], worker_id)
            if claimed is None:
                report.outcome = "idle"
                report.reason = "WORK_INTAKE_CLAIM_LOST"
                return self._close_cycle(report)
            report.claimed = claimed["work_item_id"]
            if claimed["owner_only"]:
                marked = self._mark(
                    claimed["work_item_id"], "owner_required",
                    blocked_reason=claimed["owner_reason"],
                    kind="OWNER_REQUIRED")
                report.outcome = "idle"
                report.reason = "WORK_INTAKE_OWNER_REQUIRED"
                report.owner_required.append({
                    "work_item_id": marked["work_item_id"],
                    "owner_reason": marked["owner_reason"]})
                report.next_action = "OWNER_REQUIRED"
                return self._close_cycle(report)
            self.admit(claimed["work_item_id"], controller)
            report.admitted = claimed["work_item_id"]
            bound = self.item(claimed["work_item_id"]) or {}
            bound_mission = bound.get("mission_ref")
            if execute and bound_mission:
                mission = controller.work_once(
                    worker_id, mission_id=bound_mission)
                if mission is not None and mission["id"] == bound_mission:
                    report.advanced.append({
                        "mission_id": mission["id"],
                        "state": mission["state"],
                        "work_item_id": claimed["work_item_id"]})
            self.reconcile(claimed["work_item_id"], controller)
            remaining = self.eligible()
            if remaining:
                report.next_action = "WORK_INTAKE_CONTINUE"
            else:
                report.next_action = "not_applicable"
            report.reason = "CYCLE_COMPLETED"
            return self._close_cycle(report)
        except BaseException:
            self._close_cycle(report, outcome="refused",
                              reason="WORK_INTAKE_CYCLE_ABANDONED")
            raise

    def _upsert(self, packet: WorkPacket) -> dict[str, Any]:
        now = self.clock()
        digest = payload_hash(packet.payload)
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (packet.work_item_id,)).fetchone()
            if existing is None:
                lineage = db.execute(
                    "SELECT work_item_id FROM work_items"
                    " WHERE lineage_id=? ORDER BY created_at LIMIT 1",
                    (packet.lineage_id,)).fetchone()
                if lineage is not None and lineage["work_item_id"] != packet.work_item_id:
                    raise WorkIntakeRefusal(
                        "WORK_INTAKE_LINEAGE_COLLISION",
                        "lineage %s already bound to %s"
                        % (packet.lineage_id, lineage["work_item_id"]),
                        work_item_id=packet.work_item_id)
                state = "blocked" if packet.blocked else "queued"
                db.execute(
                    "INSERT INTO work_items (work_item_id, lineage_id, sequence,"
                    " source_kind, source_ref, payload_hash, payload_json,"
                    " owner_only, owner_reason, state, blocked_reason,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (packet.work_item_id, packet.lineage_id, packet.sequence,
                     packet.source_kind, packet.source_ref, digest,
                     canonical_json(packet.payload), int(packet.owner_only),
                     packet.owner_reason, state,
                     "source_blocked" if packet.blocked else "not_applicable",
                     now, now))
                self._event(db, packet.work_item_id, "OBSERVED", None, state,
                            {"source_ref": packet.source_ref})
                row = db.execute(
                    "SELECT * FROM work_items WHERE work_item_id=?",
                    (packet.work_item_id,)).fetchone()
                parsed = self._row(row)
                parsed["changed"] = True
                return parsed
            current = self._row(existing)
            identity_changed = (
                existing["payload_hash"] != digest
                or existing["lineage_id"] != packet.lineage_id
                or int(existing["sequence"]) != packet.sequence)
            if identity_changed:
                raise WorkIntakeRefusal(
                    "WORK_INTAKE_PACKET_IMMUTABLE",
                    "item %s identity is frozen after observation"
                    % packet.work_item_id,
                    work_item_id=packet.work_item_id)
            nxt = current["state"]
            blocked_reason = current["blocked_reason"]
            kind = None
            if packet.owner_only and current["state"] != "owner_required":
                if current["state"] in {"admitted", "in_progress", "done"}:
                    raise WorkIntakeRefusal(
                        "WORK_INTAKE_PACKET_CHANGED",
                        "item %s already entered the Controller"
                        % packet.work_item_id,
                        work_item_id=packet.work_item_id)
                nxt = "owner_required"
                blocked_reason = packet.owner_reason
                kind = "OWNER_REQUIRED"
            elif (not packet.owner_only
                  and current["state"] == "owner_required"):
                nxt = "queued"
                blocked_reason = "not_applicable"
                kind = "RESUMED"
            elif packet.blocked and current["state"] in {"queued", "blocked"}:
                nxt = "blocked"
                blocked_reason = "source_blocked"
                kind = "BLOCKED"
            elif (not packet.blocked and current["state"] == "blocked"
                  and current["mission_ref"] is None):
                nxt = "queued"
                blocked_reason = "not_applicable"
                kind = "RESUMED"
            db.execute(
                "UPDATE work_items SET source_kind=?,"
                " source_ref=?, owner_only=?,"
                " owner_reason=?, state=?, blocked_reason=?, updated_at=?"
                " WHERE work_item_id=?",
                (packet.source_kind,
                 packet.source_ref, int(packet.owner_only),
                 packet.owner_reason, nxt,
                 blocked_reason, now, packet.work_item_id))
            if kind and nxt != current["state"]:
                self._event(db, packet.work_item_id, kind, current["state"], nxt,
                            {"source_ref": packet.source_ref})
            row = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (packet.work_item_id,)).fetchone()
        parsed = self._row(row)
        parsed["changed"] = (
            nxt != current["state"] or existing["payload_hash"] != digest)
        return parsed

    def _refuse_if_unadmissible(self, payload: Mapping[str, Any],
                                work_item_id: str) -> None:
        project_id = payload.get("project_id")
        with self._store.transaction() as db:
            stopped = db.execute(
                "SELECT emergency_stop FROM portfolio WHERE id=1").fetchone()
            if stopped is not None and stopped["emergency_stop"]:
                raise WorkIntakeRefusal(
                    "WORK_INTAKE_EMERGENCY_STOP",
                    "the portfolio is under an emergency stop",
                    work_item_id=work_item_id)
            if isinstance(project_id, str) and project_id:
                project_row = db.execute(
                    "SELECT state FROM projects WHERE project_id=?",
                    (project_id,)).fetchone()
                if (project_row is None
                        or project_row["state"] not in portfolio.ADMITTING):
                    state = ("unregistered" if project_row is None
                             else project_row["state"])
                    raise WorkIntakeRefusal(
                        "WORK_INTAKE_PROJECT_NOT_ADMITTING",
                        "project %s is %s" % (project_id, state),
                        work_item_id=work_item_id)

    def _idempotency_key(self, payload: Mapping[str, Any],
                         work_item_id: str) -> str:
        mode = payload.get("execution_mode", "fixture")
        if mode != "real":
            return work_item_id
        manifest = payload.get("context_manifest_hash")
        if not isinstance(manifest, str) or not manifest:
            raise WorkIntakeRefusal(
                "WORK_INTAKE_REAL_MANIFEST_MISSING",
                work_item_id, work_item_id=work_item_id)
        return routing.expected_idempotency_key(work_item_id, manifest)

    def _mission(self, controller, row: Mapping[str, Any]):
        ref = row.get("mission_ref")
        if not ref:
            return None
        return controller.store.get(ref)

    def _mark(self, work_item_id: str, state: str, *,
              blocked_reason: str | None = None, kind: str,
              detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        now = self.clock()
        with self._store.transaction() as db:
            current = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (work_item_id,)).fetchone()
            if current is None:
                raise WorkIntakeRefusal(
                    "WORK_INTAKE_UNKNOWN_ITEM", work_item_id,
                    work_item_id=work_item_id)
            reason = (blocked_reason if blocked_reason is not None
                      else current["blocked_reason"])
            if current["state"] == state and current["blocked_reason"] == reason:
                return self._row(current)
            db.execute(
                "UPDATE work_items SET state=?, blocked_reason=?, updated_at=?"
                " WHERE work_item_id=?",
                (state, reason, now, work_item_id))
            self._event(db, work_item_id, kind, current["state"], state,
                        dict(detail or {}))
            row = db.execute(
                "SELECT * FROM work_items WHERE work_item_id=?",
                (work_item_id,)).fetchone()
        return self._row(row)

    def _claim_cycle(self, worker_id: str, lease_seconds: float) -> dict[str, Any]:
        if not worker_id or lease_seconds <= 0:
            raise PolicyError("a cycle names a worker and a positive lease")
        now = self.clock()
        recovered: list[dict[str, Any]] = []
        with self._store.transaction() as db:
            open_rows = db.execute(
                "SELECT * FROM work_intake_cycles WHERE ended_at IS NULL"
                " ORDER BY sequence").fetchall()
            for row in open_rows:
                if row["lease_expires_at"] > now:
                    raise WorkIntakeRefusal(
                        "WORK_INTAKE_CYCLE_IN_FLIGHT",
                        "cycle %s is held by %s"
                        % (row["cycle_id"], row["worker_id"]),
                        cycle_id=row["cycle_id"])
                db.execute(
                    "UPDATE work_intake_cycles SET ended_at=?, outcome=?,"
                    " detail_json=? WHERE cycle_id=?",
                    (now, "idle",
                     canonical_json({"recovered_by": worker_id,
                                     "prior_worker": row["worker_id"]}),
                     row["cycle_id"]))
                recovered.append({"cycle_id": row["cycle_id"],
                                  "prior_worker": row["worker_id"]})
            last = db.execute(
                "SELECT cycle_id, sequence FROM work_intake_cycles"
                " ORDER BY sequence DESC LIMIT 1").fetchone()
            previous = None if last is None else last["cycle_id"]
            sequence = 1 if last is None else int(last["sequence"]) + 1
            cycle_id = cycle_reference(previous, sequence, worker_id, now)
            lease_token = str(uuid.uuid4())
            db.execute(
                "INSERT INTO work_intake_cycles (cycle_id, sequence,"
                " previous_cycle_id, worker_id, lease_token, lease_expires_at,"
                " started_at) VALUES (?,?,?,?,?,?,?)",
                (cycle_id, sequence, previous, worker_id, lease_token,
                 now + lease_seconds, now))
        return {"cycle_id": cycle_id, "sequence": sequence,
                "started_at": now, "recovered": recovered,
                "lease_token": lease_token}

    def _close_cycle(self, report: CycleReport, *, outcome: str | None = None,
                     reason: str | None = None) -> dict[str, Any]:
        if outcome is not None:
            report.outcome = outcome
        if reason is not None:
            report.reason = reason
        report.ended_at = self.clock()
        with self._store.transaction() as db:
            changed = db.execute(
                "UPDATE work_intake_cycles SET ended_at=?, outcome=?,"
                " detail_json=? WHERE cycle_id=? AND lease_token=?"
                " AND ended_at IS NULL",
                (report.ended_at, report.outcome,
                 canonical_json(report.as_row()), report.cycle_id,
                 report.lease_token)).rowcount
            if changed != 1:
                report.outcome = "uncertain"
                report.reason = "WORK_INTAKE_STALE_CYCLE"
                report.next_action = "RECONCILE_UNCERTAIN"
        return report.as_row()

    def _event(self, db, work_item_id: str, kind: str, old: str | None,
               new: str | None, detail: Mapping[str, Any]) -> None:
        db.execute(
            "INSERT INTO work_events (work_item_id, kind, from_state, to_state,"
            " detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (work_item_id, kind, old, new, canonical_json(dict(detail)),
             self.clock()))

    def _row(self, row) -> dict[str, Any]:
        value = dict(row)
        payload = json.loads(value.pop("payload_json"))
        value["payload"] = payload
        value["owner_only"] = bool(value["owner_only"])
        value["contract_version"] = CONTRACT_VERSION
        return value
