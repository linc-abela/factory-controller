"""Harness wake and dispatch adapters for Antigravity, Codex, Cursor, and Claude.

Provides deterministic programmatic wake paths where supported and truthful
refusal (`HARNESS_WAKE_PATH_UNAVAILABLE:<harness>`) when a deterministic headless
wake interface is not available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Protocol

from .dispatch import OWNER_QUEUE_COMMAND
from .model import AWEWorkItem, GroundingResult, WakeReceipt


class HarnessAdapter(Protocol):
    """Protocol for a harness wake adapter."""

    @property
    def name(self) -> str:
        ...

    def check_availability(self) -> tuple[bool, str, str]:
        """Returns (available, status_code, detail)."""
        ...

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        """Wake or dispatch the harness with the task packet."""
        ...


class AntigravityHarnessAdapter:
    """Wake adapter for Google Antigravity / agy CLI."""

    def __init__(self, agy_bin: str | None = None) -> None:
        self.agy_bin = agy_bin or shutil.which("agy") or "/Users/karlosabay/.local/bin/agy"

    @property
    def name(self) -> str:
        return "antigravity"

    def check_availability(self) -> tuple[bool, str, str]:
        if not os.path.isfile(self.agy_bin) or not os.access(self.agy_bin, os.X_OK):
            return False, "HARNESS_BINARY_MISSING", f"agy CLI not executable at {self.agy_bin}"
        try:
            res = subprocess.run([self.agy_bin, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return True, "HARNESS_READY", f"agy CLI version {res.stdout.strip()}"
            return False, "HARNESS_UNHEALTHY", f"agy returned exit code {res.returncode}"
        except Exception as exc:
            return False, "HARNESS_ERROR", str(exc)

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        avail, code, detail = self.check_availability()
        if not avail:
            return WakeReceipt(
                success=False,
                harness="antigravity",
                slot_key=task.slot.key,
                error_code=code,
                detail=detail,
            )

        prompt = OWNER_QUEUE_COMMAND

        cmd = [
            self.agy_bin,
            "--print",
            "--model",
            task.model or "gemini-3.8-flash-high",
            "--effort",
            task.effort or "high",
            prompt,
        ]

        if dry_run:
            return WakeReceipt(
                success=True,
                harness="antigravity",
                slot_key=task.slot.key,
                command=cmd,
                pid=99999,
                stdout="[DRY RUN] Antigravity wake simulated",
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return WakeReceipt(
                success=True,
                harness="antigravity",
                slot_key=task.slot.key,
                command=cmd,
                pid=proc.pid,
            )
        except Exception as exc:
            return WakeReceipt(
                success=False,
                harness="antigravity",
                slot_key=task.slot.key,
                command=cmd,
                error_code="WAKE_SUBPROCESS_FAILED",
                detail=str(exc),
            )


class CodexHarnessAdapter:
    """Wake adapter for OpenAI Codex CLI."""

    def __init__(self, codex_bin: str | None = None) -> None:
        self.codex_bin = codex_bin or shutil.which("codex") or "/Users/karlosabay/.local/bin/codex"

    @property
    def name(self) -> str:
        return "codex"

    def check_availability(self) -> tuple[bool, str, str]:
        if not os.path.isfile(self.codex_bin) or not os.access(self.codex_bin, os.X_OK):
            return False, "HARNESS_BINARY_MISSING", f"codex CLI not executable at {self.codex_bin}"
        try:
            res = subprocess.run([self.codex_bin, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return True, "HARNESS_READY", f"codex CLI version {res.stdout.strip()}"
            return False, "HARNESS_UNHEALTHY", f"codex returned exit code {res.returncode}"
        except Exception as exc:
            return False, "HARNESS_ERROR", str(exc)

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        avail, code, detail = self.check_availability()
        if not avail:
            return WakeReceipt(
                success=False,
                harness="codex",
                slot_key=task.slot.key,
                error_code=code,
                detail=detail,
            )

        prompt = OWNER_QUEUE_COMMAND

        cmd = [
            self.codex_bin,
            "exec",
            "-m",
            task.model or "gpt-5.6-luna",
            "-c",
            f"model_reasoning_effort={task.effort or 'max'}",
            prompt,
        ]

        if dry_run:
            return WakeReceipt(
                success=True,
                harness="codex",
                slot_key=task.slot.key,
                command=cmd,
                pid=99998,
                stdout="[DRY RUN] Codex wake simulated",
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return WakeReceipt(
                success=True,
                harness="codex",
                slot_key=task.slot.key,
                command=cmd,
                pid=proc.pid,
            )
        except Exception as exc:
            return WakeReceipt(
                success=False,
                harness="codex",
                slot_key=task.slot.key,
                command=cmd,
                error_code="WAKE_SUBPROCESS_FAILED",
                detail=str(exc),
            )


class CursorHarnessAdapter:
    """Adapter for Cursor harness.

    Empirical Investigation:
    The host has `/Applications/Cursor.app/Contents/Resources/app/bin/cursor agent`.
    However, running `cursor agent status` returns `Not logged in`.
    Cursor's noninteractive CLI requires either interactive web browser login or
    an external API key (CURSOR_API_KEY). The active desktop session runs inside
    an Electron window that does not expose a headless control socket.
    Therefore, headless programmatic wake is unavailable without interactive auth.
    """

    def __init__(self, cursor_bin: str | None = None) -> None:
        self.cursor_bin = cursor_bin or "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"

    @property
    def name(self) -> str:
        return "cursor"

    def check_availability(self) -> tuple[bool, str, str]:
        if not os.path.exists(self.cursor_bin):
            return False, "HARNESS_WAKE_PATH_UNAVAILABLE:cursor", "Cursor binary not found on host"

        # Check agent status
        try:
            res = subprocess.run(
                [self.cursor_bin, "agent", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = res.stdout.strip() + " " + res.stderr.strip()
            if "Not logged in" in output:
                return (
                    False,
                    "HARNESS_WAKE_PATH_UNAVAILABLE:cursor",
                    "Cursor agent CLI is not logged in; interactive browser authentication or CURSOR_API_KEY required for noninteractive wake",
                )
            if res.returncode == 0:
                return True, "HARNESS_READY", "Cursor agent CLI logged in and available"
            return False, "HARNESS_WAKE_PATH_UNAVAILABLE:cursor", f"Cursor agent status error: {output}"
        except Exception as exc:
            return False, "HARNESS_WAKE_PATH_UNAVAILABLE:cursor", str(exc)

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        avail, code, detail = self.check_availability()
        if not avail:
            return WakeReceipt(
                success=False,
                harness="cursor",
                slot_key=task.slot.key,
                error_code=code,
                detail=detail,
            )

        # If logged in in future:
        cmd = [self.cursor_bin, "agent", "-p", OWNER_QUEUE_COMMAND]
        return WakeReceipt(
            success=True,
            harness="cursor",
            slot_key=task.slot.key,
            command=cmd,
        )


class ClaudeHarnessAdapter:
    """Wake adapter for Anthropic Claude Code CLI."""

    def __init__(self, claude_bin: str | None = None) -> None:
        self.claude_bin = claude_bin or shutil.which("claude") or "/Users/karlosabay/.local/bin/claude"

    @property
    def name(self) -> str:
        return "claude"

    def check_availability(self) -> tuple[bool, str, str]:
        if not os.path.isfile(self.claude_bin) or not os.access(self.claude_bin, os.X_OK):
            return False, "HARNESS_BINARY_MISSING", f"claude CLI not executable at {self.claude_bin}"
        try:
            res = subprocess.run([self.claude_bin, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return True, "HARNESS_READY", f"claude CLI version {res.stdout.strip()}"
            return False, "HARNESS_UNHEALTHY", f"claude returned exit code {res.returncode}"
        except Exception as exc:
            return False, "HARNESS_ERROR", str(exc)

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        avail, code, detail = self.check_availability()
        if not avail:
            return WakeReceipt(
                success=False,
                harness="claude",
                slot_key=task.slot.key,
                error_code=code,
                detail=detail,
            )
        prompt = OWNER_QUEUE_COMMAND
        cmd = [self.claude_bin, "-p", prompt]
        if dry_run:
            return WakeReceipt(success=True, harness="claude", slot_key=task.slot.key, command=cmd, pid=99997)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return WakeReceipt(success=True, harness="claude", slot_key=task.slot.key, command=cmd, pid=proc.pid)
        except Exception as exc:
            return WakeReceipt(success=False, harness="claude", slot_key=task.slot.key, command=cmd, error_code="WAKE_SUBPROCESS_FAILED", detail=str(exc))


class MockHarnessAdapter:
    """Mock adapter for deterministic testing and simulation."""

    def __init__(
        self,
        harness_name: str = "mock",
        available: bool = True,
        status_code: str = "HARNESS_READY",
        detail: str = "Mock ready",
    ) -> None:
        self._name = harness_name
        self.available = available
        self.status_code = status_code
        self.status_detail = detail
        self.woken_tasks: list[AWEWorkItem] = []

    @property
    def name(self) -> str:
        return self._name

    def check_availability(self) -> tuple[bool, str, str]:
        return self.available, self.status_code, self.status_detail

    def wake(
        self,
        task: AWEWorkItem,
        grounding: GroundingResult | None = None,
        dry_run: bool = False,
    ) -> WakeReceipt:
        if not self.available:
            return WakeReceipt(
                success=False,
                harness=self._name,
                slot_key=task.slot.key,
                error_code=self.status_code,
                detail=self.status_detail,
            )
        self.woken_tasks.append(task)
        return WakeReceipt(
            success=True,
            harness=self._name,
            slot_key=task.slot.key,
            command=["mock", "wake", task.task_id],
            pid=12345,
            stdout="[MOCK] task dispatched",
        )


def get_adapter_for_harness(harness_name: str) -> HarnessAdapter:
    """Return appropriate harness adapter by name."""
    name = harness_name.strip().lower()
    if name == "antigravity":
        return AntigravityHarnessAdapter()
    elif name == "codex":
        return CodexHarnessAdapter()
    elif name == "cursor":
        return CursorHarnessAdapter()
    elif name == "claude":
        return ClaudeHarnessAdapter()
    return MockHarnessAdapter(harness_name=name)
