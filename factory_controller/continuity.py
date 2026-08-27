"""Portable, restart-safe Work Batons at deterministic safe boundaries.

The baton is continuity plumbing, not a scheduler.  It records enough identity
for a compatible runtime to resume a bounded run and refuses ambiguity about
repository, project, head, effects, capability or capacity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from . import capacity

SCHEMA_VERSION = "factory.controller.work_baton.v1"
SAFE_BOUNDARIES = frozenset({"pre_dispatch", "post_dispatch_reconciled"})
REQUIRED_FIELDS = frozenset({
    "schema_version", "source", "head_sha", "project_id", "run_id", "lane_id",
    "worktree", "branch", "safe_boundary", "idempotency_key",
    "required_capabilities", "compatible_profiles", "capacity_observation",
    "evaluator", "uncertainty", "issued_at",
})


class BatonRefusal(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail

    def as_row(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def issue_payload(**fields: Any) -> dict[str, Any]:
    body = {"schema_version": SCHEMA_VERSION, **fields}
    _validate_body(body)
    digest = _hash(body)
    return {**body, "baton_id": "wb_" + digest[:32], "baton_hash": digest}


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BatonRefusal("BATON_MALFORMED", "work baton must be an object")
    body = {key: item for key, item in value.items()
            if key not in ("baton_id", "baton_hash")}
    _validate_body(body)
    digest = _hash(body)
    if value.get("baton_hash") != digest or value.get("baton_id") != "wb_" + digest[:32]:
        raise BatonRefusal("BATON_FORGED", "work baton identity does not match payload")
    return dict(value)


def _validate_body(body: Mapping[str, Any]) -> None:
    missing = sorted(REQUIRED_FIELDS - set(body))
    if missing:
        raise BatonRefusal("BATON_FIELD_MISSING", "missing fields: %s" % missing)
    if body.get("schema_version") != SCHEMA_VERSION:
        raise BatonRefusal("BATON_SCHEMA_UNSUPPORTED", "unsupported work baton schema")
    for key in ("source", "project_id", "run_id", "lane_id", "worktree",
                "branch", "idempotency_key", "evaluator"):
        if not isinstance(body.get(key), str) or not body[key].strip():
            raise BatonRefusal("BATON_FIELD_INVALID", "%s must be a non-empty string" % key)
    head = body.get("head_sha")
    if not isinstance(head, str) or len(head) != 40 or any(
            character not in "0123456789abcdef" for character in head):
        raise BatonRefusal("BATON_HEAD_INVALID", "head_sha must be a lowercase Git SHA")
    if body.get("safe_boundary") not in SAFE_BOUNDARIES:
        raise BatonRefusal("BATON_BOUNDARY_UNSAFE", "checkpoint is not at a safe boundary")
    uncertainty = body.get("uncertainty")
    if not isinstance(uncertainty, dict):
        raise BatonRefusal("BATON_UNCERTAINTY_INVALID", "uncertainty must be explicit")
    effect = uncertainty.get("irreversible_effect")
    if body["safe_boundary"] == "pre_dispatch" and effect != "none":
        raise BatonRefusal("BATON_EFFECT_UNCERTAIN", "pre-dispatch baton cannot carry an effect")
    if body["safe_boundary"] == "post_dispatch_reconciled" and effect != "reconciled":
        raise BatonRefusal("BATON_EFFECT_UNCERTAIN",
                           "post-dispatch baton requires reconciled irreversible effects")
    for key in ("required_capabilities", "compatible_profiles"):
        values = body.get(key)
        if (not isinstance(values, list) or not values
                or not all(isinstance(item, str) and item for item in values)
                or len(set(values)) != len(values)):
            raise BatonRefusal("BATON_FIELD_INVALID", "%s must be distinct names" % key)
    observation = body.get("capacity_observation")
    required_capacity = {"runtime_id", "state", "observed_at", "source", "source_ref"}
    if (not isinstance(observation, dict)
            or not required_capacity.issubset(observation)
            or observation.get("state") not in capacity.CAPACITY_STATES):
        raise BatonRefusal("BATON_CAPACITY_INVALID",
                           "capacity observation is missing durable Phase-1 facts")
    if not isinstance(body.get("issued_at"), (int, float)) \
            or isinstance(body["issued_at"], bool):
        raise BatonRefusal("BATON_FIELD_INVALID", "issued_at must be numeric")


class WorkBatonStore:
    """An immutable issue ledger with exactly-once consumption."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path, self.clock = str(path), clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_batons (
                  baton_id TEXT PRIMARY KEY,
                  baton_hash TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('issued','consumed')),
                  consumed_by TEXT,
                  consumed_at REAL,
                  created_at REAL NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def issue(self, baton: Mapping[str, Any]) -> dict[str, Any]:
        row = validate(baton)
        encoded = _canonical(row)
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload_json, state FROM work_batons WHERE baton_id=?",
                (row["baton_id"],)).fetchone()
            if existing:
                if existing["payload_json"] != encoded:
                    raise BatonRefusal("BATON_CONFLICT", "baton id already has another payload")
                return {**row, "state": existing["state"], "replayed": True}
            db.execute("INSERT INTO work_batons VALUES (?,?,?,?,?,?,?)",
                       (row["baton_id"], row["baton_hash"], encoded, "issued",
                        None, None, self.clock()))
        return {**row, "state": "issued", "replayed": False}

    def consume(self, baton: Mapping[str, Any], *, target_profile: str,
                project_id: str, source: str, head_sha: str,
                capabilities: list[str], capacity_reading: Mapping[str, Any],
                now: float | None = None) -> dict[str, Any]:
        row = validate(baton)
        if row["project_id"] != project_id or row["source"] != source:
            raise BatonRefusal("BATON_CROSS_PROJECT", "baton project/source differs")
        if row["head_sha"] != head_sha:
            raise BatonRefusal("BATON_STALE_HEAD", "baton head differs from checkout head")
        if target_profile not in row["compatible_profiles"]:
            raise BatonRefusal("BATON_RUNTIME_INCOMPATIBLE", "runtime is not compatible")
        if not set(row["required_capabilities"]).issubset(capabilities):
            raise BatonRefusal("BATON_CAPABILITY_MISSING", "runtime lacks required capability")
        if not isinstance(capacity_reading, dict):
            raise BatonRefusal("BATON_CAPACITY_UNUSABLE", "capacity reading is malformed")
        reading_provenance = all(
            isinstance(capacity_reading.get(key), str)
            and capacity_reading[key] not in ("", "unknown", "not_applicable",
                                              "not_run", "not_measurable")
            for key in ("source", "source_ref"))
        if (capacity_reading.get("usable") is not True
                or capacity_reading.get("state") not in capacity.USABLE
                or not reading_provenance):
            raise BatonRefusal("BATON_CAPACITY_UNUSABLE",
                               str(capacity_reading.get("reason", "capacity is unknown")))
        if capacity_reading.get("runtime_id") != target_profile:
            raise BatonRefusal("BATON_CAPACITY_MISMATCH", "capacity belongs to another runtime")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute(
                "SELECT payload_json, state, consumed_by FROM work_batons WHERE baton_id=?",
                (row["baton_id"],)).fetchone()
            if stored is None or stored["payload_json"] != _canonical(row):
                raise BatonRefusal("BATON_NOT_ISSUED", "baton is absent from durable store")
            if stored["state"] == "consumed":
                if stored["consumed_by"] == target_profile:
                    db.commit()
                    return {"baton_id": row["baton_id"], "state": "consumed",
                            "consumed_by": target_profile, "replayed": True}
                raise BatonRefusal("BATON_ALREADY_CONSUMED", "another runtime consumed baton")
            db.execute("UPDATE work_batons SET state='consumed', consumed_by=?, consumed_at=? "
                       "WHERE baton_id=? AND state='issued'",
                       (target_profile, self.clock(), row["baton_id"]))
            db.commit()
        return {"baton_id": row["baton_id"], "state": "consumed",
                "consumed_by": target_profile, "replayed": False}

    def inspect(self, baton_id: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            if baton_id:
                rows = db.execute("SELECT * FROM work_batons WHERE baton_id=?",
                                  (baton_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM work_batons ORDER BY created_at, baton_id").fetchall()
        batons = []
        for item in rows:
            payload = json.loads(item["payload_json"])
            batons.append({"baton_id": item["baton_id"], "state": item["state"],
                           "project_id": payload["project_id"], "run_id": payload["run_id"],
                           "lane_id": payload["lane_id"], "head_sha": payload["head_sha"],
                           "safe_boundary": payload["safe_boundary"],
                           "compatible_profiles": payload["compatible_profiles"],
                           "consumed_by": item["consumed_by"],
                           "created_at": item["created_at"],
                           "consumed_at": item["consumed_at"]})
        return {"schema_version": SCHEMA_VERSION, "count": len(batons), "batons": batons}
