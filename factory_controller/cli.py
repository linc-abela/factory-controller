"""Operator CLI for submission, unattended workers, status, and history."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

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
    cancel = sub.add_parser("cancel")
    cancel.add_argument("mission_id")
    harness = sub.add_parser("harness")
    harness.add_argument("--missions", type=int, default=10)
    return p


def _controller(args) -> Controller:
    return Controller(MissionStore(args.db), JsonProcessAdapter(shlex.split(args.adapter)), retry_policy=RetryPolicy())


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
    elif args.command == "cancel":
        print(json.dumps({"state": store.cancel(args.mission_id)}))
    elif args.command == "harness":
        ids = []
        for index in range(args.missions):
            mission, _ = controller.submit({"work_item_id": f"HARNESS-{index + 1}", "repository": f"disposable-{index + 1}"}, f"harness:{index + 1}")
            ids.append(mission["id"])
        while controller.work_once("harness-worker") is not None:
            pass
        states = {mission_id: store.get(mission_id)["state"] for mission_id in ids}  # type: ignore[index]
        print(json.dumps({"missions": len(ids), "states": states, "counts": store.counts()}, sort_keys=True))
        return 0 if set(states.values()) == {"DONE"} else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

