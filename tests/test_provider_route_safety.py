"""Conformance matrix for multi-provider routing, failover, and the side-effect boundary.

Every case here uses two provider identities and an injected availability
outcome.  The property under test is always the same one: a provider may be
swapped while nothing can have run, and never once something might have.
"""

from __future__ import annotations

import time
import unittest

from factory_controller.store import ConflictError
from tests.support import ALPHA, BETA, LayerAdapter, ProcessDeath, RouteTestCase, mission_payload


class PreDispatchFallbackTests(RouteTestCase, unittest.TestCase):
    def test_first_choice_is_used_when_it_is_available(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "route:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        route = store.route_history(mission["id"])
        self.assertEqual(route["selected_provider_profile"], ALPHA)
        self.assertEqual(route["fallback_count"], 0)

    def test_a_proven_unavailable_provider_falls_back_before_any_side_effect(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "route:2")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        route = store.route_history(mission["id"])
        self.assertEqual([leg["provider_profile"] for leg in route["legs"]], [ALPHA, BETA])
        self.assertEqual(route["selected_provider_profile"], BETA)
        self.assertEqual(route["fallback_count"], 1)
        self.assertEqual(route["legs"][1]["selection_reason"], "fallback_after:" + ALPHA)

    def test_the_fallback_leg_carries_the_same_idempotency_key(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "route:3")
        controller.work_once("w1")
        keys = {leg["idempotency_key"] for leg in store.route_history(mission["id"])["legs"]}
        self.assertEqual(keys, {"route:3"})

    def test_all_candidates_unavailable_refuses_before_dispatch(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA, BETA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "route:4")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("NO_ADMISSIBLE_PROVIDER", result["terminal_reason"])

    def test_no_fallback_policy_stops_after_the_first_unavailable_provider(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        payload = mission_payload(execution_policy={"no_fallback": True})
        mission, _ = controller.submit(payload, "route:5")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("PROVIDER_FALLBACK_FORBIDDEN", result["terminal_reason"])
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [ALPHA])

    def test_a_denied_profile_is_never_dispatched_to(self):
        adapter = LayerAdapter()
        controller, _, _ = self.build(adapter)
        payload = mission_payload(execution_policy={"denied_profiles": [ALPHA]})
        controller.submit(payload, "route:6")
        controller.work_once("w1")
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [BETA])

    def test_route_leg_limit_bounds_the_fallback_chain(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA, BETA])
        controller, _, _ = self.build(adapter)
        payload = mission_payload(execution_policy={"max_route_legs": 1})
        controller.submit(payload, "route:7")
        result = controller.work_once("w1")
        self.assertIn("PROVIDER_ROUTE_EXHAUSTED", result["terminal_reason"])
        self.assertEqual(len(adapter.dispatches), 1)


class SideEffectBoundaryTests(RouteTestCase, unittest.TestCase):
    def test_a_layer_that_will_not_prove_no_process_ran_blocks_the_switch(self):
        adapter = LayerAdapter(silent_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "boundary:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("PROVIDER_SWITCH_AFTER_SIDE_EFFECT", result["terminal_reason"])
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [ALPHA])
        route = store.route_history(mission["id"])
        self.assertEqual(route["switch_refusals"][0]["code"], "PROVIDER_SWITCH_AFTER_SIDE_EFFECT")

    def test_the_boundary_is_recorded_at_the_leg_that_crossed_it(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "boundary:2")
        controller.work_once("w1")
        boundary = store.route_history(mission["id"])["side_effect_boundary"]
        self.assertEqual(boundary["provider_profile"], BETA)
        self.assertEqual(boundary["leg"], 2)
        self.assertTrue(boundary["process_started"])

    def test_recovery_after_dispatch_never_selects_a_new_provider(self):
        """Availability flips between runs; the dispatched mission must not move."""

        adapter = LayerAdapter(crash_on="verify")
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "boundary:3")
        with self.assertRaises(ProcessDeath):
            controller.work_once("killed")
        self.assertEqual(store.get(mission["id"])["state"], "dispatched")
        time.sleep(0.05)

        # The first provider is now unavailable. The mission is past the
        # boundary, so the Controller must resume rather than reroute.
        adapter.proven_unavailable.add(ALPHA)
        resumed = self.reopen(path, adapter)
        result = resumed.work_once("replacement")
        self.assertEqual(result["state"], "completed")
        route = resumed.store.route_history(mission["id"])
        self.assertEqual(route["selected_provider_profile"], ALPHA)
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [ALPHA])

    def test_a_crash_before_the_step_output_lands_recovers_on_the_same_profile(self):
        adapter = LayerAdapter()
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "boundary:4")
        claimed = store.claim("w1", lease_seconds=0.02)
        token = claimed["lease_token"]
        # Cross the boundary and record the leg, but never complete the step.
        store.begin_step(claimed["id"], token, "dispatch", {"mission": claimed["payload"]})
        store.record_run(claimed["id"], 1,
                         {"reason": "first_admissible", "considered": []},
                         {"provider_profile": ALPHA, "classification": "completed", "process_started": True},
                         "boundary:4")
        store.transition(claimed["id"], token, "dispatched")
        time.sleep(0.05)

        adapter.proven_unavailable.add(ALPHA)
        resumed = self.reopen(path, adapter)
        result = resumed.work_once("replacement")
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [ALPHA])
        self.assertTrue(adapter.dispatches[0]["recover_only"])
        # Past the boundary a non-retryable failure is `failed`, never `refused`:
        # `refused` is reserved for missions that never left `dispatching`.
        self.assertEqual(result["state"], "failed")
        self.assertIn("DISPATCHED_RESULT_UNRECOVERABLE", result["terminal_reason"])

    def test_recovery_returning_a_different_provider_is_refused(self):
        adapter = LayerAdapter()
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "boundary:5")
        claimed = store.claim("w1", lease_seconds=0.02)
        token = claimed["lease_token"]
        store.begin_step(claimed["id"], token, "dispatch", {"mission": claimed["payload"]})
        store.record_run(claimed["id"], 1,
                         {"reason": "first_admissible", "considered": []},
                         {"provider_profile": ALPHA, "classification": "completed", "process_started": True},
                         "boundary:5")
        store.transition(claimed["id"], token, "dispatched")
        time.sleep(0.05)

        class Swapper(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["receipt"]["provider_profile"] = BETA
                return response

        resumed = self.reopen(path, Swapper())
        result = resumed.work_once("replacement")
        self.assertEqual(result["state"], "failed")
        self.assertIn("PROVIDER_SWITCH_AFTER_SIDE_EFFECT", result["terminal_reason"])


class CrashAndLeaseTests(RouteTestCase, unittest.TestCase):
    def test_a_crash_during_selection_leaves_no_dispatched_state(self):
        adapter = LayerAdapter(crash_on="dispatch")
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "crash:1")
        with self.assertRaises(ProcessDeath):
            controller.work_once("killed")
        self.assertEqual(store.get(mission["id"])["state"], "dispatching")
        self.assertEqual(store.runs(mission["id"]), [])
        time.sleep(0.05)
        result = self.reopen(path, adapter).work_once("replacement")
        self.assertEqual(result["state"], "completed")

    def test_a_stale_lease_returns_a_pre_dispatch_mission_to_the_queue(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "crash:2")
        store.claim("gone", lease_seconds=0.01)
        time.sleep(0.03)
        self.assertEqual(store.recover_stale(), 1)
        self.assertEqual(store.get(mission["id"])["state"], "admitted")
        self.assertEqual(controller.work_once("w2")["state"], "completed")

    def test_a_stale_lease_after_dispatch_stays_at_the_post_dispatch_state(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(mission_payload(), "crash:3")
        claimed = store.claim("gone", lease_seconds=0.01)
        store.transition(claimed["id"], claimed["lease_token"], "dispatched")
        time.sleep(0.03)
        store.recover_stale()
        self.assertEqual(store.get(mission["id"])["state"], "dispatched")


class ReplayAndIdentityTests(RouteTestCase, unittest.TestCase):
    def test_resubmitting_the_same_key_and_payload_replays_the_mission(self):
        controller, _, _ = self.build(LayerAdapter())
        payload = mission_payload()
        first, created = controller.submit(payload, "replay:1")
        second, again = controller.submit(payload, "replay:1")
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])

    def test_the_same_key_with_different_input_conflicts(self):
        controller, _, _ = self.build(LayerAdapter())
        controller.submit(mission_payload(), "replay:2")
        with self.assertRaises(ConflictError):
            controller.submit(mission_payload(capability="review"), "replay:2")

    def test_a_layer_binding_a_different_key_is_refused(self):
        class Diverging(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["receipt"]["idempotency_key"] = "someone-elses-key"
                return response

        controller, _, _ = self.build(Diverging())
        controller.submit(mission_payload(), "replay:3")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("IDEMPOTENCY_KEY_DIVERGED", result["terminal_reason"])

    def test_the_route_records_the_key_the_layer_was_given(self):
        adapter = LayerAdapter()
        controller, _, _ = self.build(adapter)
        controller.submit(mission_payload(), "replay:4")
        controller.work_once("w1")
        self.assertEqual(adapter.dispatches[0]["idempotency_key"], "replay:4")


class CancellationTests(RouteTestCase, unittest.TestCase):
    def test_cancelling_before_dispatch_is_clean(self):
        controller, store, _ = self.build(LayerAdapter())
        mission, _ = controller.submit(mission_payload(), "cancel:1")
        self.assertEqual(store.cancel(mission["id"]), "cancelled")
        self.assertIsNone(controller.work_once("w1"))

    def test_cancelling_after_dispatch_is_refused_rather_than_silently_dropped(self):
        controller, store, _ = self.build(LayerAdapter())
        mission, _ = controller.submit(mission_payload(), "cancel:2")
        claimed = store.claim("w1")
        store.transition(claimed["id"], claimed["lease_token"], "dispatched")
        with self.assertRaises(ValueError):
            store.cancel(mission["id"])


class EvaluatorAndEvidenceTests(RouteTestCase, unittest.TestCase):
    def test_a_failing_gate_escalates_and_keeps_the_route_history(self):
        adapter = LayerAdapter(gates_pass=False)
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "gate:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "escalated")
        self.assertIn("ACCEPTANCE_GATE_FAILED", result["terminal_reason"])
        self.assertEqual(store.route_history(mission["id"])["selected_provider_profile"], ALPHA)

    def test_an_evaluator_that_skips_a_declared_gate_never_passes(self):
        class Partial(LayerAdapter):
            def execute(self, step, operation_key, value):
                if step == "evaluate":
                    return {"passed": True,
                            "gate_outcomes": [{"gate_id": "SOMETHING-ELSE", "passed": True}]}
                return super().execute(step, operation_key, value)

        controller, _, _ = self.build(Partial())
        controller.submit(mission_payload(acceptance_gate_ids=["G-DECLARED"]), "gate:2")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "escalated")
        self.assertIn("ACCEPTANCE_GATE_UNEVALUATED", result["terminal_reason"])
        self.assertIn("G-DECLARED", result["terminal_reason"])

    def test_evidence_rejection_fails_the_mission_after_the_boundary(self):
        controller, store, _ = self.build(LayerAdapter(evidence=False))
        mission, _ = controller.submit(mission_payload(), "gate:3")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "failed")
        self.assertIn("EVIDENCE_REJECTED", result["terminal_reason"])

    def test_verification_failure_never_reroutes(self):
        adapter = LayerAdapter(verified=False)
        controller, _, _ = self.build(adapter)
        controller.submit(mission_payload(), "gate:4")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(adapter.dispatches), 1)


class ExecutionModeTests(RouteTestCase, unittest.TestCase):
    def test_a_fixture_result_cannot_complete_a_real_mission(self):
        """The SF-134 gap, closed: a dry-run result is not a mission result."""

        controller, _, _ = self.build(LayerAdapter(mode="fixture"))
        payload = mission_payload(execution_mode="real",
                                  context_manifest_hash="c" * 64,
                                  acceptance_gate_ids=["G"])
        controller.submit(payload, "SF-135-ROUTE:" + "c" * 64)
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("EXECUTION_MODE_MISMATCH", result["terminal_reason"])

    def test_a_real_result_cannot_complete_a_fixture_mission(self):
        controller, _, _ = self.build(LayerAdapter(mode="real"))
        controller.submit(mission_payload(), "mode:2")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("EXECUTION_MODE_MISMATCH", result["terminal_reason"])

    def test_a_silent_layer_cannot_complete_a_real_mission(self):
        controller, _, _ = self.build(LayerAdapter(mode="unstated"))
        payload = mission_payload(execution_mode="real", context_manifest_hash="d" * 64)
        controller.submit(payload, "SF-135-ROUTE:" + "d" * 64)
        result = controller.work_once("w1")
        self.assertIn("EXECUTION_MODE_UNPROVEN", result["terminal_reason"])

    def test_a_real_mission_must_carry_the_key_evidence_core_will_accept(self):
        controller, _, _ = self.build(LayerAdapter(mode="real"))
        payload = mission_payload(execution_mode="real", context_manifest_hash="e" * 64)
        with self.assertRaises(Exception) as caught:
            controller.submit(payload, "an-operator-chose-this")
        self.assertIn("IDEMPOTENCY_KEY_NOT_BRIDGE_DERIVABLE", str(caught.exception))

    def test_a_real_mission_must_declare_its_acceptance_gates(self):
        controller, _, _ = self.build(LayerAdapter(mode="real"))
        payload = mission_payload(execution_mode="real", context_manifest_hash="f" * 64,
                                  acceptance_gate_ids=[])
        with self.assertRaises(Exception) as caught:
            controller.submit(payload, "SF-135-ROUTE:" + "f" * 64)
        self.assertIn("ACCEPTANCE_GATE_UNDECLARED", str(caught.exception))

    def test_a_real_mission_completes_when_every_obligation_is_met(self):
        adapter = LayerAdapter(mode="real")
        controller, store, _ = self.build(adapter)
        key = "SF-135-ROUTE:" + "0" * 64
        payload = mission_payload(execution_mode="real", context_manifest_hash="0" * 64)
        mission, _ = controller.submit(payload, key)
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(store.telemetry(mission["id"])["execution_mode"], "real")


class BudgetGateTests(RouteTestCase, unittest.TestCase):
    PRICED = {"cost_amount": 6.0, "cost_currency": "USD", "cost_state": "reported"}

    def test_a_ceiling_that_is_not_reached_permits_every_leg(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA], usage=self.PRICED)
        controller, store, _ = self.build(adapter)
        payload = mission_payload(execution_policy={"budget_ceiling": 100, "budget_currency": "USD"})
        mission, _ = controller.submit(payload, "budget:1")
        self.assertEqual(controller.work_once("w1")["state"], "completed")
        self.assertEqual(store.telemetry(mission["id"])["reported_cost"]["state"], "reported")

    def test_a_known_exhausted_ceiling_refuses_the_next_dispatch(self):
        """Leg one reports 6.0 against a ceiling of 10; leg two must not start."""

        adapter = LayerAdapter(proven_unavailable=[ALPHA, BETA], usage=self.PRICED)
        controller, store, _ = self.build(adapter)
        payload = mission_payload(
            provider_candidates=[ALPHA, BETA],
            execution_policy={"budget_ceiling": 10, "budget_currency": "USD", "max_route_legs": 5})
        mission, _ = controller.submit(payload, "budget:2")

        # A priced but unavailable leg still spends nothing, so drive the
        # accounting with a served leg that is then asked to fall back.
        class Expensive(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["status"] = "provider_unavailable"
                response["receipt"]["process_started"] = False
                response["receipt"]["usage"] = BudgetGateTests.PRICED
                return response

        controller.adapter = Expensive(proven_unavailable=[], usage=self.PRICED)
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("MISSION_BUDGET_EXHAUSTED", result["terminal_reason"])
        legs = store.route_history(mission["id"])["legs"]
        self.assertEqual([leg["outcome"] for leg in legs][-1], "provider_unavailable")

    def test_unknown_cost_never_blocks_and_stays_unknown(self):
        adapter = LayerAdapter(usage=None)
        controller, store, _ = self.build(adapter)
        payload = mission_payload(execution_policy={"budget_ceiling": 1, "budget_currency": "USD"})
        mission, _ = controller.submit(payload, "budget:3")
        self.assertEqual(controller.work_once("w1")["state"], "completed")
        cost = store.telemetry(mission["id"])["reported_cost"]
        self.assertEqual(cost["state"], "unknown")
        self.assertEqual(cost["unpriced_legs"], 1)


class RouteExplainabilityTests(RouteTestCase, unittest.TestCase):
    def test_the_history_answers_every_operator_question(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "explain:1")
        controller.work_once("w1")
        route = store.route_history(mission["id"])
        self.assertEqual(route["selected_provider_profile"], BETA)
        self.assertEqual(route["legs"][0]["outcome"], "provider_unavailable")
        self.assertEqual(route["legs"][1]["selection_reason"], "fallback_after:" + ALPHA)
        self.assertEqual([item["profile"] for item in route["legs"][0]["considered"]],
                         [ALPHA, BETA])
        self.assertEqual(route["side_effect_boundary"]["provider_profile"], BETA)
        self.assertEqual(route["switch_refusals"], [])

    def test_the_history_survives_a_restart_because_it_is_durable(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, _, path = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "explain:2")
        controller.work_once("w1")
        reopened = self.reopen(path, adapter)
        self.assertEqual(reopened.store.route_history(mission["id"])["fallback_count"], 1)

    def test_route_legs_are_append_only(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "explain:3")
        controller.work_once("w1")
        with store.connect() as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE runs SET provider_profile='rewritten' WHERE mission_id=?", (mission["id"],))
            with self.assertRaises(Exception):
                db.execute("DELETE FROM runs WHERE mission_id=?", (mission["id"],))

    def test_the_route_names_a_profile_exactly_once(self):
        """One key per concept: an alias beside it is a fork, not a courtesy."""

        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "explain:5")
        controller.work_once("w1")
        route = store.route_history(mission["id"])
        self.assertNotIn("selected_profile", route)
        self.assertNotIn("profile", route["legs"][0])
        self.assertNotIn("profile", route["side_effect_boundary"])
        self.assertEqual(route["selected_provider_profile"], ALPHA)

    def test_a_mission_with_no_declared_candidates_still_records_one_leg(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter)
        payload = mission_payload()
        payload.pop("provider_candidates")
        mission, _ = controller.submit(payload, "explain:4")
        controller.work_once("w1")
        route = store.route_history(mission["id"])
        self.assertEqual(len(route["legs"]), 1)
        self.assertEqual(route["legs"][0]["selection_reason"], "layer_default")


class TelemetryTests(RouteTestCase, unittest.TestCase):
    def test_the_seam_reports_measured_facts_and_explicit_absences(self):
        adapter = LayerAdapter(proven_unavailable=[ALPHA],
                               usage={"input_tokens": 100, "output_tokens": 20})
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(
            mission_payload(context_manifest_hash="a" * 64,
                            repository_remote_url="git@example.com:demo.git"),
            "telemetry:1")
        controller.work_once("w1")
        telemetry = store.telemetry(mission["id"])
        self.assertEqual(telemetry["outcome"], "completed")
        self.assertEqual(telemetry["provider_profile"], BETA)
        self.assertEqual(telemetry["route_legs"], 2)
        self.assertEqual(telemetry["fallback_count"], 1)
        self.assertEqual(telemetry["retries"], 0)
        self.assertEqual(telemetry["elapsed_execution_ms"], 24)
        self.assertEqual(telemetry["reported_input_tokens"]["total"], 200)
        self.assertEqual(telemetry["reported_cost"]["state"], "unknown")
        self.assertFalse(telemetry["owner_intervention"])
        self.assertEqual(telemetry["context_reference"]["context_manifest_hash"], "a" * 64)
        self.assertEqual(telemetry["context_reference"]["idempotency_key"], "telemetry:1")

    def test_absent_measurements_are_named_not_zeroed(self):
        class Silent(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["receipt"].pop("duration_ms")
                return response

        controller, store, _ = self.build(Silent())
        mission, _ = controller.submit(mission_payload(), "telemetry:2")
        controller.work_once("w1")
        telemetry = store.telemetry(mission["id"])
        self.assertEqual(telemetry["elapsed_execution_ms"], "unknown")
        self.assertEqual(telemetry["unmeasured_legs"], 1)
        self.assertEqual(telemetry["reported_input_tokens"], "unknown")
        self.assertEqual(telemetry["context_reference"]["context_manifest_hash"], "unknown")

    def test_a_uniformly_declared_absence_keeps_its_own_word(self):
        """`not_applicable` legs are not flattened into `unknown`."""

        adapter = LayerAdapter(usage={"cost_state": "not_applicable"})
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "telemetry:4")
        controller.work_once("w1")
        self.assertEqual(store.telemetry(mission["id"])["reported_cost"]["state"],
                         "not_applicable")

    def test_mixed_absence_words_across_legs_fall_back_to_unknown(self):
        class Mixed(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                state = "not_applicable" if value["route"]["provider_profile"] == ALPHA else "not_run"
                response["receipt"]["usage"] = {"cost_state": state}
                return response

        controller, store, _ = self.build(Mixed(proven_unavailable=[ALPHA]))
        mission, _ = controller.submit(mission_payload(), "telemetry:5")
        controller.work_once("w1")
        self.assertEqual(store.telemetry(mission["id"])["reported_cost"]["state"], "unknown")

    def test_an_escalated_mission_is_marked_as_needing_the_owner(self):
        controller, store, _ = self.build(LayerAdapter(gates_pass=False))
        mission, _ = controller.submit(mission_payload(), "telemetry:3")
        controller.work_once("w1")
        self.assertTrue(store.telemetry(mission["id"])["owner_intervention"])


if __name__ == "__main__":
    unittest.main()
