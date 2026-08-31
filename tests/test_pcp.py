"""Product Candidate Package intake and the canonical Casino materialization."""

from __future__ import annotations

from copy import deepcopy
import unittest

from factory_controller import pcp


class ProductCandidatePackageTests(unittest.TestCase):
    def test_casino_package_is_buildable_and_materializes_one_mission(self):
        package = pcp.materialize_casino_pcp()
        validated = pcp.validate(package)
        intake = pcp.intake(package)

        self.assertEqual(validated["package_id"], "lodus-casino")
        self.assertEqual(intake.verdict, "ACCEPTED")
        self.assertEqual(intake.mission["work_item_id"], "lodus-casino:build")
        self.assertEqual(intake.mission["source_pcp"], "lodus-casino@v1")
        self.assertEqual(intake.mission["capability"], "development")
        self.assertTrue(intake.mission["mutates_repository"])
        self.assertEqual(
            [item["profile_id"] for item in intake.mission["required_capabilities"]],
            ["P-2", "P-7"],
        )
        self.assertEqual(intake.package_digest, pcp.package_digest(package))

    def test_missing_external_problem_evidence_is_not_a_computable_package(self):
        package = deepcopy(pcp.materialize_casino_pcp())
        package["problem"]["evidence_refs"] = [{"ref": "vault://internal", "external": False}]

        with self.assertRaises(pcp.PCPRefusal) as raised:
            pcp.validate(package)
        self.assertEqual(raised.exception.code, "PCP_EXTERNAL_EVIDENCE_MISSING")

    def test_non_build_investment_decision_does_not_enter_factory_intake(self):
        package = deepcopy(pcp.materialize_casino_pcp())
        package["investment_decision"]["decision"] = "hold"

        with self.assertRaises(pcp.PCPRefusal) as raised:
            pcp.intake(package)
        self.assertEqual(raised.exception.code, "PCP_NOT_BUILDABLE")

    def test_unknown_fields_are_refused_instead_of_ignored(self):
        package = deepcopy(pcp.materialize_casino_pcp())
        package["temporary_operator_override"] = True

        with self.assertRaises(pcp.PCPRefusal) as raised:
            pcp.validate(package)
        self.assertEqual(raised.exception.code, "PCP_UNKNOWN_FIELDS")


if __name__ == "__main__":
    unittest.main()
