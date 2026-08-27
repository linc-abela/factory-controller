"""Stage 6: the Factory-to-Production boundary, as durable state and refusals.

The Factory produces a verified release candidate.  Production operates it.
This module is the seam between those two sentences, and everything in it
exists to keep one property true: **nothing the Factory can compute may mutate
a real production environment.**  Authority to do that is created by a person,
recorded once, and bound to exactly one immutable bundle.

Four shapes are deliberately absent, and each absence is the enforcement.

There is **no hotfix verb**.  Containment offers rollback, traffic stop and
safe stop; there is no operation anywhere in this module that changes source.
A confirmed defect leaves through :meth:`ProductionLedger.route_defect` as a
bug mission for the Factory, which is the only path back into code.

There is **no place to put a secret**.  An environment declares logical
reference *names*, bounded by a pattern a real token cannot satisfy, and a
bundle's environment schema is a name-to-spec map whose specs may not carry a
value at all.  The boundary is not scanned; it has no container.

There is **no autonomous production class**.  ``autonomous`` is refused at
registration for a production environment, so the gate cannot be configured
away by an operator, an adapter, or a later policy edit.

There is **no self-healing**.  Rollback targets a bundle this ledger already
recorded as healthy.  It cannot build one, choose a newer one, or invent a
target; when no healthy predecessor exists the deployment escalates instead.

Telemetry is never authority.  Health records classify a deployment and can
carry it to ``degraded`` or ``failed``; no health record, alert or adapter
return value can reach ``approved``, declare an incident, or close one.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


#: Reproduced from factory-evidence-core ``src/contracts/replay.py``; the same
#: four words ``routing.CANONICAL_ABSENCE`` and ``store.CANONICAL_ABSENCE``
#: carry.  A deployment fact that is not known is spelled with one of these and
#: is never a zero, a blank, or an estimate.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run", "not_measurable"})

CONTRACT_VERSION = "factory-controller/production/1.0"
BUNDLE_SCHEMA = "factory.controller.release_bundle.v1"

#: The host runtime's name for the projection it reads, adopted verbatim from
#: ``factory-bridge/fixtures/sf-138a-release-bundle.json`` rather than aliased.
COMPAT_SCHEMA = "controller-release-bundle-compat-v1"

#: The only rollback this contract performs: return to a release this ledger
#: already recorded healthy.  Stated once, and carried into the host view so
#: the host is not left to infer a strategy.
ROLLBACK_STRATEGY = "previous-recorded-healthy"

#: What an environment *is*.  The class is not a label: it selects the
#: authority rule, and it is fixed at registration.
ENVIRONMENT_CLASSES = ("local-sim", "staging", "production")

#: The one class that can never deploy without a person.
GATED_CLASSES = frozenset({"production"})

#: Reused from ``portfolio.PROJECT_STATES`` rather than re-spelled: an
#: environment admits new releases only while ``enabled``; ``paused`` and
#: ``draining`` differ in declared intent, not mechanics.
ENVIRONMENT_STATES = ("enabled", "paused", "draining")
ADMITTING = frozenset({"enabled"})

#: The release lifecycle.  ``uncertain`` is a first-class state rather than an
#: error: an operation that may or may not have reached the environment is a
#: fact the ledger has to be able to hold, because the alternative is guessing.
DEPLOYMENT_STATES = (
    "admitted", "awaiting_approval", "approved", "deploying", "verifying",
    "healthy", "degraded", "failed", "rolling_back", "recovered",
    "rollback_failed", "cancelled", "uncertain", "escalated",
)

TERMINAL = frozenset({"healthy", "recovered", "failed", "rollback_failed",
                      "cancelled", "escalated"})

#: A deployment is in flight when it holds, or may hold, a live operation
#: against the environment.  ``uncertain`` counts: that is the whole point.
IN_FLIGHT = frozenset({"admitted", "awaiting_approval", "approved", "deploying",
                       "verifying", "degraded", "rolling_back", "uncertain"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "admitted": frozenset({"awaiting_approval", "approved", "cancelled"}),
    "awaiting_approval": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"deploying", "cancelled"}),
    "deploying": frozenset({"verifying", "failed", "uncertain"}),
    "verifying": frozenset({"healthy", "degraded", "failed", "uncertain"}),
    "degraded": frozenset({"rolling_back", "healthy", "failed", "escalated"}),
    "failed": frozenset({"rolling_back", "escalated"}),
    "rolling_back": frozenset({"recovered", "rollback_failed", "uncertain"}),
    "rollback_failed": frozenset({"escalated"}),
    "uncertain": frozenset({"verifying", "healthy", "degraded", "failed",
                            "rolling_back", "escalated"}),
    "healthy": frozenset({"degraded", "failed"}),
}

#: How a health observation is read.  ``unknown`` is one of the four absence
#: words, not a fifth outcome, and it never advances anything on its own.
HEALTH_OUTCOMES = ("healthy", "degraded", "failed", "unknown")

#: Containment actions.  The list is short on purpose and there is no fourth
#: entry that edits anything.
CONTAINMENT_ACTIONS = ("rollback", "traffic_stop", "safe_stop")

INCIDENT_STATES = ("declared", "classified", "contained", "recovering",
                   "verified", "closed", "escalated")

#: Adopted from ``production-incident-contract.md`` § 2 rather than reinvented.
INCIDENT_CLASSES = ("outage", "triaged_defect")

#: Where an emergency stop reaches.
STOP_SCOPES = ("environment", "project", "portfolio")

#: A logical reference to something held by the operating system or a managed
#: store.  The pattern alone is *not* the boundary: a lower-case token matches
#: it, and pretending otherwise would be a check that reads stronger than it
#: is.  Two other things do the work.  A reference is accepted only from an
#: environment registration -- no bundle, request or adapter has a field it
#: could arrive in -- and it must be namespaced to its own project, so it can
#: neither be a foreign value nor another project's name.
LOGICAL_REF = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

#: An environment variable *declaration* may say what a key is.  It may not say
#: what the key holds: there is no ``default``, no ``example``, no ``value``.
ENV_SPEC_KEYS = frozenset({"type", "required", "description"})
ENV_TYPES = frozenset({"string", "integer", "boolean"})
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

DEFAULT_ENVIRONMENT_CONCURRENCY = 1
DEFAULT_MAX_ROLLBACK_ATTEMPTS = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
  environment_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  environment_class TEXT NOT NULL,
  repository TEXT NOT NULL,
  service_ref TEXT NOT NULL,
  state TEXT NOT NULL,
  autonomous INTEGER NOT NULL DEFAULT 0,
  deployment_concurrency INTEGER NOT NULL,
  max_rollback_attempts INTEGER NOT NULL,
  change_window_json TEXT NOT NULL,
  blast_radius_json TEXT NOT NULL,
  secret_refs_json TEXT NOT NULL,
  approver_refs_json TEXT NOT NULL,
  emergency_stop INTEGER NOT NULL DEFAULT 0,
  policy_version TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS environments_by_project
  ON environments(project_id, environment_id);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY,
  deployment_key TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL REFERENCES environments(environment_id),
  bundle_digest TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  release_sha TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  state TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  approved_by TEXT,
  approval_ref TEXT,
  approved_at REAL,
  operation_key TEXT UNIQUE,
  operation_ref TEXT,
  adapter TEXT,
  rollback_of TEXT,
  rollback_attempts INTEGER NOT NULL DEFAULT 0,
  health_outcome TEXT,
  started_at REAL,
  ended_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deployments_by_environment
  ON deployments(environment_id, id);
CREATE TABLE IF NOT EXISTS production_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  environment_id TEXT,
  deployment_id TEXT,
  incident_ref TEXT,
  kind TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS production_events_by_deployment
  ON production_events(deployment_id, sequence);
CREATE TRIGGER IF NOT EXISTS production_events_no_update
BEFORE UPDATE ON production_events
BEGIN SELECT RAISE(ABORT, 'production events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS production_events_no_delete
BEFORE DELETE ON production_events
BEGIN SELECT RAISE(ABORT, 'production events are append-only'); END;
CREATE TABLE IF NOT EXISTS incidents (
  incident_ref TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL REFERENCES environments(environment_id),
  incident_class TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  declared_at REAL NOT NULL,
  affected_release_sha TEXT NOT NULL,
  affected_bundle_ref TEXT NOT NULL,
  failing_behaviour TEXT NOT NULL,
  blast_radius TEXT NOT NULL,
  state TEXT NOT NULL,
  containment TEXT,
  routed_mission_ref TEXT,
  closed_at REAL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_by_release
  ON incidents(affected_release_sha, incident_ref);
"""


class PolicyError(ValueError):
    """A declaration the Controller will not store, stated as written."""


class ProductionRefusal(Exception):
    """A refusal carried out of a transaction so it outlives the rollback.

    An explanation written next to a ``raise`` inside ``transaction()`` is
    unwound with everything else, taking the record of *why* with it.
    """

    def __init__(self, code: str, detail: str,
                 environment_id: str | None = None,
                 deployment_id: str | None = None) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail
        self.environment_id, self.deployment_id = environment_id, deployment_id

    def as_row(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail,
                "environment_id": self.environment_id,
                "deployment_id": self.deployment_id}


def canonical_json(value: Any) -> str:
    """The Controller's own canonical form, matching ``store.canonical_json``.

    Deliberately *not* Evidence Core's rule, which is a different function over
    different bytes.  A bundle digest is a Controller identity; saying which
    rule minted it is what keeps the two from being compared as if they were
    one.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# the release bundle
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReleaseBundle:
    """The Factory's terminal deliverable, and the only thing deployable.

    Provider- and cloud-neutral by construction: ``artifact`` is whatever
    identity the target actually has, and a repository with no built image
    carries ``not_applicable`` rather than an invented digest.
    """

    bundle_ref: str
    project_id: str
    repository: str
    release_sha: str
    mission_ref: str
    evidence_refs: tuple[str, ...]
    evaluator_receipts: tuple[str, ...]
    artifact: Any
    env_schema: Mapping[str, Mapping[str, Any]]
    migration: Mapping[str, Any]
    release_policy_version: str
    provenance: Mapping[str, Any]
    schema_version: str = BUNDLE_SCHEMA

    @property
    def bundle_digest(self) -> str:
        return digest(self.as_row())

    def as_row(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bundle_ref": self.bundle_ref,
            "project_id": self.project_id,
            "repository": self.repository,
            "release_sha": self.release_sha,
            "mission_ref": self.mission_ref,
            "evidence_refs": list(self.evidence_refs),
            "evaluator_receipts": list(self.evaluator_receipts),
            "artifact": self.artifact,
            "env_schema": {name: dict(spec)
                           for name, spec in sorted(self.env_schema.items())},
            "migration": dict(self.migration),
            "release_policy_version": self.release_policy_version,
            "provenance": dict(self.provenance),
        }

    def compat_view(self, environment: "EnvironmentPolicy") -> dict[str, Any]:
        """The projection the host runtime consumes, derived rather than forked.

        ``factory-bridge``'s ``src/factory_bridge/production.py`` holds a
        ``ReleaseBundle`` it calls *"a small compatibility view over the
        Controller-owned Release Bundle"*, written in parallel with this file
        and therefore against a guess at it.  Its required keys are
        ``release_id``, ``project_id``, ``service_id``, ``candidate_sha``,
        ``release_policy_version``, ``evidence_refs``, and objects at
        ``environment_schema``, ``rollback`` and ``provenance``.

        Rather than record five renames as a conflict and leave the host unable
        to read a real bundle, this emits exactly that shape from the fields
        above.  Nothing is invented: ``service_id`` comes from the environment
        the release is being deployed to, which is where a service binding
        actually lives, and the two evidence lists join because the host view
        has one.  ``COMPAT_SCHEMA`` is their name for it, adopted verbatim.
        """
        return {
            "schema_version": COMPAT_SCHEMA,
            "release_id": self.bundle_ref,
            "project_id": self.project_id,
            "service_id": environment.service_ref,
            "candidate_sha": self.release_sha,
            "release_policy_version": self.release_policy_version,
            "evidence_refs": [*self.evidence_refs, *self.evaluator_receipts],
            "environment_schema": {name: dict(spec)
                                   for name, spec in sorted(self.env_schema.items())},
            "rollback": {"strategy": ROLLBACK_STRATEGY,
                         "reverse_ref": self.migration["reverse_ref"]},
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReleaseBundle":
        if not isinstance(payload, Mapping):
            raise PolicyError("release bundle must be an object")
        if payload.get("schema_version", BUNDLE_SCHEMA) != BUNDLE_SCHEMA:
            raise PolicyError("release bundle schema_version must be %s" % BUNDLE_SCHEMA)
        unexpected = set(payload) - {
            "schema_version", "bundle_ref", "project_id", "repository",
            "release_sha", "mission_ref", "evidence_refs", "evaluator_receipts",
            "artifact", "env_schema", "migration", "release_policy_version",
            "provenance"}
        if unexpected:
            # A field this contract does not define is refused rather than
            # ignored.  An ignored field is how an approval arrives inside the
            # thing being approved.
            raise PolicyError("release bundle carries unknown fields: %s"
                              % ", ".join(sorted(unexpected)))
        release_sha = payload.get("release_sha")
        if not isinstance(release_sha, str) or not SHA_PATTERN.fullmatch(release_sha):
            raise PolicyError("release_sha must be a 40-character lower-case commit id")
        return cls(
            bundle_ref=_text(payload, "bundle_ref"),
            project_id=_text(payload, "project_id"),
            repository=_text(payload, "repository"),
            release_sha=release_sha,
            mission_ref=_text(payload, "mission_ref"),
            evidence_refs=_refs(payload, "evidence_refs"),
            evaluator_receipts=_refs(payload, "evaluator_receipts"),
            artifact=_artifact(payload.get("artifact")),
            env_schema=_env_schema(payload.get("env_schema")),
            migration=_migration(payload.get("migration")),
            release_policy_version=_text(payload, "release_policy_version"),
            provenance=_provenance(payload.get("provenance")),
        )


def _text(payload: Mapping[str, Any], key: str, limit: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise PolicyError("%s must be a non-empty string" % key)
    return value


def _refs(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)) or not value:
        raise PolicyError("%s must be a non-empty list of references" % key)
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 512:
            raise PolicyError("%s entries must be non-empty strings" % key)
        out.append(item)
    return tuple(out)


def _artifact(value: Any) -> Any:
    """An image digest, some other identity, or an absence word.

    A repository that ships no artifact records ``not_applicable``.  It does
    not record an empty string, and it does not borrow the commit id.
    """
    if isinstance(value, str):
        if value not in CANONICAL_ABSENCE:
            raise PolicyError(
                "artifact as a bare string must be one of %s"
                % ", ".join(sorted(CANONICAL_ABSENCE)))
        return value
    if not isinstance(value, Mapping) or not value:
        raise PolicyError("artifact must be an object or an absence value")
    kind = value.get("kind")
    identity = value.get("identity")
    if not isinstance(kind, str) or not kind:
        raise PolicyError("artifact.kind must be a non-empty string")
    if not isinstance(identity, str) or not identity:
        raise PolicyError("artifact.identity must be a non-empty string")
    if set(value) - {"kind", "identity"}:
        raise PolicyError("artifact carries unknown fields")
    # A mutable tag is not an identity: the same name resolves to different
    # bytes tomorrow, which is the one thing a release bundle may not do.
    if identity in ("latest", "main", "staging", "stable", "edge"):
        raise PolicyError("artifact.identity must be immutable, not a moving tag")
    return {"kind": kind, "identity": identity}


def _env_schema(value: Any) -> dict[str, dict[str, Any]]:
    """Names and shapes only.  There is nowhere in here to put a value."""
    if not isinstance(value, Mapping):
        raise PolicyError("env_schema must be an object")
    out: dict[str, dict[str, Any]] = {}
    for name, spec in value.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise PolicyError("env_schema key %r is not an environment name" % (name,))
        if not isinstance(spec, Mapping):
            raise PolicyError("env_schema[%s] must be an object" % name)
        unexpected = set(spec) - ENV_SPEC_KEYS
        if unexpected:
            raise PolicyError(
                "env_schema[%s] may declare only %s; it carries %s"
                % (name, ", ".join(sorted(ENV_SPEC_KEYS)), ", ".join(sorted(unexpected))))
        kind = spec.get("type")
        if kind not in ENV_TYPES:
            raise PolicyError("env_schema[%s].type must be one of %s"
                              % (name, ", ".join(sorted(ENV_TYPES))))
        required = spec.get("required", True)
        if not isinstance(required, bool):
            raise PolicyError("env_schema[%s].required must be boolean" % name)
        description = spec.get("description", "")
        if not isinstance(description, str) or len(description) > 512:
            raise PolicyError("env_schema[%s].description must be a short string" % name)
        out[name] = {"type": kind, "required": required, "description": description}
    return out


def _migration(value: Any) -> dict[str, Any]:
    """Forward and reverse, or a stated absence.  Never a forward-only step."""
    if not isinstance(value, Mapping):
        raise PolicyError("migration must be an object")
    if set(value) - {"forward_ref", "reverse_ref"}:
        raise PolicyError("migration carries unknown fields")
    forward = value.get("forward_ref")
    reverse = value.get("reverse_ref")
    for name, item in (("forward_ref", forward), ("reverse_ref", reverse)):
        if not isinstance(item, str) or not item:
            raise PolicyError("migration.%s must be a reference or an absence value" % name)
    if (forward in CANONICAL_ABSENCE) != (reverse in CANONICAL_ABSENCE):
        # A forward step whose reverse is unknown is precisely the release that
        # cannot be rolled back, so it is refused at the bundle rather than
        # discovered during an incident.
        raise PolicyError(
            "a migration must declare both directions or neither; "
            "a forward step with no reverse cannot be rolled back")
    return {"forward_ref": forward, "reverse_ref": reverse}


def _provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError("provenance must be an object")
    for key in ("built_by", "built_at", "contract_version"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise PolicyError("provenance.%s is required" % key)
    if set(value) - {"built_by", "built_at", "contract_version"}:
        raise PolicyError("provenance carries unknown fields")
    return dict(value)


# --------------------------------------------------------------------------- #
# environment policy
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvironmentPolicy:
    """One operated environment's declared envelope.

    ``approver_refs`` is the Owner's own list of who may approve a release
    here.  It is the entire authority mechanism: a caller is an approver
    because the Owner wrote it into the environment, never because it says so.
    """

    environment_id: str
    project_id: str
    environment_class: str
    repository: str
    service_ref: str
    approver_refs: tuple[str, ...]
    state: str = "enabled"
    autonomous: bool = False
    deployment_concurrency: int = DEFAULT_ENVIRONMENT_CONCURRENCY
    max_rollback_attempts: int = DEFAULT_MAX_ROLLBACK_ATTEMPTS
    change_window: Mapping[str, Any] = field(default_factory=dict)
    blast_radius: Mapping[str, Any] = field(default_factory=dict)
    secret_refs: tuple[str, ...] = ()
    policy_version: str = "unset"

    def __post_init__(self) -> None:
        if self.environment_class not in ENVIRONMENT_CLASSES:
            raise PolicyError("environment_class must be one of %s"
                              % ", ".join(ENVIRONMENT_CLASSES))
        if self.state not in ENVIRONMENT_STATES:
            raise PolicyError("environment state must be one of %s"
                              % ", ".join(ENVIRONMENT_STATES))
        for name, value in (("environment_id", self.environment_id),
                            ("project_id", self.project_id),
                            ("repository", self.repository),
                            ("service_ref", self.service_ref)):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise PolicyError("%s must be a non-empty string" % name)
        if self.autonomous and self.environment_class in GATED_CLASSES:
            # Not a default that can be flipped: a production environment has
            # no representation in which it deploys without a person.
            raise PolicyError(
                "a %s environment cannot be autonomous; a release here is "
                "approved by a person or it does not happen"
                % self.environment_class)
        if not self.approver_refs and self.environment_class in GATED_CLASSES:
            raise PolicyError(
                "a %s environment must declare at least one approver"
                % self.environment_class)
        for ref in self.approver_refs:
            if not isinstance(ref, str) or not ref or len(ref) > 128:
                raise PolicyError("approver references must be short strings")
        if len(set(self.approver_refs)) != len(self.approver_refs):
            raise PolicyError("approver references must be unique")
        for ref in self.secret_refs:
            if not isinstance(ref, str) or not LOGICAL_REF.fullmatch(ref):
                raise PolicyError(
                    "a secret reference is a logical name matching %s"
                    % LOGICAL_REF.pattern)
            if not ref.startswith(self.project_id + "."):
                raise PolicyError(
                    "secret reference %r must be namespaced to %s; a name that "
                    "is not this project's is either a foreign project's or "
                    "not a name at all" % (ref, self.project_id))
        for name, value in (("deployment_concurrency", self.deployment_concurrency),
                            ("max_rollback_attempts", self.max_rollback_attempts)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError("%s must be a non-negative integer" % name)
        if self.deployment_concurrency < 1:
            raise PolicyError("deployment_concurrency must be at least 1")
        for name, value in (("change_window", self.change_window),
                            ("blast_radius", self.blast_radius)):
            if not isinstance(value, Mapping):
                raise PolicyError("%s must be an object" % name)

    @property
    def gated(self) -> bool:
        return self.environment_class in GATED_CLASSES or not self.autonomous


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HealthRecord:
    """What an environment reported, as observed.  Not a decision."""

    checks_passed: int
    checks_failed: int
    evidence_ref: str
    observed_at: float

    def as_row(self) -> dict[str, Any]:
        return {"checks_passed": self.checks_passed,
                "checks_failed": self.checks_failed,
                "evidence_ref": self.evidence_ref,
                "observed_at": self.observed_at}


def classify_health(record: HealthRecord | None) -> str:
    """Read an observation.  Absence of an observation is ``unknown``.

    ``unknown`` is one of the four absence words rather than a fifth outcome,
    and it advances nothing: a deployment with no health evidence stays where
    it is until someone or something produces evidence.
    """
    if record is None:
        return "unknown"
    if record.checks_failed and record.checks_passed:
        return "degraded"
    if record.checks_failed:
        return "failed"
    if record.checks_passed:
        return "healthy"
    return "unknown"


# --------------------------------------------------------------------------- #
# the deployment adapter seam
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DeploymentOutcome:
    """What an adapter reports.  ``reached`` is deliberately three-valued.

    ``None`` means the adapter cannot say whether the environment was touched.
    That is not a failure and not a success; it is the fact that produces
    ``uncertain``, and an adapter that cannot tell must be able to say so.
    """

    reached: bool | None
    operation_ref: str
    adapter: str
    detail: str = ""


class DeploymentPort(Protocol):
    """The smallest surface a real deployment mechanism has to offer.

    Everything host- or cloud-specific lives behind this and outside this
    repository.  The Controller never learns what a deployment *is*.
    """

    name: str

    def deploy(self, bundle: ReleaseBundle, environment: EnvironmentPolicy,
               operation_key: str) -> DeploymentOutcome: ...

    def rollback(self, bundle: ReleaseBundle, environment: EnvironmentPolicy,
                 operation_key: str) -> DeploymentOutcome: ...


class DeterministicDeploymentAdapter:
    """A fake that reaches nothing, for local simulation and for tests.

    It mints an operation reference from the operation key alone, so the same
    key always yields the same reference and a duplicate call is visible as a
    duplicate rather than as two different operations.
    """

    name = "deterministic"

    def __init__(self, reached: bool | None = True) -> None:
        self._reached = reached
        self.calls: list[tuple[str, str]] = []

    def deploy(self, bundle: ReleaseBundle, environment: EnvironmentPolicy,
               operation_key: str) -> DeploymentOutcome:
        self.calls.append(("deploy", operation_key))
        return self._outcome("deploy", operation_key)

    def rollback(self, bundle: ReleaseBundle, environment: EnvironmentPolicy,
                 operation_key: str) -> DeploymentOutcome:
        self.calls.append(("rollback", operation_key))
        return self._outcome("rollback", operation_key)

    def _outcome(self, verb: str, operation_key: str) -> DeploymentOutcome:
        return DeploymentOutcome(
            reached=self._reached,
            operation_ref="%s:%s" % (verb, digest({"k": operation_key})[:16]),
            adapter=self.name,
            detail="no environment was contacted")


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #

def deployment_key(project_id: str, environment_id: str, bundle_digest: str,
                   attempt: int) -> str:
    """The identity a duplicate request collides with.

    The bundle digest is in the key because deploying *different bytes* is a
    different deployment; ``attempt`` is in it because deploying the same bytes
    again after a terminal outcome is a deliberate second act and has to be
    nameable.  What it is not is a free retry: an unresolved ``uncertain``
    deployment for the environment refuses the next admission outright.
    """
    return "%s:%s:%s:%d" % (project_id, environment_id, bundle_digest, attempt)


def operation_key(deployment_id: str, verb: str, attempt: int) -> str:
    return "%s:%s:%d" % (deployment_id, verb, attempt)


class ProductionLedger:
    """Durable Stage-6 state, on the mission store's own connection.

    It borrows the store rather than opening a second database: a deployment
    and the mission that produced its candidate have to be able to move under
    one transaction, and two files cannot do that.
    """

    def __init__(self, store) -> None:
        self._store = store
        with store.transaction() as db:
            db.executescript(SCHEMA)

    # -- environments ------------------------------------------------------ #

    def register_environment(self, policy: EnvironmentPolicy) -> None:
        now = time.time()
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT environment_class, project_id FROM environments"
                " WHERE environment_id=?", (policy.environment_id,)).fetchone()
            if existing is not None and (
                    existing["environment_class"] != policy.environment_class
                    or existing["project_id"] != policy.project_id):
                # Class selects the authority rule.  Letting it change under an
                # existing id would let a gated environment become an
                # autonomous one without a new registration.
                raise PolicyError(
                    "environment %s is already registered with a different "
                    "class or project" % policy.environment_id)
            db.execute(
                "INSERT INTO environments VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(environment_id) DO UPDATE SET"
                "  repository=excluded.repository,"
                "  service_ref=excluded.service_ref, state=excluded.state,"
                "  autonomous=excluded.autonomous,"
                "  deployment_concurrency=excluded.deployment_concurrency,"
                "  max_rollback_attempts=excluded.max_rollback_attempts,"
                "  change_window_json=excluded.change_window_json,"
                "  blast_radius_json=excluded.blast_radius_json,"
                "  secret_refs_json=excluded.secret_refs_json,"
                "  approver_refs_json=excluded.approver_refs_json,"
                "  policy_version=excluded.policy_version,"
                "  updated_at=excluded.updated_at",
                (policy.environment_id, policy.project_id,
                 policy.environment_class, policy.repository, policy.service_ref,
                 policy.state, int(policy.autonomous),
                 policy.deployment_concurrency, policy.max_rollback_attempts,
                 canonical_json(dict(policy.change_window)),
                 canonical_json(dict(policy.blast_radius)),
                 canonical_json(list(policy.secret_refs)),
                 canonical_json(list(policy.approver_refs)),
                 0, policy.policy_version, now, now))
            self._append(db, "environment_registered", policy.project_id,
                         environment_id=policy.environment_id,
                         detail={"environment_class": policy.environment_class,
                                 "autonomous": policy.autonomous,
                                 "state": policy.state})

    def environment(self, environment_id: str) -> EnvironmentPolicy:
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM environments WHERE environment_id=?",
                             (environment_id,)).fetchone()
        if row is None:
            raise ProductionRefusal("ENVIRONMENT_NOT_REGISTERED",
                                    "environment %s is not registered" % environment_id,
                                    environment_id=environment_id)
        return _policy_from_row(row)

    def environments(self, project_id: str) -> tuple[EnvironmentPolicy, ...]:
        """Scoped by project.  There is no unscoped listing on purpose."""
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM environments WHERE project_id=?"
                " ORDER BY environment_id", (project_id,)).fetchall()
        return tuple(_policy_from_row(row) for row in rows)

    def set_environment_state(self, environment_id: str, state: str) -> None:
        if state not in ENVIRONMENT_STATES:
            raise PolicyError("environment state must be one of %s"
                              % ", ".join(ENVIRONMENT_STATES))
        with self._store.transaction() as db:
            row = self._environment_row(db, environment_id)
            db.execute("UPDATE environments SET state=?, updated_at=?"
                       " WHERE environment_id=?", (state, time.time(), environment_id))
            self._append(db, "environment_state", row["project_id"],
                         environment_id=environment_id,
                         from_state=row["state"], to_state=state, detail={})

    def emergency_stop(self, scope: str, *, project_id: str | None = None,
                       environment_id: str | None = None,
                       engaged: bool = True) -> tuple[str, ...]:
        """Stop reaching exactly as far as the caller said, and no further."""
        if scope not in STOP_SCOPES:
            raise PolicyError("stop scope must be one of %s" % ", ".join(STOP_SCOPES))
        with self._store.transaction() as db:
            if scope == "environment":
                if not environment_id:
                    raise PolicyError("an environment stop names an environment")
                rows = [self._environment_row(db, environment_id)]
            elif scope == "project":
                if not project_id:
                    raise PolicyError("a project stop names a project")
                rows = db.execute("SELECT * FROM environments WHERE project_id=?",
                                  (project_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM environments").fetchall()
            stopped = []
            for row in rows:
                db.execute("UPDATE environments SET emergency_stop=?, updated_at=?"
                           " WHERE environment_id=?",
                           (int(engaged), time.time(), row["environment_id"]))
                self._append(db, "emergency_stop", row["project_id"],
                             environment_id=row["environment_id"],
                             detail={"scope": scope, "engaged": engaged})
                stopped.append(row["environment_id"])
        return tuple(sorted(stopped))

    # -- admission --------------------------------------------------------- #

    def admit_release(self, bundle: ReleaseBundle, environment_id: str,
                      requested_by: str, attempt: int = 1) -> str:
        """Take custody of a bundle for one environment, or refuse and say why."""
        refusal = None
        deployment_id = None
        with self._store.transaction() as db:
            row = self._environment_row(db, environment_id)
            policy = _policy_from_row(row)
            key = deployment_key(policy.project_id, environment_id,
                                 bundle.bundle_digest, attempt)
            existing = db.execute(
                "SELECT id, bundle_digest, state FROM deployments WHERE deployment_key=?",
                (key,)).fetchone()
            if existing is not None:
                # The same bundle, the same environment, the same attempt: this
                # is the same deployment, not a second one.
                return existing["id"]
            refusal = self._admission_refusal(db, policy, bundle, environment_id)
            if refusal is None:
                deployment_id = "dep_%s" % uuid.uuid4().hex[:16]
                state = "awaiting_approval" if policy.gated else "approved"
                now = time.time()
                db.execute(
                    "INSERT INTO deployments (id, deployment_key, project_id,"
                    " environment_id, bundle_digest, bundle_json, release_sha,"
                    " attempt, state, requested_by, rollback_attempts,"
                    " created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (deployment_id, key, policy.project_id, environment_id,
                     bundle.bundle_digest, canonical_json(bundle.as_row()),
                     bundle.release_sha, attempt, state, requested_by, now, now))
                self._append(db, "release_admitted", policy.project_id,
                             environment_id=environment_id,
                             deployment_id=deployment_id,
                             to_state=state,
                             detail={"bundle_digest": bundle.bundle_digest,
                                     "release_sha": bundle.release_sha,
                                     "requested_by": requested_by,
                                     "attempt": attempt})
            else:
                self._append(db, "release_refused", policy.project_id,
                             environment_id=environment_id,
                             detail=refusal.as_row())
        if refusal is not None:
            raise refusal
        return deployment_id

    def _admission_refusal(self, db, policy: EnvironmentPolicy,
                           bundle: ReleaseBundle,
                           environment_id: str) -> ProductionRefusal | None:
        if bundle.project_id != policy.project_id:
            return ProductionRefusal(
                "ENVIRONMENT_PROJECT_MISMATCH",
                "bundle belongs to %s and environment %s belongs to %s"
                % (bundle.project_id, environment_id, policy.project_id),
                environment_id=environment_id)
        if bundle.repository != policy.repository:
            return ProductionRefusal(
                "ENVIRONMENT_REPOSITORY_MISMATCH",
                "bundle repository does not match the environment binding",
                environment_id=environment_id)
        stop = db.execute("SELECT emergency_stop FROM environments"
                          " WHERE environment_id=?", (environment_id,)).fetchone()
        if stop["emergency_stop"]:
            return ProductionRefusal(
                "EMERGENCY_STOP_ENGAGED",
                "environment %s is under an emergency stop" % environment_id,
                environment_id=environment_id)
        if policy.state not in ADMITTING:
            return ProductionRefusal(
                "ENVIRONMENT_NOT_ADMITTING",
                "environment %s is %s" % (environment_id, policy.state),
                environment_id=environment_id)
        unresolved = db.execute(
            "SELECT id FROM deployments WHERE environment_id=? AND state='uncertain'",
            (environment_id,)).fetchone()
        if unresolved is not None:
            return ProductionRefusal(
                "DEPLOYMENT_UNCERTAIN_UNRESOLVED",
                "deployment %s may or may not have reached %s; a new release "
                "here would be a blind duplicate"
                % (unresolved["id"], environment_id),
                environment_id=environment_id,
                deployment_id=unresolved["id"])
        in_flight = db.execute(
            "SELECT COUNT(*) AS n FROM deployments WHERE environment_id=?"
            " AND state IN (%s)" % ",".join("?" * len(IN_FLIGHT)),
            (environment_id, *sorted(IN_FLIGHT))).fetchone()["n"]
        if in_flight >= policy.deployment_concurrency:
            return ProductionRefusal(
                "ENVIRONMENT_CONCURRENCY_EXCEEDED",
                "environment %s already has %d release(s) in flight"
                % (environment_id, in_flight),
                environment_id=environment_id)
        return None

    # -- authority --------------------------------------------------------- #

    def approve(self, deployment_id: str, approved_by: str, approval_ref: str,
                bundle_digest: str) -> None:
        """Create the authority a gated environment requires.

        Three things have to hold at once, and each closes a different way in:
        the approver is one the Owner listed on this environment; the approver
        is not the requester; and the approval names the exact bundle digest it
        is approving, so an approval cannot be carried onto other bytes.
        """
        refusal = None
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            policy = _policy_from_row(self._environment_row(db, row["environment_id"]))
            if approved_by not in policy.approver_refs:
                refusal = ProductionRefusal(
                    "PRODUCTION_APPROVAL_UNAUTHORIZED",
                    "%r is not an approver the Owner declared for %s"
                    % (approved_by, policy.environment_id),
                    environment_id=policy.environment_id,
                    deployment_id=deployment_id)
            elif approved_by == row["requested_by"]:
                refusal = ProductionRefusal(
                    "PRODUCTION_APPROVAL_SELF",
                    "the actor that requested this release cannot approve it",
                    environment_id=policy.environment_id,
                    deployment_id=deployment_id)
            elif bundle_digest != row["bundle_digest"]:
                refusal = ProductionRefusal(
                    "PRODUCTION_APPROVAL_BUNDLE_MISMATCH",
                    "approval names a different bundle than the deployment holds",
                    environment_id=policy.environment_id,
                    deployment_id=deployment_id)
            elif row["state"] != "awaiting_approval":
                refusal = self._transition_refusal(row, "approved")
            if refusal is None:
                now = time.time()
                db.execute(
                    "UPDATE deployments SET state='approved', approved_by=?,"
                    " approval_ref=?, approved_at=?, updated_at=? WHERE id=?",
                    (approved_by, approval_ref, now, now, deployment_id))
                self._append(db, "release_approved", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             from_state=row["state"], to_state="approved",
                             detail={"approved_by": approved_by,
                                     "approval_ref": approval_ref,
                                     "bundle_digest": bundle_digest})
            else:
                self._append(db, "approval_refused", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             detail=refusal.as_row())
        if refusal is not None:
            raise refusal

    # -- execution --------------------------------------------------------- #

    def deploy(self, deployment_id: str, port: DeploymentPort) -> str:
        """Start exactly one deployment operation, or refuse to start any.

        The operation key is claimed in its own transaction *before* the port
        is called and is unique in the table, so a second call cannot mint a
        second operation even if the first is still running.
        """
        key = self._claim_operation(deployment_id, "deploy")
        bundle, policy = self._bundle_and_policy(deployment_id)
        try:
            outcome = port.deploy(bundle, policy, key)
        except Exception as exc:                                   # noqa: BLE001
            # The port raised after the key was claimed: whether anything
            # reached the environment is exactly what nobody knows.
            self._settle(deployment_id, "uncertain", adapter=getattr(port, "name", "unknown"),
                         detail={"reason": "adapter raised %s" % type(exc).__name__})
            return "uncertain"
        return self._record_outcome(deployment_id, outcome, verb="deploy")

    def rollback(self, deployment_id: str, port: DeploymentPort) -> str:
        """Return the environment to a bundle this ledger recorded as healthy.

        The target is looked up, never chosen: there is no path here that
        builds, selects or promotes anything, which is what keeps bounded
        recovery from becoming self-healing.
        """
        refusal = None
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            policy = _policy_from_row(self._environment_row(db, row["environment_id"]))
            if row["rollback_attempts"] >= policy.max_rollback_attempts:
                refusal = ProductionRefusal(
                    "ROLLBACK_ATTEMPTS_EXHAUSTED",
                    "environment %s permits %d rollback attempt(s)"
                    % (policy.environment_id, policy.max_rollback_attempts),
                    environment_id=policy.environment_id,
                    deployment_id=deployment_id)
            else:
                target = db.execute(
                    "SELECT id, bundle_json FROM deployments WHERE environment_id=?"
                    " AND state IN ('healthy','recovered') AND id!=?"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (row["environment_id"], deployment_id)).fetchone()
                if target is None:
                    refusal = ProductionRefusal(
                        "ROLLBACK_TARGET_UNKNOWN",
                        "no previously healthy release is recorded for %s"
                        % policy.environment_id,
                        environment_id=policy.environment_id,
                        deployment_id=deployment_id)
            if refusal is None:
                db.execute(
                    "UPDATE deployments SET state='rolling_back', rollback_of=?,"
                    " rollback_attempts=rollback_attempts+1, updated_at=?"
                    " WHERE id=?", (target["id"], time.time(), deployment_id))
                self._append(db, "rollback_started", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             from_state=row["state"], to_state="rolling_back",
                             detail={"rollback_of": target["id"]})
        if refusal is not None:
            with self._store.transaction() as db:
                self._append(db, "rollback_refused",
                             self._deployment_row(db, deployment_id)["project_id"],
                             environment_id=refusal.environment_id,
                             deployment_id=deployment_id, detail=refusal.as_row())
            raise refusal
        key = self._claim_operation(deployment_id, "rollback")
        bundle, policy = self._bundle_and_policy(deployment_id)
        try:
            outcome = port.rollback(bundle, policy, key)
        except Exception as exc:                                   # noqa: BLE001
            self._settle(deployment_id, "uncertain",
                         adapter=getattr(port, "name", "unknown"),
                         detail={"reason": "adapter raised %s" % type(exc).__name__})
            return "uncertain"
        if outcome.reached is None:
            return self._settle(deployment_id, "uncertain", adapter=outcome.adapter,
                                operation_ref=outcome.operation_ref,
                                detail={"detail": outcome.detail})
        if not outcome.reached:
            return self._settle(deployment_id, "rollback_failed",
                                adapter=outcome.adapter,
                                operation_ref=outcome.operation_ref,
                                detail={"detail": outcome.detail})
        return self._settle(deployment_id, "recovered", adapter=outcome.adapter,
                            operation_ref=outcome.operation_ref,
                            detail={"detail": outcome.detail})

    def record_health(self, deployment_id: str, record: HealthRecord | None) -> str:
        """Classify an observation.  This can never create authority.

        ``healthy`` and ``degraded`` and ``failed`` are all reachable from
        here; ``approved`` is not, and neither is any incident state.
        """
        outcome = classify_health(record)
        if outcome == "unknown":
            with self._store.transaction() as db:
                row = self._deployment_row(db, deployment_id)
                self._append(db, "health_unknown", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             detail={"health_outcome": "unknown"})
            return "unknown"
        return self._settle(deployment_id, outcome,
                            detail={"health": record.as_row() if record else None,
                                    "health_outcome": outcome},
                            health_outcome=outcome)

    def record_slo_event(self, environment_id: str, name: str, value: Any,
                         observed_at: float) -> None:
        """Append an operational signal.  Nothing reads it as a decision."""
        with self._store.transaction() as db:
            row = self._environment_row(db, environment_id)
            self._append(db, "slo_event", row["project_id"],
                         environment_id=environment_id,
                         detail={"name": name, "value": value,
                                 "observed_at": observed_at,
                                 "authority": "not_applicable"})

    def cancel(self, deployment_id: str, reason: str) -> str:
        return self._settle(deployment_id, "cancelled", detail={"reason": reason})

    def escalate(self, deployment_id: str, reason: str) -> str:
        return self._settle(deployment_id, "escalated", detail={"reason": reason})

    def reconcile(self, deployment_id: str, observed_state: str,
                  evidence_ref: str) -> str:
        """Resolve an ``uncertain`` deployment against something observed.

        The only exit from ``uncertain`` runs through here and requires a
        reference to whatever was looked at.  A retry is not an exit.
        """
        if observed_state not in ALLOWED_TRANSITIONS["uncertain"]:
            raise PolicyError(
                "an uncertain deployment resolves to one of %s"
                % ", ".join(sorted(ALLOWED_TRANSITIONS["uncertain"])))
        return self._settle(deployment_id, observed_state,
                            detail={"reconciled_from": "uncertain",
                                    "evidence_ref": evidence_ref})

    def reconcile_on_restart(self, stale_after_seconds: float = 0.0) -> tuple[str, ...]:
        """After a crash, every operation that may be in flight becomes uncertain.

        Not retried, not failed, not assumed complete.  The process that knew
        what it had started is gone, so the ledger records that it does not
        know either, and admission is closed for that environment until a
        person or an observation resolves it.
        """
        moved = []
        cutoff = time.time() - stale_after_seconds
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM deployments WHERE state IN ('deploying','verifying')"
                " AND updated_at<=?", (cutoff,)).fetchall()
            for row in rows:
                db.execute("UPDATE deployments SET state='uncertain', updated_at=?"
                           " WHERE id=?", (time.time(), row["id"]))
                self._append(db, "restart_reconciliation", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=row["id"], from_state=row["state"],
                             to_state="uncertain",
                             detail={"reason": "process restarted mid-operation"})
                moved.append(row["id"])
        return tuple(moved)

    # -- incidents --------------------------------------------------------- #

    def declare_incident(self, *, incident_ref: str, environment_id: str,
                         declared_by: str, incident_class: str,
                         affected_release_sha: str, affected_bundle_ref: str,
                         failing_behaviour: str, blast_radius: str) -> None:
        """Only a person declares an incident.

        ``production-incident-contract.md`` I-4 requires a human identity and
        says plainly it is *never a tool, agent or alert*.  Here that is the
        same list the Owner wrote on the environment: telemetry can move a
        deployment to ``degraded`` and can do nothing whatever to an incident.
        """
        if incident_class not in INCIDENT_CLASSES:
            raise PolicyError("incident_class must be one of %s"
                              % ", ".join(INCIDENT_CLASSES))
        if not SHA_PATTERN.fullmatch(affected_release_sha):
            raise PolicyError("affected_release_sha must be a 40-character commit id")
        if not blast_radius:
            raise PolicyError("a blast-radius statement is required and may not be empty")
        refusal = None
        with self._store.transaction() as db:
            row = self._environment_row(db, environment_id)
            policy = _policy_from_row(row)
            if declared_by not in policy.approver_refs:
                refusal = ProductionRefusal(
                    "INCIDENT_DECLARATION_UNAUTHORIZED",
                    "%r is not a person the Owner declared for %s; an alert, "
                    "an adapter or a model cannot declare an incident"
                    % (declared_by, environment_id),
                    environment_id=environment_id)
            elif db.execute("SELECT 1 FROM incidents WHERE incident_ref=?",
                            (incident_ref,)).fetchone() is not None:
                refusal = ProductionRefusal(
                    "INCIDENT_REF_REUSED",
                    "incident reference %s is already allocated" % incident_ref,
                    environment_id=environment_id)
            if refusal is None:
                now = time.time()
                db.execute(
                    "INSERT INTO incidents (incident_ref, project_id,"
                    " environment_id, incident_class, declared_by, declared_at,"
                    " affected_release_sha, affected_bundle_ref,"
                    " failing_behaviour, blast_radius, state, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?, 'declared', ?)",
                    (incident_ref, policy.project_id, environment_id,
                     incident_class, declared_by, now, affected_release_sha,
                     affected_bundle_ref, failing_behaviour, blast_radius, now))
                self._append(db, "incident_declared", policy.project_id,
                             environment_id=environment_id,
                             incident_ref=incident_ref, to_state="declared",
                             detail={"declared_by": declared_by,
                                     "incident_class": incident_class,
                                     "affected_release_sha": affected_release_sha})
            else:
                self._append(db, "incident_refused", policy.project_id,
                             environment_id=environment_id,
                             detail=refusal.as_row())
        if refusal is not None:
            raise refusal

    def contain(self, incident_ref: str, action: str) -> None:
        """Containment is rollback, traffic stop, or safe stop.

        There is no fourth action, and in particular there is no action that
        changes source.  A production hotfix is not refused at run time here;
        it is unrepresentable.
        """
        if action not in CONTAINMENT_ACTIONS:
            raise PolicyError(
                "containment is one of %s; changing source in Production is "
                "not an action this contract has"
                % ", ".join(CONTAINMENT_ACTIONS))
        with self._store.transaction() as db:
            row = self._incident_row(db, incident_ref)
            db.execute("UPDATE incidents SET state='contained', containment=?,"
                       " updated_at=? WHERE incident_ref=?",
                       (action, time.time(), incident_ref))
            self._append(db, "incident_contained", row["project_id"],
                         environment_id=row["environment_id"],
                         incident_ref=incident_ref, from_state=row["state"],
                         to_state="contained", detail={"action": action})

    def route_defect(self, incident_ref: str, work_item_id: str,
                     summary: str) -> dict[str, Any]:
        """Send a confirmed defect back into the Factory as a bug mission.

        This returns a mission payload rather than performing a change: the
        only way a fix reaches source is by being built and verified through
        the same lifecycle as any other release.
        """
        with self._store.transaction() as db:
            row = self._incident_row(db, incident_ref)
            environment = self._environment_row(db, row["environment_id"])
            payload = {
                "work_item_id": work_item_id,
                "project_id": row["project_id"],
                "repository": environment["repository"],
                "capability": "bug",
                "baseline_sha": row["affected_release_sha"],
                "incident_ref": incident_ref,
                "summary": summary,
                "origin": "production_incident",
            }
            db.execute("UPDATE incidents SET routed_mission_ref=?, state='recovering',"
                       " updated_at=? WHERE incident_ref=?",
                       (work_item_id, time.time(), incident_ref))
            self._append(db, "defect_routed", row["project_id"],
                         environment_id=row["environment_id"],
                         incident_ref=incident_ref, from_state=row["state"],
                         to_state="recovering", detail=payload)
        return payload

    def close_incident(self, incident_ref: str, verified_by: str,
                       evidence_ref: str) -> None:
        refusal = None
        with self._store.transaction() as db:
            row = self._incident_row(db, incident_ref)
            policy = _policy_from_row(self._environment_row(db, row["environment_id"]))
            if verified_by not in policy.approver_refs:
                refusal = ProductionRefusal(
                    "INCIDENT_CLOSURE_UNAUTHORIZED",
                    "%r cannot close an incident on %s"
                    % (verified_by, row["environment_id"]),
                    environment_id=row["environment_id"])
            if refusal is None:
                now = time.time()
                db.execute("UPDATE incidents SET state='closed', closed_at=?,"
                           " updated_at=? WHERE incident_ref=?",
                           (now, now, incident_ref))
                self._append(db, "incident_closed", row["project_id"],
                             environment_id=row["environment_id"],
                             incident_ref=incident_ref, from_state=row["state"],
                             to_state="closed",
                             detail={"verified_by": verified_by,
                                     "evidence_ref": evidence_ref})
        if refusal is not None:
            raise refusal

    # -- reading ----------------------------------------------------------- #

    def deployment(self, deployment_id: str) -> dict[str, Any]:
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
        return dict(row)

    def receipt(self, deployment_id: str) -> dict[str, Any]:
        """What actually happened, with unknown facts spelled as absences."""
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            environment = self._environment_row(db, row["environment_id"])
            events = db.execute(
                "SELECT kind, from_state, to_state, created_at FROM production_events"
                " WHERE deployment_id=? ORDER BY sequence", (deployment_id,)).fetchall()
        return {
            "contract_version": CONTRACT_VERSION,
            "deployment_id": deployment_id,
            "project_id": row["project_id"],
            "environment_id": row["environment_id"],
            "environment_class": environment["environment_class"],
            "release_sha": row["release_sha"],
            "bundle_digest": row["bundle_digest"],
            "state": row["state"],
            "operation_key": _absent(row["operation_key"], "not_run"),
            "operation_ref": _absent(row["operation_ref"], "not_run"),
            "adapter": _absent(row["adapter"], "not_run"),
            "approved_by": _absent(row["approved_by"], "not_applicable"
                                   if not _policy_from_row(environment).gated
                                   else "not_run"),
            "approval_ref": _absent(row["approval_ref"], "not_applicable"
                                    if not _policy_from_row(environment).gated
                                    else "not_run"),
            "health_outcome": _absent(row["health_outcome"], "not_run"),
            "rollback_of": _absent(row["rollback_of"], "not_applicable"),
            "rollback_attempts": row["rollback_attempts"],
            "started_at": _absent(row["started_at"], "not_run"),
            "ended_at": _absent(row["ended_at"], "not_run"),
            "transitions": [dict(event) for event in events],
        }

    def correlate(self, release_sha: str) -> dict[str, Any]:
        """Release to incident, by the identity both already carry."""
        with self._store.transaction() as db:
            deployments = db.execute(
                "SELECT id, environment_id, state FROM deployments"
                " WHERE release_sha=? ORDER BY created_at", (release_sha,)).fetchall()
            incidents = db.execute(
                "SELECT incident_ref, state, incident_class FROM incidents"
                " WHERE affected_release_sha=? ORDER BY declared_at",
                (release_sha,)).fetchall()
        return {"release_sha": release_sha,
                "deployments": [dict(row) for row in deployments],
                "incidents": [dict(row) for row in incidents]}

    def events(self, project_id: str) -> tuple[dict[str, Any], ...]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM production_events WHERE project_id=? ORDER BY sequence",
                (project_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    # -- internals --------------------------------------------------------- #

    def _claim_operation(self, deployment_id: str, verb: str) -> str:
        """Take the one operation slot, or refuse.  Nothing runs before this."""
        refusal = None
        key = None
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            target = "deploying" if verb == "deploy" else "rolling_back"
            if verb == "deploy" and row["state"] != "approved":
                refusal = ProductionRefusal(
                    "PRODUCTION_APPROVAL_REQUIRED"
                    if row["state"] == "awaiting_approval"
                    else "DEPLOYMENT_STATE_INVALID",
                    "a deployment starts from 'approved'; this one is %r"
                    % row["state"],
                    environment_id=row["environment_id"],
                    deployment_id=deployment_id)
            elif db.execute("SELECT emergency_stop FROM environments"
                            " WHERE environment_id=?",
                            (row["environment_id"],)).fetchone()["emergency_stop"]:
                refusal = ProductionRefusal(
                    "EMERGENCY_STOP_ENGAGED",
                    "environment %s is under an emergency stop"
                    % row["environment_id"],
                    environment_id=row["environment_id"],
                    deployment_id=deployment_id)
            if refusal is None:
                # The state machine is what makes this exactly-once: no
                # transition returns a deployment to a state a claim can start
                # from, so a second call never gets here.  The UNIQUE index on
                # operation_key is the backstop under that, not the mechanism.
                key = operation_key(deployment_id, verb, row["rollback_attempts"])
                now = time.time()
                db.execute(
                    "UPDATE deployments SET state=?, operation_key=?,"
                    " started_at=COALESCE(started_at,?), updated_at=?"
                    " WHERE id=?", (target, key, now, now, deployment_id))
                self._append(db, "operation_claimed", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             from_state=row["state"], to_state=target,
                             detail={"operation_key": key, "verb": verb})
        if refusal is not None:
            raise refusal
        return key

    def _record_outcome(self, deployment_id: str, outcome: DeploymentOutcome,
                        verb: str) -> str:
        if outcome.reached is None:
            state = "uncertain"
        elif outcome.reached:
            state = "verifying"
        else:
            state = "failed"
        return self._settle(deployment_id, state, adapter=outcome.adapter,
                            operation_ref=outcome.operation_ref,
                            detail={"verb": verb, "detail": outcome.detail})

    def _settle(self, deployment_id: str, state: str, *,
                adapter: str | None = None, operation_ref: str | None = None,
                health_outcome: str | None = None,
                detail: Mapping[str, Any] | None = None) -> str:
        refusal = None
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            if state != row["state"] and state not in ALLOWED_TRANSITIONS.get(
                    row["state"], frozenset()):
                refusal = self._transition_refusal(row, state)
            if refusal is None:
                now = time.time()
                db.execute(
                    "UPDATE deployments SET state=?, adapter=COALESCE(?, adapter),"
                    " operation_ref=COALESCE(?, operation_ref),"
                    " health_outcome=COALESCE(?, health_outcome),"
                    " ended_at=CASE WHEN ? THEN ? ELSE ended_at END, updated_at=?"
                    " WHERE id=?",
                    (state, adapter, operation_ref, health_outcome,
                     int(state in TERMINAL), now, now, deployment_id))
                self._append(db, "deployment_state", row["project_id"],
                             environment_id=row["environment_id"],
                             deployment_id=deployment_id,
                             from_state=row["state"], to_state=state,
                             detail=dict(detail or {}))
        if refusal is not None:
            raise refusal
        return state

    @staticmethod
    def _transition_refusal(row, state: str) -> ProductionRefusal:
        return ProductionRefusal(
            "DEPLOYMENT_TRANSITION_INVALID",
            "a deployment in %r does not move to %r" % (row["state"], state),
            environment_id=row["environment_id"], deployment_id=row["id"])

    def _bundle_and_policy(self, deployment_id: str):
        with self._store.transaction() as db:
            row = self._deployment_row(db, deployment_id)
            environment = self._environment_row(db, row["environment_id"])
        return (ReleaseBundle.from_payload(json.loads(row["bundle_json"])),
                _policy_from_row(environment))

    @staticmethod
    def _environment_row(db, environment_id: str):
        row = db.execute("SELECT * FROM environments WHERE environment_id=?",
                         (environment_id,)).fetchone()
        if row is None:
            raise ProductionRefusal("ENVIRONMENT_NOT_REGISTERED",
                                    "environment %s is not registered" % environment_id,
                                    environment_id=environment_id)
        return row

    @staticmethod
    def _deployment_row(db, deployment_id: str):
        row = db.execute("SELECT * FROM deployments WHERE id=?",
                         (deployment_id,)).fetchone()
        if row is None:
            raise ProductionRefusal("DEPLOYMENT_NOT_FOUND",
                                    "deployment %s is not recorded" % deployment_id,
                                    deployment_id=deployment_id)
        return row

    @staticmethod
    def _incident_row(db, incident_ref: str):
        row = db.execute("SELECT * FROM incidents WHERE incident_ref=?",
                         (incident_ref,)).fetchone()
        if row is None:
            raise ProductionRefusal("INCIDENT_NOT_FOUND",
                                    "incident %s is not recorded" % incident_ref)
        return row

    @staticmethod
    def _append(db, kind: str, project_id: str, *, environment_id=None,
                deployment_id=None, incident_ref=None, from_state=None,
                to_state=None, detail: Mapping[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO production_events (project_id, environment_id,"
            " deployment_id, incident_ref, kind, from_state, to_state,"
            " detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, environment_id, deployment_id, incident_ref, kind,
             from_state, to_state, canonical_json(dict(detail or {})), time.time()))


def _absent(value: Any, word: str) -> Any:
    """A fact that is not there is spelled, never blanked and never zeroed."""
    if word not in CANONICAL_ABSENCE:
        raise PolicyError("absence must be one of %s"
                          % ", ".join(sorted(CANONICAL_ABSENCE)))
    return word if value is None else value


def _policy_from_row(row) -> EnvironmentPolicy:
    return EnvironmentPolicy(
        environment_id=row["environment_id"],
        project_id=row["project_id"],
        environment_class=row["environment_class"],
        repository=row["repository"],
        service_ref=row["service_ref"],
        approver_refs=tuple(json.loads(row["approver_refs_json"])),
        state=row["state"],
        autonomous=bool(row["autonomous"]),
        deployment_concurrency=row["deployment_concurrency"],
        max_rollback_attempts=row["max_rollback_attempts"],
        change_window=json.loads(row["change_window_json"]),
        blast_radius=json.loads(row["blast_radius_json"]),
        secret_refs=tuple(json.loads(row["secret_refs_json"])),
        policy_version=row["policy_version"],
    )
