"""The frozen portfolio's improvement slot, joined to the Stage-8 plane.

Stage 8 has been complete and unreachable since it was written.  Its own
docstring says why by design -- "there is no improvement process", nothing here
polls or wakes up, and an experiment exists because somebody called
``admit_experiment``.  In the first-dogfood run nobody did: ``factory cycle``
carries every slot, DF-4 included, through the ordinary mission pipeline, so
the run produced DF-4's acceptance-gate evidence and none of the rest of its
``evidence_required`` -- no baseline measurement, no post-change measurement,
no promotion decision.  ``experiments`` was 0 and ``deployments`` was 0.

This module is that missing caller, and only that.  It adds no capability to
Stage 8: every bound, refusal and ordering rule stays in ``improvement.py`` and
``production.py``, and the functions below fail by letting those refusals out.

Three things are deliberately *not* here.

There is **no metric this module invents**.  Every reading is declared in a
frozen Owner objective contract and read out of an acceptance gate the mission
already declares and runs, by one of two readers.  A gate that produced no
readable value is ``not_measurable``, which Stage 8 never reads as improvement.
So the Controller cannot measure the lab by any command the project did not
declare in its own gate source.

There is **no way for the candidate to be its own evaluator**.  The producer
identity is the provider profile the route actually selected, recorded when the
mission ran; the evaluator identity is this seam.  ``seal_candidate`` and
``evaluate_candidate`` compare the two, and a mission that recorded no provider
seals as ``unknown`` rather than as this module's own name.

There is **no promotion this module decides**.  ``stage_promotion`` calls the
Stage-6 ledger, which applies the same admission a person's release gets and
refuses a gated class outright.  What is recorded beside it is the Owner's own
shift grant, because that grant is what authorized this portfolio, and DF-4's
``evidence_required`` asks for the promotion decision *with its approval
reference* rather than for a deployment.  Nothing here deploys: this lab
declares no deployment target, and the only ``DeploymentPort`` in the corpus
reports that no environment was contacted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import improvement, production
from .context import sha256_hex


CONTRACT_SCHEMA = "factory.controller.internal_dogfood_improvement_objective.v1"

#: The two ways a declared gate's own output becomes a number.  Adding a third
#: means adding a function here, which is the point of the mapping being closed:
#: a reader that could be named in the contract but not implemented would let a
#: frozen objective claim a measurement nothing produces.
READERS = ("unittest_ran", "json_field")

#: The work class the frozen portfolio gives its improvement slot.  A mission
#: of any other class never reaches this module.
WORK_CLASS = "improvement"


class ObjectiveError(ValueError):
    """An improvement objective contract the Controller will not load."""


@dataclass(frozen=True)
class ImprovementContract:
    """One Owner objective, its readings, and where a candidate may be staged."""

    objective: improvement.Objective
    trigger_class: str
    readings: tuple[Mapping[str, Any], ...]
    protected_surfaces: Mapping[str, tuple[str, ...]]
    promotion: Mapping[str, Any]
    contract_digest: str

    @property
    def project_id(self) -> str:
        return self.objective.project_id

    @property
    def gate_ids(self) -> tuple[str, ...]:
        """The gates a measurement reads, in declaration order, deduplicated."""

        seen: list[str] = []
        for reading in self.readings:
            gate = reading["gate_id"]
            if gate not in seen:
                seen.append(gate)
        return tuple(seen)

    @property
    def environment(self) -> str:
        return str(self.promotion["environment_id"])

    def as_row(self) -> dict[str, Any]:
        return {"objective": self.objective.as_row(),
                "trigger_class": self.trigger_class,
                "readings": [dict(reading) for reading in self.readings],
                "environment_id": self.environment,
                "contract_digest": self.contract_digest}


# --------------------------------------------------------------------------- #
# the frozen objective
# --------------------------------------------------------------------------- #

def load(path: Any) -> ImprovementContract:
    try:
        with open(str(path), encoding="utf-8") as handle:
            body = json.load(handle)
    except (OSError, ValueError) as error:
        raise ObjectiveError("the improvement objective is unreadable: %s"
                             % error) from None
    return contract_from_payload(body)


def contract_from_payload(body: Any) -> ImprovementContract:
    """Refuse anything the Stage-8 plane could not then hold to account."""

    if not isinstance(body, Mapping):
        raise ObjectiveError("an improvement objective is an object")
    if body.get("schema_version") != CONTRACT_SCHEMA:
        raise ObjectiveError("unsupported improvement objective schema %r"
                             % (body.get("schema_version"),))
    metrics = body.get("metrics")
    if not isinstance(metrics, Sequence) or isinstance(metrics, str) or not metrics:
        raise ObjectiveError("an objective states at least one metric")
    try:
        objective = improvement.Objective(
            objective_ref=str(body["objective_ref"]),
            project_id=str(body["project_id"]),
            improvement_class=str(body["improvement_class"]),
            statement=str(body["statement"]),
            objective_version=str(body.get("objective_version", "unset")),
            metrics=tuple(
                improvement.Metric(
                    metric_id=str(metric["metric_id"]),
                    direction=str(metric["direction"]),
                    role=str(metric.get("role", "objective")),
                    min_delta_ratio=float(metric.get("min_delta_ratio", 0.0)),
                    tolerance_ratio=float(metric.get("tolerance_ratio", 0.0)),
                )
                for metric in metrics),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ObjectiveError("the improvement objective is invalid: %s" % error) from None

    readings = _readings(body, objective)
    promotion = body.get("promotion")
    if not isinstance(promotion, Mapping) or not promotion.get("environment_id"):
        raise ObjectiveError(
            "an improvement objective names the environment its candidate may "
            "be staged to; there is no default")
    surfaces = _surfaces(body)
    return ImprovementContract(
        objective=objective,
        trigger_class=str(body.get("trigger_class", "owner_objective")),
        readings=readings,
        protected_surfaces=surfaces,
        promotion=dict(promotion),
        contract_digest=sha256_hex(improvement.canonical_json(dict(body))),
    )


def _readings(body: Mapping[str, Any],
              objective: improvement.Objective) -> tuple[Mapping[str, Any], ...]:
    """One reading per metric, no metric without one, no reading without a metric.

    Both directions are refused because both are ways a comparison stops
    meaning anything: a metric with no reading is a number nothing produces,
    and a reading with no metric is a measurement nothing judges.
    """

    measurement = body.get("measurement")
    declared = measurement.get("readings") if isinstance(measurement, Mapping) else None
    if not isinstance(declared, Sequence) or isinstance(declared, str):
        raise ObjectiveError("an objective declares how each metric is read")
    readings = []
    for item in declared:
        if not isinstance(item, Mapping):
            raise ObjectiveError("each reading is an object")
        reader = item.get("reader")
        if reader not in READERS:
            raise ObjectiveError(
                "reader %r is not one of %s" % (reader, ", ".join(READERS)))
        if not item.get("gate_id") or not item.get("metric_id"):
            raise ObjectiveError("each reading names a metric and a gate")
        if reader == "json_field" and not item.get("field"):
            raise ObjectiveError("a json_field reading names its field")
        readings.append(dict(item))
    stated = {reading["metric_id"] for reading in readings}
    declared_metrics = {metric.metric_id for metric in objective.metrics}
    if stated != declared_metrics:
        raise ObjectiveError(
            "every metric needs exactly one reading; unread: %s, unjudged: %s"
            % (sorted(declared_metrics - stated) or "none",
               sorted(stated - declared_metrics) or "none"))
    if len(stated) != len(readings):
        raise ObjectiveError("a metric is read twice")
    return tuple(readings)


def _surfaces(body: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    declared = body.get("protected_surfaces", {})
    if not isinstance(declared, Mapping):
        raise ObjectiveError("protected_surfaces maps a surface name to prefixes")
    surfaces: dict[str, tuple[str, ...]] = {}
    for name, prefixes in declared.items():
        if isinstance(prefixes, str) or not isinstance(prefixes, Sequence) or not prefixes:
            raise ObjectiveError(
                "protected surface %r covers nothing" % (name,))
        surfaces[str(name)] = tuple(str(prefix) for prefix in prefixes)
    return surfaces


def merged_surfaces(base: Mapping[str, Sequence[str]],
                    extra: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Add the project's own protected paths without dropping a mandatory one.

    ``ImprovementPolicy`` already refuses a policy that omits a mandatory
    surface; this only ever widens, so a contract cannot narrow the protection
    the Controller declares for every dogfood project.
    """

    merged = {name: tuple(prefixes) for name, prefixes in base.items()}
    for name, prefixes in extra.items():
        merged[name] = tuple(dict.fromkeys((*merged.get(name, ()), *prefixes)))
    return merged


# --------------------------------------------------------------------------- #
# reading a gate's own output
# --------------------------------------------------------------------------- #

def measurements(contract: ImprovementContract,
                 gate_outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every declared metric, read from the gates, absences spelled out.

    A metric whose gate did not run, produced no output, or produced output
    this reader cannot turn into a number is ``not_measurable``.  That is one
    of the four canonical absence words and Stage 8 never reads it as
    improvement -- an unknown starting point refuses a baseline, and an unknown
    candidate reading refuses a verdict of improved.
    """

    by_gate = {outcome.get("gate_id"): outcome for outcome in gate_outcomes
               if isinstance(outcome, Mapping)}
    values: dict[str, Any] = {}
    for reading in contract.readings:
        outcome = by_gate.get(reading["gate_id"])
        if outcome is None:
            values[reading["metric_id"]] = "not_measurable"
            continue
        if reading["reader"] == "unittest_ran":
            values[reading["metric_id"]] = _unittest_ran(outcome)
        else:
            values[reading["metric_id"]] = _json_field(outcome, reading["field"])
    return values


def _streams(outcome: Mapping[str, Any]) -> str:
    parts = [outcome.get(name) for name in ("stdout_tail", "stderr_tail")]
    return "\n".join(part for part in parts if isinstance(part, str))


def _unittest_ran(outcome: Mapping[str, Any]) -> Any:
    """How many tests ran, only if the run ended OK.

    Tests that ran are not tests that passed, so a run that ended FAILED or
    ERROR reports the absence rather than its own count.  ``unittest`` writes
    both lines to standard error, which is why both streams are read.
    """

    text = _streams(outcome)
    marker = "\nRan "
    index = text.rfind(marker)
    if index < 0:
        return "not_measurable"
    tail = text[index + len(marker):]
    if "\nOK" not in tail:
        return "not_measurable"
    word = tail.split(" ", 1)[0]
    try:
        return int(word)
    except ValueError:
        return "not_measurable"


def _json_field(outcome: Mapping[str, Any], field: str) -> Any:
    """One field of the JSON document a gate printed.

    The stream is bounded, so the document may be missing its opening brace.
    A reading that cannot be parsed is the absence and never a default: this
    reader is how a *regression* is seen, and a regression that silently reads
    as zero would be recorded as an improvement on a decreasing metric.
    """

    text = outcome.get("stdout_tail")
    if not isinstance(text, str):
        return "not_measurable"
    left, right = text.find("{"), text.rfind("}")
    if left < 0 or right <= left:
        return "not_measurable"
    try:
        body = json.loads(text[left:right + 1])
    except ValueError:
        return "not_measurable"
    if not isinstance(body, Mapping) or field not in body:
        return "not_measurable"
    value = body[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "not_measurable"
    return value


# --------------------------------------------------------------------------- #
# the lifecycle, in the order Stage 8 requires it
# --------------------------------------------------------------------------- #

def ensure_environment(ledger: production.ProductionLedger,
                       contract: ImprovementContract, *, repository: str,
                       policy_version: str) -> production.EnvironmentPolicy:
    """Register the one environment the objective names, idempotently."""

    promotion = contract.promotion
    policy = production.EnvironmentPolicy(
        environment_id=contract.environment,
        project_id=contract.project_id,
        environment_class=str(promotion.get("environment_class", "staging")),
        repository=repository,
        service_ref=str(promotion.get("service_ref", contract.project_id)),
        approver_refs=tuple(str(ref) for ref in promotion.get("approver_refs", ())),
        autonomous=bool(promotion.get("autonomous", True)),
        policy_version=policy_version,
    )
    try:
        ledger.register_environment(policy)
    except production.ProductionRefusal:
        # Already registered under this identity.  Re-registering is the
        # Owner's act and not this seam's, so an existing environment is used
        # as it stands rather than rewritten under it.
        return ledger.environment(contract.environment)
    return policy


def attempt_objective(contract: ImprovementContract,
                      attempt: int) -> improvement.Objective:
    """The objective one slot attempt is admitted under.

    ``experiment_reference`` is derived from the objective, the generation and
    the baseline, so a second attempt against the same three collides with the
    row already there -- which is exactly right for a replay and exactly wrong
    for a retry, where the first attempt's mission is settled and a new one has
    to exist.  The attempt reaches the identity the same way it already reaches
    a mission's: as a ``#N`` suffix, and nowhere else.  A generation is not
    used for this, because a generation means the parent was accepted and its
    candidate became the next baseline, and a retry means neither.
    """

    if attempt <= 1:
        return contract.objective
    return improvement.Objective(
        objective_ref="%s#%d" % (contract.objective.objective_ref, attempt),
        project_id=contract.objective.project_id,
        improvement_class=contract.objective.improvement_class,
        statement=contract.objective.statement,
        metrics=contract.objective.metrics,
        objective_version=contract.objective.objective_version,
    )


def abandon_spent(plane: improvement.ImprovementPlane,
                  contract: ImprovementContract, store) -> str | None:
    """Close an open experiment whose mission settled without completing.

    A refused attempt leaves an experiment holding a settled mission it can
    never seal.  Left open it consumes the project's single concurrency slot
    and the seam's next attempt has nowhere to go, so the retry would report
    the dead mission as running forever.  Nothing is deleted: the generation
    closes ``abandoned`` with its reason, which is the disposition Stage 8 has
    for a lineage that stopped without a verdict.
    """

    row = open_for(plane, contract)
    if row is None:
        return None
    if not row["mission_ref"]:
        if row["baseline_json"] is None:
            # Admitted, never baselined: a crash between the two leaves an
            # experiment nothing can advance and the slot cannot get past.
            _close(plane, row["experiment_ref"], "abandoned",
                   "no baseline was recorded")
            return row["experiment_ref"]
        return None
    mission = store.get(row["mission_ref"])
    if mission is None or mission["state"] not in improvement.MISSION_SETTLED \
            or mission["state"] == "completed":
        return None
    _close(plane, row["experiment_ref"], "abandoned",
           "mission %s settled %s" % (row["mission_ref"], mission["state"]))
    return row["experiment_ref"]


def open_experiment(plane: improvement.ImprovementPlane,
                    contract: ImprovementContract, *, repository: str,
                    baseline_sha: str, isolation_ref: str,
                    baseline: Mapping[str, Any],
                    attempt: int = 1) -> dict[str, Any]:
    """Register the objective, admit generation 1, and pin the baseline.

    The order is Stage 8's and is the anti-gaming property: ``record_baseline``
    refuses once a mission exists, and ``create_candidate_mission`` refuses
    without a baseline.  So a caller cannot produce a candidate first and pick
    the number it is compared against afterwards, and this function returns
    before any mission is submitted.
    """

    objective = attempt_objective(contract, attempt)
    plane.register_objective(objective)
    row = plane.admit_experiment(
        objective.objective_ref, contract.trigger_class,
        objective.objective_ref,
        target_repository=repository, baseline_sha=baseline_sha,
        isolation_ref=isolation_ref)
    if row.get("baseline_json") is None:
        try:
            plane.record_baseline(row["experiment_ref"], baseline)
        except (improvement.ImprovementRefusal, improvement.PolicyError):
            # An experiment admitted but never baselined can never be sealed,
            # and it holds the project's single concurrency slot, so the next
            # attempt would refuse IMPROVEMENT_CONCURRENCY_EXCEEDED forever.
            # A generation that cannot state where it started is abandoned
            # here, with the refusal still raised to the caller.
            _close(plane, row["experiment_ref"], "abandoned",
                   "the baseline could not be measured")
            raise
        row = plane._experiment(row["experiment_ref"])
    return dict(row)


def open_for(plane: improvement.ImprovementPlane,
             contract: ImprovementContract) -> dict[str, Any] | None:
    """The project's open experiment, if it has one."""

    for row in plane.experiments(contract.project_id):
        if row["disposition"] is None:
            return dict(row)
    return None


def settle(plane: improvement.ImprovementPlane,
           ledger: production.ProductionLedger,
           contract: ImprovementContract, *, experiment_ref: str,
           mission: Mapping[str, Any], producer_identity: str,
           evaluator_identity: str, changed_paths: Sequence[str],
           candidate: Mapping[str, Any], approval_ref: str,
           release_policy_version: str, provenance_at: str) -> dict[str, Any]:
    """Seal, compare, stage and close, stopping at the first refusal.

    Every stop is Stage 8's or Stage 6's own, reported rather than worked
    around.  A candidate that touched a protected surface, that has no change
    set, that did not pass its own gates, that could not be measured, or that
    was not measured better than its pinned baseline, all end here without a
    promotion -- which is the behaviour DF-4's ``stop_conditions`` describe and
    the reason its failure mode was called silent.
    """

    # A change set the candidate seam could not derive arrives as the absence
    # word, not as a list.  It is carried through as an empty set on purpose so
    # `check_change_set` refuses IMPROVEMENT_CHANGE_SET_UNKNOWN: not knowing
    # what changed is a different fact from knowing nothing did, and this is
    # the one place that distinction could be lost.
    paths = (sorted(str(item) for item in changed_paths)
             if isinstance(changed_paths, (list, tuple)) else [])
    outcome: dict[str, Any] = {"experiment_ref": experiment_ref,
                               "candidate": dict(candidate),
                               "changed_paths": paths}
    try:
        plane.seal_candidate(experiment_ref, mission,
                             producer_identity=producer_identity,
                             changed_paths=outcome["changed_paths"])
    except (improvement.ImprovementRefusal, improvement.PolicyError) as refusal:
        return _stopped(plane, outcome, "seal", refusal)
    try:
        comparison = plane.evaluate_candidate(
            experiment_ref, evaluator_identity=evaluator_identity,
            measurements=candidate,
            objective_digest=contract.objective.objective_digest)
    except (improvement.ImprovementRefusal, improvement.PolicyError) as refusal:
        return _stopped(plane, outcome, "evaluate", refusal)
    outcome["comparison"] = comparison
    outcome["verdict"] = comparison["verdict"]
    if comparison["verdict"] != "improved":
        # Not a failure of the run: a candidate measured no better than its
        # baseline is exactly what this plane exists to refuse to promote.
        outcome["disposition"] = _close(plane, experiment_ref, "rejected",
                                        "comparative verdict %s"
                                        % comparison["verdict"])
        return outcome
    row = plane.lineage(experiment_ref)
    bundle = release_bundle(
        contract, row, mission,
        release_policy_version=release_policy_version,
        provenance_at=provenance_at)
    try:
        deployment_id = plane.stage_promotion(experiment_ref, bundle,
                                              contract.environment)
    except (improvement.ImprovementRefusal, improvement.PolicyError,
            production.ProductionRefusal, production.PolicyError) as refusal:
        return _stopped(plane, outcome, "promote", refusal)
    outcome["deployment_id"] = deployment_id
    outcome["bundle_digest"] = bundle.bundle_digest
    outcome["approval_ref"] = approval_ref
    outcome["deployment"] = ledger.deployment(deployment_id)
    outcome["disposition"] = _close(plane, experiment_ref, "accepted",
                                    "promotion staged as %s" % deployment_id)
    return outcome


def release_bundle(contract: ImprovementContract, row: Mapping[str, Any],
                   mission: Mapping[str, Any], *, release_policy_version: str,
                   provenance_at: str) -> production.ReleaseBundle:
    """The candidate as a release bundle, with nothing invented in it.

    ``artifact`` is ``not_applicable`` rather than the commit id: this lab
    builds nothing, and borrowing the commit would be a made-up artifact
    identity of exactly the kind ``_artifact`` refuses a moving tag for.
    """

    return production.ReleaseBundle.from_payload({
        "bundle_ref": "rc-%s" % row["experiment_ref"],
        "project_id": row["project_id"],
        "repository": row["target_repository"],
        "release_sha": row["candidate_sha"],
        "mission_ref": mission["id"],
        "evidence_refs": ["mission://%s" % mission["id"],
                          "experiment://%s" % row["experiment_ref"]],
        "evaluator_receipts": ["gate://%s/%s" % (mission["id"], gate)
                               for gate in contract.gate_ids],
        "artifact": "not_applicable",
        "env_schema": {},
        "migration": {"forward_ref": "not_applicable",
                      "reverse_ref": "not_applicable"},
        "release_policy_version": release_policy_version,
        "provenance": {"built_by": "factory-controller/dogfood-improvement",
                       "built_at": provenance_at,
                       "contract_version": production.CONTRACT_VERSION},
    })


def _close(plane: improvement.ImprovementPlane, experiment_ref: str,
           disposition: str, reason: str) -> str:
    try:
        plane.close(experiment_ref, disposition, reason=reason)
    except (improvement.ImprovementRefusal, improvement.PolicyError):
        return "unknown"
    return disposition


def _stopped(plane: improvement.ImprovementPlane, outcome: dict[str, Any],
             stage: str, refusal: Exception) -> dict[str, Any]:
    """Record the stop and abandon the generation, keeping the reason."""

    code = getattr(refusal, "code", type(refusal).__name__)
    outcome["stopped_at"] = stage
    outcome["refusal_code"] = code
    outcome["refusal_detail"] = str(refusal)
    outcome["disposition"] = _close(plane, outcome["experiment_ref"], "abandoned",
                                    "%s refused: %s" % (stage, code))
    return outcome


__all__ = ["CONTRACT_SCHEMA", "READERS", "WORK_CLASS", "ImprovementContract",
           "ObjectiveError", "abandon_spent", "attempt_objective",
           "contract_from_payload", "ensure_environment", "load",
           "measurements", "merged_surfaces", "open_experiment", "open_for",
           "release_bundle", "settle"]
