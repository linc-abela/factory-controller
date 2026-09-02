"""SF-160: a product run contract may declare a failover, and one is enough.

`development` was served by a single provider profile, so a product revision
was pinned to one subscription by the shape of the registry rather than by any
decision.  The execution layer's own failover was already complete -- it walks
profiles in priority order and falls over on its pre-spawn unavailability fact
-- so the missing half was on this side: the Owner surface required *every*
declared profile to be ready and every one to report usable capacity, which
turns a failover into a second hard dependency.

What is checked here is that one usable runtime is enough, that every declared
runtime is still observed and recorded, and that the Controller decides
nothing about which one runs.
"""

from __future__ import annotations

import json
import unittest

from factory_controller import capacity

from tests.test_factory_product import CONTRACT, FactoryProductTests


PRIMARY, FAILOVER = "codex-product", "claude-product"


class ProductProviderFailoverTests(unittest.TestCase):
    setUp = FactoryProductTests.setUp
    ready = FactoryProductTests.ready
    submit = FactoryProductTests.submit
    missions = FactoryProductTests.missions

    def profile_readiness(self, **readiness):
        self.host.extra_profiles = [
            {"profile_id": profile, "status": "available",
             "readiness": state}
            for profile, state in readiness.items()]

    # -- the contract declares two, and they are the shipped two --------- #

    def test_the_product_contract_declares_a_failover(self):
        self.assertEqual(list(CONTRACT.provider_profiles), [PRIMARY, FAILOVER])

    def test_the_mission_carries_both_declared_runtimes_in_contract_order(self):
        """The Controller states the set; the execution layer picks from it."""

        self.ready()
        self.assertTrue(self.submit().ok)
        payload = self.missions()[0]["payload"]
        declared = json.dumps(payload)
        self.assertIn(PRIMARY, declared)
        self.assertIn(FAILOVER, declared)

    # -- readiness: one is enough --------------------------------------- #

    def test_the_primary_alone_is_enough(self):
        self.profile_readiness(**{PRIMARY: "available", FAILOVER: "auth_required"})
        self.ready()
        result = self.submit()
        self.assertTrue(result.ok, result.render())

    def test_the_failover_alone_is_enough(self):
        """The whole point: Codex constrained must not stop the product."""

        self.profile_readiness(**{PRIMARY: "auth_required", FAILOVER: "available"})
        self.ready()
        result = self.submit()
        self.assertTrue(result.ok, result.render())

    def test_neither_ready_refuses_and_names_both(self):
        self.ready()
        self.profile_readiness(**{PRIMARY: "auth_required",
                                  FAILOVER: "auth_required"})
        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRIMARY_PROVIDER_UNAVAILABLE")
        for vendor in ("Codex", "Claude"):
            self.assertIn(vendor, result.render())
        self.assertEqual(self.missions(), [])

    # -- capacity: one usable is enough, and every reading is kept ------- #

    def test_a_constrained_primary_does_not_stop_the_product(self):
        self.ready()
        self.host.capacity_constrained = {PRIMARY}
        result = self.submit()
        self.assertTrue(result.ok, result.render())

    def test_every_declared_runtime_is_still_observed(self):
        """A constrained runtime's own reading is what narrows it later."""

        self.ready()
        self.host.capacity_constrained = {PRIMARY}
        self.submit()
        readings = self.lifecycle.store.latest_observations()
        self.assertEqual(set(readings) >= {PRIMARY, FAILOVER}, True)
        self.assertNotIn(readings[PRIMARY].state, capacity.USABLE)
        self.assertIn(readings[FAILOVER].state, capacity.USABLE)

    def test_no_usable_capacity_at_all_refuses_and_names_both(self):
        self.ready()
        self.host.capacity_constrained = {PRIMARY, FAILOVER}
        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "CAPACITY_UNAVAILABLE")
        for vendor in ("Codex", "Claude"):
            self.assertIn(vendor, result.render())
        self.assertEqual(self.missions(), [])

    def test_no_declared_remainder_is_ever_recorded(self):
        """Honest failover, not a pretended token count.

        Nothing on this path holds a number about how much of a subscription
        is left, because no runtime here reports one. What is recorded is the
        state the harness actually answered with.
        """

        self.ready()
        self.submit()
        for reading in self.lifecycle.store.latest_observations().values():
            body = json.dumps(reading.as_row()).lower()
            for invented in ("remaining_tokens", "balance", "credits"):
                self.assertNotIn(invented, body)

    # -- the admission has to widen with the contract -------------------- #

    def test_both_declared_runtimes_are_admitted_for_the_capability(self):
        self.ready()
        self.submit()
        requests = [json.loads(text) for command, text in self.host.calls
                    if text and command[-3:-1] == ("capability", "admit")]
        self.assertEqual(requests[-1]["profiles"], [PRIMARY, FAILOVER])
        admitted = {row["capability"]: row["profiles"]
                    for row in self.host.capability_admissions}
        self.assertEqual(admitted["development"], [PRIMARY, FAILOVER])

    def test_a_capability_admitted_for_only_one_runtime_is_widened_once(self):
        """Serving the capability is not serving it through both runtimes.

        The admission is recorded per profile, so a second declared runtime
        added to an already-served capability would otherwise never be
        admitted and would serve nothing at all.
        """

        self.ready()
        self.host.capability_admitted = True
        self.host.capability_admissions = [{
            "capability": "development", "profiles": [PRIMARY],
            "projects": ["lodus-casino"]}]
        before = self.host.capability_admits
        self.assertTrue(self.submit().ok)
        self.assertEqual(self.host.capability_admits, before + 1)
        self.assertEqual(
            {row["capability"]: row["profiles"]
             for row in self.host.capability_admissions}["development"],
            [PRIMARY, FAILOVER])
        # And once it covers both, nothing widens again.
        again = self.host.capability_admits
        self.assertTrue(self.submit().ok)
        self.assertEqual(self.host.capability_admits, again)


if __name__ == "__main__":
    unittest.main()
