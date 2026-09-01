"""The Owner product path: one package the Owner named, one mission.

Two things are being held to account here.  The first is that a product
mission is materialized by the *same* seam the frozen internal portfolio uses,
so the two cannot drift into two identity schemes.  The second is that the
Owner act is a record with a hash over it, not a flag somebody set.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from factory_controller import dogfood_intake, pcp, product


CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
PRODUCT_CONTRACT = CONTRACTS / "lodus-casino-product-run-contract.json"

REGISTRY = [{
    "project_id": "lodus-casino",
    "repository_remote_url": "https://github.com/linc-abela/lodus-casino.git",
    "checkout": "/Users/Shared/Projects/software-factory/lodus-casino",
    "capabilities": ["development"],
    "resolution": "resolved",
}]


def contract() -> product.ProductContract:
    return product.ProductContract.load(PRODUCT_CONTRACT)


def accepted() -> pcp.PCPIntake:
    return pcp.intake(pcp.materialize_casino_pcp())


class ProductContractTests(unittest.TestCase):
    def test_the_shipped_contract_loads(self):
        value = contract()
        self.assertEqual(value.package_id, "lodus-casino")
        self.assertEqual(value.project_id, "lodus-casino")
        self.assertEqual(value.projects, ("lodus-casino",))
        self.assertTrue(value.mutates_repository)
        self.assertEqual(value.publish_prefix, "public/")

    def test_the_shipped_contract_gates_are_derivable_against_its_own_source(self):
        """A gate whose command cannot be derived is a gate recorded not_run."""

        value = contract()
        commands = dogfood_intake.gate_commands(
            value.acceptance_gate_ids, value.acceptance_gate_source, "/tmp/checkout")
        self.assertEqual(sorted(commands), ["dev-check", "dev-evaluate", "dev-test"])
        self.assertEqual(commands["dev-evaluate"], ["/tmp/checkout/dev", "evaluate"])

    def test_the_shipped_contract_baseline_is_the_one_its_gates_name(self):
        """The gate source pins a commit; a second baseline would be a fork."""

        value = contract()
        self.assertIn("@" + value.baseline_sha + ":", value.acceptance_gate_source)

    def test_a_contract_of_another_schema_is_refused(self):
        body = json.loads(PRODUCT_CONTRACT.read_text())
        body["schema_version"] = "something.else.v1"
        with self.assertRaises(product.ProductRefusal) as caught:
            product.ProductContract.from_payload(body)
        self.assertEqual(caught.exception.code, "PRODUCT_CONTRACT_INVALID")

    def test_a_contract_missing_a_field_is_refused_by_name(self):
        for field in ("run_ref", "project_id", "baseline_sha", "publish_prefix",
                      "review_environment_id", "production_environment_id"):
            body = json.loads(PRODUCT_CONTRACT.read_text())
            body.pop(field)
            with self.assertRaises(product.ProductRefusal, msg=field) as caught:
                product.ProductContract.from_payload(body)
            self.assertEqual(caught.exception.code, "PRODUCT_CONTRACT_INVALID")
            self.assertIn(field, caught.exception.detail)

    def test_an_unreadable_contract_is_refused_rather_than_defaulted(self):
        with self.assertRaises(product.ProductRefusal) as caught:
            product.ProductContract.load(CONTRACTS / "no-such-contract.json")
        self.assertEqual(caught.exception.code, "PRODUCT_CONTRACT_UNAVAILABLE")


class ProductMissionTests(unittest.TestCase):
    def test_the_mission_reference_is_the_package_work_item(self):
        """Not renamed here: a second name for a work item is a second item."""

        mission = product.mission_for(contract(), accepted())
        self.assertEqual(mission.mission_ref, "lodus-casino:build")
        self.assertEqual(mission.project_id, "lodus-casino")
        self.assertTrue(mission.mutates_repository)

    def test_every_mission_field_comes_from_the_package_or_the_contract(self):
        value, intake = contract(), accepted()
        mission = product.mission_for(value, intake)
        self.assertEqual(mission.objective, intake.mission["objective"])
        self.assertEqual(mission.baseline_sha, value.baseline_sha)
        self.assertEqual(mission.acceptance_gate_ids, value.acceptance_gate_ids)
        self.assertEqual(mission.work_class, value.work_class)
        self.assertEqual(mission.environment_class, value.environment_class)

    def test_a_package_this_factory_is_not_configured_for_is_refused(self):
        package = pcp.materialize_casino_pcp()
        package["package_id"] = "some-other-product"
        with self.assertRaises(product.ProductRefusal) as caught:
            product.mission_for(contract(), pcp.intake(package))
        self.assertEqual(caught.exception.code, "PRODUCT_PACKAGE_MISMATCH")

    def test_a_package_with_an_open_decision_is_carried_and_named(self):
        """Accepted-degraded is the package module's verdict, not ours to flip."""

        package = pcp.materialize_casino_pcp()
        package["decision_ledger"][0]["status"] = "open"
        package["decision_ledger"][0].pop("resolution", None)
        package["decision_ledger"][0]["owner_role"] = "Owner / CEO"
        package["decision_ledger"][0]["deadline"] = "2026-12-31"
        intake = pcp.intake(package)
        self.assertEqual(intake.verdict, "ACCEPTED_DEGRADED")
        product.mission_for(contract(), intake)
        self.assertEqual(product.unresolved(intake), ("CASINO-RULES-001",))

    def test_a_resolved_package_leaves_nothing_open(self):
        self.assertEqual(product.unresolved(accepted()), ())


class MissionBriefTests(unittest.TestCase):
    def test_the_brief_names_the_package_and_the_deciding_gate(self):
        text = product.brief(contract(), accepted())
        self.assertIn("lodus-casino@v1", text)
        self.assertIn("./dev evaluate", text)
        self.assertIn("MISSION.md", text)

    def test_the_brief_fits_the_bound_the_evidence_layer_enforces(self):
        self.assertLessEqual(len(product.brief(contract(), accepted())),
                             product.BRIEF_LIMIT)

    def test_the_brief_restates_no_requirement_of_its_own(self):
        """The repository's MISSION.md is the instruction; this is a pointer."""

        text = product.brief(contract(), accepted()).lower()
        for smuggled in ("shoe", "higher", "lower", "browser", "odds"):
            self.assertNotIn(smuggled, text)


class OwnerActTests(unittest.TestCase):
    def test_the_act_names_the_package_bytes_it_submitted(self):
        intake = accepted()
        act = product.owner_act(contract(), intake, owner="an-owner",
                                approval_ref="factory-owner-1-x",
                                at="2026-09-01T00:00:00Z")
        self.assertEqual(act["package_digest"], intake.package_digest)
        self.assertEqual(act["chosen_action"], "submit")
        self.assertEqual(act["evidence_class"], "human_authority")

    def test_the_act_hash_covers_the_act(self):
        base = dict(contract=contract(), intake=accepted())
        act = product.owner_act(base["contract"], base["intake"], owner="a",
                                approval_ref="r", at="2026-09-01T00:00:00Z")
        tampered = {**act, "owner": "somebody-else"}
        recomputed = product.owner_act(
            base["contract"], base["intake"], owner="somebody-else",
            approval_ref="r", at="2026-09-01T00:00:00Z")
        self.assertNotEqual(tampered["act_hash"], recomputed["act_hash"])

    def test_the_same_submission_produces_the_same_act(self):
        made = [product.owner_act(contract(), accepted(), owner="a",
                                  approval_ref="r", at="2026-09-01T00:00:00Z")
                for _ in range(2)]
        self.assertEqual(made[0], made[1])

    def test_an_act_without_an_owner_is_refused(self):
        for field in ({"owner": ""}, {"approval_ref": " "}, {"at": ""}):
            arguments = {"owner": "a", "approval_ref": "r",
                         "at": "2026-09-01T00:00:00Z", **field}
            with self.assertRaises(product.ProductRefusal) as caught:
                product.owner_act(contract(), accepted(), **arguments)
            self.assertEqual(caught.exception.code, "PRODUCT_OWNER_ACT_INVALID")


class SharedMaterializationTests(unittest.TestCase):
    """The product path and the internal portfolio share one intake seam."""

    def build(self, **overrides):
        value, intake = contract(), accepted()
        mission = product.mission_for(value, intake)
        arguments = dict(
            portfolio_ref=value.run_ref, run_ref=value.run_ref,
            registry=REGISTRY, registry_digest="d" * 16,
            provider_profiles=list(value.provider_profiles),
            corpus_identity="package://%s@%s" % (intake.mission["source_pcp"],
                                                 intake.package_digest),
            owner="an-owner", approval_ref="factory-owner-1-x",
            granted_at=0.0, expires_at=3600.0, now=1.0,
            stage1={"command": ["python3"], "workdir": "/tmp"},
        )
        arguments.update(overrides)
        return dogfood_intake.build(mission, **arguments)

    def test_a_product_mission_materializes_through_the_shared_seam(self):
        built = self.build()
        self.assertEqual(built.mission_ref, "lodus-casino:build")
        self.assertEqual(built.capability, "development")
        self.assertEqual(built.baseline_sha, contract().baseline_sha)
        self.assertTrue(built.idempotency_key.startswith("lodus-casino:build:"))

    def test_the_gates_run_against_the_candidate_not_the_baseline_checkout(self):
        payload = self.build().payload
        self.assertTrue(payload["stage1"]["mutates_repository"])
        self.assertEqual(payload["stage1"]["gate_commands"]["dev-evaluate"],
                         [REGISTRY[0]["checkout"] + "/dev", "evaluate"])

    def test_the_corpus_identity_is_the_package_digest(self):
        intake = accepted()
        manifest = self.build().admission["admission_evidence"]["context_manifest"]
        self.assertEqual(manifest["corpus_identity"],
                         "package://lodus-casino@v1@" + intake.package_digest)

    def test_the_same_package_submitted_twice_has_one_identity(self):
        self.assertEqual(self.build().idempotency_key,
                         self.build().idempotency_key)

    def test_a_different_package_is_a_different_mission_identity(self):
        other = self.build(corpus_identity="package://lodus-casino@v1@" + "0" * 64)
        self.assertNotEqual(self.build().idempotency_key, other.idempotency_key)

    def test_an_unregistered_project_is_refused_before_a_provider_is_named(self):
        with self.assertRaises(dogfood_intake.IntakeError) as caught:
            self.build(registry=[])
        self.assertEqual(caught.exception.code, "PROJECT_NOT_REGISTERED")

    def test_a_project_registered_for_no_admissible_capability_is_refused(self):
        row = {**REGISTRY[0], "capabilities": ["something-else"]}
        with self.assertRaises(dogfood_intake.IntakeError) as caught:
            self.build(registry=[row])
        self.assertEqual(caught.exception.code, "CAPABILITY_NOT_ADMISSIBLE")

    def test_a_gate_source_naming_another_repository_is_refused(self):
        value, intake = contract(), accepted()
        mission = product.mission_for(value, intake)
        forked = product.ProductMission(
            **{**mission.__dict__,
               "acceptance_gate_source":
                   "https://github.com/linc-abela/factory-bug-lab.git@%s:dev"
                   % value.baseline_sha})
        with self.assertRaises(dogfood_intake.IntakeError) as caught:
            dogfood_intake.build(
                forked, portfolio_ref=value.run_ref, run_ref=value.run_ref,
                registry=REGISTRY, registry_digest="d" * 16,
                provider_profiles=list(value.provider_profiles),
                corpus_identity="package://x@y", owner="o", approval_ref="r",
                granted_at=0.0, expires_at=1.0, now=1.0, stage1={})
        self.assertEqual(caught.exception.code, "ACCEPTANCE_GATE_SOURCE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
