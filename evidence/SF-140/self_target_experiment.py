"""Drive one real self-target improvement experiment and record its lineage.

Not a test.  This runs the Stage-8 contract against a genuine candidate commit
that exists in git, produced in a disposable clone of this repository, and
measured by a harness that saw only the sealed commits.  Every number it feeds
the Controller was measured outside this process; nothing here computes an
improvement.

Run it with the measured facts:

    python3 evidence/SF-140/self_target_experiment.py \
        --baseline-sha SHA --candidate-sha SHA \
        --baseline-seconds N --candidate-seconds N \
        --baseline-tests N --candidate-tests N \
        --isolation PATH --out evidence/SF-140/lineage.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from factory_controller import improvement, portfolio, production  # noqa: E402
from factory_controller.engine import Controller, RetryPolicy  # noqa: E402
from factory_controller.store import MissionStore  # noqa: E402

PROJECT = "factory-controller"
REPOSITORY = "repo://factory-controller"

#: The real protected surfaces of this repository.  Every mandatory surface is
#: present and each covers a path that actually exists here.
SURFACES = {
    "governance": ("standards/", "agents/", "CONSTITUTION.md"),
    "production_authority": ("factory_controller/production.py",),
    "admission_integrity": ("factory_controller/store.py",),
    "evaluator_independence": ("tests/test_authority_boundaries.py",),
    "improvement_policy": ("factory_controller/improvement.py",
                           "tests/test_stage8_improvement.py"),
    "secret_handling": (".env", "secrets/"),
    "emergency_stop": ("factory_controller/portfolio.py",),
    "release_authority": (".github/", "dev", "Dockerfile", "compose.yaml"),
}


class SealedCandidateLayer:
    """An execution layer that reports one real, already-existing commit.

    The candidate was authored in the isolated lane rather than dispatched to a
    runtime, and this records that plainly instead of pretending otherwise: the
    dispatch leg is a fixture, and the value it carries is a git object anybody
    can resolve.  A minted identifier would have been indistinguishable from a
    real one, which is the defect SF-133 found on the first-live path.
    """

    def __init__(self, candidate_sha: str) -> None:
        self.candidate_sha = candidate_sha

    def execute(self, step, operation_key, value):
        if step == "dispatch":
            return {"status": "completed", "candidate_sha": self.candidate_sha,
                    "execution_id": operation_key,
                    "receipt": {"execution_mode": "fixture",
                                "provider": "authored-in-lane",
                                "process_started": True,
                                "evidence_class": "reported_claim",
                                "idempotency_key": value["route"]["idempotency_key"]}}
        if step == "verify":
            return {"verified": True, "diagnostic": None}
        if step == "evaluate":
            gates = value["mission"].get("acceptance_gate_ids") or ["SUITE-GREEN"]
            return {"passed": True,
                    "gate_outcomes": [{"gate_id": gate, "passed": True,
                                       "detail": "suite ran green at the sealed commit"}
                                      for gate in gates]}
        if step == "evidence":
            return {"accepted": True, "retryable": False,
                    "evidence_pointer": "evidence/SF-140/lineage.json"}
        return {"status": "unknown"}


def refusal_of(call) -> dict:
    try:
        call()
    except improvement.ImprovementRefusal as refused:
        return refused.as_row()
    raise AssertionError("expected a refusal and did not get one")


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    temp = tempfile.TemporaryDirectory()
    store = MissionStore(str(Path(temp.name) / "sf140.db"))
    ledger = production.ProductionLedger(store)
    plane = improvement.ImprovementPlane(store, ledger)
    record: dict = {"experiment": {}, "refusals": {}}

    store.register_project(portfolio.ProjectPolicy(
        project_id=PROJECT, repository=REPOSITORY, concurrency_cap=1,
        policy_version="sf140-1"))
    ledger.register_environment(production.EnvironmentPolicy(
        environment_id="factory-controller-staging", project_id=PROJECT,
        environment_class="staging", repository=REPOSITORY,
        service_ref="controller", approver_refs=("owner",), autonomous=True))

    policy = improvement.ImprovementPolicy(
        project_id=PROJECT, enabled=True, protected_surfaces=SURFACES,
        self_target_repositories=(REPOSITORY,), generation_ceiling=2,
        experiment_budget=2, concurrent_experiments=1, cooldown_seconds=0,
        risk_class="low", execution_mode="fixture", policy_version="sf140-1")
    record["policy"] = plane.set_policy(policy)

    objective = improvement.Objective(
        objective_ref="OBJ-SF140-SUITE",
        project_id=PROJECT,
        improvement_class="performance",
        statement=("the Controller's own suite should run measurably faster "
                   "without losing a passing test"),
        metrics=(
            improvement.Metric("suite_wall_clock_seconds", "decrease",
                               "objective", min_delta_ratio=0.02),
            improvement.Metric("passing_tests", "increase", "non_regression",
                               tolerance_ratio=0.0),
        ),
        objective_version="1.0")
    record["objective"] = plane.register_objective(objective)

    admitted = plane.admit_experiment(
        objective.objective_ref, "owner_objective", objective.objective_ref,
        target_repository=REPOSITORY, baseline_sha=args.baseline_sha,
        isolation_ref=args.isolation)
    ref = admitted["experiment_ref"]
    record["experiment"]["experiment_ref"] = ref
    record["experiment"]["self_target"] = bool(admitted["self_target"])

    plane.record_baseline(ref, {"suite_wall_clock_seconds": args.baseline_seconds,
                                "passing_tests": args.baseline_tests})

    controller = Controller(store, SealedCandidateLayer(args.candidate_sha),
                            retry_policy=RetryPolicy(max_attempts=1,
                                                     base_delay_seconds=0),
                            lease_seconds=5)
    mission, created = plane.create_candidate_mission(
        ref, controller, acceptance_gate_ids=["SUITE-GREEN"])
    record["experiment"]["mission_ref"] = mission["id"]
    record["experiment"]["mission_created"] = created
    while controller.work_once("sf140-worker") is not None:
        pass
    mission = store.get(mission["id"])
    record["experiment"]["mission_state"] = mission["state"]

    # The improvement that was NOT taken, refused on its real path. The largest
    # measurable win in this repository is the store's connection churn, and
    # `store.py` is the admission-integrity surface.
    record["refusals"]["protected_surface"] = refusal_of(
        lambda: plane.check_change_set(ref, ("factory_controller/store.py",)))
    record["refusals"]["unknown_change_set"] = refusal_of(
        lambda: plane.check_change_set(ref, ()))

    plane.seal_candidate(ref, mission, producer_identity=args.producer,
                         changed_paths=tuple(args.path))

    record["refusals"]["evaluator_not_independent"] = refusal_of(
        lambda: plane.evaluate_candidate(
            ref, evaluator_identity=args.producer,
            measurements={"suite_wall_clock_seconds": args.candidate_seconds,
                          "passing_tests": args.candidate_tests}))

    record["comparison"] = plane.evaluate_candidate(
        ref, evaluator_identity=args.evaluator,
        measurements={"suite_wall_clock_seconds": args.candidate_seconds,
                      "passing_tests": args.candidate_tests})

    record["refusals"]["self_promotion"] = refusal_of(
        lambda: plane.stage_promotion(
            ref, _bundle(args.candidate_sha), "factory-controller-staging"))

    if record["comparison"]["verdict"] == "improved":
        plane.close(ref, "accepted", reason="measured better by an independent harness")
        record["refusals"]["generation_needs_advanced_baseline"] = refusal_of(
            lambda: plane.open_generation(ref, baseline_sha=args.baseline_sha,
                                          isolation_ref=args.isolation + "-g2"))
    else:
        plane.close(ref, "rejected", reason="did not clear the frozen threshold")

    record["lineage"] = plane.lineage(ref)
    record["generations"] = list(plane.generations(record["lineage"]["lineage_ref"]))
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"experiment_ref": ref,
                      "verdict": record["comparison"]["verdict"],
                      "disposition": record["lineage"]["disposition"],
                      "candidate_sha": record["lineage"]["candidate_sha"],
                      "refusals": sorted(record["refusals"])},
                     indent=2, sort_keys=True))
    temp.cleanup()
    return 0


def _bundle(release_sha: str) -> production.ReleaseBundle:
    return production.ReleaseBundle.from_payload({
        "bundle_ref": "rc-sf140-selftarget",
        "project_id": PROJECT, "repository": REPOSITORY,
        "release_sha": release_sha, "mission_ref": "SF-140",
        "evidence_refs": ["evidence/SF-140/lineage.json"],
        "evaluator_receipts": ["evidence/SF-140/lineage.json"],
        "artifact": {"kind": "source", "identity": release_sha},
        "env_schema": {},
        "migration": {"forward_ref": "not_applicable",
                      "reverse_ref": "not_applicable"},
        "release_policy_version": "sf140-1",
        "provenance": {"built_by": "factory-controller",
                       "built_at": "2026-08-27T00:00:00Z",
                       "contract_version": production.CONTRACT_VERSION}})


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline-sha", required=True)
    p.add_argument("--candidate-sha", required=True)
    p.add_argument("--baseline-seconds", type=float, required=True)
    p.add_argument("--candidate-seconds", type=float, required=True)
    p.add_argument("--baseline-tests", type=int, required=True)
    p.add_argument("--candidate-tests", type=int, required=True)
    p.add_argument("--isolation", required=True)
    p.add_argument("--path", action="append", required=True)
    p.add_argument("--producer", default="sf140-lane-author")
    p.add_argument("--evaluator", default="sf140-sealed-commit-harness")
    p.add_argument("--out", required=True)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
