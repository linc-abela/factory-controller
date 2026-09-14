from __future__ import annotations

import os
import shutil
import subprocess

from factory_v2.models import CandidateIdentity, DistributionResult, MissionContext, Verdict


class AntigravityVerifier:
    """Independent review + QA/E2E. Separate verdicts, same Antigravity family."""

    name = "Antigravity"
    harness_mode = "real"

    def __init__(self, *, env: dict[str, str] | None = None, binary: str = "antigravity"):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary

    def review(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        return self._run("review", ctx, candidate)

    def qa(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        return self._run("qa", ctx, candidate)

    def _run(self, kind: str, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        identity = "antigravity:reviewer-1" if kind == "review" else "antigravity:qa-1"
        if binary is None:
            return Verdict(
                kind=kind,
                candidate=candidate,
                passed=False,
                defects=(f"antigravity binary unavailable for {kind}",),
                harness_mode="real",
                verifier_identity=identity,
            )
        try:
            proc = subprocess.run(
                [
                    binary,
                    kind,
                    "--candidate-id",
                    candidate.candidate_id,
                    "--source-revision",
                    candidate.source_revision,
                    "--artifact-hash",
                    candidate.artifact_hash,
                    "--artifact-uri",
                    candidate.artifact_uri,
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
                candidate=candidate,
                passed=False,
                defects=(f"antigravity {kind} launch failed: {exc}",),
                harness_mode="real",
                verifier_identity=identity,
            )
        if proc.returncode != 0:
            return Verdict(
                kind=kind,
                candidate=candidate,
                passed=False,
                defects=(proc.stderr.strip() or f"antigravity {kind} failed",),
                harness_mode="real",
                verifier_identity=identity,
            )
        return Verdict(
            kind=kind,
            candidate=candidate,
            passed=True,
            harness_mode="real",
            verifier_identity=identity,
        )


class AntigravityDistributor:
    """DistributionExecutor targeting the Antigravity Production profile."""

    name = "Antigravity"
    harness_mode = "real"
    profile = "production"

    def __init__(self, *, env: dict[str, str] | None = None, binary: str = "antigravity"):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary

    def distribute(
        self, candidate: CandidateIdentity, mission_id: str
    ) -> DistributionResult:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            raise RuntimeError("antigravity production binary unavailable")
        proc = subprocess.run(
            [
                binary,
                "distribute",
                "--profile",
                "production",
                "--candidate-id",
                candidate.candidate_id,
                "--source-revision",
                candidate.source_revision,
                "--artifact-hash",
                candidate.artifact_hash,
                "--artifact-uri",
                candidate.artifact_uri,
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
            candidate=candidate,
            harness_mode="real",
            receipt=proc.stdout.strip() or f"distributed:{candidate.candidate_id}",
        )
