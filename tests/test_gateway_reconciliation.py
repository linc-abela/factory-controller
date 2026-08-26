"""Reconciliation against the landed `factory-bridge` OpenRouter surface.

Read against `factory-bridge` `cb1d02b7bd0efe55301d5a6b6a71584445647d8a`
(SF-137C), whose `src/factory_bridge/openrouter.py` is a metered tool loop with
its own receipt schema.  Both sides built a model-gateway contract in parallel
and neither could see the other, so what matters is what survives the wire.

Three facts are recorded here as executable checks rather than as prose.

**GR-1.  The executor has no caller on the bridge's request path.**  At
cb1d02b7, `openrouter.py` is imported by `tests/test_openrouter.py` and by
nothing in `src/` -- not `adapter.py`, `service.py`, `provider.py` or
`protocol.py` -- so a metered gateway execution cannot yet be *requested*
through the bridge protocol, and no `metered_execution_receipt.v1` reaches a
Controller over the wire.  This Controller's seam is therefore specified and
tested against a translation of their frozen record shape.  That is a
sequencing fact about parallel work, not a defect in it.

**GR-2.  Only three refusal codes are marked pre-spawn.**  `OpenRouterError`
defaults `dispatch_started=True`, and exactly `CONTEXT_TOO_LARGE`,
`LANE_INVALID` and `MODEL_NOT_ALLOWED` override it.  So `AUTH_FAILED` and
`QUOTA_EXHAUSTED` -- a rejected key and an exhausted balance, both pre-spawn
facts in principle -- arrive marked "may have run".  The default errs safe and
the Controller does not second-guess it: an unproven negative is not a proof,
whichever side failed to prove it.  The consequence is recorded below, because
it means the brief's pre-dispatch gateway selection cannot be driven by those
two codes as the bridge reports them today.

**GR-3.  The bridge reports a provider allowlist, never a serving provider,
and no data-policy enforcement at all.**  So `actual_provider` stays `unknown`
rather than being filled in from the allowlist, and a mission requiring
zero-data-retention is refused against a real bridge receipt.  Failing closed
is the correct outcome; it is recorded so nobody reads the refusal as a bug.

Nothing is edited in the sibling's repository.
"""

from __future__ import annotations

import unittest

from factory_controller import gateway
from factory_controller.engine import NonRetryableFailure
from tests.support import ALPHA, RouteTestCase, mission_payload
from tests.test_model_gateway import SLUG, GatewayLayer, gateway_mission


#: Verified at cb1d02b7 by reading `openrouter.py`.  Reproduced, not imported.
BRIDGE_CODES = (
    "AUTH_FAILED", "CONTEXT_TOO_LARGE", "DISCONNECTED", "GATEWAY_REJECTED",
    "INVALID_EXECUTION_RESULT", "LANE_INVALID", "MALFORMED_RESPONSE",
    "MODEL_MISMATCH", "MODEL_NOT_ALLOWED", "PATH_ESCAPE", "PROVIDER_MISMATCH",
    "PROVIDER_UNAVAILABLE", "QUOTA_EXHAUSTED", "RATE_LIMITED",
    "REPEATED_TOOL_CALL", "REQUEST_TOO_LARGE", "RESPONSE_TOO_LARGE",
    "RESPONSE_TRUNCATED", "RUNAWAY", "TEST_FAILED", "TIMEOUT", "TOOL_FAILED",
    "UNSUPPORTED_TOOL_CALL",
)

BRIDGE_RECEIPT = {
    "schema_version": "factory.bridge.metered_execution_receipt.v1",
    "profile_id": "openrouter-primary",
    "model": SLUG,
    "provider_allowlist": ["vendor-a", "vendor-b"],
    "turns": 4, "commands": 6,
    "usage": {"prompt_tokens": 1200, "completion_tokens": 340,
              "total_tokens": 1540, "precision": "exact"},
    "cost": {"usd": "0.01824000", "precision": "exact"},
    "candidate_sha": "b" * 40,
    "transcript_hash": "c" * 64,
}


class SchemaTests(unittest.TestCase):
    def test_the_schema_name_is_the_bridges_own(self):
        self.assertEqual(gateway.BRIDGE_RECEIPT_SCHEMA,
                         "factory.bridge.metered_execution_receipt.v1")

    def test_a_receipt_in_another_schema_is_not_read_as_this_one(self):
        self.assertIsNone(gateway.reconcile_bridge_receipt(
            {**BRIDGE_RECEIPT, "schema_version": "something.else.v1"}))
        self.assertIsNone(gateway.reconcile_bridge_receipt("not a receipt"))

    def test_exact_usage_and_cost_survive_the_translation(self):
        facts = gateway.reconcile_bridge_receipt(BRIDGE_RECEIPT)
        self.assertEqual(facts["input_tokens"], 1200)
        self.assertEqual(facts["output_tokens"], 340)
        self.assertEqual(facts["total_tokens"], 1540)
        self.assertEqual(facts["cost_state"], "reported")
        self.assertEqual(facts["cost_currency"], "USD")

    def test_the_exact_decimal_is_kept_beside_the_float_the_budget_uses(self):
        """Turning their string into a float is a loss, so both are carried."""

        facts = gateway.reconcile_bridge_receipt(BRIDGE_RECEIPT)
        self.assertEqual(facts["cost_amount_text"], "0.01824000")
        self.assertEqual(facts["cost_amount"], 0.01824)

    def test_their_precision_vocabulary_needs_no_fifth_absence_word(self):
        """`exact`/`unknown`; `unknown` is already one of Evidence Core's four."""

        inexact = gateway.reconcile_bridge_receipt({
            **BRIDGE_RECEIPT,
            "usage": {"prompt_tokens": None, "completion_tokens": None,
                      "total_tokens": None, "precision": "unknown"},
            "cost": {"usd": None, "precision": "unknown"}})
        for field in ("input_tokens", "output_tokens", "total_tokens",
                      "cost_amount_text", "retries", "generation_id"):
            self.assertEqual(inexact[field], "unknown", field)
        self.assertIn("unknown", gateway.CANONICAL_ABSENCE)
        self.assertIsNone(inexact["cost_amount"])
        self.assertNotEqual(inexact["input_tokens"], 0)

    def test_the_provider_allowlist_is_not_mistaken_for_a_serving_provider(self):
        facts = gateway.reconcile_bridge_receipt(BRIDGE_RECEIPT)
        self.assertEqual(facts["provider_allowlist"], ("vendor-a", "vendor-b"))
        self.assertEqual(facts["actual_provider"], "unknown")

    def test_a_bridge_receipt_is_read_through_the_one_entry_point(self):
        self.assertEqual(gateway.facts_from_response(BRIDGE_RECEIPT, None),
                         gateway.reconcile_bridge_receipt(BRIDGE_RECEIPT))


class RefusalTranslationTests(unittest.TestCase):
    def test_every_mapped_code_is_one_the_bridge_actually_raises(self):
        self.assertTrue(set(gateway.BRIDGE_REFUSALS) <= set(BRIDGE_CODES))

    def test_an_unmapped_code_is_treated_as_an_uncertain_outcome(self):
        """The safe direction for a name this Controller has never seen."""

        for code in set(BRIDGE_CODES) - set(gateway.BRIDGE_REFUSALS):
            translated, _ = gateway.from_bridge_error(code, True)
            self.assertEqual(translated, "GATEWAY_OUTCOME_UNCERTAIN")
            self.assertTrue(gateway.GATEWAY_REFUSALS[translated])

    def test_the_three_pre_spawn_codes_may_be_rerouted_and_the_rest_may_not(self):
        for code in gateway.BRIDGE_PRE_SPAWN_CODES:
            translated, started = gateway.from_bridge_error(code, False)
            self.assertFalse(started)
            self.assertEqual(gateway.may_reroute(translated, started), (True, "PRE_DISPATCH"))

    def test_quota_and_auth_arrive_marked_may_have_run_so_no_reroute_follows(self):
        """GR-2: the brief's pre-dispatch selection cannot ride on these codes."""

        for code in ("QUOTA_EXHAUSTED", "AUTH_FAILED", "RATE_LIMITED"):
            self.assertNotIn(code, gateway.BRIDGE_PRE_SPAWN_CODES)
            translated, started = gateway.from_bridge_error(code, True)
            self.assertTrue(started)
            self.assertEqual(gateway.may_reroute(translated, started),
                             (False, "SIDE_EFFECT_POSSIBLE"))

    def test_translation_adds_no_rule_of_its_own(self):
        """It renames and reports; `may_reroute` still decides."""

        translated, started = gateway.from_bridge_error("TIMEOUT", False)
        self.assertEqual(gateway.may_reroute(translated, started),
                         (False, "OUTCOME_UNCERTAIN_BY_REFUSAL_CODE"))


class EndToEndBridgeShapeTests(RouteTestCase, unittest.TestCase):
    def run_with(self, receipt, payload=None, key="bridge-shape"):
        layer = GatewayLayer(direct_refusal="quota_exhausted", facts=receipt)
        controller, store, _ = self.build(layer, lease_seconds=0)
        controller.submit(payload or gateway_mission(), key)
        return controller.work_once("w1"), store

    def test_a_real_bridge_receipt_reaches_the_ledger_intact(self):
        payload = gateway_mission(gateway_policy={
            "enabled": True, "allowed_model_slugs": [SLUG]})
        mission, store = self.run_with(BRIDGE_RECEIPT, payload)
        self.assertEqual(mission["state"], "completed")
        facts = store.runs(mission["id"])[-1]["receipt"]["gateway"]
        self.assertEqual(facts["receipt_schema"], gateway.BRIDGE_RECEIPT_SCHEMA)
        self.assertEqual(facts["cost_amount_text"], "0.01824000")
        self.assertEqual(facts["transcript_hash"], "c" * 64)

    def test_a_zero_data_retention_requirement_fails_closed_against_the_bridge(self):
        """GR-3: the bridge confirms no data policy, so the mission stops."""

        mission, _ = self.run_with(BRIDGE_RECEIPT)
        self.assertEqual(mission["state"], "refused")
        self.assertIn("GATEWAY_PRIVACY_NOT_CONFIRMED", mission["terminal_reason"])

    def test_a_bridge_priced_leg_counts_once_toward_a_project_budget(self):
        """`usage` reported nothing here, so the gateway's own figure is used."""

        from factory_controller import portfolio
        payload = gateway_mission(gateway_policy={
            "enabled": True, "allowed_model_slugs": [SLUG]}, project_id="alpha")
        layer = GatewayLayer(direct_refusal="quota_exhausted", facts=BRIDGE_RECEIPT)
        controller, store, _ = self.build(layer, lease_seconds=0)
        store.register_project(portfolio.ProjectPolicy("alpha", "repo://alpha"))
        controller.submit(payload, "budget-once")
        mission = controller.work_once("w1")
        spend = store.portfolio_economics("alpha")["projects"][0]["provider_spend"]
        self.assertEqual(spend["known_spend"], 0.01824)
        self.assertEqual(spend["priced_legs"], 1)
        self.assertEqual(spend["unpriced_legs"], 1, "the refused direct leg is unpriced")


if __name__ == "__main__":
    unittest.main()
