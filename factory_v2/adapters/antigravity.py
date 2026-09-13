from __future__ import annotations

import os
import shutil
import subprocess

from factory_v2.models import DistributionResult, MissionContext, Verdict


class AntigravityVerifier:
    """Independent review + QA/E2E. Separate verdicts, same Antigravity family."""

    name = "Antigravity"
    harness_mode = "real"

    def __init__(self, *, env: dict[str, str] | None = None, binary: str = "antigravity"):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary

    def review(self, ctx: MissionContext, artifact_id: str) -> Verdict:
        return self._run("review", ctx, artifact_id)

    def qa(self, ctx: MissionContext, artifact_id: str) -> Verdict:
        return self._run("qa", ctx, artifact_id)

    def _run(self, kind: str, ctx: MissionContext, artifact_id: str) -> Verdict:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return Verdict(
                kind=kind,
                artifact_id=artifact_id,
                passed=False,
                defects=(f"antigravity binary unavailable for {kind}",),
                harness_mode="real",
            )
        try:
            proc = subprocess.run(
                [
                    binary,
                    kind,
                    "--artifact",
                    artifact_id,
                    "--workspace",
                    ctx.workspace_path,
                    "--mission",
                    ctx.mission_id,
                ],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=3600,
                check=False,
            )
        except OSError as exc:
            return Verdict(
                kind=kind,
                artifact_id=artifact_id,
                passed=False,
                defects=(f"antigravity {kind} launch failed: {exc}",),
                harness_mode="real",
            )
        if proc.returncode != 0:
            return Verdict(
                kind=kind,
                artifact_id=artifact_id,
                passed=False,
                defects=(proc.stderr.strip() or f"antigravity {kind} failed",),
                harness_mode="real",
            )
        return Verdict(
            kind=kind,
            artifact_id=artifact_id,
            passed=True,
            harness_mode="real",
        )


class AntigravityDistributor:
    """DistributionExecutor targeting the Antigravity Production profile."""

    name = "Antigravity"
    harness_mode = "real"
    profile = "production"

    def __init__(self, *, env: dict[str, str] | None = None, binary: str = "antigravity"):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary

    def distribute(self, artifact_id: str, mission_id: str) -> DistributionResult:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            raise RuntimeError("antigravity production binary unavailable")
        proc = subprocess.run(
            [
                binary,
                "distribute",
                "--profile",
                "production",
                "--artifact",
                artifact_id,
                "--mission",
                mission_id,
            ],
            capture_output=True,
            text=True,
            env=self._env,
            timeout=3600,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "antigravity distribute failed")
        return DistributionResult(
            artifact_id=artifact_id,
            harness_mode="real",
            receipt=proc.stdout.strip() or f"distributed:{artifact_id}",
        )
