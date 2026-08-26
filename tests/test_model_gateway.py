"""OpenRouter as an admitted execution profile, and the fences around it.

The Owner made a model gateway first-class in the MVP.  What keeps it from
becoming the Factory's authority is that it enters as an ordinary candidate on
the route the Controller already had: same selection order, same side-effect
boundary, same receipts, same evidence classes.  These tests check the entry
points where that could quietly stop being true.
"""

from __future__ import annotations

import unittest

from factory_controller import gateway, routing
from factory_controller.engine import NonRetryableFailure
from tests.support import ALPHA, RouteTestCase, mission_payload


SLUG = "vendor/model-a"
OTHER = "vendor/model-b"


def gateway_mission(**extra):
    payload = mission_payload(
        provider_candidates=[{"profile": ALPHA, "capabilities": ["implement"]}],
        gateway_profiles=[{"profile": "gw-1", "gateway": "openrouter", "model_slug": SLUG,
                           "capabilities": ["implement"], "privacy": ["zero_data_retention"]}],
        gateway_policy={"enabled": True, "allowed_model_slugs": [SLUG],
                        "required_privacy": ["zero_data_retention"]})
    payload.update(extra)
    return payload


class GatewayLayer:
    """An execution layer holding one direct harness and one gateway profile."""

    def __init__(self, *, direct_refusal=None, direct_started=False, facts=None,
                 gateway_status="completed", gateway_refusal=None, gateway_started=True):
        self.direct_refusal = direct_refusal
        self.direct_started = direct_started
        self.facts = facts
        self.gateway_status = gateway_status
        self.gateway_refusal = gateway_refusal
        self.gateway_started = gateway_started
        self.routes: list[dict] = []

    def execute(self, step, operation_key, value):
        if step == "verify":
            return {"verified": True}
        if step == "evaluate":
            gates = value["mission"].get("acceptance_gate_ids") or ["G"]
            return {"passed": True, "gate_outcomes": [{"gate_id": g, "passed": True} for g in gates]}
        if step == "evidence":
            return {"accepted": True, "evidence_pointer": "e" * 64}
        route = value["route"]
        self.routes.append(dict(route))
        profile = route["provider_profile"]
        receipt = {"provider_profile": profile, "provider": "layer",
                   "execution_mode": "fixture", "idempotency_key": route["idempotency_key"]}
        if profile == ALPHA:
            if self.direct_refusal:
                return {"status": "provider_unavailable", "diagnostic": self.direct_refusal,
                        "receipt": {**receipt, "process_started": self.direct_started,
                                    "refusal_code": self.direct_refusal}}
            return {"status": "completed", "candidate_sha": "a" * 40,
                    "receipt": {**receipt, "process_started": True}}
        receipt["gateway"] = self.facts if self.facts is not None else {
            "gateway": "openrouter", "requested_model": SLUG, "actual_model": SLUG,
            "actual_provider": "vendor", "generation_id": "gen-1",
            "input_tokens": 1200, "output_tokens": 340,
            "cost_amount": 0.0182, "cost_currency": "USD",
            "privacy_enforced": ["zero_data_retention"]}
        if self.gateway_refusal:
            return {"status": "provider_unavailable", "diagnostic": self.gateway_refusal,
                    "receipt": {**receipt, "process_started": self.gateway_started,
                                "refusal_code": self.gateway_refusal}}
        return {"status": self.gateway_status, "candidate_sha": "b" * 40,
                "receipt": {**receipt, "process_started": True}}


class AdmissionTests(unittest.TestCase):
    def profile(self, **extra):
        base = {"profile": "gw", "gateway": "openrouter", "model_slug": SLUG}
        base.update(extra)
        return gateway.GatewayProfile(**base)

    def test_a_gateway_is_off_until_the_owner_turns_it_on(self):
        self.assertEqual(gateway.admit(self.profile(), gateway.GatewayPolicy()),
                         "GATEWAY_DISABLED")

    def test_implicit_auto_routing_is_never_a_default(self):
        """An implicit model cannot be allowlisted, priced, or reproduced."""

        policy = gateway.GatewayPolicy(enabled=True, allowed_model_slugs=(gateway.AUTO_SLUG,))
        self.assertEqual(gateway.admit(self.profile(model_slug=gateway.AUTO_SLUG), policy),
                         "GATEWAY_IMPLICIT_AUTO_ROUTING_REFUSED")
        allowed = gateway.GatewayPolicy(enabled=True, allow_auto_routing=True,
                                        allowed_model_slugs=(gateway.AUTO_SLUG,))
        self.assertIsNone(gateway.admit(self.profile(model_slug=gateway.AUTO_SLUG), allowed))

    def test_a_profile_must_name_an_explicit_slug_to_exist_at_all(self):
        with self.assertRaises(routing.PolicyError):
            gateway.GatewayProfile(profile="gw", model_slug="")

    def test_an_unlisted_model_a_missing_privacy_term_and_a_missing_capability(self):
        enabled = gateway.GatewayPolicy(enabled=True, allowed_model_slugs=(SLUG,))
        self.assertEqual(gateway.admit(self.profile(model_slug=OTHER), enabled),
                         "GATEWAY_MODEL_NOT_ALLOWLISTED")
        private = gateway.GatewayPolicy(enabled=True, required_privacy=("zero_data_retention",))
        self.assertEqual(gateway.admit(self.profile(), private),
                         "GATEWAY_PRIVACY_REQUIREMENT_UNMET")
        capable = gateway.GatewayPolicy(enabled=True, required_capability="implement")
        self.assertEqual(gateway.admit(self.profile(), capable),
                         "GATEWAY_CAPABILITY_UNSUPPORTED")

    def test_a_fallback_model_outside_the_allowlist_is_refused_at_policy_build(self):
        with self.assertRaises(routing.PolicyError):
            gateway.GatewayPolicy.from_payload({"gateway_policy": {
                "enabled": True, "allowed_model_slugs": [SLUG], "fallback_models": [OTHER]}})

    def test_duplicate_profile_names_are_refused(self):
        with self.assertRaises(routing.PolicyError):
            gateway.profiles_from_payload({"gateway_profiles": [
                {"profile": "gw", "model_slug": SLUG}, {"profile": "gw", "model_slug": OTHER}]})


class RerouteBoundaryTests(unittest.TestCase):
    def test_an_unproven_negative_never_unlocks_a_reroute(self):
        for started in (True, None):
            self.assertEqual(gateway.may_reroute(None, started),
                             (False, "SIDE_EFFECT_POSSIBLE"))

    def test_a_refusal_code_naming_an_unknowable_outcome_beats_the_layers_claim(self):
        """A timed-out request may have been served; the layer cannot know."""

        for code in ("GATEWAY_TIMEOUT", "GATEWAY_MALFORMED_RESPONSE", "GATEWAY_OUTCOME_UNCERTAIN"):
            self.assertTrue(gateway.GATEWAY_REFUSALS[code])
            self.assertEqual(gateway.may_reroute(code, False),
                             (False, "OUTCOME_UNCERTAIN_BY_REFUSAL_CODE"))

    def test_a_pre_spawn_refusal_with_proof_may_be_rerouted(self):
        for code in ("GATEWAY_AUTHENTICATION_FAILED", "GATEWAY_INSUFFICIENT_CREDITS",
                     "GATEWAY_RATE_LIMITED", "GATEWAY_OUTAGE", "GATEWAY_MODEL_UNAVAILABLE",
                     "GATEWAY_TOOL_CAPABILITY_UNSUPPORTED"):
            self.assertFalse(gateway.GATEWAY_REFUSALS[code])
            self.assertEqual(gateway.may_reroute(code, False), (True, "PRE_DISPATCH"))

    def test_every_direct_unavailability_reason_is_a_pre_spawn_fact(self):
        self.assertEqual(set(gateway.DIRECT_UNAVAILABLE_REASONS), {
            "quota_exhausted", "rate_limited", "authentication_failed",
            "insufficient_credits", "provider_unavailable", "conserved"})


class ReportedFactTests(unittest.TestCase):
    def test_a_silent_gateway_reports_unknown_and_never_zero(self):
        facts = gateway.facts_from_response({}, gateway.GatewayProfile("gw", model_slug=SLUG))
        for field in ("actual_model", "actual_provider", "generation_id",
                      "input_tokens", "output_tokens", "retries"):
            self.assertEqual(facts[field], "unknown", field)
        self.assertEqual(facts["cost_state"], "unknown")
        self.assertIsNone(facts["cost_amount"])
        self.assertNotEqual(facts["input_tokens"], 0)

    def test_the_requested_model_is_the_controllers_and_the_actual_model_is_not(self):
        """Echoing the request as the answer would hide a failover completely."""

        facts = gateway.facts_from_response({}, gateway.GatewayProfile("gw", model_slug=SLUG))
        self.assertEqual(facts["requested_model"], SLUG)
        self.assertEqual(facts["actual_model"], "unknown")

    def test_a_priced_leg_keeps_amount_currency_and_the_reported_state(self):
        facts = gateway.facts_from_response(
            {"cost_amount": 0.5, "cost_currency": "USD", "input_tokens": 10}, None)
        self.assertEqual((facts["cost_amount"], facts["cost_currency"], facts["cost_state"]),
                         (0.5, "USD", "reported"))
        self.assertEqual(facts["evidence_class"], "reported_claim")

    def test_a_declared_absence_word_survives_and_an_invented_one_does_not(self):
        self.assertEqual(gateway.facts_from_response({"cost_state": "not_applicable"}, None)
                         ["cost_state"], "not_applicable")
        self.assertEqual(gateway.facts_from_response({"cost_state": "unavailable"}, None)
                         ["cost_state"], "unknown")

    def test_no_gateway_block_and_no_profile_means_no_gateway_facts(self):
        self.assertIsNone(gateway.facts_from_response(None, None))

    def test_an_undeclared_substitution_is_caught_by_the_receipt(self):
        policy = gateway.GatewayPolicy(enabled=True, allowed_model_slugs=(SLUG,))
        profile = gateway.GatewayProfile("gw", model_slug=SLUG)
        served = gateway.facts_from_response({"actual_model": OTHER}, profile)
        self.assertEqual(gateway.undeclared_failover(served, policy, profile),
                         "GATEWAY_UNDECLARED_MODEL_SUBSTITUTION")

    def test_a_declared_failover_is_permitted(self):
        policy = gateway.GatewayPolicy(enabled=True, allowed_model_slugs=(SLUG, OTHER),
                                       fallback_models=(OTHER,))
        profile = gateway.GatewayProfile("gw", model_slug=SLUG)
        served = gateway.facts_from_response({"actual_model": OTHER}, profile)
        self.assertIsNone(gateway.undeclared_failover(served, policy, profile))

    def test_an_unconfirmed_privacy_requirement_is_a_refusal(self):
        policy = gateway.GatewayPolicy(enabled=True, required_privacy=("zero_data_retention",))
        self.assertEqual(gateway.privacy_refusal({"privacy_enforced": []}, policy),
                         "GATEWAY_PRIVACY_NOT_CONFIRMED")
        self.assertIsNone(gateway.privacy_refusal(
            {"privacy_enforced": ["zero_data_retention"]}, policy))


class EndToEndTests(RouteTestCase, unittest.TestCase):
    def run_mission(self, adapter, payload=None, key="gw-key"):
        controller, store, path = self.build(adapter, lease_seconds=0)
        controller.submit(payload or gateway_mission(), key)
        return controller.work_once("w1"), store

    def test_a_direct_harness_is_preferred_while_it_is_available(self):
        layer = GatewayLayer()
        mission, store = self.run_mission(layer)
        self.assertEqual(mission["state"], "completed")
        self.assertEqual([route["provider_profile"] for route in layer.routes], [ALPHA])
        self.assertIsNone(store.runs(mission["id"])[0]["receipt"]["gateway"])

    def test_quota_exhaustion_selects_the_gateway_before_dispatch_exactly_once(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted", direct_started=False)
        mission, store = self.run_mission(layer)
        self.assertEqual(mission["state"], "completed")
        self.assertEqual([route["provider_profile"] for route in layer.routes], [ALPHA, "gw-1"])
        legs = store.runs(mission["id"])
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[1]["receipt"]["gateway"]["actual_model"], SLUG)
        self.assertEqual(legs[1]["receipt"]["gateway"]["cost_amount"], 0.0182)
        self.assertEqual(mission["result"]["dispatch"]["candidate_sha"], "b" * 40)

    def test_the_admitted_model_slug_crosses_the_wire_and_no_credential_does(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted")
        self.run_mission(layer)
        route = layer.routes[1]
        self.assertEqual(route["gateway"], {"gateway": "openrouter", "model_slug": SLUG,
                                            "privacy": ["zero_data_retention"]})
        self.assertNotIn("token", str(route).lower())
        self.assertNotIn("key", set(route["gateway"]))

    def test_an_unproven_direct_failure_never_reaches_the_gateway(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted", direct_started=None)
        mission, _ = self.run_mission(layer)
        self.assertEqual(mission["state"], "refused")
        self.assertIn("PROVIDER_SWITCH_AFTER_SIDE_EFFECT", mission["terminal_reason"])
        self.assertEqual(len(layer.routes), 1)

    def test_a_gateway_timeout_stops_the_mission_rather_than_re_routing(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted",
                             gateway_refusal="GATEWAY_TIMEOUT", gateway_started=False)
        mission, _ = self.run_mission(layer)
        self.assertEqual(mission["state"], "refused")
        self.assertIn("PROVIDER_SWITCH_AFTER_UNCERTAIN_OUTCOME", mission["terminal_reason"])

    def test_an_unadmitted_gateway_profile_is_recorded_and_never_offered(self):
        payload = gateway_mission(gateway_policy={"enabled": True,
                                                  "allowed_model_slugs": [OTHER]})
        layer = GatewayLayer(direct_refusal="quota_exhausted")
        mission, store = self.run_mission(layer, payload)
        self.assertEqual(mission["state"], "refused")
        self.assertEqual([route["provider_profile"] for route in layer.routes], [ALPHA])
        refusals = [event for event in store.history(mission["id"])
                    if event["kind"] == "GATEWAY_PROFILE_REFUSED"]
        self.assertEqual(refusals[0]["detail"]["code"], "GATEWAY_MODEL_NOT_ALLOWLISTED")

    def test_a_substituted_model_fails_the_mission_after_the_fact(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted",
                             facts={"actual_model": OTHER, "actual_provider": "vendor",
                                    "privacy_enforced": ["zero_data_retention"]})
        mission, store = self.run_mission(layer)
        self.assertIn("GATEWAY_UNDECLARED_MODEL_SUBSTITUTION", mission["terminal_reason"])
        # The state is `refused` rather than `failed` because the receipt checks
        # run before the mission leaves `dispatching` -- pre-existing Stage-2
        # behaviour, shared with PROVIDER_POLICY_VIOLATION.  The fact that a
        # process did run is not lost: the leg records it.
        self.assertEqual(mission["state"], "refused")
        self.assertIs(store.runs(mission["id"])[-1]["receipt"]["process_started"], True)

    def test_an_unconfirmed_privacy_term_fails_the_mission(self):
        layer = GatewayLayer(direct_refusal="quota_exhausted",
                             facts={"actual_model": SLUG, "privacy_enforced": []})
        mission, store = self.run_mission(layer)
        self.assertEqual(mission["state"], "refused")
        self.assertIn("GATEWAY_PRIVACY_NOT_CONFIRMED", mission["terminal_reason"])
        self.assertIs(store.runs(mission["id"])[-1]["receipt"]["process_started"], True)

    def test_removing_the_gateway_configuration_leaves_direct_operation_intact(self):
        """Disabling the gateway must not be a Factory outage."""

        payload = mission_payload(provider_candidates=[{"profile": ALPHA}])
        mission, store = self.run_mission(GatewayLayer(), payload, key="direct-only")
        self.assertEqual(mission["state"], "completed")
        self.assertEqual(store.runs(mission["id"])[0]["provider_profile"], ALPHA)

    def test_the_same_logical_mission_runs_either_way_with_one_contract(self):
        """Portability: the mission and evidence contracts do not change."""

        direct, _ = self.run_mission(GatewayLayer(), key="portable-direct")
        viagw, _ = self.run_mission(GatewayLayer(direct_refusal="conserved"),
                                    key="portable-gateway")
        for mission in (direct, viagw):
            self.assertEqual(mission["state"], "completed")
            self.assertEqual(set(mission["result"]),
                             {"dispatch", "verification", "evaluation", "evidence"})
            self.assertEqual(mission["result"]["evidence"]["evidence_pointer"], "e" * 64)
            self.assertEqual(mission["payload"]["acceptance_gate_ids"], ["G"])

    def test_a_malformed_gateway_policy_is_refused_at_submission(self):
        controller, _, _ = self.build(GatewayLayer())
        with self.assertRaises(NonRetryableFailure) as caught:
            controller.submit(gateway_mission(gateway_policy={
                "enabled": True, "allowed_model_slugs": [SLUG],
                "fallback_models": [OTHER]}), "bad-policy")
        self.assertIn("INVALID_GATEWAY_POLICY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
