from __future__ import annotations

from factory_v2.canonical import identity_for
from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import (
    CandidateIdentity,
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
        label = self.artifacts[self._i]
        self._i += 1
        candidate = identity_for(label, ctx.workspace_path)
        return ExecutorResult(
            candidate=candidate,
            grok_session_ref=f"grok-{label}",
            harness_mode="simulated",
            simulated=True,
        )


class ScriptedHermes:
    """Simulated Nous Hermes campaign: Hermes itself delegates to Grok Build."""

    name = "Nous Hermes Agent"
    harness_mode = "simulated"

    def __init__(self, executor: EngineeringExecutor):
        self.executor = executor
        self.calls: list[MissionContext] = []

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult:
        self.calls.append(ctx)
        work = WorkItem(
            objective=ctx.pcp.get("product", {}).get("objective") or "implement admitted PCP",
            defects=ctx.defects,
        )
        executed = self.executor.implement(ctx, work)
        session = ctx.hermes_session_id or f"hermes-{ctx.mission_id}"
        if executed.blocked or executed.candidate is None:
            return EngineeringResult(
                blocked=True,
                reason=executed.reason or "executor blocked",
                harness_mode=executed.harness_mode,
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=True,
                hermes_session_id=session,
            )
        return EngineeringResult(
            candidate=executed.candidate,
            hermes_session_id=session,
            grok_session_ref=executed.grok_session_ref,
            harness_mode="simulated",
            manager_name=self.name,
            executor_name=self.executor.name,
            executor_called=True,
        )


class ScriptedVerifier:
    """Map candidate_id -> (review_pass, qa_pass). Separate channels."""

    name = "Antigravity"
    harness_mode = "simulated"

    def __init__(
        self,
        table: dict[str, tuple[bool, bool]],
        *,
        substitute: CandidateIdentity | None = None,
    ):
        self.table = table
        self.substitute = substitute
        self.review_calls: list[str] = []
        self.qa_calls: list[str] = []

    def review(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        del ctx
        self.review_calls.append(candidate.candidate_id)
        bound = self.substitute or candidate
        passed, _ = self.table[candidate.candidate_id]
        return Verdict(
            kind="review",
            candidate=bound,
            passed=passed,
            defects=() if passed else (f"review fail on {candidate.candidate_id}",),
            harness_mode="simulated",
            verifier_identity="antigravity:reviewer-1",
        )

    def qa(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict:
        del ctx
        self.qa_calls.append(candidate.candidate_id)
        bound = self.substitute or candidate
        _, passed = self.table[candidate.candidate_id]
        return Verdict(
            kind="qa",
            candidate=bound,
            passed=passed,
            defects=() if passed else (f"qa fail on {candidate.candidate_id}",),
            harness_mode="simulated",
            verifier_identity="antigravity:qa-1",
        )


class ScriptedDistributor:
    name = "Antigravity"
    harness_mode = "simulated"
    profile = "production"

    def __init__(self, replace_with: CandidateIdentity | None = None):
        self.replace_with = replace_with
        self.calls: list[tuple[CandidateIdentity, str]] = []

    def distribute(
        self, candidate: CandidateIdentity, mission_id: str
    ) -> DistributionResult:
        self.calls.append((candidate, mission_id))
        return DistributionResult(
            candidate=self.replace_with or candidate,
            harness_mode="simulated",
            receipt=f"sim-dist:{candidate.candidate_id}",
        )
