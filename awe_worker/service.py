"""Safe lifecycle wrapper for the single AWE continuation supervisor.

The lifecycle wrapper owns only a manifest, one PID receipt, and the existing
``AWEContinuationSupervisor`` process.  It does not create another scheduler or
copy an authentication token into durable state.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

SERVICE_SCHEMA = "factory-controller/awe-continuation-service/1.0"


class ServiceError(ValueError):
    """A lifecycle operation that cannot be performed safely."""


class ContinuationService:
    """Manage exactly one foreground continuation supervisor process."""

    def __init__(
        self,
        db_path: str | Path,
        state_dir: str | Path | None = None,
        worker_id: str = "awe-continuation-1",
    ) -> None:
        self.db_path = str(db_path)
        self.worker_id = worker_id
        default_dir = Path(self.db_path).resolve().parent / ".awe-worker"
        self.state_dir = Path(state_dir).expanduser() if state_dir else default_dir
        if not self.worker_id or "/" in self.worker_id or "\\" in self.worker_id:
            raise ServiceError("worker_id must be a single safe name")
        self.manifest_path = self.state_dir / f"{self.worker_id}.service.json"
        self.pid_path = self.state_dir / f"{self.worker_id}.pid.json"
        self.log_path = self.state_dir / f"{self.worker_id}.log"

    def install(
        self,
        command: Sequence[str],
        *,
        working_dir: str | Path,
        interval_seconds: float,
        apply: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Plan or write an idempotent manifest; never starts the process."""
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ServiceError("service command must be a non-empty argument array")
        working = Path(working_dir).expanduser().resolve()
        if not working.is_dir():
            raise ServiceError(f"working directory does not exist: {working}")
        if interval_seconds < 0:
            raise ServiceError("interval_seconds cannot be negative")
        manifest = {
            "schema_version": SERVICE_SCHEMA,
            "worker_id": self.worker_id,
            "db_path": self.db_path,
            "command": list(command),
            "working_dir": str(working),
            "interval_seconds": interval_seconds,
            "pid_path": str(self.pid_path),
            "log_path": str(self.log_path),
        }
        existing = self._read_manifest()
        unchanged = bool(
            existing
            and self.manifest_path.is_file()
            and all(existing.get(key) == value for key, value in manifest.items())
        )
        result = {
            "schema_version": SERVICE_SCHEMA,
            "worker_id": self.worker_id,
            "manifest_path": str(self.manifest_path),
            "pid_path": str(self.pid_path),
            "log_path": str(self.log_path),
            "manifest": manifest,
            "outcome": "unchanged" if unchanged else "planned",
            "applied": False,
            "starts_process": False,
        }
        if not apply or unchanged:
            return result
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(
                {**manifest, "installed_at": time.time() if now is None else now},
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        result["outcome"] = "installed"
        result["applied"] = True
        return result

    def start(self) -> dict[str, Any]:
        """Start the installed command once, refusing a duplicate live PID."""
        manifest = self._read_manifest()
        if not manifest or manifest.get("schema_version") != SERVICE_SCHEMA:
            return {
                "ok": False,
                "code": "SERVICE_NOT_INSTALLED",
                "detail": f"install the continuation service first: {self.manifest_path}",
            }
        current = self._read_pid()
        if current and self._pid_alive(int(current.get("pid", 0))):
            return {
                "ok": False,
                "code": "SERVICE_ALREADY_RUNNING",
                "pid": current.get("pid"),
                "worker_id": self.worker_id,
            }
        ledger_pid = self._running_liveness_pid()
        if ledger_pid is not None:
            return {
                "ok": False,
                "code": "SERVICE_ALREADY_RUNNING",
                "pid": ledger_pid,
                "worker_id": self.worker_id,
                "detail": "the durable worker liveness record already owns this service identity",
            }
        if current:
            self._remove_pid_receipt()

        command = manifest.get("command")
        working_dir = manifest.get("working_dir")
        if not isinstance(command, list) or not command or not isinstance(working_dir, str):
            return {"ok": False, "code": "SERVICE_MANIFEST_INVALID"}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            log = self.log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                cwd=working_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            try:
                log.close()
            except UnboundLocalError:
                pass
            return {"ok": False, "code": "SERVICE_START_FAILED", "detail": str(exc)}
        finally:
            # The child owns the file descriptor after Popen; closing the
            # parent's handle avoids leaking one descriptor per restart.
            try:
                log.close()
            except UnboundLocalError:
                pass
        self.pid_path.write_text(
            json.dumps(
                {
                    "schema_version": SERVICE_SCHEMA,
                    "worker_id": self.worker_id,
                    "pid": process.pid,
                    "started_at": time.time(),
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {"ok": True, "code": "SERVICE_STARTED", "pid": process.pid}

    def stop(self) -> dict[str, Any]:
        """Request a graceful stop for the PID receipt owned by this service."""
        receipt = self._read_pid()
        if not receipt:
            return {"ok": True, "code": "SERVICE_NOT_RUNNING", "worker_id": self.worker_id}
        pid = int(receipt.get("pid", 0))
        if not self._pid_alive(pid):
            self._remove_pid_receipt()
            return {"ok": True, "code": "SERVICE_NOT_RUNNING", "worker_id": self.worker_id}
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return {"ok": False, "code": "SERVICE_STOP_FAILED", "detail": str(exc), "pid": pid}
        return {"ok": True, "code": "SERVICE_STOP_REQUESTED", "pid": pid}

    def restart(self) -> dict[str, Any]:
        """Stop the owned process, then start the same installed command."""
        stopped = self.stop()
        if not stopped.get("ok"):
            return stopped
        if stopped.get("code") == "SERVICE_STOP_REQUESTED":
            pid = int(stopped["pid"])
            deadline = time.monotonic() + 5.0
            while self._pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
        return self.start()

    def status(self) -> dict[str, Any]:
        """Report manifest and PID state without starting or stopping anything."""
        manifest = self._read_manifest()
        receipt = self._read_pid()
        pid = int(receipt.get("pid", 0)) if receipt else 0
        running = bool(pid and self._pid_alive(pid))
        if not running:
            ledger_pid = self._running_liveness_pid()
            if ledger_pid is not None:
                pid = ledger_pid
                running = True
        return {
            "ok": True,
            "schema_version": SERVICE_SCHEMA,
            "worker_id": self.worker_id,
            "manifest_present": manifest is not None,
            "manifest_path": str(self.manifest_path),
            "pid_path": str(self.pid_path),
            "log_path": str(self.log_path),
            "service_state": "running" if running else "stopped",
            "pid": pid if running else None,
            "installed_command": manifest.get("command") if manifest else None,
        }

    def _read_manifest(self) -> dict[str, Any] | None:
        return self._read_json(self.manifest_path)

    def _read_pid(self) -> dict[str, Any] | None:
        return self._read_json(self.pid_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _remove_pid_receipt(self) -> None:
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

    def _running_liveness_pid(self) -> int | None:
        """Catch a live owned worker even if its PID receipt was interrupted."""
        if self.db_path == ":memory:":
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT pid FROM awe_worker_liveness "
                    "WHERE worker_id = ? AND status = 'running' "
                    "ORDER BY last_heartbeat DESC LIMIT 1;",
                    (self.worker_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if not row:
            return None
        pid = int(row[0])
        return pid if self._pid_alive(pid) else None
