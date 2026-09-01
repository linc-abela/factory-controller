"""A two-provider execution layer with injectable availability, for route tests.

The adapter stands in for `factory-bridge`.  It never lets the Controller see a
provider name it was not given, and it reports availability the way the real
layer does: a refusal plus a receipt that either proves no process started or
declines to say.  That second case is the one the Controller must treat as
unsafe, so it is directly injectable here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore


ALPHA = "provider-alpha"
BETA = "provider-beta"


class ProcessDeath(BaseException):
    """Stands in for the worker process being killed mid-step."""


class LayerAdapter:
    def __init__(self, *, proven_unavailable=(), silent_unavailable=(), mode="fixture",
                 usage=None, echo_key=True, crash_on=None, gates_pass=True,
                 evidence=True, verified=True) -> None:
        self.proven_unavailable = set(proven_unavailable)
        self.silent_unavailable = set(silent_unavailable)
        self.mode = mode
        self.usage = usage
        self.echo_key = echo_key
        self.crash_on = crash_on
        self.gates_pass = gates_pass
        self.evidence = evidence
        self.verified = verified
        self.dispatches: list[dict[str, Any]] = []
        self.crashed = False

    # -- execution layer ------------------------------------------------ #

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        if step == self.crash_on and not self.crashed:
            self.crashed = True
            raise ProcessDeath(step)
        if step == "context":
            # Keep generic engine/autopilot tests on the deterministic fixture;
            # the real Factory lifecycle injects the checked-in Broker builder.
            from factory_controller.safe_provider import build_context
            result = build_context(value["context_request"])
            if result.get("status") == "built":
                result["measurement"] = {
                    **result.get("measurement", {}),
                    "baseline_context_bytes": 1,
                    "baseline_context_files": 1,
                    "selected_context_bytes": 1,
                }
            return result
        if step == "dispatch":
            return self._dispatch(operation_key, value)
        if step == "verify":
            return {"verified": self.verified, "diagnostic": None if self.verified else "ANCESTRY_FAILED"}
        if step == "evaluate":
            declared = value["mission"].get("acceptance_gate_ids") or ["G"]
            return {"passed": self.gates_pass,
                    "gate_outcomes": [{"gate_id": gate, "passed": self.gates_pass, "detail": "layer"}
                                      for gate in declared]}
        if step == "evidence":
            return {"accepted": self.evidence, "retryable": False,
                    "evidence_pointer": "e" * 64,
                    "diagnostic": None if self.evidence else "EVIDENCE_REJECTED"}
        return {"status": "unknown"}

    def _dispatch(self, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        route = value["route"]
        profile = route["provider_profile"]
        self.dispatches.append(dict(route))
        receipt: dict[str, Any] = {
            "provider_profile": profile,
            "provider": None if profile is None else profile + "/v1",
            "execution_mode": self.mode,
            "duration_ms": 12,
            "usage": self.usage,
        }
        if self.echo_key:
            receipt["idempotency_key"] = route["idempotency_key"]
        if profile in self.silent_unavailable:
            # The layer declines without saying whether anything began.
            return {"status": "provider_unavailable", "diagnostic": "LAYER_SILENT",
                    "receipt": receipt}
        if profile in self.proven_unavailable:
            return {"status": "provider_unavailable", "diagnostic": "PROFILE_UNAVAILABLE",
                    "receipt": {**receipt, "process_started": False,
                                "refusal_code": "PROFILE_UNAVAILABLE"}}
        return {"status": "completed", "candidate_sha": "a" * 40,
                "execution_id": operation_key,
                "receipt": {**receipt, "process_started": True}}


def mission_payload(**extra: Any) -> dict[str, Any]:
    payload = {
        "work_item_id": "SF-135-ROUTE",
        "execution_mode": "fixture",
        "acceptance_gate_ids": ["G"],
        "provider_candidates": [
            {"profile": ALPHA, "capabilities": ["implement"]},
            {"profile": BETA, "capabilities": ["implement"]},
        ],
    }
    payload.update(extra)
    return payload


class RouteTestCase:
    """Mixin providing a temporary store and a Controller over one adapter."""

    def build(self, adapter, *, lease_seconds: float = 5, max_attempts: int = 3):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)  # type: ignore[attr-defined]
        path = Path(temp.name) / "controller.db"
        store = MissionStore(path)
        controller = Controller(
            store, adapter,
            retry_policy=RetryPolicy(max_attempts=max_attempts, base_delay_seconds=0),
            lease_seconds=lease_seconds)
        return controller, store, path

    @staticmethod
    def reopen(path, adapter, *, lease_seconds: float = 5):
        """A replacement worker process over the same durable database."""

        return Controller(MissionStore(path), adapter,
                          retry_policy=RetryPolicy(base_delay_seconds=0),
                          lease_seconds=lease_seconds)


class Clock:
    """A hand-wound clock.  Ageing is a function of time, so time is an input."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class PortfolioTestCase(RouteTestCase):
    """A store with a controllable clock, plus the two registry conveniences."""

    def portfolio_store(self, adapter=None, *, clock=None, **policy):
        import tempfile
        from pathlib import Path
        from factory_controller import portfolio
        from factory_controller.engine import Controller, RetryPolicy
        from factory_controller.store import MissionStore

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)  # type: ignore[attr-defined]
        path = Path(temp.name) / "controller.db"
        clock = clock or Clock()
        store = MissionStore(path, clock=clock)
        if policy:
            store.set_portfolio_policy(portfolio.PortfolioPolicy(**policy))
        controller = Controller(store, adapter or LayerAdapter(),
                                retry_policy=RetryPolicy(base_delay_seconds=0),
                                lease_seconds=0)
        return controller, store, clock, path

    @staticmethod
    def register(store, project_id, **policy):
        from factory_controller import portfolio
        policy.setdefault("repository", "repo://" + project_id)
        return store.register_project(portfolio.ProjectPolicy(project_id=project_id, **policy))

    @staticmethod
    def submit(controller, key, project_id=None, **extra):
        payload = {"work_item_id": key, "execution_mode": "fixture",
                   "acceptance_gate_ids": ["G"]}
        if project_id is not None:
            payload["project_id"] = project_id
        payload.update(extra)
        mission, _ = controller.submit(payload, key)
        return mission["id"]
