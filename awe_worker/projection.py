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

from .model import ExecutionSlot, canonical_lifecycle_status

PASS = "PASS"
FAIL_CLOSED = "FAIL_CLOSED"

STALE_DISPATCH_EXECUTABLE = "STALE_DISPATCH_EXECUTABLE"
DUPLICATE_DASHBOARD_CURRENT = "DUPLICATE_DASHBOARD_CURRENT"
DASHBOARD_STATUS_MISMATCH = "DASHBOARD_STATUS_MISMATCH"
DISPATCH_PHYSICAL_MISMATCH = "DISPATCH_PHYSICAL_MISMATCH"
DASHBOARD_MODEL_MISMATCH = "DASHBOARD_MODEL_MISMATCH"
UNKNOWN_PHYSICAL_TRUTH = "UNKNOWN_PHYSICAL_TRUTH"
UNKNOWN_PROJECTION_SNAPSHOT = "UNKNOWN_PROJECTION_SNAPSHOT"
NO_EXECUTABLE_TASK = "NO_EXECUTABLE_TASK"
NO_CURRENT_POINTER = "NO_CURRENT_POINTER"
SELECTED_TASK_MISMATCH = "SELECTED_TASK_MISMATCH"
SLOT_CURRENT_AMBIGUOUS = "SLOT_CURRENT_AMBIGUOUS"
DISPATCH_SLOT_AMBIGUOUS = "DISPATCH_SLOT_AMBIGUOUS"
INCOMPLETE_EXECUTION_PROFILE = "INCOMPLETE_EXECUTION_PROFILE"

_CANONICAL_PHYSICAL = frozenset({"Queue", "In Progress", "Blocked", "Review", "Processed"})

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

    findings.extend(_duplicate_physical_findings(phys))
    phys_by_id = {item.task_id: item for item in phys if item.task_id}

    findings.extend(_duplicate_current_findings(dash))
    findings.extend(_unknown_physical_findings(phys, dash, pointers))
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


def evaluate_claim_preflight(
    snapshot: Mapping[str, Any] | None,
    selected_task_id: str | None = None,
    slot: ExecutionSlot | Mapping[str, Any] | str | None = None,
) -> DiagnosticReport:
    """Read-only gate used before local ledger or source-of-record claim.

    A missing snapshot is unknown projection truth and must fail closed.
    When a selected task and slot are supplied, the selected target must bind
    to the authorized Dashboard Current pointer and Dispatch pointer for that
    slot. This function never claims, moves pages, or writes Dashboard/Dispatch.
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
    report = diagnose_snapshot(snapshot)
    if not selected_task_id and slot is None:
        return report
    findings = list(report.findings)
    findings.extend(
        _authorized_pointer_findings(
            snapshot,
            selected_task_id=_norm(selected_task_id),
            slot=slot,
        )
    )
    verdict = PASS if not findings else FAIL_CLOSED
    return DiagnosticReport(verdict=verdict, findings=tuple(findings))


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
        derived_dashboard.append(
            {
                "task_id": task_id,
                "status": status,
                "current": canonical_lifecycle_status(status) in _ACTIVE_DASHBOARD_CURRENT,
                "lane": lane,
                "model_effort": profile,
            }
        )
    rows = list(dashboard) if dashboard is not None else derived_dashboard
    derived_dispatch = dispatch
    if dispatch is None:
        claimable = [
            row
            for row in rows
            if _truthy(row.get("current"))
            and canonical_lifecycle_status(str(row.get("status") or ""))
            in {"Queue", "In Progress"}
        ]
        in_progress = [
            row for row in claimable if canonical_lifecycle_status(str(row.get("status") or "")) == "In Progress"
        ]
        chosen = None
        if len(in_progress) == 1:
            chosen = in_progress[0]
        elif len(claimable) == 1:
            chosen = claimable[0]
        if chosen:
            derived_dispatch = {
                "harness": chosen.get("lane") or "",
                "dispatch_state": "EXECUTABLE",
                "task_id": chosen.get("task_id") or "",
                "expected_lifecycle_state": chosen.get("status") or "",
                "execution_profile": chosen.get("model_effort") or "",
            }
        else:
            derived_dispatch = {
                "harness": "",
                "dispatch_state": "NO_EXECUTABLE_TASK",
                "task_id": "",
            }
    return {
        "physical": physical,
        "dashboard": rows,
        "dispatch": derived_dispatch,
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
    if physical is None or not physical.task_id:
        return False
    if not physical.lane or physical.canonical_status not in _CANONICAL_PHYSICAL:
        return False
    return _exact_slot(physical.execution_profile, physical.lane) is not None


def _duplicate_physical_findings(physical: Sequence[PhysicalTask]) -> list[Finding]:
    findings: list[Finding] = []
    counts: dict[str, int] = {}
    for item in physical:
        if not item.task_id:
            continue
        counts[item.task_id] = counts.get(item.task_id, 0) + 1
    for task_id, count in sorted(counts.items()):
        if count < 2:
            continue
        findings.append(
            Finding(
                code=UNKNOWN_PHYSICAL_TRUTH,
                task_id=task_id,
                detail=f"{count} physical AWE pages exist for {task_id}; physical truth is ambiguous",
            )
        )
    return findings


def _unknown_physical_findings(
    physical: Sequence[PhysicalTask],
    dashboard: Sequence[DashboardRow],
    pointers: Sequence[DispatchView],
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, ...]] = set()
    phys_by_id = {item.task_id: item for item in physical if item.task_id}

    def add(code_task: str, lane: str, detail: str, code: str = UNKNOWN_PHYSICAL_TRUTH) -> None:
        key = (code, code_task, lane, detail)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            Finding(
                code=code,
                task_id=code_task,
                lane=lane,
                detail=detail,
            )
        )

    for item in physical:
        if not item.task_id:
            continue
        if not _physical_known(item):
            add(
                item.task_id,
                item.lane,
                "Physical AWE task is missing a parseable lane, lifecycle, or exact execution profile",
            )

    for row in dashboard:
        if row.current and row.model_effort and _exact_slot(row.model_effort, row.lane) is None:
            add(
                row.task_id,
                row.lane,
                "Dashboard Current row has a missing or unparseable exact execution profile",
                INCOMPLETE_EXECUTION_PROFILE,
            )
        if not row.current:
            continue
        if not row.task_id:
            add("", row.lane, "Dashboard Current row has no Task ID; physical AWE truth is unknown")
            continue
        known = phys_by_id.get(row.task_id)
        if not _physical_known(known):
            add(
                row.task_id,
                row.lane,
                "Dashboard Current task has unknown or missing physical AWE ancestry",
            )

    for pointer in pointers:
        if not pointer.executable:
            continue
        if pointer.execution_profile and _exact_slot(pointer.execution_profile, pointer.harness) is None:
            add(
                pointer.task_id,
                pointer.harness,
                "EXECUTABLE Dispatch pointer has a missing or unparseable exact execution profile",
                INCOMPLETE_EXECUTION_PROFILE,
            )
        expected = canonical_lifecycle_status(pointer.expected_lifecycle_state)
        if expected not in _CANONICAL_PHYSICAL:
            add(
                pointer.task_id,
                pointer.harness,
                "EXECUTABLE Dispatch pointer is missing a parseable expected lifecycle",
                INCOMPLETE_EXECUTION_PROFILE,
            )
        if not pointer.task_id:
            add(
                "",
                pointer.harness,
                "Dispatch is EXECUTABLE with no Task ID; physical AWE truth is unknown",
            )
            continue
        known = phys_by_id.get(pointer.task_id)
        if not _physical_known(known):
            add(
                pointer.task_id,
                pointer.harness,
                "EXECUTABLE Dispatch pointer has unknown or missing physical AWE ancestry",
            )
    return findings


def _duplicate_current_findings(dashboard: Sequence[DashboardRow]) -> list[Finding]:
    findings: list[Finding] = []
    by_slot: dict[str, list[DashboardRow]] = {}
    for row in dashboard:
        if not row.current:
            continue
        slot = _exact_slot(row.model_effort, row.lane)
        if slot is None:
            continue
        by_slot.setdefault(slot.key, []).append(row)
    for slot_key, rows in sorted(by_slot.items()):
        if len(rows) < 2:
            continue
        ids = ", ".join(sorted(row.task_id or "?" for row in rows))
        findings.append(
            Finding(
                code=DUPLICATE_DASHBOARD_CURRENT,
                lane=rows[0].lane,
                detail=(
                    f"{len(rows)} Dashboard rows are Current for exact slot '{slot_key}' "
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
    by_slot: dict[str, list[DispatchView]] = {}
    for pointer in pointers:
        slot = _exact_slot(pointer.execution_profile, pointer.harness)
        if slot is not None:
            by_slot.setdefault(slot.key, []).append(pointer)
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
        elif expected in _CANONICAL_PHYSICAL and physical.canonical_status != expected:
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
    for slot_key, slot_pointers in sorted(by_slot.items()):
        if len(slot_pointers) < 2:
            continue
        ids = ", ".join(sorted(pointer.task_id or "?" for pointer in slot_pointers))
        findings.append(
            Finding(
                code=DISPATCH_SLOT_AMBIGUOUS,
                lane=slot_pointers[0].harness,
                detail=(
                    f"{len(slot_pointers)} Dispatch pointers match exact slot '{slot_key}' "
                    f"({ids}); start/resume grant is ambiguous"
                ),
            )
        )
    return findings


def _slot_from_profile(profile: str, harness: str = "") -> ExecutionSlot | None:
    return _exact_slot(profile, harness)


def _exact_slot(profile: str, harness: str = "") -> ExecutionSlot | None:
    text = _norm(profile).replace("→", "->")
    if not text:
        return None
    try:
        if "->" in text:
            return ExecutionSlot.parse(text)
        parts = [p.strip() for p in text.split("/") if p.strip()]
        if len(parts) >= 3:
            return ExecutionSlot.parse("/".join(parts[:3]))
        if len(parts) >= 2 and harness:
            return ExecutionSlot.parse(f"{harness}/{parts[0]}/{parts[1]}")
        return None
    except ValueError:
        return None


def _row_matches_slot(row: DashboardRow, slot: ExecutionSlot) -> bool:
    if _lane_key(row.lane) and _lane_key(row.lane) != slot.harness:
        return False
    parsed = _slot_from_profile(row.model_effort, row.lane or slot.harness)
    if parsed is None:
        return False
    return parsed.matches(slot)


def _pointer_matches_slot(pointer: DispatchView, slot: ExecutionSlot) -> bool:
    if _lane_key(pointer.harness) and _lane_key(pointer.harness) != slot.harness:
        return False
    parsed = _exact_slot(pointer.execution_profile, pointer.harness or slot.harness)
    if parsed is None:
        return False
    return parsed.matches(slot)


def _authorized_pointer_findings(
    snapshot: Mapping[str, Any],
    selected_task_id: str,
    slot: ExecutionSlot | Mapping[str, Any] | str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    if slot is None or not selected_task_id:
        findings.append(
            Finding(
                code=SELECTED_TASK_MISMATCH,
                detail="Claim/startup selected a task without an exact slot or selected Task ID",
            )
        )
        return findings
    try:
        resolved_slot = ExecutionSlot.parse(slot)
    except ValueError:
        findings.append(
            Finding(
                code=SELECTED_TASK_MISMATCH,
                task_id=selected_task_id,
                detail="Claim/startup slot is not an exact (harness, model, effort) identity",
            )
        )
        return findings

    phys = tuple(_as_physical(item) for item in (snapshot.get("physical") or ()))
    dash = tuple(_as_dashboard(item) for item in (snapshot.get("dashboard") or ()))
    pointers = _as_dispatch_list(snapshot.get("dispatch"))
    selected_matches = [item for item in phys if item.task_id == selected_task_id]
    selected_physical = selected_matches[0] if len(selected_matches) == 1 else None
    current_rows = [row for row in dash if row.current and _row_matches_slot(row, resolved_slot)]
    slot_pointers = [pointer for pointer in pointers if _pointer_matches_slot(pointer, resolved_slot)]
    executable = [pointer for pointer in slot_pointers if pointer.executable]
    is_resume = bool(
        selected_physical is not None
        and selected_physical.canonical_status == "In Progress"
    )
    expected_lifecycle = "In Progress" if is_resume else "Queue"
    action = "In Progress resume" if is_resume else "Queue claim"

    if len(selected_matches) != 1 or not _physical_known(selected_physical):
        findings.append(
            Finding(
                code=UNKNOWN_PHYSICAL_TRUTH,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=f"Selected {selected_task_id} has unknown, duplicate, or incomplete physical AWE truth",
            )
        )
    elif selected_physical is not None:
        physical_slot = _exact_slot(selected_physical.execution_profile, selected_physical.lane)
        if _lane_key(selected_physical.lane) != resolved_slot.harness or (
            physical_slot is None or not physical_slot.matches(resolved_slot)
        ):
            findings.append(
                Finding(
                    code=SELECTED_TASK_MISMATCH,
                    task_id=selected_task_id,
                    lane=resolved_slot.harness,
                    detail=(
                        f"Physical profile '{selected_physical.execution_profile}' "
                        f"does not match slot {resolved_slot.key}"
                    ),
                )
            )
        elif selected_physical.canonical_status not in {"Queue", "In Progress"}:
            findings.append(
                Finding(
                    code=SELECTED_TASK_MISMATCH,
                    task_id=selected_task_id,
                    lane=resolved_slot.harness,
                    detail=(
                        f"Physical lifecycle '{selected_physical.status}' cannot be claimed "
                        f"or resumed for slot {resolved_slot.key}"
                    ),
                )
            )

    if len(current_rows) > 1:
        ids = ", ".join(sorted(row.task_id or "?" for row in current_rows))
        findings.append(
            Finding(
                code=SLOT_CURRENT_AMBIGUOUS,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"{len(current_rows)} Dashboard Current=YES rows match slot "
                    f"{resolved_slot.key} ({ids})"
                ),
            )
        )
        return findings
    if not current_rows:
        findings.append(
            Finding(
                code=NO_CURRENT_POINTER,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=f"No Dashboard Current=YES task matches slot {resolved_slot.key}",
            )
        )

    current_id = current_rows[0].task_id if current_rows else ""
    if current_id and current_id != selected_task_id:
        findings.append(
            Finding(
                code=SELECTED_TASK_MISMATCH,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"Selected {selected_task_id} is not the authorized Current pointer "
                    f"{current_id} for slot {resolved_slot.key}"
                ),
            )
        )
    elif current_rows:
        current_status = current_rows[0].canonical_status
        if current_status != expected_lifecycle:
            findings.append(
                Finding(
                    code=DASHBOARD_STATUS_MISMATCH,
                    task_id=selected_task_id,
                    lane=resolved_slot.harness,
                    detail=(
                        f"Dashboard Current status '{current_rows[0].status}' is not "
                        f"{expected_lifecycle} for this {action}"
                    ),
                )
            )

    if len(slot_pointers) > 1:
        ids = ", ".join(sorted(pointer.task_id or "?" for pointer in slot_pointers))
        findings.append(
            Finding(
                code=DISPATCH_SLOT_AMBIGUOUS,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"{len(slot_pointers)} Dispatch pointers match slot {resolved_slot.key} "
                    f"({ids})"
                ),
            )
        )
        return findings

    if not executable:
        findings.append(
            Finding(
                code=NO_EXECUTABLE_TASK,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"Dispatch is NO_EXECUTABLE_TASK for slot {resolved_slot.key}; "
                    f"{action} is not authorized"
                ),
            )
        )
        return findings

    pointer = executable[0]
    if pointer.task_id != selected_task_id:
        findings.append(
            Finding(
                code=SELECTED_TASK_MISMATCH,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"Selected {selected_task_id} disagrees with EXECUTABLE Dispatch "
                    f"pointer {pointer.task_id or '(empty)'}"
                ),
            )
        )
    parsed_pointer = _exact_slot(pointer.execution_profile, pointer.harness)
    if parsed_pointer is None:
        findings.append(
            Finding(
                code=INCOMPLETE_EXECUTION_PROFILE,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail="EXECUTABLE Dispatch pointer is missing a parseable exact execution profile",
            )
        )
    elif not parsed_pointer.matches(resolved_slot):
        findings.append(
            Finding(
                code=SELECTED_TASK_MISMATCH,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"EXECUTABLE Dispatch profile '{pointer.execution_profile}' "
                    f"does not match slot {resolved_slot.key}"
                ),
            )
        )
    expected = canonical_lifecycle_status(pointer.expected_lifecycle_state)
    if expected not in _CANONICAL_PHYSICAL:
        findings.append(
            Finding(
                code=INCOMPLETE_EXECUTION_PROFILE,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail="EXECUTABLE Dispatch pointer is missing a parseable expected lifecycle",
            )
        )
    elif expected != expected_lifecycle:
        findings.append(
            Finding(
                code=DISPATCH_PHYSICAL_MISMATCH,
                task_id=selected_task_id,
                lane=resolved_slot.harness,
                detail=(
                    f"EXECUTABLE Dispatch expected '{pointer.expected_lifecycle_state}' "
                    f"but {action} requires '{expected_lifecycle}'"
                ),
            )
        )
    return findings

