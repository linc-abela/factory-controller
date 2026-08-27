"""The Phase-1 runtime palette, admitted without the Controller knowing a brand.

Three runtimes are required MVP capacity and a fourth is optional gateway
capacity.  The Controller cannot tell them apart, and that is the property
under test: every one of these assertions is about *profile strings*, because
if the Controller could recognise a vendor it could also depend on one.
"""

from __future__ import annotations

import unittest

from factory_controller import routing

#: Opaque to this package by construction.  The strings are the shipped
#: `factory-bridge` profile ids; the Controller orders them and nothing else.
PALETTE = ("codex-primary", "cursor-secondary", "claude-secondary")
OPTIONAL_GATEWAY = "gateway-overflow"


def candidates(*profiles):
    return [routing.Candidate(profile, ("prototype",)) for profile in profiles]


class RuntimePaletteTests(unittest.TestCase):

    def test_all_three_required_runtimes_are_admissible(self):
        selection = routing.select(routing.ExecutionPolicy(), candidates(*PALETTE))
        self.assertEqual(selection.profile, PALETTE[0])
        self.assertEqual({item.reason for item in selection.considered},
                         {"admissible"})

    def test_selection_walks_the_palette_in_declared_order(self):
        attempted = []
        for expected in PALETTE:
            selection = routing.select(routing.ExecutionPolicy(),
                                       candidates(*PALETTE), attempted)
            self.assertEqual(selection.profile, expected)
            attempted.append(expected)
        exhausted = routing.select(routing.ExecutionPolicy(),
                                   candidates(*PALETTE), attempted)
        self.assertIsNone(exhausted.profile)

    def test_a_route_can_reach_every_member_within_the_default_leg_bound(self):
        """Three required runtimes and a bound of three legs is not a coincidence."""
        self.assertGreaterEqual(routing.DEFAULT_MAX_ROUTE_LEGS, len(PALETTE) - 1)

    def test_any_one_runtime_can_be_denied_without_disturbing_the_others(self):
        for denied in PALETTE:
            selection = routing.select(
                routing.ExecutionPolicy(denied_profiles=(denied,)),
                candidates(*PALETTE))
            self.assertNotEqual(selection.profile, denied)
            self.assertIsNotNone(selection.profile)

    def test_the_optional_gateway_is_never_reached_unless_it_is_declared(self):
        """Optional capacity must not become a dependency.

        The Factory works with the required palette alone: the gateway profile
        is simply absent from the candidate list, and absence needs no policy.
        """
        selection = routing.select(routing.ExecutionPolicy(), candidates(*PALETTE))
        self.assertNotIn(OPTIONAL_GATEWAY,
                         [item.profile for item in selection.considered])

    def test_disabling_the_optional_gateway_leaves_the_palette_serving(self):
        selection = routing.select(
            routing.ExecutionPolicy(denied_profiles=(OPTIONAL_GATEWAY,)),
            candidates(*PALETTE, OPTIONAL_GATEWAY))
        self.assertEqual(selection.profile, PALETTE[0])
        denied = [item for item in selection.considered
                  if item.profile == OPTIONAL_GATEWAY]
        self.assertEqual([item.reason for item in denied], ["denied_by_policy"])

    def test_the_palette_alone_still_serves_when_only_the_gateway_is_allowed_out(self):
        """The whole required set exhausted, with the gateway denied, is a refusal.

        Stated explicitly so nobody later 'fixes' it by falling through to the
        optional gateway, which is the exact shape of an optional capability
        becoming required.
        """
        selection = routing.select(
            routing.ExecutionPolicy(denied_profiles=(OPTIONAL_GATEWAY,)),
            candidates(*PALETTE, OPTIONAL_GATEWAY), attempted=PALETTE)
        self.assertIsNone(selection.profile)
        self.assertEqual(selection.refusal_code, "PROVIDER_ROUTE_EXHAUSTED")

    def test_an_allowlist_pins_the_palette_exactly(self):
        selection = routing.select(
            routing.ExecutionPolicy(allowed_profiles=PALETTE),
            candidates(*PALETTE, OPTIONAL_GATEWAY))
        excluded = [item for item in selection.considered
                    if item.reason == "not_in_allowlist"]
        self.assertEqual([item.profile for item in excluded], [OPTIONAL_GATEWAY])

    def test_the_controller_recognises_no_member_of_the_palette_by_name(self):
        """Substituting nonsense for every profile changes nothing at all."""
        real = routing.select(routing.ExecutionPolicy(), candidates(*PALETTE))
        nonsense = ("aaa", "bbb", "ccc")
        fake = routing.select(routing.ExecutionPolicy(), candidates(*nonsense))
        self.assertEqual(fake.profile, nonsense[0])
        self.assertEqual([item.reason for item in fake.considered],
                         [item.reason for item in real.considered])


if __name__ == "__main__":
    unittest.main()
