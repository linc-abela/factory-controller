from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory_v2.models import (
    CandidateIdentity,
    DistributionResult,
    EngineeringResult,
    ExecutorResult,
    MissionContext,
    Verdict,
    WorkItem,
)


@runtime_checkable
class EngineeringExecutor(Protocol):
    """Coding/implementation capability. Target: Grok Build."""

    name: str
    harness_mode: str

    def credentials_available(self) -> bool: ...

    def implement(self, ctx: MissionContext, work: WorkItem) -> ExecutorResult: ...


@runtime_checkable
class EngineeringManager(Protocol):
    """Engineering Manager capability. Target: Nous Hermes Agent.

    Hermes coordinates Grok Build. Controller does not call the executor.
    """

    name: str
    harness_mode: str

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult: ...


@runtime_checkable
class Verifier(Protocol):
    """Independent verification. Target: Antigravity.

    Review and QA remain separate verdicts even when both use Antigravity.
    """

    name: str
    harness_mode: str

    def review(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict: ...

    def qa(self, ctx: MissionContext, candidate: CandidateIdentity) -> Verdict: ...


@runtime_checkable
class DistributionExecutor(Protocol):
    """Distribution/production capability. Target: Antigravity Production."""

    name: str
    harness_mode: str

    def distribute(
        self, candidate: CandidateIdentity, mission_id: str
    ) -> DistributionResult: ...
