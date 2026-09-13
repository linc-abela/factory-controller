#!/usr/bin/env python3
"""Real Controller -> Nous Hermes -> CursorCLIExecutor smoke for SFV2-006."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_v2.adapters.antigravity import AntigravityDistributor, AntigravityVerifier
from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.canonical import fixture_path
from factory_v2.machine import Controller
from factory_v2.store import Store


def main() -> int:
    executor = CursorCLIExecutor()
    manager = NousHermesAdapter(executor=executor)
    if shutil.which("hermes") is None:
        print(json.dumps({"status": "FAIL", "reason": "hermes runtime unavailable"}, indent=2))
        return 1
    if not executor.credentials_available():
        print(
            json.dumps(
                {
                    "status": "OWNER_ACTION_REQUIRED",
                    "reason": "cursor CLI login or CURSOR_API_KEY required",
                    "auth_mode": executor.auth_mode(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    pcp = json.loads(fixture_path("valid-approved-pcp.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="sfv2-006-hermes-cursor-") as raw:
        root = Path(raw)
        ctl = Controller(
            Store(root / "ledger.sqlite"),
            manager,
            AntigravityVerifier(),
            AntigravityDistributor(),
            root / "sandboxes",
        )
        snap = ctl.admit_pcp(pcp)
        after = ctl.tick(snap.mission_id)
        payload = {
            "status": "PASS" if after.current is not None and after.blocked_reason is None else "FAIL",
            "mission_id": after.mission_id,
            "state": after.state.value,
            "blocked_reason": after.blocked_reason,
            "candidate": None if after.current is None else after.current.as_dict(),
            "executor_name": manager._executor_name(),
            "auth_mode": executor.auth_mode(),
            "requested_model": executor.requested_model,
            "provenance": executor.last_provenance,
            "mock_executor": False,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
