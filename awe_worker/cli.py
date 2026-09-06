"""Command-line interface for Autonomous AWE Worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
from .observation import AWEObservationService, DirectoryTaskSource
from .reconciliation import TurnCadenceReconciler
from .worker import AWEAutonomousWorker


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
    obs_p.add_argument("--source-dir", default="tests/fixtures/work_exchange", help="Directory of task packets")
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
    wake_p.add_argument("--dry-run", action="store_true", default=True)

    # ground
    ground_p = subparsers.add_parser("ground", help="Ground a task using Context Broker")
    ground_p.add_argument("--repo", default=".", help="Repository path to ground")
    ground_p.add_argument("--task-id", default="SF-217")

    # cycle
    cycle_p = subparsers.add_parser("cycle", help="Run one autonomous intake/dispatch/reconcile cycle")
    cycle_p.add_argument("--worker-id", default="host-worker", help="Worker identity")
    cycle_p.add_argument("--slot", help="Target execution slot")
    cycle_p.add_argument("--source-dir", default="tests/fixtures/work_exchange")
    cycle_p.add_argument("--repo", default=".")
    cycle_p.add_argument("--dry-run", action="store_true", default=True)

    # status
    subparsers.add_parser("status", help="Show worker ledger health and active claims")

    args = parser.parse_args(argv)
    ledger = AWELedger(args.db)

    if args.command == "observe":
        src = DirectoryTaskSource(args.source_dir)
        obs = AWEObservationService(src)
        slot = ExecutionSlot.parse(args.slot) if args.slot else None
        tasks = obs.filter_eligible(slot=slot)
        output = [t.as_dict() for t in tasks]
        json.dump({"eligible_count": len(tasks), "tasks": output}, sys.stdout, indent=2)
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
        src = DirectoryTaskSource(args.source_dir)
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=src,
            target_repo=args.repo,
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
            "detail": summary.detail,
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

        json.dump({
            "active_claims_count": len(active),
            "active_claims": active,
            "harnesses": harness_status,
        }, sys.stdout, indent=2)
        print()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
