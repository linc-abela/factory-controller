"""Controller-owned multi-PCP mission queue.

The trigger is a promoted PCP artifact in the canonical Vault product path.
Notion is never consulted. Duplicate detection is path + package digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import pcp

CONTRACT_VERSION = "factory-controller/pcp-missions/1.0"
PROMOTED_DIR = "PRODUCTS"
DRAFT_MARKERS = ("-draft", "/prototypes/")
HANDOFF_ONLY_FIELDS = frozenset({"g5_promotion", "factory_handoff"})

LIFECYCLE = (
    "DETECTED",
    "CLARITY_REQUIRED",
    "IMPLEMENT",
    "FUNCTIONAL_E2E",
    "RC_ALPHA",
    "OWNER_VALIDATION",
    "CERTIFY",
    "RC_BETA",
    "OWNER_SIGNOFF",
    "PRODUCTION",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pcp_missions (
  mission_key TEXT PRIMARY KEY,
  package_id TEXT NOT NULL,
  package_version INTEGER NOT NULL,
  package_digest TEXT NOT NULL,
  canonical_path TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  hold INTEGER NOT NULL DEFAULT 0,
  lifecycle TEXT NOT NULL,
  worker_lease TEXT,
  lease_expires_at REAL,
  rc_alpha_url TEXT NOT NULL DEFAULT '',
  rc_beta_url TEXT NOT NULL DEFAULT '',
  clarification TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pcp_missions_by_package
  ON pcp_missions(package_id, package_version);
"""


@dataclass(frozen=True)
class PCPMission:
    mission_key: str
    package_id: str
    package_version: int
    package_digest: str
    canonical_path: str
    lifecycle: str
    hold: bool = False
    rc_alpha_url: str = ""
    rc_beta_url: str = ""
    clarification: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "mission_key": self.mission_key,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "package_digest": self.package_digest,
            "canonical_path": self.canonical_path,
            "lifecycle": self.lifecycle,
            "hold": self.hold,
            "rc_alpha_url": self.rc_alpha_url,
            "rc_beta_url": self.rc_beta_url,
            "clarification": self.clarification,
            "contract_version": CONTRACT_VERSION,
        }


def is_promoted_pcp_path(path: Path, vault_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return False
    if not relative.startswith(PROMOTED_DIR + "/"):
        return False
    if any(marker in relative for marker in DRAFT_MARKERS):
        return False
    name = path.name
    return name.startswith("pcp") and name.endswith(".json")


def discover_promoted_pcps(vault_root: str | Path) -> tuple[Path, ...]:
    root = Path(vault_root)
    products = root / PROMOTED_DIR
    if not products.is_dir():
        return ()
    found = [
        path for path in sorted(products.glob("*/pcp*.json"))
        if is_promoted_pcp_path(path, root)
    ]
    return tuple(found)


def _canonical_intake_body(package: Mapping[str, Any]) -> dict[str, Any]:
    """Project a promoted PCP onto the intake schema.

    Lab/handoff metadata may sit beside the canonical fields. Extra top-level
    keys and extra nested keys are dropped. Unknown capability profiles and
    prototype dispositions are ignored or coerced so they cannot veto intake.
    """

    body = {
        key: value for key, value in package.items()
        if key in pcp._TOP_LEVEL and key not in HANDOFF_ONLY_FIELDS
    }
    problem = body.get("problem")
    if isinstance(problem, Mapping):
        refs = []
        for item in problem.get("evidence_refs") or ():
            if isinstance(item, Mapping) and item.get("ref"):
                refs.append({
                    "ref": item["ref"],
                    "external": bool(item.get("external")),
                })
        body["problem"] = {
            "statement": problem.get("statement"),
            "evidence_refs": refs,
        }
    outcomes = []
    for item in body.get("outcome_criteria") or ():
        if not isinstance(item, Mapping):
            continue
        if {"outcome_id", "statement", "measurable_by"} <= set(item):
            outcomes.append({
                "outcome_id": item["outcome_id"],
                "statement": item["statement"],
                "measurable_by": item["measurable_by"],
            })
    if outcomes:
        body["outcome_criteria"] = outcomes
    decisions = []
    for item in body.get("decision_ledger") or ():
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status == "open_for_factory":
            status = "resolved"
            resolution = item.get("resolution") or item.get("rationale") or (
                "Factory-owned architecture decision"
            )
        else:
            resolution = item.get("resolution")
        row = dict(item)
        row["status"] = status
        if status == "resolved":
            row["resolution"] = resolution
        decisions.append(row)
    if decisions:
        body["decision_ledger"] = decisions
    caps = []
    for item in body.get("required_capabilities") or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("profile_id") not in pcp.PROFILE_IDS:
            continue
        if {"profile_id", "activated_by", "reason"} <= set(item):
            caps.append({
                "profile_id": item["profile_id"],
                "activated_by": item["activated_by"],
                "reason": item["reason"],
            })
    body["required_capabilities"] = caps
    evidence = body.get("evidence")
    if isinstance(evidence, Mapping):
        def _refs(field: str) -> list[str]:
            out: list[str] = []
            for item in evidence.get(field) or ():
                if isinstance(item, str) and item.strip():
                    out.append(item)
                elif isinstance(item, Mapping) and item.get("ref"):
                    out.append(str(item["ref"]))
            return out

        prototypes = []
        for item in evidence.get("prototype_refs") or ():
            if not isinstance(item, Mapping) or not item.get("ref"):
                continue
            disposition = item.get("disposition")
            if disposition not in pcp.PROTOTYPE_DISPOSITIONS:
                disposition = "DISPOSABLE_SPIKE"
            row = {"ref": item["ref"], "disposition": disposition}
            if disposition == "FOUNDATION_SEED" and item.get("commit_sha"):
                row["commit_sha"] = item["commit_sha"]
            prototypes.append(row)
        body["evidence"] = {
            "validation_findings_refs": _refs("validation_findings_refs"),
            "prototype_refs": prototypes,
            "opportunity_refs": _refs("opportunity_refs"),
            "competitive_refs": _refs("competitive_refs"),
        }
    return body


def _strip_handoff(package: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical_intake_body(package)


def clarity_check(package: Mapping[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """Return (lifecycle, clarification, intake-row-or-None)."""
    body = _canonical_intake_body(package)
    try:
        intake = pcp.intake(body)
    except pcp.PCPRefusal as refusal:
        return "CLARITY_REQUIRED", "%s: %s" % (refusal.code, refusal.detail), None
    if intake.mission.get("open_decisions"):
        return (
            "CLARITY_REQUIRED",
            "open Owner/product decisions: %s"
            % ", ".join(intake.mission["open_decisions"]),
            intake.as_row(),
        )
    return "IMPLEMENT", "", intake.as_row()


def mission_key(canonical_path: str, package_digest: str) -> str:
    return "%s@%s" % (canonical_path, package_digest)


def write_rc_alpha_surface(root: str | Path, mission: PCPMission) -> Path:
    """Write a reachable RC-alpha surface for Owner Validation. No Notion."""
    dest = Path(root) / mission.package_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>%s RC-alpha</title></head><body>"
        "<h1>%s</h1><p>RC-alpha. Stop: Owner Validation.</p>"
        "</body></html>\n" % (mission.package_id, mission.package_id),
        encoding="utf-8",
    )
    (dest / "health.json").write_text(
        json.dumps({
            "app": mission.package_id,
            "status": "ok",
            "lifecycle": "OWNER_VALIDATION",
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


class PCPMissionPlane:
    """Durable PCP missions on the Controller store. Not a second scheduler."""

    def __init__(self, store, vault_root: str | Path, *, clock=None) -> None:
        self._store = store
        self.vault_root = Path(vault_root)
        self.clock = clock or store.clock
        with store.transaction() as db:
            db.executescript(SCHEMA)

    def sync(self, *, source_revision: str = "") -> tuple[PCPMission, ...]:
        admitted: list[PCPMission] = []
        for path in discover_promoted_pcps(self.vault_root):
            relative = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
            try:
                package = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(package, Mapping):
                continue
            lifecycle, clarification, intake_row = clarity_check(package)
            digest = pcp.package_digest(_strip_handoff(package))
            package_id = str(package.get("package_id") or relative)
            version = int(package.get("package_version") or 1)
            key = mission_key(relative, digest)
            admitted.append(self._upsert(
                key=key,
                package_id=package_id,
                package_version=version,
                package_digest=digest,
                canonical_path=relative,
                source_revision=source_revision,
                lifecycle=lifecycle,
                clarification=clarification,
                payload=intake_row or dict(package),
            ))
        return tuple(admitted)

    def list(self) -> tuple[PCPMission, ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM pcp_missions ORDER BY created_at, mission_key"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def set_hold(self, mission_key_value: str, hold: bool) -> None:
        now = self.clock()
        with self._store.transaction() as db:
            db.execute(
                "UPDATE pcp_missions SET hold=?, updated_at=? WHERE mission_key=?",
                (int(hold), now, mission_key_value),
            )

    def set_rc_url(self, mission_key_value: str, *, alpha: str = "", beta: str = "") -> None:
        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT lifecycle FROM pcp_missions WHERE mission_key=?",
                (mission_key_value,),
            ).fetchone()
            if row is None:
                return
            lifecycle = row["lifecycle"]
            if alpha:
                lifecycle = "OWNER_VALIDATION"
            if beta:
                lifecycle = "OWNER_SIGNOFF"
            db.execute(
                """UPDATE pcp_missions
                   SET rc_alpha_url=CASE WHEN ? != '' THEN ? ELSE rc_alpha_url END,
                       rc_beta_url=CASE WHEN ? != '' THEN ? ELSE rc_beta_url END,
                       lifecycle=?, updated_at=?
                   WHERE mission_key=?""",
                (alpha, alpha, beta, beta, lifecycle, now, mission_key_value),
            )

    def claim(self, mission_key_value: str, worker_id: str, *,
              lease_seconds: float = 120.0) -> bool:
        now = self.clock()
        expires = now + lease_seconds
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT hold, worker_lease, lease_expires_at FROM pcp_missions WHERE mission_key=?",
                (mission_key_value,),
            ).fetchone()
            if row is None or row["hold"]:
                return False
            lease = row["worker_lease"]
            until = row["lease_expires_at"]
            if lease and until is not None and until > now and lease != worker_id:
                return False
            db.execute(
                """UPDATE pcp_missions
                   SET worker_lease=?, lease_expires_at=?, updated_at=?
                   WHERE mission_key=?""",
                (worker_id, expires, now, mission_key_value),
            )
            return True

    def advance(self, *, worker_id: str = "factory-pcp",
                rc_alpha_for=None) -> tuple[PCPMission, ...]:
        """Admit clear PCPs as Controller missions and record RC-alpha when ready.

        ``rc_alpha_for(mission) -> url`` is optional Factory processing. Notion
        is never consulted. Duplicate path+digest stays one mission.
        """

        self.sync()
        advanced: list[PCPMission] = []
        for row in self.list():
            if row.hold or row.lifecycle == "CLARITY_REQUIRED":
                continue
            payload = {
                "work_item_id": "%s:build" % row.package_id,
                "project_id": row.package_id,
                "source_pcp": row.canonical_path,
                "package_digest": row.package_digest,
                "lifecycle": row.lifecycle,
            }
            try:
                self._store.submit(payload, row.mission_key)
            except Exception:
                continue
            if row.lifecycle in {"OWNER_VALIDATION", "OWNER_SIGNOFF", "PRODUCTION"}:
                advanced.append(row)
                continue
            if not self.claim(row.mission_key, worker_id):
                continue
            url = ""
            if callable(rc_alpha_for):
                url = rc_alpha_for(row) or ""
            elif row.rc_alpha_url:
                url = row.rc_alpha_url
            if url:
                self.set_rc_url(row.mission_key, alpha=url)
            advanced.append(
                next(item for item in self.list() if item.mission_key == row.mission_key)
            )
        return tuple(advanced)

    def _upsert(self, **fields) -> PCPMission:
        now = self.clock()
        key = fields["key"]
        payload = json.dumps(fields["payload"], sort_keys=True, separators=(",", ":"))
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM pcp_missions WHERE mission_key=?", (key,)
            ).fetchone()
            if existing is not None:
                return self._from_row(existing)
            db.execute(
                """INSERT INTO pcp_missions (
                     mission_key, package_id, package_version, package_digest,
                     canonical_path, source_revision, priority, hold, lifecycle,
                     worker_lease, lease_expires_at, rc_alpha_url, rc_beta_url,
                     clarification, payload_json, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,0,0,?,NULL,NULL,'','',?,?,?,?)""",
                (
                    key, fields["package_id"], fields["package_version"],
                    fields["package_digest"], fields["canonical_path"],
                    fields["source_revision"], fields["lifecycle"],
                    fields["clarification"], payload, now, now,
                ),
            )
            row = db.execute(
                "SELECT * FROM pcp_missions WHERE mission_key=?", (key,)
            ).fetchone()
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> PCPMission:
        return PCPMission(
            mission_key=row["mission_key"],
            package_id=row["package_id"],
            package_version=int(row["package_version"]),
            package_digest=row["package_digest"],
            canonical_path=row["canonical_path"],
            lifecycle=row["lifecycle"],
            hold=bool(row["hold"]),
            rc_alpha_url=row["rc_alpha_url"] or "",
            rc_beta_url=row["rc_beta_url"] or "",
            clarification=row["clarification"] or "",
        )
