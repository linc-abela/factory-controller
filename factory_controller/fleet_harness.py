"""Harness adapter: run a selected profile and normalize provider errors.

Hermes consumes only AVAILABLE / QUOTA_EXHAUSTED / TEMPORARILY_UNAVAILABLE /
COMPLETED / FAILED. Provider-specific strings stay in this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capability_map import Profile

_AG_E2E_SCHEMA = (
    '{"type":"object","properties":{'
    '"result":{"type":"string"},'
    '"candidate_head":{"type":"string"},'
    '"scenarios":{"type":"array","items":{"type":"string"}},'
    '"defects":{"type":"array","items":{"type":"string"}},'
    '"detail":{"type":"string"},'
    '"mission_key":{"type":"string"},'
    '"run_id":{"type":"string"}'
    '}}'
)
AVAILABLE = "AVAILABLE"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota exceeded",
    "quota_exceeded",
    "rate limit exceeded",
    "usage limit reached",
    "you've hit your usage limit",
    "hit your usage limit",
    "context length exceeded",
    "5h limit",
    "5-hour limit",
    "5 hour limit",
)
_UNAVAILABLE_MARKERS = (
    "not logged in",
    "authentication required",
    "unauthorized",
    "harness_binary_missing",
    "harness_wake_path_unavailable",
)


@dataclass(frozen=True)
class HarnessReceipt:
    status: str
    harness: str
    model: str
    effort: str
    returncode: int = 0
    detail: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "harness": self.harness,
            "model": self.model,
            "effort": self.effort,
            "returncode": self.returncode,
            "detail": self.detail,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


class FleetHarness:
    """Wake Codex or Cursor for a resolver-selected profile."""

    def run(self, profile: Profile, prompt: str, cwd: Path) -> HarnessReceipt:
        if profile.harness == "codex":
            return self._codex(profile, prompt, cwd)
        if profile.harness == "cursor":
            return self._cursor(profile, prompt, cwd)
        if profile.harness == "antigravity":
            return self._antigravity(profile, prompt, cwd)
        return HarnessReceipt(
            status=TEMPORARILY_UNAVAILABLE,
            harness=profile.harness,
            model=profile.model,
            effort=profile.effort,
            detail="HARNESS_UNSUPPORTED",
        )

    def _codex(self, profile: Profile, prompt: str, cwd: Path) -> HarnessReceipt:
        binary = shutil.which("codex")
        if not binary:
            return self._receipt(
                profile, TEMPORARILY_UNAVAILABLE, -1, "", "",
                "HARNESS_BINARY_MISSING")
        cmd = [
            binary, "exec",
            "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", profile.model, "-c",
            "model_reasoning_effort=%s" % profile.effort, prompt,
        ]
        return self._spawn(profile, cmd, cwd)

    def _antigravity(self, profile: Profile, prompt: str, cwd: Path) -> HarnessReceipt:
        binary = shutil.which("agy") or "/Users/karlosabay/.local/bin/agy"
        if not os.path.exists(binary):
            return self._receipt(
                profile, TEMPORARILY_UNAVAILABLE, -1, "", "",
                "HARNESS_BINARY_MISSING")
        cmd = [
            binary, "--print",
            "--model", provider_model_id(profile),
            "--effort", profile.effort,
            "--dangerously-skip-permissions",
            "--add-dir", str(cwd),
            "--print-timeout", "15m0s",
            "--output-format", "json",
            "--json-schema", _AG_E2E_SCHEMA,
            prompt,
        ]
        return self._spawn(profile, cmd, cwd)

    def _cursor(self, profile: Profile, prompt: str, cwd: Path) -> HarnessReceipt:
        binary = shutil.which("cursor") or (
            "/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
        if not os.path.exists(binary):
            return self._receipt(
                profile, TEMPORARILY_UNAVAILABLE, -1, "", "",
                "HARNESS_WAKE_PATH_UNAVAILABLE:cursor")
        model = profile.model if profile.effort != "runtime" else "auto"
        cmd = [binary, "agent", "-p", "--force", "--model", model, prompt]
        return self._spawn(profile, cmd, cwd)

    def _spawn(self, profile: Profile, cmd: list[str], cwd: Path) -> HarnessReceipt:
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=2400)
        except subprocess.TimeoutExpired as exc:
            return self._receipt(
                profile, TEMPORARILY_UNAVAILABLE, -1, "", str(exc)[:500],
                "TIMEOUT")
        except OSError as exc:
            return self._receipt(
                profile, TEMPORARILY_UNAVAILABLE, -1, "", str(exc)[:500],
                "WAKE_SUBPROCESS_FAILED")
        return self._receipt(
            profile, "", proc.returncode, proc.stdout or "", proc.stderr or "", "")

    def _receipt(
        self, profile: Profile, status: str, returncode: int,
        stdout: str, stderr: str, detail: str,
    ) -> HarnessReceipt:
        text = "%s\n%s\n%s" % (stdout, stderr, detail)
        if not status:
            status = classify_provider_output(text, returncode)
        return HarnessReceipt(
            status=status,
            harness=profile.harness,
            model=profile.model,
            effort=profile.effort,
            returncode=returncode,
            detail=detail or status,
            stdout_tail=stdout[-4000:],
            stderr_tail=stderr[-2000:],
        )


def provider_model_id(profile: Profile) -> str:
    """Translate a mapping profile onto the harness binary's model id.

    Routing still names a capability. This is adapter-only: Gemini 3.8 Medium
    on Antigravity is `gemini-3.8-flash-medium`.
    """

    model = profile.model
    if profile.harness == "antigravity" and model.startswith("gemini-"):
        if "flash" not in model and "pro" not in model:
            return "%s-flash-%s" % (model, profile.effort)
    return model


def classify_provider_output(text: str, returncode: int = 0) -> str:
    """Map provider stderr/stdout onto a generic availability status."""

    lowered = text.lower()
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return QUOTA_EXHAUSTED
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
        return TEMPORARILY_UNAVAILABLE
    if returncode == 0:
        return COMPLETED
    return FAILED
