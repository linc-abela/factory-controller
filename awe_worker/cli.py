"""Command-line interface for Autonomous AWE Worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .cadence import CadenceContinuationCoordinator
from .detector import CompletionDetector
from .grounding import ContextBrokerGrounder
from .harness import (
    AntigravityHarnessAdapter,
    CodexHarnessAdapter,
    CursorHarnessAdapter,
    get_adapter_for_harness,
)
from .ledger import AWELedger
from .model import (
    AWEWorkItem,
    CertificationRecord,
    CertificationVerdict,
    ExecutionSlot,
)
from .notion import (
    DEFAULT_AWE_DATABASE_ID,
    LiveNotionTaskSource,
    NotionClient,
    NotionSourceOfRecord,
)
from .observation import AWEObservationService, DirectoryTaskSource, NotionTaskSource
from .reconciliation import TurnCadenceReconciler
from .scheduler import AWEScheduledRunner
from .worker import AWEAutonomousWorker


def _add_dry_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add explicit live and dry-run flags with fail-safe default."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="Execute live dispatch and write-back",
    )
    group.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Alias for --live: execute live dispatch",
    )
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Simulate dispatch without executing external commands (default: True)",
    )


def _resolve_task_source(
    source_type: str,
    source_dir: str,
    database_id: str,
) -> tuple[Any, NotionSourceOfRecord | None]:
    """Resolve task source and optional source-of-record based on configuration."""
    nc = NotionClient()
    if source_type == "notion" and nc.is_configured:
        src = LiveNotionTaskSource(client=nc, database_id=database_id)
        sor = NotionSourceOfRecord(client=nc)
        return src, sor

    # Default to directory source
    src = DirectoryTaskSource(source_dir)
    return src, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="awe-worker",
        description="Autonomous Agent Work Exchange (AWE) Queue Worker",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("AWE_WORKER_DB", "awe_worker.db"),
        help="Path to SQLite claims ledger database",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # observe
    obs_p = subparsers.add_parser("observe", help="Observe eligible tasks in exchange")
    obs_p.add_argument("--source", choices=["notion", "dir"], default="notion", help="Task source (default: notion)")
    obs_p.add_argument("--source-dir", default="tests/fixtures/work_exchange", help="Directory of task packets if source=dir")
    obs_p.add_argument("--database-id", default=DEFAULT_AWE_DATABASE_ID, help="Notion AWE Database ID")
    obs_p.add_argument("--slot", help="Optional execution slot (e.g. 'antigravity/gemini-3.8-flash/high')")

    # claim
    claim_p = subparsers.add_parser("claim", help="Atomically claim a task")
    claim_p.add_argument("--task-id", required=True, help="Task ID to claim")
    claim_p.add_argument("--worker-id", default="local-worker-1", help="Worker ID")
    claim_p.add_argument("--slot", default="antigravity/gemini-3.8-flash/high", help="Execution slot")
    claim_p.add_argument("--lease", type=float, default=60.0, help="Lease duration in seconds")

    # wake
    wake_p = subparsers.add_parser("wake", help="Wake/dispatch a harness for a task")
    wake_p.add_argument("--task-id", required=True)
    wake_p.add_argument("--harness", default="antigravity", choices=["antigravity", "codex", "cursor", "claude"])
    wake_p.add_argument("--model", default="gemini-3.8-flash-high")
    wake_p.add_argument("--effort", default="high")
    _add_dry_run_arguments(wake_p)

    # ground
    ground_p = subparsers.add_parser("ground", help="Ground a task using Context Broker")
    ground_p.add_argument("--repo", default=".", help="Repository path to ground")
    ground_p.add_argument("--task-id", default="SF-217")

    # cycle
    cycle_p = subparsers.add_parser("cycle", help="Run one autonomous intake/dispatch/reconcile cycle")
    cycle_p.add_argument("--worker-id", default="host-worker", help="Worker identity")
    cycle_p.add_argument("--slot", help="Target execution slot")
    cycle_p.add_argument("--source", choices=["notion", "dir"], default="notion", help="Task source (default: notion)")
    cycle_p.add_argument("--source-dir", default="tests/fixtures/work_exchange")
    cycle_p.add_argument("--database-id", default=DEFAULT_AWE_DATABASE_ID)
    cycle_p.add_argument("--repo", default=".")
    _add_dry_run_arguments(cycle_p)

    # run (scheduled worker service)
    run_p = subparsers.add_parser("run", help="Run bounded or persistent scheduled worker loop")
    run_p.add_argument("--worker-id", default="host-worker-1", help="Worker identity")
    run_p.add_argument("--slot", help="Target execution slot")
    run_p.add_argument("--interval", type=float, default=10.0, help="Cycle interval in seconds")
    run_p.add_argument("--max-cycles", type=int, default=None, help="Maximum cycles before stopping (optional)")
    run_p.add_argument("--source", choices=["notion", "dir"], default="notion")
    run_p.add_argument("--source-dir", default="tests/fixtures/work_exchange")
    run_p.add_argument("--database-id", default=DEFAULT_AWE_DATABASE_ID)
    run_p.add_argument("--repo", default=".")
    run_p.add_argument("--heartbeat-file", default=None, help="Path to write JSON heartbeat file")
    _add_dry_run_arguments(run_p)

    # status
    subparsers.add_parser("status", help="Show worker ledger health, active claims, and liveness")

    args = parser.parse_args(argv)
    ledger = AWELedger(args.db)

    if args.command == "observe":
        src, _ = _resolve_task_source(args.source, args.source_dir, args.database_id)
        obs = AWEObservationService(src)
        slot = ExecutionSlot.parse(args.slot) if args.slot else None
        tasks = obs.filter_eligible(slot=slot)
        output = [t.as_dict() for t in tasks]
        json.dump({"eligible_count": len(tasks), "source": args.source, "tasks": output}, sys.stdout, indent=2)
        print()
        return 0

    elif args.command == "claim":
        slot = ExecutionSlot.parse(args.slot)
        res = ledger.claim(
            task_id=args.task_id,
            lineage_id=args.task_id,
            worker_id=args.worker_id,
            slot_key=slot.key,
            lease_seconds=args.lease,
        )
        json.dump({"ok": res.ok, "action": res.action, "token": res.token, "code": res.code, "detail": res.detail}, sys.stdout, indent=2)
        print()
        return 0 if res.ok else 1

    elif args.command == "wake":
        adapter = get_adapter_for_harness(args.harness)
        slot = ExecutionSlot(harness=args.harness, model=args.model, effort=args.effort)
        task = AWEWorkItem(
            task_id=args.task_id,
            title=f"Task {args.task_id}",
            lane=args.harness,
            role="developer",
            status="In Progress",
            model=args.model,
            effort=args.effort,
            sequence=1,
        )
        receipt = adapter.wake(task, dry_run=args.dry_run)
        json.dump({
            "success": receipt.success,
            "harness": receipt.harness,
            "error_code": receipt.error_code,
            "detail": receipt.detail,
            "command": receipt.command,
        }, sys.stdout, indent=2)
        print()
        return 0 if receipt.success else 1

    elif args.command == "ground":
        grounder = ContextBrokerGrounder()
        task = AWEWorkItem(
            task_id=args.task_id,
            title=f"Task {args.task_id}",
            lane="antigravity",
            role="developer",
            status="In Progress",
            model="gemini-3.8-flash",
            effort="high",
            sequence=1,
        )
        res = grounder.ground_task(repo_path=args.repo, task=task)
        json.dump({
            "ok": res.ok,
            "source": res.source,
            "repo_identity": res.repo_identity,
            "head_sha": res.head_sha,
            "manifest_digest": res.manifest_digest,
            "selected_paths": res.selected_paths,
            "full_eligible_bytes": res.full_eligible_bytes,
            "selected_bytes": res.selected_bytes,
            "reduction_ratio": f"{res.reduction_ratio * 100:.1f}%",
            "latency_ms": f"{res.latency_ms:.2f}ms",
            "fallback_reason": res.fallback_reason,
        }, sys.stdout, indent=2)
        print()
        return 0 if res.ok else 1

    elif args.command == "cycle":
        slot = ExecutionSlot.parse(args.slot) if args.slot else None
        src, sor = _resolve_task_source(args.source, args.source_dir, args.database_id)
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=src,
            target_repo=args.repo,
            source_of_record=sor,
        )
        summary = worker.run_cycle(
            worker_id=args.worker_id,
            target_slot=slot,
            dry_run=args.dry_run,
        )
        json.dump({
            "cycle_id": summary.cycle_id,
            "worker_id": summary.worker_id,
            "health": summary.health,
            "claimed_task": summary.claimed_task,
            "grounding_source": summary.grounding_source,
            "wake_receipt": {
                "success": summary.wake_receipt.success,
                "harness": summary.wake_receipt.harness,
                "error_code": summary.wake_receipt.error_code,
                "detail": summary.wake_receipt.detail,
            } if summary.wake_receipt else None,
            "detail": summary.detail,
        }, sys.stdout, indent=2)
        print()
        return 0

    elif args.command == "run":
        slot = ExecutionSlot.parse(args.slot) if args.slot else None
        src, sor = _resolve_task_source(args.source, args.source_dir, args.database_id)
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=src,
            target_repo=args.repo,
            source_of_record=sor,
        )
        runner = AWEScheduledRunner(
            worker=worker,
            worker_id=args.worker_id,
            heartbeat_file=args.heartbeat_file,
        )
        summaries = runner.run(
            interval_seconds=args.interval,
            max_cycles=args.max_cycles,
            target_slot=slot,
            dry_run=args.dry_run,
        )
        json.dump({
            "worker_id": args.worker_id,
            "cycles_run": len(summaries),
            "last_health": summaries[-1].health if summaries else "idle",
            "last_claimed_task": summaries[-1].claimed_task if summaries else None,
        }, sys.stdout, indent=2)
        print()
        return 0

    elif args.command == "status":
        active = ledger.get_active_claims()
        # Probe harnesses
        harness_status = {}
        for h in ["antigravity", "codex", "cursor", "claude"]:
            adapter = get_adapter_for_harness(h)
            avail, code, det = adapter.check_availability()
            harness_status[h] = {"available": avail, "code": code, "detail": det}

        # Check worker liveness
        runner = AWEScheduledRunner(
            worker=AWEAutonomousWorker(ledger=ledger, source=DirectoryTaskSource("tests/fixtures/work_exchange")),
            worker_id="status-probe",
        )
        liveness = [
            {
                "worker_id": rec.worker_id,
                "pid": rec.pid,
                "status": rec.status,
                "cycles_completed": rec.cycles_completed,
                "last_heartbeat": rec.last_heartbeat,
            }
            for rec in runner.get_liveness_status()
        ]

        json.dump({
            "active_claims_count": len(active),
            "active_claims": active,
            "harnesses": harness_status,
            "worker_liveness": liveness,
        }, sys.stdout, indent=2)
        print()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
