"""The Owner's product intake path: one Product Candidate Package, one mission.

``dogfood.py`` and ``dogfood_intake.py`` carry the frozen internal portfolio,
where the Owner names nothing and the next mission is a serial rule.  A real
product is the opposite act: the Owner names one package and that package is
the whole of the intent.  This module is the difference between those two, and
nothing else -- the mission it produces is materialized by exactly the same
``dogfood_intake.build`` the internal path uses, so a product mission and an
internal one have one identity scheme, one manifest rule and one admission
document between them.

Three things are deliberately absent.

**There is no second intake format.**  ``pcp.py`` already validates the package
and mints ``<package_id>:build``; this module consumes that verdict and refuses
to restate any of it.  A package the Laboratory did not resolve is refused
there, not softened here.

**There is no invented mission text.**  Every field of the mission below comes
from either the package or the product run contract.  The provider's own
instruction is the target repository's ``MISSION.md``; the bounded brief this
module derives is a pointer to the package identity, not a second brief.

**The Owner act is a record, not a side effect.**  ``owner_act`` returns a
self-hashed document naming who submitted which package bytes under which
approval.  Recording it is the caller's job, which is what keeps a submission
from becoming something a scheduler could perform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import pcp
from .context import sha256_hex


CONTRACT_SCHEMA = "factory.controller.product_run_contract.v1"
OWNER_ACT_SCHEMA = "factory.controller.product_owner_intake.v1"

#: Where a repository keeps the statement the provider is instructed to treat
#: as authoritative.  Every provider profile's argv begins "Read MISSION.md
#: first"; this is that file, and it is the only channel a revision's own
#: requirements can travel on -- the wire brief is bounded at 256 characters,
#: which is a pointer, not a set of requirements.
DEFAULT_MISSION_STATEMENT_PATH = "MISSION.md"

#: The namespace the execution layer will accept a revision base under.  A
#: branch on purpose: a lane clones the registered checkout and a plain clone
#: carries branch refs only.
REVISION_REF_PREFIX = "refs/heads/factory/revision/"

#: The bound ``factory-evidence-core`` enforces on a mission brief, reproduced
#: so an over-long brief is refused where the Owner can read why rather than at
#: the far end of the dispatch path.
BRIEF_LIMIT = 256


class ProductRefusal(ValueError):
    """One plain-English reason a product submission cannot be materialized."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _refuse(code: str, detail: str) -> None:
    raise ProductRefusal(code, detail)


@dataclass(frozen=True)
class ProductContract:
    """What the Factory was configured to accept for one product.

    The package says what to build.  This says which repository the mission
    runs against, at which frozen baseline, behind which gates, and through
    which provider profile -- the four facts a package deliberately does not
    carry, because they are the Factory's configuration and not the product's
    intent.
    """

    run_ref: str
    package_id: str
    project_id: str
    provider_profiles: tuple[str, ...]
    work_class: str
    environment_class: str
    baseline_sha: str
    acceptance_gate_ids: tuple[str, ...]
    acceptance_gate_source: str
    publish_prefix: str
    capability_request: str
    review_environment_id: str
    production_environment_id: str
    mutates_repository: bool = True
    acceptance_gate_expectations: Mapping[str, Any] = field(default_factory=dict)
    mission_statement_path: str = DEFAULT_MISSION_STATEMENT_PATH

    @property
    def projects(self) -> tuple[str, ...]:
        return (self.project_id,)

    @classmethod
    def from_payload(cls, value: Any) -> "ProductContract":
        if not isinstance(value, Mapping):
            _refuse("PRODUCT_CONTRACT_INVALID", "the product run contract is not an object")
        if value.get("schema_version") != CONTRACT_SCHEMA:
            _refuse("PRODUCT_CONTRACT_INVALID",
                    "the product run contract schema_version must be %s" % CONTRACT_SCHEMA)
        text_fields = (
            "run_ref", "package_id", "project_id", "work_class",
            "environment_class", "baseline_sha", "acceptance_gate_source",
            "publish_prefix", "capability_request", "review_environment_id",
            "production_environment_id",
        )
        for name in text_fields:
            item = value.get(name)
            if not isinstance(item, str) or not item.strip() or len(item) > 1024:
                _refuse("PRODUCT_CONTRACT_INVALID",
                        "the product run contract field %r is missing or malformed" % name)
        for name in ("provider_profiles", "acceptance_gate_ids"):
            item = value.get(name)
            if (not isinstance(item, list) or not item
                    or not all(isinstance(entry, str) and entry.strip() for entry in item)):
                _refuse("PRODUCT_CONTRACT_INVALID",
                        "the product run contract field %r must be a non-empty list" % name)
        mutates = value.get("mutates_repository", True)
        if not isinstance(mutates, bool):
            _refuse("PRODUCT_CONTRACT_INVALID", "mutates_repository must be a boolean")
        expectations = value.get("acceptance_gate_expectations", {})
        if not isinstance(expectations, Mapping):
            _refuse("PRODUCT_CONTRACT_INVALID",
                    "acceptance_gate_expectations must be an object")
        statement = value.get("mission_statement_path",
                              DEFAULT_MISSION_STATEMENT_PATH)
        if not isinstance(statement, str) or not statement.strip() \
                or ".." in statement.split("/") or statement.startswith("/"):
            _refuse("PRODUCT_CONTRACT_INVALID",
                    "mission_statement_path is malformed")
        return cls(
            run_ref=value["run_ref"], package_id=value["package_id"],
            project_id=value["project_id"],
            provider_profiles=tuple(value["provider_profiles"]),
            work_class=value["work_class"],
            environment_class=value["environment_class"],
            baseline_sha=value["baseline_sha"],
            acceptance_gate_ids=tuple(value["acceptance_gate_ids"]),
            acceptance_gate_source=value["acceptance_gate_source"],
            publish_prefix=value["publish_prefix"],
            capability_request=value["capability_request"],
            review_environment_id=value["review_environment_id"],
            production_environment_id=value["production_environment_id"],
            mutates_repository=mutates,
            acceptance_gate_expectations={
                str(gate): dict(body) for gate, body in expectations.items()
                if isinstance(body, Mapping)},
            mission_statement_path=statement,
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProductContract":
        try:
            body = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _refuse("PRODUCT_CONTRACT_UNAVAILABLE",
                    "the product run contract could not be read: %s" % exc)
        return cls.from_payload(body)


@dataclass(frozen=True)
class ProductMission:
    """One product mission, in the shape ``dogfood_intake.build`` reads.

    Frozen and derived: every field is either a package field or a contract
    field, so two runs of the same package against the same contract produce
    the same mission identity without this module holding a clock or a counter.
    """

    mission_ref: str
    project_id: str
    work_class: str
    environment_class: str
    objective: str
    baseline_sha: str
    acceptance_gate_ids: tuple[str, ...]
    acceptance_gate_source: str
    mutates_repository: bool
    acceptance_gate_expectations: Mapping[str, Any] = field(default_factory=dict)


def revision_of(intake: pcp.PCPIntake) -> Mapping[str, Any] | None:
    """The release this package supersedes, when it supersedes one."""

    body = intake.mission.get("revision")
    return body if isinstance(body, Mapping) else None


def revision_ref(intake: pcp.PCPIntake) -> str:
    """The branch the revision base is opened on, derived not minted.

    One ref per package version, so a repeated Owner command lands on the ref
    it already opened rather than forking a second lineage for one revision.
    """

    body = revision_of(intake)
    if body is None:
        _refuse("PRODUCT_NOT_A_REVISION", "this package supersedes nothing")
    return "%sv%d" % (REVISION_REF_PREFIX, intake.package_version)


def revision_addendum(intake: pcp.PCPIntake) -> str:
    """The Owner's requested changes, as the provider will read them.

    This is the one place a product requirement becomes provider-visible text,
    and every requirement in it is copied out of the package the Owner
    submitted.  Nothing here decides what the product should do; the numbering
    and the sentences around the list are the package's own identities and the
    contract's own gate.

    It has to be this file rather than the wire brief because the brief is
    bounded at 256 characters by ``factory-evidence-core`` -- enough for a
    pointer, not for a decision.  Every provider profile is already instructed
    to read the repository's mission statement first and treat it as
    authoritative, so a revision's requirements travel on the channel that
    already exists instead of on a second one.
    """

    body = revision_of(intake)
    if body is None:
        _refuse("PRODUCT_NOT_A_REVISION", "this package supersedes nothing")
    changes = "\n".join(
        "%d. %s" % (number, item)
        for number, item in enumerate(body["requested_changes"], start=1))
    return (
        "## Revision requested by the Owner -- %s\n\n"
        "Authoritative source of intent for this revision: the Product\n"
        "Candidate Package `%s`. Everything stated above still holds except\n"
        "where a requirement below changes it.\n\n"
        "The Owner reviewed Release Candidate `%s` at candidate `%s` and\n"
        "returned it for changes (Owner Validation `%s`). This repository is\n"
        "at that rejected candidate: revise it, do not rebuild it.\n\n"
        "Deliver, in addition to everything above:\n\n%s\n\n"
        "The mission is complete when the declared acceptance gates still\n"
        "exit 0 and every requirement above is met.\n"
        % (intake.mission["source_pcp"], intake.mission["source_pcp"],
           body["predecessor_rc"], body["predecessor_candidate_sha"],
           body["owner_validation_id"], changes))


def mission_for(contract: ProductContract, intake: pcp.PCPIntake, *,
                baseline_sha: str | None = None,
                acceptance_gate_source: str | None = None) -> ProductMission:
    """Turn one accepted package plus one contract into one mission.

    The mission reference is the package's own work item -- minted by
    ``pcp.intake`` and not renamed here, because a second name for the same
    work item is a second work item.

    ``baseline_sha`` is the one contract field a revision overrides, and it is
    a required argument for one: the contract pins the *product's* frozen
    baseline, and a revision that started there would rebuild from a commit
    the Owner has already seen superseded, silently discarding the candidate
    they reviewed.  The caller supplies the revision base because proving that
    base descends from the rejected candidate is a Git fact, and Git facts are
    not this module's to assert.
    """

    if intake.verdict not in ("ACCEPTED", "ACCEPTED_DEGRADED"):
        _refuse("PRODUCT_PACKAGE_NOT_ACCEPTED",
                "the Product Candidate Package was not accepted for build: %s"
                % intake.verdict)
    if intake.mission.get("capability") != "development":
        _refuse("PRODUCT_CAPABILITY_UNEXPECTED",
                "this package mints capability %r, which the product path does "
                "not carry" % intake.mission.get("capability"))
    if intake.package_id != contract.package_id:
        _refuse("PRODUCT_PACKAGE_MISMATCH",
                "this Factory is configured for package %r, not %r"
                % (contract.package_id, intake.package_id))
    if revision_of(intake) is not None and baseline_sha is None:
        _refuse("PRODUCT_REVISION_BASE_REQUIRED",
                "a revision is built from its own base commit, never from the "
                "product's frozen baseline")
    return ProductMission(
        mission_ref=intake.mission["work_item_id"],
        project_id=contract.project_id,
        work_class=contract.work_class,
        environment_class=contract.environment_class,
        objective=intake.mission["objective"],
        baseline_sha=baseline_sha or contract.baseline_sha,
        acceptance_gate_ids=contract.acceptance_gate_ids,
        acceptance_gate_source=(acceptance_gate_source
                                or contract.acceptance_gate_source),
        mutates_repository=contract.mutates_repository,
        acceptance_gate_expectations=contract.acceptance_gate_expectations,
    )


def brief(contract: ProductContract, intake: pcp.PCPIntake) -> str:
    """The one bounded sentence the provider sees beside ``MISSION.md``.

    It names the package the Owner submitted and the gate that decides the
    mission.  It deliberately restates no requirement: the repository's own
    ``MISSION.md`` is the provider's instruction, and a brief that repeated it
    would be a second, drifting copy of the boundary.
    """

    gate = contract.acceptance_gate_ids[-1].replace("-", " ", 1)
    statement = contract.mission_statement_path
    if revision_of(intake) is None:
        text = ("Deliver the product described in %s, from Product "
                "Candidate Package %s. The mission is complete when ./%s exits "
                "0 with every recorded outcome criterion met." % (
                    statement, intake.mission["source_pcp"], gate))
    else:
        # A pointer, not a second copy of the requirements: they are in the
        # mission statement, which is what the provider is told to read first.
        text = ("Revise this checkout per the Owner revision section of %s, "
                "from Product Candidate Package %s. The mission is complete "
                "when ./%s exits 0 and every requested change is delivered."
                % (statement, intake.mission["source_pcp"], gate))
    if len(text) > BRIEF_LIMIT:
        _refuse("PRODUCT_BRIEF_TOO_LONG",
                "the derived mission brief exceeds %d characters" % BRIEF_LIMIT)
    return text


def owner_act(contract: ProductContract, intake: pcp.PCPIntake, *,
              owner: str, approval_ref: str, at: str) -> dict[str, Any]:
    """The durable record that a person, not a scheduler, submitted a product.

    Self-hashed over its own body so the record cannot be edited without the
    edit being visible, in the same shape the live admission document's own
    assertions use.
    """

    for name, value in (("owner", owner), ("approval_ref", approval_ref), ("at", at)):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            _refuse("PRODUCT_OWNER_ACT_INVALID",
                    "the Owner submission field %r is required" % name)
    body = {
        "schema_version": OWNER_ACT_SCHEMA,
        "evidence_class": "human_authority",
        "run_ref": contract.run_ref,
        "package_id": intake.package_id,
        "package_version": intake.package_version,
        "package_digest": intake.package_digest,
        "source_pcp": intake.mission["source_pcp"],
        "work_item_id": intake.mission["work_item_id"],
        "project_id": contract.project_id,
        "baseline_sha": contract.baseline_sha,
        **({} if revision_of(intake) is None
           else {"revision": dict(revision_of(intake))}),
        "question": "Does the Owner submit %s into the Factory for build?"
                    % intake.mission["source_pcp"],
        "options": ["submit", "refuse"],
        "chosen_action": "submit",
        "owner": owner,
        "approval_ref": approval_ref,
        "submitted_at": at,
    }
    return {**body, "act_hash": sha256_hex(body)}


def package_from(path: str | Path) -> tuple[dict[str, Any], pcp.PCPIntake]:
    """Read and accept one package file, or refuse with the package's own code."""

    try:
        package = pcp.load(path)
        return package, pcp.intake(package)
    except pcp.PCPRefusal as refusal:
        _refuse(refusal.code, refusal.detail)


def unresolved(intake: pcp.PCPIntake) -> Sequence[str]:
    """Decisions the package left open, which the Owner is submitting anyway."""

    return tuple(intake.mission.get("open_decisions") or ())


# --------------------------------------------------------------------------- #
# what a completed product mission becomes
# --------------------------------------------------------------------------- #


def rc_id_for(contract: ProductContract, candidate_sha: str) -> str:
    """One Release Candidate identity per candidate commit, derived not minted.

    ``release.seal`` is idempotent on exactly this id and refuses
    ``RC_IDENTITY_MISMATCH`` when the same id is offered different bytes.  So
    deriving the id from the bytes is what makes a repeated review the same
    review: a minted id would seal a second candidate for one commit, and a
    constant id would collide the moment a second candidate existed.
    """

    if not isinstance(candidate_sha, str) or len(candidate_sha) < 12:
        _refuse("PRODUCT_CANDIDATE_INVALID",
                "a candidate is a full commit id")
    return "rc-%s-%s" % (contract.package_id, candidate_sha[:12])


def gate_source_path(contract: ProductContract) -> str:
    """The repository path the acceptance gates are read from.

    ``acceptance_gate_source`` is ``<remote>@<baseline>:<path>``; a remote URL
    carries its own colons, so the path is the last segment and never the
    second.
    """

    return contract.acceptance_gate_source.rsplit(":", 1)[-1]


def decision_boundary(contract: ProductContract, changed_paths: Any) -> dict[str, Any]:
    """Did the candidate rewrite the thing that judges it?

    This is the one independent-QA question a product mission can be asked
    from durable state alone, and it is not redundant with the gates: the
    gates run from the *candidate's* own checkout, so a provider that relaxed
    its gate source would pass gates that no longer mean what the contract
    declared -- and every other record in the run would still be green.  The
    package's own prohibitions say the same thing in the product's words.

    Read from the stage-1 evaluator's recorded ``changed_paths``.  Nothing is
    re-derived from git here: candidate truth belongs to Evidence Core's
    verifier, and this is a question about a list the Factory already holds.
    An absent list is ``unknown`` rather than an empty pass -- the check
    cannot be performed, which is not the same fact as the boundary holding.
    """

    source = gate_source_path(contract)
    if not isinstance(changed_paths, (list, tuple)):
        return {"held": False, "outcome": "unknown", "gate_source": source,
                "changed_paths": "unknown", "violations": []}
    paths = [str(item) for item in changed_paths]
    violations = [item for item in paths if item == source]
    return {"held": not violations,
            "outcome": "held" if not violations else "violated",
            "gate_source": source, "changed_paths": paths,
            "violations": violations}


def release_bundle(contract: ProductContract, *, work_item_id: str,
                   mission_id: str, repository: str, candidate_sha: str,
                   artifact: Any, evidence_pointer: str,
                   provenance_at: str) -> dict[str, Any]:
    """The completed mission as a release bundle payload, nothing invented.

    Every field is a durable row or the artifact identity the execution layer
    minted from the candidate commit.  ``artifact`` is a real digest here,
    unlike the internal improvement path's ``not_applicable``: a product
    publishes bytes, and which bytes is the whole question a review answers.

    ``env_schema`` and ``migration`` are empty and ``not_applicable`` because
    a browser-only static bundle has neither, which the package states as a
    prohibition rather than an omission.
    """

    return {
        "bundle_ref": "rc-%s" % work_item_id,
        "project_id": contract.project_id,
        "repository": repository,
        "release_sha": candidate_sha,
        "mission_ref": work_item_id,
        "evidence_refs": ["mission://%s" % mission_id,
                          "evidence://%s" % evidence_pointer,
                          "package://%s" % contract.package_id],
        "evaluator_receipts": ["gate://%s/%s" % (mission_id, gate)
                               for gate in contract.acceptance_gate_ids],
        "artifact": artifact,
        "env_schema": {},
        "migration": {"forward_ref": "not_applicable",
                      "reverse_ref": "not_applicable"},
        "release_policy_version": contract.run_ref,
        "provenance": {"built_by": "factory-controller/product",
                       "built_at": provenance_at,
                       "contract_version": "factory-controller/production/1.0"},
    }


__all__ = [
    "BRIEF_LIMIT", "CONTRACT_SCHEMA", "DEFAULT_MISSION_STATEMENT_PATH",
    "OWNER_ACT_SCHEMA", "REVISION_REF_PREFIX", "ProductContract",
    "ProductMission", "ProductRefusal", "brief", "decision_boundary",
    "gate_source_path", "mission_for", "owner_act", "package_from",
    "rc_id_for", "release_bundle", "revision_addendum", "revision_of",
    "revision_ref", "unresolved",
]
