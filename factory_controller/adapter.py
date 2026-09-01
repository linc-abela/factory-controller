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
