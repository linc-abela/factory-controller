"""Durable Phase-1 Release Candidate and same-artifact lifecycle.

This is an orchestration layer over the existing ``ProductionLedger``.  It
adds only the missing product-release records: an immutable RC seal, the
review deployment identity, and Owner Validation.  Production deployment,
approval, health, and rollback remain the existing ledger's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, Mapping

from . import production


RC_SCHEMA_VERSION = "factory.controller.release_candidate.v1"
ARTIFACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DECISIONS = frozenset({"VALIDATED", "RETURN_FOR_CHANGES"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS release_candidates (
  rc_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  mission_ref TEXT NOT NULL,
  candidate_sha TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  bundle_digest TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  verification_refs_json TEXT NOT NULL,
  qa_refs_json TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  sealed_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS release_candidates_no_update
BEFORE UPDATE ON release_candidates
BEGIN SELECT RAISE(ABORT, 'release candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS release_candidates_no_delete
BEFORE DELETE ON release_candidates
BEGIN SELECT RAISE(ABORT, 'release candidates are immutable'); END;
CREATE TABLE IF NOT EXISTS release_deployments (
  deployment_ref TEXT PRIMARY KEY,
  rc_id TEXT NOT NULL REFERENCES release_candidates(rc_id),
  deployment_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  environment_class TEXT NOT NULL,
  candidate_sha TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  bundle_digest TEXT NOT NULL,
  validation_surface TEXT,
  state TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS release_deployments_identity
  ON release_deployments(rc_id, environment_id);
CREATE TRIGGER IF NOT EXISTS release_deployments_no_update
BEFORE UPDATE ON release_deployments
BEGIN SELECT RAISE(ABORT, 'release deployment records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS release_deployments_no_delete
BEFORE DELETE ON release_deployments
BEGIN SELECT RAISE(ABORT, 'release deployment records are immutable'); END;
CREATE TABLE IF NOT EXISTS owner_validations (
  validation_id TEXT PRIMARY KEY,
  rc_id TEXT NOT NULL REFERENCES release_candidates(rc_id),
  deployment_ref TEXT NOT NULL REFERENCES release_deployments(deployment_ref),
  candidate_sha TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  validation_surface TEXT NOT NULL,
  validation_environment TEXT NOT NULL,
  decision TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  decided_at REAL NOT NULL,
  notes TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS owner_validations_no_update
BEFORE UPDATE ON owner_validations
BEGIN SELECT RAISE(ABORT, 'owner validations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS owner_validations_no_delete
BEFORE DELETE ON owner_validations
BEGIN SELECT RAISE(ABORT, 'owner validations are immutable'); END;
CREATE TABLE IF NOT EXISTS release_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  rc_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS release_events_by_rc ON release_events(rc_id, sequence);
CREATE TRIGGER IF NOT EXISTS release_events_no_update
BEFORE UPDATE ON release_events
BEGIN SELECT RAISE(ABORT, 'release events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS release_events_no_delete
BEFORE DELETE ON release_events
BEGIN SELECT RAISE(ABORT, 'release events are append-only'); END;
"""


class ReleaseRefusal(ValueError):
    """A release lifecycle operation refused without changing authority."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReleaseCandidate:
    rc_id: str
    project_id: str
    mission_ref: str
    candidate_sha: str
    artifact_digest: str
    bundle_digest: str
    verification_refs: tuple[str, ...]
    qa_refs: tuple[str, ...]
    manifest_digest: str
    sealed_at: float

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": RC_SCHEMA_VERSION,
            "rc_id": self.rc_id,
            "project_id": self.project_id,
            "mission_ref": self.mission_ref,
            "candidate_sha": self.candidate_sha,
            "artifact_digest": self.artifact_digest,
            "bundle_digest": self.bundle_digest,
            "verification_refs": list(self.verification_refs),
            "qa_refs": list(self.qa_refs),
            "manifest_digest": self.manifest_digest,
            "sealed_at": self.sealed_at,
        }


@dataclass(frozen=True)
class OwnerValidation:
    validation_id: str
    rc_id: str
    deployment_ref: str
    candidate_sha: str
    artifact_digest: str
    validation_surface: str
    validation_environment: str
    decision: str
    decided_by: str
    decided_at: float
    notes: str

    def as_row(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "rc_id": self.rc_id,
            "deployment_ref": self.deployment_ref,
            "candidate_sha": self.candidate_sha,
            "artifact_digest": self.artifact_digest,
            "validation_surface": self.validation_surface,
            "validation_environment": self.validation_environment,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "notes": self.notes,
        }


def _refs(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ReleaseRefusal("RELEASE_EVIDENCE_MISSING", "%s must be non-empty" % field)
    values = tuple(value)
    if any(not isinstance(item, str) or not item or len(item) > 1024 for item in values):
        raise ReleaseRefusal("RELEASE_EVIDENCE_INVALID", "%s contains an invalid ref" % field)
    return values


def _surface(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ReleaseRefusal("REVIEW_SURFACE_INVALID", "review surface is required")
    if value.startswith("https://"):
        return value
    if value.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
        return value
    raise ReleaseRefusal("REVIEW_SURFACE_INVALID",
                         "review surface must be HTTPS or an explicit local target")


def _artifact_digest(bundle: production.ReleaseBundle) -> str:
    artifact = bundle.artifact
    if (not isinstance(artifact, Mapping)
            or artifact.get("kind") not in {"image", "static-bundle", "web-bundle"}
            or not isinstance(artifact.get("identity"), str)
            or not ARTIFACT_DIGEST.fullmatch(artifact["identity"])):
        raise ReleaseRefusal(
            "IMMUTABLE_ARTIFACT_REQUIRED",
            "a Release Candidate requires a sha256 artifact identity, not a tag or absence",
        )
    return artifact["identity"]


def _candidate(row: Mapping[str, Any]) -> ReleaseCandidate:
    return ReleaseCandidate(
        rc_id=row["rc_id"], project_id=row["project_id"],
        mission_ref=row["mission_ref"], candidate_sha=row["candidate_sha"],
        artifact_digest=row["artifact_digest"], bundle_digest=row["bundle_digest"],
        verification_refs=tuple(json.loads(row["verification_refs_json"])),
        qa_refs=tuple(json.loads(row["qa_refs_json"])),
        manifest_digest=row["manifest_digest"], sealed_at=row["sealed_at"],
    )


def _validation(row: Mapping[str, Any]) -> OwnerValidation:
    return OwnerValidation(
        validation_id=row["validation_id"], rc_id=row["rc_id"],
        deployment_ref=row["deployment_ref"], candidate_sha=row["candidate_sha"],
        artifact_digest=row["artifact_digest"], validation_surface=row["validation_surface"],
        validation_environment=row["validation_environment"], decision=row["decision"],
        decided_by=row["decided_by"], decided_at=row["decided_at"], notes=row["notes"],
    )


class ReleaseLifecycle:
    """The product-facing release path, backed by the Controller's DB."""

    def __init__(self, store, *, clock=time.time) -> None:
        self._store = store
        self._clock = clock
        with store.transaction() as db:
            db.executescript(SCHEMA)

    def seal(self, rc_id: str, bundle: production.ReleaseBundle,
             *, verification_refs: Any, qa_refs: Any) -> ReleaseCandidate:
        if not isinstance(rc_id, str) or not RC_ID.fullmatch(rc_id):
            raise ReleaseRefusal("RC_ID_INVALID", "rc_id is malformed")
        if not isinstance(bundle, production.ReleaseBundle):
            raise ReleaseRefusal("RC_BUNDLE_INVALID", "seal requires a Controller ReleaseBundle")
        artifact_digest = _artifact_digest(bundle)
        verified = _refs(verification_refs, "verification_refs")
        qa = _refs(qa_refs, "qa_refs")
        manifest = {
            "schema_version": RC_SCHEMA_VERSION,
            "rc_id": rc_id,
            "project_id": bundle.project_id,
            "mission_ref": bundle.mission_ref,
            "candidate_sha": bundle.release_sha,
            "artifact_digest": artifact_digest,
            "bundle_digest": bundle.bundle_digest,
            "verification_refs": list(verified),
            "qa_refs": list(qa),
        }
        manifest_digest = production.digest(manifest)
        with self._store.transaction() as db:
            existing = db.execute("SELECT * FROM release_candidates WHERE rc_id=?",
                                  (rc_id,)).fetchone()
            if existing is not None:
                same = (existing["bundle_digest"] == bundle.bundle_digest
                        and existing["artifact_digest"] == artifact_digest
                        and existing["manifest_digest"] == manifest_digest)
                if not same:
                    raise ReleaseRefusal("RC_IDENTITY_MISMATCH",
                                         "rc_id is already sealed to different bytes")
                return _candidate(existing)
            db.execute(
                "INSERT INTO release_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rc_id, bundle.project_id, bundle.mission_ref, bundle.release_sha,
                 artifact_digest, bundle.bundle_digest,
                 production.canonical_json(bundle.as_row()), production.canonical_json(list(verified)),
                 production.canonical_json(list(qa)), manifest_digest, self._clock()))
            self._event(db, rc_id, "rc_sealed", manifest)
            row = db.execute("SELECT * FROM release_candidates WHERE rc_id=?",
                             (rc_id,)).fetchone()
        return _candidate(row)

    def candidate(self, rc_id: str) -> ReleaseCandidate:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM release_candidates WHERE rc_id=?",
                             (rc_id,)).fetchone()
        if row is None:
            raise ReleaseRefusal("RC_NOT_FOUND", rc_id)
        return _candidate(row)

    def _bundle(self, rc_id: str) -> tuple[ReleaseCandidate, production.ReleaseBundle]:
        rc = self.candidate(rc_id)
        with self._store.transaction() as db:
            row = db.execute("SELECT bundle_json FROM release_candidates WHERE rc_id=?",
                             (rc_id,)).fetchone()
        return rc, production.ReleaseBundle.from_payload(json.loads(row["bundle_json"]))

    def deploy_review(self, rc_id: str, ledger: production.ProductionLedger,
                      port: production.DeploymentPort, *,
                      review_environment_id: str, requested_by: str,
                      review_url: str, health: production.HealthRecord | None = None
                      ) -> dict[str, Any]:
        rc, bundle = self._bundle(rc_id)
        surface = _surface(review_url)
        policy = ledger.environment(review_environment_id)
        if policy.environment_class != "staging":
            raise ReleaseRefusal("REVIEW_ENVIRONMENT_REQUIRED",
                                 "the Owner surface must be a staging environment")
        deployment_id = ledger.admit_release(bundle, review_environment_id, requested_by)
        state = ledger.deployment(deployment_id)["state"]
        if state == "approved":
            state = ledger.deploy(deployment_id, port)
        if state == "verifying" and health is not None:
            state = ledger.record_health(deployment_id, health)
        elif state == "verifying":
            state = "health_pending"
        deployment_ref = "review:%s:%s" % (rc_id, review_environment_id)
        row = {
            "deployment_ref": deployment_ref, "rc_id": rc_id,
            "deployment_id": deployment_id, "environment_id": review_environment_id,
            "environment_class": policy.environment_class, "candidate_sha": rc.candidate_sha,
            "artifact_digest": rc.artifact_digest, "bundle_digest": rc.bundle_digest,
            "validation_surface": surface, "state": state, "created_at": self._clock(),
        }
        self._record_deployment(row)
        self._event_simple(rc_id, "review_deployed", {
            "deployment_ref": deployment_ref, "deployment_id": deployment_id,
            "artifact_digest": rc.artifact_digest, "review_url": surface, "state": state,
        })
        return {**row, "receipt": ledger.receipt(deployment_id)}

    def record_owner_validation(self, validation_id: str, rc_id: str, *,
                                deployment_ref: str, decision: str,
                                decided_by: str, decided_at: float,
                                notes: str = "") -> OwnerValidation:
        if not isinstance(validation_id, str) or not RC_ID.fullmatch(validation_id):
            raise ReleaseRefusal("VALIDATION_ID_INVALID", "validation_id is malformed")
        if decision not in DECISIONS:
            raise ReleaseRefusal("OWNER_VALIDATION_INVALID", "decision must be VALIDATED or RETURN_FOR_CHANGES")
        if not isinstance(decided_by, str) or not decided_by or len(decided_by) > 256:
            raise ReleaseRefusal("OWNER_VALIDATION_INVALID", "decided_by is required")
        if not isinstance(decided_at, (int, float)) or isinstance(decided_at, bool):
            raise ReleaseRefusal("OWNER_VALIDATION_INVALID", "decided_at is required")
        rc = self.candidate(rc_id)
        with self._store.transaction() as db:
            deployment = db.execute(
                "SELECT * FROM release_deployments WHERE deployment_ref=? AND rc_id=?",
                (deployment_ref, rc_id)).fetchone()
            if deployment is None:
                raise ReleaseRefusal("REVIEW_DEPLOYMENT_NOT_FOUND", deployment_ref)
            if deployment["environment_class"] != "staging":
                raise ReleaseRefusal("REVIEW_ENVIRONMENT_REQUIRED", deployment_ref)
            deployment_id = deployment["deployment_id"]
            prod_row = db.execute("SELECT state, bundle_digest, release_sha FROM deployments WHERE id=?",
                                  (deployment_id,)).fetchone()
            if prod_row is None or prod_row["state"] != "healthy":
                raise ReleaseRefusal("REVIEW_NOT_HEALTHY", "Owner Validation requires a healthy exact review deployment")
            if (prod_row["bundle_digest"] != rc.bundle_digest
                    or prod_row["release_sha"] != rc.candidate_sha):
                raise ReleaseRefusal("REVIEW_ARTIFACT_MISMATCH", "review deployment is not the sealed RC")
            existing = db.execute("SELECT * FROM owner_validations WHERE validation_id=?",
                                  (validation_id,)).fetchone()
            if existing is not None:
                requested = (existing["rc_id"], existing["deployment_ref"], existing["decision"],
                             existing["decided_by"], existing["artifact_digest"])
                actual = (rc_id, deployment_ref, decision, decided_by, rc.artifact_digest)
                if requested != actual:
                    raise ReleaseRefusal("VALIDATION_IDENTITY_MISMATCH",
                                         "validation_id is already bound to another decision")
                return _validation(existing)
            row = (
                validation_id, rc_id, deployment_ref, rc.candidate_sha,
                rc.artifact_digest, deployment["validation_surface"],
                deployment["environment_id"], decision, decided_by, float(decided_at),
                notes[:2048] if isinstance(notes, str) else "",
            )
            db.execute("INSERT INTO owner_validations VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
            self._event(db, rc_id, "owner_validation_recorded", {
                "validation_id": validation_id, "deployment_ref": deployment_ref,
                "decision": decision, "candidate_sha": rc.candidate_sha,
                "artifact_digest": rc.artifact_digest, "decided_by": decided_by,
            })
            stored = db.execute("SELECT * FROM owner_validations WHERE validation_id=?",
                                (validation_id,)).fetchone()
        return _validation(stored)

    def owner_validation(self, validation_id: str) -> OwnerValidation:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM owner_validations WHERE validation_id=?",
                             (validation_id,)).fetchone()
        if row is None:
            raise ReleaseRefusal("OWNER_VALIDATION_NOT_FOUND", validation_id)
        return _validation(row)

    def promote_validated(self, rc_id: str, validation_id: str,
                          ledger: production.ProductionLedger,
                          port: production.DeploymentPort, *,
                          production_environment_id: str, requested_by: str,
                          artifact_digest: str | None = None,
                          candidate_sha: str | None = None,
                          health: production.HealthRecord | None = None
                          ) -> dict[str, Any]:
        rc, bundle = self._bundle(rc_id)
        validation = self.owner_validation(validation_id)
        if validation.rc_id != rc_id or validation.decision != "VALIDATED":
            raise ReleaseRefusal("OWNER_VALIDATION_REQUIRED",
                                 "Production promotion requires VALIDATED for this RC")
        if validation.artifact_digest != rc.artifact_digest:
            raise ReleaseRefusal("OWNER_VALIDATION_INVALID", "stored validation does not match the sealed RC")
        if artifact_digest is not None and artifact_digest != rc.artifact_digest:
            raise ReleaseRefusal("ARTIFACT_IDENTITY_MISMATCH",
                                 "a rebuilt or mutated artifact cannot use prior Owner Validation")
        if candidate_sha is not None and candidate_sha != rc.candidate_sha:
            raise ReleaseRefusal("CANDIDATE_IDENTITY_MISMATCH",
                                 "a different candidate cannot use prior Owner Validation")
        # An Owner Validation is a decision about a review that was healthy
        # when they made it. Between then and here that same review can be
        # observed failed, and promotion read only the stored decision -- so a
        # release the environment had already reported broken could still be
        # admitted to Production. The ordering the chain claims is: healthy
        # exact REVIEW *now*, then Owner Validation, then same-artifact
        # Production.
        with self._store.transaction() as db:
            review = db.execute(
                "SELECT deployment_id FROM release_deployments WHERE rc_id=?"
                " AND environment_class='staging'", (rc_id,)).fetchone()
            state = None if review is None else db.execute(
                "SELECT state FROM deployments WHERE id=?",
                (review["deployment_id"],)).fetchone()
        if review is None:
            raise ReleaseRefusal("REVIEW_DEPLOYMENT_NOT_FOUND", rc_id)
        if state is None or state["state"] != "healthy":
            raise ReleaseRefusal(
                "REVIEW_NOT_HEALTHY",
                "Production promotion requires the exact review deployment to "
                "be healthy now, not only when it was validated")
        policy = ledger.environment(production_environment_id)
        if policy.environment_class != "production":
            raise ReleaseRefusal("PRODUCTION_ENVIRONMENT_REQUIRED",
                                 "promotion target must be a production environment")
        if policy.project_id != rc.project_id:
            raise ReleaseRefusal("PRODUCTION_PROJECT_MISMATCH", "production target belongs to another project")
        deployment_id = ledger.admit_release(bundle, production_environment_id, requested_by)
        deployment = ledger.deployment(deployment_id)
        if deployment["state"] == "awaiting_approval":
            ledger.approve(deployment_id, validation.decided_by, validation_id,
                           rc.bundle_digest)
        state = ledger.deployment(deployment_id)["state"]
        if state == "approved":
            state = ledger.deploy(deployment_id, port)
        if state == "verifying" and health is not None:
            state = ledger.record_health(deployment_id, health)
        elif state == "verifying":
            state = "health_pending"
        deployment_ref = "production:%s:%s" % (rc_id, production_environment_id)
        row = {
            "deployment_ref": deployment_ref, "rc_id": rc_id,
            "deployment_id": deployment_id, "environment_id": production_environment_id,
            "environment_class": policy.environment_class, "candidate_sha": rc.candidate_sha,
            "artifact_digest": rc.artifact_digest, "bundle_digest": rc.bundle_digest,
            "validation_surface": None, "state": state, "created_at": self._clock(),
        }
        self._record_deployment(row)
        self._event_simple(rc_id, "production_promoted", {
            "deployment_ref": deployment_ref, "deployment_id": deployment_id,
            "validation_id": validation_id, "artifact_digest": rc.artifact_digest,
            "state": state,
        })
        return {**row, "receipt": ledger.receipt(deployment_id),
                "same_artifact": rc.artifact_digest == validation.artifact_digest}

    def rollback_production(self, rc_id: str, ledger: production.ProductionLedger,
                            port: production.DeploymentPort, *,
                            production_environment_id: str) -> dict[str, Any]:
        rc = self.candidate(rc_id)
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT * FROM release_deployments WHERE rc_id=? AND environment_id=?"
                " AND environment_class='production'",
                (rc_id, production_environment_id)).fetchone()
        if row is None:
            raise ReleaseRefusal("PRODUCTION_DEPLOYMENT_NOT_FOUND", rc_id)
        state = ledger.rollback(row["deployment_id"], port)
        receipt = ledger.receipt(row["deployment_id"])
        self._event_simple(rc_id, "production_rollback", {
            "deployment_id": row["deployment_id"], "state": state,
            "restored_from": receipt.get("rollback_of"),
        })
        return {"rc_id": rc_id, "deployment_id": row["deployment_id"],
                "state": state, "receipt": receipt}

    def reset_review(self, rc_id: str, *, resetter: Any) -> dict[str, Any]:
        """Reset only through an explicit target adapter seam; never fake it."""

        rc = self.candidate(rc_id)
        if not callable(resetter):
            raise ReleaseRefusal("REVIEW_RESET_UNAVAILABLE",
                                 "review target did not provide a reset operation")
        result = resetter(rc)
        if not isinstance(result, Mapping) or result.get("reset") is not True:
            raise ReleaseRefusal("REVIEW_RESET_UNPROVEN",
                                 "review reset did not return a proven reset fact")
        self._event_simple(rc_id, "review_reset", dict(result))
        return {"rc_id": rc_id, **dict(result)}

    def events(self, rc_id: str) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute("SELECT * FROM release_events WHERE rc_id=? ORDER BY sequence",
                             (rc_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    def _record_deployment(self, row: Mapping[str, Any]) -> None:
        with self._store.transaction() as db:
            existing = db.execute("SELECT * FROM release_deployments WHERE deployment_ref=?",
                                  (row["deployment_ref"],)).fetchone()
            if existing is not None:
                fields = ("rc_id", "deployment_id", "environment_id", "candidate_sha",
                          "artifact_digest", "bundle_digest", "validation_surface")
                if any(existing[field] != row[field] for field in fields):
                    raise ReleaseRefusal("RELEASE_DEPLOYMENT_IDENTITY_MISMATCH",
                                         "deployment reference is already bound")
                return
            db.execute(
                "INSERT INTO release_deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[field] for field in (
                    "deployment_ref", "rc_id", "deployment_id", "environment_id",
                    "environment_class", "candidate_sha", "artifact_digest",
                    "bundle_digest", "validation_surface", "state", "created_at")))

    def _event(self, db, rc_id: str, kind: str, detail: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO release_events(rc_id,kind,detail_json,created_at) VALUES (?,?,?,?)",
                   (rc_id, kind, production.canonical_json(dict(detail)), self._clock()))

    def _event_simple(self, rc_id: str, kind: str, detail: Mapping[str, Any]) -> None:
        with self._store.transaction() as db:
            self._event(db, rc_id, kind, detail)


__all__ = [
    "DECISIONS", "OwnerValidation", "RC_SCHEMA_VERSION", "ReleaseCandidate",
    "ReleaseLifecycle", "ReleaseRefusal",
]
