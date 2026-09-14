from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from factory_v2.adapters.antigravity import AntigravityDistributor, AntigravityVerifier
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.adapters.selection import build_executor, selected_executor_kind
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.canonical import ContractError, load_pcp_file
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.store import Store

DEFAULT_HOME = Path(os.environ.get("FACTORY_V2_HOME", Path.home() / ".factory-v2"))


def _home() -> Path:
    root = Path(os.environ.get("FACTORY_V2_HOME", DEFAULT_HOME))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _controller(*, simulated: bool) -> Controller:
    home = _home()
    store = Store(home / "ledger.sqlite")
    workspace = home / "sandboxes"
    if simulated:
        print("harness_mode=simulated  (test doubles; not real Hermes/Grok/Antigravity)")
        executor = ScriptedGrok(["sim-artifact-1"])
        manager = ScriptedHermes(executor)
        verifier = ScriptedVerifier({"sim-artifact-1": (True, True)})
        distributor = ScriptedDistributor()
    else:
        kind = selected_executor_kind()
        executor = build_executor(kind)
        print(
            f"harness_mode=real  executor={kind}  "
            "(fail-closed if Hermes/executor/Antigravity are unavailable)"
        )
        manager = NousHermesAdapter(executor=executor)
        verifier = AntigravityVerifier()
        distributor = AntigravityDistributor()
    return Controller(store, manager, verifier, distributor, workspace)


def _print(snap) -> None:
    print(json.dumps(snap.as_status_dict(), indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-v2",
        description="Software Factory v2 Controller (lifecycle authority).",
    )
    parser.add_argument(
        "--simulated",
        action="store_true",
        help="Use labeled simulated adapters. Default is real (fail-closed).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_admit = sub.add_parser("admit-pcp", help="Gate 1: admit a canonical Owner-approved PCP")
    p_admit.add_argument("pcp_json")
    p_tick = sub.add_parser("tick", help="Advance one legal lifecycle step")
    p_tick.add_argument("mission_id")
    p_status = sub.add_parser("status")
    p_status.add_argument("mission_id")
    p_approve = sub.add_parser("approve", help="Owner Gate 2 APPROVE")
    p_approve.add_argument("mission_id")
    p_reject = sub.add_parser("reject", help="Owner Gate 2 REJECT")
    p_reject.add_argument("mission_id")
    p_reject.add_argument("--reason", default="")
    p_dist = sub.add_parser("distribute")
    p_dist.add_argument("mission_id")
    p_serve = sub.add_parser(
        "serve",
        help="Local event-driven PCP intake (POST /v1/pcp). Normal V2 path.",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FACTORY_V2_INTAKE_PORT", "8790")),
    )
    args = parser.parse_args(argv)
    ctl = _controller(simulated=args.simulated)
    try:
        if args.cmd == "admit-pcp":
            _print(ctl.admit_pcp(load_pcp_file(args.pcp_json)))
        elif args.cmd == "tick":
            _print(ctl.tick(args.mission_id))
        elif args.cmd == "status":
            _print(ctl.get(args.mission_id))
        elif args.cmd == "approve":
            _print(ctl.owner_decide(args.mission_id, "APPROVE"))
        elif args.cmd == "reject":
            _print(ctl.owner_decide(args.mission_id, "REJECT", args.reason))
        elif args.cmd == "distribute":
            _print(ctl.distribute(args.mission_id))
        elif args.cmd == "serve":
            from factory_v2.serve import serve_forever

            try:
                serve_forever(ctl, host=args.host, port=args.port)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            parser.error(args.cmd)
    except (GateError, InvariantError, ContractError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
