"""JSON subprocess seam for bridge, verification, and Evidence Core adapters."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Sequence

from .engine import RetryableFailure


class JsonProcessAdapter:
    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 300) -> None:
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

