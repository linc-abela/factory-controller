from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from factory_v2.adapters.antigravity import AntigravityDistributor, AntigravityVerifier
from factory_v2.adapters.grok_build import GrokBuildAdapter
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.adapters.simulated import (
    ScriptedDistributor,
    ScriptedGrok,
    ScriptedHermes,
    ScriptedVerifier,
)
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.models import PCP
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
        print("harness_mode=real  (fail-closed if Hermes/Grok/Antigravity are unavailable)")
        executor = GrokBuildAdapter()
        manager = NousHermesAdapter(executor)
        verifier = AntigravityVerifier()
        distributor = AntigravityDistributor()
    return Controller(store, manager, executor, verifier, distributor, workspace)


def _pcp_from_file(path: str) -> PCP:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return PCP(
        title=data["title"],
        intent=data["intent"],
        product=data.get("product", ""),
        extra={k: v for k, v in data.items() if k not in {"title", "intent", "product"}},
    )


def _print(snap) -> None:
    print(
        json.dumps(
            {
                "mission_id": snap.mission_id,
                "state": snap.state.value,
                "pcp_hash": snap.pcp_hash,
                "current_artifact_id": snap.current_artifact_id,
                "approved_artifact_id": snap.approved_artifact_id,
                "owner_decision": snap.owner_decision,
                "blocked_reason": snap.blocked_reason,
                "candidates": [
                    {
                        "candidate_id": c.candidate_id,
                        "artifact_id": c.artifact_id,
                        "review": c.review_verdict,
                        "qa": c.qa_verdict,
                        "status": c.status,
                    }
                    for c in snap.candidates
                ],
            },
            indent=2,
        )
    )


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
    p_admit = sub.add_parser("admit-pcp", help="Gate 1: admit an already-approved PCP")
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
    args = parser.parse_args(argv)
    ctl = _controller(simulated=args.simulated)
    try:
        if args.cmd == "admit-pcp":
            _print(ctl.admit_pcp(_pcp_from_file(args.pcp_json)))
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
        else:
            parser.error(args.cmd)
    except (GateError, InvariantError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
