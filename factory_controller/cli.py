"""Operator CLI for submission, unattended workers, status, and history."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from . import advisor as advisory
from . import portfolio
from . import production
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
    return p


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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
