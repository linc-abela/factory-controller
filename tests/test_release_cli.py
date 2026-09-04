"""The release lifecycle, reached the way an Owner reaches it.

``release.py`` was complete and correct and no command invoked it, which is a
different failure from not having it: a lifecycle nothing can call cannot be
the thing an Owner Validation actually runs through.  These tests exercise the
whole exact-artifact chain across the process boundary the Owner types at, and
they assert the refusals as hard as the successes -- a promotion that survives
a rebuilt artifact would be the one bug this chain exists to prevent.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import io
import json
import tempfile
import threading
from pathlib import Path
import unittest

from factory_controller import cli, production
from factory_controller import factory as factory_lifecycle
from factory_controller.store import MissionStore

from tests.test_release_lifecycle import REPOSITORY, bundle


#: The document a static surface serves at ``/health.json``. The Casino
#: artifact's own file, so the probe's semantics are tested against the shape
#: the product actually ships rather than an invented one.
HEALTH_OK = b'{"app": "lodus-casino", "status": "ok"}'


def serve(directory: Path) -> tuple[str, http.server.ThreadingHTTPServer]:
    """A real loopback surface serving exactly `directory`.

    A REVIEW is now settled only by a probe of the declared surface, so these
    tests serve one. That is not ceremony: the command being exercised is the
    one an Owner types, and before this the whole chain could be driven with
    counts and no server at all -- which is the defect, not the setup cost.
    """

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_address[1], server


class ReleaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = str(self.root / "controller.db")
        ledger = production.ProductionLedger(MissionStore(self.db))
        ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-review", project_id="lodus-casino",
            environment_class="staging", repository=REPOSITORY,
            service_ref="lodus-casino-review-web", approver_refs=("owner",),
            autonomous=True, policy_version="phase-1"))
        ledger.register_environment(production.EnvironmentPolicy(
            environment_id="lodus-casino-production", project_id="lodus-casino",
            environment_class="production", repository=REPOSITORY,
            service_ref="lodus-casino-production-web", approver_refs=("owner",),
            policy_version="phase-1"))

    def run_cli(self, *arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["--db", self.db, *arguments])
        text = stream.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def artifact_dir(self, number: int, *, artifact_char: str | None = None) -> Path:
        """One release's real bytes on disk, named by their real identity.

        The bundle carries the digest of these files rather than a made-up
        one, so the resolver, the probe's entry comparison and the deployment
        adapter are all looking at the same file set -- which is what the
        exact-artifact chain claims and what a synthetic identity cannot test.
        """

        marker = artifact_char or "abcdef"[(number - 1) % 6]
        directory = self.root / ("artifact-%03d-%s" % (number, marker))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_bytes(
            b"<!doctype html><title>casino %s</title>" % marker.encode())
        (directory / "health.json").write_bytes(HEALTH_OK)
        return directory

    def files(self, number: int, *, artifact_char: str | None = None):
        directory = self.artifact_dir(number, artifact_char=artifact_char)
        return directory, {path.name: path.read_bytes()
                           for path in sorted(directory.iterdir())}

    def bundle_path(self, number: int, *, artifact_char: str | None = None) -> str:
        path = self.root / ("bundle-%03d.json" % number)
        _, files = self.files(number, artifact_char=artifact_char)
        body = bundle(number, artifact_char=artifact_char).as_row()
        body["artifact"] = {"kind": "static-bundle",
                            "identity": production.deployable_digest(files)}
        path.write_text(json.dumps(body))
        return str(path)

    def seal(self, number: int = 1, *, artifact_char: str | None = None):
        return self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--bundle", self.bundle_path(number, artifact_char=artifact_char),
            "--verification-ref", "verification/casino/%03d" % number,
            "--qa-ref", "qa/casino/%03d" % number)

    def surface(self, number: int = 1, *, artifact_char: str | None = None) -> str:
        directory = self.artifact_dir(number, artifact_char=artifact_char)
        url, server = serve(directory)
        self.addCleanup(server.shutdown)
        return url

    def review(self, number: int = 1, *, artifact_char: str | None = None,
               url: str | None = None):
        directory = self.artifact_dir(number, artifact_char=artifact_char)
        return self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url",
            url or self.surface(number, artifact_char=artifact_char),
            "--probe", "--artifact-dir", str(directory))

    def validate(self, deployment_ref: str, *, decision="VALIDATED", number=1):
        return self.run_cli(
            "release", "validate", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--validation", "CASINO-MVP-VALIDATION-%03d" % number,
            "--deployment-ref", deployment_ref, "--decision", decision,
            "--actor", "owner")

    # -- the chain ------------------------------------------------------- #

    def promote(self, number: int = 1):
        directory = self.artifact_dir(number)
        return self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-%03d" % number,
            "--validation", "CASINO-MVP-VALIDATION-%03d" % number,
            "--environment", "lodus-casino-production", "--actor", "factory",
            "--review-url", self.surface(number), "--probe",
            "--artifact-dir", str(directory))

    def release_through(self, number: int):
        """Seal, review, validate and promote one release from the command line."""

        code, sealed = self.seal(number)
        self.assertEqual(code, 0)
        code, deployed = self.review(number)
        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "healthy")
        self.assertEqual(deployed["artifact_digest"], sealed["artifact_digest"])
        code, validated = self.validate(deployed["deployment_ref"], number=number)
        self.assertEqual(code, 0)
        self.assertEqual(validated["decision"], "VALIDATED")
        self.assertEqual(validated["artifact_digest"], sealed["artifact_digest"])
        code, promoted = self.promote(number)
        self.assertEqual(code, 0)
        return sealed, deployed, promoted

    def test_the_whole_exact_artifact_chain_runs_from_the_command_line(self):
        sealed, _, promoted = self.release_through(1)
        self.assertTrue(sealed["artifact_digest"].startswith("sha256:"))
        self.assertEqual(promoted["artifact_digest"], sealed["artifact_digest"])
        self.assertEqual(promoted["candidate_sha"], sealed["candidate_sha"])

        code, events = self.run_cli("release", "events", "--rc", "CASINO-MVP-RC-001")
        self.assertEqual(code, 0)
        self.assertEqual([row["kind"] for row in events][:2],
                         ["rc_sealed", "review_deployed"])

    def test_a_rollback_restores_the_previous_release_and_refuses_without_one(self):
        """The first production release has nothing behind it, and says so."""

        _, _, first = self.release_through(1)
        code, refused = self.run_cli(
            "release", "rollback", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-production")
        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "ROLLBACK_TARGET_UNKNOWN")

        self.release_through(2)
        code, rolled = self.run_cli(
            "release", "rollback", "--rc", "CASINO-MVP-RC-002",
            "--environment", "lodus-casino-production")
        self.assertEqual(code, 0)
        self.assertEqual(rolled["state"], "recovered")
        self.assertEqual(rolled["receipt"]["rollback_of"], first["deployment_id"])
        self.assertIn("production_rollback",
                      [row["kind"] for row in self.run_cli(
                          "release", "events", "--rc", "CASINO-MVP-RC-002")[1]])

    # -- the refusals ---------------------------------------------------- #

    def test_a_bundle_without_an_immutable_artifact_cannot_be_sealed(self):
        body = bundle(1).as_row()
        body["artifact"] = {"kind": "static-bundle", "identity": "latest"}
        path = self.root / "mutable.json"
        path.write_text(json.dumps(body))
        code, result = self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-009", "--bundle", str(path),
            "--verification-ref", "v", "--qa-ref", "q")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_BUNDLE_INVALID")
        self.assertIn("immutable", result["refused"]["detail"])

    def test_sealing_without_independent_qa_evidence_is_refused(self):
        code, result = self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-010",
            "--bundle", self.bundle_path(1), "--verification-ref", "v")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_EVIDENCE_MISSING")

    def test_a_review_surface_that_is_not_reachable_is_refused(self):
        self.seal()
        code, result = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "file:///tmp/casino")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "REVIEW_SURFACE_INVALID")

    def test_production_cannot_stand_in_for_the_owner_review_surface(self):
        self.seal()
        code, result = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-production", "--actor", "factory",
            "--review-url", "https://review.example.invalid/casino")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "REVIEW_ENVIRONMENT_REQUIRED")

    def test_promotion_without_an_owner_validation_is_refused(self):
        self.seal()
        self.review()
        code, result = self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-001",
            "--validation", "CASINO-MVP-VALIDATION-404",
            "--environment", "lodus-casino-production", "--actor", "factory")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "OWNER_VALIDATION_NOT_FOUND")

    def test_return_for_changes_stops_the_promotion(self):
        self.seal()
        _, deployed = self.review()
        self.validate(deployed["deployment_ref"], decision="RETURN_FOR_CHANGES")
        code, result = self.run_cli(
            "release", "promote", "--rc", "CASINO-MVP-RC-001",
            "--validation", "CASINO-MVP-VALIDATION-001",
            "--environment", "lodus-casino-production", "--actor", "factory")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "OWNER_VALIDATION_REQUIRED")

    def test_an_unknown_release_candidate_refuses_rather_than_traces(self):
        code, result = self.run_cli("release", "show", "--rc", "CASINO-MVP-RC-404")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RC_NOT_FOUND")

    def test_a_missing_argument_refuses_rather_than_traces(self):
        code, result = self.run_cli("release", "seal", "--rc", "CASINO-MVP-RC-011")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_ARGUMENTS_INVALID")

    def test_the_live_adapter_takes_the_owners_token_from_the_file_they_named(self):
        """The one thing that makes the live path reachable at all.

        Without this the live transport is constructed with nothing, so the
        Owner-facing command is *incapable* of contacting the host however
        complete its setup is.  The token is read for this command only: it
        reaches the transport as an argument and nothing writes it anywhere.
        The deploy still refuses before any network call, because no artifact
        bytes were named -- which is the fail-closed order this asserts too.
        """

        from unittest import mock
        from factory_controller import google_production

        seen = {}

        class CapturingTransport(google_production.FirebaseHostingRestTransport):
            def __init__(self, **keywords):
                seen.update(keywords)
                super().__init__(**keywords)

        token_file = self.root / "deploy-token"
        token_file.write_text("owner-issued-value\n")
        self.seal(1)
        with mock.patch.object(google_production, "FirebaseHostingRestTransport",
                               CapturingTransport):
            code, deployed = self.run_cli(
                "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
                "--environment", "lodus-casino-review", "--actor", "factory",
                "--review-url", "https://lodus-casino-review.web.app",
                "--adapter", "google", "--deploy-token-file", str(token_file))

        self.assertEqual(seen.get("token"), "owner-issued-value")
        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "failed")
        self.assertTrue(deployed["receipt"]["operation_ref"].endswith(":rejected"))
        receipt_text = json.dumps(deployed, sort_keys=True)
        self.assertNotIn("owner-issued-value", receipt_text)

    def test_a_deploy_token_file_that_is_not_there_refuses_rather_than_traces(self):
        """The operator's own input, refused by name before anything is tried.

        This read sits outside the refusal handler the rest of the command
        runs under, so an absent file raised ``FileNotFoundError`` straight
        through the one command the Phase-1 release path is driven by: the
        Owner got a traceback instead of a code, and the ledger got no record
        of why nothing happened.  The transport must not even be constructed.
        """

        from unittest import mock
        from factory_controller import google_production

        built = []

        class CountingTransport(google_production.FirebaseHostingRestTransport):
            def __init__(self, **keywords):
                built.append(keywords)
                super().__init__(**keywords)

        self.seal(1)
        with mock.patch.object(google_production, "FirebaseHostingRestTransport",
                               CountingTransport):
            code, result = self.run_cli(
                "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
                "--environment", "lodus-casino-review", "--actor", "factory",
                "--review-url", "https://lodus-casino-review.web.app",
                "--adapter", "google",
                "--deploy-token-file", str(self.root / "absent-token"))

        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "DEPLOY_TOKEN_UNAVAILABLE")
        self.assertEqual(built, [])
        code, events = self.run_cli("release", "events", "--rc",
                                    "CASINO-MVP-RC-001")
        self.assertEqual(code, 0)
        self.assertEqual([event for event in events
                          if event.get("kind") == "review_deployed"], [])

    def test_an_empty_deploy_token_file_is_refused_as_no_token_at_all(self):
        """A file that exists and holds nothing is not a credential.

        Stripped to the empty string it is falsy, so the transport would have
        been built with ``token=""`` and the refusal would have come back from
        Google as an auth failure -- a network round trip to learn something
        this host already knows.
        """

        token_file = self.root / "empty-token"
        token_file.write_text("   \n")
        self.seal(1)
        code, result = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "https://lodus-casino-review.web.app",
            "--adapter", "google", "--deploy-token-file", str(token_file))
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "DEPLOY_TOKEN_UNAVAILABLE")
        self.assertIn("is empty", result["refused"]["detail"])

    def test_a_release_bundle_that_cannot_be_read_refuses_rather_than_traces(self):
        """The sibling of the token read, in the same command.

        Named separately rather than by widening the handler that already
        wraps this block: an ``OSError`` from the adapter or the store reaches
        the same place, and calling that a bad argument would be a false
        refusal -- worse, in a fail-closed ledger, than an unhandled one.
        """

        code, result = self.run_cli(
            "release", "seal", "--rc", "CASINO-MVP-RC-001",
            "--bundle", str(self.root / "absent-bundle.json"),
            "--verification-ref", "verification/casino/001",
            "--qa-ref", "qa/casino/001")
        self.assertEqual(code, 1)
        self.assertEqual(result["refused"]["code"], "RELEASE_BUNDLE_UNREADABLE")

    def test_deploy_review_with_google_adapter(self):
        # 1. Missing artifact bytes fails closed (never returns reached=True with zero bytes)
        self.seal(1)
        code, deployed = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "https://lodus-casino-review.web.app",
            "--adapter", "google")
        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "failed")
        self.assertTrue(deployed["receipt"]["operation_ref"].endswith(":rejected"))

        # 2. Real artifact files with explicitly selected simulation succeeds
        art_dir, files = self.files(2)
        art_digest = production.deployable_digest(files)
        self.assertEqual(art_digest,
                         json.loads(Path(self.bundle_path(2)).read_text())
                         ["artifact"]["identity"])

        self.assertEqual(self.seal(2)[0], 0)

        code, deployed2 = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-002",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", self.surface(2),
            "--adapter", "google", "--simulate", "--artifact-dir", str(art_dir),
            "--probe")
        self.assertEqual(code, 0)
        self.assertEqual(deployed2["state"], "healthy")
        self.assertIn("google-firebase", deployed2["receipt"]["adapter"])


class LedgerResolutionTests(unittest.TestCase):
    """SF-158: which Factory a command with no `--db` is talking about.

    The Owner was shown a Release Candidate by `./dev factory status` and told
    RC_NOT_FOUND by `./dev release show`, because the default ledger path was
    resolved inside the `factory` branch alone and every other command opened a
    file in whatever directory it ran from. One host, one Factory, one ledger.
    """

    def resolved(self, argv):
        return Path(cli._resolved_db(cli.parser().parse_args(argv)))

    def test_the_default_is_the_installed_factorys_own_ledger(self):
        expected = (factory_lifecycle.FactoryConfig.default().state_dir
                    / "factory-controller.db")

        self.assertEqual(self.resolved(["release", "show", "--rc", "r"]),
                         expected)
        self.assertEqual(self.resolved(["factory", "status"]), expected)
        self.assertEqual(self.resolved(["status"]), expected)

    def test_an_explicit_path_still_points_where_it_says(self):
        self.assertEqual(
            self.resolved(["--db", "/tmp/other.db", "release", "show",
                           "--rc", "r"]),
            Path("/tmp/other.db"))
        self.assertEqual(
            self.resolved(["--db", "local.db", "release", "show", "--rc", "r"]),
            Path.cwd() / "local.db")


if __name__ == "__main__":
    unittest.main()


class ReviewProbeTests(unittest.TestCase):
    """SF-179 C: a probe that proves nothing can still carry Owner Validation.

    `--probe` ran the real health verifier without the one argument that makes
    it a verification: `expected_entry_content`.  Checks 1, 2, 4 and 5 ask
    whether *something* answered; a surface serving any 200 at `/` and at
    `/health.json` produced `checks_failed=0`, which settles the review
    deployment `healthy`, which is exactly what `record_owner_validation`
    requires.  The exact-artifact chain would then have been anchored to a
    review nobody proved was the release.
    """

    setUp = ReleaseCommandTests.setUp
    run_cli = ReleaseCommandTests.run_cli
    bundle_path = ReleaseCommandTests.bundle_path

    SEALED = b"<!DOCTYPE html><title>the sealed release</title>\n"
    OTHER = b"<!DOCTYPE html><title>something else entirely</title>\n"

    def surface(self, entry: bytes):
        """A loopback review surface serving whatever it is given."""

        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = (b'{"status": "ok"}\n'
                        if self.path == "/health.json" else entry)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def sealed_rc(self, entry: bytes = SEALED):
        """One sealed RC whose artifact is materialized where a probe finds it."""

        art = self.root / "artifact"
        art.mkdir(parents=True, exist_ok=True)
        (art / "index.html").write_bytes(entry)
        digest = production.deployable_digest({"index.html": entry})
        body = bundle(1).as_row()
        body["artifact"] = {"kind": "static-bundle", "identity": digest}
        path = self.root / "bundle-probe.json"
        path.write_text(json.dumps(body))
        code, _ = self.run_cli("release", "seal", "--rc", "CASINO-MVP-RC-001",
                               "--bundle", str(path), "--verification-ref", "v",
                               "--qa-ref", "q")
        self.assertEqual(code, 0)
        return str(art)

    def probe(self, url, art_dir=None):
        arguments = ["release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
                     "--environment", "lodus-casino-review", "--actor",
                     "factory", "--review-url", url, "--probe"]
        if art_dir is not None:
            arguments += ["--artifact-dir", art_dir]
        return self.run_cli(*arguments)

    def test_a_surface_serving_other_bytes_never_reaches_healthy(self):
        art = self.sealed_rc()
        url = self.surface(self.OTHER)

        code, deployed = self.probe(url, art)

        self.assertEqual(code, 0)
        self.assertNotEqual(deployed["state"], "healthy")
        code, refused = self.run_cli(
            "release", "validate", "--rc", "CASINO-MVP-RC-001",
            "--validation", "CASINO-MVP-VALIDATION-001",
            "--deployment-ref", deployed["deployment_ref"],
            "--decision", "VALIDATED", "--actor", "owner")
        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "REVIEW_NOT_HEALTHY")

    def test_a_surface_serving_the_sealed_bytes_is_healthy(self):
        art = self.sealed_rc()
        url = self.surface(self.SEALED)

        code, deployed = self.probe(url, art)

        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "healthy")

    def test_a_probe_refuses_when_the_sealed_artifact_is_not_on_this_host(self):
        """Fail closed: an absent artifact is not a weaker probe."""

        self.sealed_rc()
        url = self.surface(self.SEALED)

        code, refused = self.probe(url, str(self.root / "nowhere"))

        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"],
                         "REVIEW_ARTIFACT_UNAVAILABLE")


class PromotionOrderingTests(unittest.TestCase):
    """SF-180 B03: promotion read the stored decision, not the review's state.

    An Owner Validation is a decision about a review that was healthy when
    they made it.  Between that moment and the promotion the same review can
    be observed failed, and `promote_validated` looked only at the stored
    validation -- so a release the environment had already reported broken
    was still admitted to Production.
    """

    setUp = ReleaseCommandTests.setUp
    run_cli = ReleaseCommandTests.run_cli
    artifact_dir = ReleaseCommandTests.artifact_dir
    files = ReleaseCommandTests.files
    bundle_path = ReleaseCommandTests.bundle_path
    seal = ReleaseCommandTests.seal
    surface = ReleaseCommandTests.surface
    review = ReleaseCommandTests.review
    validate = ReleaseCommandTests.validate
    promote = ReleaseCommandTests.promote

    def validated(self):
        self.assertEqual(self.seal(1)[0], 0)
        code, deployed = self.review(1)
        self.assertEqual(code, 0)
        self.assertEqual(deployed["state"], "healthy")
        code, validated = self.validate(deployed["deployment_ref"])
        self.assertEqual(code, 0)
        self.assertEqual(validated["decision"], "VALIDATED")
        return deployed

    def test_a_review_observed_failed_after_validation_stops_the_promotion(self):
        deployed = self.validated()

        code, health = self.run_cli(
            "production", "health", "--deployment", deployed["deployment_id"],
            "--failed", "1", "--ref", "probe://failed-after-validation")
        self.assertEqual(code, 0)
        self.assertEqual(health["state"], "failed")

        code, refused = self.promote(1)

        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "REVIEW_NOT_HEALTHY")

    def test_a_review_still_healthy_promotes_exactly_as_before(self):
        self.validated()

        code, promoted = self.promote(1)

        self.assertEqual(code, 0)
        self.assertTrue(promoted["same_artifact"])

    def test_a_fabricated_negative_observation_is_refused_at_the_command(self):
        """SF-180 B02, reached the way an operator reaches it."""

        self.assertEqual(self.seal(1)[0], 0)

        code, refused = self.run_cli(
            "release", "deploy-review", "--rc", "CASINO-MVP-RC-001",
            "--environment", "lodus-casino-review", "--actor", "factory",
            "--review-url", "https://review.example.invalid/casino",
            "--passed", "-1")

        self.assertEqual(code, 1)
        self.assertEqual(refused["refused"]["code"], "HEALTH_OBSERVATION_INVALID")
