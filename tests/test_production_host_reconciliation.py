"""Final Stage-6 reconciliation against the landed production host runtime.

Read against `factory-bridge` `6b5d131b8c14b5c7a690c8a4d1a01978f6521568`
(`src/factory_bridge/production.py`), which is three commits past the head
SF-138 reconciled with.  Its rules are *reproduced* here rather than imported:
the two repositories have no dependency edge in either direction, and that is
the point of the boundary.  Reproducing them means a change on either side
shows up as a failing test here instead of as a refusal in front of an
operator.

Two halves of the seam, and SF-138 only closed one of them.

`ReleaseBundle.compat_view` was written to emit the host's bundle shape, and
the host has since tightened that shape considerably -- exact key sets on
`rollback` and `provenance`, a fixed `schema_version`, a fixed rollback
strategy, a 40-hex-only candidate.  The first half of this file is that shape,
checked.

The second half is the drift SF-138 left open.  The host requires a separate
*production authority* object for a gated class and its own docstring names
what it expects: *"Its deployment receipt is the separate, durable control-plane
fact this host consumes."*  Nothing on the Controller side ever checked that
`ProductionLedger.receipt` satisfies those rules, and the moment it is checked
the interesting part is *when*: the host demands `state == "deploying"`, which
is true only while the port call is in flight.  The receipt is therefore not a
document to be filed after a deployment; it is the authority *during* one, and
the test below takes it from inside a real `DeploymentPort.deploy`.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from factory_controller import production
from factory_controller.store import MissionStore


#: Verified at 6b5d131: the host's own constants, reproduced.
HOST_COMPAT_SCHEMA = "controller-release-bundle-compat-v1"
HOST_CONTROLLER_CONTRACT = "factory-controller/production/1.0"
HOST_ROLLBACK_STRATEGY = "previous-recorded-healthy"
HOST_BUNDLE_KEYS = {"schema_version", "release_id", "project_id", "service_id",
                    "candidate_sha", "release_policy_version", "evidence_refs",
                    "environment_schema", "rollback", "provenance"}
HOST_ENV_SPEC_KEYS = {"type", "required", "description"}
HOST_ENV_TYPES = {"string", "integer", "boolean"}
HOST_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HOST_SHA = re.compile(r"^[0-9a-f]{40}$")
HOST_DIGEST = re.compile(r"^[0-9a-f]{64}$")
HOST_AUTHORITY_STRINGS = ("deployment_id", "bundle_digest", "approved_by",
                          "approval_ref")

#: `_SECRET_KEYS` at 6b5d131, reproduced.  The host rejects any nested key
#: matching it, in the bundle and in the authority object alike.
HOST_SECRET_KEYS = re.compile(
    r"(^|_)(secret|password|token|api_key|private_key|credential_value)($|_)",
    re.IGNORECASE)

#: The four canonical absence words.  The host refuses an authority field whose
#: value is one of them, which is only true if it holds the same four.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

SHA = "a" * 40


def reject_secret_values(value, path="bundle"):
    """`_reject_secret_values` at 6b5d131, reproduced."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and HOST_SECRET_KEYS.search(key):
                raise AssertionError("%s.%s is a secret-shaped key" % (path, key))
            reject_secret_values(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_values(item, "%s[%d]" % (path, index))


def host_reads_bundle(value):
    """`ReleaseBundle.from_compat` at 6b5d131, reproduced as assertions."""

    assert isinstance(value, dict), "release bundle must be an object"
    reject_secret_values(value)
    unexpected = set(value) - HOST_BUNDLE_KEYS
    assert not unexpected, "unknown compatibility fields: %s" % sorted(unexpected)
    for name in ("schema_version", "release_id", "project_id", "service_id",
                 "candidate_sha", "release_policy_version"):
        item = value.get(name)
        assert isinstance(item, str) and item and len(item) <= 512, \
            "%s is required" % name
    assert value["schema_version"] == HOST_COMPAT_SCHEMA, "unsupported schema"
    assert HOST_SHA.fullmatch(value["candidate_sha"].lower()), \
        "candidate_sha must be a 40-hex commit identity"
    refs = value.get("evidence_refs")
    assert isinstance(refs, list) and refs and len(refs) <= 64, "evidence_refs"
    for ref in refs:
        assert isinstance(ref, str) and ref and len(ref) <= 512, "evidence_refs"
    for name in ("environment_schema", "rollback", "provenance"):
        assert isinstance(value.get(name), dict), "%s must be an object" % name
    for name, spec in value["environment_schema"].items():
        assert isinstance(name, str) and HOST_ENV_NAME.fullmatch(name), name
        assert isinstance(spec, dict) and set(spec) == HOST_ENV_SPEC_KEYS, name
        assert spec["type"] in HOST_ENV_TYPES, name
        assert isinstance(spec["required"], bool), name
        assert isinstance(spec["description"], str) and len(spec["description"]) <= 512
    rollback = value["rollback"]
    assert set(rollback) == {"strategy", "reverse_ref"}, "rollback shape"
    assert rollback["strategy"] == HOST_ROLLBACK_STRATEGY, "rollback strategy"
    assert isinstance(rollback["reverse_ref"], str) and rollback["reverse_ref"]
    provenance = value["provenance"]
    assert set(provenance) == {"built_by", "built_at", "contract_version"}
    for name in provenance:
        assert isinstance(provenance[name], str) and provenance[name]
    return True


def host_reads_authority(authority, *, environment_class, project_id,
                         environment_id, release_sha, operation_id):
    """`_authority_reference` at 6b5d131, reproduced as assertions."""

    if environment_class != "production":
        assert authority is None, \
            "local-sim and staging do not consume a production approval"
        return True
    assert isinstance(authority, dict), "Controller deployment receipt is required"
    reject_secret_values(authority, "authority")
    required = {
        "contract_version": HOST_CONTROLLER_CONTRACT,
        "project_id": project_id,
        "environment_id": environment_id,
        "environment_class": "production",
        "release_sha": release_sha,
        "operation_key": operation_id,
        "state": "deploying",
    }
    for key, expected in required.items():
        assert authority.get(key) == expected, \
            "authority %s does not match custody: %r != %r" % (
                key, authority.get(key), expected)
    for key in HOST_AUTHORITY_STRINGS:
        item = authority.get(key)
        assert isinstance(item, str) and item and len(item) <= 512, \
            "authority %s is absent" % key
        assert item not in CANONICAL_ABSENCE, \
            "authority %s is a stated absence, not a value" % key
    assert HOST_DIGEST.fullmatch(authority["bundle_digest"]), \
        "authority bundle_digest is malformed"
    return True


def bundle_payload(**overrides):
    payload = {
        "bundle_ref": "rc-139",
        "project_id": "shop",
        "repository": "https://example.invalid/shop.git",
        "release_sha": SHA,
        "mission_ref": "SF-139",
        "evidence_refs": ["evidence/shop/SF-139.json"],
        "evaluator_receipts": ["receipts/evaluate.json"],
        "artifact": {"kind": "image", "identity": "sha256:" + "c" * 64},
        "env_schema": {"PORT": {"type": "integer", "required": True,
                                "description": "service port"},
                       "LOG_LEVEL": {"type": "string", "required": False,
                                     "description": "structured logging level"}},
        "migration": {"forward_ref": "migrations/001.sql",
                      "reverse_ref": "migrations/001.down.sql"},
        "release_policy_version": "1.0",
        "provenance": {"built_by": "factory-controller",
                       "built_at": "2026-08-27T00:00:00Z",
                       "contract_version": production.CONTRACT_VERSION},
    }
    payload.update(overrides)
    return payload


def bundle(**overrides):
    return production.ReleaseBundle.from_payload(bundle_payload(**overrides))


def environment(**overrides):
    values = {"environment_id": "shop-prod", "project_id": "shop",
              "environment_class": "production",
              "repository": "https://example.invalid/shop.git",
              "service_ref": "shop-web", "approver_refs": ("owner", "deputy")}
    values.update(overrides)
    return production.EnvironmentPolicy(**values)


def staging(**overrides):
    values = {"environment_id": "shop-staging", "environment_class": "staging",
              "autonomous": True, "approver_refs": ("owner",)}
    values.update(overrides)
    return environment(**values)


# --------------------------------------------------------------------------- #
# the bundle projection
# --------------------------------------------------------------------------- #

class CompatBundleTests(unittest.TestCase):

    def test_the_host_reads_the_projection_this_controller_emits(self):
        self.assertTrue(host_reads_bundle(bundle().compat_view(environment())))

    def test_the_service_binding_comes_from_the_environment_not_the_bundle(self):
        view = bundle().compat_view(environment(service_ref="shop-api"))
        self.assertEqual(view["service_id"], "shop-api")

    def test_both_evidence_lists_join_because_the_host_view_has_one(self):
        view = bundle().compat_view(environment())
        self.assertEqual(view["evidence_refs"],
                         ["evidence/shop/SF-139.json", "receipts/evaluate.json"])

    def test_the_rollback_strategy_is_the_one_the_host_pins(self):
        self.assertEqual(production.ROLLBACK_STRATEGY, HOST_ROLLBACK_STRATEGY)

    def test_a_migration_absence_still_satisfies_the_hosts_reverse_ref(self):
        """A release with no migration is still deployable by the host."""
        view = bundle(migration={"forward_ref": "not_applicable",
                                 "reverse_ref": "not_applicable"}
                      ).compat_view(environment())
        self.assertTrue(host_reads_bundle(view))

    def test_the_projection_carries_no_key_the_host_would_call_unknown(self):
        self.assertEqual(set(bundle().compat_view(environment())), HOST_BUNDLE_KEYS)

    def test_the_reproduced_host_reader_would_actually_catch_a_drift(self):
        """The check is proven able to fire, not only able to pass."""
        view = bundle().compat_view(environment())
        view["rollback"]["strategy"] = "previous-admitted-release"
        with self.assertRaises(AssertionError):
            host_reads_bundle(view)


# --------------------------------------------------------------------------- #
# the production authority object
# --------------------------------------------------------------------------- #

class AuthorityPort:
    """A port that reads the Controller's authority at the only moment it holds.

    The host requires `state == "deploying"`, which is true from the instant
    `_claim_operation` commits until the outcome is settled.  Taking the
    receipt from inside `deploy` is therefore not a test convenience; it is
    where the real adapter has to take it.
    """

    name = "authority-probe"

    def __init__(self, ledger, deployment_id):
        self._ledger = ledger
        self._deployment_id = deployment_id
        self.authority = None

    def deploy(self, bundle_, environment_, operation_key):
        self.authority = self._ledger.receipt(self._deployment_id)
        return production.DeploymentOutcome(
            reached=True, adapter=self.name,
            operation_ref="local://%s" % operation_key, detail={})

    def rollback(self, bundle_, environment_, operation_key):
        return self.deploy(bundle_, environment_, operation_key)


class ProductionAuthorityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MissionStore(str(Path(self.tmp.name) / "controller.db"))
        self.ledger = production.ProductionLedger(self.store)

    def gated_deployment(self):
        policy = environment()
        self.ledger.register_environment(policy)
        deployment = self.ledger.admit_release(bundle(), policy.environment_id,
                                               "factory")
        self.ledger.approve(deployment, "owner", "signoff/139",
                            bundle().bundle_digest)
        port = AuthorityPort(self.ledger, deployment)
        self.ledger.deploy(deployment, port)
        return policy, deployment, port.authority

    def test_the_receipt_is_the_authority_the_host_requires(self):
        policy, deployment, authority = self.gated_deployment()
        self.assertTrue(host_reads_authority(
            authority, environment_class="production", project_id=policy.project_id,
            environment_id=policy.environment_id, release_sha=SHA,
            operation_id=production.operation_key(deployment, "deploy", 0)))

    def test_the_authority_names_the_person_who_approved_and_the_signoff(self):
        _, _, authority = self.gated_deployment()
        self.assertEqual(authority["approved_by"], "owner")
        self.assertEqual(authority["approval_ref"], "signoff/139")

    def test_an_unapproved_release_never_reaches_the_authority_shape(self):
        """The refusal happens before the port, so no authority is ever built."""
        policy = environment()
        self.ledger.register_environment(policy)
        deployment = self.ledger.admit_release(bundle(), policy.environment_id,
                                               "factory")
        port = AuthorityPort(self.ledger, deployment)
        with self.assertRaises(production.ProductionRefusal) as raised:
            self.ledger.deploy(deployment, port)
        self.assertEqual(raised.exception.code, "PRODUCTION_APPROVAL_REQUIRED")
        self.assertIsNone(port.authority)

    def test_an_ungated_environment_passes_no_authority_at_all(self):
        policy = staging()
        self.ledger.register_environment(policy)
        deployment = self.ledger.admit_release(bundle(), policy.environment_id,
                                               "factory")
        self.ledger.deploy(deployment, AuthorityPort(self.ledger, deployment))
        self.assertTrue(host_reads_authority(
            None, environment_class="staging", project_id=policy.project_id,
            environment_id=policy.environment_id, release_sha=SHA,
            operation_id=production.operation_key(deployment, "deploy", 0)))

    def test_the_authority_carries_no_secret_shaped_key(self):
        _, _, authority = self.gated_deployment()
        reject_secret_values(authority, "authority")

    def test_the_reproduced_authority_reader_would_actually_catch_a_drift(self):
        policy, deployment, authority = self.gated_deployment()
        authority["state"] = "verifying"
        with self.assertRaises(AssertionError):
            host_reads_authority(
                authority, environment_class="production",
                project_id=policy.project_id,
                environment_id=policy.environment_id, release_sha=SHA,
                operation_id=production.operation_key(deployment, "deploy", 0))


class AbsenceVocabularyTests(unittest.TestCase):
    """The sixth fork of the four words, and the first inside a safety check.

    The host refuses an authority field whose value is a stated absence.  That
    refusal is only as good as the set it checks against, and at 6b5d131 the
    host's set held `not_measured` where every other layer holds
    `not_measurable` -- so a receipt field spelled with the canonical word
    would have been accepted by the host as a real approval value.  It is not a
    difference of dialect; it is a one-word typo inside the check that stops a
    stated absence from standing in for a person's signature.
    """

    def test_the_controller_holds_exactly_the_four_canonical_words(self):
        self.assertEqual(production.CANONICAL_ABSENCE, CANONICAL_ABSENCE)

    def test_the_receipts_absences_are_all_canonical(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MissionStore(str(Path(tmp.name) / "controller.db"))
        ledger = production.ProductionLedger(store)
        policy = environment()
        ledger.register_environment(policy)
        deployment = ledger.admit_release(bundle(), policy.environment_id, "factory")
        receipt = ledger.receipt(deployment)
        spelled = {value for value in receipt.values()
                   if isinstance(value, str)
                   and value.startswith(("not_", "unknown"))}
        self.assertTrue(spelled <= CANONICAL_ABSENCE, spelled)
        self.assertIn("not_run", spelled)


if __name__ == "__main__":
    unittest.main()
