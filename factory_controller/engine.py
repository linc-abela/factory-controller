"""Restartable mission runner composed over provider-neutral adapter steps.

The Controller decides *what* must happen and *whether it may*; it never decides
*how* a provider is invoked.  Routing here is ordering and admission over opaque
profile names, and the one rule that matters is the side-effect boundary: a
provider may be swapped freely until a process might have started, and never
afterwards unless the execution layer proves none did.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from . import capacity, context, gateway, routing
from .context import ContextBudget, ContextError, ContextRequest
from .routing import ExecutionPolicy, PolicyError, Selection
from .store import (DISPATCH_STEP, RECONCILE_STEP, MissionStore,
                    effective_dispatch_step)


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


class CapacityDeferred(Exception):
    """Every runtime this mission may use is out of quota, and none of them ran.

    Deliberately not a failure of either existing kind.  A retry would spend an
    attempt on a fact about a window rather than about the work, and a
    non-retryable failure would refuse a mission that is perfectly good and
    will run unchanged in a few hours.  Carried out of ``_dispatch`` so the
    baton is written and the deferral taken by the one caller that holds the
    lease.
    """

    def __init__(self, reason: str, resume_at: float | None,
                 plan: "capacity.CapacityPlan | None" = None) -> None:
        super().__init__(reason)
        self.reason, self.resume_at, self.plan = reason, resume_at, plan


#: Used when the mission declares no candidate profiles at all.  The execution
#: layer then picks, which is the Stage-2 behaviour and stays legal: one leg,
#: recorded like any other, so route history is uniform across both shapes.
LAYER_DEFAULT = "layer_default"


def _layer_default_selection() -> Selection:
    return Selection(None, LAYER_DEFAULT, ())


def _unserved_refusal(receipt: routing.Receipt, response: dict[str, Any]) -> bool:
    """True when the layer named a refusal and proved it served nothing.

    All three facts are required, and each closes a way to launder a run.  A
    completion or a candidate means something was produced, whatever the status
    field says.  ``process_started is False`` is the layer's own proof that no
    provider began -- ``side_effect_possible`` already encodes that an unproven
    negative is not a proof, so ``None`` does not qualify.  And the refusal must
    name itself: a leg with nothing to report is left to the served-leg guards
    rather than passed over silently.
    """

    return (response.get("status") != "completed"
            and not response.get("candidate_sha")
            and not receipt.side_effect_possible
            and bool(receipt.refusal_code))


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
        try:
            _gateway_candidates(payload)
        except PolicyError as exc:
            raise NonRetryableFailure("INVALID_GATEWAY_POLICY: %s" % exc) from exc
        try:
            ContextRequest.from_payload(payload)
            ContextBudget.from_payload(payload)
        except ContextError as exc:
            raise NonRetryableFailure("INVALID_CONTEXT_REQUEST: %s" % exc) from exc
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
              *, memo_value: dict[str, Any] | None = None,
              replay_input: bool = False,
              adapter_step: str | None = None) -> dict[str, Any]:
        """Run one durable step, or return the output a previous run recorded.

        ``memo_value`` is the step's durable identity; ``value`` is what the
        adapter is handed.  They differ only for dispatch, whose route changes
        between fallback legs while the step itself stays the same operation.
        """

        if adapter_step == RECONCILE_STEP and isinstance(value.get("route"), dict):
            # The proof's own digest travels with the leg it authorizes, so the
            # durable step input records which derived evidence licensed it and
            # the execution layer can be asked for a lookup rather than a run.
            proof = self.store.reconciliation(mission["id"]) or {}
            # The body travels beside its digest.  The consumer has to prove
            # the sealed response against the *original* request it was
            # emitted for, and the digest alone names that binding without
            # carrying it.  Recorded input only -- ``memo_value`` is the
            # mission payload, so the step's durable identity is unchanged.
            value = {**value, "route": {
                **value["route"],
                "reconcile_proof": proof.get("proof_digest"),
                "reconcile_proof_record": proof or None}}
        started = self.store.begin_step(
            mission["id"], mission["lease_token"], name,
            value if memo_value is None else memo_value,
            recorded_input=value)
        if started["status"] == "COMPLETED":
            # A COMPLETED step is historical truth and is returned, never
            # re-run.  SF-167 let a ``recover_only`` route fall through this
            # guard and call the dispatch adapter again over a settled row,
            # which both duplicated the provider leg and overwrote the output
            # that recorded why the first attempt stopped.  A repaired attempt
            # gets its own durable identity instead -- see
            # ``store.effective_dispatch_step``.
            return started["output"]
        adapter_value = value
        if replay_input and isinstance(started.get("input"), dict):
            adapter_value = started["input"]
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
            output = self.adapter.execute(
                adapter_step or name, started["operation_key"], adapter_value)
        finally:
            stopped.set()
            if thread.is_alive():
                thread.join(timeout=min(1.0, self.lease_seconds))
        if heartbeat_error:
            raise heartbeat_error[0]
        incomplete = {"retryable_error", routing.PROVIDER_UNAVAILABLE, "unavailable"}
        if not (isinstance(output, dict) and output.get("status") in incomplete):
            self.store.complete_step(mission["id"], mission["lease_token"], name, output)
        return output

    def _runtime_payload(self, mission: dict[str, Any]) -> dict[str, Any]:
        """Overlay an immutable recovery binding onto the adapter input.

        The original mission payload is the admission identity and remains
        untouched.  A pre-provider revision recovery stores its verified
        execution checkout in ``context_bindings``; every later adapter step
        sees that same binding, while the Context Broker remains responsible
        for checking the checkout's actual remote, HEAD, and cleanliness.
        """

        payload = mission["payload"]
        binding = self.store.context_binding(mission["id"])
        if binding is None:
            return payload
        try:
            context.validate_revision_context_binding(
                binding,
                expected_project_id=payload.get("project_id"),
                expected_repository_remote_url=payload.get(
                    "repository_remote_url"),
                expected_revision_sha=payload.get("baseline_sha"),
            )
        except context.ContextError:
            raise NonRetryableFailure("INVALID_REVISION_CONTEXT_BINDING")
        stage1 = payload.get("stage1")
        overlay = binding.get("stage1")
        checkout = binding.get("checkout")
        if not isinstance(stage1, dict) or not isinstance(overlay, dict) \
                or not isinstance(checkout, str) or not checkout:
            raise NonRetryableFailure("INVALID_REVISION_CONTEXT_BINDING")
        return {**payload, "stage1": {**stage1, **overlay}}

    def _cancelled(self, mission_id: str, lease_token: str) -> bool:
        current = self.store.get(mission_id)
        if current and current["cancel_requested"]:
            target = "cancelled" if current["state"] == "dispatching" else "escalated"
            reason = "OPERATOR_CANCELLED" if target == "cancelled" else "CANCELLATION_AFTER_SIDE_EFFECT"
            self.store.transition(mission_id, lease_token, target, reason=reason, release_lease=True)
            return True
        return False

    # ------------------------------------------------------------------ #
    # context
    # ------------------------------------------------------------------ #

    def _context(self, mission: dict[str, Any], resume_state: str) -> dict[str, Any] | None:
        """Bind this mission to exactly one context manifest, or refuse.

        The Controller states an entitlement and checks the answer.  It does not
        read, rank, or open anything in the target repository; the whole of this
        method is a policy comparison over facts a broker reported.

        Two properties come out of the machinery rather than from new code.
        *Stickiness*: the manifest is a durable memoized step, so a restart after
        dispatch returns the manifest that was used and the broker is never asked
        again.  *Replay safety*: for a real mission the idempotency key already
        is ``work_item_id:context_manifest_hash``, so the same work against a
        different manifest is not a replay at all -- it is a different mission
        identity, which the store refuses as a key conflict.

        Freshness is enforced only before the irreversible boundary.  Afterwards
        the execution stays bound to the manifest it already ran on, and calling
        that stale would be re-deciding a decision that has already had effects.
        """

        request = ContextRequest.from_payload(mission["payload"])
        if request is None:
            return None
        budget = ContextBudget.from_payload(mission["payload"])
        pre_boundary = resume_state == "dispatching"

        token_refusal = context.reported_token_refusal(
            budget, self.store.telemetry(mission["id"])["reported_input_tokens"])
        if token_refusal:
            self._refuse_context(mission, token_refusal, None)
            raise NonRetryableFailure(
                "%s: ceiling %s reported input tokens"
                % (token_refusal, budget.max_reported_input_tokens))

        runtime_payload = self._runtime_payload(mission)
        context_step = "context"
        binding = self.store.context_binding(mission["id"])
        prior_context = self.store.step_record(mission["id"], "context")
        if binding is not None and prior_context is not None \
                and prior_context.get("status") == "COMPLETED" \
                and isinstance(prior_context.get("output"), dict) \
                and prior_context["output"].get("refusal_code") == "STALE_HEAD":
            # Keep the original refusal row as evidence and give the repaired
            # attempt a new durable step identity. Reusing the completed
            # refusal would replay it forever; overwriting it would erase why
            # the first attempt stopped.
            context_step = "context-recovery"
        response = self._step(
            mission, context_step,
            {"context_request": request.as_wire(),
             # The adapter may bind the declared request to the execution-layer
             # checkout, but it remains the only process allowed to inspect it.
             "mission": runtime_payload},
            adapter_step="context")
        package = context.ContextPackage.from_response(response)
        if package.status == "unavailable":
            # Nothing was memoized, so a later attempt may ask again.  Only a
            # broker that answered gets to bind this mission to anything.
            self._refuse_context(mission, package.refusal_code or "CONTEXT_BROKER_UNAVAILABLE",
                                 package)
            raise RetryableFailure(package.refusal_code or "CONTEXT_BROKER_UNAVAILABLE")

        refusal = context.verify(
            request, package,
            declared_manifest_hash=mission["payload"].get("context_manifest_hash"),
            budget=budget,
            now=self.store.clock() if pre_boundary else None)
        if refusal:
            self._refuse_context(mission, refusal, package)
            raise NonRetryableFailure("%s: mission %s" % (refusal, mission["id"]))
        self.store.log(mission["id"], "CONTEXT_BOUND", {
            "attempt": mission["attempt_count"],
            "context_manifest_hash": package.manifest.manifest_hash,
            "corpus_identity": package.manifest.corpus_identity,
            "policy_identity": package.manifest.policy_identity,
            "selected_refs": len(package.manifest.selected_refs),
            "measurement": package.as_row()["measurement"],
            "reduction": package.measurement.reduction,
            "freshness_enforced": pre_boundary,
        })
        return package.as_row()

    def _refuse_context(self, mission: dict[str, Any], code: str,
                        package: "context.ContextPackage | None") -> None:
        self.store.log(mission["id"], "CONTEXT_REFUSED", {
            "attempt": mission["attempt_count"], "code": code,
            "context_manifest_hash": None if package is None or package.manifest is None
            else package.manifest.manifest_hash,
            "broker_status": None if package is None else package.status,
        })

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
            "provider_profile": profile, "detail": detail})

    def _dispatch(self, mission: dict[str, Any], resume_state: str) -> dict[str, Any]:
        """Produce this mission's dispatch result, routing only where it is safe."""

        payload = self._runtime_payload(mission)
        policy = ExecutionPolicy.from_payload(payload)
        direct = routing.candidates_from_payload(payload)
        admitted, refused = _gateway_candidates(payload)
        for profile, code in refused:
            self.store.log(mission["id"], "GATEWAY_PROFILE_REFUSED", {
                "attempt": mission["attempt_count"], "profile": profile.profile,
                "model_slug": profile.model_slug, "code": code})
        # A gateway profile is an ordinary candidate, placed after the direct
        # harnesses in declared order.  That is the whole of "prefer a direct
        # harness, fall back to the gateway": the existing deterministic
        # selector orders it, and the existing side-effect boundary confines it
        # to legs where nothing can have run.  No second selection rule exists.
        gateways = {profile.profile: profile for profile in admitted}
        candidates = direct + tuple(routing.Candidate(profile.profile, profile.capabilities)
                                    for profile in admitted)
        prior = self.store.runs(mission["id"])
        committed = [leg for leg in prior if leg["process_started"] is not False]
        # Which durable identity this attempt's dispatch belongs to.  A settled
        # pre-provider refusal keeps its row as the evidence of why the first
        # attempt stopped, and the repaired attempt runs under the recovery
        # name -- without it the memo would replay that refusal for ever.
        step = self._dispatch_step(mission)

        if resume_state != "dispatching" or committed:
            return self._recover(mission, committed, prior, step)

        # A lost lease can leave the provider call between its durable STARTED
        # marker and its receipt. Reuse the exact route recorded before that
        # call. Selecting again here would turn an unresolved effect into a
        # second provider leg, even when the operation key is unchanged.
        started = self.store.step_record(mission["id"], step)
        # A STARTED marker with no run leg is the only unresolved case here.
        # When every prior leg proved ``process_started=False``, the same
        # marker is a safe pre-boundary capacity deferral; it must return to
        # ordinary selection so a reset or another declared runtime can serve
        # the mission instead of pinning it to the old refusal.
        if (started is not None and started.get("status") == "STARTED"
                and not prior):
            recorded = started.get("input")
            route = recorded.get("route") if isinstance(recorded, dict) else None
            if not isinstance(route, dict) or route.get("idempotency_key") != mission["idempotency_key"]:
                raise NonRetryableFailure("UNCERTAIN_DISPATCH_ROUTE_INVALID")
            recovery_route = {**route, "recover_only": True}
            response = self._step(
                mission, step,
                {"mission": payload, "route": recovery_route},
                memo_value={"mission": payload},
                adapter_step=self._dispatch_operation(mission))
            receipt = _with_gateway(
                routing.receipt_from_response(response, Selection(
                    route.get("provider_profile"),
                    "reconcile_uncertain_dispatch", ()), ()),
                response, gateways.get(route.get("provider_profile")))
            self._record(
                mission,
                Selection(route.get("provider_profile"),
                          "reconcile_uncertain_dispatch", ()),
                receipt)
            self._observe_refusal(receipt)
            if response.get("status") == routing.PROVIDER_UNAVAILABLE:
                if receipt.process_started is False:
                    raise RetryableFailure(
                        "UNCERTAIN_DISPATCH_RETRYABLE_UNAVAILABLE")
                raise NonRetryableFailure(
                    "UNCERTAIN_DISPATCH_OUTCOME_UNRESOLVED")
            self._verify_receipt(mission, receipt, response)
            return response

        # Capacity narrows, and narrows through the Owner's own mechanism: the
        # profiles a closed window rules out are added to `denied_profiles`, so
        # `routing.select` refuses them for the reason it already has and no
        # second concept of "unavailable" enters the selector.  The list can
        # only grow here, which is what makes "capacity cannot widen authority"
        # structural rather than asserted.
        plan = self._capacity_plan(mission, direct)
        if plan is not None:
            if plan.exhausted:
                # Every subscription runtime this mission declared is cooling.
                # A declared gateway is deliberately *not* tried: substituting
                # metered capacity for a quota window is the one thing Phase 1
                # says must never happen by itself.  A mission the Owner wants
                # served by a gateway declares it as the mission's own runtime,
                # which reaches this code with no direct candidate at all.
                gate = Selection(None, "capacity_gate", ())
                self._record(mission, gate,
                             routing.unserved_receipt(gate, [], "CAPACITY_UNAVAILABLE"))
                self.store.log(mission["id"], "CAPACITY_REFUSED", plan.as_row())
                raise CapacityDeferred("CAPACITY_UNAVAILABLE", plan.resume_at, plan)
            if plan.denied:
                policy = replace(policy,
                                 denied_profiles=policy.denied_profiles + plan.denied)
                self.store.log(mission["id"], "CAPACITY_NARROWED", plan.as_row())

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
                exhausted, resume_at = self._quota_exhaustion(spent)
                if exhausted:
                    # The window closed between the claim and the dispatch, and
                    # every leg proved nothing began.  Losing the mission here
                    # is the defect: it is a good mission whose provider is
                    # asleep, so it goes back on the queue for the reset.
                    raise CapacityDeferred("CAPACITY_EXHAUSTED_MID_DISPATCH", resume_at)
                raise NonRetryableFailure("%s: considered %d candidate(s)" % (code, len(candidates)))
            response = self._step(
                mission, step,
                {"mission": payload, "route": _route(selection, attempted, mission, False,
                                                     gateways.get(selection.profile))},
                memo_value={"mission": payload},
                adapter_step=self._dispatch_operation(mission))
            receipt = _with_gateway(routing.receipt_from_response(response, selection, attempted),
                                    response, gateways.get(selection.profile))
            self._record(mission, selection, receipt)
            spent.append(receipt)
            self._observe_refusal(receipt)
            if response.get("status") != routing.PROVIDER_UNAVAILABLE:
                self._verify_receipt(mission, receipt, response)
                return response
            allowed, why = gateway.may_reroute(receipt.refusal_code, receipt.process_started)
            if not allowed:
                # Two ways to lose the right to re-route, and the second is new.
                # The layer may have failed to prove nothing ran; or it may have
                # claimed so under a refusal code -- a timeout, a malformed
                # answer -- that names a condition in which it cannot know.  An
                # unproven negative is not a proof, and neither is an unknowable
                # one.
                code = ("PROVIDER_SWITCH_AFTER_SIDE_EFFECT" if why == "SIDE_EFFECT_POSSIBLE"
                        else "PROVIDER_SWITCH_AFTER_UNCERTAIN_OUTCOME")
                self._refuse_switch(mission, code, receipt.provider_profile,
                                    "process_started=%r refusal_code=%r"
                                    % (receipt.process_started, receipt.refusal_code))
                raise NonRetryableFailure(
                    "%s: %s did not prove no process started (%s)"
                    % (code, receipt.provider_profile or LAYER_DEFAULT, why))
            attempted.append(selection.profile or LAYER_DEFAULT)

    # ------------------------------------------------------------------ #
    # capacity
    # ------------------------------------------------------------------ #

    def _capacity_plan(self, mission: dict[str, Any],
                       direct: tuple[routing.Candidate, ...]) -> capacity.CapacityPlan | None:
        """What the durable capacity record says about this mission's runtimes.

        ``None`` means capacity has no subject: nobody registered a runtime and
        nobody recorded a measurement, so the Factory behaves exactly as it did
        before this module existed.  That is the same shape the scheduler uses,
        and it is what keeps every pre-capacity mission unchanged.
        """

        if not direct:
            return None
        readings = self.store.capacity_readings()
        if not readings:
            return None
        return capacity.plan(tuple(item.profile for item in direct), readings,
                             capacity.WorkEstimate.from_payload(mission["payload"]))

    def _observe_refusal(self, receipt: routing.Receipt) -> None:
        """Record a provider's quota refusal as the capacity observation it is.

        This is the whole of "no probe".  The most current statement anyone can
        make about a window is the harness declining to serve one, and it
        arrives on a path that was already recording the leg.
        """

        if receipt.provider_profile is None:
            return
        observation = capacity.observation_from_refusal(
            receipt.provider_profile, receipt.refusal_code, self.store.clock())
        if observation is not None:
            self.store.observe_capacity(observation)

    def _quota_exhaustion(self, spent: list[routing.Receipt]) -> tuple[bool, float | None]:
        """Was every leg so far a *proven* quota refusal, and when may we retry?

        Both halves of the test are required.  One leg that failed for another
        reason means the mission has a problem a reset will not fix, and one
        leg that could not prove nothing started means the mission is past the
        boundary -- where a deferral would be exactly the duplicate irreversible
        effect this design exists to make impossible.

        Only legs a *profile* was chosen for are examined.  The Controller's
        own refusals -- the budget gate, the capacity gate, "no admissible
        provider" -- are recorded as legs with no profile, and counting one of
        those would make a deferred mission un-deferrable on its second pass,
        because its own previous refusal would be sitting in the history.

        The resume time comes from the readings the refusals themselves just
        wrote, so it is the provider's own statement rather than a guess.
        """

        served = [receipt for receipt in spent if receipt.provider_profile is not None]
        if not served:
            return (False, None)
        for receipt in served:
            if receipt.process_started is not False:
                return (False, None)
            if receipt.refusal_code not in capacity.QUOTA_REFUSAL_CODES:
                return (False, None)
        readings = self.store.capacity_readings()
        times = [readings[receipt.provider_profile].resume_at
                 for receipt in served
                 if receipt.provider_profile in readings
                 and readings[receipt.provider_profile].resume_at is not None]
        return (True, min(times) if times else None)

    def _dispatch_step(self, mission: dict[str, Any]) -> str:
        """This mission's current dispatch step identity.

        Monotonic: a COMPLETED step is never rewritten, so once the original
        dispatch settled as a proven pre-provider refusal every later attempt
        resolves to the recovery name -- including a ``_recover`` that has to
        find the memo the repaired attempt wrote, not the refusal before it.
        """

        return effective_dispatch_step(
            self.store.step_records(mission["id"]),
            reconciled=self.store.reconciliation(mission["id"]) is not None)[0]

    def _dispatch_operation(self, mission: dict[str, Any]) -> str:
        """Which adapter operation this attempt is allowed to perform.

        A mission carrying a durable reconciliation proof may only *look up*
        the result its provider already produced.  Naming a different adapter
        operation is what makes that structural: ``dispatch-reconcile`` reaches
        no provider selection and no provider process, and the frame it sends
        is refused by the Bridge unless the sealed response is already there.
        """

        if self.store.reconciliation(mission["id"]) is None:
            return DISPATCH_STEP
        return RECONCILE_STEP

    def _recover(self, mission: dict[str, Any], committed: list[dict[str, Any]],
                 prior: list[dict[str, Any]],
                 step: str = DISPATCH_STEP) -> dict[str, Any]:
        """Resume a mission that already crossed the boundary.  Never reroute.

        The recorded step output is preferred; when a crash landed between the
        provider running and the output being recorded, the layer is asked to
        return the result bound to this idempotency key, on the same profile.
        """

        payload = self._runtime_payload(mission)
        profile = committed[-1]["provider_profile"] if committed else None
        selection = Selection(profile, "recover_existing_result", ())
        response = self._step(
            mission, step,
            {"mission": payload, "route": _route(selection, (), mission, True)},
            memo_value={"mission": payload},
            adapter_step=self._dispatch_operation(mission))
        receipt = _with_gateway(routing.receipt_from_response(response, selection, ()),
                                response, None)
        served_model = (receipt.gateway or {}).get("actual_model")
        prior_model = (committed[-1]["receipt"].get("gateway") or {}).get("actual_model") \
            if committed else None
        if prior_model and served_model and served_model != prior_model \
                and served_model not in routing.CANONICAL_ABSENCE:
            # Same rule as the profile check below, one level finer.  A gateway
            # that answers a recovery with a different model has changed which
            # model did work that already had effects.
            self._refuse_switch(mission, "GATEWAY_MODEL_SWITCH_AFTER_SIDE_EFFECT",
                                receipt.provider_profile,
                                "recovery returned %r for a leg served by %r"
                                % (served_model, prior_model))
            raise NonRetryableFailure(
                "GATEWAY_MODEL_SWITCH_AFTER_SIDE_EFFECT: %s -> %s" % (prior_model, served_model))
        if committed and receipt.provider_profile not in (None, profile):
            self._refuse_switch(mission, "PROVIDER_SWITCH_AFTER_SIDE_EFFECT", receipt.provider_profile,
                                "recovery returned %r for a mission dispatched on %r"
                                % (receipt.provider_profile, profile))
            raise NonRetryableFailure(
                "PROVIDER_SWITCH_AFTER_SIDE_EFFECT: recovery changed provider %r -> %r"
                % (profile, receipt.provider_profile))
        if len(prior) == len(self.store.runs(mission["id"])):
            self._record(mission, selection, receipt)
        if response.get("status") == routing.PROVIDER_UNAVAILABLE:
            raise NonRetryableFailure("DISPATCHED_RESULT_UNRECOVERABLE: %s"
                                      % (receipt.refusal_code or routing.PROVIDER_UNAVAILABLE))
        self._verify_receipt(mission, receipt, response)
        return response

    def _verify_receipt(self, mission: dict[str, Any], receipt: routing.Receipt,
                        response: dict[str, Any]) -> None:
        """The dispatch guards, applied to every served leg.

        The mode and key checks are equalities, so neither default direction can
        launder a run: a fixture receipt fails a real mission and a real receipt
        fails a fixture one.

        The policy check is here rather than only at selection because the
        execution layer does its own selecting.  ``factory-bridge`` c9787d5
        picks from its own priority-ordered registry and the Controller has no
        wire field to name a profile, so an Owner allow/deny list would be
        advice unless it is also enforced against the profile that actually ran.
        """

        if _unserved_refusal(receipt, response):
            # Nothing was served, so there is no served leg to check.  These
            # guards all ask "what did the run prove?", and a layer that
            # refused before starting a process proves nothing about a run it
            # never made: its execution_mode is absent, not wrong, and its
            # idempotency key was never bound.  Applying them here replaced the
            # layer's own refusal code with EXECUTION_MODE_UNPROVEN, so the
            # Owner was told the mode could not be proven when the real answer
            # was UNSUPPORTED_CAPABILITY.  Five of the eight missions on the
            # host carry that same masked reason.  Returning leaves the refusal
            # to ``work_once``, which raises the layer's diagnostic verbatim;
            # the mission still fails, and fails closed.
            return

        policy = ExecutionPolicy.from_payload(mission["payload"])
        served = receipt.provider_profile
        if served is not None:
            if served in policy.denied_profiles:
                raise NonRetryableFailure(
                    "PROVIDER_POLICY_VIOLATION: %s is denied by this mission" % served)
            if policy.allowed_profiles and served not in policy.allowed_profiles:
                raise NonRetryableFailure(
                    "PROVIDER_POLICY_VIOLATION: %s is outside the allowed set" % served)
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
        if receipt.gateway:
            # Admission constrained what was *asked* for.  Only the receipt says
            # what answered, so the allowlist is enforced a second time against
            # the model that actually ran -- the same reason the profile
            # allow/deny list is enforced here rather than only at selection.
            gateway_policy = gateway.GatewayPolicy.from_payload(mission["payload"])
            served = _served_gateway_profile(mission["payload"], receipt.provider_profile)
            for check in (gateway.undeclared_failover(receipt.gateway, gateway_policy, served),
                          gateway.privacy_refusal(receipt.gateway, gateway_policy)):
                if check:
                    raise NonRetryableFailure("%s: %s reported %r" % (
                        check, receipt.provider_profile,
                        receipt.gateway.get("actual_model")))

    # ------------------------------------------------------------------ #
    # the mission
    # ------------------------------------------------------------------ #

    def work_once(self, worker_id: str, *, resume_only: bool = False,
                  project_ids: tuple[str, ...] | None = None,
                  mission_id: str | None = None) -> dict[str, Any] | None:
        self.store.recover_stale()
        mission = self.store.claim(worker_id, lease_seconds=self.lease_seconds,
                                   resume_only=resume_only, project_ids=project_ids,
                                   mission_id=mission_id)
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
            self._context(mission, resume_state)
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
            runtime_payload = self._runtime_payload(mission)
            verification = self._step(mission, "verify", {"mission": runtime_payload, "dispatch": dispatch})
            if self._cancelled(mission_id, token):
                return self.store.get(mission_id)
            if not verification.get("verified"):
                raise NonRetryableFailure(verification.get("diagnostic", "CANDIDATE_VERIFICATION_FAILED"))
            if resume_state in {"dispatching", "dispatched"}:
                self.store.transition(mission_id, token, "candidate_verified", detail={"candidate_sha": dispatch["candidate_sha"]})
            evaluation = self._step(mission, "evaluate", {"mission": runtime_payload, "dispatch": dispatch, "verification": verification})
            gate_refusal = _gate_refusal(mission["payload"], evaluation)
            if gate_refusal:
                self.store.transition(mission_id, token, "escalated", reason=gate_refusal, release_lease=True)
                return self.store.get(mission_id)
            if not evaluation.get("passed"):
                self.store.transition(mission_id, token, "escalated", reason=evaluation.get("diagnostic", "ACCEPTANCE_GATE_FAILED"), release_lease=True)
                return self.store.get(mission_id)
            if resume_state in {"dispatching", "dispatched", "candidate_verified"}:
                self.store.transition(mission_id, token, "evaluated", detail={"gate_outcomes": evaluation.get("gate_outcomes", [])})
            evidence = self._step(mission, "evidence", {"mission": runtime_payload, "dispatch": dispatch, "verification": verification, "evaluation": evaluation})
            if not evidence.get("accepted"):
                if evidence.get("retryable"):
                    raise RetryableFailure(evidence.get("diagnostic", "EVIDENCE_BINDING_FAILED"))
                raise NonRetryableFailure(evidence.get("diagnostic", "EVIDENCE_REJECTED"))
            if resume_state != "evidence_sealed":
                self.store.transition(mission_id, token, "evidence_sealed", detail={"evidence_pointer": evidence.get("evidence_pointer")})
            result = {"dispatch": dispatch, "verification": verification, "evaluation": evaluation, "evidence": evidence}
            self.store.transition(mission_id, token, "completed", result=result, release_lease=True)
        except CapacityDeferred as exc:
            # The checkpoint is recorded *before* the deferral, while the lease
            # is still held, so what it says is what was true when the work
            # stopped.  `store.defer` then re-checks the safe boundary itself
            # and refuses if anything might have run, which is why this handler
            # needs no boundary test of its own -- and why a checkpoint that
            # says `post_dispatch_unreconciled` can never be followed by one.
            checkpoint = self.store.capacity_checkpoint(
                mission_id,
                _last_runtime_reading(self.store, mission_id,
                                      self.store.capacity_readings()))
            self.store.log(mission_id, "CAPACITY_CHECKPOINT", {
                "reason": exc.reason,
                "resume_at": exc.resume_at if exc.resume_at is not None else "unknown",
                "checkpoint": checkpoint})
            self.store.defer(mission_id, token, exc.reason, exc.resume_at)
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

def _last_runtime_reading(store: MissionStore, mission_id: str,
                          readings: dict[str, Any]):
    """The capacity reading for the runtime this mission last touched, if any."""

    legs = store.runs(mission_id)
    profile = legs[-1]["provider_profile"] if legs else None
    return readings.get(profile) if profile else None


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
    expectations = payload.get("acceptance_gate_expectations", {})
    if not isinstance(expectations, Mapping):
        return "ACCEPTANCE_GATE_EXPECTATION_INVALID"
    invalid = set(expectations) - set(declared)
    if invalid:
        return "ACCEPTANCE_GATE_EXPECTATION_INVALID: " + ", ".join(sorted(invalid))
    for gate, expectation in expectations.items():
        if (not isinstance(expectation, Mapping)
                or expectation.get("passed") is not False
                or type(expectation.get("exit_code")) is not int
                or not 0 <= expectation["exit_code"] <= 255):
            return "ACCEPTANCE_GATE_EXPECTATION_INVALID: " + str(gate)
    outcomes = evaluation.get("gate_outcomes") or ()
    if not isinstance(outcomes, (list, tuple)):
        return "ACCEPTANCE_GATE_UNEVALUATED: gate_outcomes is not a list"
    seen = {outcome.get("gate_id"): outcome for outcome in outcomes if isinstance(outcome, dict)}
    missing = [gate for gate in declared if gate not in seen]
    if missing:
        return "ACCEPTANCE_GATE_UNEVALUATED: " + ", ".join(missing)
    failed = []
    for gate in declared:
        outcome = seen[gate]
        expectation = expectations.get(gate)
        if expectation is None:
            satisfied = outcome.get("passed") is True
        else:
            satisfied = (outcome.get("passed") is False
                          and outcome.get("exit_code") == expectation["exit_code"])
        if not satisfied:
            failed.append(gate)
    if failed:
        return "ACCEPTANCE_GATE_FAILED: " + ", ".join(failed)
    return None


def _gateway_candidates(payload: dict[str, Any]) -> tuple:
    """Split declared gateway profiles into admitted and refused."""

    admitted, refused = [], []
    for profile, code in gateway.admitted_profiles(payload):
        (refused.append((profile, code)) if code else admitted.append(profile))
    return tuple(admitted), tuple(refused)


def _served_gateway_profile(payload: dict[str, Any], profile: str | None):
    for candidate in gateway.profiles_from_payload(payload):
        if candidate.profile == profile:
            return candidate
    return None


def _with_gateway(receipt: routing.Receipt, response: dict[str, Any],
                  profile) -> routing.Receipt:
    raw = response.get("receipt") if isinstance(response.get("receipt"), dict) else {}
    facts = gateway.facts_from_response(raw.get("gateway"), profile)
    return receipt if facts is None else replace(receipt, gateway=facts)


def _route(selection: Selection, attempted: tuple | list, mission: dict[str, Any],
           recover_only: bool, gateway_profile=None) -> dict[str, Any]:
    """What the execution layer is told about this leg.

    A gateway leg carries the model slug the Owner admitted, because the layer
    cannot request a model it was not told.  That is the only vendor-shaped
    value that crosses, it came from the mission's own declaration, and no
    credential accompanies it.
    """

    if gateway_profile is not None:
        return {**_route(selection, attempted, mission, recover_only),
                "gateway": {"gateway": gateway_profile.gateway,
                            "model_slug": gateway_profile.model_slug,
                            "privacy": list(gateway_profile.privacy)}}
    return {
        "provider_profile": selection.profile,
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
    if "profile" in raw and "provider_profile" not in raw:
        raw["provider_profile"] = raw.pop("profile")
    if "provider_identity" in raw and "provider" not in raw:
        raw["provider"] = raw.pop("provider_identity")
    raw["fallback_chain"] = tuple(raw.get("fallback_chain") or ())
    raw["selection_trace"] = tuple(raw.get("selection_trace") or ())
    raw["usage"] = routing.Usage(**(raw.get("usage") or {}))
    raw["gateway"] = raw.get("gateway") or None
    return routing.Receipt(**raw)
