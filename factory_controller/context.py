"""Context request, manifest verification, and context economics.

The Controller declares *what context a mission is entitled to* and checks that
what came back is the context the mission is bound to.  It never opens a
repository file, never orders or scores one, and never decides which files
matter.  That selection is the Context Broker's, and this module is deliberately
unable to do it: it holds no file system access at all.

Three shapes here are reproduced from ``factory-evidence-core``, not invented,
in the same way ``routing.CANONICAL_ABSENCE`` is.  Each is pinned by a test so a
fork is a failure rather than a drift:

* ``ContextManifest`` -- ``src/contracts/mvp.py``.  Seven fields, and the
  ``manifest_hash`` is the sha256 of the other six under Evidence Core's own
  canonical encoding.  ``src/evidence/validation.py`` re-derives exactly that,
  so a manifest this module accepts is one Evidence Core will also accept.
* ``RetrievalReceipt`` -- the same file.  A deterministic selection receipt,
  and explicitly never an authoritative fact source.
* ``CONTEXT_HASH_MISMATCH`` / ``CONTEXT_MISSION_MISMATCH`` -- the refusal names
  ``validate_collection`` already raises for these two conditions.  A second
  spelling for a condition production code already names would be the identity
  divergence the corpus records, so they are adopted rather than prefixed.

Everything measured about a manifest lives beside it in ``ContextMeasurement``
rather than inside it.  Byte counts change when a repository changes; manifest
identity must not, because that identity is what an evidence chain is bound to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence


#: Reproduced from factory-evidence-core ``src/contracts/mvp.py``.
CONTEXT_SCHEMA_VERSION = "1.0"

#: The broker's answer to one request.  ``built`` is the only one that carries a
#: manifest; the other two carry a refusal code and nothing else.
BROKER_STATUSES = ("built", "refused", "unavailable")

#: What a cache said about itself.  ``unknown`` is a canonical absence word and
#: is what an unreporting broker gets -- never a guessed miss.
CACHE_STATES = ("hit", "miss", "unknown")


class ContextError(ValueError):
    """The mission's declared context request is unusable as written."""


# --------------------------------------------------------------------------- #
# Evidence Core's canonical encoding, reproduced exactly
# --------------------------------------------------------------------------- #

def canonical_bytes(value: Any) -> bytes:
    """``src/render/canonical.py``.  Note the trailing newline and unescaped
    non-ASCII: this differs from ``store.canonical_json`` on both counts, and a
    manifest digest computed under the store's rule would never match."""

    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and \
        all(char in "0123456789abcdef" for char in value)


# --------------------------------------------------------------------------- #
# what the Controller declares
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ContextRequest:
    """The mission's context entitlement.  Declarative, and only declarative.

    ``mission_input_hash`` binds a manifest to *this* mission's immutable input.
    No upstream document defined how that value is derived -- Evidence Core
    consumes it and its own fixtures carry it pre-computed -- so the Controller
    derives it here, from the mission identity fields alone, and sends it to the
    broker to echo back.  One derivation, in one place, re-derivable by anyone.
    """

    corpus_identity: str
    policy_identity: str
    mission_input_hash: str
    required_anchors: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    repository_remote_url: str | None = None
    baseline_sha: str | None = None
    purpose: str | None = None
    max_age_seconds: float | None = None
    #: Mirrored from the mission's context budget so the ceiling travels with
    #: the entitlement.  Declared once, enforced twice: the broker refuses its
    #: own overrun, and the Controller re-checks the measurement it gets back.
    max_bytes: int | None = None
    max_files: int | None = None
    schema_version: str = CONTEXT_SCHEMA_VERSION

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "ContextRequest | None":
        raw = (payload or {}).get("context_request")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ContextError("context_request must be an object")
        corpus = _required_str(raw, "corpus_identity")
        policy = _required_str(raw, "policy_identity")
        budget = ContextBudget.from_payload(payload)
        age = _optional_number(raw, "max_age_seconds")
        if age is not None and age <= 0:
            raise ContextError("max_age_seconds must be positive")
        return cls(
            corpus_identity=corpus,
            policy_identity=policy,
            mission_input_hash=mission_input_hash(payload or {}),
            required_anchors=_string_tuple(raw, "required_anchors"),
            allowed_paths=_string_tuple(raw, "allowed_paths"),
            denied_paths=_string_tuple(raw, "denied_paths"),
            repository_remote_url=_optional_str(raw, "repository_remote_url")
            or _optional_str(payload or {}, "repository_remote_url"),
            baseline_sha=_optional_str(raw, "baseline_sha")
            or _optional_str(payload or {}, "baseline_sha"),
            purpose=_optional_str(raw, "purpose"),
            max_age_seconds=age,
            max_bytes=budget.max_bytes,
            max_files=budget.max_files,
        )

    def as_wire(self) -> dict[str, Any]:
        """What the broker is handed.  No mission state, no policy internals."""

        return {
            "schema_version": self.schema_version,
            "corpus_identity": self.corpus_identity,
            "policy_identity": self.policy_identity,
            "mission_input_hash": self.mission_input_hash,
            "required_anchors": list(self.required_anchors),
            "allowed_paths": list(self.allowed_paths),
            "denied_paths": list(self.denied_paths),
            "repository_remote_url": self.repository_remote_url,
            "baseline_sha": self.baseline_sha,
            "purpose": self.purpose,
            "max_bytes": self.max_bytes,
            "max_files": self.max_files,
        }


#: The mission fields that make one mission a different mission.  Retry counts,
#: provider policy and budgets are deliberately absent: changing a ceiling must
#: not orphan a manifest that is still correct for the same work.
MISSION_IDENTITY_FIELDS = ("work_item_id", "capability", "repository_remote_url",
                           "baseline_sha", "acceptance_gate_ids", "execution_mode")


def mission_input_hash(payload: dict[str, Any]) -> str:
    """Derive the mission input identity a manifest must be bound to."""

    value = {name: payload.get(name) for name in MISSION_IDENTITY_FIELDS}
    gates = value.get("acceptance_gate_ids")
    value["acceptance_gate_ids"] = sorted(gates) if isinstance(gates, (list, tuple)) else None
    return sha256_hex(value)


@dataclass(frozen=True)
class ContextBudget:
    """A hard ceiling on materialized context, beside the spending ceiling.

    ``max_reported_input_tokens`` is checked only against tokens a provider
    actually reported.  An unreported count stays unknown and never becomes a
    refusal: refusing on an estimate would be the invented number the whole
    absence vocabulary exists to prevent.
    """

    max_bytes: int | None = None
    max_files: int | None = None
    max_reported_input_tokens: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "ContextBudget":
        raw = (payload or {}).get("context_budget") or {}
        if not isinstance(raw, dict):
            raise ContextError("context_budget must be an object")
        budget = cls(
            max_bytes=_optional_int(raw, "max_bytes"),
            max_files=_optional_int(raw, "max_files"),
            max_reported_input_tokens=_optional_int(raw, "max_reported_input_tokens"),
        )
        for name in ("max_bytes", "max_files", "max_reported_input_tokens"):
            value = getattr(budget, name)
            if value is not None and value <= 0:
                raise ContextError("%s must be positive" % name)
        return budget

    @property
    def declared(self) -> bool:
        return any(value is not None for value in
                   (self.max_bytes, self.max_files, self.max_reported_input_tokens))


# --------------------------------------------------------------------------- #
# what the broker returns
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ContextManifest:
    """Reproduced from ``src/contracts/mvp.py``.  Identity only, no economics."""

    schema_version: str
    mission_input_hash: str
    manifest_hash: str
    corpus_identity: str
    policy_identity: str
    selected_refs: tuple[str, ...]
    unresolved_questions: tuple[str, ...]

    def unhashed(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_input_hash": self.mission_input_hash,
            "corpus_identity": self.corpus_identity,
            "policy_identity": self.policy_identity,
            "selected_refs": list(self.selected_refs),
            "unresolved_questions": list(self.unresolved_questions),
        }

    @property
    def derived_hash(self) -> str:
        """``src/evidence/validation.py`` re-derives the digest exactly so."""

        return sha256_hex(self.unhashed())

    @property
    def intact(self) -> bool:
        return (self.derived_hash == self.manifest_hash
                and _is_sha256(self.manifest_hash)
                and _is_sha256(self.mission_input_hash)
                and len(set(self.selected_refs)) == len(self.selected_refs))


@dataclass(frozen=True)
class ContextMeasurement:
    """Measured facts about one build.  Bytes and files are exact or absent.

    Nothing here is an estimate and nothing here is a token count: bytes are not
    tokens, and the Controller has no tokenizer.  Reported provider token usage
    is a different record entirely and lives on the execution receipt.
    """

    baseline_context_bytes: int | None = None
    baseline_context_files: int | None = None
    selected_context_bytes: int | None = None
    selected_context_files: int | None = None
    manifest_build_ms: int | None = None
    cache_state: str = "unknown"
    cache_identity: str | None = None
    built_at: float | None = None
    head_sha: str | None = None
    repository_remote_url: str | None = None
    #: The broker's own opaque content-addressed reference to what it built.
    #: The Controller carries it and never parses it: it is the broker's
    #: identity for the materialized package, not the Controller's for the
    #: mission, and the two must not be collapsed into one.
    broker_manifest_digest: str | None = None
    policy_digest: str | None = None
    #: A broker's statement about token counting.  Always a canonical absence
    #: word unless a real count was supplied; bytes are never converted.
    context_token_count: Any = "unknown"

    @property
    def reduction(self) -> dict[str, Any]:
        """Baseline versus selected.  Absent inputs make an absent answer."""

        base, chosen = self.baseline_context_bytes, self.selected_context_bytes
        if base is None or chosen is None:
            return {"state": "not_measurable"}
        if base == 0:
            return {"state": "not_applicable", "baseline_context_bytes": 0}
        return {
            "state": "measured",
            "baseline_context_bytes": base,
            "selected_context_bytes": chosen,
            "saved_bytes": base - chosen,
            "reduction_ratio": round((base - chosen) / base, 6),
        }


@dataclass(frozen=True)
class RetrievalReceipt:
    """Reproduced from ``src/contracts/mvp.py``.  Never an authority."""

    schema_version: str = CONTEXT_SCHEMA_VERSION
    context_manifest_hash: str = ""
    selected_refs: tuple[str, ...] = ()
    excluded_refs: tuple[str, ...] = ()
    mandatory_fact_coverage: tuple[str, ...] = ()
    refusal_code: str | None = None


@dataclass(frozen=True)
class ContextPackage:
    """One broker answer, read as facts.  Anything unparseable stays absent."""

    status: str
    manifest: ContextManifest | None = None
    receipt: RetrievalReceipt = field(default_factory=RetrievalReceipt)
    measurement: ContextMeasurement = field(default_factory=ContextMeasurement)
    refusal_code: str | None = None

    @classmethod
    def from_response(cls, raw: Any) -> "ContextPackage":
        if not isinstance(raw, dict):
            return cls("unavailable", refusal_code="CONTEXT_BROKER_UNREADABLE")
        status = raw.get("status")
        status = status if status in BROKER_STATUSES else "unavailable"
        manifest = _manifest_from(raw.get("manifest"))
        code = raw.get("refusal_code") or raw.get("diagnostic")
        return cls(
            status="built" if status == "built" and manifest is not None else
            ("unavailable" if status == "built" else status),
            manifest=manifest,
            receipt=_receipt_from(raw.get("receipt")),
            measurement=_measurement_from(raw.get("measurement")),
            refusal_code=code if isinstance(code, str) and code else None,
        )

    def as_row(self) -> dict[str, Any]:
        """The durable projection.  This is what a later run is bound to."""

        return {
            "status": self.status,
            "refusal_code": self.refusal_code,
            "manifest": None if self.manifest is None else {
                **self.manifest.unhashed(), "manifest_hash": self.manifest.manifest_hash},
            "receipt": {
                "schema_version": self.receipt.schema_version,
                "context_manifest_hash": self.receipt.context_manifest_hash,
                "selected_refs": list(self.receipt.selected_refs),
                "excluded_refs": list(self.receipt.excluded_refs),
                "mandatory_fact_coverage": list(self.receipt.mandatory_fact_coverage),
                "refusal_code": self.receipt.refusal_code,
            },
            "measurement": {
                "baseline_context_bytes": self.measurement.baseline_context_bytes,
                "baseline_context_files": self.measurement.baseline_context_files,
                "selected_context_bytes": self.measurement.selected_context_bytes,
                "selected_context_files": self.measurement.selected_context_files,
                "manifest_build_ms": self.measurement.manifest_build_ms,
                "cache_state": self.measurement.cache_state,
                "cache_identity": self.measurement.cache_identity,
                "built_at": self.measurement.built_at,
                "head_sha": self.measurement.head_sha,
                "repository_remote_url": self.measurement.repository_remote_url,
                "broker_manifest_digest": self.measurement.broker_manifest_digest,
                "policy_digest": self.measurement.policy_digest,
                "context_token_count": self.measurement.context_token_count,
            },
        }


def package_from_row(row: Any) -> ContextPackage:
    """Rebuild a package from its durable row, so a restart binds the same one."""

    return ContextPackage.from_response(row)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

def verify(request: ContextRequest, package: ContextPackage, *,
           declared_manifest_hash: str | None = None,
           budget: ContextBudget = ContextBudget(),
           now: float | None = None) -> str | None:
    """Return the refusal code for this package, or ``None`` to proceed.

    The order is not cosmetic.  Integrity comes first because every later check
    reads a field the digest protects, and a tampered manifest that passes an
    identity check has only proved the tamper was competent.
    """

    if package.status == "unavailable":
        return package.refusal_code or "CONTEXT_BROKER_UNAVAILABLE"
    if package.status == "refused":
        return package.refusal_code or "CONTEXT_SELECTION_REFUSED"
    manifest = package.manifest
    if manifest is None:
        return "CONTEXT_MANIFEST_MISSING"
    if manifest.schema_version != CONTEXT_SCHEMA_VERSION:
        return "CONTEXT_SCHEMA_UNSUPPORTED"
    if not manifest.intact:
        return "INVALID_CONTEXT_MANIFEST"
    if manifest.mission_input_hash != request.mission_input_hash:
        return "CONTEXT_MISSION_MISMATCH"
    if manifest.corpus_identity != request.corpus_identity:
        return "CONTEXT_REPOSITORY_MISMATCH"
    if manifest.policy_identity != request.policy_identity:
        return "CONTEXT_POLICY_MISMATCH"
    if declared_manifest_hash and manifest.manifest_hash != declared_manifest_hash:
        return "CONTEXT_HASH_MISMATCH"
    if package.receipt.context_manifest_hash not in ("", manifest.manifest_hash):
        return "CONTEXT_RECEIPT_MISMATCH"
    remote = package.measurement.repository_remote_url
    if request.repository_remote_url and remote and remote != request.repository_remote_url:
        return "CONTEXT_REPOSITORY_MISMATCH"
    head = package.measurement.head_sha
    if request.baseline_sha and head and head != request.baseline_sha:
        return "CONTEXT_HEAD_MISMATCH"

    selected = set(manifest.selected_refs)
    missing = [anchor for anchor in request.required_anchors if anchor not in selected]
    if missing:
        return "CONTEXT_ANCHOR_MISSING"
    if request.denied_paths and any(_under(ref, request.denied_paths) for ref in selected):
        return "CONTEXT_DENIED_PATH_SELECTED"
    if request.allowed_paths and not all(_under(ref, request.allowed_paths) for ref in selected):
        return "CONTEXT_PATH_NOT_ADMITTED"

    if request.max_age_seconds is not None:
        built = package.measurement.built_at
        if built is None:
            # An unstated build time cannot be shown to be fresh, and a freshness
            # requirement that passes on silence is not a requirement.
            return "CONTEXT_FRESHNESS_UNPROVEN"
        if now is not None and now - built > request.max_age_seconds:
            return "CONTEXT_MANIFEST_STALE"

    return budget_refusal(budget, package.measurement)


def budget_refusal(budget: ContextBudget, measurement: ContextMeasurement) -> str | None:
    """Fail closed on a *measured* overrun.  An unmeasured build never passes.

    A declared byte or file ceiling with no measurement behind it is refused
    rather than waved through: a ceiling nobody measured against is decoration.
    """

    if budget.max_bytes is not None:
        if measurement.selected_context_bytes is None:
            return "CONTEXT_BUDGET_UNMEASURED"
        if measurement.selected_context_bytes > budget.max_bytes:
            return "CONTEXT_BUDGET_EXCEEDED"
    if budget.max_files is not None:
        if measurement.selected_context_files is None:
            return "CONTEXT_BUDGET_UNMEASURED"
        if measurement.selected_context_files > budget.max_files:
            return "CONTEXT_FILE_BUDGET_EXCEEDED"
    return None


def reported_token_refusal(budget: ContextBudget, reported_input_tokens: Any) -> str | None:
    """The token ceiling, applied only to a count a provider actually reported.

    ``reported_input_tokens`` is ``store.telemetry``'s own shape: a dict with a
    total when something was reported, and an absence word when nothing was.
    """

    if budget.max_reported_input_tokens is None:
        return None
    if not isinstance(reported_input_tokens, dict):
        return None
    total = reported_input_tokens.get("total")
    if isinstance(total, int) and not isinstance(total, bool) \
            and total > budget.max_reported_input_tokens:
        return "CONTEXT_TOKEN_BUDGET_EXCEEDED"
    return None


def _under(ref: str, roots: Sequence[str]) -> bool:
    """Path containment on the ref as written.  The broker owns real paths.

    This is a string relation over the refs a manifest declares, not a file
    system question, and it is intentionally the weaker of the two: symlink and
    traversal escapes are refused where the files actually are.
    """

    return any(ref == root or ref.startswith(root.rstrip("/") + "/") for root in roots)


# --------------------------------------------------------------------------- #
# operator explainability
# --------------------------------------------------------------------------- #

def explain(request: ContextRequest | None, package: ContextPackage | None,
            budget: ContextBudget = ContextBudget(),
            refusal: str | None = None) -> dict[str, Any]:
    """Which context was used, why, how big it was, and what was refused."""

    if request is None:
        return {"context_state": "not_applicable",
                "detail": "this mission declared no context request"}
    if package is None:
        return {"context_state": "not_run", "request": request.as_wire(),
                "refusal_code": refusal}
    manifest = package.manifest
    return {
        "context_state": "bound" if refusal is None and manifest else "refused",
        "refusal_code": refusal or package.refusal_code,
        "request": request.as_wire(),
        "context_manifest_hash": None if manifest is None else manifest.manifest_hash,
        "corpus_identity": None if manifest is None else manifest.corpus_identity,
        "policy_identity": None if manifest is None else manifest.policy_identity,
        "selected_refs": [] if manifest is None else list(manifest.selected_refs),
        "excluded_refs": list(package.receipt.excluded_refs),
        "unresolved_questions": [] if manifest is None else list(manifest.unresolved_questions),
        "required_anchors_covered": manifest is not None and all(
            anchor in set(manifest.selected_refs) for anchor in request.required_anchors),
        "measurement": package.as_row()["measurement"],
        "reduction": package.measurement.reduction,
        "budget": {"max_bytes": budget.max_bytes, "max_files": budget.max_files,
                   "max_reported_input_tokens": budget.max_reported_input_tokens},
    }


# --------------------------------------------------------------------------- #
# readers -- every one fails closed on the wrong type rather than coercing
# --------------------------------------------------------------------------- #

def _manifest_from(raw: Any) -> ContextManifest | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ContextManifest(
            schema_version=str(raw["schema_version"]),
            mission_input_hash=str(raw["mission_input_hash"]),
            manifest_hash=str(raw["manifest_hash"]),
            corpus_identity=str(raw["corpus_identity"]),
            policy_identity=str(raw["policy_identity"]),
            selected_refs=tuple(str(item) for item in raw["selected_refs"]),
            unresolved_questions=tuple(str(item) for item in raw["unresolved_questions"]),
        )
    except (KeyError, TypeError):
        return None


def _receipt_from(raw: Any) -> RetrievalReceipt:
    if not isinstance(raw, dict):
        return RetrievalReceipt()
    code = raw.get("refusal_code")
    return RetrievalReceipt(
        schema_version=str(raw.get("schema_version") or CONTEXT_SCHEMA_VERSION),
        context_manifest_hash=str(raw.get("context_manifest_hash") or ""),
        selected_refs=_str_tuple(raw.get("selected_refs")),
        excluded_refs=_str_tuple(raw.get("excluded_refs")),
        mandatory_fact_coverage=_str_tuple(raw.get("mandatory_fact_coverage")),
        refusal_code=code if isinstance(code, str) and code else None,
    )


def _measurement_from(raw: Any) -> ContextMeasurement:
    if not isinstance(raw, dict):
        return ContextMeasurement()
    state = raw.get("cache_state")
    identity = raw.get("cache_identity")
    remote = raw.get("repository_remote_url")
    head = raw.get("head_sha")
    broker_digest = raw.get("broker_manifest_digest")
    policy = raw.get("policy_digest")
    return ContextMeasurement(
        baseline_context_bytes=_count(raw.get("baseline_context_bytes")),
        baseline_context_files=_count(raw.get("baseline_context_files")),
        selected_context_bytes=_count(raw.get("selected_context_bytes")),
        selected_context_files=_count(raw.get("selected_context_files")),
        manifest_build_ms=_count(raw.get("manifest_build_ms")),
        cache_state=state if state in CACHE_STATES else "unknown",
        cache_identity=identity if isinstance(identity, str) and identity else None,
        built_at=float(raw["built_at"]) if isinstance(raw.get("built_at"), (int, float))
        and not isinstance(raw.get("built_at"), bool) else None,
        head_sha=head if isinstance(head, str) and head else None,
        repository_remote_url=remote if isinstance(remote, str) and remote else None,
        broker_manifest_digest=broker_digest if isinstance(broker_digest, str)
        and broker_digest else None,
        policy_digest=policy if isinstance(policy, str) and policy else None,
        context_token_count=canonical_absence(raw.get("context_token_count")),
    )


#: Reproduced from factory-evidence-core ``src/contracts/replay.py`` via
#: ``routing.CANONICAL_ABSENCE``.  Kept here too because this module must not
#: import routing: context is upstream of provider selection, not part of it.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run", "not_measurable"})

#: Words other layers have used for absence that are not in the vocabulary.
#: They are translated at this seam rather than propagated, because a fifth
#: spelling of "we do not know" is the identity divergence the corpus records.
ABSENCE_ALIASES = {"unavailable": "not_measurable", "n/a": "not_applicable",
                   "none": "unknown", "null": "unknown", "": "unknown"}


def canonical_absence(value: Any) -> Any:
    """Keep a real count; translate a known alias; refuse to invent anything."""

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        if value in CANONICAL_ABSENCE:
            return value
        return ABSENCE_ALIASES.get(value.strip().lower(), "unknown")
    return "unknown"


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _required_str(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ContextError("%s is required and must be a non-empty string" % name)
    return value


def _optional_str(raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContextError("%s must be a non-empty string" % name)
    return value


def _string_tuple(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name) or ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item for item in value):
        raise ContextError("%s must be a list of non-empty strings" % name)
    return tuple(value)


def _optional_int(raw: dict[str, Any], name: str) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContextError("%s must be an integer" % name)
    return value


def _optional_number(raw: dict[str, Any], name: str) -> float | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContextError("%s must be a number" % name)
    return float(value)
