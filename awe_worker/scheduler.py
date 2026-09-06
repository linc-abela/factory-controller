"""Scheduled worker service, liveness watchdog, and crash recovery.

Provides a bounded scheduled execution loop with heartbeat tracking,
stale worker detection, graceful signal handling, and clean crash recovery.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

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


class AWEScheduledRunner:
    """Bounded scheduled execution service for Autonomous AWE Worker."""

    def __init__(
        self,
        worker: AWEAutonomousWorker,
        worker_id: str = "awe-worker-1",
        heartbeat_file: str | Path | None = None,
        stale_threshold_seconds: float = 90.0,
    ) -> None:
        self.worker = worker
        self.worker_id = worker_id
        self.heartbeat_file = Path(heartbeat_file) if heartbeat_file else None
        self.stale_threshold = stale_threshold_seconds
        self._stop_requested = False
        self._init_liveness_table()

    def _init_liveness_table(self) -> None:
        with self.worker.ledger._get_connection() as conn:
            conn.executescript(LIVENESS_SCHEMA)

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
        summary_dict = {
            "cycle_id": last_summary.cycle_id if last_summary else None,
            "claimed_task": last_summary.claimed_task if last_summary else None,
            "health": last_summary.health if last_summary else "unknown",
            "detail": last_summary.detail if last_summary else "",
        } if last_summary else {}

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

        if self.heartbeat_file:
            try:
                hb_data = {
                    "worker_id": self.worker_id,
                    "pid": pid,
                    "timestamp": ts,
                    "status": status,
                    "cycles_completed": cycles_completed,
                    "last_summary": summary_dict,
                }
                self.heartbeat_file.write_text(json.dumps(hb_data, indent=2), encoding="utf-8")
            except Exception:
                pass

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

    def run(
        self,
        interval_seconds: float = 10.0,
        max_cycles: int | None = None,
        target_slot: ExecutionSlot | None = None,
        dry_run: bool = False,
    ) -> list[CycleSummary]:
        """Execute a scheduled worker loop with bounded intervals."""
        self._stop_requested = False
        cycles: list[CycleSummary] = []
        cycle_count = 0

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
                cycle_count += 1
                summary = self.worker.run_cycle(
                    worker_id=self.worker_id,
                    target_slot=target_slot,
                    dry_run=dry_run,
                )
                cycles.append(summary)

                self.record_heartbeat(
                    cycles_completed=cycle_count,
                    status="running",
                    last_summary=summary,
                )

                if max_cycles is not None and cycle_count >= max_cycles:
                    break

                # Sleep in short increments to remain responsive to stop requests
                sleep_remaining = interval_seconds
                while sleep_remaining > 0 and not self._stop_requested:
                    step = min(sleep_remaining, 0.5)
                    time.sleep(step)
                    sleep_remaining -= step

        finally:
            self.record_heartbeat(
                cycles_completed=cycle_count,
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
