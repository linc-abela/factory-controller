"""Restartable mission runner composed over provider-neutral adapter steps."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Protocol

from .store import MissionStore


class StepAdapter(Protocol):
    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0

    def delay(self, attempt: int) -> float:
        return self.base_delay_seconds * (2 ** max(0, attempt - 1))


class RetryableFailure(RuntimeError):
    pass


class NonRetryableFailure(RuntimeError):
    pass


class Controller:
    def __init__(self, store: MissionStore, adapter: StepAdapter,
                 *, retry_policy: RetryPolicy = RetryPolicy(), lease_seconds: float = 30) -> None:
        self.store = store
        self.adapter = adapter
        self.retry_policy = retry_policy
        self.lease_seconds = lease_seconds

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        return self.store.submit(payload, idempotency_key, max_attempts=self.retry_policy.max_attempts)

    def _step(self, mission: dict[str, Any], name: str, value: dict[str, Any]) -> dict[str, Any]:
        started = self.store.begin_step(mission["id"], mission["lease_token"], name, value)
        if started["status"] == "COMPLETED":
            return started["output"]
        self.store.renew(mission["id"], mission["lease_token"], self.lease_seconds)
        stopped = threading.Event()
        heartbeat_error: list[BaseException] = []

        def heartbeat() -> None:
            interval = max(0.01, min(5.0, self.lease_seconds / 3))
            while not stopped.wait(interval):
                try:
                    self.store.renew(mission["id"], mission["lease_token"], self.lease_seconds)
                except BaseException as exc:
                    heartbeat_error.append(exc)
                    return

        thread = threading.Thread(target=heartbeat, name=f"lease-heartbeat-{mission['id']}", daemon=True)
        if self.lease_seconds > 0:
            thread.start()
        try:
            output = self.adapter.execute(name, started["operation_key"], value)
        finally:
            stopped.set()
            if thread.is_alive():
                thread.join(timeout=min(1.0, self.lease_seconds))
        if heartbeat_error:
            raise heartbeat_error[0]
        if not (isinstance(output, dict) and output.get("status") in {"retryable_error"}):
            self.store.complete_step(mission["id"], mission["lease_token"], name, output)
        return output

    def _cancelled(self, mission_id: str, lease_token: str) -> bool:
        current = self.store.get(mission_id)
        if current and current["cancel_requested"]:
            target = "cancelled" if current["state"] == "dispatching" else "escalated"
            reason = "OPERATOR_CANCELLED" if target == "cancelled" else "CANCELLATION_AFTER_SIDE_EFFECT"
            self.store.transition(mission_id, lease_token, target, reason=reason, release_lease=True)
            return True
        return False

    def work_once(self, worker_id: str) -> dict[str, Any] | None:
        self.store.recover_stale()
        mission = self.store.claim(worker_id, lease_seconds=self.lease_seconds)
        if mission is None:
            return None
        mission_id, token = mission["id"], mission["lease_token"]
        try:
            resume_state = mission["state"]
            current = self.store.get(mission_id)
            if current and current["cancel_requested"]:
                self.store.transition(mission_id, token, "cancelled", reason="OPERATOR_CANCELLED", release_lease=True)
                return self.store.get(mission_id)
            dispatch = self._step(mission, "dispatch", {"mission": mission["payload"]})
            if self._cancelled(mission_id, token):
                return self.store.get(mission_id)
            status = dispatch.get("status")
            if status == "retryable_error":
                raise RetryableFailure(dispatch.get("diagnostic", status))
            if status == "blocked":
                self.store.transition(mission_id, token, "escalated", reason=dispatch.get("diagnostic", status), release_lease=True)
                return self.store.get(mission_id)
            if status != "completed" or not dispatch.get("candidate_sha"):
                raise NonRetryableFailure(dispatch.get("diagnostic", "DISPATCH_REFUSED"))
            if resume_state == "dispatching":
                self.store.transition(mission_id, token, "dispatched", detail={"candidate_sha": dispatch["candidate_sha"], "execution_id": dispatch.get("execution_id")})
            verification = self._step(mission, "verify", {"mission": mission["payload"], "dispatch": dispatch})
            if self._cancelled(mission_id, token):
                return self.store.get(mission_id)
            if not verification.get("verified"):
                raise NonRetryableFailure(verification.get("diagnostic", "CANDIDATE_VERIFICATION_FAILED"))
            if resume_state in {"dispatching", "dispatched"}:
                self.store.transition(mission_id, token, "candidate_verified", detail={"candidate_sha": dispatch["candidate_sha"]})
            evaluation = self._step(mission, "evaluate", {"mission": mission["payload"], "dispatch": dispatch, "verification": verification})
            if not evaluation.get("passed"):
                self.store.transition(mission_id, token, "escalated", reason=evaluation.get("diagnostic", "ACCEPTANCE_GATE_FAILED"), release_lease=True)
                return self.store.get(mission_id)
            if resume_state in {"dispatching", "dispatched", "candidate_verified"}:
                self.store.transition(mission_id, token, "evaluated", detail={"gate_outcomes": evaluation.get("gate_outcomes", [])})
            evidence = self._step(mission, "evidence", {"mission": mission["payload"], "dispatch": dispatch, "verification": verification, "evaluation": evaluation})
            if not evidence.get("accepted"):
                if evidence.get("retryable"):
                    raise RetryableFailure(evidence.get("diagnostic", "EVIDENCE_BINDING_FAILED"))
                raise NonRetryableFailure(evidence.get("diagnostic", "EVIDENCE_REJECTED"))
            if resume_state != "evidence_sealed":
                self.store.transition(mission_id, token, "evidence_sealed", detail={"evidence_pointer": evidence.get("evidence_pointer")})
            result = {"dispatch": dispatch, "verification": verification, "evaluation": evaluation, "evidence": evidence}
            self.store.transition(mission_id, token, "completed", result=result, release_lease=True)
        except RetryableFailure as exc:
            current = self.store.get(mission_id)
            if current and current["state"] == "dispatching":
                self.store.retry(mission_id, token, str(exc), self.retry_policy.delay(mission["attempt_count"]))
            else:
                self.store.transition(mission_id, token, "escalated", reason=f"RETRY_AFTER_SIDE_EFFECT: {exc}", release_lease=True)
        except NonRetryableFailure as exc:
            current = self.store.get(mission_id)
            target = "refused" if current and current["state"] == "dispatching" else "failed"
            self.store.transition(mission_id, token, target, reason=str(exc), release_lease=True)
        return self.store.get(mission_id)
