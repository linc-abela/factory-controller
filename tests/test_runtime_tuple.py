"""Admitted runtime tuple pins the selected Factory path."""

from __future__ import annotations

import unittest

from factory_controller import advisor, runtime_tuple


class AdmittedRuntimeTupleTests(unittest.TestCase):
    def test_tuple_names_current_bridge_not_the_obsolete_candidate(self):
        body = runtime_tuple.load()
        self.assertEqual(body["bridge_main"], runtime_tuple.admitted_bridge_sha())
        self.assertNotEqual(
            body["bridge_main"], advisor.FROZEN_BRIDGE_DEPENDENCY_SHA)
        self.assertEqual(
            body["bridge_main"],
            "43358a7024e36eba03327fbec14c5696afb4465e")

    def test_scheduled_manager_defaults_to_the_admitted_bridge_pin(self):
        port = advisor.scheduled_manager()
        if isinstance(port, advisor.BlockedAdvisor):
            self.skipTest("hermes is not installed on this host")
        self.assertEqual(
            port.expected_bridge_sha, runtime_tuple.admitted_bridge_sha())
        self.assertNotEqual(
            port.expected_bridge_sha, advisor.FROZEN_BRIDGE_DEPENDENCY_SHA)


if __name__ == "__main__":
    unittest.main()
