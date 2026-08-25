"""Reconciliation against the landed `factory-bridge` multi-provider surface.

Read against `factory-bridge` `c9787d5ce3c8605099d245204d53533aa155c720`
(SF-135A, 144 tests green on this host, re-run from that checkout rather than
quoted).  Two sides implemented the same rule independently -- fall back only
before a provider process can have spawned -- and the reconciliation is about
what survives the wire between them.

Two drifts are recorded here as executable checks rather than as prose:

**RD-1.  `ADAPTER_UNAVAILABLE` straddles the bridge's own spawn boundary.**
`AdapterRegistry.dispatch` respects the boundary internally: only
`provider.PreDispatchUnavailable` lets it try another candidate.  But the single
wire code it eventually refuses with is raised from twelve sites in `src/`, of
which at least four are strictly *after* the provider process ran -- non-zero
exit (`adapter.py:182`), timeout (`provider.py:242`), cancellation
(`provider.py:248`) and a stream that would not close (`provider.py:294`) --
alongside seven strictly pre-spawn sites and one ambiguous wrapper
(`service.py:144`).  A client therefore cannot re-route on that code, and this
Controller must not.  The check below is that it does not.

**RD-2.  The bridge's `selection_trace` does not survive a refusal.**  It is
written into the run receipt on disk and echoed in `provider_result_claim` on
success, but `protocol.refusal_response` sets `provider_result_claim=None` and
`receipt_hashes=()`.  So on the exact path where an operator most wants the
route explanation -- nothing ran, why? -- none reaches the Controller.  The
Controller records the trace when it is given one and records its absence
otherwise; it never invents one.

Neither drift is edited in the sibling's repository.  Both are recorded, and
both are held by a test here.
"""

from __future__ import annotations

import unittest

from factory_controller import routing
from tests.support import ALPHA, BETA, LayerAdapter, RouteTestCase, mission_payload


#: Verified at c9787d5: `protocol.REFUSALS` keys the Controller reasons about.
#: Reproduced, not imported -- the bridge is a separate repository with no
#: dependency edge in either direction, which is the point of the boundary.
BRIDGE_REFUSALS_CONSUMED = (
    "ADAPTER_UNAVAILABLE",
    "IDEMPOTENCY_CONFLICT",
    "INVALID_EXECUTION_RESULT",
    "CONTAINMENT_UNAVAILABLE",
)


class BridgeRefusedAdapter(LayerAdapter):
    """A bridge that refuses `ADAPTER_UNAVAILABLE`, saying nothing about spawn."""

    def _dispatch(self, operation_key, value):
        response = super()._dispatch(operation_key, value)
        route = value["route"]
        return {"status": "refused", "diagnostic": "ADAPTER_UNAVAILABLE",
                "receipt": {"provider_profile": route["provider_profile"],
                            "refusal_code": "ADAPTER_UNAVAILABLE",
                            "execution_mode": self.mode}}


class TracingAdapter(LayerAdapter):
    """A bridge that does supply its own selection trace, as on the success path."""

    TRACE = ("alpha-profile:unavailable_pre_dispatch:no executable",
             "beta-profile:selected")

    def _dispatch(self, operation_key, value):
        response = super()._dispatch(operation_key, value)
        response["receipt"]["selection_trace"] = list(self.TRACE)
        response["receipt"]["provider"] = "beta/v2"
        return response


class RD1AdapterUnavailableTests(RouteTestCase, unittest.TestCase):
    def test_the_controller_does_not_reroute_on_adapter_unavailable(self):
        """It cannot: four of the code's raise sites are past the spawn."""

        adapter = BridgeRefusedAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "rd1:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertEqual([call["provider_profile"] for call in adapter.dispatches], [ALPHA])
        self.assertNotIn("PROVIDER_SWITCH", result["terminal_reason"])

    def test_a_refusal_is_recorded_as_a_leg_that_may_have_run(self):
        adapter = BridgeRefusedAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "rd1:2")
        controller.work_once("w1")
        legs = store.route_history(mission["id"])["legs"]
        self.assertEqual(len(legs), 1)
        self.assertIsNone(legs[0]["process_started"])

    def test_only_a_distinguishable_pre_spawn_answer_permits_fallback(self):
        """`provider_unavailable` plus `process_started: false` is that answer."""

        adapter = LayerAdapter(proven_unavailable=[ALPHA])
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "rd1:3")
        controller.work_once("w1")
        self.assertEqual(store.route_history(mission["id"])["fallback_count"], 1)

    def test_the_refusal_codes_the_controller_reasons_about_are_the_bridges(self):
        self.assertIn("ADAPTER_UNAVAILABLE", BRIDGE_REFUSALS_CONSUMED)
        self.assertNotIn(routing.PROVIDER_UNAVAILABLE, BRIDGE_REFUSALS_CONSUMED)


class RD2SelectionTraceTests(RouteTestCase, unittest.TestCase):
    def test_a_supplied_trace_is_recorded_verbatim(self):
        adapter = TracingAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "rd2:1")
        controller.work_once("w1")
        leg = store.route_history(mission["id"])["legs"][0]
        self.assertEqual(leg["layer_selection_trace"], list(TracingAdapter.TRACE))

    def test_an_absent_trace_is_empty_and_never_reconstructed(self):
        adapter = LayerAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(mission_payload(), "rd2:2")
        controller.work_once("w1")
        leg = store.route_history(mission["id"])["legs"][0]
        self.assertEqual(leg["layer_selection_trace"], [])

    def test_a_malformed_trace_is_dropped_rather_than_stored(self):
        receipt = routing.receipt_from_response(
            {"status": "completed", "receipt": {"selection_trace": "not-a-list"}},
            routing.Selection("p", "r", ()), ())
        self.assertEqual(receipt.selection_trace, ())

    def test_the_receipt_uses_the_bridges_field_names(self):
        """`provider_profile` and `provider`, as written in its run receipt."""

        receipt = routing.receipt_from_response(
            {"status": "completed",
             "receipt": {"provider_profile": "beta-profile", "provider": "beta/v2"}},
            routing.Selection(None, "r", ()), ())
        self.assertEqual(receipt.provider_profile, "beta-profile")
        self.assertEqual(receipt.provider, "beta/v2")


class OwnerPolicyBindsTheLayersChoiceTests(RouteTestCase, unittest.TestCase):
    """The bridge selects from its own registry; the Owner's list must still bind.

    `BridgeRequest` at c9787d5 carries no field naming a requested profile, so
    the Controller cannot steer the choice.  It can refuse the result, and does.
    """

    class Substituting(LayerAdapter):
        def _dispatch(self, operation_key, value):
            response = super()._dispatch(operation_key, value)
            response["receipt"]["provider_profile"] = BETA
            return response

    def test_a_denied_profile_that_the_layer_chose_anyway_is_refused(self):
        controller, _, _ = self.build(self.Substituting())
        payload = mission_payload(provider_candidates=[ALPHA],
                                  execution_policy={"denied_profiles": [BETA]})
        controller.submit(payload, "policy:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("PROVIDER_POLICY_VIOLATION", result["terminal_reason"])

    def test_a_profile_outside_the_allowlist_is_refused_after_the_fact(self):
        controller, _, _ = self.build(self.Substituting())
        payload = mission_payload(provider_candidates=[ALPHA],
                                  execution_policy={"allowed_profiles": [ALPHA]})
        controller.submit(payload, "policy:2")
        result = controller.work_once("w1")
        self.assertIn("PROVIDER_POLICY_VIOLATION", result["terminal_reason"])

    def test_a_permitted_substitution_is_allowed_and_recorded_as_what_ran(self):
        controller, store, _ = self.build(self.Substituting())
        payload = mission_payload(provider_candidates=[ALPHA])
        mission, _ = controller.submit(payload, "policy:3")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        route = store.route_history(mission["id"])
        self.assertEqual(route["selected_provider_profile"], BETA)
        self.assertEqual(route["legs"][0]["selection_reason"], "first_admissible")

    def test_a_layer_that_names_no_profile_falls_back_to_the_requested_one(self):
        class Silent(LayerAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["receipt"].pop("provider_profile")
                return response

        controller, store, _ = self.build(Silent())
        mission, _ = controller.submit(mission_payload(), "policy:4")
        controller.work_once("w1")
        self.assertEqual(store.route_history(mission["id"])["selected_provider_profile"], ALPHA)


if __name__ == "__main__":
    unittest.main()
