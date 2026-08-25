"""Restartable mission runner composed over provider-neutral adapter steps."""

from __future__ import annotations

from dataclasses import dataclass
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
        output = self.adapter.execute(name, started["operation_key"], value)
        self.store.complete_step(mission["id"], mission["lease_token"], name, output)
        return output

    def work_once(self, worker_id: str) -> dict[str, Any] | None:
        self.store.recover_stale()
        mission = self.store.claim(worker_id, lease_seconds=self.lease_seconds)
        if mission is None:
            return None
        mission_id, token = mission["id"], mission["lease_token"]
        try:
            current = self.store.get(mission_id)
            if current and current["cancel_requested"]:
                self.store.transition(mission_id, token, "CANCELLED", reason="OPERATOR_CANCELLED", release_lease=True)
                return self.store.get(mission_id)
            self.store.transition(mission_id, token, "IN_PROGRESS")
            dispatch = self._step(mission, "dispatch", {"mission": mission["payload"]})
            status = dispatch.get("status")
            if status in {"blocked", "retryable_error"}:
                raise RetryableFailure(dispatch.get("diagnostic", status))
            if status != "completed" or not dispatch.get("candidate_sha"):
                raise NonRetryableFailure(dispatch.get("diagnostic", "DISPATCH_REFUSED"))
            self.store.transition(mission_id, token, "AWAITING_VERIFICATION", detail={"candidate_sha": dispatch["candidate_sha"]})
            verification = self._step(mission, "verify", {"mission": mission["payload"], "dispatch": dispatch})
            if not verification.get("verified"):
                raise NonRetryableFailure(verification.get("diagnostic", "CANDIDATE_VERIFICATION_FAILED"))
            evidence = self._step(mission, "evidence", {"mission": mission["payload"], "dispatch": dispatch, "verification": verification})
            if not evidence.get("accepted"):
                if evidence.get("retryable"):
                    raise RetryableFailure(evidence.get("diagnostic", "EVIDENCE_BINDING_FAILED"))
                raise NonRetryableFailure(evidence.get("diagnostic", "EVIDENCE_REJECTED"))
            result = {"dispatch": dispatch, "verification": verification, "evidence": evidence}
            self.store.transition(mission_id, token, "DONE", result=result, release_lease=True)
        except RetryableFailure as exc:
            self.store.retry(mission_id, token, str(exc), self.retry_policy.delay(mission["attempt_count"]))
        except NonRetryableFailure as exc:
            self.store.transition(mission_id, token, "FAILED", reason=str(exc), release_lease=True)
        return self.store.get(mission_id)

