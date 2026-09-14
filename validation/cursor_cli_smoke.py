#!/usr/bin/env python3
"""Bounded real Cursor CLI smoke for SFV2-006. Never prints secrets."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.models import MissionContext, WorkItem


def main() -> int:
    binary = shutil.which("agent")
    version = "unavailable"
    if binary:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip() or "unknown"
    executor = CursorCLIExecutor()
    auth_mode = executor.auth_mode()
    if binary is None or not executor.credentials_available():
        print(
            json.dumps(
                {
                    "status": "OWNER_ACTION_REQUIRED",
                    "reason": "cursor CLI login or CURSOR_API_KEY required",
                    "cli_path": binary,
                    "cli_version": version,
                    "auth_mode": auth_mode,
                    "requested_model": executor.requested_model,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="sfv2-006-cursor-smoke-") as raw:
        workspace = Path(raw)
        target = workspace / "hello.txt"
        ctx = MissionContext(
            mission_id="msn-sfv2-006-cursor-smoke",
            lineage_id="msn-sfv2-006-cursor-smoke",
            pcp_hash="0" * 64,
            pcp={"product": {"objective": "create hello.txt containing hello"}},
            workspace_path=str(workspace),
        )
        work = WorkItem(
            objective=(
                "Create hello.txt in this workspace containing exactly the "
                "text hello. Then print JSON with candidate_id, "
                "source_revision, artifact_hash, artifact_uri."
            )
        )
        result = executor.implement(ctx, work)
        evidence = {
            "status": "PASS" if not result.blocked and target.is_file() else "FAIL",
            "blocked": result.blocked,
            "reason": result.reason,
            "cli_path": binary,
            "cli_version": version,
            "auth_mode": auth_mode,
            "requested_model": executor.requested_model,
            "command_shape": [
                "agent",
                "-p",
                "<prompt>",
                "--output-format",
                "json",
                "--workspace",
                "<sandbox>",
                "--trust",
                "--sandbox",
                "enabled",
                "--model",
                executor.requested_model,
            ],
            "candidate": None if result.candidate is None else result.candidate.as_dict(),
            "artifact_exists": target.is_file(),
            "artifact_text": target.read_text(encoding="utf-8") if target.is_file() else "",
            "provenance": executor.last_provenance,
            "simulated": result.simulated,
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
