"""Controller-owned multi-PCP mission queue.

The trigger is a promoted PCP artifact in the canonical Vault product path.
Notion is never consulted. Duplicate detection is path + package digest.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
    "HERMES",
    "ARCHITECTURE",
    "IMPLEMENT",
    "INTEGRATION",
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
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pcp_missions_by_package
  ON pcp_missions(package_id, package_version);
"""

OWNER_DECISIONS = """
CREATE TABLE IF NOT EXISTS owner_validation_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_key TEXT NOT NULL,
  candidate_head TEXT NOT NULL,
  decision TEXT NOT NULL,
  feedback TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS owner_validation_by_mission
  ON owner_validation_decisions(mission_key, created_at);
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
    evidence: dict[str, Any] | None = None

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
    return "HERMES", "", intake.as_row()


def mission_key(canonical_path: str, package_digest: str) -> str:
    return "%s@%s" % (canonical_path, package_digest)


_PROJECT_ROOTS = (
    Path("/Users/Shared/Projects"),
    Path("/Users/Shared/Projects/software-factory"),
)
_STUB_MARKERS = (
    "<p>RC-alpha. Stop: Owner Validation.</p>",
    "RC-alpha. Stop: Owner Validation.",
)


def _web_root(checkout: Path) -> Path | None:
    if not checkout.is_dir():
        return None
    if (checkout / "index.html").is_file():
        return checkout
    public = checkout / "public"
    if (public / "index.html").is_file():
        return public
    return None


def locate_prototype(vault_root: str | Path, mission: PCPMission, *,
                     project_roots: tuple[Path, ...] | None = None
                     ) -> Path | None:
    """Locate Lab prototype bytes as Architecture *input*. Never RC-alpha."""

    names: list[str] = []
    pcp_path = Path(vault_root) / mission.canonical_path
    try:
        package = json.loads(pcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    handoff = package.get("factory_handoff") if isinstance(package, Mapping) else None
    if isinstance(handoff, Mapping):
        source = handoff.get("source_repository")
        if isinstance(source, str) and source.strip():
            names.append(source.strip().rsplit("/", 1)[-1])
        product_id = handoff.get("product_id")
        if isinstance(product_id, str) and product_id.strip():
            names.append(product_id.strip())
    names.append(mission.package_id)
    seen: set[str] = set()
    roots = project_roots if project_roots is not None else _PROJECT_ROOTS
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        for root in roots:
            candidates = []
            exact = root / name
            if exact.is_dir():
                candidates.append(exact)
            candidates.extend(sorted(
                (path for path in root.glob(name + "-*") if path.is_dir()),
                key=lambda path: path.name, reverse=True))
            for candidate in candidates:
                web = _web_root(candidate)
                if web is not None:
                    return web
    return None


resolve_product_checkout = locate_prototype  # architecture input only; never RC-alpha


def write_rc_alpha_surface(root: str | Path, mission: PCPMission) -> Path:
    """Test/fixture helper. Not a working product RC."""
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


def is_stub_rc_body(body: str) -> bool:
    return any(marker in body for marker in _STUB_MARKERS)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _fetch(url: str, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve_product_rc(web_root: Path, *, state_dir: str | Path, package_id: str,
                     candidate_head: str, lane: str = "pcp-rc-alpha") -> str:
    """Serve a sealed Factory candidate. Prototype checkouts are not valid."""

    if not candidate_head or not str(candidate_head).strip():
        return ""
    if lane not in {"pcp-rc-alpha", "pcp-e2e"}:
        return ""
    root = Path(web_root).resolve()
    marker = root / ".factory-candidate.json"
    try:
        identity = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if identity.get("candidate_head") != candidate_head:
        return ""
    if _web_root(root) is None and not (root / "index.html").is_file():
        return ""
    receipt_dir = Path(state_dir) / lane
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / ("%s.json" % package_id)
    if receipt.is_file():
        try:
            prior = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
        url = str(prior.get("url") or "")
        pid = int(prior.get("pid") or 0)
        prior_root = Path(str(prior.get("root") or "")).resolve()
        if (
            url and _pid_alive(pid)
            and str(prior.get("candidate_head") or "") == candidate_head
            and prior_root == root
        ):
            try:
                body = _fetch(url)
            except (OSError, urllib.error.URLError, TimeoutError, ValueError):
                body = ""
            if body and not is_stub_rc_body(body):
                return url
    port = _free_port()
    log = receipt.with_suffix(".log")
    with log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "http.server", str(port),
                "--bind", "127.0.0.1", "--directory", str(root),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = "http://127.0.0.1:%d/" % port
    body = ""
    for _ in range(50):
        if proc.poll() is not None:
            return ""
        try:
            body = _fetch(url)
            if body:
                break
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(0.05)
    if not body or is_stub_rc_body(body):
        proc.terminate()
        return ""
    receipt.write_text(
        json.dumps({
            "url": url, "pid": proc.pid, "root": str(root),
            "candidate_head": candidate_head,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return url


def _parse_evidence(row) -> dict[str, Any]:
    try:
        raw = row["evidence_json"]
    except (KeyError, IndexError, TypeError):
        return {}
    try:
        body = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


class PCPMissionPlane:
    """Durable PCP missions on the Controller store. Not a second scheduler."""

    def __init__(self, store, vault_root: str | Path, *, clock=None) -> None:
        self._store = store
        self.vault_root = Path(vault_root)
        self.clock = clock or store.clock
        with store.transaction() as db:
            db.executescript(SCHEMA)
            db.executescript(OWNER_DECISIONS)
            cols = {row[1] for row in db.execute("PRAGMA table_info(pcp_missions)")}
            if "evidence_json" not in cols:
                db.execute(
                    "ALTER TABLE pcp_missions ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
                )

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

    def set_rc_url(self, mission_key_value: str, *, alpha: str = "", beta: str = "",
                   evidence: Mapping[str, Any] | None = None) -> None:
        from .golden_path import IncompleteChain, missing_links

        now = self.clock()
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT lifecycle, evidence_json FROM pcp_missions WHERE mission_key=?",
                (mission_key_value,),
            ).fetchone()
            if row is None:
                return
            stored = {}
            try:
                stored = json.loads(row["evidence_json"] or "{}")
            except ValueError:
                stored = {}
            merged = {**stored, **(evidence or {})}
            lifecycle = row["lifecycle"]
            if alpha:
                missing = missing_links(merged)
                if missing:
                    raise IncompleteChain(missing)
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

    def ingest_owner_validation(
        self, mission_key_value: str, candidate_head: str, decision: str,
        feedback: str = "",
    ) -> PCPMission | None:
        """Record Owner Validation. REJECT continues the same mission automatically."""

        from .golden_path import apply_owner_reject, lifecycle_of

        now = self.clock()
        decision = str(decision or "").upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("OWNER_VALIDATION_DECISION_INVALID")
        with self._store.transaction() as db:
            db.executescript(OWNER_DECISIONS)
            db.execute(
                """INSERT INTO owner_validation_decisions
                   (mission_key, candidate_head, decision, feedback, created_at)
                   VALUES (?,?,?,?,?)""",
                (mission_key_value, candidate_head, decision, feedback, now),
            )
            row = db.execute(
                "SELECT * FROM pcp_missions WHERE mission_key=?",
                (mission_key_value,),
            ).fetchone()
            if row is None:
                return None
            evidence = _parse_evidence(row)
            event = {
                "mission_key": mission_key_value,
                "candidate_head": candidate_head,
                "decision": decision,
                "feedback": feedback,
                "timestamp": now,
                "notion_required": False,
            }
            history = list(evidence.get("owner_validation_history") or [])
            history.append(event)
            evidence["owner_validation"] = event
            evidence["owner_validation_history"] = history
            alpha = row["rc_alpha_url"] or ""
            lifecycle = row["lifecycle"]
            if decision == "REJECT":
                before = str((evidence.get("integration") or {}).get("candidate_head")
                             or (evidence.get("rc_alpha") or {}).get("candidate_head")
                             or "")
                evidence = apply_owner_reject(evidence)
                after = str((evidence.get("integration") or {}).get("candidate_head") or "")
                if after != before:
                    alpha = ""
                lifecycle = lifecycle_of(evidence)
            db.execute(
                """UPDATE pcp_missions
                   SET evidence_json=?, lifecycle=?, rc_alpha_url=?,
                       worker_lease=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE mission_key=?""",
                (json.dumps(evidence, sort_keys=True), lifecycle, alpha,
                 now, mission_key_value),
            )
        return next(
            item for item in self.list() if item.mission_key == mission_key_value)

    def record_evidence(self, mission_key_value: str, evidence: Mapping[str, Any],
                        *, lifecycle: str = "") -> None:
        from .golden_path import lifecycle_of

        now = self.clock()
        body = json.dumps(dict(evidence), sort_keys=True)
        next_lifecycle = lifecycle or lifecycle_of(evidence)
        with self._store.transaction() as db:
            db.execute(
                """UPDATE pcp_missions
                   SET evidence_json=?, lifecycle=?, updated_at=?
                   WHERE mission_key=?""",
                (body, next_lifecycle, now, mission_key_value),
            )

    def advance(self, *, worker_id: str = "factory-pcp",
                process=None) -> tuple[PCPMission, ...]:
        """Admit clear PCPs and run Hermes golden-path processing.

        ``process(mission) -> evidence`` is Factory/Hermes continuation. A URL
        inside evidence is recorded as RC-alpha only when the eight-link chain
        is complete. Notion is never consulted. Duplicate path+digest stays
        one mission. ``rc_alpha_for`` checkout serving is gone.
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
                pass
            if row.lifecycle in {"OWNER_SIGNOFF", "PRODUCTION"}:
                advanced.append(row)
                continue
            if row.lifecycle == "OWNER_VALIDATION":
                ov = (row.evidence or {}).get("owner_validation") or {}
                if str(ov.get("decision") or "").upper() != "REJECT":
                    advanced.append(row)
                    continue
            if not self.claim(row.mission_key, worker_id):
                continue
            if callable(process):
                evidence = process(row) or {}
                self.record_evidence(row.mission_key, evidence)
                url = str((evidence.get("rc_alpha") or {}).get("url") or "")
                if url:
                    try:
                        self.set_rc_url(row.mission_key, alpha=url, evidence=evidence)
                    except Exception:
                        pass
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
            evidence=_parse_evidence(row),
        )
