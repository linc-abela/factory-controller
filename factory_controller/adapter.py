"""JSON subprocess seam for bridge, verification, and Evidence Core adapters."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Sequence

from .engine import RetryableFailure


#: How long one step may take before the Controller stops waiting for it.
#:
#: This has to be *longer* than anything the execution layer is allowed to do,
#: because the Controller giving up is not the same fact as the work failing.
#: At 300 seconds it was shorter than both: a provider profile declares
#: ``timeout_seconds: 3600``, and a mission may declare three acceptance gates
#: at ``gate_timeout_seconds`` 1800 each.  SF-157 measured what that costs.  A
#: dogfood mission ran for five minutes, the adapter raised
#: ``ADAPTER_UNAVAILABLE`` on its own timeout while the provider was still
#: working, the retry was refused ``LANE_ALREADY_ACTIVE`` by the lane its own
#: first attempt still held, and the slot's remaining attempts were spent on
#: ``PROJECT_CAPACITY_EXHAUSTED`` within five seconds -- three attempts gone,
#: none of them a statement about the work, and a lane left `uncertain`.  The
#: only reason DF-1 ever passed is that it happened to finish inside the five
#: minutes.
STEP_TIMEOUT_SECONDS = 7200.0


class JsonProcessAdapter:
    def __init__(self, command: Sequence[str], *,
                 timeout_seconds: float = STEP_TIMEOUT_SECONDS) -> None:
        if not command:
            raise ValueError("adapter command is required")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        request = json.dumps({"step": step, "operation_key": operation_key, "input": value}, sort_keys=True)
        try:
            completed = subprocess.run(
                self.command, input=request, text=True, capture_output=True,
                timeout=self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RetryableFailure(f"ADAPTER_UNAVAILABLE: {exc}") from exc
        if completed.returncode != 0:
            raise RetryableFailure(f"ADAPTER_EXIT_{completed.returncode}: {completed.stderr.strip()}")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RetryableFailure("ADAPTER_INVALID_JSON") from exc
        if not isinstance(response, dict):
            raise RetryableFailure("ADAPTER_INVALID_RESPONSE")
        return response


@dataclass(frozen=True)
class HostCommandResult:
    """The small result shape needed by the native host lifecycle seam."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_host_command(command: Sequence[str], *, cwd: str | None = None,
                     input_text: str | None = None,
                     timeout_seconds: float = 300) -> HostCommandResult:
    """Run one caller-supplied argv without a shell.

    The lifecycle coordinator is the policy layer; this function is only the
    existing process boundary. Keeping host execution here preserves the
    Controller's provider-neutral core and gives tests a single replacement
    point for every host fact and mutation.
    """

    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("host command is a non-empty argument array")
    try:
        completed = subprocess.run(
            tuple(command), cwd=cwd, input=input_text, text=True,
            capture_output=True, timeout=timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HostCommandResult(127, "", str(exc))
    return HostCommandResult(completed.returncode, completed.stdout,
                             completed.stderr)


def commit_new_repository(root: str, message: str) -> str:
    """Create one local commit so a derived product has a baseline identity.

    Git object resolution stays outside this package; this only records files
    the Factory itself just wrote.
    """

    from pathlib import Path
    target = Path(root)
    commands = (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "factory@software-factory.invalid"),
        ("git", "config", "user.name", "Software Factory"),
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", message),
        ("git", "log", "-1", "--format=%H"),
    )
    sha = ""
    for command in commands:
        result = run_host_command(command, cwd=str(target))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or "git %s failed" % command[1])
        sha = result.stdout.strip()
    if len(sha) != 40:
        raise RuntimeError("bootstrap commit is not a full SHA")
    return sha


def publish_new_repository(root: str, name: str) -> None:
    """Host the Factory-derived bootstrap so Bridge can resolve the project.

    The application is still unimplemented; this only publishes the stub tree.
    """

    viewed = run_host_command(("gh", "repo", "view", name, "--json", "name"))
    if viewed.returncode != 0:
        created = run_host_command(
            ("gh", "repo", "create", name, "--private", "--source", root,
             "--remote", "origin", "--push"),
            cwd=root,
        )
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or created.stdout.strip()
                               or "hosted repository create failed")
        return
    origin = run_host_command(
        ("git", "config", "--get", "remote.origin.url"), cwd=root)
    if origin.returncode != 0 or not origin.stdout.strip():
        added = run_host_command(
            ("git", "remote", "add", "origin",
             "https://github.com/%s.git" % name),
            cwd=root)
        if added.returncode != 0:
            raise RuntimeError(added.stderr.strip() or added.stdout.strip()
                               or "origin could not be recorded")
    pushed = run_host_command(("git", "push", "-u", "origin", "HEAD"), cwd=root)
    if pushed.returncode != 0:
        raise RuntimeError(pushed.stderr.strip() or pushed.stdout.strip()
                           or "hosted repository push failed")
