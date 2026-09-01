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


def mission_for(contract: ProductContract, intake: pcp.PCPIntake) -> ProductMission:
    """Turn one accepted package plus one contract into one mission.

    The mission reference is the package's own ``<package_id>:build`` work item
    -- minted by ``pcp.intake`` and not renamed here, because a second name for
    the same work item is a second work item.
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
    return ProductMission(
        mission_ref=intake.mission["work_item_id"],
        project_id=contract.project_id,
        work_class=contract.work_class,
        environment_class=contract.environment_class,
        objective=intake.mission["objective"],
        baseline_sha=contract.baseline_sha,
        acceptance_gate_ids=contract.acceptance_gate_ids,
        acceptance_gate_source=contract.acceptance_gate_source,
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
    text = ("Deliver the product described in MISSION.md, from Product "
            "Candidate Package %s. The mission is complete when ./%s exits 0 "
            "with every recorded outcome criterion met." % (
                intake.mission["source_pcp"], gate))
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


__all__ = [
    "BRIEF_LIMIT", "CONTRACT_SCHEMA", "OWNER_ACT_SCHEMA", "ProductContract",
    "ProductMission", "ProductRefusal", "brief", "mission_for", "owner_act",
    "package_from", "unresolved",
]
