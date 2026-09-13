from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from factory_v2.states import MissionState


@dataclass(frozen=True)
class PCP:
    """Approved PCP admitted at Gate 1. Identity is the content hash."""

    title: str
    intent: str
    product: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        body = {
            "title": self.title,
            "intent": self.intent,
            "product": self.product,
        }
        body.update(self.extra)
        return body


@dataclass(frozen=True)
class WorkItem:
    objective: str
    defects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    artifact_id: str
    sequence: int
    review_verdict: str = "none"
    qa_verdict: str = "none"
    status: str = "produced"


@dataclass(frozen=True)
class Verdict:
    kind: str  # "review" | "qa"
    artifact_id: str
    passed: bool
    defects: tuple[str, ...] = ()
    harness_mode: str = "simulated"


@dataclass(frozen=True)
class EngineeringResult:
    blocked: bool = False
    reason: str = ""
    candidate_artifact_id: str | None = None
    candidate_id: str | None = None
    harness_mode: str = "simulated"
    manager_name: str = ""
    executor_name: str = ""
    executor_called: bool = False


@dataclass(frozen=True)
class ExecutorResult:
    blocked: bool = False
    reason: str = ""
    artifact_id: str | None = None
    harness_mode: str = "real"
    simulated: bool = False


@dataclass(frozen=True)
class DistributionResult:
    artifact_id: str
    harness_mode: str = "simulated"
    receipt: str = ""


@dataclass(frozen=True)
class MissionContext:
    mission_id: str
    pcp_hash: str
    pcp: dict[str, Any]
    workspace_path: str
    defects: tuple[str, ...] = ()
    current_artifact_id: str | None = None


@dataclass(frozen=True)
class MissionSnapshot:
    mission_id: str
    pcp_hash: str
    state: MissionState
    current_candidate_id: str | None
    current_artifact_id: str | None
    approved_artifact_id: str | None
    owner_decision: str | None
    blocked_reason: str | None
    candidates: tuple[Candidate, ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    pcp: dict[str, Any] = field(default_factory=dict)
