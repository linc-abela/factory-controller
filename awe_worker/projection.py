"""Read-only AWE projection diagnostic for startup fail-closed checks.

Physical AWE ancestry is authoritative. Dashboard and Dispatch are projections.
This module never moves pages, rewrites Dashboard/Dispatch, claims work, or
infers execution identity from guesswork.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .model import canonical_lifecycle_status

PASS = "PASS"
FAIL_CLOSED = "FAIL_CLOSED"

STALE_DISPATCH_EXECUTABLE = "STALE_DISPATCH_EXECUTABLE"
DUPLICATE_DASHBOARD_CURRENT = "DUPLICATE_DASHBOARD_CURRENT"
DASHBOARD_STATUS_MISMATCH = "DASHBOARD_STATUS_MISMATCH"
DISPATCH_PHYSICAL_MISMATCH = "DISPATCH_PHYSICAL_MISMATCH"
DASHBOARD_MODEL_MISMATCH = "DASHBOARD_MODEL_MISMATCH"
UNKNOWN_PHYSICAL_TRUTH = "UNKNOWN_PHYSICAL_TRUTH"
UNKNOWN_PROJECTION_SNAPSHOT = "UNKNOWN_PROJECTION_SNAPSHOT"

_ACTIVE_DASHBOARD_CURRENT = frozenset({"Queue", "In Progress", "Blocked", "Review"})

_UNSAFE_FOR_EXECUTABLE = frozenset({"Review", "Processed", "Blocked"})


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _lane_key(value: str | None) -> str:
    return _norm(value).lower()


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if value in (False, None):
        return False
    text = str(value).strip().upper()
    return text in {"1", "TRUE", "YES", "__YES__", "Y"}


def _profile_key(value: str | None) -> str:
    return " ".join(_norm(value).lower().replace("→", "->").replace("—", "-").split())


@dataclass(frozen=True)
class PhysicalTask:
    task_id: str
    lane: str
    status: str
    execution_profile: str = ""

    @property
    def canonical_status(self) -> str:
        return canonical_lifecycle_status(self.status)


@dataclass(frozen=True)
class DashboardRow:
    task_id: str
    status: str
    current: bool
    lane: str
    model_effort: str = ""

    @property
    def canonical_status(self) -> str:
        return canonical_lifecycle_status(self.status)


@dataclass(frozen=True)
class DispatchView:
    harness: str
    dispatch_state: str
    task_id: str = ""
    expected_lifecycle_state: str = ""
    execution_profile: str = ""

    @property
    def executable(self) -> bool:
        return _norm(self.dispatch_state).upper() == "EXECUTABLE"


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str
    task_id: str = ""
    lane: str = ""


@dataclass(frozen=True)
class DiagnosticReport:
    verdict: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.verdict == PASS and not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "findings": [asdict(item) for item in self.findings],
            "human": self.as_text(),
            "authoritative": "physical_awe",
            "mutates": False,
        }

    def as_text(self) -> str:
        lines = [f"AWE projection diagnostic: {self.verdict}"]
        if not self.findings:
            lines.append(
                "Physical AWE, Dashboard, and Dispatch agree. "
                "Projection drift does not block a claim."
            )
            return "\n".join(lines)
        for finding in self.findings:
            prefix = finding.task_id or finding.lane or "projection"
            lines.append(f"- {finding.code}: {prefix} — {finding.detail}")
        lines.append("Do not claim work. Physical AWE remains authoritative.")
        return "\n".join(lines)


def diagnose_projection(
    physical: Sequence[PhysicalTask] | Iterable[Mapping[str, Any]],
    dashboard: Sequence[DashboardRow] | Iterable[Mapping[str, Any]],
    dispatch: Sequence[DispatchView] | Iterable[Mapping[str, Any]] | Mapping[str, Any] | DispatchView | None,
) -> DiagnosticReport:
    """Compare physical AWE, Dashboard projection, and Dispatch pointer(s)."""
    phys = tuple(_as_physical(item) for item in physical)
    dash = tuple(_as_dashboard(item) for item in dashboard)
    pointers = _as_dispatch_list(dispatch)
    findings: list[Finding] = []

    phys_by_id = {item.task_id: item for item in phys if item.task_id}

    findings.extend(_duplicate_current_findings(dash))
    findings.extend(_unknown_physical_findings(phys_by_id, dash, pointers))
    findings.extend(_status_mismatch_findings(phys_by_id, dash))
    findings.extend(_model_mismatch_findings(phys_by_id, dash))
    findings.extend(_dispatch_findings(phys_by_id, pointers))

    verdict = PASS if not findings else FAIL_CLOSED
    return DiagnosticReport(verdict=verdict, findings=tuple(findings))


def diagnose_snapshot(snapshot: Mapping[str, Any]) -> DiagnosticReport:
    return diagnose_projection(
        snapshot.get("physical") or (),
        snapshot.get("dashboard") or (),
        snapshot.get("dispatch"),
    )


def load_snapshot(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("snapshot must be a JSON object")
    return data


def diagnose_snapshot_path(path: str | Path) -> DiagnosticReport:
    return diagnose_snapshot(load_snapshot(path))


def evaluate_claim_preflight(snapshot: Mapping[str, Any] | None) -> DiagnosticReport:
    """Read-only gate used before local ledger or source-of-record claim.

    A missing snapshot is unknown projection truth and must fail closed.
    This function never claims, moves pages, or writes Dashboard/Dispatch.
    """
    if snapshot is None:
        return DiagnosticReport(
            verdict=FAIL_CLOSED,
            findings=(
                Finding(
                    code=UNKNOWN_PROJECTION_SNAPSHOT,
                    detail=(
                        "No projection snapshot was supplied to the claim/startup path; "
                        "unknown physical/Dashboard/Dispatch truth must fail closed"
                    ),
                ),
            ),
        )
    return diagnose_snapshot(snapshot)


def snapshot_from_work_items(
    tasks: Sequence[Any],
    dashboard: Sequence[Mapping[str, Any]] | None = None,
    dispatch: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a diagnostic snapshot from observed work items.

    When dashboard/dispatch are omitted, Dashboard rows are derived from the
    same physical tasks so in-memory tests stay consistent. Live sources must
    supply independent Dashboard and Dispatch views.
    """
    physical = []
    derived_dashboard = []
    for task in tasks:
        task_id = _norm(str(getattr(task, "task_id", "") or ""))
        lane = _norm(str(getattr(task, "lane", "") or ""))
        status = _norm(str(getattr(task, "status", "") or ""))
        model = _norm(str(getattr(task, "model", "") or ""))
        effort = _norm(str(getattr(task, "effort", "") or ""))
        profile = _norm(str(getattr(task, "execution_profile", "") or ""))
        if not profile and (lane or model or effort):
            profile = " → ".join(part for part in (lane, " / ".join(p for p in (model, effort) if p)) if part)
        physical.append(
            {
                "task_id": task_id,
                "lane": lane,
                "status": status,
                "execution_profile": profile,
            }
        )
        current = getattr(task, "current", None)
        if current is None:
            current = canonical_lifecycle_status(status) in _ACTIVE_DASHBOARD_CURRENT
        derived_dashboard.append(
            {
                "task_id": task_id,
                "status": status,
                "current": bool(current),
                "lane": lane,
                "model_effort": profile,
            }
        )
    return {
        "physical": physical,
        "dashboard": list(dashboard) if dashboard is not None else derived_dashboard,
        "dispatch": dispatch,
    }


def _as_physical(item: PhysicalTask | Mapping[str, Any]) -> PhysicalTask:
    if isinstance(item, PhysicalTask):
        return item
    return PhysicalTask(
        task_id=_norm(str(item.get("task_id") or "")),
        lane=_norm(str(item.get("lane") or "")),
        status=_norm(str(item.get("status") or "")),
        execution_profile=_norm(str(item.get("execution_profile") or item.get("model_effort") or "")),
    )


def _as_dashboard(item: DashboardRow | Mapping[str, Any]) -> DashboardRow:
    if isinstance(item, DashboardRow):
        return item
    return DashboardRow(
        task_id=_norm(str(item.get("task_id") or "")),
        status=_norm(str(item.get("status") or "")),
        current=_truthy(item.get("current")),
        lane=_norm(str(item.get("lane") or "")),
        model_effort=_norm(str(item.get("model_effort") or item.get("execution_profile") or "")),
    )


def _as_dispatch_list(
    dispatch: Sequence[DispatchView] | Iterable[Mapping[str, Any]] | Mapping[str, Any] | DispatchView | None,
) -> tuple[DispatchView, ...]:
    if dispatch is None:
        return ()
    if isinstance(dispatch, DispatchView):
        return (dispatch,)
    if isinstance(dispatch, Mapping):
        return (_as_dispatch(dispatch),)
    return tuple(_as_dispatch(item) for item in dispatch)


def _as_dispatch(item: DispatchView | Mapping[str, Any]) -> DispatchView:
    if isinstance(item, DispatchView):
        return item
    return DispatchView(
        harness=_norm(str(item.get("harness") or item.get("lane") or "")),
        dispatch_state=_norm(str(item.get("dispatch_state") or item.get("state") or "")),
        task_id=_norm(str(item.get("task_id") or "")),
        expected_lifecycle_state=_norm(
            str(item.get("expected_lifecycle_state") or item.get("physical_lifecycle") or "")
        ),
        execution_profile=_norm(str(item.get("execution_profile") or "")),
    )


def _physical_known(physical: PhysicalTask | None) -> bool:
    return physical is not None and bool(physical.task_id) and bool(physical.canonical_status)


def _unknown_physical_findings(
    phys_by_id: Mapping[str, PhysicalTask],
    dashboard: Sequence[DashboardRow],
    pointers: Sequence[DispatchView],
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()

    def add(code_task: str, lane: str, detail: str) -> None:
        key = (code_task, lane, detail)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            Finding(
                code=UNKNOWN_PHYSICAL_TRUTH,
                task_id=code_task,
                lane=lane,
                detail=detail,
            )
        )

    for row in dashboard:
        if not row.current:
            continue
        if not row.task_id:
            add("", row.lane, "Dashboard Current row has no Task ID; physical AWE truth is unknown")
            continue
        physical = phys_by_id.get(row.task_id)
        if not _physical_known(physical):
            add(
                row.task_id,
                row.lane,
                "Dashboard Current task has unknown or missing physical AWE ancestry",
            )

    for pointer in pointers:
        if not pointer.executable:
            continue
        if not pointer.task_id:
            add(
                "",
                pointer.harness,
                "Dispatch is EXECUTABLE with no Task ID; physical AWE truth is unknown",
            )
            continue
        physical = phys_by_id.get(pointer.task_id)
        if not _physical_known(physical):
            add(
                pointer.task_id,
                pointer.harness,
                "EXECUTABLE Dispatch pointer has unknown or missing physical AWE ancestry",
            )
    return findings


def _duplicate_current_findings(dashboard: Sequence[DashboardRow]) -> list[Finding]:
    findings: list[Finding] = []
    by_lane: dict[str, list[DashboardRow]] = {}
    for row in dashboard:
        if not row.current:
            continue
        by_lane.setdefault(_lane_key(row.lane), []).append(row)
    for lane, rows in sorted(by_lane.items()):
        if len(rows) < 2:
            continue
        ids = ", ".join(sorted(row.task_id or "?" for row in rows))
        findings.append(
            Finding(
                code=DUPLICATE_DASHBOARD_CURRENT,
                lane=lane,
                detail=(
                    f"{len(rows)} Dashboard rows are Current for harness '{lane}' "
                    f"({ids}); claim is ambiguous"
                ),
            )
        )
    return findings


def _status_mismatch_findings(
    phys_by_id: Mapping[str, PhysicalTask],
    dashboard: Sequence[DashboardRow],
) -> list[Finding]:
    findings: list[Finding] = []
    for row in dashboard:
        physical = phys_by_id.get(row.task_id)
        if physical is None:
            continue
        if physical.canonical_status != row.canonical_status:
            findings.append(
                Finding(
                    code=DASHBOARD_STATUS_MISMATCH,
                    task_id=row.task_id,
                    lane=row.lane or physical.lane,
                    detail=(
                        f"Dashboard status '{row.status}' disagrees with physical "
                        f"'{physical.status}'"
                    ),
                )
            )
    return findings


def _model_mismatch_findings(
    phys_by_id: Mapping[str, PhysicalTask],
    dashboard: Sequence[DashboardRow],
) -> list[Finding]:
    findings: list[Finding] = []
    for row in dashboard:
        physical = phys_by_id.get(row.task_id)
        if physical is None or not physical.execution_profile or not row.model_effort:
            continue
        if _profile_key(physical.execution_profile) != _profile_key(row.model_effort):
            findings.append(
                Finding(
                    code=DASHBOARD_MODEL_MISMATCH,
                    task_id=row.task_id,
                    lane=row.lane or physical.lane,
                    detail=(
                        f"Dashboard model assignment '{row.model_effort}' disagrees with "
                        f"physical '{physical.execution_profile}'"
                    ),
                )
            )
    return findings


def _dispatch_findings(
    phys_by_id: Mapping[str, PhysicalTask],
    pointers: Sequence[DispatchView],
) -> list[Finding]:
    findings: list[Finding] = []
    for pointer in pointers:
        if not pointer.executable:
            continue
        physical = phys_by_id.get(pointer.task_id)
        if not _physical_known(physical):
            continue
        expected = canonical_lifecycle_status(pointer.expected_lifecycle_state)
        if physical.canonical_status in _UNSAFE_FOR_EXECUTABLE:
            findings.append(
                Finding(
                    code=STALE_DISPATCH_EXECUTABLE,
                    task_id=pointer.task_id,
                    lane=pointer.harness or physical.lane,
                    detail=(
                        f"Dispatch is EXECUTABLE while physical ancestry is "
                        f"'{physical.status}'"
                    ),
                )
            )
        elif expected and physical.canonical_status != expected:
            findings.append(
                Finding(
                    code=DISPATCH_PHYSICAL_MISMATCH,
                    task_id=pointer.task_id,
                    lane=pointer.harness or physical.lane,
                    detail=(
                        f"Dispatch expected '{pointer.expected_lifecycle_state}' "
                        f"but physical ancestry is '{physical.status}'"
                    ),
                )
            )
    return findings
