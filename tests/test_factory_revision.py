"""SF-160: `./dev factory revise` -- one Owner act, RETURN_FOR_CHANGES onward.

The first build/review path was complete and the second Owner turn was not: a
package could supersede another, and `pcp.intake` still minted one
`<package_id>:build` work item for every version, the run contract still pinned
the original baseline, and Owner Validation still required a health row nothing
could produce from an observation.  A resubmit would therefore have collided
with the mission whose result the Owner rejected, or rebuilt from a commit they
had already seen superseded.

What is checked here is that the revision is a *different* mission built from
an *explicit* predecessor, that the release the Owner turned away survives it
untouched and unpromoted, and that health is measured rather than asserted.
"""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import replace
from pathlib import Path
import unittest

from factory_controller import factory, pcp, product, release
from factory_controller import store as store_mod

from tests.test_factory_lifecycle import fake_context
from tests.test_factory_product import CHECKOUT, CONTRACT, ProductReviewTests


REQUESTED = [
    "Replace free-form stake entry with predefined stake choices.",
    "Remove decimal points from whole-number monetary displays.",
]


class StaleContextAdapter:
    """Reproduce SF-162's settled pre-provider Context Broker refusal."""

    def execute(self, step, operation_key, value):
        if step == "context":
            return {"status": "refused", "refusal_code": "STALE_HEAD"}
        return {"status": "completed", "candidate_sha": "a" * 40}


def revision_package(*, rc_id, candidate_sha, validation_id="ov-1",
                     version=2, changes=None, decision=None):
    """A superseding package, built from the frozen one it supersedes."""

    body = dict(pcp.materialize_casino_pcp())
    body["package_version"] = version
    body["supersedes"] = "lodus-casino@v1"
    body["revision"] = {
        "predecessor_rc": rc_id,
        "predecessor_candidate_sha": candidate_sha,
        "owner_validation_id": validation_id,
        "owner_decision": decision or pcp.RETURN_FOR_CHANGES,
        "requested_changes": list(REQUESTED if changes is None else changes),
    }
    return body


class ReviewSurface:
    """A real loopback web root, because the probe really fetches one."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status = body, status
        surface = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(surface.status)
                self.send_header("Content-Length", str(len(surface.body)))
                self.end_headers()
                self.wfile.write(surface.body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FactoryRevisionTests(unittest.TestCase):
    setUp = ProductReviewTests.setUp
    ready = ProductReviewTests.ready
    submit = ProductReviewTests.submit
    missions = ProductReviewTests.missions
    review = ProductReviewTests.review
    candidates = ProductReviewTests.candidates
    finish_the_product_mission = \
        ProductReviewTests.finish_the_product_mission
    CANDIDATE = ProductReviewTests.CANDIDATE

    def reviewed(self):
        """Get to the state SF-160 starts from: one sealed, reviewed RC.

        The loopback surface is started before the review because the review
        deployment records its surface immutably -- which is the production
        behaviour too, where the surface is one fixed loopback address.
        """

        self.surface = ReviewSurface(b"")
        self.addCleanup(self.surface.close)
        self.lifecycle.config = replace(self.lifecycle.config,
                                        review_url=self.surface.url)
        self.ready()
        self.submit()
        self.finish_the_product_mission()
        result = self.review()
        self.assertTrue(result.ok, result.render())
        self.rc_id = result.details["rc_id"]
        self.served = Path(result.details["review_root"])
        return result

    def serve_the_reviewed_bytes(self, body=None, status=200):
        """What the Owner's own `./dev review up` puts on that surface."""

        entry = self.served / "index.html"
        self.surface.body = entry.read_bytes() if body is None else body
        self.surface.status = status
        return self.surface

    def package(self, **options):
        path = Path(self.temp.name) / ("pcp-revision-%s.json"
                                       % options.get("version", 2))
        path.write_text(json.dumps(revision_package(
            rc_id=options.pop("rc_id", self.rc_id),
            candidate_sha=options.pop("candidate_sha", self.CANDIDATE),
            **options)))
        return path

    def revise(self, path=None, **options):
        return self.lifecycle.dispatch(
            "revise", package=str(path or self.package(**options)))

    def validations(self):
        release.ReleaseLifecycle(self.lifecycle.store)
        with self.lifecycle.store.transaction() as db:
            return [dict(row) for row in
                    db.execute("SELECT * FROM owner_validations")]

    # -- the identity a revision has to have ----------------------------- #

    def test_a_superseding_package_mints_its_own_work_item(self):
        """The whole defect: every version minted `<package_id>:build`."""

        first = pcp.intake(pcp.materialize_casino_pcp())
        second = pcp.intake(revision_package(rc_id="rc-x", candidate_sha="7" * 40))
        self.assertEqual(first.mission["work_item_id"], "lodus-casino:build")
        self.assertEqual(second.mission["work_item_id"], "lodus-casino:revision:2")
        self.assertNotEqual(first.mission["work_item_id"],
                            second.mission["work_item_id"])

    def test_a_first_version_may_not_claim_a_predecessor(self):
        body = revision_package(rc_id="rc-x", candidate_sha="7" * 40, version=1)
        body["supersedes"] = None
        with self.assertRaises(pcp.PCPRefusal) as caught:
            pcp.validate(body)
        self.assertEqual(caught.exception.code, "PCP_REVISION_INVALID")

    def test_a_revision_names_the_release_it_supersedes_completely(self):
        for missing in ("predecessor_rc", "predecessor_candidate_sha",
                        "owner_validation_id", "owner_decision",
                        "requested_changes"):
            body = revision_package(rc_id="rc-x", candidate_sha="7" * 40)
            body["revision"].pop(missing)
            with self.assertRaises(pcp.PCPRefusal) as caught:
                pcp.validate(body)
            self.assertEqual(caught.exception.code, "PCP_REVISION_INVALID", missing)

    def test_only_a_returned_release_can_be_revised(self):
        body = revision_package(rc_id="rc-x", candidate_sha="7" * 40,
                                decision="VALIDATED")
        with self.assertRaises(pcp.PCPRefusal) as caught:
            pcp.validate(body)
        self.assertEqual(caught.exception.code, "PCP_REVISION_INVALID")

    def test_a_revision_is_never_built_from_the_products_frozen_baseline(self):
        intake = pcp.intake(revision_package(rc_id="rc-x", candidate_sha="7" * 40))
        with self.assertRaises(product.ProductRefusal) as caught:
            product.mission_for(CONTRACT, intake)
        self.assertEqual(caught.exception.code, "PRODUCT_REVISION_BASE_REQUIRED")

    def test_the_requested_changes_reach_the_provider_verbatim(self):
        """The one place a product requirement becomes provider-visible text."""

        intake = pcp.intake(revision_package(rc_id="rc-x", candidate_sha="7" * 40))
        addendum = product.revision_addendum(intake)
        for change in REQUESTED:
            self.assertIn(change, addendum)
        self.assertIn("rc-x", addendum)
        self.assertIn("lodus-casino@v2", addendum)
        # The wire brief stays a pointer; it is bounded and cannot carry these.
        brief = product.brief(CONTRACT, intake)
        self.assertLessEqual(len(brief), product.BRIEF_LIMIT)
        for change in REQUESTED:
            self.assertNotIn(change, brief)

    # -- the Owner act --------------------------------------------------- #

    def test_one_command_records_the_decision_and_opens_the_revision(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise()
        self.assertTrue(result.ok, result.render())

        recorded = self.validations()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["decision"], "RETURN_FOR_CHANGES")
        self.assertEqual(recorded[0]["rc_id"], self.rc_id)
        self.assertEqual(recorded[0]["candidate_sha"], self.CANDIDATE)
        self.assertEqual(recorded[0]["decided_by"], "owner")

        self.assertEqual(result.details["work_item_id"],
                         "lodus-casino:revision:2")
        self.assertEqual(result.details["predecessor_rc"], self.rc_id)
        self.assertEqual(result.details["predecessor_candidate_sha"],
                         self.CANDIDATE)
        self.assertEqual(result.details["baseline_sha"],
                         result.details["revision_sha"])
        self.assertNotEqual(result.details["baseline_sha"],
                            CONTRACT.baseline_sha)

    def test_the_revision_base_descends_from_the_candidate_that_was_reviewed(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        self.revise()
        self.assertEqual(len(self.host.revision_requests), 1)
        request = self.host.revision_requests[0]
        self.assertEqual(request["predecessor_sha"], self.CANDIDATE)
        self.assertEqual(request["ref"], "refs/heads/factory/revision/v2")
        for change in REQUESTED:
            self.assertIn(change, request["addendum"])

    def test_the_revision_is_admitted_as_a_second_distinct_mission(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        self.revise()
        missions = self.missions()
        self.assertEqual(len(missions), 2)
        items = [row["payload"]["work_item_id"] for row in missions]
        self.assertEqual(items, ["lodus-casino:build", "lodus-casino:revision:2"])
        self.assertNotEqual(missions[0]["idempotency_key"],
                            missions[1]["idempotency_key"])
        self.assertEqual(missions[0]["state"], "completed")

    def test_repeating_the_command_changes_nothing(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        first = self.revise()
        second = self.revise()
        self.assertTrue(second.ok, second.render())
        self.assertEqual(second.state, "already-submitted")
        self.assertEqual(first.details["mission_id"], second.details["mission_id"])
        self.assertEqual(first.details["revision_sha"],
                         second.details["revision_sha"])
        self.assertEqual(len(self.validations()), 1)
        self.assertEqual(len(self.missions()), 2)

    def test_an_unproven_terminal_revision_opens_a_distinct_successor_attempt(self):
        """A legacy key conflict gets a fresh manifest, never a rewritten row."""
        self.reviewed()
        self.serve_the_reviewed_bytes()
        first = self.revise()
        self.assertTrue(first.ok, first.render())

        # Reproduce the legacy admission shape: the old row owns the stable
        # attempt-1 key but its serialized input predates the current payload.
        with self.lifecycle.store.transaction() as db:
            db.execute("UPDATE missions SET payload_hash=?, state=?, "
                       "terminal_reason=? WHERE id=?",
                       ("legacy-payload-hash", "refused",
                        "IDEMPOTENCY_KEY_UNPROVEN: layer echoed no key",
                        first.details["mission_id"]))
        legacy = self.lifecycle.store.get(first.details["mission_id"])

        second = self.revise()
        self.assertTrue(second.ok, second.render())
        self.assertEqual(second.details["attempt"], 2)
        self.assertEqual(second.details["predecessor_mission_id"],
                         first.details["mission_id"])
        self.assertNotEqual(second.details["mission_id"],
                            first.details["mission_id"])
        self.assertEqual(len(self.missions()), 3)

        after = self.lifecycle.store.get(first.details["mission_id"])
        self.assertEqual(after["state"], legacy["state"])
        self.assertEqual(after["terminal_reason"], legacy["terminal_reason"])
        self.assertEqual(after["payload_hash"], "legacy-payload-hash")
        self.assertTrue(any(
            row["reason"] == "REVISION_SUCCESSOR_OPENED"
            and row["mission_id"] == second.details["mission_id"]
            for row in self.lifecycle.store.coordination(None)))

    # -- SF-179 A: which conflicts are that mission's retry --------------- #
    #
    # SF-178 B1: terminal state plus `IDEMPOTENCY_KEY_UNPROVEN` says a mission
    # died, not that the mission that died is the one being resubmitted.  The
    # key binds only the identity fields and the manifest, so a conflict can
    # still carry a different provider, gate command, class or budget.

    def legacy_predecessor(self, stage1=None, **changes):
        """Leave behind the durable shape SF-176 actually recovered.

        The historical row was admitted before a revision had its own
        checkout, so its stage-1 paths and Owner act counter differ from a
        fresh admission while the work it describes is identical.  Keyword
        arguments overlay whatever else a case needs to differ.
        """

        first = self.revise()
        self.assertTrue(first.ok, first.render())
        row = self.lifecycle.store.get(first.details["mission_id"])
        payload = json.loads(json.dumps(row["payload"]))
        payload["approval_ref"] = payload["approval_ref"] + "-shift-13"
        product_checkout = "/Users/Shared/Projects/software-factory/lodus-casino"
        payload["stage1"].update({
            "revision_grounding": None,
            "repository": product_checkout,
            "gate_workdir": product_checkout,
            "gate_commands": {gate: [product_checkout + "/dev",
                                     gate.removeprefix("dev-")]
                              for gate in payload["stage1"]["gate_commands"]},
        })
        payload["stage1"].update(stage1 or {})
        payload.update(changes)
        with self.lifecycle.store.transaction() as db:
            db.execute("UPDATE missions SET payload_json=?, payload_hash=?, "
                       "state=?, terminal_reason=? WHERE id=?",
                       (store_mod.canonical_json(payload),
                        store_mod.payload_hash(payload), "refused",
                        "IDEMPOTENCY_KEY_UNPROVEN: layer echoed no key",
                        first.details["mission_id"]))
        return first, self.lifecycle.store.get(first.details["mission_id"])

    def assert_fails_closed(self, first, legacy, code):
        """No successor, and the historical row is left exactly as it was."""

        second = self.revise()
        self.assertFalse(second.ok, second.render())
        self.assertEqual(second.details["code"], code)
        self.assertEqual(len(self.missions()), 2)
        after = self.lifecycle.store.get(first.details["mission_id"])
        self.assertEqual(after["payload_hash"], legacy["payload_hash"])
        self.assertEqual(after["state"], legacy["state"])
        self.assertEqual(after["terminal_reason"], legacy["terminal_reason"])
        self.assertEqual(
            [], [row for row in self.lifecycle.store.coordination(None)
                 if row["reason"] == "REVISION_SUCCESSOR_OPENED"])
        return second

    def test_the_real_legacy_admission_shape_still_opens_a_successor(self):
        """Drifted Owner act counter and stage-1 paths, identical work."""

        self.reviewed()
        self.serve_the_reviewed_bytes()
        first, legacy = self.legacy_predecessor()
        second = self.revise()
        self.assertTrue(second.ok, second.render())
        self.assertEqual(second.details["attempt"], 2)
        self.assertEqual(second.details["predecessor_mission_id"],
                         first.details["mission_id"])
        after = self.lifecycle.store.get(first.details["mission_id"])
        self.assertEqual(after["payload_hash"], legacy["payload_hash"])
        self.assertEqual(after["payload"], legacy["payload"])
        self.assertEqual(after["state"], legacy["state"])

    def test_a_changed_provider_never_inherits_the_legacy_retry(self):
        """The exact SF-178 B1 reproduction: same key, different provider."""

        self.reviewed()
        self.serve_the_reviewed_bytes()
        first, legacy = self.legacy_predecessor(provider_candidates=[
            {"profile": "unrelated-product", "capabilities": ["development"]}])
        self.assert_fails_closed(first, legacy, "REVISION_INPUT_CHANGED")

    def test_a_changed_stage_one_command_never_inherits_the_retry(self):
        """The paths may drift; what is executed in them may not."""

        self.reviewed()
        self.serve_the_reviewed_bytes()
        first, legacy = self.legacy_predecessor(
            stage1={"command": ["/usr/bin/env", "python3", "-m", "elsewhere"]})
        self.assert_fails_closed(first, legacy, "REVISION_INPUT_CHANGED")

    def test_other_payload_only_differences_stay_fail_closed(self):
        """Every field outside the proven drift set is part of the request."""

        for field_name, value in (("work_class", "hotfix"),
                                  ("environment_class", "production"),
                                  ("policy_version", "SF-999-other"),
                                  ("context_budget", {"max_bytes": 1,
                                                      "max_files": 1})):
            with self.subTest(field=field_name):
                self.setUp()
                self.reviewed()
                self.serve_the_reviewed_bytes()
                first, legacy = self.legacy_predecessor(
                    **{field_name: value})
                self.assert_fails_closed(first, legacy,
                                         "REVISION_INPUT_CHANGED")

    def test_the_divergence_check_reads_every_field_it_was_not_told_to_skip(self):
        """A payload field added later is fail-closed without being listed."""

        base = {"work_item_id": "lodus-casino:revision:2", "attempt": 1,
                "approval_ref": "a", "stage1": {"command": ["run"],
                                                "gate_workdir": "/old"}}
        same = {**base, "approval_ref": "b",
                "stage1": {"command": ["run"], "gate_workdir": "/new"}}
        self.assertIsNone(factory.legacy_conflict_divergence(base, same))
        self.assertEqual(
            "attempt",
            factory.legacy_conflict_divergence(base, {**same, "attempt": 2}))
        self.assertEqual(
            "provider_candidates",
            factory.legacy_conflict_divergence(base, {**same,
                                                      "provider_candidates": []}))
        self.assertEqual(
            "future_field",
            factory.legacy_conflict_divergence(base, {**same,
                                                      "future_field": 1}))
        self.assertEqual(
            "stage1.mutates_repository",
            factory.legacy_conflict_divergence(
                base, {**same, "stage1": {**same["stage1"],
                                          "mutates_repository": True}}))
        self.assertEqual("stage1",
                         factory.legacy_conflict_divergence(base,
                                                            {**same,
                                                             "stage1": None}))

    # -- SF-162: where the revision is grounded --------------------------- #
    #
    # The Owner ran this command for real and it refused: the Context Broker
    # grounds only the current HEAD of the checkout it reads, the registered
    # checkout is on the product branch, and the base is deliberately on no
    # branch.  The repair is a checkout of the base, not a weaker Broker.

    def watch_grounding(self):
        """Record which local copy each repository grounding is read from."""

        seen = []
        # The injected builder never sees a checkout, so it cannot show which
        # one was used.  Preflighting through the real seam is the only place
        # that argument exists.
        self.lifecycle.context_builder = None

        def build(wire, *, checkout, interpreter):
            seen.append({"checkout": checkout,
                         "baseline_sha": wire.get("baseline_sha")})
            return fake_context(wire)

        self.lifecycle._build_context = build
        return seen

    def test_the_revision_is_grounded_on_the_base_not_the_product_branch(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        seen = self.watch_grounding()
        result = self.revise()
        self.assertTrue(result.ok, result.render())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["baseline_sha"], result.details["revision_sha"])
        self.assertEqual(seen[0]["checkout"], result.details["revision_checkout"])
        # The registered checkout is where the STALE_HEAD came from.
        self.assertNotEqual(seen[0]["checkout"], CHECKOUT)

    def test_the_admitted_revision_carries_the_same_execution_grounding(self):
        """The checkout used in preflight is the one durable Stage-1 receives."""

        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise()
        self.assertTrue(result.ok, result.render())
        mission = self.missions()[1]
        stage1 = mission["payload"]["stage1"]
        grounding = stage1["revision_grounding"]
        self.assertEqual(stage1["repository"], result.details["revision_checkout"])
        self.assertEqual(stage1["gate_workdir"], result.details["revision_checkout"])
        self.assertEqual(stage1["gate_commands"]["dev-evaluate"], [
            result.details["revision_checkout"] + "/dev", "evaluate"])
        self.assertEqual(grounding["revision_sha"], result.details["revision_sha"])
        self.assertEqual(grounding["checkout"], result.details["revision_checkout"])
        self.assertEqual(grounding["revision_ref"], result.details["revision_ref"])

    def test_start_rebinds_the_existing_refusal_without_starting_a_provider(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        revised = self.revise()
        mission_id = revised.details["mission_id"]

        self.lifecycle.controller.adapter = StaleContextAdapter()
        refused = self.lifecycle.controller.work_once("stale-worker")
        self.assertEqual(refused["state"], "refused")
        self.assertIn("STALE_HEAD", refused["terminal_reason"])
        self.assertEqual(self.lifecycle.store.runs(mission_id), [])

        started = self.lifecycle.dispatch("start")
        self.assertTrue(started.ok, started.render())
        self.assertIn("Existing revision mission rebound", started.render())
        resumed = self.lifecycle.store.get(mission_id)
        self.assertEqual(resumed["state"], "admitted")
        self.assertEqual(resumed["attempt_count"], 1)
        self.assertEqual(self.lifecycle.store.runs(mission_id), [])
        self.assertEqual(len(self.host.revision_resolves), 1)
        self.assertEqual(self.host.revision_resolves[0]["revision_sha"],
                         revised.details["revision_sha"])
        self.assertTrue(any(event["kind"] == "REVISION_CONTEXT_REBOUND"
                            for event in self.lifecycle.store.history(mission_id)))

    def test_an_ordinary_build_is_still_grounded_on_the_registered_checkout(self):
        """Nothing about ordinary work moves; only the revision path is new."""

        self.ready()
        seen = self.watch_grounding()
        self.assertTrue(self.submit().ok)
        self.assertEqual([call["checkout"] for call in seen], [CHECKOUT])
        self.assertEqual(seen[0]["baseline_sha"], CONTRACT.baseline_sha)

    def test_an_ordinary_stale_checkout_is_still_refused(self):
        """SF-163 must not turn the ordinary stale-head guard into a retry."""

        self.ready()
        self.lifecycle.context_builder = None

        def stale(wire, *, checkout, interpreter):
            answer = fake_context(wire)
            answer["measurement"]["head_sha"] = "c" * 40
            return answer

        self.lifecycle._build_context = stale
        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "CONTEXT_HEAD_MISMATCH")
        self.assertEqual(self.missions(), [])

    def test_a_base_with_no_checkout_to_ground_on_is_refused(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        original = self.host._revision

        def without_checkout(arguments):
            answer = original(arguments)
            body = json.loads(answer.stdout)
            body.pop("revision_checkout")
            return type(answer)(0, json.dumps(body), "")

        self.host._revision = without_checkout
        result = self.revise()
        self.assertFalse(result.ok)
        self.assertEqual(result.details.get("code"),
                         "REVISION_CHECKOUT_NOT_OPENED")

    def test_the_checkout_the_base_was_grounded_on_is_recorded(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise()
        self.assertTrue(result.details["revision_checkout"].endswith(
            result.details["revision_sha"]))

    # -- what must survive the revision ---------------------------------- #

    def test_the_returned_release_stays_sealed_and_unpromoted(self):
        self.reviewed()
        before = self.candidates()
        self.serve_the_reviewed_bytes()
        self.revise()
        self.assertEqual(self.candidates(), before)
        with self.lifecycle.store.transaction() as db:
            promoted = db.execute(
                "SELECT COUNT(*) AS n FROM release_deployments"
                " WHERE environment_class='production'").fetchone()
        self.assertEqual(promoted["n"], 0)

    def test_a_release_already_in_production_is_not_revisable(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        with self.lifecycle.store.transaction() as db:
            row = db.execute("SELECT * FROM release_deployments"
                             " WHERE rc_id=?", (self.rc_id,)).fetchone()
            db.execute(
                "INSERT INTO release_deployments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("production:%s:x" % self.rc_id, self.rc_id,
                 row["deployment_id"], "lodus-casino-production", "production",
                 row["candidate_sha"], row["artifact_digest"],
                 row["bundle_digest"], None, "healthy", row["created_at"]))
        result = self.revise()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "RELEASE_ALREADY_PROMOTED")
        self.assertEqual(self.validations(), [])

    def test_a_package_naming_another_candidate_is_refused(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise(candidate_sha="9" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "REVISION_PREDECESSOR_MISMATCH")
        self.assertEqual(self.validations(), [])
        self.assertEqual(len(self.missions()), 1)

    def test_a_package_naming_a_release_this_factory_never_sealed_is_refused(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise(rc_id="rc-lodus-casino-000000000000")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "RC_NOT_FOUND")
        self.assertEqual(len(self.missions()), 1)

    def test_a_first_build_package_is_not_a_revision(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.lifecycle.dispatch("revise", package=str(self.package_path))
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_NOT_A_REVISION")

    def test_revise_with_no_package_named_refuses(self):
        self.reviewed()
        result = self.lifecycle.dispatch("revise")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_PACKAGE_REQUIRED")

    # -- health is observed, never asserted ------------------------------ #

    def test_the_review_surface_is_measured_and_that_is_what_makes_it_healthy(self):
        self.reviewed()
        with self.lifecycle.store.transaction() as db:
            before = db.execute(
                "SELECT state FROM deployments").fetchone()["state"]
        self.assertEqual(before, "verifying")
        self.serve_the_reviewed_bytes()
        self.assertTrue(self.revise().ok)
        with self.lifecycle.store.transaction() as db:
            row = db.execute("SELECT state, health_outcome FROM"
                             " deployments").fetchone()
        self.assertEqual(row["state"], "healthy")
        self.assertEqual(row["health_outcome"], "healthy")

    def test_a_stopped_review_surface_refuses_and_records_no_health(self):
        self.reviewed()
        surface = self.serve_the_reviewed_bytes()
        surface.close()
        result = self.revise()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "REVIEW_SURFACE_UNREACHABLE")
        with self.lifecycle.store.transaction() as db:
            row = db.execute("SELECT state FROM deployments").fetchone()
        self.assertEqual(row["state"], "verifying")
        self.assertEqual(self.validations(), [])

    def test_a_surface_serving_something_else_refuses(self):
        """Health is about *this* release, or it is not health at all."""

        self.reviewed()
        self.serve_the_reviewed_bytes(body=b"<!doctype html><title>other</title>\n")
        result = self.revise()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "REVIEW_SURFACE_MISMATCH")
        self.assertEqual(self.validations(), [])

    def test_a_surface_answering_an_error_refuses(self):
        self.reviewed()
        self.serve_the_reviewed_bytes(status=503)
        result = self.revise()
        self.assertFalse(result.ok)
        self.assertIn(result.details["code"],
                      {"REVIEW_SURFACE_MISMATCH", "REVIEW_SURFACE_UNREACHABLE"})
        self.assertEqual(self.validations(), [])

    # -- what the Owner is told ------------------------------------------ #

    def test_status_distinguishes_the_returned_release_and_the_revision(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        self.revise()
        rendered = self.lifecycle.dispatch("status").render()
        self.assertIn("Returned for changes: %s" % self.rc_id, rendered)
        self.assertIn("stays sealed and is not promoted", rendered)
        self.assertIn("lodus-casino:revision:2", rendered)
        self.assertIn("Revision lineage", rendered)

    def test_the_owner_act_records_the_predecessor_it_supersedes(self):
        self.reviewed()
        self.serve_the_reviewed_bytes()
        result = self.revise()
        acts = [row for row in self.lifecycle.store.coordination(None, limit=100)
                if row.get("reason") == "FACTORY_OWNER_ACTION"
                and (row.get("detail") or {}).get("action") == "revise"]
        self.assertEqual(len(acts), 1)
        detail = acts[0]["detail"]
        self.assertEqual(detail["predecessor_rc"], self.rc_id)
        self.assertEqual(detail["predecessor_candidate_sha"], self.CANDIDATE)
        self.assertEqual(detail["revision_sha"], result.details["revision_sha"])


if __name__ == "__main__":
    unittest.main()
