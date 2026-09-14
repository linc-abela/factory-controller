#!/usr/bin/env python3
"""Live Owner-approved PCP event -> intake -> Hermes -> Cursor CLI, no manual tick."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.adapters.simulated import ScriptedDistributor
from factory_v2.canonical import fixture_path
from factory_v2.machine import Controller
from factory_v2.models import CandidateIdentity, MissionContext, Verdict
from factory_v2.serve import serve_forever
from factory_v2.store import Store


class PassThroughVerifier:
    """Live intake smoke stops at Owner Gate 2 without a second Hermes campaign."""

    name = "Antigravity"
    harness_mode = "simulated"

    def review(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        del ctx
        return Verdict(
            kind="review",
            candidate=candidate,
            passed=True,
            harness_mode="simulated",
            verifier_identity="antigravity:reviewer-1",
        )

    def qa(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        del ctx
        return Verdict(
            kind="qa",
            candidate=candidate,
            passed=True,
            harness_mode="simulated",
            verifier_identity="antigravity:qa-1",
        )


def _server() -> int:
    executor = CursorCLIExecutor()
    manager = NousHermesAdapter(executor=executor)
    home = Path(os.environ["FACTORY_V2_HOME"])
    ctl = Controller(
        Store(home / "ledger.sqlite"),
        manager,
        PassThroughVerifier(),
        ScriptedDistributor(),
        home / "sandboxes",
    )
    serve_forever(ctl, "127.0.0.1", int(os.environ["FACTORY_V2_INTAKE_PORT"]))
    return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.1)
    raise RuntimeError("intake server did not become healthy")


def main() -> int:
    if sys.argv[1:] == ["--server"]:
        return _server()
    executor = CursorCLIExecutor()
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
    pcp["product"]["objective"] = (
        "Create hello.txt in the bounded workspace containing exactly the text hello."
    )
    pcp["product"]["user_outcome"] = "hello.txt exists and contains hello."
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="sfv2-006-pcp-intake-") as raw:
        home = Path(raw)
        env = dict(os.environ)
        env["FACTORY_V2_HOME"] = str(home)
        env["FACTORY_V2_INTAKE_PORT"] = str(port)
        env["PYTHONPATH"] = str(ROOT)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--server"],
            cwd=str(ROOT),
            env=env,
        )
        try:
            _wait_health(port)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/pcp",
                data=json.dumps(pcp).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3600) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                status_code = resp.status
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        payload = {
            "status": "PASS"
            if status_code == 200
            and body.get("current")
            and body.get("state") == "OWNER_VALIDATION"
            else "FAIL",
            "http_status": status_code,
            "manual_tick": False,
            "manual_admit_pcp": False,
            "intake": "POST /v1/pcp",
            "mission": body,
            "executor_name": "Cursor CLI",
            "auth_mode": executor.auth_mode(),
            "requested_model": executor.requested_model,
            "mock_executor": False,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
