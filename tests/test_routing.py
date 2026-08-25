"""Unit tests for the provider-neutral policy, selection, receipt, and budget layer."""

from __future__ import annotations

import unittest

from factory_controller import routing
from factory_controller.routing import (
    Candidate,
    ExecutionPolicy,
    PolicyError,
    Selection,
    Usage,
)


ALPHA = Candidate("provider-alpha", ("implement",))
BETA = Candidate("provider-beta", ("implement", "review"))
GAMMA = Candidate("provider-gamma", ("review",))


class PolicyTests(unittest.TestCase):
    def test_absent_policy_is_permissive_but_bounded(self):
        policy = ExecutionPolicy.from_payload({})
        self.assertEqual(policy.allowed_profiles, ())
        self.assertFalse(policy.no_fallback)
        self.assertEqual(policy.max_route_legs, routing.DEFAULT_MAX_ROUTE_LEGS)
        self.assertIsNone(policy.budget_ceiling)

    def test_budget_ceiling_requires_a_currency(self):
        with self.assertRaises(PolicyError):
            ExecutionPolicy.from_payload({"execution_policy": {"budget_ceiling": 5}})

    def test_ceiling_must_be_positive_and_legs_at_least_one(self):
        for bad in ({"budget_ceiling": 0, "budget_currency": "USD"}, {"max_route_legs": 0}):
            with self.assertRaises(PolicyError):
                ExecutionPolicy.from_payload({"execution_policy": bad})

    def test_malformed_policy_fields_refuse_rather_than_coerce(self):
        for bad in ({"allowed_profiles": "alpha"}, {"no_fallback": "yes"},
                    {"max_route_legs": "3"}, {"required_capability": ""}):
            with self.assertRaises(PolicyError):
                ExecutionPolicy.from_payload({"execution_policy": bad})

    def test_candidates_accept_plain_strings_and_reject_duplicates(self):
        parsed = routing.candidates_from_payload({"provider_candidates": ["a", {"profile": "b"}]})
        self.assertEqual([item.profile for item in parsed], ["a", "b"])
        with self.assertRaises(PolicyError):
            routing.candidates_from_payload({"provider_candidates": ["a", "a"]})


class SelectionTests(unittest.TestCase):
    def test_first_admissible_in_declared_order_wins(self):
        selection = routing.select(ExecutionPolicy(), [ALPHA, BETA])
        self.assertEqual(selection.profile, "provider-alpha")
        self.assertEqual(selection.reason, "first_admissible")

    def test_selection_is_deterministic(self):
        policy = ExecutionPolicy(denied_profiles=("provider-alpha",))
        first = routing.select(policy, [ALPHA, BETA, GAMMA], ("provider-beta",))
        second = routing.select(policy, [ALPHA, BETA, GAMMA], ("provider-beta",))
        self.assertEqual(first, second)
        self.assertEqual(first.profile, "provider-gamma")

    def test_denied_beats_allowed_when_a_profile_is_on_both_lists(self):
        policy = ExecutionPolicy(allowed_profiles=("provider-alpha",),
                                 denied_profiles=("provider-alpha",))
        selection = routing.select(policy, [ALPHA, BETA])
        self.assertIsNone(selection.profile)
        self.assertEqual(selection.refusal_code, "NO_ADMISSIBLE_PROVIDER")
        self.assertEqual(selection.considered[0].reason, "denied_by_policy")

    def test_allowlist_excludes_everything_not_named(self):
        selection = routing.select(ExecutionPolicy(allowed_profiles=("provider-beta",)),
                                   [ALPHA, BETA])
        self.assertEqual(selection.profile, "provider-beta")
        self.assertEqual(selection.considered[0].reason, "not_in_allowlist")

    def test_required_capability_excludes_a_profile_that_does_not_offer_it(self):
        selection = routing.select(ExecutionPolicy(required_capability="implement"),
                                   [GAMMA, BETA])
        self.assertEqual(selection.profile, "provider-beta")
        self.assertEqual(selection.considered[0].reason, "capability_not_offered")

    def test_fallback_names_the_profile_it_followed(self):
        selection = routing.select(ExecutionPolicy(), [ALPHA, BETA], ("provider-alpha",))
        self.assertEqual(selection.profile, "provider-beta")
        self.assertEqual(selection.reason, "fallback_after:provider-alpha")

    def test_no_fallback_policy_refuses_a_second_leg(self):
        selection = routing.select(ExecutionPolicy(no_fallback=True), [ALPHA, BETA],
                                   ("provider-alpha",))
        self.assertEqual(selection.refusal_code, "PROVIDER_FALLBACK_FORBIDDEN")

    def test_route_leg_limit_is_enforced_before_candidates_run_out(self):
        selection = routing.select(ExecutionPolicy(max_route_legs=1), [ALPHA, BETA],
                                   ("provider-alpha",))
        self.assertEqual(selection.refusal_code, "PROVIDER_ROUTE_EXHAUSTED")

    def test_every_candidate_is_explained_even_when_one_is_chosen(self):
        selection = routing.select(ExecutionPolicy(denied_profiles=("provider-alpha",)),
                                   [ALPHA, BETA])
        self.assertEqual([item.reason for item in selection.considered],
                         ["denied_by_policy", "admissible"])


class UsageTests(unittest.TestCase):
    def test_absent_usage_is_unknown_and_never_zero(self):
        usage = routing.usage_from_response(None)
        self.assertEqual(usage.cost_state, "unknown")
        self.assertIsNone(usage.cost_amount)
        self.assertIsNone(usage.input_tokens)

    def test_reported_cost_requires_both_amount_and_currency(self):
        partial = routing.usage_from_response({"cost_amount": 1.5})
        self.assertEqual(partial.cost_state, "unknown")
        self.assertIsNone(partial.cost_amount)

    def test_a_declared_absence_word_survives_verbatim(self):
        for word in sorted(routing.CANONICAL_ABSENCE):
            self.assertEqual(routing.usage_from_response({"cost_state": word}).cost_state, word)

    def test_an_unrecognised_absence_word_becomes_unknown(self):
        self.assertEqual(routing.usage_from_response({"cost_state": "free"}).cost_state, "unknown")

    def test_a_fabricated_amount_beside_an_absence_word_is_refused(self):
        with self.assertRaises(PolicyError):
            Usage(cost_amount=1.0, cost_currency="USD", cost_state="unknown")

    def test_negative_and_boolean_token_counts_are_rejected(self):
        usage = routing.usage_from_response({"input_tokens": -1, "output_tokens": True})
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.output_tokens)


class ReceiptTests(unittest.TestCase):
    @staticmethod
    def _receipt(**raw):
        return routing.receipt_from_response(
            {"status": "completed", "receipt": raw}, Selection("p", "first_admissible", ()), ())

    def test_an_unstated_process_start_is_treated_as_possible(self):
        self.assertIsNone(self._receipt().process_started)
        self.assertTrue(self._receipt().side_effect_possible)

    def test_only_an_explicit_false_unlocks_a_reroute(self):
        self.assertFalse(self._receipt(process_started=False).side_effect_possible)
        self.assertTrue(self._receipt(process_started=True).side_effect_possible)

    def test_an_unrecognised_execution_mode_is_unknown_not_assumed(self):
        self.assertEqual(self._receipt(execution_mode="production").execution_mode, "unknown")
        self.assertEqual(self._receipt(execution_mode="real").execution_mode, "real")

    def test_the_receipt_carries_provider_claims_as_claims(self):
        self.assertEqual(self._receipt().evidence_class, "reported_claim")

    def test_an_unserved_leg_proves_nothing_ran(self):
        receipt = routing.unserved_receipt(Selection(None, "x", ()), (), "NO_ADMISSIBLE_PROVIDER")
        self.assertFalse(receipt.side_effect_possible)
        self.assertEqual(receipt.usage.cost_state, "not_applicable")


class BudgetTests(unittest.TestCase):
    POLICY = ExecutionPolicy(budget_ceiling=10.0, budget_currency="USD")

    @staticmethod
    def _priced(amount, currency="USD"):
        return routing.Receipt(
            provider_profile="p", provider=None, selection_reason="r", fallback_chain=(),
            selection_trace=(), process_started=True, duration_ms=None, classification="completed",
            refusal_code=None, execution_mode="real", idempotency_key="k",
            usage=Usage(cost_amount=amount, cost_currency=currency, cost_state="reported"))

    @staticmethod
    def _unpriced():
        return routing.Receipt(
            provider_profile="p", provider=None, selection_reason="r", fallback_chain=(),
            selection_trace=(), process_started=True, duration_ms=None, classification="completed",
            refusal_code=None, execution_mode="real", idempotency_key="k")

    def test_no_ceiling_is_not_applicable_rather_than_unlimited(self):
        state = routing.accumulate(ExecutionPolicy(), [self._priced(1.0)])
        self.assertEqual(state.state, "not_applicable")
        self.assertIsNone(routing.refuse_dispatch(state))

    def test_spend_below_the_ceiling_permits_the_next_dispatch(self):
        state = routing.accumulate(self.POLICY, [self._priced(4.0), self._priced(5.0)])
        self.assertEqual(state.state, "within")
        self.assertEqual(state.known_spend, 9.0)
        self.assertIsNone(routing.refuse_dispatch(state))

    def test_reaching_the_ceiling_fails_closed(self):
        state = routing.accumulate(self.POLICY, [self._priced(10.0)])
        self.assertTrue(state.exhausted)
        self.assertEqual(routing.refuse_dispatch(state), "MISSION_BUDGET_EXHAUSTED")

    def test_unknown_cost_is_counted_as_unpriced_and_never_estimated(self):
        state = routing.accumulate(self.POLICY, [self._priced(2.0), self._unpriced()])
        self.assertEqual(state.known_spend, 2.0)
        self.assertEqual(state.unpriced_legs, 1)
        self.assertIsNone(routing.refuse_dispatch(state))

    def test_a_foreign_currency_is_never_converted_and_fails_closed(self):
        state = routing.accumulate(self.POLICY, [self._priced(3.0, "EUR")])
        self.assertEqual(state.known_spend, 0.0)
        self.assertEqual(state.currency_conflicts, 1)
        self.assertEqual(routing.refuse_dispatch(state), "MISSION_BUDGET_CURRENCY_MISMATCH")


class VocabularyTests(unittest.TestCase):
    """These values are factory-evidence-core's. A fork here is a defect."""

    def test_absence_vocabulary_matches_contracts_replay(self):
        self.assertEqual(routing.CANONICAL_ABSENCE,
                         frozenset({"unknown", "not_applicable", "not_run", "not_measurable"}))

    def test_bridge_result_statuses_match_orchestration_verification(self):
        self.assertEqual(routing.BRIDGE_RESULT_STATUSES,
                         ("completed", "blocked", "refused", "no_candidate", "partial_result"))

    def test_provider_unavailable_is_not_a_bridge_envelope_status(self):
        self.assertNotIn(routing.PROVIDER_UNAVAILABLE, routing.BRIDGE_RESULT_STATUSES)

    def test_expected_idempotency_key_matches_evidence_core(self):
        self.assertEqual(routing.expected_idempotency_key("SF-1", "a" * 64), "SF-1:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
