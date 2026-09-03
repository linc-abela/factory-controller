"""SF-179 B: one identity, from the seal to the byte before the network.

Every deployment-adapter test until now injected its own ``artifact_resolver``,
so nothing had ever run the real ``file_system_artifact_resolver`` against a
review root the Controller actually sealed.  Two things were wrong there and
neither could be seen from either side alone.

The Release Candidate was sealed on the *packaging container's* hash -- the
SHA-256 of the canonical tar the execution layer wrote -- while the resolver
and the deployment adapter derive the identity of the *file set*: each sorted
name in UTF-8, then that file's exact bytes.  The resolver therefore found the
review directory, disagreed with its own name for it, and returned nothing;
the adapter then refused ``EMPTY_OR_MISSING_ARTIFACT`` before any network call.
Production could never have been reached.

The review root also carried the publish prefix, so the review surface served
``/index.html`` from ``<root>/public`` while the resolver would have handed the
hosting target a file named ``public/index.html`` -- the same release serving
two different layouts.

What is checked here is the single property both defects broke: the bytes the
Owner reviews, the bytes the resolver returns, and the bytes the adapter is
about to upload are one file set with one identity.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from factory_controller import google_production, production

from tests.test_factory_product import CONTRACT, ProductReviewTests


class RecordingTransport:
    """A transport that proves whether the network was reached at all."""

    def __init__(self):
        self.deploys = []

    def deploy_release(self, config, artifact_digest, files, operation_key):
        self.deploys.append({"digest": artifact_digest, "files": dict(files)})
        return {"version_id": "v1", "release_id": "rel-1"}

    def rollback_release(self, config, target_version_id, operation_key):
        raise AssertionError("no rollback in this test")


class ReleaseArtifactIdentityTests(unittest.TestCase):
    setUp = ProductReviewTests.setUp
    ready = ProductReviewTests.ready
    submit = ProductReviewTests.submit
    review = ProductReviewTests.review
    finish_the_product_mission = ProductReviewTests.finish_the_product_mission
    CANDIDATE = ProductReviewTests.CANDIDATE

    def sealed(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        result = self.review()
        self.assertTrue(result.ok, result.render())
        return result.details["artifact_digest"], Path(result.details["review_root"])

    def resolve(self, digest, root):
        return google_production.file_system_artifact_resolver(
            digest, base_dirs=[root])

    def test_the_resolver_returns_exactly_the_bytes_that_were_sealed(self):
        digest, root = self.sealed()
        files = self.resolve(digest, root)

        self.assertTrue(files, "the real resolver found nothing for %s" % digest)
        self.assertEqual(production.deployable_digest(files), digest)
        for name, body in files.items():
            self.assertEqual(body, (root / name).read_bytes())
        self.assertEqual(sorted(files),
                         sorted(path.relative_to(root).as_posix()
                                for path in root.rglob("*") if path.is_file()))

    def test_the_review_root_holds_the_deployable_layout_not_the_prefix(self):
        """What the Owner opens at `/` is what the host serves at `/`."""

        _, root = self.sealed()
        self.assertTrue((root / "index.html").is_file())
        self.assertFalse((root / "public").exists())

    def test_the_adapter_re_derives_the_same_identity_and_reaches_the_network(self):
        digest, root = self.sealed()
        transport = RecordingTransport()
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=transport,
            artifact_resolver=lambda value: self.resolve(value, root))
        bundle = self.bundle_for(digest)

        outcome = adapter.deploy(bundle, self.environment(), "op-1")

        self.assertTrue(outcome.reached, outcome.detail)
        self.assertEqual(len(transport.deploys), 1)
        self.assertEqual(transport.deploys[0]["digest"], digest)
        self.assertEqual(transport.deploys[0]["files"], self.resolve(digest, root))

    def test_one_changed_byte_stops_the_deployment_before_the_network(self):
        """The check is the reason the identity is worth sealing."""

        digest, root = self.sealed()
        entry = root / "index.html"
        entry.write_bytes(entry.read_bytes() + b"<!-- tampered -->")
        transport = RecordingTransport()
        adapter = google_production.FirebaseHostingDeploymentAdapter(
            {}, transport=transport,
            artifact_resolver=lambda value: self.resolve(value, root))

        bundle = self.bundle_for(digest)
        outcome = adapter.deploy(bundle, self.environment(), "op-2")

        self.assertFalse(outcome.reached)
        self.assertIn("EMPTY_OR_MISSING_ARTIFACT", outcome.detail)
        self.assertEqual(transport.deploys, [])
        self.assertEqual(bundle.artifact["identity"], digest)

    # -- the two records the adapter needs, and nothing else -------------- #

    candidates = ProductReviewTests.candidates

    def bundle_for(self, digest):
        """The bundle the Owner's own review sealed, read back from the ledger."""

        row = self.candidates()[0]
        self.assertEqual(row["artifact_digest"], digest)
        return production.ReleaseBundle.from_payload(json.loads(row["bundle_json"]))

    def environment(self):
        return self.lifecycle.production.environment(
            CONTRACT.review_environment_id)
