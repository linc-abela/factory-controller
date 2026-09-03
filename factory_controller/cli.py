"""Operator CLI for submission, unattended workers, status, and history."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from . import activation
from . import capacity
from . import advisor as advisory
from . import dogfood
from . import factory as factory_lifecycle
from . import continuity
from . import improvement as imp
from . import maintenance as mnt
from . import portfolio
from . import production
from . import rehearsal
from . import release
from . import shift as shift_plane
from . import shift_runtime
from . import supervisor as sup
from .adapter import JsonProcessAdapter
from .engine import Controller, RetryPolicy
from .store import MissionStore


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory-controller")
    p.add_argument("--db", default="factory-controller.db")
    p.add_argument("--adapter", default=f"{shlex.quote(sys.executable)} -m factory_controller.safe_provider")
    sub = p.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--key", required=True)
    submit.add_argument("--file", type=Path)
    work = sub.add_parser("work-once")
    work.add_argument("--worker", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--worker", required=True)
    worker.add_argument("--poll-seconds", type=float, default=1)
    worker.add_argument("--max-idle-polls", type=int, default=0)
    status = sub.add_parser("status")
    status.add_argument("mission_id", nargs="?")
    history = sub.add_parser("history")
    history.add_argument("mission_id")
    route = sub.add_parser("route")
    route.add_argument("mission_id")
    telemetry = sub.add_parser("telemetry")
    telemetry.add_argument("mission_id")
    ctx = sub.add_parser("context")
    ctx.add_argument("mission_id")
    economics = sub.add_parser("economics")
    economics.add_argument("--corpus", default=None)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("mission_id")
    harness = sub.add_parser("harness")
    harness.add_argument("--missions", type=int, default=10)
    baton = sub.add_parser("baton")
    baton.add_argument("action", choices=("inspect",))
    baton.add_argument("--baton-id")

    cap = sub.add_parser("capacity")
    cap.add_argument("action", choices=("policy", "policies", "observe",
                                        "readings", "observations", "brief",
                                        "checkpoint"))
    cap.add_argument("--runtime", dest="cap_runtime")
    cap.add_argument("--state", dest="cap_state", choices=capacity.CAPACITY_STATES)
    cap.add_argument("--source", dest="cap_source")
    cap.add_argument("--source-ref", dest="cap_source_ref")
    cap.add_argument("--observed-at", dest="cap_observed_at", type=float)
    cap.add_argument("--window-started-at", dest="cap_window_started_at", type=float)
    cap.add_argument("--expected-reset-at", dest="cap_reset_at", type=float)
    cap.add_argument("--remaining", dest="cap_remaining", type=float)
    cap.add_argument("--unit", dest="cap_unit")
    cap.add_argument("--precision", dest="cap_precision",
                     choices=capacity.PRECISIONS, default="unknown")
    cap.add_argument("--unmanaged", dest="cap_unmanaged", action="store_true")
    cap.add_argument("--handoff", dest="cap_handoff",
                     choices=capacity.HANDOFF_MODES, default="allowed")
    cap.add_argument("--max-age", dest="cap_max_age", type=float,
                     default=capacity.DEFAULT_OBSERVATION_MAX_AGE_SECONDS)
    cap.add_argument("--backoff", dest="cap_backoff", type=float,
                     default=capacity.DEFAULT_UNKNOWN_RESET_BACKOFF_SECONDS)
    cap.add_argument("--policy-version", dest="cap_policy_version", default="unset")
    cap.add_argument("--mission", dest="cap_mission")
    cap.add_argument("--from-bridge", dest="cap_from_bridge", type=Path,
                     help="a `factory-bridge capacity status` reading, as JSON; "
                          "the layer that can see a harness measures, this one "
                          "decides what runs")

    project = sub.add_parser("project")
    project.add_argument("action", choices=("register", "state", "list"))
    project.add_argument("--id")
    project.add_argument("--repository")
    project.add_argument("--state", choices=portfolio.PROJECT_STATES)
    project.add_argument("--priority", type=int, default=portfolio.DEFAULT_PRIORITY)
    project.add_argument("--cap", type=int, default=portfolio.DEFAULT_PROJECT_CONCURRENCY)
    project.add_argument("--budget", type=float)
    project.add_argument("--currency")
    project.add_argument("--context-ceiling", type=int)
    project.add_argument("--gate", action="append", default=[], dest="gates",
                         help="an acceptance gate this project's repository "
                              "declares; repeatable")
    project.add_argument("--gate-source",
                         help="where the gate list was read from, e.g. "
                              "<repository>@<sha>:<path>")
    project.add_argument("--policy-version", default="unset")

    pf = sub.add_parser("portfolio")
    pf.add_argument("--concurrency", type=int)
    pf.add_argument("--aging", type=float)
    pf.add_argument("--policy-version")
    pf.add_argument("--emergency-stop", action="store_true")
    pf.add_argument("--resume", action="store_true")

    depend = sub.add_parser("depend")
    depend.add_argument("mission_id")
    depend.add_argument("--on", required=True, dest="depends_on")
    depend.add_argument("--on-failure", choices=portfolio.ON_FAILURE, default="block")
    deps = sub.add_parser("deps")
    deps.add_argument("mission_id")
    sub.add_parser("schedule")
    coordination = sub.add_parser("coordination")
    coordination.add_argument("mission_id", nargs="?")
    coordination.add_argument("--limit", type=int, default=200)
    pfe = sub.add_parser("portfolio-economics")
    pfe.add_argument("--project", default=None)
    advise = sub.add_parser("advise")
    advise.add_argument("--proposals", type=Path,
                        help="a JSON advisor response to replay deterministically")
    advise.add_argument("--endpoint", help="an advisory HTTP endpoint to consult instead")
    advise.add_argument("--policy", type=Path, help="the Owner's advisor policy, as JSON")
    advise.add_argument("--probe", action="store_true",
                        help="report endpoint presence without consulting it")

    prod = sub.add_parser("production")
    prod.add_argument("action", choices=(
        "env-register", "env-list", "env-state", "admit", "approve", "deploy",
        "health", "rollback", "receipt", "stop", "reconcile", "incident",
        "contain", "route-defect", "correlate"))
    prod.add_argument("--environment")
    prod.add_argument("--project")
    prod.add_argument("--class", dest="environment_class",
                      choices=production.ENVIRONMENT_CLASSES)
    prod.add_argument("--repository")
    prod.add_argument("--service")
    prod.add_argument("--approver", action="append", default=[],
                      help="who may approve a release here; repeatable")
    prod.add_argument("--secret-ref", action="append", default=[],
                      dest="secret_refs",
                      help="a logical name in this project's namespace, never a value")
    prod.add_argument("--autonomous", action="store_true",
                      help="deploy without approval; refused for a production class")
    prod.add_argument("--state", choices=production.ENVIRONMENT_STATES)
    prod.add_argument("--concurrency", type=int,
                      default=production.DEFAULT_ENVIRONMENT_CONCURRENCY)
    prod.add_argument("--max-rollbacks", type=int,
                      default=production.DEFAULT_MAX_ROLLBACK_ATTEMPTS)
    prod.add_argument("--bundle", type=Path, help="a release bundle, as JSON")
    prod.add_argument("--deployment")
    prod.add_argument("--actor")
    prod.add_argument("--ref")
    prod.add_argument("--digest")
    prod.add_argument("--attempt", type=int, default=1)
    prod.add_argument("--passed", type=int, default=0)
    prod.add_argument("--failed", type=int, default=0)
    prod.add_argument("--scope", choices=production.STOP_SCOPES, default="environment")
    prod.add_argument("--resume-stop", action="store_true")
    prod.add_argument("--incident")
    prod.add_argument("--incident-class", choices=production.INCIDENT_CLASSES,
                      default="outage")
    prod.add_argument("--release-sha")
    prod.add_argument("--behaviour")
    prod.add_argument("--blast-radius")
    prod.add_argument("--action-name", choices=production.CONTAINMENT_ACTIONS)
    prod.add_argument("--work-item")
    prod.add_argument("--summary")
    prod.add_argument("--policy-version", dest="prod_policy_version", default="unset")

    rel = sub.add_parser(
        "release",
        help="the exact-artifact path: seal, review, Owner Validation, "
             "promotion, rollback")
    rel.add_argument("action", choices=(
        "seal", "deploy-review", "validate", "promote", "rollback", "show",
        "events"))
    rel.add_argument("--rc", dest="rc_id")
    rel.add_argument("--bundle", type=Path, dest="rel_bundle",
                     help="a Controller release bundle, as JSON; '-' reads stdin")
    rel.add_argument("--verification-ref", action="append", default=[],
                     dest="verification_refs",
                     help="durable verification evidence; repeatable")
    rel.add_argument("--qa-ref", action="append", default=[], dest="qa_refs",
                     help="durable independent QA evidence; repeatable")
    rel.add_argument("--environment", dest="rel_environment")
    rel.add_argument("--review-url", dest="review_url")
    rel.add_argument("--actor", dest="rel_actor")
    rel.add_argument("--validation", dest="validation_id")
    rel.add_argument("--deployment-ref", dest="deployment_ref")
    rel.add_argument("--decision", choices=sorted(release.DECISIONS))
    rel.add_argument("--notes", default="")
    rel.add_argument("--passed", type=int, default=0, dest="rel_passed")
    rel.add_argument("--failed", type=int, default=0, dest="rel_failed")
    rel.add_argument("--health-ref", dest="health_ref")
    rel.add_argument("--adapter", dest="rel_adapter", default="deterministic",
                     choices=("deterministic", "google", "firebase", "google-firebase-hosting", "google-simulated"),
                     help="the deployment adapter to use (default: deterministic)")
    rel.add_argument("--simulate", action="store_true",
                     help="explicitly select simulated transport for testing")
    rel.add_argument("--artifact-dir", type=Path, dest="artifact_dir",
                     help="directory containing unsealed artifact files to deploy")
    rel.add_argument("--probe", action="store_true",
                     help="probe the review URL or deployment target with the real health verifier")

    imp_parser = sub.add_parser("improvement")
    imp_parser.add_argument("action", choices=(
        "policy", "enable", "disable", "objective", "objectives", "admit",
        "baseline", "mission", "seal", "evaluate", "generation", "promote",
        "revert", "close", "lineage", "generations", "list"))
    imp_parser.add_argument("--project")
    imp_parser.add_argument("--file", type=Path, dest="imp_file",
                            help="policy, objective or measurement JSON")
    imp_parser.add_argument("--objective", dest="imp_objective")
    imp_parser.add_argument("--experiment")
    imp_parser.add_argument("--lineage")
    imp_parser.add_argument("--parent")
    imp_parser.add_argument("--trigger-class", dest="imp_trigger_class",
                            choices=imp.TRIGGER_CLASSES, default="owner_objective")
    imp_parser.add_argument("--source", dest="imp_source")
    imp_parser.add_argument("--repository", dest="imp_repository")
    imp_parser.add_argument("--baseline-sha", dest="imp_baseline_sha")
    imp_parser.add_argument("--isolation", dest="imp_isolation")
    imp_parser.add_argument("--producer", dest="imp_producer")
    imp_parser.add_argument("--evaluator", dest="imp_evaluator")
    imp_parser.add_argument("--path", action="append", default=[],
                            dest="imp_paths", help="a changed path; repeatable")
    imp_parser.add_argument("--gate", action="append", default=[], dest="imp_gates")
    imp_parser.add_argument("--environment", dest="imp_environment")
    imp_parser.add_argument("--bundle", type=Path, dest="imp_bundle")
    imp_parser.add_argument("--disposition", choices=imp.DISPOSITIONS)
    imp_parser.add_argument("--reason", dest="imp_reason", default="operator")

    mnt_parser = sub.add_parser("maintenance")
    mnt_parser.add_argument("action", choices=(
        "policy", "enable", "disable", "trigger", "repair", "lineage", "list",
        "close"))
    mnt_parser.add_argument("--project")
    mnt_parser.add_argument("--trigger-class", choices=mnt.TRIGGER_CLASSES,
                            default="production_incident")
    mnt_parser.add_argument("--source")
    mnt_parser.add_argument("--trigger")
    mnt_parser.add_argument("--env-class", action="append", default=[],
                            dest="mnt_environment_classes",
                            choices=production.ENVIRONMENT_CLASSES,
                            help="repeatable; a production class is refused")
    mnt_parser.add_argument("--budget", type=int, dest="mnt_budget",
                            default=mnt.DEFAULT_REPAIR_BUDGET)
    mnt_parser.add_argument("--concurrency", type=int, dest="mnt_concurrency",
                            default=mnt.DEFAULT_CONCURRENCY)
    mnt_parser.add_argument("--cooldown", type=float,
                            default=mnt.DEFAULT_COOLDOWN_SECONDS)
    mnt_parser.add_argument("--attempt-ceiling", type=int,
                            default=mnt.DEFAULT_ATTEMPT_CEILING)
    mnt_parser.add_argument("--suppression-threshold", type=int,
                            default=mnt.DEFAULT_SUPPRESSION_THRESHOLD)
    mnt_parser.add_argument("--mode", dest="mnt_mode", default="fixture",
                            choices=("fixture", "real"))
    mnt_parser.add_argument("--gate", action="append", default=[],
                            dest="mnt_gates")
    mnt_parser.add_argument("--disposition", choices=mnt.DISPOSITIONS)
    mnt_parser.add_argument("--reason", dest="mnt_reason", default="operator")
    mnt_parser.add_argument("--policy-version", dest="mnt_policy_version",
                            default="unset")

    dog = sub.add_parser("dogfood")
    dog.add_argument("action", choices=("contract", "preflight", "gate",
                                       "rehearse"))
    dog.add_argument("--contract", dest="dog_contract",
                     default="contracts/internal-dogfood-run-contract.json")
    dog.add_argument("--report", dest="dog_reports", action="append", default=[],
                     metavar="NAME=PATH",
                     help="a JSON report from another repository, e.g. "
                          "bridge_doctor=/tmp/doctor.json; repeatable")
    dog.add_argument("--evidence", dest="dog_evidence",
                     help="observed values for the productization gate")
    dog.add_argument("--root", dest="dog_root",
                     help="a directory for the rehearsal's disposable stores")
    dog.add_argument("--scenario", dest="dog_scenarios", action="append",
                     default=[], help="run only this scenario; repeatable")
    dog.add_argument("--label", dest="dog_label", default=activation.DEFAULT_LABEL)
    dog.add_argument("--agents-dir", dest="dog_agents_dir",
                     default=str(Path.home() / "Library" / "LaunchAgents"))
    dog.add_argument("--state-dir", dest="dog_state_dir",
                     default=str(Path.home() / ".factory-controller"))

    sh = sub.add_parser("shift")
    sh.add_argument("action", choices=("portfolio", "gate", "preview", "apply",
                                       "revoke", "suspend", "resume", "status",
                                       "brief", "admit", "grants", "events"))
    sh.add_argument("--contract", dest="sh_contract",
                    default="contracts/internal-dogfood-run-contract.json")
    sh.add_argument("--portfolio", dest="sh_portfolio",
                    default="contracts/first-dogfood-mission-portfolio.json")
    sh.add_argument("--request", dest="sh_request", default="SF-144-shift-1",
                    help="the Owner's activation request; a second apply of "
                         "the same one is the same act, and a new decision "
                         "needs a new name")
    sh.add_argument("--approval", dest="sh_approval",
                    help="path to the Owner's durable shift approval record")
    sh.add_argument("--missions", type=int, dest="sh_missions", default=4)
    sh.add_argument("--duration-seconds", type=float, dest="sh_duration",
                    default=4 * 3600.0)
    sh.add_argument("--budget", type=float, dest="sh_budget", default=25.0)
    sh.add_argument("--currency", dest="sh_currency", default="USD")
    sh.add_argument("--reason", dest="sh_reason", default="operator")
    sh.add_argument("--actor", dest="sh_actor", default="owner")
    sh.add_argument("--resume-ref", dest="sh_resume_ref",
                    help="where the durable state a resume would read is recorded")
    sh.add_argument("--report", dest="sh_reports", action="append", default=[],
                    metavar="NAME=PATH",
                    help="a JSON report from another repository; repeatable")
    sh.add_argument("--reachable", dest="sh_reachable",
                    metavar="PATH",
                    help="JSON mapping each project to the commits an operator "
                         "confirmed its remote can serve")
    sh.add_argument("--limit", type=int, dest="sh_limit", default=50)
    sh.add_argument("--label", dest="sh_label", default=activation.DEFAULT_LABEL)
    sh.add_argument("--agents-dir", dest="sh_agents_dir",
                    default=str(Path.home() / "Library" / "LaunchAgents"))
    sh.add_argument("--state-dir", dest="sh_state_dir",
                    default=str(Path.home() / ".factory-controller"))

    runtime = sub.add_parser(
        "shift-runtime",
        help="observe crash-safe runtime state; this is not shift governance")
    runtime.add_argument("action", choices=("status", "resume-preview"),
                         help="read runtime state without claiming or advancing work")
    runtime.add_argument("--mission", dest="runtime_mission",
                         help="limit the observation to one mission id")
    runtime.add_argument("--project", dest="runtime_project",
                         help="limit the observation to one project")
    runtime.add_argument("--target-profile", dest="runtime_profile",
                         help="target the named compatible runtime in a resume preview")

    sup_parser = sub.add_parser("supervisor")
    sup_parser.add_argument("action", choices=(
        "status", "policy", "policies", "enable", "disable", "cycle", "start",
        "pause", "resume", "drain", "stop", "emergency-stop", "clear-emergency",
        "hold", "release", "clear-health", "brief", "cycles", "selections",
        "transitions", "service", "service-plan", "service-install",
        "service-doctor", "service-uninstall", "approval"))
    sup_parser.add_argument("--project", dest="sup_project")
    sup_parser.add_argument("--worker", dest="sup_worker", default="supervisor")
    sup_parser.add_argument("--actor", dest="sup_actor", default="owner")
    sup_parser.add_argument("--reason", dest="sup_reason", default="operator")
    sup_parser.add_argument("--evidence", dest="sup_evidence",
                            default="not_applicable")
    sup_parser.add_argument("--class", action="append", default=[],
                            dest="sup_classes", choices=sup.WORK_CLASSES,
                            help="a work class this project allows; repeatable")
    sup_parser.add_argument("--missions-per-cycle", type=int,
                            dest="sup_missions",
                            default=sup.DEFAULT_MISSIONS_PER_CYCLE)
    sup_parser.add_argument("--maintenance-admissions", type=int,
                            dest="sup_maintenance",
                            default=sup.DEFAULT_MAINTENANCE_ADMISSIONS)
    sup_parser.add_argument("--improvement-admissions", type=int,
                            dest="sup_improvement",
                            default=sup.DEFAULT_IMPROVEMENT_ADMISSIONS)
    sup_parser.add_argument("--window-start", type=int, dest="sup_window_start")
    sup_parser.add_argument("--window-end", type=int, dest="sup_window_end")
    sup_parser.add_argument("--failure-threshold", type=int,
                            dest="sup_threshold",
                            default=sup.DEFAULT_FAILURE_THRESHOLD)
    sup_parser.add_argument("--suppression-seconds", type=float,
                            dest="sup_suppression",
                            default=sup.DEFAULT_SUPPRESSION_SECONDS)
    sup_parser.add_argument("--lease-seconds", type=float, dest="sup_lease",
                            default=sup.DEFAULT_CYCLE_LEASE_SECONDS)
    sup_parser.add_argument("--interval-seconds", type=int, dest="sup_interval",
                            default=300)
    sup_parser.add_argument("--cycle", dest="sup_cycle")
    sup_parser.add_argument("--limit", type=int, dest="sup_limit", default=50)
    sup_parser.add_argument("--policy-version", dest="sup_policy_version",
                            default="unset")
    sup_parser.add_argument("--label", dest="sup_label",
                            default=activation.DEFAULT_LABEL)
    sup_parser.add_argument("--agents-dir", dest="sup_agents_dir",
                            default=str(Path.home() / "Library" / "LaunchAgents"))
    sup_parser.add_argument("--state-dir", dest="sup_state_dir",
                            default=str(Path.home() / ".factory-controller"))
    sup_parser.add_argument("--working-dir", dest="sup_working_dir",
                            default=str(Path.cwd()))
    sup_parser.add_argument("--approval", dest="sup_approval",
                            help="path to a durable Owner activation approval")
    sup_parser.add_argument("--apply", dest="sup_apply", action="store_true",
                            help="write the service files; without it nothing "
                                 "is written and the plan is printed")

    factory_parser = sub.add_parser(
        "factory", help="the bounded Owner install/start/run/stop/status surface")
    factory_parser.add_argument(
        "factory_action",
        choices=("install", "start", "run", "product", "revise", "review",
                 "cycle", "stop", "status"))
    factory_parser.add_argument(
        "--package", type=Path,
        help="the Product Candidate Package to submit; required by 'product' "
             "and by 'revise', and used by nothing else")
    factory_parser.add_argument(
        "--watch", action="store_true",
        help="keep observing status until completion, attention, or Ctrl+C")
    factory_parser.add_argument(
        "--interval", type=float,
        default=factory_lifecycle.DEFAULT_WATCH_INTERVAL_SECONDS,
        help="status refresh interval in seconds (default: 30)")
    return p


def _acceptance_gates(store, project_id, repository, declared) -> tuple[list[str], str]:
    """Gates an operator typed, or the ones the project declared.  Never both,
    and never a literal invented here.

    An operator naming gates on the command line is a person choosing, which is
    a different act from a supervisor promoting work nobody typed -- so it is
    allowed and it is labelled ``operator`` in the mission payload rather than
    borrowing the registry's provenance.
    """

    if declared:
        return list(declared), "operator"
    return store.declared_acceptance_gates(project_id, repository)


def _controller(args) -> Controller:
    return Controller(MissionStore(args.db), JsonProcessAdapter(shlex.split(args.adapter)), retry_policy=RetryPolicy())


def _production(args, store) -> int:
    """The Owner's own surface onto Stage 6.

    A refusal prints as a refusal and exits non-zero rather than raising: an
    operator running this during an incident should get the code and the
    reason, not a traceback.

    The deployment port here is the deterministic one, which reaches nothing.
    A real one is a host mechanism and lives outside this repository, so the
    Controller cannot be the place a production mutation accidentally becomes
    available just because someone typed a command.
    """
    ledger = production.ProductionLedger(store)
    port = production.DeterministicDeploymentAdapter()
    try:
        if args.action == "env-register":
            ledger.register_environment(production.EnvironmentPolicy(
                environment_id=args.environment, project_id=args.project,
                environment_class=args.environment_class,
                repository=args.repository, service_ref=args.service,
                approver_refs=tuple(args.approver), state=args.state or "enabled",
                autonomous=args.autonomous, deployment_concurrency=args.concurrency,
                max_rollback_attempts=args.max_rollbacks,
                secret_refs=tuple(args.secret_refs),
                policy_version=args.prod_policy_version))
            result = {"environment_id": args.environment, "registered": True}
        elif args.action == "env-list":
            result = [policy.__dict__ for policy in ledger.environments(args.project)]
        elif args.action == "env-state":
            ledger.set_environment_state(args.environment, args.state)
            result = {"environment_id": args.environment, "state": args.state}
        elif args.action == "admit":
            bundle = production.ReleaseBundle.from_payload(
                json.loads(args.bundle.read_text() if args.bundle else sys.stdin.read()))
            deployment = ledger.admit_release(bundle, args.environment,
                                              args.actor, attempt=args.attempt)
            result = {"deployment_id": deployment,
                      "bundle_digest": bundle.bundle_digest,
                      "state": ledger.deployment(deployment)["state"]}
        elif args.action == "approve":
            ledger.approve(args.deployment, args.actor, args.ref, args.digest)
            result = ledger.receipt(args.deployment)
        elif args.action == "deploy":
            result = {"state": ledger.deploy(args.deployment, port),
                      "adapter": port.name}
        elif args.action == "health":
            record = production.HealthRecord(
                checks_passed=args.passed, checks_failed=args.failed,
                evidence_ref=args.ref or "unknown", observed_at=time.time())
            result = {"state": ledger.record_health(args.deployment, record)}
        elif args.action == "rollback":
            result = {"state": ledger.rollback(args.deployment, port)}
        elif args.action == "receipt":
            result = ledger.receipt(args.deployment)
        elif args.action == "stop":
            result = {"scope": args.scope, "engaged": not args.resume_stop,
                      "environments": ledger.emergency_stop(
                          args.scope, project_id=args.project,
                          environment_id=args.environment,
                          engaged=not args.resume_stop)}
        elif args.action == "reconcile":
            result = {"uncertain": ledger.reconcile_on_restart()}
        elif args.action == "incident":
            ledger.declare_incident(
                incident_ref=args.incident, environment_id=args.environment,
                declared_by=args.actor, incident_class=args.incident_class,
                affected_release_sha=args.release_sha,
                affected_bundle_ref=args.ref, failing_behaviour=args.behaviour,
                blast_radius=args.blast_radius)
            result = {"incident_ref": args.incident, "state": "declared"}
        elif args.action == "contain":
            ledger.contain(args.incident, args.action_name)
            result = {"incident_ref": args.incident, "containment": args.action_name}
        elif args.action == "route-defect":
            result = ledger.route_defect(args.incident, args.work_item, args.summary)
        else:
            result = ledger.correlate(args.release_sha)
    except production.ProductionRefusal as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 1
    except production.PolicyError as error:
        print(json.dumps({"refused": {"code": "POLICY_INVALID",
                                      "detail": str(error)}}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True, default=list))
    return 0


def _release(args, store) -> int:
    """The Owner's surface onto the exact-artifact release path.

    ``release.py`` held this whole lifecycle and nothing reached it, which is a
    different failure from not having it: logic no command can invoke cannot be
    the thing an Owner Validation runs through.  This is that command, and it
    adds no rule -- every refusal below is raised by the lifecycle itself.

    The deployment port is the deterministic one, for the same reason
    ``_production`` uses it: reaching a real target is a host mechanism and
    lives outside this repository, so a production mutation never becomes
    available merely because somebody typed a command here.
    """

    lifecycle = release.ReleaseLifecycle(store)
    ledger = production.ProductionLedger(store)
    adapter_choice = getattr(args, "rel_adapter", "deterministic")
    if adapter_choice in ("google", "firebase", "google-firebase-hosting", "google-simulated"):
        from . import google_production
        if adapter_choice == "google-simulated" or getattr(args, "simulate", False):
            transport = google_production.SimulatedFirebaseTransport()
        else:
            transport = google_production.FirebaseHostingRestTransport()
        artifact_dir = getattr(args, "artifact_dir", None)
        resolver = (lambda d: google_production.file_system_artifact_resolver(d, base_dirs=[artifact_dir])) if artifact_dir else None
        port = google_production.FirebaseHostingDeploymentAdapter({}, transport=transport, artifact_resolver=resolver, store=store)
    else:
        port = production.DeterministicDeploymentAdapter()
    health = None
    if getattr(args, "probe", False) and getattr(args, "review_url", None):
        from . import google_production
        verifier = google_production.StaticWebHealthVerifier()
        health = verifier.verify(args.review_url)
    elif args.rel_passed or args.rel_failed:
        health = production.HealthRecord(
            checks_passed=args.rel_passed, checks_failed=args.rel_failed,
            evidence_ref=args.health_ref or "unknown", observed_at=time.time())
    try:
        if args.action == "seal":
            body = (sys.stdin.read() if str(args.rel_bundle) == "-"
                    else args.rel_bundle.read_text())
            bundle = production.ReleaseBundle.from_payload(json.loads(body))
            result = lifecycle.seal(
                args.rc_id, bundle,
                verification_refs=args.verification_refs,
                qa_refs=args.qa_refs).as_row()
        elif args.action == "deploy-review":
            result = lifecycle.deploy_review(
                args.rc_id, ledger, port,
                review_environment_id=args.rel_environment,
                requested_by=args.rel_actor, review_url=args.review_url,
                health=health)
        elif args.action == "validate":
            result = lifecycle.record_owner_validation(
                args.validation_id, args.rc_id,
                deployment_ref=args.deployment_ref, decision=args.decision,
                decided_by=args.rel_actor, decided_at=time.time(),
                notes=args.notes).as_row()
        elif args.action == "promote":
            result = lifecycle.promote_validated(
                args.rc_id, args.validation_id, ledger, port,
                production_environment_id=args.rel_environment,
                requested_by=args.rel_actor, health=health)
        elif args.action == "rollback":
            result = lifecycle.rollback_production(
                args.rc_id, ledger, port,
                production_environment_id=args.rel_environment)
        elif args.action == "events":
            result = list(lifecycle.events(args.rc_id))
        else:
            result = lifecycle.candidate(args.rc_id).as_row()
    except release.ReleaseRefusal as refusal:
        print(json.dumps({"refused": {"code": refusal.code,
                                      "detail": refusal.detail}}, sort_keys=True))
        return 1
    except production.ProductionRefusal as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 1
    except production.PolicyError as error:
        # A bundle the production contract itself rejects, before the release
        # lifecycle ever sees it -- a mutable artifact tag is caught here.
        print(json.dumps({"refused": {"code": "RELEASE_BUNDLE_INVALID",
                                      "detail": str(error)}}, sort_keys=True))
        return 1
    except (AttributeError, TypeError, ValueError) as error:
        print(json.dumps({"refused": {"code": "RELEASE_ARGUMENTS_INVALID",
                                      "detail": str(error)}}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True, default=list))
    return 0


def _maintenance(args, controller) -> int:
    """The Owner's own surface onto Stage 7.

    Deliberately without a ``run`` verb.  Every command here is one act an
    operator asked for; there is nothing to start and therefore nothing that
    keeps going after the command returns.
    """
    store = controller.store
    plane = mnt.MaintenancePlane(store, production.ProductionLedger(store))
    try:
        if args.action == "policy":
            if not args.mnt_environment_classes and args.project and not args.mnt_gates:
                current = plane.policy(args.project)
                if current is not None and args.mnt_policy_version == "unset":
                    print(json.dumps(current.as_row(), sort_keys=True))
                    return 0
            classes = tuple(args.mnt_environment_classes) or ("local-sim", "staging")
            result = plane.set_policy(mnt.MaintenancePolicy(
                project_id=args.project, enabled=True, environment_classes=classes,
                repair_budget=args.mnt_budget, concurrency=args.mnt_concurrency,
                cooldown_seconds=args.cooldown,
                attempt_ceiling=args.attempt_ceiling,
                suppression_threshold=args.suppression_threshold,
                execution_mode=args.mnt_mode,
                policy_version=args.mnt_policy_version))
        elif args.action in ("enable", "disable"):
            result = plane.set_enabled(args.project, args.action == "enable")
        elif args.action == "trigger":
            result = plane.admit_trigger(args.trigger_class, args.source)
        elif args.action == "repair":
            lineage = plane.lineage(args.trigger)
            gates, source = _acceptance_gates(
                store, lineage["project_id"], lineage["repository"], args.mnt_gates)
            mission, created = plane.create_repair_mission(
                args.trigger, controller, acceptance_gate_ids=gates,
                extra={"acceptance_gate_source": source})
            result = {"created": created, "mission": mission}
        elif args.action == "lineage":
            result = plane.lineage(args.trigger)
        elif args.action == "close":
            result = plane.close(args.trigger, args.disposition,
                                 reason=args.mnt_reason)
        else:
            result = list(plane.repairs(args.project))
    except mnt.MaintenanceRefusal as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 2
    except portfolio.GateProvenanceError as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 2
    except mnt.PolicyError as exc:
        print(json.dumps({"refused": {"code": "MAINTENANCE_POLICY_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _improvement(args, controller) -> int:
    """The Owner's own surface onto Stage 8.

    Deliberately without a ``run`` verb, on the same principle as Stage 7:
    every command here is one act an operator asked for, so there is nothing
    that keeps going after the command returns.  ``policy`` and ``objective``
    read a JSON file because both carry nested declarations -- protected
    surfaces and frozen metrics -- and squeezing either onto a flag would make
    the two most safety-critical declarations the two hardest to review.
    """

    store = controller.store
    plane = imp.ImprovementPlane(store, production.ProductionLedger(store))
    try:
        if args.action == "policy":
            if args.imp_file is None:
                current = plane.policy(args.project)
                result = None if current is None else current.as_row()
            else:
                declared = json.loads(args.imp_file.read_text())
                declared.setdefault("project_id", args.project)
                result = plane.set_policy(_improvement_policy(declared))
        elif args.action in ("enable", "disable"):
            result = plane.set_enabled(args.project, args.action == "enable")
        elif args.action == "objective":
            if args.imp_file is None:
                found = plane.objective(args.imp_objective)
                result = None if found is None else found.as_row()
            else:
                declared = json.loads(args.imp_file.read_text())
                result = plane.register_objective(imp.Objective(
                    objective_ref=declared["objective_ref"],
                    project_id=declared["project_id"],
                    improvement_class=declared["improvement_class"],
                    statement=declared["statement"],
                    metrics=tuple(imp.Metric(**item) for item in declared["metrics"]),
                    objective_version=declared.get("objective_version", "unset")))
        elif args.action == "objectives":
            result = list(plane.objectives(args.project))
        elif args.action == "admit":
            result = plane.admit_experiment(
                args.imp_objective, args.imp_trigger_class,
                args.imp_source or args.imp_objective,
                target_repository=args.imp_repository,
                baseline_sha=args.imp_baseline_sha,
                isolation_ref=args.imp_isolation)
        elif args.action == "generation":
            result = plane.open_generation(
                args.parent, baseline_sha=args.imp_baseline_sha,
                isolation_ref=args.imp_isolation)
        elif args.action == "baseline":
            result = plane.record_baseline(
                args.experiment, json.loads(args.imp_file.read_text()))
        elif args.action == "mission":
            lineage = plane.lineage(args.experiment)
            gates, source = _acceptance_gates(
                store, lineage["project_id"], lineage["target_repository"],
                args.imp_gates)
            mission, created = plane.create_candidate_mission(
                args.experiment, controller, acceptance_gate_ids=gates,
                extra={"acceptance_gate_source": source})
            result = {"created": created, "mission": mission}
        elif args.action == "seal":
            row = plane.experiments()
            mission_ref = next(item["mission_ref"] for item in row
                               if item["experiment_ref"] == args.experiment)
            result = plane.seal_candidate(
                args.experiment, {"id": mission_ref,
                                  "state": (store.get(mission_ref) or {}).get("state")},
                producer_identity=args.imp_producer,
                changed_paths=tuple(args.imp_paths))
        elif args.action == "evaluate":
            result = plane.evaluate_candidate(
                args.experiment, evaluator_identity=args.imp_evaluator,
                measurements=json.loads(args.imp_file.read_text()))
        elif args.action == "promote":
            bundle = production.ReleaseBundle.from_payload(
                json.loads(args.imp_bundle.read_text()))
            result = {"deployment_id": plane.stage_promotion(
                args.experiment, bundle, args.imp_environment)}
        elif args.action == "revert":
            result = plane.revert(args.experiment, reason=args.imp_reason)
        elif args.action == "close":
            result = plane.close(args.experiment, args.disposition,
                                 reason=args.imp_reason)
        elif args.action == "lineage":
            result = plane.lineage(args.experiment)
        elif args.action == "generations":
            result = list(plane.generations(args.lineage))
        else:
            result = list(plane.experiments(args.project))
    except portfolio.GateProvenanceError as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 2
    except imp.ImprovementRefusal as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 2
    except (imp.PolicyError, production.PolicyError) as exc:
        print(json.dumps({"refused": {"code": "IMPROVEMENT_POLICY_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _capacity(args, controller) -> int:
    """The Owner's surface onto Phase-1 capacity.

    Two of these write and they write facts, not decisions: ``policy`` records
    which runtimes the Owner put under management, and ``observe`` appends one
    measurement with the provenance that makes it usable.  Everything else
    reads.  There is deliberately no verb that marks a runtime available, moves
    a reset time, or overrides a refusal -- a window is a fact about a vendor's
    accounting, and a Factory that could declare one open would simply dispatch
    into a closed one and be told so by the harness.
    """

    store = controller.store
    try:
        if args.action == "policy":
            row = store.set_runtime_policy(capacity.RuntimePolicy(
                runtime_id=args.cap_runtime, managed=not args.cap_unmanaged,
                max_observation_age_seconds=args.cap_max_age,
                handoff=args.cap_handoff,
                unknown_reset_backoff_seconds=args.cap_backoff,
                policy_version=args.cap_policy_version))
            print(json.dumps(row, sort_keys=True))
            return 0
        if args.action == "policies":
            print(json.dumps({key: value.as_row() for key, value
                              in store.runtime_policies().items()}, sort_keys=True))
            return 0
        if args.action == "observe" and args.cap_from_bridge is not None:
            reading = capacity.observation_from_bridge_status(
                json.loads(args.cap_from_bridge.read_text()), store.clock(),
                args.cap_runtime)
            if reading is None:
                # The bridge holds no record for this profile.  Recording an
                # absence as a state would make an unmanaged runtime look
                # measured, so nothing is written and the caller is told.
                print(json.dumps({"refused": {
                    "code": "CAPACITY_OBSERVATION_ABSENT",
                    "detail": "the execution layer holds no capacity record "
                              "for this profile"}}, sort_keys=True))
                return 1
            print(json.dumps(store.observe_capacity(reading), sort_keys=True))
            return 0
        if args.action == "observe":
            row = store.observe_capacity(capacity.CapacityObservation(
                runtime_id=args.cap_runtime, state=args.cap_state,
                observed_at=(args.cap_observed_at if args.cap_observed_at is not None
                             else store.clock()),
                source=args.cap_source or "", source_ref=args.cap_source_ref or "",
                window_started_at=args.cap_window_started_at,
                expected_reset_at=args.cap_reset_at,
                remaining_units=args.cap_remaining, unit=args.cap_unit,
                precision=args.cap_precision))
            print(json.dumps(row, sort_keys=True))
            return 0
        if args.action == "readings":
            print(json.dumps({key: value.as_row() for key, value
                              in store.capacity_readings().items()}, sort_keys=True))
            return 0
        if args.action == "observations":
            print(json.dumps(store.capacity_observations(args.cap_runtime),
                             sort_keys=True))
            return 0
        if args.action == "checkpoint":
            print(json.dumps(store.capacity_checkpoint(args.cap_mission), sort_keys=True))
            return 0
        brief = sup.OperationsSupervisor(controller).capacity_brief()
        print(json.dumps(brief, sort_keys=True))
        # A brief nobody can act on is still a true brief, so this exits 1 only
        # when there is no usable runtime at all -- the one reading an operator
        # or a host job wants to branch on.
        return 0 if brief["usable_now"] else 1
    except (capacity.PolicyError, KeyError, TypeError) as exc:
        print(json.dumps({"refused": {"code": "CAPACITY_DECLARATION_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 1


def _supervisor(args, controller) -> int:
    """The Owner's own surface onto Stage 9.

    ``cycle`` is the only verb that causes anything, and it causes exactly one
    bounded cycle: the command returns and nothing is left running.  There is
    deliberately no verb here that approves a release, widens a policy, changes
    a protected surface, or installs a host service -- each of those is an act
    an earlier stage already made an explicit Owner decision, and offering a
    shortcut to it from the always-on surface is precisely how an operating
    layer would acquire authority it was never granted.
    """

    plane = sup.OperationsSupervisor(controller)
    transitions = {"start": "running", "resume": "running", "pause": "paused",
                   "drain": "draining", "stop": "stopped",
                   "emergency-stop": "emergency_stopped",
                   "clear-emergency": "stopped"}
    try:
        if args.action == "status":
            result = plane.control()
        elif args.action == "policy":
            if args.sup_project and not args.sup_classes \
                    and args.sup_policy_version == "unset" \
                    and args.sup_window_start is None:
                current = plane.policy(args.sup_project)
                if current is not None:
                    print(json.dumps(current.as_row(), sort_keys=True))
                    return 0
            result = plane.set_policy(sup.SupervisorPolicy(
                project_id=args.sup_project,
                work_classes=tuple(args.sup_classes) or sup.WORK_CLASSES,
                missions_per_cycle=args.sup_missions,
                maintenance_admissions=args.sup_maintenance,
                improvement_admissions=args.sup_improvement,
                window_start_hour=args.sup_window_start,
                window_end_hour=args.sup_window_end,
                failure_threshold=args.sup_threshold,
                suppression_seconds=args.sup_suppression,
                policy_version=args.sup_policy_version))
        elif args.action == "policies":
            result = [policy.as_row() for policy in plane.policies()]
        elif args.action in ("enable", "disable"):
            result = plane.set_enabled(args.sup_project, args.action == "enable")
        elif args.action == "cycle":
            result = plane.cycle(args.sup_worker, lease_seconds=args.sup_lease)
        elif args.action in transitions:
            result = plane.transition(transitions[args.action],
                                      actor=args.sup_actor, reason=args.sup_reason,
                                      evidence_ref=args.sup_evidence)
        elif args.action in ("hold", "release"):
            result = plane.hold(args.sup_project, held=args.action == "hold")
        elif args.action == "clear-health":
            result = plane.clear_health(args.sup_project, actor=args.sup_actor)
        elif args.action == "brief":
            result = plane.brief()
        elif args.action == "cycles":
            result = list(plane.cycles(limit=args.sup_limit))
        elif args.action == "transitions":
            result = list(plane.transitions())
        elif args.action == "selections":
            result = list(plane.selections(args.sup_cycle))
        elif args.action == "approval":
            result = activation.approval(args.sup_approval, label=args.sup_label)
        elif args.action.startswith("service-"):
            return _supervisor_service(args, plane)
        else:
            result = plane.service_contract(
                invocation=_service_invocation(args),
                interval_seconds=args.sup_interval)
    except sup.SupervisorRefusal as refusal:
        print(json.dumps({"refused": refusal.as_row()}, sort_keys=True))
        return 2
    except sup.PolicyError as exc:
        print(json.dumps({"refused": {"code": "SUPERVISOR_POLICY_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _dogfood(args, controller) -> int:
    """Read the run contract, or read the host against it.  Nothing is written.

    The service state is computed here rather than taken as a report: it is the
    one prerequisite the Controller can observe directly, and reading it from
    the same plan the install command would write means the preflight cannot
    disagree with the installer about what "installed" means.
    """

    try:
        contract = dogfood.load_contract(args.dog_contract)
    except dogfood.ContractError as exc:
        print(json.dumps({"refused": {"code": "DOGFOOD_CONTRACT_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    if args.action == "contract":
        print(json.dumps(contract.as_row(), sort_keys=True))
        return 0
    if args.action == "rehearse":
        if not args.dog_root:
            print(json.dumps({"refused": {
                "code": "DOGFOOD_REHEARSAL_ROOT_REQUIRED",
                "detail": "a rehearsal writes disposable stores and needs a "
                          "directory to write them in"}}, sort_keys=True))
            return 2
        result = rehearsal.run(args.dog_root, only=tuple(args.dog_scenarios))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["outcome"] == "REHEARSED" else 1
    if args.action == "gate":
        observed = (json.loads(Path(args.dog_evidence).read_text())
                    if args.dog_evidence else {})
        result = dogfood.productization_gate(contract, observed)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["verdict"] == "PROCEED_TO_PRODUCTIZATION" else 1

    reports = {}
    for pair in args.dog_reports:
        name, _, path = pair.partition("=")
        if not path:
            print(json.dumps({"refused": {"code": "DOGFOOD_REPORT_MALFORMED",
                                          "detail": "expected NAME=PATH, got %r"
                                                    % pair}}, sort_keys=True))
            return 2
        try:
            reports[name] = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            print(json.dumps({"refused": {"code": "DOGFOOD_REPORT_UNREADABLE",
                                          "detail": "%s: %s" % (name, exc)}},
                             sort_keys=True))
            return 2
    plane = sup.OperationsSupervisor(controller)
    try:
        plan = activation.from_contract(
            plane.service_contract(invocation=_service_invocation(args),
                                   interval_seconds=300),
            agents_dir=args.dog_agents_dir, state_dir=args.dog_state_dir,
            working_dir=str(Path.cwd()), label=args.dog_label)
        service = activation.doctor(plan)
    except activation.ActivationError as exc:
        service = {"definition_present": False, "drift": "unknown",
                   "detail": str(exc)}
    result = dogfood.preflight(contract, store=controller.store,
                               supervisor_plane=plane, reports=reports,
                               service_doctor=service).as_row()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


def _shift_facts(args, controller, contract, entry):
    """Gather what the gate reads, from the planes that own each fact.

    The preflight is the one the Factory already had, run here rather than
    re-implemented, so the shift gate cannot disagree with ``dogfood preflight``
    about whether the host is ready.
    """

    reports = {}
    for pair in args.sh_reports:
        name, _, path = pair.partition("=")
        if not path:
            raise shift_plane.ShiftGovernanceRefusal("SHIFT_REPORT_MALFORMED",
                                           "expected NAME=PATH, got %r" % pair)
        try:
            reports[name] = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise shift_plane.ShiftGovernanceRefusal("SHIFT_REPORT_UNREADABLE",
                                           "%s: %s" % (name, exc))
    reachable = None
    if args.sh_reachable:
        try:
            reachable = json.loads(Path(args.sh_reachable).read_text())
        except (OSError, ValueError) as exc:
            raise shift_plane.ShiftGovernanceRefusal("SHIFT_REACHABILITY_UNREADABLE",
                                           str(exc))
    registry = reports.get("project_registry")
    registry = (registry or {}).get("projects") if isinstance(registry, dict) else None
    doctor = reports.get("bridge_doctor")
    offered = doctor.get("capabilities") if isinstance(doctor, dict) else None
    plane = sup.OperationsSupervisor(controller)
    ledger = production.ProductionLedger(controller.store)
    try:
        plan = activation.from_contract(
            plane.service_contract(invocation=_service_invocation(args),
                                   interval_seconds=300),
            agents_dir=args.sh_agents_dir, state_dir=args.sh_state_dir,
            working_dir=str(Path.cwd()), label=args.sh_label)
        service = activation.doctor(plan)
    except activation.ActivationError as exc:
        service = {"definition_present": False, "drift": "unknown",
                   "detail": str(exc)}
    pre = dogfood.preflight(contract, store=controller.store,
                            supervisor_plane=plane, reports=reports,
                            service_doctor=service,
                            improvement_plane=imp.ImprovementPlane(
                                controller.store, ledger)).as_row()
    declared = {}
    for name in contract.projects:
        try:
            gates, source = controller.store.declared_acceptance_gates(name)
        except Exception:                                 # noqa: BLE001
            continue
        declared[name] = {"acceptance_gate_ids": list(gates), "source": source}
    readings = {name: reading.as_row() for name, reading
                in controller.store.capacity_readings().items()}
    denied = controller.store.portfolio_policy().as_row().get("denied_profiles", ())
    usable = shift_plane.eligible(contract.provider_profiles, readings, denied)
    request = shift_plane.ActivationRequest(
        request_ref=args.sh_request, run_ref=contract.run_ref,
        portfolio_ref=entry.portfolio_ref, mission_ceiling=args.sh_missions,
        duration_seconds=args.sh_duration, budget_ceiling=args.sh_budget,
        budget_currency=args.sh_currency)
    facts = shift_plane.GateFacts(
        preflight=pre, portfolio=entry, request=request,
        contract_projects=contract.projects,
        contract_work_classes=contract.work_classes,
        contract_environment_classes=contract.environment_classes,
        contract_budget_ceiling=contract.budget_ceiling,
        contract_budget_currency=contract.budget_currency,
        declared_gates=declared, capacity_readings=readings,
        eligible_profiles=usable, fetchable_shas=reachable,
        project_registry=registry, offered_capabilities=offered)
    return facts, plane, readings, usable, denied


def _shift_runtime(args, controller) -> int:
    """Expose the runtime's observational seam without adding authority.

    ``shift`` owns the Owner's grant and suspend decisions.  This distinct
    command reaches the crash-safe runtime's status and cold-start resume
    projection, both of which read durable state and never claim work.
    """

    runtime = shift_runtime.ShiftRuntime(controller)
    try:
        if args.action == "status":
            result = runtime.status(
                mission_id=args.runtime_mission,
                project_id=args.runtime_project,
            )
        else:
            result = runtime.resume_preview(
                mission_id=args.runtime_mission,
                target_profile=args.runtime_profile,
            )
    except shift_runtime.ShiftRefusal as refusal:
        # Runtime refusals deliberately keep their flat row shape; governance
        # refusals remain nested under ``refused`` in the shift CLI.
        print(json.dumps(refusal.as_row(), sort_keys=True, default=str))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _shift(args, controller) -> int:
    """The Owner's four acts and the readings behind them.  Nothing is started.

    ``apply`` writes a grant and no more: it loads no service, starts no
    process and admits no mission by itself.  A shift becomes work only when
    the ordinary execution path reads the grant and finds it active, which is
    the separation scope 5 of SF-144 asks for -- preparing a host and
    authorizing missions are different acts with different records.
    """

    try:
        entry = shift_plane.load_portfolio(args.sh_portfolio)
        contract = dogfood.load_contract(args.sh_contract)
    except (shift_plane.ShiftError, dogfood.ContractError) as exc:
        print(json.dumps({"refused": {"code": "SHIFT_CONTRACT_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    if args.action == "portfolio":
        print(json.dumps(entry.as_row(), sort_keys=True))
        return 0
    plane = shift_plane.ShiftPlane(controller.store)
    if args.action == "grants":
        print(json.dumps({"grants": plane.grants(args.sh_limit)}, sort_keys=True))
        return 0
    if args.action == "events":
        print(json.dumps({"events": plane.events(limit=args.sh_limit)},
                         sort_keys=True))
        return 0
    if args.action in ("revoke", "suspend", "resume"):
        try:
            if args.action == "revoke":
                result = plane.revoke(args.sh_request, reason=args.sh_reason,
                                      actor=args.sh_actor)
            elif args.action == "resume":
                result = plane.resume(args.sh_request, actor=args.sh_actor)
            else:
                outcomes = plane.outcomes(entry)
                in_flight = sum(
                    1 for ref, value in outcomes.items()
                    if value not in shift_plane.TERMINAL_MISSION_STATES)
                result = plane.suspend(args.sh_request,
                                       resume_ref=args.sh_resume_ref or "",
                                       missions_in_flight=in_flight,
                                       actor=args.sh_actor)
        except shift_plane.ShiftGovernanceRefusal as refusal:
            print(json.dumps(refusal.as_row(), sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        facts, ops, readings, usable, denied = _shift_facts(
            args, controller, contract, entry)
    except shift_plane.ShiftGovernanceRefusal as refusal:
        print(json.dumps(refusal.as_row(), sort_keys=True))
        return 2
    reading = shift_plane.gate(facts)
    if args.action == "gate":
        print(json.dumps(reading, sort_keys=True))
        return 0 if reading["ready"] else 1
    approval = shift_plane.approval_record(args.sh_approval,
                                           request_ref=args.sh_request)
    if args.action == "preview":
        result = plane.preview(facts, approval=approval)
        runtime = shift_runtime.ShiftRuntime(
            controller, supervisor_plane=ops)
        result["runtime"] = {
            "status": runtime.status(),
            "resume_preview": runtime.resume_preview(),
        }
        print(json.dumps(result, sort_keys=True, default=str))
        return 0 if reading["ready"] else 1
    if args.action == "apply":
        try:
            result = plane.apply(facts, approval, actor=args.sh_actor)
        except shift_plane.ShiftGovernanceRefusal as refusal:
            print(json.dumps(refusal.as_row(), sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    control = ops.control()
    observed = plane.observe(
        entry, control_state=control.get("state", "stopped"),
        gate_ready=reading["ready"], capacity_readings=readings,
        profiles=contract.provider_profiles, denied=denied,
        emergency_stop=controller.store.portfolio_policy().emergency_stop)
    grant = plane.grant(args.sh_request) or plane.grant()
    now = time.time()
    if args.action == "status":
        print(json.dumps({
            "contract_version": shift_plane.CONTRACT_VERSION,
            "state": shift_plane.state(grant, observed, now),
            "drain_reasons": list(shift_plane.drain_reasons(grant, observed, now)),
            "grant": None if grant is None else grant.as_row(),
            "gate_ready": reading["ready"], "blockers": reading["blockers"],
            "control_state": control.get("state", "unknown"),
            "eligible_profiles": list(usable),
        }, sort_keys=True, default=str))
        return 0
    if args.action == "admit":
        print(json.dumps(shift_plane.admission(grant, entry, observed,
                                               plane.outcomes(entry), now,
                                               plane.retryable(entry)),
                         sort_keys=True))
        return 0
    owner_actions = [{"check": row["check"], "detail": row["detail"]}
                     for row in reading["blockers"]]
    print(json.dumps(shift_plane.brief(
        grant, observed, reading, entry, plane.outcomes(entry), now,
        admitted_projects=sorted(controller.store.projects()),
        admitted_capabilities=sorted(contract.work_classes),
        owner_actions=owner_actions,
        retryable=plane.retryable(entry)), sort_keys=True, default=str))
    return 0


def _service_invocation(args) -> list[str]:
    return [sys.executable, "-m", "factory_controller.cli",
            "--db", args.db, "supervisor", "cycle"]


def _supervisor_service(args, plane) -> int:
    """Install, inspect or remove the host service.  It never loads one.

    ``--apply`` is required to write anything, and an apply is refused unless a
    durable Owner approval already says this host may run the supervisor --
    scope 5 of SF-142, expressed where it can be checked rather than promised.
    Even with both, the job definition is written and nothing is started: the
    activation step is returned as text for the Owner to run.
    """

    contract = plane.service_contract(invocation=_service_invocation(args),
                                      interval_seconds=args.sup_interval)
    try:
        plan = activation.from_contract(
            contract, agents_dir=args.sup_agents_dir,
            state_dir=args.sup_state_dir, working_dir=args.sup_working_dir,
            label=args.sup_label)
    except activation.ActivationError as exc:
        print(json.dumps({"refused": {"code": "SUPERVISOR_SERVICE_PLAN_INVALID",
                                      "detail": str(exc)}}, sort_keys=True))
        return 2
    granted = activation.approval(args.sup_approval, label=plan.label)
    if args.action == "service-doctor":
        print(json.dumps({**activation.doctor(plan), "approval": granted},
                         sort_keys=True))
        return 0
    if args.action == "service-uninstall":
        print(json.dumps(activation.uninstall(plan, apply=args.sup_apply),
                         sort_keys=True))
        return 0
    if args.action == "service-plan" or not args.sup_apply:
        print(json.dumps({**activation.install(plan, apply=False),
                          "approval": granted}, sort_keys=True))
        return 0
    if not granted["approved"]:
        print(json.dumps({"refused": {
            "code": "SUPERVISOR_ACTIVATION_UNAPPROVED",
            "detail": "writing the host service needs a durable Owner "
                      "approval; %s" % granted["detail"],
            "approval": granted,
            "activation": activation.activation_command(plan)}}, sort_keys=True))
        return 2
    print(json.dumps({**activation.install(plan, apply=True),
                      "approval": granted}, sort_keys=True))
    return 0


def _improvement_policy(declared: dict) -> "imp.ImprovementPolicy":
    """Turn a declaration file into a policy, keeping the tuples tuples."""

    surfaces = {name: tuple(prefixes) for name, prefixes
                in declared.get("protected_surfaces", {}).items()}
    values = dict(declared)
    values["protected_surfaces"] = surfaces
    for name in ("improvement_classes", "trigger_classes", "environment_classes",
                 "self_target_repositories"):
        if name in values:
            values[name] = tuple(values[name])
    return imp.ImprovementPolicy(**values)


def _resolved_db(args) -> Path:
    """Which Factory a command with no ``--db`` is talking about.

    The installed one.  This resolution used to happen inside the ``factory``
    branch alone, so `./dev factory status` read the host's ledger while
    `./dev release show` read a file in whatever directory it was run from --
    and an Owner recording the validation of a Release Candidate they had just
    been shown was told RC_NOT_FOUND.  One host, one Factory, one ledger; an
    explicit ``--db`` still points wherever it says.
    """

    db_path = Path(args.db)
    if args.db == "factory-controller.db":
        return factory_lifecycle.FactoryConfig.default().state_dir / args.db
    if not db_path.is_absolute():
        return Path.cwd() / db_path
    return db_path


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.db = str(_resolved_db(args))
    if args.command == "factory":
        config = factory_lifecycle.FactoryConfig.default()
        db_path = Path(args.db)
        lifecycle = factory_lifecycle.FactoryLifecycle(
            Controller(MissionStore(db_path),
                       JsonProcessAdapter(shlex.split(args.adapter)),
                       retry_policy=RetryPolicy()),
            config=config)
        if args.watch:
            if args.factory_action != "status":
                print("BLOCKED: --watch is only supported for factory status.")
                return 1
            try:
                return lifecycle.watch(args.interval)
            except factory_lifecycle.FactoryRefusal as refusal:
                print("BLOCKED: " + refusal.detail)
                return 1
        result = lifecycle.dispatch(args.factory_action, package=args.package)
        print(result.render())
        return 0 if result.ok else 1
    controller = _controller(args)
    store = controller.store
    if args.command == "submit":
        payload = json.loads(args.file.read_text() if args.file else sys.stdin.read())
        mission, created = controller.submit(payload, args.key)
        print(json.dumps({"created": created, "mission": mission}, sort_keys=True))
    elif args.command == "work-once":
        print(json.dumps(controller.work_once(args.worker), sort_keys=True))
    elif args.command == "worker":
        idle = 0
        while args.max_idle_polls == 0 or idle < args.max_idle_polls:
            result = controller.work_once(args.worker)
            idle = idle + 1 if result is None else 0
            if result is None:
                time.sleep(args.poll_seconds)
    elif args.command == "status":
        print(json.dumps(store.get(args.mission_id) if args.mission_id else store.counts(), sort_keys=True))
    elif args.command == "history":
        print(json.dumps(store.history(args.mission_id), sort_keys=True))
    elif args.command == "route":
        print(json.dumps(store.route_history(args.mission_id), sort_keys=True))
    elif args.command == "telemetry":
        print(json.dumps(store.telemetry(args.mission_id), sort_keys=True))
    elif args.command == "context":
        print(json.dumps(store.context_history(args.mission_id), sort_keys=True))
    elif args.command == "economics":
        print(json.dumps(store.economics(args.corpus), sort_keys=True))
    elif args.command == "cancel":
        print(json.dumps({"state": store.cancel(args.mission_id)}))
    elif args.command == "baton":
        report = continuity.WorkBatonStore(args.db).inspect(args.baton_id)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["count"] or args.baton_id is None else 1
    elif args.command == "project":
        if args.action == "list":
            print(json.dumps({key: value.as_row() for key, value in store.projects().items()},
                             sort_keys=True))
        elif args.action == "state":
            print(json.dumps(store.set_project_state(args.id, args.state), sort_keys=True))
        else:
            print(json.dumps(store.register_project(portfolio.ProjectPolicy(
                project_id=args.id, repository=args.repository,
                state=args.state or "enabled", priority=args.priority,
                concurrency_cap=args.cap, budget_ceiling=args.budget,
                budget_currency=args.currency, context_ceiling_bytes=args.context_ceiling,
                acceptance_gate_ids=tuple(args.gates),
                acceptance_gate_source=args.gate_source,
                policy_version=args.policy_version)), sort_keys=True))
    elif args.command == "portfolio":
        current = store.portfolio_policy()
        if args.emergency_stop or args.resume:
            print(json.dumps(store.emergency_stop(bool(args.emergency_stop)), sort_keys=True))
        elif args.concurrency is None and args.aging is None and args.policy_version is None:
            print(json.dumps(current.as_row(), sort_keys=True))
        else:
            print(json.dumps(store.set_portfolio_policy(portfolio.PortfolioPolicy(
                portfolio_concurrency=current.portfolio_concurrency if args.concurrency is None
                else args.concurrency,
                emergency_stop=current.emergency_stop,
                aging_seconds=current.aging_seconds if args.aging is None else args.aging,
                policy_version=current.policy_version if args.policy_version is None
                else args.policy_version)), sort_keys=True))
    elif args.command == "depend":
        print(json.dumps(store.add_dependency(args.mission_id, args.depends_on,
                                              on_failure=args.on_failure), sort_keys=True))
    elif args.command == "deps":
        print(json.dumps(store.dependency_status(args.mission_id), sort_keys=True))
    elif args.command == "schedule":
        print(json.dumps(store.schedule_preview(), sort_keys=True))
    elif args.command == "coordination":
        print(json.dumps(store.coordination(args.mission_id, limit=args.limit), sort_keys=True))
    elif args.command == "portfolio-economics":
        print(json.dumps(store.portfolio_economics(args.project), sort_keys=True))
    elif args.command == "advise":
        endpoint = advisory.endpoint_advisor(args.endpoint) if args.endpoint else None
        if args.probe:
            print(json.dumps((endpoint or advisory.endpoint_advisor()).probe(), sort_keys=True))
            return 0
        port = endpoint
        if args.proposals:
            port = advisory.StaticAdvisor(json.loads(args.proposals.read_text()))
        policy = json.loads(args.policy.read_text()) if args.policy else {}
        print(json.dumps(advisory.coordinate(store, port, policy), sort_keys=True))
    elif args.command == "production":
        return _production(args, store)
    elif args.command == "release":
        return _release(args, store)
    elif args.command == "maintenance":
        return _maintenance(args, controller)
    elif args.command == "improvement":
        return _improvement(args, controller)
    elif args.command == "capacity":
        return _capacity(args, controller)
    elif args.command == "supervisor":
        return _supervisor(args, controller)
    elif args.command == "dogfood":
        return _dogfood(args, controller)
    elif args.command == "shift":
        return _shift(args, controller)
    elif args.command == "shift-runtime":
        return _shift_runtime(args, controller)
    elif args.command == "harness":
        ids = []
        for index in range(args.missions):
            mission, _ = controller.submit({
                "work_item_id": f"HARNESS-{index + 1}",
                "repository": f"disposable-{index + 1}",
                "execution_mode": "fixture",
                "acceptance_gate_ids": ["HARNESS-GATE"],
            }, f"harness:{index + 1}")
            ids.append(mission["id"])
        while controller.work_once("harness-worker") is not None:
            pass
        states = {mission_id: store.get(mission_id)["state"] for mission_id in ids}  # type: ignore[index]
        print(json.dumps({"missions": len(ids), "states": states, "counts": store.counts()}, sort_keys=True))
        return 0 if set(states.values()) == {"completed"} else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
