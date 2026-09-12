"""Scheduled worker service, liveness watchdog, and crash recovery.

Provides a bounded scheduled execution loop with heartbeat tracking,
stale worker detection, graceful signal handling, and clean crash recovery.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .dispatch import DISPATCH_PAGE_IDS, DISPATCH_STALE, HeadlessDispatchResolver, NO_EXECUTABLE_TASK
from .model import CycleSummary, ExecutionSlot
from .worker import AWEAutonomousWorker

LIVENESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS awe_worker_liveness (
    worker_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    cycles_completed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    last_summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS awe_worker_runtime (
    worker_id TEXT PRIMARY KEY,
    service_state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    last_observation_at REAL,
    last_cycle_at REAL,
    last_claimed_task TEXT,
    last_wake_json TEXT NOT NULL DEFAULT '{}',
    work_state TEXT NOT NULL DEFAULT 'unknown',
    slots_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    last_summary_json TEXT NOT NULL DEFAULT '{}'
);
"""


@dataclass(frozen=True)
class WorkerLivenessRecord:
    worker_id: str
    pid: int
    started_at: float
    last_heartbeat: float
    cycles_completed: int
    status: str
    last_summary: dict[str, Any]


@dataclass(frozen=True)
class WorkerRuntimeRecord:
    """Durable operational state for the cross-lane continuation service."""

    worker_id: str
    service_state: str
    updated_at: float
    last_observation_at: float | None
    last_cycle_at: float | None
    last_claimed_task: str | None
    last_wake_by_harness: dict[str, Any]
    work_state: str
    slots: list[str]
    last_error: str
    last_summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "service_state": self.service_state,
            "updated_at": self.updated_at,
            "last_observation_at": self.last_observation_at,
            "last_cycle_at": self.last_cycle_at,
            "last_claimed_task": self.last_claimed_task,
            "last_wake_by_harness": self.last_wake_by_harness,
            "work_state": self.work_state,
            "slots": self.slots,
            "last_error": self.last_error,
            "last_summary": self.last_summary,
        }


class SlotProvider(Protocol):
    """Read exact execution slots from the authoritative Dispatch projection."""

    def observe_slots(self) -> Sequence[ExecutionSlot]:
        ...


class LiveDispatchSlotProvider:
    """Discover all executable slots from the three fixed Dispatch pages.

    This provider deliberately has no workspace search fallback.  A missing,
    ambiguous, unauthenticated, or physically stale Dispatch projection yields
    no slot and a diagnostic that the scheduler records for the Owner.
    """

    def __init__(
        self,
        client: Any = None,
        resolver: HeadlessDispatchResolver | None = None,
        harnesses: Sequence[str] | None = None,
    ) -> None:
        self.resolver = resolver or HeadlessDispatchResolver(client=client)
        self.harnesses = tuple(harnesses or DISPATCH_PAGE_IDS.keys())
        self.last_observed_at: float | None = None
        self.last_errors: dict[str, str] = {}
        self.last_states: dict[str, str] = {}

    def observe_slots(self) -> tuple[ExecutionSlot, ...]:
        slots: list[ExecutionSlot] = []
        slot_keys: set[str] = set()
        self.last_observed_at = time.time()
        self.last_errors = {}
        self.last_states = {}
        for harness in self.harnesses:
            results = self.resolver.resolve_all(harness)
            for result in results:
                pointer = result.pointer
                if result.ok and result.code == "OK" and pointer is not None:
                    try:
                        slot = ExecutionSlot.parse(pointer.execution_profile)
                    except ValueError:
                        self.last_errors[harness] = "EXECUTION_PROFILE_UNKNOWN"
                        continue
                    if slot.harness != harness.lower():
                        self.last_errors[harness] = "EXECUTION_PROFILE_HARNESS_MISMATCH"
                        continue
                    if slot.key in slot_keys:
                        self.last_errors[harness] = "DISPATCH_SLOT_AMBIGUOUS"
                        self.last_states[harness] = "stale"
                        slots = [candidate for candidate in slots if candidate.key != slot.key]
                        slot_keys.discard(slot.key)
                        continue
                    slot_keys.add(slot.key)
                    slots.append(slot)
                    self.last_states[harness] = "executable"
                elif result.ok and result.code == NO_EXECUTABLE_TASK:
                    self.last_states[harness] = "no_executable_task"
                else:
                    self.last_errors[harness] = result.detail or result.code or DISPATCH_STALE
                    self.last_states[harness] = "stale"
        return tuple(slots)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "last_observation_at": self.last_observed_at,
            "states": dict(self.last_states),
            "errors": dict(self.last_errors),
        }


class AWEScheduledRunner:
    """Bounded scheduled execution service for Autonomous AWE Worker."""

    def __init__(
        self,
        worker: AWEAutonomousWorker,
        worker_id: str = "awe-worker-1",
        heartbeat_file: str | Path | None = None,
        stale_threshold_seconds: float = 90.0,
        slot_provider: SlotProvider | None = None,
        target_slots: Sequence[ExecutionSlot] | None = None,
        settlement_rechecks: int = 1,
    ) -> None:
        self.worker = worker
        self.worker_id = worker_id
        self.heartbeat_file = Path(heartbeat_file) if heartbeat_file else None
        self.stale_threshold = stale_threshold_seconds
        self.slot_provider = slot_provider
        self.target_slots = tuple(
            ExecutionSlot.parse(slot) for slot in (target_slots or ())
        )
        self.settlement_rechecks = max(0, int(settlement_rechecks))
        self._stop_requested = False
        self._init_liveness_table()

    def _init_liveness_table(self) -> None:
        with self.worker.ledger._get_connection() as conn:
            conn.executescript(LIVENESS_SCHEMA)

    def _runtime_row(self) -> dict[str, Any]:
        with self.worker.ledger._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM awe_worker_runtime WHERE worker_id = ?;",
                (self.worker_id,),
            ).fetchone()
        if row is None:
            return {
                "worker_id": self.worker_id,
                "service_state": "not_run",
                "updated_at": 0.0,
                "last_observation_at": None,
                "last_cycle_at": None,
                "last_claimed_task": None,
                "last_wake_json": "{}",
                "work_state": "unknown",
                "slots_json": "[]",
                "last_error": "",
                "last_summary_json": "{}",
            }
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _json_object(raw: Any, default: Any) -> Any:
        try:
            value = json.loads(raw or "")
            return value
        except (TypeError, ValueError):
            return default

    def _write_runtime(
        self,
        *,
        now: float,
        service_state: str | None = None,
        last_observation_at: float | None = None,
        last_cycle_at: float | None = None,
        last_claimed_task: str | None = None,
        last_wake_by_harness: Mapping[str, Any] | None = None,
        work_state: str | None = None,
        slots: Sequence[str] | None = None,
        last_error: str | None = None,
        last_summary: Mapping[str, Any] | None = None,
    ) -> None:
        current = self._runtime_row()
        wake = (
            dict(last_wake_by_harness)
            if last_wake_by_harness is not None
            else self._json_object(current.get("last_wake_json"), {})
        )
        slot_values = (
            list(slots)
            if slots is not None
            else self._json_object(current.get("slots_json"), [])
        )
        summary = (
            dict(last_summary)
            if last_summary is not None
            else self._json_object(current.get("last_summary_json"), {})
        )
        with self.worker.ledger._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO awe_worker_runtime (
                    worker_id, service_state, updated_at, last_observation_at,
                    last_cycle_at, last_claimed_task, last_wake_json, work_state,
                    slots_json, last_error, last_summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    service_state = excluded.service_state,
                    updated_at = excluded.updated_at,
                    last_observation_at = excluded.last_observation_at,
                    last_cycle_at = excluded.last_cycle_at,
                    last_claimed_task = excluded.last_claimed_task,
                    last_wake_json = excluded.last_wake_json,
                    work_state = excluded.work_state,
                    slots_json = excluded.slots_json,
                    last_error = excluded.last_error,
                    last_summary_json = excluded.last_summary_json;
                """,
                (
                    self.worker_id,
                    service_state or current.get("service_state", "not_run"),
                    now,
                    last_observation_at
                    if last_observation_at is not None
                    else current.get("last_observation_at"),
                    last_cycle_at if last_cycle_at is not None else current.get("last_cycle_at"),
                    last_claimed_task
                    if last_claimed_task is not None
                    else current.get("last_claimed_task"),
                    json.dumps(wake, sort_keys=True),
                    work_state or current.get("work_state", "unknown"),
                    json.dumps(slot_values, sort_keys=True),
                    last_error if last_error is not None else current.get("last_error", ""),
                    json.dumps(summary, sort_keys=True),
                ),
            )

    def record_observation(
        self,
        slots: Sequence[ExecutionSlot],
        *,
        detail: str = "",
        work_state: str | None = None,
        now: float | None = None,
    ) -> None:
        """Persist the result of a Dispatch observation before any claim."""
        ts = time.time() if now is None else now
        provider_detail = ""
        if self.slot_provider is not None:
            diagnostics = getattr(self.slot_provider, "diagnostics", None)
            if callable(diagnostics):
                data = diagnostics()
                errors = data.get("errors") if isinstance(data, Mapping) else {}
                if isinstance(errors, Mapping) and errors:
                    provider_detail = "; ".join(
                        f"{key}: {value}" for key, value in errors.items()
                    )
        error_parts: list[str] = []
        for part in (detail, provider_detail):
            if part and part not in error_parts:
                error_parts.append(part)
        error = "; ".join(error_parts)
        if work_state is None:
            work_state = "active" if slots else ("projection_refused" if error else "no_eligible_work")
        self._write_runtime(
            now=ts,
            service_state="running",
            last_observation_at=ts,
            work_state=work_state,
            slots=[slot.key for slot in slots],
            last_error=error,
        )

    def _runtime_from_row(self, row: Mapping[str, Any]) -> WorkerRuntimeRecord:
        return WorkerRuntimeRecord(
            worker_id=str(row.get("worker_id") or self.worker_id),
            service_state=str(row.get("service_state") or "unknown"),
            updated_at=float(row.get("updated_at") or 0.0),
            last_observation_at=(
                float(row["last_observation_at"])
                if row.get("last_observation_at") is not None
                else None
            ),
            last_cycle_at=(
                float(row["last_cycle_at"])
                if row.get("last_cycle_at") is not None
                else None
            ),
            last_claimed_task=row.get("last_claimed_task"),
            last_wake_by_harness=self._json_object(row.get("last_wake_json"), {}),
            work_state=str(row.get("work_state") or "unknown"),
            slots=self._json_object(row.get("slots_json"), []),
            last_error=str(row.get("last_error") or ""),
            last_summary=self._json_object(row.get("last_summary_json"), {}),
        )

    def get_runtime_status(self) -> WorkerRuntimeRecord:
        """Return the durable service/cycle/wake observability snapshot."""
        return self._runtime_from_row(self._runtime_row())

    def recover_crashed_workers(self, now: float | None = None) -> list[str]:
        """Detect and recover any orphaned claims from previously crashed worker instances."""
        ts = time.time() if now is None else now
        recovered: list[str] = []

        with self.worker.ledger._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM awe_worker_liveness WHERE worker_id != ? AND status = 'running';",
                (self.worker_id,),
            )
            rows = cursor.fetchall()
            for r in rows:
                last_hb = float(r["last_heartbeat"])
                pid = int(r["pid"])
                dead = False

                if (ts - last_hb) > self.stale_threshold:
                    dead = True
                else:
                    # Check if process exists on host
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        dead = True

                if dead:
                    conn.execute(
                        "UPDATE awe_worker_liveness SET status = 'crashed', last_heartbeat = ? WHERE worker_id = ?;",
                        (ts, r["worker_id"]),
                    )
                    recovered.append(r["worker_id"])

        if recovered:
            # Clean expired leases across the ledger
            self.worker.ledger.clean_expired_leases(now=ts)

        return recovered

    def record_heartbeat(
        self,
        cycles_completed: int,
        status: str = "running",
        last_summary: CycleSummary | None = None,
        now: float | None = None,
    ) -> None:
        """Update heartbeat in ledger database and optional heartbeat file."""
        ts = time.time() if now is None else now
        pid = os.getpid()
        summary_dict = self._summary_dict(last_summary)

        summary_json = json.dumps(summary_dict)

        with self.worker.ledger._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO awe_worker_liveness (
                    worker_id, pid, started_at, last_heartbeat, cycles_completed, status, last_summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    pid = excluded.pid,
                    last_heartbeat = excluded.last_heartbeat,
                    cycles_completed = excluded.cycles_completed,
                    status = excluded.status,
                    last_summary_json = excluded.last_summary_json;
                """,
                (self.worker_id, pid, ts, ts, cycles_completed, status, summary_json),
            )

        if last_summary is not None:
            self._record_cycle_summary(last_summary, now=ts)
            self._write_runtime(now=ts, service_state=status)
        else:
            self._write_runtime(now=ts, service_state=status)

        if self.heartbeat_file:
            try:
                hb_data = {
                    "worker_id": self.worker_id,
                    "pid": pid,
                    "timestamp": ts,
                    "status": status,
                    "cycles_completed": cycles_completed,
                    "last_summary": summary_dict,
                    "runtime": self.get_runtime_status().as_dict(),
                }
                self.heartbeat_file.write_text(json.dumps(hb_data, indent=2), encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _summary_dict(summary: CycleSummary | None) -> dict[str, Any]:
        if summary is None:
            return {}
        wake = summary.wake_receipt
        return {
            "cycle_id": summary.cycle_id,
            "claimed_task": summary.claimed_task,
            "health": summary.health,
            "detail": summary.detail,
            "observed_tasks": summary.observed_tasks,
            "grounding_source": summary.grounding_source,
            "wake_receipt": {
                "success": wake.success,
                "harness": wake.harness,
                "slot_key": wake.slot_key,
                "error_code": wake.error_code,
                "detail": wake.detail,
            } if wake else None,
        }

    @staticmethod
    def _work_state_for_summary(summary: CycleSummary) -> str:
        if summary.health == "idle":
            return "no_eligible_work"
        if summary.health in {"escalated", "owner_gated"}:
            return "owner_gated"
        if summary.health == "refused":
            return "projection_refused" if "PROJECTION" in summary.detail else "blocked"
        if summary.health == "harness_unavailable":
            return "harness_unavailable"
        return "active"

    def _record_cycle_summary(self, summary: CycleSummary, *, now: float) -> None:
        current = self._runtime_row()
        wake_by_harness = self._json_object(current.get("last_wake_json"), {})
        if not isinstance(wake_by_harness, dict):
            wake_by_harness = {}
        wake = summary.wake_receipt
        if wake is not None:
            wake_by_harness[wake.harness] = {
                "timestamp": now,
                "success": wake.success,
                "slot_key": wake.slot_key,
                "error_code": wake.error_code,
                "detail": wake.detail,
            }
        error = ""
        if wake is not None and wake.error_code:
            error = wake.error_code
        elif summary.health in {"refused", "conflict", "escalated", "owner_gated"}:
            error = summary.detail
        self._write_runtime(
            now=now,
            last_cycle_at=now,
            last_claimed_task=summary.claimed_task,
            last_wake_by_harness=wake_by_harness,
            work_state=self._work_state_for_summary(summary),
            last_error=error,
            last_summary=self._summary_dict(summary),
        )

    def get_liveness_status(self) -> list[WorkerLivenessRecord]:
        """Fetch status of all known workers."""
        records: list[WorkerLivenessRecord] = []
        with self.worker.ledger._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM awe_worker_liveness ORDER BY last_heartbeat DESC;")
            for r in cursor.fetchall():
                try:
                    summary = json.loads(r["last_summary_json"])
                except Exception:
                    summary = {}
                records.append(
                    WorkerLivenessRecord(
                        worker_id=r["worker_id"],
                        pid=int(r["pid"]),
                        started_at=float(r["started_at"]),
                        last_heartbeat=float(r["last_heartbeat"]),
                        cycles_completed=int(r["cycles_completed"]),
                        status=r["status"],
                        last_summary=summary,
                    )
                )
        return records

    def stop(self) -> None:
        """Signal the worker loop to stop gracefully."""
        self._stop_requested = True

    def _observe_slots(
        self,
        target_slot: ExecutionSlot | None,
    ) -> tuple[list[ExecutionSlot], str]:
        """Resolve the slots for one poll without inventing execution identity."""
        if target_slot is not None:
            return [target_slot], "explicit target slot"
        if self.target_slots:
            return list(self.target_slots), "configured target slots"
        if self.slot_provider is None:
            # Preserve the old runner's refusal for callers that have not opted
            # into a provider-backed continuation service.
            return [None], "no slot provider configured"

        try:
            observer = getattr(self.slot_provider, "observe_slots", None)
            if callable(observer):
                raw_slots = observer()
            else:
                candidate = getattr(self.slot_provider, "slots", ())
                raw_slots = candidate() if callable(candidate) else candidate
            slots: list[ExecutionSlot] = []
            for raw in raw_slots or ():
                slot = ExecutionSlot.parse(raw)
                if slot not in slots:
                    slots.append(slot)
        except (TypeError, ValueError, RuntimeError) as exc:
            return [], f"SLOT_OBSERVATION_FAILED: {exc}"

        diagnostics = getattr(self.slot_provider, "diagnostics", None)
        detail = ""
        if callable(diagnostics):
            data = diagnostics()
            errors = data.get("errors") if isinstance(data, Mapping) else {}
            if isinstance(errors, Mapping) and errors:
                detail = "; ".join(f"{key}: {value}" for key, value in errors.items())
        return slots, detail

    @staticmethod
    def _is_settled(summary: CycleSummary) -> bool:
        return bool(
            summary.completion_report
            and summary.completion_report.state == "DONE"
        )

    def run(
        self,
        interval_seconds: float = 10.0,
        max_cycles: int | None = None,
        target_slot: ExecutionSlot | None = None,
        dry_run: bool = False,
    ) -> list[CycleSummary]:
        """Execute a multi-slot scheduled continuation loop.

        ``max_cycles`` bounds scheduler polls, not slots.  An explicit
        ``target_slot`` retains the original one-slot behavior.  A
        ``slot_provider`` enables live multi-lane Dispatch observation and one
        bounded immediate re-observation after a task settles, allowing a
        downstream Queue to wake without recursively activating it here.
        """
        self._stop_requested = False
        cycles: list[CycleSummary] = []
        poll_count = 0
        if max_cycles is not None and max_cycles <= 0:
            return cycles

        # Install signal handlers for graceful stop
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _handle_signal(signum: int, frame: Any) -> None:
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            # Signal handling might not be available in non-main threads
            pass

        # Recover from any previous crashes
        self.recover_crashed_workers()

        self.record_heartbeat(cycles_completed=0, status="running")

        try:
            while not self._stop_requested:
                poll_count += 1
                rechecks = 0
                while not self._stop_requested:
                    slots, observation_detail = self._observe_slots(target_slot)
                    if self.slot_provider is not None:
                        self.record_observation(
                            [slot for slot in slots if slot is not None],
                            detail=observation_detail,
                            work_state=(
                                "projection_refused"
                                if observation_detail and not slots
                                else None
                            ),
                        )
                    elif slots and slots[0] is not None:
                        self.record_observation(
                            [slot for slot in slots if slot is not None],
                        )

                    if not slots:
                        self.record_heartbeat(
                            cycles_completed=len(cycles),
                            status="running",
                            now=time.time(),
                        )
                        break

                    settled = False
                    for slot in slots:
                        if self._stop_requested:
                            break
                        summary = self.worker.run_cycle(
                            worker_id=self.worker_id,
                            target_slot=slot,
                            dry_run=dry_run,
                        )
                        cycles.append(summary)
                        settled = settled or self._is_settled(summary)
                        self.record_heartbeat(
                            cycles_completed=len(cycles),
                            status="running",
                            last_summary=summary,
                        )

                    if (
                        self.slot_provider is None
                        or not settled
                        or rechecks >= self.settlement_rechecks
                    ):
                        break
                    # Re-read fixed Dispatch pages immediately once after
                    # settlement.  The coordinator still owns integration and
                    # downstream activation; this loop only observes/wakes.
                    rechecks += 1

                if max_cycles is not None and poll_count >= max_cycles:
                    break

                # Sleep in short increments to remain responsive to stop requests
                sleep_remaining = interval_seconds
                while sleep_remaining > 0 and not self._stop_requested:
                    step = min(sleep_remaining, 0.5)
                    time.sleep(step)
                    sleep_remaining -= step

        finally:
            self.record_heartbeat(
                cycles_completed=len(cycles),
                status="stopped" if not self._stop_requested else "gracefully_stopped",
                last_summary=cycles[-1] if cycles else None,
            )
            # Restore signal handlers
            try:
                signal.signal(signal.SIGINT, prev_sigint)
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, AttributeError):
                pass

        return cycles


class AWEContinuationSupervisor(AWEScheduledRunner):
    """Named service surface for live cross-lane continuation.

    The implementation intentionally remains the existing scheduled runner:
    there is one lease ledger, one projection preflight, one cadence
    coordinator, and one service heartbeat.  The subclass makes the supported
    always-on entry point explicit without introducing a second scheduler.
    """

    pass
