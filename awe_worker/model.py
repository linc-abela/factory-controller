"""Data models and value objects for Autonomous AWE Worker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class AWEStatus(str, Enum):
    QUEUE = "Queue"
    IN_PROGRESS = "In Progress"
    BLOCKED = "Blocked"
    REVIEW = "Review"
    PROCESSED = "Processed"
    DONE = "Done"  # legacy alias for Review; never write this on new transitions


CANONICAL_ACTIVE_STATES = (
    AWEStatus.QUEUE.value,
    AWEStatus.IN_PROGRESS.value,
    AWEStatus.BLOCKED.value,
    AWEStatus.REVIEW.value,
    AWEStatus.PROCESSED.value,
)
TERMINAL_PRODUCER_STATES = (AWEStatus.REVIEW.value, AWEStatus.DONE.value)


def canonical_lifecycle_status(raw: str | None) -> str:
    """Map any observed lifecycle label onto the canonical vocabulary.

    New writes use Review. Legacy Done is accepted as Review during migration.
    """
    value = (raw or "").strip()
    if value == AWEStatus.DONE.value:
        return AWEStatus.REVIEW.value
    return value


class CertificationVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class GateOutcome(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    PENDING = "PENDING"


@dataclass(frozen=True)
class ExecutionSlot:
    """Canonical execution slot identity: (harness, model, effort).

    Examples:
        - harness="antigravity", model="gemini-3.8-flash", effort="high"
        - harness="codex", model="gpt-5.6-luna", effort="max"
        - harness="cursor", model="grok-4.6", effort="high"
    """

    harness: str
    model: str
    effort: str

    @classmethod
    def parse(cls, raw: str | Mapping[str, Any] | ExecutionSlot) -> ExecutionSlot:
        if isinstance(raw, ExecutionSlot):
            return raw
        if isinstance(raw, Mapping):
            slot = cls(
                harness=str(raw.get("harness", "")).strip().lower(),
                model=cls._normalize_model(str(raw.get("model", ""))),
                effort=str(raw.get("effort", "")).strip().lower(),
            )
            if not (slot.harness and slot.model and slot.effort):
                raise ValueError(
                    "EXECUTION_PROFILE_UNKNOWN: exact (harness, model, effort) is required"
                )
            return slot
        raw_str = str(raw).strip().replace("→", "->")
        if "->" in raw_str:
            harness_part, rest = raw_str.split("->", 1)
            parts = [harness_part.strip()] + [p.strip() for p in rest.split("/") if p.strip()]
        elif "/" in raw_str:
            parts = [p.strip() for p in raw_str.split("/") if p.strip()]
        else:
            parts = [raw_str]

        if len(parts) < 3 or not all(parts[:3]):
            raise ValueError(
                "EXECUTION_PROFILE_UNKNOWN: exact (harness, model, effort) is required; "
                "do not default effort to medium"
            )
        return cls(
            harness=parts[0].lower(),
            model=cls._normalize_model(parts[1]),
            effort=parts[2].lower(),
        )

    @staticmethod
    def _normalize_model(model_name: str) -> str:
        s = model_name.strip().lower()
        s = re.sub(r"\s+", "-", s)
        return s

    @property
    def key(self) -> str:
        return f"{self.harness}/{self.model}/{self.effort}"

    def matches(self, other: ExecutionSlot) -> bool:
        if self.harness != other.harness:
            return False
        # Normalize model matching (e.g. gemini-3.8-flash matches gemini-3.8-flash-high if effort is high)
        s_model = self.model.replace("-high", "").replace("-medium", "").replace("-max", "")
        o_model = other.model.replace("-high", "").replace("-medium", "").replace("-max", "")
        if s_model != o_model:
            return False
        return self.effort == other.effort

    def __str__(self) -> str:
        return f"{self.harness.capitalize()} -> {self.model} ({self.effort.capitalize()})"


@dataclass(frozen=True)
class AWEWorkItem:
    """An admitted work packet in Agent Work Exchange."""

    task_id: str
    title: str
    lane: str
    role: str
    status: str
    model: str
    effort: str
    sequence: int
    task_page_url: str = ""
    task_page_id: str = ""
    dashboard_page_id: str = ""
    notes: str = ""
    verdict: str = ""
    body_markdown: str = ""
    owner_only: bool = False
    owner_reason: str = "not_applicable"
    current: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AWEWorkItem:
        task_id = str(data.get("task_id", "")).strip()
        sequence = data.get("sequence")
        if sequence is None:
            # Try to parse sequence from task_id e.g. "SF-217" -> 217
            match = re.search(r"\d+", task_id)
            sequence = int(match.group(0)) if match else 999999

        return cls(
            task_id=task_id,
            title=str(data.get("title", task_id)),
            lane=str(data.get("lane", "")).strip(),
            role=str(data.get("role", "")).strip(),
            status=str(data.get("status", AWEStatus.QUEUE.value)),
            model=str(data.get("model", "")).strip(),
            effort=str(data.get("effort", "medium")).strip(),
            sequence=int(sequence),
            task_page_url=str(data.get("task_page_url", "")),
            task_page_id=str(data.get("task_page_id", "")),
            dashboard_page_id=str(data.get("dashboard_page_id", "")),
            notes=str(data.get("notes", "")),
            verdict=str(data.get("verdict", "")),
            body_markdown=str(data.get("body_markdown", "")),
            owner_only=bool(data.get("owner_only", False)),
            owner_reason=str(data.get("owner_reason", "not_applicable")),
            current=bool(data.get("current", False)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )

    @property
    def slot(self) -> ExecutionSlot:
        return ExecutionSlot(
            harness=self.lane.lower(),
            model=ExecutionSlot._normalize_model(self.model),
            effort=self.effort.lower(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "lane": self.lane,
            "role": self.role,
            "status": self.status,
            "model": self.model,
            "effort": self.effort,
            "sequence": self.sequence,
            "task_page_url": self.task_page_url,
            "task_page_id": self.task_page_id,
            "dashboard_page_id": self.dashboard_page_id,
            "notes": self.notes,
            "verdict": self.verdict,
            "body_markdown": self.body_markdown,
            "owner_only": self.owner_only,
            "owner_reason": self.owner_reason,
            "current": self.current,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    action: str  # "claimed", "resumed", "refused"
    token: str | None = None
    lease_expires_at: float = 0.0
    code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class GroundingResult:
    ok: bool
    source: str  # "broker", "direct_git_fallback"
    repo_identity: str
    head_sha: str
    manifest_digest: str = ""
    manifest_ref: dict[str, Any] = field(default_factory=dict)
    selected_paths: list[str] = field(default_factory=list)
    overview: dict[str, Any] = field(default_factory=dict)
    full_eligible_bytes: int = 0
    selected_bytes: int = 0
    reduction_ratio: float = 0.0
    latency_ms: float = 0.0
    fallback_reason: str = ""


@dataclass(frozen=True)
class WakeReceipt:
    success: bool
    harness: str
    slot_key: str
    command: list[str] = field(default_factory=list)
    pid: int | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class CandidateHead:
    task_id: str
    branch_name: str
    head_sha: str
    base_sha: str
    pr_number: int | None = None
    pr_url: str | None = None
    frozen_at: float = 0.0


@dataclass(frozen=True)
class CertificationRecord:
    task_id: str
    head_sha: str
    role: str  # "review", "qa"
    slot_key: str
    verdict: CertificationVerdict
    evidence_ref: str = ""
    defects: list[str] = field(default_factory=list)
    certified_at: float = 0.0


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    task_id: str
    head_sha: str
    next_action: str  # "PROPOSE_CANONICAL_INTEGRATION", "ROUTER_REWORK_SAME_LINEAGE", "WAIT_CERTIFICATION"
    missing_roles: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    new_main_sha: str = ""
    detail: str = ""


@dataclass(frozen=True)
class CompletionReport:
    state: str  # "DONE", "IN_PROGRESS", "BLOCKED", "FORGED_DONE", "STALE_HEAD"
    task_id: str
    head_sha: str = ""
    base_sha: str = ""
    branch_name: str = ""
    pr_number: int | None = None
    evidence_valid: bool = False
    detail: str = ""


@dataclass(frozen=True)
class EscalationReport:
    escalated: bool
    task_id: str
    reason_code: str = "not_applicable"
    detail: str = ""
    action_required: str = ""


@dataclass(frozen=True)
class CycleSummary:
    worker_id: str
    cycle_id: str
    observed_tasks: int
    claimed_task: str | None
    grounding_source: str | None
    wake_receipt: WakeReceipt | None
    completion_report: CompletionReport | None
    gate_decision: GateDecision | None
    escalation: EscalationReport | None
    health: str = "healthy"
    detail: str = ""
