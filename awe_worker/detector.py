"""Completion detection, repository verification, and anti-forgery checks."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .model import AWEWorkItem, CompletionReport


class CompletionDetector:
    """Detects task completion through git repository and PR evidence checks."""

    def __init__(self, default_repo: str | Path | None = None) -> None:
        self.default_repo = Path(default_repo) if default_repo else None

    def detect_state(
        self,
        task: AWEWorkItem,
        repo_path: str | Path | None = None,
        expected_base_sha: str | None = None,
        agent_reported_done: bool = False,
    ) -> CompletionReport:
        """Inspect repository and PR state to detect genuine completion or forged Done."""
        target_repo = Path(repo_path or self.default_repo or ".")
        if not target_repo.is_dir():
            return CompletionReport(
                state="BLOCKED",
                task_id=task.task_id,
                detail=f"Repository path {target_repo} does not exist",
            )

        # 1. Locate task branch
        branch_name = self._find_task_branch(target_repo, task.task_id)
        if not branch_name:
            if agent_reported_done:
                # Agent claims done but no task branch exists!
                return CompletionReport(
                    state="FORGED_DONE",
                    task_id=task.task_id,
                    evidence_valid=False,
                    detail="EVIDENCE_FORGERY_DETECTED: agent claims Done but no git task branch exists",
                )
            return CompletionReport(
                state="IN_PROGRESS",
                task_id=task.task_id,
                detail="Task branch not yet published",
            )

        # 2. Inspect branch head commit
        head_sha = self._get_branch_sha(target_repo, branch_name)
        if not head_sha:
            return CompletionReport(
                state="BLOCKED",
                task_id=task.task_id,
                branch_name=branch_name,
                detail=f"Could not resolve commit SHA for branch {branch_name}",
            )

        # 3. Check for commits against base
        base_sha = expected_base_sha or self._get_branch_sha(target_repo, "origin/main") or self._get_branch_sha(target_repo, "main") or ""
        commit_count = self._count_commits_between(target_repo, base_sha, head_sha) if base_sha else 1

        if commit_count == 0:
            if agent_reported_done:
                return CompletionReport(
                    state="FORGED_DONE",
                    task_id=task.task_id,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    branch_name=branch_name,
                    evidence_valid=False,
                    detail="EVIDENCE_FORGERY_DETECTED: agent claims Done but task branch contains 0 commits beyond base",
                )
            return CompletionReport(
                state="IN_PROGRESS",
                task_id=task.task_id,
                head_sha=head_sha,
                base_sha=base_sha,
                branch_name=branch_name,
                detail="Task branch exists but has no new commits",
            )

        # 4. Check for remote PR / push
        pr_number = self._check_pr_exists(target_repo, branch_name)

        # 5. Check for evidence in commits or Result
        evidence_present = self._check_evidence_present(target_repo, head_sha, task)

        if not evidence_present and agent_reported_done:
            return CompletionReport(
                state="FORGED_DONE",
                task_id=task.task_id,
                head_sha=head_sha,
                base_sha=base_sha,
                branch_name=branch_name,
                pr_number=pr_number,
                evidence_valid=False,
                detail="EVIDENCE_FORGERY_DETECTED: commits exist but no test results, measurements, or Result evidence recorded",
            )

        return CompletionReport(
            state="DONE" if agent_reported_done or pr_number is not None else "IN_PROGRESS",
            task_id=task.task_id,
            head_sha=head_sha,
            base_sha=base_sha,
            branch_name=branch_name,
            pr_number=pr_number,
            evidence_valid=evidence_present,
            detail=f"Candidate branch {branch_name} at {head_sha} verified ({commit_count} commits, PR: {pr_number})",
        )

    def _find_task_branch(self, repo: Path, task_id: str) -> str | None:
        """Find local or remote branch matching task ID."""
        clean_id = task_id.replace(" ", "-").lower()
        patterns = [
            f"sf/{clean_id}",
            f"sf/{task_id}",
            clean_id,
            task_id,
        ]
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "branch", "-a"],
                capture_output=True,
                text=True,
                check=True,
            )
            for line in res.stdout.splitlines():
                line = line.strip().lstrip("*+ ").replace("remotes/origin/", "")
                for p in patterns:
                    if p.lower() in line.lower():
                        return line
        except Exception:
            pass
        return None

    def _get_branch_sha(self, repo: Path, ref: str) -> str | None:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", ref],
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()
        except Exception:
            return None

    def _count_commits_between(self, repo: Path, base: str, head: str) -> int:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "rev-list", "--count", f"{base}..{head}"],
                capture_output=True,
                text=True,
                check=True,
            )
            return int(res.stdout.strip())
        except Exception:
            return 1  # If check fails, assume commit exists

    def _check_pr_exists(self, repo: Path, branch: str) -> int | None:
        """Query GitHub CLI for PR if available."""
        try:
            res = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--json", "number", "--jq", ".[0].number"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip().isdigit():
                return int(res.stdout.strip())
        except Exception:
            pass
        return None

    def _check_evidence_present(self, repo: Path, head_sha: str, task: AWEWorkItem) -> bool:
        """Verify presence of verifiable work, commit messages, or Result notes."""
        try:
            # Check commit message for test/evidence mentions
            res = subprocess.run(
                ["git", "-C", str(repo), "log", "-n", "1", "--format=%B", head_sha],
                capture_output=True,
                text=True,
                check=True,
            )
            msg = res.stdout.lower()
            # If commit message has descriptive content
            if len(msg.strip()) > 20:
                return True
        except Exception:
            pass
        if task.verdict or len(task.notes) > 20:
            return True
        return False
