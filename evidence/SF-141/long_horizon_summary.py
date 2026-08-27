"""Re-derive the Stage-9 long-horizon numbers from the simulation itself.

Run from the repository root:

    python3 -m evidence.SF-141.long_horizon_summary   # not importable; use:
    python3 evidence/SF-141/long_horizon_summary.py

Writes ``long_horizon_summary.json`` beside this file.  Everything in it is
counted from durable state after the run, not asserted by the suite, so the two
can disagree -- which is the point of keeping them separate.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.test_stage9_long_horizon import (  # noqa: E402
    LongHorizonSimulation, OUTAGE_HOURS, PROJECTS, TICKS)


def main() -> int:
    LongHorizonSimulation.setUpClass()
    try:
        trace = LongHorizonSimulation.trace
        plane = LongHorizonSimulation.plane
        store = LongHorizonSimulation.store
        layer = LongHorizonSimulation.layer
        missions = plane._mission_lines()
        completed = [row for row in missions if row["state"] == "completed"]
        summary = {
            "virtual_hours": len(trace) // 4,
            "cycles": len(trace),
            "cycles_recorded": len(plane.cycles(limit=1_000_000)),
            "projects": sorted(PROJECTS),
            "missions": len(missions),
            "mission_states": dict(Counter(row["state"] for row in missions)),
            "completed_by_project": dict(Counter(row["project_id"]
                                                 for row in completed)),
            "provider_invocations": len(layer.dispatch_keys),
            "distinct_provider_invocations": len(set(layer.dispatch_keys)),
            "duplicate_irreversible_effects":
                len(layer.dispatch_keys) - len(set(layer.dispatch_keys)),
            "promotions": sum(len(entry["promoted"]) for entry in trace),
            "mission_advances": sum(len(entry["advanced"]) for entry in trace),
            "idle_cycles": sum(1 for entry in trace if entry["outcome"] == "idle"),
            "outcome_classification": dict(Counter(
                row["classification"] for entry in trace
                for row in entry["advanced"])),
            "cycle_refusals": dict(Counter(reason for entry in trace
                                           for reason in entry["refused"])),
            "outage_hours": sorted(OUTAGE_HOURS),
            "deployments_total": _deployments(store),
            "deployments_caused_by_supervisor": _deployments(store)
                - len(LongHorizonSimulation.owner_deployments),
            "delta_provider_spend": store.portfolio_economics(
                "delta")["projects"][0]["provider_spend"],
            "final_control_state": plane.control()["state"],
        }
    finally:
        LongHorizonSimulation.tearDownClass()
    out = pathlib.Path(__file__).with_name("long_horizon_summary.json")
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _deployments(store) -> int:
    with store.transaction() as db:
        return db.execute("SELECT COUNT(*) AS n FROM deployments").fetchone()["n"]


if __name__ == "__main__":
    raise SystemExit(main())
