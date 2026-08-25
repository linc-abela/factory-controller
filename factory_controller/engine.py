"""Restartable mission runner composed over provider-neutral adapter steps.

The Controller decides *what* must happen and *whether it may*; it never decides
*how* a provider is invoked.  Routing here is ordering and admission over opaque
profile names, and the one rule that matters is the side-effect boundary: a
provider may be swapped freely until a process might have started, and never
afterwards unless the execution layer proves none did.
"""

from __future__ import annotations

from dataclasses import asdict
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from . import routing
from .routing import ExecutionPolicy, PolicyError, Selection
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


#: Used when the mission declares no candidate profiles at all.  The execution
#: layer then picks, which is the Stage-2 behaviour and stays legal: one leg,
#: recorded like any other, so route history is uniform across both shapes.
LAYER_DEFAULT = "layer_default"


def _layer_default_selection() -> Selection:
    return Selection(None, LAYER_DEFAULT, ())


class Controller:
    def __init__(self, store: MissionStore, adapter: StepAdapter,
                 *, retry_policy: RetryPolicy = RetryPolicy(), lease_seconds: float = 30) -> None:
        self.store = store
        self.adapter = adapter
        self.retry_policy = retry_policy
        self.lease_seconds = lease_seconds

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        # Refuse an unusable mission at submission rather than at dispatch. The
        # same check runs again in work_once, because a mission may also reach
        # the store directly.
        self.validate(payload, idempotency_key)
        return self.store.submit(payload, idempotency_key, max_attempts=self.retry_policy.max_attempts)

    # ------------------------------------------------------------------ #
    # admission
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate(payload: dict[str, Any], idempotency_key: str) -> str:
        """Check what must hold before a mission may ever be dispatched.

        Returns the declared execution mode.  A *real* mission carries three
        obligations a fixture mission does not, and all three were open holes
        after SF-134: its idempotency key must be the one Evidence Core will
        accept, it must name the acceptance gates its target repository
        declares, and it must be proven real by whatever executes it.
        """

        mode = payload.get("execution_mode", "fixture")
        if mode not in routing.EXECUTION_MODES:
            raise NonRetryableFailure("INVALID_EXECUTION_MODE: %r" % (mode,))
        try:
            ExecutionPolicy.from_payload(payload)
            routing.candidates_from_payload(payload)
        except PolicyError as exc:
            raise NonRetryableFailure("INVALID_EXECUTION_POLICY: %s" % exc) from exc
        if mode != "real":
            return mode
        work_item_id = payload.get("work_item_id")
        manifest = payload.get("context_manifest_hash")
        if not isinstance(work_item_id, str) or not work_item_id:
            raise NonRetryableFailure("REAL_MISSION_WORK_ITEM_MISSING")
        if not isinstance(manifest, str) or not manifest:
            raise NonRetryableFailure("REAL_MISSION_CONTEXT_MANIFEST_MISSING")
        expected = routing.expected_idempotency_key(work_item_id, manifest)
        if idempotency_key != expected:
            # factory-evidence-core refuses IDEMPOTENCY_BINDING_MISMATCH for any
            # other value, so this mission could never reach the bridge under the
            # identity the Controller would later use to recover it.
            raise NonRetryableFailure(
                "IDEMPOTENCY_KEY_NOT_BRIDGE_DERIVABLE: %s != %s" % (idempotency_key, expected))
        if not _declared_gates(payload):
            raise NonRetryableFailure("ACCEPTANCE_GATE_UNDECLARED")
        return mode

    # ------------------------------------------------------------------ #
    # steps
    # ------------------------------------------------------------------ #

    def _step(self, mission: dict[str, Any], name: str, value: dict[str, Any],
              *, memo_value: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one durable step, or return the output a previous run recorded.

        ``memo_value`` is the step's durable identity; ``value`` is what the
        adapter is handed.  They differ only for dispatch, whose route changes
        between fallback legs while the step itself stays the same operation.
        """

        started = self.store.begin_step(mission["id"], mission["lease_token"], name,
                                        value if memo_value is None else memo_value)
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
        incomplete = {"retryable_error", routing.PROVIDER_UNAVAILABLE}
        if not (isinstance(output, dict) and output.get("status") in incomplete):
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

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #

    def _record(self, mission: dict[str, Any], selection: Selection,
                receipt: routing.Receipt) -> None:
        self.store.record_run(
            mission["id"], mission["attempt_count"],
            {"reason": selection.reason,
             "considered": [asdict(item) for item in selection.considered]},
            _receipt_row(receipt), mission["idempotency_key"])

    def _refuse_switch(self, mission: dict[str, Any], code: str, profile: str | None,
                       detail: str) -> None:
        self.store.log(mission["id"], "ROUTE_SWITCH_REFUSED", {
            "attempt": mission["attempt_count"], "code": code,
            "profile": profile, "detail": detail})

    def _dispatch(self, mission: dict[str, Any], resume_state: str) -> dict[str, Any]:
        """Produce this mission's dispatch result, routing only where it is safe."""

        payload = mission["payload"]
        policy = ExecutionPolicy.from_payload(payload)
        candidates = routing.candidates_from_payload(payload)
        prior = self.store.runs(mission["id"])
        committed = [leg for leg in prior if leg["process_started"] is not False]

        if resume_state != "dispatching" or committed:
            return self._recover(mission, committed, prior)

        spent = [_receipt_value(leg["receipt"]) for leg in prior]
        attempted: list[str] = []
        while True:
            # The budget gate runs before selection: a hard ceiling is an Owner
            # constraint on dispatching at all, not on which provider is picked.
            budget = routing.accumulate(policy, spent)
            refusal = routing.refuse_dispatch(budget)
            if refusal:
                gate = Selection(None, "budget_gate", ())
                self._record(mission, gate, routing.unserved_receipt(gate, attempted, refusal))
                raise NonRetryableFailure(
                    "%s: known spend %s of ceiling %s" % (refusal, budget.known_spend, budget.ceiling))
            selection = routing.select(policy, candidates, attempted) if candidates \
                else _layer_default_selection()
            if candidates and not selection.selected:
                code = selection.refusal_code or "NO_ADMISSIBLE_PROVIDER"
                self._record(mission, selection, routing.unserved_receipt(selection, attempted, code))
                raise NonRetryableFailure("%s: considered %d candidate(s)" % (code, len(candidates)))
            response = self._step(
                mission, "dispatch",
                {"mission": payload, "route": _route(selection, attempted, mission, False)},
                memo_value={"mission": payload})
            receipt = routing.receipt_from_response(response, selection, attempted)
            self._record(mission, selection, receipt)
            spent.append(receipt)
            if response.get("status") != routing.PROVIDER_UNAVAILABLE:
                self._verify_receipt(mission, receipt, response)
                return response
            if receipt.side_effect_possible:
                # The layer neither served the request nor proved nothing ran.
                # An unproven negative is not a proof, so this mission stops here
                # rather than handing the same work to a second provider.
                self._refuse_switch(mission, "PROVIDER_SWITCH_AFTER_SIDE_EFFECT",
                                    receipt.profile, "process_started=%r" % (receipt.process_started,))
                raise NonRetryableFailure(
                    "PROVIDER_SWITCH_AFTER_SIDE_EFFECT: %s did not prove no process started"
                    % (receipt.profile or LAYER_DEFAULT))
            attempted.append(selection.profile or LAYER_DEFAULT)

    def _recover(self, mission: dict[str, Any], committed: list[dict[str, Any]],
                 prior: list[dict[str, Any]]) -> dict[str, Any]:
        """Resume a mission that already crossed the boundary.  Never reroute.

        The recorded step output is preferred; when a crash landed between the
        provider running and the output being recorded, the layer is asked to
        return the result bound to this idempotency key, on the same profile.
        """

        profile = committed[-1]["profile"] if committed else None
        selection = Selection(profile, "recover_existing_result", ())
        response = self._step(
            mission, "dispatch",
            {"mission": mission["payload"], "route": _route(selection, (), mission, True)},
            memo_value={"mission": mission["payload"]})
        receipt = routing.receipt_from_response(response, selection, ())
        if committed and receipt.profile not in (None, profile):
            self._refuse_switch(mission, "PROVIDER_SWITCH_AFTER_SIDE_EFFECT", receipt.profile,
                                "recovery returned %r for a mission dispatched on %r"
                                % (receipt.profile, profile))
            raise NonRetryableFailure(
                "PROVIDER_SWITCH_AFTER_SIDE_EFFECT: recovery changed provider %r -> %r"
                % (profile, receipt.profile))
        if len(prior) == len(self.store.runs(mission["id"])):
            self._record(mission, selection, receipt)
        if response.get("status") == routing.PROVIDER_UNAVAILABLE:
            raise NonRetryableFailure("DISPATCHED_RESULT_UNRECOVERABLE: %s"
                                      % (receipt.refusal_code or routing.PROVIDER_UNAVAILABLE))
        self._verify_receipt(mission, receipt, response)
        return response

    def _verify_receipt(self, mission: dict[str, Any], receipt: routing.Receipt,
                        response: dict[str, Any]) -> None:
        """The two SF-134 dispatch guards, applied to every served leg.

        Both are equality checks, so neither default direction can launder a
        run: a fixture receipt fails a real mission and a real receipt fails a
        fixture one.
        """

        declared = mission["payload"].get("execution_mode", "fixture")
        reported = receipt.execution_mode
        if reported in routing.EXECUTION_MODES and reported != declared:
            raise NonRetryableFailure(
                "EXECUTION_MODE_MISMATCH: mission declares %s, layer reported %s"
                % (declared, reported))
        if declared == "real":
            if reported != "real":
                raise NonRetryableFailure("EXECUTION_MODE_UNPROVEN: layer reported %s" % reported)
            if receipt.idempotency_key is None:
                raise NonRetryableFailure("IDEMPOTENCY_KEY_UNPROVEN: layer echoed no key")
        if receipt.idempotency_key not in (None, mission["idempotency_key"]):
            raise NonRetryableFailure(
                "IDEMPOTENCY_KEY_DIVERGED: layer bound %s, mission is %s"
                % (receipt.idempotency_key, mission["idempotency_key"]))

    # ------------------------------------------------------------------ #
    # the mission
    # ------------------------------------------------------------------ #

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
            self.validate(mission["payload"], mission["idempotency_key"])
            dispatch = self._dispatch(mission, resume_state)
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
            gate_refusal = _gate_refusal(mission["payload"], evaluation)
            if gate_refusal:
                self.store.transition(mission_id, token, "escalated", reason=gate_refusal, release_lease=True)
                return self.store.get(mission_id)
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _declared_gates(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("acceptance_gate_ids") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _gate_refusal(payload: dict[str, Any], evaluation: dict[str, Any]) -> str | None:
    """Every gate the mission declared must have been run and must have passed.

    Before this, an evaluator could return one placeholder outcome naming a gate
    nobody asked for and the mission would complete.  A declared gate with no
    outcome is `not_run`, and `not_run` is not a pass.
    """

    declared = _declared_gates(payload)
    if not declared:
        return None
    outcomes = evaluation.get("gate_outcomes") or ()
    if not isinstance(outcomes, (list, tuple)):
        return "ACCEPTANCE_GATE_UNEVALUATED: gate_outcomes is not a list"
    seen = {outcome.get("gate_id"): outcome for outcome in outcomes if isinstance(outcome, dict)}
    missing = [gate for gate in declared if gate not in seen]
    if missing:
        return "ACCEPTANCE_GATE_UNEVALUATED: " + ", ".join(missing)
    failed = [gate for gate in declared if seen[gate].get("passed") is not True]
    if failed:
        return "ACCEPTANCE_GATE_FAILED: " + ", ".join(failed)
    return None


def _route(selection: Selection, attempted: tuple | list, mission: dict[str, Any],
           recover_only: bool) -> dict[str, Any]:
    """What the execution layer is told about this leg.  No vendor detail here."""

    return {
        "profile": selection.profile,
        "selection_reason": selection.reason,
        "attempted_profiles": list(attempted),
        "recover_only": recover_only,
        "idempotency_key": mission["idempotency_key"],
        "capability": mission["payload"].get("capability"),
        "execution_mode": mission["payload"].get("execution_mode", "fixture"),
    }


def _receipt_row(receipt: routing.Receipt) -> dict[str, Any]:
    row = asdict(receipt)
    row["fallback_chain"] = list(receipt.fallback_chain)
    return row


def _receipt_value(row: dict[str, Any]) -> routing.Receipt:
    """Rebuild a receipt from its durable row so budgets survive a restart."""

    raw = dict(row)
    raw["fallback_chain"] = tuple(raw.get("fallback_chain") or ())
    raw["usage"] = routing.Usage(**(raw.get("usage") or {}))
    return routing.Receipt(**raw)
