from __future__ import annotations

from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import (
    DistributionResult,
    EngineeringResult,
    ExecutorResult,
    MissionContext,
    Verdict,
    WorkItem,
)


class ScriptedGrok:
    """Deterministic EngineeringExecutor double. harness_mode=simulated."""

    name = "Grok Build"
    harness_mode = "simulated"

    def __init__(self, artifacts: list[str], *, credentials: bool = True):
        self.artifacts = list(artifacts)
        self._credentials = credentials
        self.calls: list[tuple[str, WorkItem]] = []
        self._i = 0

    def credentials_available(self) -> bool:
        return self._credentials

    def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult:
        self.calls.append((ctx.mission_id, work))
        if not self._credentials:
            return ExecutorResult(
                blocked=True,
                reason="simulated executor was configured without credentials",
                harness_mode="simulated",
                simulated=True,
            )
        if self._i >= len(self.artifacts):
            return ExecutorResult(
                blocked=True,
                reason="no scripted candidate remaining",
                harness_mode="simulated",
                simulated=True,
            )
        artifact_id = self.artifacts[self._i]
        self._i += 1
        return ExecutorResult(
            artifact_id=artifact_id,
            harness_mode="simulated",
            simulated=True,
        )


class ScriptedHermes:
    """Deterministic EngineeringManager double. Always delegates to the executor."""

    name = "Nous Hermes Agent"
    harness_mode = "simulated"

    def __init__(self, executor: EngineeringExecutor):
        self.executor = executor
        self.calls: list[MissionContext] = []

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult:
        self.calls.append(ctx)
        work = WorkItem(
            objective=ctx.pcp.get("intent") or ctx.pcp.get("title") or "implement",
            defects=ctx.defects,
        )
        executed = self.executor.implement(ctx, work)
        if executed.blocked or not executed.artifact_id:
            return EngineeringResult(
                blocked=True,
                reason=executed.reason or "executor blocked",
                harness_mode=executed.harness_mode,
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=True,
            )
        return EngineeringResult(
            candidate_artifact_id=executed.artifact_id,
            harness_mode="simulated",
            manager_name=self.name,
            executor_name=self.executor.name,
            executor_called=True,
        )


class ScriptedVerifier:
    """Map artifact_id -> (review_pass, qa_pass). Separate channels."""

    name = "Antigravity"
    harness_mode = "simulated"

    def __init__(self, table: dict[str, tuple[bool, bool]]):
        self.table = table
        self.review_calls: list[str] = []
        self.qa_calls: list[str] = []

    def review(self, ctx: MissionContext, artifact_id: str) -> Verdict:
        del ctx
        self.review_calls.append(artifact_id)
        passed, _ = self.table[artifact_id]
        return Verdict(
            kind="review",
            artifact_id=artifact_id,
            passed=passed,
            defects=() if passed else (f"review fail on {artifact_id}",),
            harness_mode="simulated",
        )

    def qa(self, ctx: MissionContext, artifact_id: str) -> Verdict:
        del ctx
        self.qa_calls.append(artifact_id)
        _, passed = self.table[artifact_id]
        return Verdict(
            kind="qa",
            artifact_id=artifact_id,
            passed=passed,
            defects=() if passed else (f"qa fail on {artifact_id}",),
            harness_mode="simulated",
        )


class ScriptedDistributor:
    name = "Antigravity"
    harness_mode = "simulated"
    profile = "production"

    def __init__(self, replace_with: str | None = None):
        self.replace_with = replace_with
        self.calls: list[tuple[str, str]] = []

    def distribute(self, artifact_id: str, mission_id: str) -> DistributionResult:
        self.calls.append((artifact_id, mission_id))
        return DistributionResult(
            artifact_id=self.replace_with or artifact_id,
            harness_mode="simulated",
            receipt=f"sim-dist:{artifact_id}",
        )
