"""Unattended engineering-management loop, as one finite Controller cycle.

The Owner-facing name for the manager is an advisory endpoint.  This module
never names a vendor: it records requested versus observed identities the
adapter reports, validates every proposal against durable Controller facts,
and only then admits or advances ordinary missions.

Hard eligibility is computed before preference ranking.  Unknown readiness,
quota, or cost stays unknown and cannot be compensated by a high prior.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import advisor
from . import routing
from . import work_intake
from . import work_source
from .store import canonical_json, payload_hash


CONTRACT_VERSION = "factory-controller/management/1.0"
EXPORT_SCHEMA = "factory.controller.management_record.v1"
AUTHORITY_SCHEMA = work_source.AUTHORITY_SCHEMA
AUTHORITY_NAME = work_source.AUTHORITY_NAME

BOOTSTRAP_PRIOR_VERSION = "phase-2-agent-capability-mapping/2026-09-05"

FORBIDDEN_CHILD_FIELDS = (
    "execution_mode", "acceptance_gate_ids", "context_manifest_hash",
    "idempotency_key", "gateway_policy", "advisor_policy", "budget_ceiling",
    "max_route_legs", "max_attempts",
)

DEFAULT_CYCLE_LEASE_SECONDS = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS management_cycles (
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
CREATE INDEX IF NOT EXISTS management_cycles_open
  ON management_cycles(ended_at, lease_expires_at);
CREATE TABLE IF NOT EXISTS management_records (
  record_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  work_item_id TEXT,
  mission_id TEXT,
  state_version TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  judgment_json TEXT NOT NULL,
  eligibility_json TEXT NOT NULL,
  selection_json TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  reconciliation_json TEXT NOT NULL,
  export_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS management_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS management_events_no_update
BEFORE UPDATE ON management_events
BEGIN SELECT RAISE(ABORT, 'management events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS management_events_no_delete
BEFORE DELETE ON management_events
BEGIN SELECT RAISE(ABORT, 'management events are append-only'); END;
CREATE TABLE IF NOT EXISTS source_manifests (
  manifest_id TEXT PRIMARY KEY,
  root_realpath TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  authority_digest TEXT NOT NULL,
  registered_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_observations (
  profile TEXT PRIMARY KEY,
  observation_json TEXT NOT NULL,
  recorded_at REAL NOT NULL
);
"""


class ManagementRefusal(Exception):
    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail
        self.extra = extra

    def as_row(self) -> dict[str, Any]:
        row = {"code": self.code, "detail": self.detail}
        row.update(self.extra)
        return row


class ManagerPort(Protocol):
    def judge(self, snapshot: dict[str, Any]) -> dict[str, Any]: ...
    def observed_identity(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


def _table_exists(db, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def load_authority(root: str | Path) -> dict[str, Any]:
    """A scheduled inbox must carry an Owner-granted stamp, not a prompt."""

    try:
        return work_source.load_authority(root)
    except work_source.PacketError as exc:
        raise ManagementRefusal(exc.code, exc.detail) from exc


def inherit_envelope(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Children may narrow parent authorization.  They may not widen it."""

    merged = dict(parent)
    for field_name in FORBIDDEN_CHILD_FIELDS:
        if field_name in child and child[field_name] != parent.get(field_name):
            raise ManagementRefusal(
                "MANAGEMENT_ENVELOPE_WIDENED",
                "child may not change %s" % field_name,
                field=field_name)
    policy = dict(parent.get("execution_policy") or {})
    child_policy = child.get("execution_policy") or {}
    if isinstance(child_policy, dict):
        parent_allowed = tuple(policy.get("allowed_profiles") or ())
        child_allowed = tuple(child_policy.get("allowed_profiles") or ())
        if parent_allowed and any(item not in parent_allowed for item in child_allowed):
            raise ManagementRefusal(
                "MANAGEMENT_ENVELOPE_WIDENED",
                "child allowlist is not a subset of the parent")
        parent_ceiling = policy.get("budget_ceiling")
        child_ceiling = child_policy.get("budget_ceiling")
        if parent_ceiling is not None and child_ceiling is not None:
            if float(child_ceiling) > float(parent_ceiling):
                raise ManagementRefusal(
                    "MANAGEMENT_ENVELOPE_WIDENED",
                    "child budget exceeds parent ceiling")
        parent_legs = int(policy.get("max_route_legs") or routing.DEFAULT_MAX_ROUTE_LEGS)
        child_legs = child_policy.get("max_route_legs")
        if child_legs is not None and int(child_legs) > parent_legs:
            raise ManagementRefusal(
                "MANAGEMENT_ENVELOPE_WIDENED",
                "child fan-out exceeds parent max_route_legs")
        if child_allowed:
            policy["allowed_profiles"] = list(child_allowed)
    merged["execution_policy"] = policy
    return merged


def observation_status(obs: Any, *, now: float) -> str:
    """Omitted, bare, or stale availability is unknown — never schedulable."""

    if not isinstance(obs, Mapping):
        return "unknown"
    if obs.get("classification") != "available":
        return "unknown"
    if obs.get("quota_state") != "available":
        return "unknown"
    source = obs.get("source")
    if not isinstance(source, str) or not source.strip():
        return "unknown"
    if not isinstance(obs.get("observed_at"), (int, float)):
        return "unknown"
    fresh_until = obs.get("fresh_until")
    if not isinstance(fresh_until, (int, float)) or float(fresh_until) < now:
        return "unknown"
    return "admissible"


def hard_eligibility(payload: Mapping[str, Any], *,
                     observations: Mapping[str, Any] | None = None,
                     now: float = 0.0,
                     readiness: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Stage 1: constraints.  Preference scores are not consulted here."""

    policy = routing.ExecutionPolicy.from_payload(dict(payload))
    candidates = routing.candidates_from_payload(dict(payload))
    selection = routing.select(policy, candidates)
    fleet = dict(observations or {})
    ready = dict(readiness or {})
    eligible: list[str] = []
    rejected: list[dict[str, str]] = []
    for consideration in selection.considered:
        if consideration.profile in ready:
            status = ready[consideration.profile]
        elif consideration.profile in fleet:
            status = observation_status(fleet[consideration.profile], now=now)
        else:
            status = "unknown"
        if status == "unknown":
            rejected.append({"profile": consideration.profile,
                             "reason": "readiness_unknown"})
            continue
        if status != "admissible":
            rejected.append({"profile": consideration.profile, "reason": status})
            continue
        if not consideration.admissible:
            rejected.append({"profile": consideration.profile,
                             "reason": consideration.reason})
            continue
        eligible.append(consideration.profile)
    return {
        "eligible": eligible,
        "rejected": rejected,
        "required_capability": policy.required_capability,
        "selection_reason": selection.reason,
        "refusal_code": None if eligible else (selection.refusal_code or "NO_ELIGIBLE_PROFILE"),
    }


def prefer(eligible: Sequence[str], priors: Mapping[str, float]) -> str | None:
    """Stage 2: conservative ranking inside an already-eligible set."""

    if not eligible:
        return None
    return max(eligible, key=lambda profile: (float(priors.get(profile, 0.0)),
                                              -list(eligible).index(profile)))


def reviewer_requirement(selected: str, payload: Mapping[str, Any],
                         *, eligible: Sequence[str],
                         fleet: Mapping[str, Any] | None = None,
                         now: float = 0.0) -> str | None:
    """Reviewer identity comes from the eligible fleet, not the packet."""

    packet_reviewer = (payload.get("execution_policy") or {}).get("reviewer")
    if isinstance(packet_reviewer, str) and packet_reviewer == selected:
        raise ManagementRefusal(
            "SELF_INDEPENDENT_REVIEW",
            "packet may not name the selected executor as its reviewer")
    for profile, obs in dict(fleet or {}).items():
        if profile != selected and observation_status(obs, now=now) == "admissible":
            return profile
    for profile in eligible:
        if profile != selected:
            return profile
    return None


@dataclass
class CycleReport:
    cycle_id: str
    sequence: int
    outcome: str
    started_at: float
    ended_at: float
    reason: str = "CYCLE_COMPLETED"
    next_action: str = "not_applicable"
    record_id: str | None = None
    owner_attention: dict[str, Any] | None = None
    degraded: str = "none"
    lease_token: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id, "sequence": self.sequence,
            "outcome": self.outcome, "reason": self.reason,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "next_action": self.next_action, "record_id": self.record_id,
            "owner_attention": self.owner_attention, "degraded": self.degraded,
            "contract_version": CONTRACT_VERSION,
        }


def status_lines(reading: Mapping[str, Any]) -> tuple[str, ...]:
    """Owner-facing management lines.  No vendor name, no internal ids required."""

    recon = reading.get("reconciliation") or {}
    decision = reading.get("last_decision") or {}
    identities = reading.get("selected_observed_profiles") or {}
    attention = reading.get("owner_attention_need")
    mission = reading.get("current_managed_mission") or "none"
    selected = decision.get("selected") if isinstance(decision, dict) else None
    observed = identities.get("observed_executor") if isinstance(identities, dict) else None
    requested = identities.get("requested_executor") if isinstance(identities, dict) else None
    reviewer = reading.get("reviewer_requirement") or "none"
    degraded = reading.get("degraded") or "none"
    state = "in flight" if reading.get("open_cycle_id") else (recon.get("state") or "idle")
    lines = [
        "Management: %s" % state,
        "Managed mission: %s" % mission,
        "Last decision: requested %s, selected %s, observed %s, reviewer %s" % (
            requested or "unknown", selected or "none", observed or "unknown", reviewer),
        "Reconciliation: %s; degraded: %s" % (recon.get("state") or "none", degraded),
    ]
    if attention:
        code = attention.get("code") if isinstance(attention, dict) else attention
        lines.append("Attention: management requires Owner action (%s)" % code)
    return tuple(lines)


class ManagementPlane:
    def __init__(self, store, *, clock=None) -> None:
        self._store = store
        self.clock = clock or store.clock
        with store.transaction() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(management_cycles)")}
            if "lease_token" not in columns:
                db.execute(
                    "ALTER TABLE management_cycles ADD COLUMN lease_token TEXT")

    def state_version(self) -> str:
        with self._store.transaction() as db:
            def _max(table: str) -> int:
                if not _table_exists(db, table):
                    return 0
                row = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS seq FROM %s" % table
                ).fetchone()
                return int(row["seq"] if row is not None else 0)

            events = _max("events")
            work_events = _max("work_events")
            cycles = _max("management_cycles")
            mgmt_events = _max("management_events")
            if _table_exists(db, "work_items"):
                items = db.execute(
                    "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS updated "
                    "FROM work_items"
                ).fetchone()
            else:
                items = {"n": 0, "updated": 0}
            manifests = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(registered_at), 0) AS registered "
                "FROM source_manifests"
            ).fetchone()
            fleet_rows = db.execute(
                "SELECT profile, observation_json, recorded_at FROM fleet_observations"
            ).fetchall()
        fleet_map = {
            row["profile"]: json.loads(row["observation_json"]) for row in fleet_rows
        }
        recorded = max((row["recorded_at"] for row in fleet_rows), default=0)
        return payload_hash({
            "events": events,
            "work_events": work_events,
            "management_cycles": cycles,
            "management_events": mgmt_events,
            "work_items": int(items["n"] if items else 0),
            "work_items_updated": items["updated"] if items else 0,
            "source_manifests": int(manifests["n"] if manifests else 0),
            "source_manifests_registered": manifests["registered"] if manifests else 0,
            "fleet_observations": len(fleet_map),
            "fleet_observations_recorded": recorded,
            "fleet_digest": payload_hash(fleet_map),
        })[:32]

    def register_source_manifest(self, root: str | Path) -> dict[str, Any]:
        """Persist Factory-owned source identity. Inbox files are not authority."""

        authority = load_authority(root)
        real = str(Path(root).resolve())
        manifest_id = payload_hash({
            "root": real,
            "digest": authority["authority_digest"],
            "revision": authority["source_revision"],
        })[:32]
        now = self.clock()
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM source_manifests WHERE root_realpath=?",
                (real,)).fetchone()
            if existing is not None:
                same = (
                    existing["authority_digest"] == authority["authority_digest"]
                    and existing["source_revision"] == authority["source_revision"]
                    and existing["source"] == authority["source"]
                    and existing["granted_by"] == authority["granted_by"])
                if not same:
                    raise ManagementRefusal(
                        "MANAGEMENT_SOURCE_IDENTITY_CHANGED",
                        "Factory-owned source identity is immutable after registration")
                return dict(existing)
            db.execute(
                "INSERT INTO source_manifests ("
                " manifest_id, root_realpath, source, granted_by,"
                " source_revision, authority_digest, registered_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (manifest_id, real, authority["source"], authority["granted_by"],
                 authority["source_revision"], authority["authority_digest"], now))
        return {"manifest_id": manifest_id, **authority}

    def bind_source(self, root: str | Path, authority: Mapping[str, Any]) -> dict[str, Any]:
        real = str(Path(root).resolve())
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM source_manifests WHERE root_realpath=?",
                (real,)).fetchone()
        if row is None:
            raise ManagementRefusal(
                "MANAGEMENT_SOURCE_UNBOUND",
                "no Factory-owned source manifest for %s" % real)
        if row["authority_digest"] != authority.get("authority_digest"):
            raise ManagementRefusal(
                "MANAGEMENT_SOURCE_DIGEST_MISMATCH",
                "inbox authority digest is not the registered manifest")
        if row["source_revision"] != authority.get("source_revision"):
            raise ManagementRefusal(
                "MANAGEMENT_SOURCE_REVISION_MISMATCH",
                "inbox source_revision is not the registered manifest")
        if row["source"] != authority.get("source") or row["granted_by"] != authority.get("granted_by"):
            raise ManagementRefusal(
                "MANAGEMENT_SOURCE_IDENTITY_MISMATCH",
                "inbox source identity is not the registered manifest")
        return dict(row)

    def record_fleet_observation(self, profile: str, observation: Mapping[str, Any]) -> None:
        now = self.clock()
        with self._store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO fleet_observations"
                " (profile, observation_json, recorded_at) VALUES (?,?,?)",
                (profile, canonical_json(dict(observation)), now))

    def fleet_observations(self) -> dict[str, Any]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT profile, observation_json FROM fleet_observations"
            ).fetchall()
        return {row["profile"]: json.loads(row["observation_json"]) for row in rows}

    def status(self) -> dict[str, Any]:
        latest = self.latest_record()
        open_cycle = self._open_cycle()
        receipt = {} if latest is None else json.loads(latest["receipt_json"])
        selection = {} if latest is None else json.loads(latest["selection_json"])
        identities = dict(receipt.get("identities") or {})
        return {
            "contract_version": CONTRACT_VERSION,
            "current_managed_mission": None if latest is None else latest.get("mission_id"),
            "last_decision": None if latest is None else selection,
            "selected_observed_profiles": None if latest is None else {
                "requested_manager": identities.get("requested_profile"),
                "observed_manager": identities.get("observed_profile"),
                "requested_executor": receipt.get("requested_executor"),
                "observed_executor": receipt.get("observed_executor"),
            },
            "reviewer_requirement": None if latest is None else selection.get("reviewer"),
            "reconciliation": None if latest is None else json.loads(
                latest["reconciliation_json"]),
            "degraded": "cycle_in_flight" if open_cycle else "none",
            "owner_attention_need": None if latest is None else json.loads(
                latest["export_json"]).get("owner_attention"),
            "open_cycle_id": None if open_cycle is None else open_cycle["cycle_id"],
        }

    def latest_record(self) -> dict[str, Any] | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM management_records ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def export_records(self) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT export_json FROM management_records ORDER BY created_at"
            ).fetchall()
        return tuple(json.loads(row["export_json"]) for row in rows)

    def cycle(self, worker_id: str, *, source_dir: str | Path,
              controller, manager: ManagerPort,
              priors: Mapping[str, float] | None = None,
              policy: advisor.AdvisorPolicy | None = None,
              readiness: Mapping[str, str] | None = None,
              lease_seconds: float = DEFAULT_CYCLE_LEASE_SECONDS,
              execute: bool = True) -> dict[str, Any]:
        claim = self._claim_cycle(worker_id, lease_seconds)
        report = CycleReport(
            cycle_id=claim["cycle_id"], sequence=claim["sequence"],
            outcome="completed", started_at=claim["started_at"],
            ended_at=claim["started_at"], lease_token=claim.get("lease_token"))
        try:
            authority = load_authority(source_dir)
            self.bind_source(source_dir, authority)
            source = work_source.DirectoryWorkSource(
                source_dir, source_kind="scheduled")
            intake = work_intake.WorkIntakePlane(self._store, clock=self.clock)
            observed = intake.observe(source)
            version = self.state_version()
            snapshot = self._snapshot(authority, observed, version)
            try:
                judgment = manager.judge(snapshot)
            except PermissionError as exc:
                err = str(exc)
                missing_model = err == "ADVISOR_MODEL_ABSENT"
                missing_session = (
                    err.startswith("ADVISOR_") and err.endswith("ABSENT")
                    and not missing_model)
                attention = None
                if missing_session:
                    report.reason = "EXTERNAL_OWNER_AUTH_REQUIRED"
                    report.next_action = "OWNER_AUTH"
                    attention = {
                        "code": report.reason,
                        "action": (
                            "sign in to the local advisory HTTP surface once; "
                            "do not paste a secret into a task page or Git"
                        ),
                    }
                    report.owner_attention = attention
                    recon = "owner_auth_required"
                elif missing_model:
                    report.reason = "MANAGER_PROVIDER_ADAPTER_BLOCKED"
                    report.next_action = "WAIT_MANAGER"
                    recon = "manager_adapter_blocked"
                else:
                    report.reason = "MANAGER_UNAVAILABLE"
                    report.next_action = "WAIT_MANAGER"
                    recon = "manager_unavailable"
                report.outcome = "idle"
                report.degraded = "manager_unavailable"
                self._recover_authorized(intake, controller, execute)
                return self._close(report, snapshot=snapshot, judgment={
                    "error": str(exc)}, eligibility={}, selection={},
                    receipt={}, reconciliation={"state": recon},
                    attention=attention)
            except (ValueError, OSError) as exc:
                report.outcome = "idle"
                report.reason = "MANAGER_JUDGMENT_UNUSABLE"
                report.degraded = "manager_unavailable"
                report.next_action = "WAIT_MANAGER"
                return self._close(report, snapshot=snapshot, judgment={
                    "error": str(exc)}, eligibility={}, selection={},
                    receipt={}, reconciliation={"state": "manager_unavailable"})
            if self.state_version() != version:
                raise ManagementRefusal(
                    "MANAGEMENT_STALE_DECISION",
                    "controller state moved during judgment")
            identities = manager.observed_identity(judgment)
            facts = advisor.Facts(
                projects=tuple(sorted(self._store.projects())),
                missions=tuple(sorted(row["id"] for row in self._store.all_missions())),
                edges={key: tuple(value) for key, value in self._store.dependency_graph().items()})
            granted = policy or advisor.AdvisorPolicy(
                enabled=True,
                allowed_kinds=("decompose", "specialist_profile", "next_mission"),
                allowed_profiles=tuple((priors or {}).keys()),
                max_proposals=8)
            outcome = advisor.consult(
                advisor.StaticAdvisor(judgment),
                {"snapshot": snapshot}, granted, facts)
            illegal = [verdict.as_row() for verdict in outcome.verdicts if not verdict.accepted]
            nxt = intake.eligible()
            if not nxt:
                report.outcome = "idle"
                report.reason = "MANAGEMENT_EMPTY"
                report.next_action = "not_applicable"
                return self._close(
                    report, snapshot=snapshot, judgment=judgment,
                    eligibility={}, selection={"illegal": illegal},
                    receipt={"identities": identities},
                    reconciliation={"state": "idle"})
            item = nxt[0]
            payload = dict(item.get("payload") or {})
            if item.get("owner_only"):
                attention = {
                    "code": "OWNER_ACTION_REQUIRED",
                    "reason": item.get("owner_reason"),
                    "work_item_id": item["work_item_id"],
                }
                report.outcome = "idle"
                report.reason = "MANAGEMENT_OWNER_REQUIRED"
                report.owner_attention = attention
                report.next_action = "OWNER_REQUIRED"
                return self._close(
                    report, snapshot=snapshot, judgment=judgment,
                    eligibility={}, selection={"illegal": illegal},
                    receipt={"identities": identities},
                    reconciliation={"state": "owner_required"},
                    work_item_id=item["work_item_id"], attention=attention)
            eligibility = hard_eligibility(
                payload, observations=self.fleet_observations(),
                now=self.clock(), readiness=readiness)
            selected = prefer(eligibility["eligible"], priors or {})
            if selected is None:
                report.outcome = "idle"
                report.reason = eligibility.get("refusal_code") or "NO_ELIGIBLE_PROFILE"
                report.next_action = "not_applicable"
                return self._close(
                    report, snapshot=snapshot, judgment=judgment,
                    eligibility=eligibility, selection={"illegal": illegal},
                    receipt={"identities": identities},
                    reconciliation={"state": "ineligible"},
                    work_item_id=item["work_item_id"])
            reviewer = reviewer_requirement(
                selected, payload, eligible=eligibility["eligible"],
                fleet=self.fleet_observations(), now=self.clock())
            narrowed = inherit_envelope(payload, {
                "execution_policy": {
                    **dict(payload.get("execution_policy") or {}),
                    "allowed_profiles": [selected],
                }})
            claimed = intake.claim(item["work_item_id"], worker_id)
            if claimed is None:
                report.outcome = "idle"
                report.reason = "MANAGEMENT_CLAIM_LOST"
                return self._close(
                    report, snapshot=snapshot, judgment=judgment,
                    eligibility=eligibility,
                    selection={"selected": selected, "reviewer": reviewer},
                    receipt={"identities": identities},
                    reconciliation={"state": "claim_lost"})
            admitted = intake.admit(claimed["work_item_id"], controller)
            mission = admitted.get("mission") or {}
            mission_id = mission.get("id")
            if mission_id and narrowed.get("execution_policy"):
                self._narrow_mission(mission_id, narrowed)
            receipt: dict[str, Any] = {
                "identities": identities,
                "requested_executor": selected,
                "observed_executor": "unknown",
                "dispatch_state": "authorized",
            }
            reconciliation = {"state": "admitted", "next_action": "DISPATCH"}
            if execute and mission_id:
                mission = controller.work_once(worker_id, mission_id=mission_id)
                if mission is None or mission.get("id") != mission_id:
                    receipt["dispatch_state"] = "uncertain"
                    reconciliation = {"state": "uncertain",
                                      "detail": "dispatch timeout or missing mission"}
                    report.degraded = "uncertain_dispatch"
                    report.next_action = "RECONCILE_UNCERTAIN"
                else:
                    observed_exec = _observed_executor(mission, self._store)
                    receipt["observed_executor"] = observed_exec
                    receipt["mission_state"] = mission.get("state")
                    receipt["dispatch_state"] = mission.get("state")
                    independent = _independent_acceptance(
                        mission, selected=selected, reviewer=reviewer)
                    receipt["independent_acceptance"] = independent
                    remaining = intake.eligible()
                    next_action = ("MANAGEMENT_CONTINUE" if remaining
                                   else "not_applicable")
                    reconciliation = {
                        "state": mission.get("state"),
                        "independent_acceptance": independent,
                        "next_action": next_action,
                    }
                    report.next_action = next_action
                    intake.reconcile(claimed["work_item_id"], controller)
            selection = {
                "selected": selected, "reviewer": reviewer,
                "illegal": illegal, "priors_version": BOOTSTRAP_PRIOR_VERSION,
            }
            export = self._export_body(
                report.cycle_id, version, authority, item, mission_id,
                snapshot, judgment, eligibility, selection, receipt,
                reconciliation, identities)
            report.record_id = self._persist(
                report.cycle_id, item["work_item_id"], mission_id, version,
                snapshot, judgment, eligibility, selection, receipt,
                reconciliation, export)
            report.reason = "CYCLE_COMPLETED"
            return self._finish(report, extra={"export": export})
        except ManagementRefusal as refusal:
            report.outcome = "refused"
            report.reason = refusal.code
            report.next_action = "not_applicable"
            return self._close(
                report, snapshot={}, judgment={"refusal": refusal.as_row()},
                eligibility={}, selection={}, receipt={},
                reconciliation={"state": "refused", "code": refusal.code})
        except work_intake.WorkIntakeRefusal as refusal:
            report.outcome = "refused"
            report.reason = refusal.code
            return self._close(
                report, snapshot={}, judgment={}, eligibility={},
                selection={}, receipt={},
                reconciliation={"state": "refused", "code": refusal.code})
        except BaseException:
            self._abandon(report)
            raise

    def _narrow_mission(self, mission_id: str, payload: Mapping[str, Any]) -> None:
        policy = payload.get("execution_policy") or {}
        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT payload_json, payload_hash FROM missions WHERE id=?",
                (mission_id,)
            ).fetchone()
            if row is None:
                return
            current = json.loads(row["payload_json"])
            expected = row["payload_hash"]
            current["execution_policy"] = policy
            rewritten = canonical_json(current)
            new_hash = payload_hash(current)
            changed = db.execute(
                "UPDATE missions SET payload_json=?, payload_hash=? "
                "WHERE id=? AND payload_hash=?",
                (rewritten, new_hash, mission_id, expected)).rowcount
            if changed != 1:
                raise ManagementRefusal(
                    "MANAGEMENT_STALE_NARROW",
                    "mission %s changed during policy narrowing" % mission_id)
            db.execute(
                "INSERT INTO management_events (cycle_id, kind, detail_json, created_at)"
                " VALUES (?,?,?,?)",
                ("narrow-%s" % mission_id, "MISSION_NARROWED",
                 canonical_json({"mission_id": mission_id, "payload_hash": new_hash}),
                 now))
        self._store.log(mission_id, "MISSION_NARROWED", {"payload_hash": new_hash})

    def _snapshot(self, authority: Mapping[str, Any],
                  observed: Sequence[Mapping[str, Any]],
                  version: str) -> dict[str, Any]:
        return {
            "controller_state_version": version,
            "authority": dict(authority),
            "observed_work_item_ids": [item["work_item_id"] for item in observed],
            "bootstrap_prior_version": BOOTSTRAP_PRIOR_VERSION,
        }

    def _export_body(self, cycle_id: str, version: str, authority: Mapping[str, Any],
                     item: Mapping[str, Any], mission_id: str | None,
                     snapshot: Mapping[str, Any], judgment: Mapping[str, Any],
                     eligibility: Mapping[str, Any], selection: Mapping[str, Any],
                     receipt: Mapping[str, Any], reconciliation: Mapping[str, Any],
                     identities: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(item.get("payload") or {})
        policy = payload.get("execution_policy") or {}
        return {
            "schema_version": EXPORT_SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "cycle_id": cycle_id,
            "operation_key": cycle_id,
            "work_item_id": item.get("work_item_id"),
            "source_kind": item.get("source_kind"),
            "source_ref": item.get("source_ref"),
            "source_revision": authority.get("source_revision", "unknown"),
            "mission_id": mission_id,
            "parent_envelope": {
                "budget_ceiling": policy.get("budget_ceiling"),
                "budget_currency": policy.get("budget_currency"),
                "max_route_legs": policy.get("max_route_legs"),
                "acceptance_gate_ids": payload.get("acceptance_gate_ids"),
                "repository_scope": payload.get("project_id"),
            },
            "controller_state_version": version,
            "policy_digest": payload_hash(policy),
            "registry_digest": payload_hash(self.fleet_observations()),
            "context_digest": payload_hash(snapshot),
            "requested_manager": identities.get("requested_profile"),
            "observed_manager": identities.get("observed_profile"),
            "requested_manager_effort": identities.get("requested_effort"),
            "observed_manager_effort": identities.get("observed_effort"),
            "hard_eligibility": eligibility,
            "selected_executor": selection.get("selected"),
            "reviewer_requirement": selection.get("reviewer"),
            "execution_receipt": receipt,
            "candidate_artifact_evidence": (None if not isinstance(
                receipt.get("independent_acceptance"), dict) else receipt[
                    "independent_acceptance"].get("evidence_pointer", "unknown")),
            "independent_outcome": receipt.get("independent_acceptance"),
            "cost_quota_latency": {
                "cost": "unknown", "quota": "unknown", "latency_ms": "unknown",
            },
            "reconciliation": reconciliation,
            "next_management_action": reconciliation.get("next_action", "not_applicable"),
            "owner_attention": None,
            "judgment_reasoning_present": bool(
                isinstance(judgment.get("reasoning"), str) and judgment["reasoning"].strip()),
        }

    def _persist(self, cycle_id: str, work_item_id: str | None, mission_id: str | None,
                 version: str, snapshot: Mapping[str, Any], judgment: Mapping[str, Any],
                 eligibility: Mapping[str, Any], selection: Mapping[str, Any],
                 receipt: Mapping[str, Any], reconciliation: Mapping[str, Any],
                 export: Mapping[str, Any]) -> str:
        record_id = "mgr_%s" % uuid.uuid4().hex[:16]
        now = self.clock()
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO management_records (record_id, cycle_id, work_item_id,"
                " mission_id, state_version, snapshot_json, judgment_json,"
                " eligibility_json, selection_json, receipt_json,"
                " reconciliation_json, export_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, cycle_id, work_item_id, mission_id, version,
                 canonical_json(snapshot), canonical_json(judgment),
                 canonical_json(eligibility), canonical_json(selection),
                 canonical_json(receipt), canonical_json(reconciliation),
                 canonical_json(export), now))
            db.execute(
                "INSERT INTO management_events (cycle_id, kind, detail_json, created_at)"
                " VALUES (?,?,?,?)",
                (cycle_id, "RECORD", canonical_json({"record_id": record_id}), now))
        return record_id

    def _close(self, report: CycleReport, **parts: Any) -> dict[str, Any]:
        attention = parts.pop("attention", None)
        work_item_id = parts.pop("work_item_id", None)
        export = self._minimal_export(report, work_item_id, attention, parts)
        report.record_id = self._persist(
            report.cycle_id, work_item_id, None,
            parts.get("snapshot", {}).get("controller_state_version", "unknown")
            if isinstance(parts.get("snapshot"), dict) else "unknown",
            parts.get("snapshot") or {}, parts.get("judgment") or {},
            parts.get("eligibility") or {}, parts.get("selection") or {},
            parts.get("receipt") or {}, parts.get("reconciliation") or {},
            export)
        return self._finish(report, extra={"export": export})

    def _minimal_export(self, report: CycleReport, work_item_id: str | None,
                        attention: Mapping[str, Any] | None,
                        parts: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": EXPORT_SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "cycle_id": report.cycle_id,
            "operation_key": report.cycle_id,
            "work_item_id": work_item_id,
            "reconciliation": parts.get("reconciliation") or {},
            "next_management_action": report.next_action,
            "owner_attention": attention,
            "hard_eligibility": parts.get("eligibility") or {},
            "execution_receipt": parts.get("receipt") or {},
            "judgment_reasoning_present": bool(
                isinstance((parts.get("judgment") or {}).get("reasoning"), str)),
        }

    def _finish(self, report: CycleReport, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        report.ended_at = self.clock()
        with self._store.transaction() as db:
            changed = db.execute(
                "UPDATE management_cycles SET ended_at=?, outcome=?, detail_json=?"
                " WHERE cycle_id=? AND lease_token=? AND ended_at IS NULL",
                (report.ended_at, report.outcome, canonical_json(report.as_row()),
                 report.cycle_id, report.lease_token)).rowcount
            if changed != 1:
                report.outcome = "uncertain"
                report.reason = "MANAGEMENT_STALE_CYCLE"
                report.degraded = "uncertain_dispatch"
                report.next_action = "RECONCILE_UNCERTAIN"
        row = report.as_row()
        if extra:
            row.update(extra)
        return row

    def _abandon(self, report: CycleReport) -> None:
        report.outcome = "refused"
        report.reason = "MANAGEMENT_CYCLE_ABANDONED"
        report.degraded = "uncertain_dispatch"
        try:
            self._finish(report)
        except Exception:
            return

    def _recover_authorized(self, intake: work_intake.WorkIntakePlane,
                            controller, execute: bool) -> None:
        for item in intake.items():
            if item["state"] in {"admitted", "in_progress"}:
                intake.reconcile(item["work_item_id"], controller)
                mission_id = item.get("mission_ref")
                if execute and isinstance(mission_id, str) and mission_id:
                    controller.work_once("recovery", mission_id=mission_id)

    def _open_cycle(self) -> dict[str, Any] | None:
        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM management_cycles WHERE ended_at IS NULL"
            ).fetchone()
            if row is None:
                return None
            if float(row["lease_expires_at"]) < now:
                db.execute(
                    "UPDATE management_cycles SET ended_at=?, outcome=?,"
                    " detail_json=?, lease_token=?"
                    " WHERE cycle_id=? AND lease_token=?",
                    (now, "refused", canonical_json({"reason": "lease_expired"}),
                     "expired:%s" % uuid.uuid4().hex[:12],
                     row["cycle_id"], row["lease_token"]))
                return None
            return dict(row)

    def _claim_cycle(self, worker_id: str, lease_seconds: float) -> dict[str, Any]:
        now = self.clock()
        with self._store.transaction() as db:
            open_row = db.execute(
                "SELECT * FROM management_cycles WHERE ended_at IS NULL"
            ).fetchone()
            if open_row is not None:
                if float(open_row["lease_expires_at"]) >= now:
                    raise ManagementRefusal(
                        "MANAGEMENT_CYCLE_IN_FLIGHT",
                        "cycle %s still holds the lease" % open_row["cycle_id"],
                        cycle_id=open_row["cycle_id"])
                db.execute(
                    "UPDATE management_cycles SET ended_at=?, outcome=?,"
                    " detail_json=?, lease_token=?"
                    " WHERE cycle_id=? AND lease_token=?",
                    (now, "refused",
                     canonical_json({"reason": "recovered_uncertain"}),
                     "recovered:%s" % uuid.uuid4().hex[:12],
                     open_row["cycle_id"], open_row["lease_token"]))
            previous = db.execute(
                "SELECT cycle_id, sequence FROM management_cycles"
                " ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = 1 if previous is None else int(previous["sequence"]) + 1
            previous_id = None if previous is None else previous["cycle_id"]
            cycle_id = "mgc_%s" % payload_hash({
                "previous": previous_id or "none", "sequence": sequence,
                "worker_id": worker_id, "started_at": now,
            })[:24]
            lease_token = str(uuid.uuid4())
            db.execute(
                "INSERT INTO management_cycles (cycle_id, sequence,"
                " previous_cycle_id, worker_id, lease_token, lease_expires_at,"
                " started_at) VALUES (?,?,?,?,?,?,?)",
                (cycle_id, sequence, previous_id, worker_id, lease_token,
                 now + lease_seconds, now))
            recovered = [] if previous is None else []
            return {"cycle_id": cycle_id, "sequence": sequence,
                    "started_at": now, "recovered": recovered,
                    "lease_token": lease_token}


def _observed_executor(mission: Mapping[str, Any], store=None) -> str:
    """Observed executor comes from Controller-written run legs, not result JSON."""

    mission_id = mission.get("id")
    if store is None or not isinstance(mission_id, str) or not mission_id:
        return "unknown"
    legs = store.runs(mission_id)
    for leg in reversed(tuple(legs)):
        served = leg.get("provider_profile")
        if isinstance(served, str) and served:
            return served
    return "unknown"


def _independent_acceptance(mission: Mapping[str, Any], *,
                            selected: str | None = None,
                            reviewer: str | None = None) -> dict[str, Any]:
    result = mission.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            result = {}
    evaluation = result.get("evaluation") or {}
    evidence = result.get("evidence") or {}
    evaluator = evaluation.get("evaluator_profile")
    if isinstance(selected, str) and evaluator == selected:
        return {
            "verdict": "rejected",
            "passed": False,
            "reason": "SELF_INDEPENDENT_REVIEW",
            "evidence_pointer": evidence.get("evidence_pointer", "unknown"),
            "timestamps": {"updated_at": mission.get("updated_at")},
            "retry_rework_regression": {
                "attempt_count": mission.get("attempt_count"),
                "terminal_reason": mission.get("terminal_reason") or "not_applicable",
            },
        }
    accepted = bool(evaluation.get("passed"))
    if reviewer and not (isinstance(evaluator, str) and evaluator and evaluator != selected):
        accepted = False
        verdict = "unknown"
    else:
        verdict = "accepted" if accepted else "rejected"
    return {
        "verdict": verdict,
        "passed": accepted,
        "evaluator_profile": evaluator if isinstance(evaluator, str) else "unknown",
        "reviewer_requirement": reviewer or "unknown",
        "evidence_pointer": evidence.get("evidence_pointer", "unknown"),
        "timestamps": {"updated_at": mission.get("updated_at")},
        "retry_rework_regression": {
            "attempt_count": mission.get("attempt_count"),
            "terminal_reason": mission.get("terminal_reason") or "not_applicable",
        },
    }
