from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from factory_v2.states import MissionState


@dataclass(frozen=True)
class CandidateIdentity:
    """Canonical candidate tuple required by SFV2-002."""

    candidate_id: str
    source_revision: str
    artifact_hash: str
    artifact_uri: str

    def as_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "source_revision": self.source_revision,
            "artifact_hash": self.artifact_hash,
            "artifact_uri": self.artifact_uri,
        }

    def key(self) -> tuple[str, str, str, str]:
        return (
            self.candidate_id,
            self.source_revision,
            self.artifact_hash,
            self.artifact_uri,
        )


@dataclass(frozen=True)
class WorkItem:
    objective: str
    defects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    identity: CandidateIdentity
    sequence: int
    attempt_id: str
    hermes_session_id: str = ""
    grok_session_ref: str = ""
    review_verdict: str = "none"
    qa_verdict: str = "none"
    status: str = "produced"

    @property
    def candidate_id(self) -> str:
        return self.identity.candidate_id

    @property
    def artifact_id(self) -> str:
        """Compatibility alias: simulated labels equal candidate_id."""
        return self.identity.candidate_id


@dataclass(frozen=True)
class Verdict:
    kind: str  # "review" | "qa"
    candidate: CandidateIdentity
    passed: bool
    defects: tuple[str, ...] = ()
    harness_mode: str = "simulated"
    evidence_uri: str = ""
    verifier_identity: str = ""


@dataclass(frozen=True)
class EngineeringResult:
    blocked: bool = False
    reason: str = ""
    candidate: CandidateIdentity | None = None
    hermes_session_id: str = ""
    grok_session_ref: str = ""
    harness_mode: str = "simulated"
    manager_name: str = ""
    executor_name: str = ""
    executor_called: bool = False
    engineering_tests: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ExecutorResult:
    blocked: bool = False
    reason: str = ""
    candidate: CandidateIdentity | None = None
    grok_session_ref: str = ""
    harness_mode: str = "real"
    simulated: bool = False
    engineering_tests: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class DistributionResult:
    candidate: CandidateIdentity
    harness_mode: str = "simulated"
    receipt: str = ""


@dataclass(frozen=True)
class MissionContext:
    mission_id: str
    lineage_id: str
    pcp_hash: str
    pcp: dict[str, Any]
    workspace_path: str
    attempt_number: int = 1
    rework_sequence: int = 0
    hermes_session_id: str | None = None
    defects: tuple[str, ...] = ()
    current: CandidateIdentity | None = None


@dataclass(frozen=True)
class MissionSnapshot:
    mission_id: str
    lineage_id: str
    pcp_hash: str
    state: MissionState
    current: CandidateIdentity | None
    approved: CandidateIdentity | None
    rc_id: str | None
    owner_decision: str | None
    blocked_reason: str | None
    hermes_session_id: str | None
    attempt_number: int
    rework_sequence: int
    owner_history: tuple[dict[str, Any], ...] = ()
    rework_history: tuple[dict[str, Any], ...] = ()
    candidates: tuple[Candidate, ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    pcp: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def current_candidate_id(self) -> str | None:
        return None if self.current is None else self.current.candidate_id

    @property
    def current_artifact_id(self) -> str | None:
        return None if self.current is None else self.current.candidate_id

    @property
    def approved_artifact_id(self) -> str | None:
        return None if self.approved is None else self.approved.candidate_id
