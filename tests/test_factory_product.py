"""`./dev factory product` — the one verb where the Owner names the work.

The dogfood verb's promise is that the Owner names nothing.  This one's is the
opposite, so what is checked here is that the package the Owner named is the
only source of intent, that submitting is an act with a record over it, and
that the widening it performs is exactly one profile and one project.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from factory_controller import pcp, product, production, release
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.engine import Controller
from factory_controller.store import MissionStore

from tests.test_factory_lifecycle import (
    FakeHost, NoopAdapter, PROTOTYPE_SHA, BUG_SHA, fake_context,
)


CONTRACT = product.ProductContract.load(
    Path(__file__).resolve().parent.parent / "contracts"
    / "lodus-casino-product-run-contract.json")
CHECKOUT = "/products/lodus-casino"


class FactoryProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        base = FactoryConfig.default()
        bridge_root = root / "bridge-source"
        (bridge_root / "contracts").mkdir(parents=True)
        self.config = replace(
            base,
            agents_dir=root / "LaunchAgents",
            state_dir=root / "state",
            bridge_prefix=root / "bridge",
            bridge_root=bridge_root,
            capability_request_path=root / "dogfood-capability.json",
        )
        self.config.capability_request_path.write_text(json.dumps({
            "accepted_unknowns": [], "capability": "bug",
            "policy_ref": "vault://a#5", "profiles": ["codex-primary"],
            "projects": ["factory-bug-lab", "factory-prototype-lab"],
            "request_ref": "SF-145A-bug-capability",
            "schema_version": "factory.bridge.capability_admission_request.v1",
        }))
        (bridge_root / "contracts" / CONTRACT.capability_request).write_text(
            json.dumps({
                "accepted_unknowns": [], "capability": "development",
                "policy_ref": "vault://active/projects/lodus-casino/overview.md",
                "profiles": list(CONTRACT.provider_profiles),
                "projects": list(CONTRACT.projects),
                "request_ref": "SF-151A-development-capability",
                "schema_version":
                    "factory.bridge.capability_admission_request.v1",
            }))

        self.package_path = root / "pcp-v1.1.0.json"
        self.package_path.write_text(json.dumps(pcp.materialize_casino_pcp()))
        self.intake = pcp.intake(pcp.materialize_casino_pcp())

        self.host = FakeHost(self.config)
        self.host.checkouts["lodus-casino"] = CHECKOUT
        self.host.extra_projects = [{
            "project_id": "lodus-casino",
            "repository_remote_url":
                "https://github.com/linc-abela/lodus-casino.git",
            "resolution": "resolved", "capabilities": ["development"],
            "checkout": CHECKOUT,
        }]
        # Both product runtimes, because the contract now declares a failover
        # and a harness that modelled one of them would test the wrong shape.
        self.host.extra_profiles = [
            {"profile_id": "codex-product", "status": "available",
             "readiness": "available"},
            {"profile_id": "claude-product", "status": "available",
             "readiness": "available"},
        ]
        self.lifecycle = FactoryLifecycle(
            Controller(MissionStore(root / "controller.db"), NoopAdapter()),
            config=self.config, runner=self.host,
            owner=OwnerIdentity(501, "owner"),
            remote_reachability={
                "factory-prototype-lab": (PROTOTYPE_SHA,),
                "factory-bug-lab": (BUG_SHA,),
                "lodus-casino": (CONTRACT.baseline_sha,),
            },
            reports={"evidence_core": {"status": "ACCEPTED", "identity": "e"},
                     "context_broker": {"status": "ok", "identity": "c"}},
            context_builder=fake_context)

    def ready(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        started = self.lifecycle.dispatch("start")
        self.assertTrue(started.ok, started.render())

    def submit(self, package=None):
        return self.lifecycle.dispatch(
            "product", package=str(package or self.package_path))

    def missions(self):
        store = self.lifecycle.store
        return [store.get(row["id"]) for row in store.all_missions()]

    def owner_actions(self):
        return [row for row in self.lifecycle.store.coordination(None, limit=100)
                if row.get("reason") == "FACTORY_OWNER_ACTION"]

    # -- refusals, and what they leave behind ---------------------------- #

    def test_a_product_with_no_package_named_refuses_and_submits_nothing(self):
        self.ready()
        result = self.lifecycle.dispatch("product")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_PACKAGE_REQUIRED")
        self.assertEqual(self.missions(), [])

    def test_a_product_from_a_stopped_factory_refuses_before_any_record(self):
        """No Owner act, no admission, no mission: nothing happened at all."""

        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "FACTORY_NOT_READY")
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.owner_actions(), [])
        self.assertEqual(self.host.capability_admits, 0)

    def test_a_package_for_another_product_refuses_and_submits_nothing(self):
        self.ready()
        other = pcp.materialize_casino_pcp()
        other["package_id"] = "some-other-product"
        path = Path(self.temp.name) / "other.json"
        path.write_text(json.dumps(other))
        result = self.submit(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_PACKAGE_MISMATCH")
        self.assertEqual(self.missions(), [])

    def test_a_package_that_is_not_a_package_refuses_with_its_own_code(self):
        self.ready()
        path = Path(self.temp.name) / "not-a-package.json"
        path.write_text(json.dumps({"package_id": "lodus-casino"}))
        result = self.submit(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PCP_REQUIRED_FIELD_MISSING")

    def test_an_unreadable_package_refuses_rather_than_defaulting(self):
        self.ready()
        result = self.submit(Path(self.temp.name) / "absent.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PCP_UNREADABLE")

    def test_a_product_without_containment_refuses(self):
        self.ready()
        self.host.containment = False
        result = self.submit()
        self.assertFalse(result.ok)
        self.assertIn("containment", result.render().lower())
        self.assertEqual(self.missions(), [])

    # -- the submission itself -------------------------------------------- #

    def test_the_owner_submission_admits_exactly_one_product_mission(self):
        self.ready()
        result = self.submit()

        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.state, "submitted")
        self.assertEqual(result.details["work_item_id"], "lodus-casino:build")
        self.assertEqual(result.details["package_digest"],
                         self.intake.package_digest)
        self.assertEqual(result.details["baseline_sha"], CONTRACT.baseline_sha)

        missions = self.missions()
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0]["payload"]["work_item_id"],
                         "lodus-casino:build")
        self.assertEqual(missions[0]["project_id"], "lodus-casino")
        self.assertEqual(missions[0]["state"], "admitted")

    def test_the_mission_carries_the_product_capability_and_its_own_gates(self):
        self.ready()
        self.submit()
        payload = self.missions()[0]["payload"]
        self.assertEqual(payload["capability"], "development")
        self.assertEqual(payload["acceptance_gate_ids"],
                         list(CONTRACT.acceptance_gate_ids))
        self.assertTrue(payload["stage1"]["mutates_repository"])
        self.assertEqual(payload["stage1"]["gate_commands"]["dev-evaluate"],
                         [CHECKOUT + "/dev", "evaluate"])
        self.assertEqual([entry["profile"]
                          for entry in payload["provider_candidates"]],
                         list(CONTRACT.provider_profiles))

    def test_the_provider_is_told_which_package_and_which_gate(self):
        """MISSION.md is the instruction; the brief is a bounded pointer."""

        self.ready()
        self.submit()
        brief = self.missions()[0]["payload"]["stage1"]["mission_brief"]
        self.assertIn("lodus-casino@v1", brief)
        self.assertIn("./dev evaluate", brief)
        self.assertLessEqual(len(brief), product.BRIEF_LIMIT)

    def test_the_corpus_identity_is_the_submitted_package_bytes(self):
        self.ready()
        self.submit()
        admission = json.loads(
            (self.config.mission_dir / "lodus-casino-build-admission.json")
            .read_text())
        manifest = admission["admission_evidence"]["context_manifest"]
        self.assertEqual(manifest["corpus_identity"],
                         "package://lodus-casino@v1@" + self.intake.package_digest)

    def test_the_owner_act_is_recorded_with_a_hash_over_it(self):
        self.ready()
        result = self.submit()

        recorded = json.loads(
            (self.config.mission_dir / "lodus-casino-build-owner-intake.json")
            .read_text())
        self.assertEqual(recorded["chosen_action"], "submit")
        self.assertEqual(recorded["evidence_class"], "human_authority")
        self.assertEqual(recorded["owner"], "owner")
        self.assertEqual(recorded["package_digest"], self.intake.package_digest)
        self.assertEqual(recorded["act_hash"], result.details["owner_act_hash"])

        actions = [row for row in self.owner_actions()
                   if row["detail"].get("action") == "product"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["detail"]["package_digest"],
                         self.intake.package_digest)

    def test_submitting_the_same_package_twice_admits_one_mission(self):
        self.ready()
        first = self.submit()
        second = self.submit()

        self.assertTrue(second.ok, second.render())
        self.assertEqual(second.state, "already-submitted")
        self.assertEqual(second.details["idempotency_key"],
                         first.details["idempotency_key"])
        self.assertEqual(len(self.missions()), 1)

    # -- the widening ------------------------------------------------------ #

    def test_the_submission_admits_development_and_nothing_wider(self):
        self.ready()
        admits_before = self.host.capability_admits
        self.submit()

        requests = [json.loads(text) for command, text in self.host.calls
                    if text and command[-3:-1] == ("capability", "admit")]
        self.assertEqual(self.host.capability_admits, admits_before + 1)
        self.assertEqual(requests[-1]["capability"], "development")
        self.assertEqual(requests[-1]["profiles"],
                         ["codex-product", "claude-product"])
        self.assertEqual(requests[-1]["projects"], ["lodus-casino"])
        self.assertEqual(requests[-1]["authorized_by"], "owner")

    def test_a_widened_request_file_cannot_widen_past_the_contract(self):
        """The contract, not the file, bounds what an admission may name."""

        self.ready()
        path = (self.config.bridge_root / "contracts"
                / CONTRACT.capability_request)
        body = json.loads(path.read_text())
        body["projects"] = ["lodus-casino", "factory-bug-lab"]
        path.write_text(json.dumps(body))

        result = self.submit()
        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "CAPABILITY_SCOPE_INVALID")
        self.assertEqual(self.missions(), [])

    def test_the_product_project_is_registered_with_its_declared_gates(self):
        self.ready()
        self.submit()
        policy = self.lifecycle.store.project("lodus-casino")
        self.assertEqual(policy.acceptance_gate_ids,
                         CONTRACT.acceptance_gate_ids)
        self.assertEqual(policy.acceptance_gate_source,
                         CONTRACT.acceptance_gate_source)
        self.assertEqual(policy.policy_version, CONTRACT.run_ref)

    def test_no_improvement_may_be_opened_over_a_product_first_build(self):
        """A second admitter would promote a candidate that does not exist."""

        self.ready()
        self.submit()
        supervisor_policy = self.lifecycle.supervisor.policy("lodus-casino")
        self.assertEqual(supervisor_policy.improvement_admissions, 0)
        self.assertEqual(supervisor_policy.work_classes, (CONTRACT.work_class,))


class ProductStatusTests(unittest.TestCase):
    """SF-158: whose blocker the Owner status surface is actually reporting.

    The live host reached exactly the shape reproduced here.  DF-1 escalated on
    acceptance gates that had exited 127 for want of a PATH; the Owner then
    submitted the real package and `./dev factory status --watch` answered
    *"The DF-1 validation mission needs Owner attention"* and stopped watching,
    while `lodus-casino:build` was admitted and eligible to advance.  Nothing
    in the ledger was wrong -- the status reading was looking at the wrong
    portfolio, because a product mission is in none.
    """

    setUp = FactoryProductTests.setUp
    ready = FactoryProductTests.ready
    submit = FactoryProductTests.submit
    missions = FactoryProductTests.missions

    def escalate_the_first_internal_slot(self):
        """Settle DF-1 the way the live host settled it: gates run, gates failed.

        Through the ordinary transitions rather than by editing rows, because
        an escalation is only reachable past the dispatch boundary and that is
        the half of the state that makes it Owner attention.
        """

        self.assertTrue(self.lifecycle.dispatch("run").ok)
        claimed = self.lifecycle.store.claim("test-escalation")
        mission_id, token = claimed["id"], claimed["lease_token"]
        for state in ("dispatched", "candidate_verified"):
            self.lifecycle.store.transition(
                mission_id, token, state, detail={"candidate_sha": "b" * 40})
        self.lifecycle.store.transition(
            mission_id, token, "escalated",
            reason="ACCEPTANCE_GATE_FAILED: dev-check, dev-test",
            release_lease=True)
        return mission_id

    def status(self):
        result = self.lifecycle.dispatch("status")
        self.assertTrue(result.ok, result.render())
        return result

    # -- precedence -------------------------------------------------------- #

    def test_without_a_product_the_internal_portfolio_still_owns_the_status(self):
        """The internal path is unchanged where there is no product to report."""

        self.ready()
        self.escalate_the_first_internal_slot()

        result = self.status()
        self.assertEqual(result.details["work_state"], "attention")
        self.assertIn("DF-1 validation mission needs Owner attention",
                      result.render())
        self.assertNotIn("lodus-casino:build", result.render())

    def test_an_admitted_product_mission_takes_the_status_headline(self):
        self.ready()
        self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)

        result = self.status()
        self.assertIn("Product: lodus-casino:build in lodus-casino is queued",
                      result.render())
        self.assertNotIn("The DF-1 validation mission needs Owner attention",
                         result.render())

    def test_the_historical_internal_attention_survives_as_history(self):
        """Demoted, not erased: the escalation is still there to be read."""

        self.ready()
        escalated = self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)

        row = self.lifecycle.store.get(escalated)
        self.assertEqual(row["state"], "escalated")
        self.assertEqual(row["terminal_reason"],
                         "ACCEPTANCE_GATE_FAILED: dev-check, dev-test")
        rendered = self.status().render()
        self.assertIn("History: the DF-1 internal validation mission is still "
                      "marked for Owner review", rendered)
        self.assertIn("does not block this product", rendered)

    def test_admitted_product_work_is_not_reported_as_paused(self):
        """The false claim itself: eligible work described as waiting on a person."""

        self.ready()
        self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)

        result = self.status()
        self.assertEqual(result.details["work_state"], "pending")
        self.assertNotIn("paused", result.render())
        # Eligible is a durable property, not a phrasing: the scheduler can
        # still claim the mission the status surface just described.
        claimed = self.lifecycle.store.claim(
            "test-eligibility", project_ids=("lodus-casino",))
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["payload"]["work_item_id"], "lodus-casino:build")

    def test_watch_keeps_watching_while_the_product_mission_advances(self):
        """`--watch` stops on attention; a live product mission is not one."""

        self.ready()
        self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)
        emitted, slept = [], []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                raise KeyboardInterrupt

        self.assertEqual(
            self.lifecycle.watch(1, emit=emitted.append, sleep=sleep), 0)
        self.assertEqual(len(emitted), 2)
        self.assertIn("Product: lodus-casino:build", emitted[0])

    # -- what the state actually is ---------------------------------------- #

    def test_the_stage_and_provider_are_reported_once_they_are_durable(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        claimed = self.lifecycle.store.claim(
            "test-stage", project_ids=("lodus-casino",))
        self.lifecycle.store.begin_step(
            claimed["id"], claimed["lease_token"], "dispatch", {"a": 1})
        self.lifecycle.store.record_run(
            claimed["id"], 1, {"reason": "PRIMARY", "considered": []},
            {"provider_profile": "codex-product", "classification": "completed",
             "process_started": True},
            claimed["idempotency_key"])

        rendered = self.status().render()
        self.assertIn("lodus-casino:build in lodus-casino is executing", rendered)
        self.assertIn("Stage: dispatch (in progress)", rendered)
        self.assertIn("Provider: Codex (codex-product)", rendered)

    def test_a_freshly_updated_step_reads_as_healthy_not_stale(self):
        """A step updated moments ago is a healthy in-progress leg."""

        self.ready()
        self.assertTrue(self.submit().ok)
        claimed = self.lifecycle.store.claim(
            "test-fresh", project_ids=("lodus-casino",))
        self.lifecycle.store.begin_step(
            claimed["id"], claimed["lease_token"], "dispatch", {"a": 1})

        started_at = time.time()
        self.lifecycle.clock = lambda: started_at + 5
        rendered = self.status().render()
        self.assertIn("Stage: dispatch (in progress) — updated 5s ago", rendered)
        self.assertNotIn("stale", rendered)

    def test_a_step_with_no_recent_update_reads_as_stale_attention(self):
        """A step whose durable record has not moved in a while is stale."""

        self.ready()
        self.assertTrue(self.submit().ok)
        claimed = self.lifecycle.store.claim(
            "test-stale", project_ids=("lodus-casino",))
        self.lifecycle.store.begin_step(
            claimed["id"], claimed["lease_token"], "dispatch", {"a": 1})

        started_at = time.time()
        self.lifecycle.clock = lambda: started_at + 920
        rendered = self.status().render()
        self.assertIn(
            "Stage: dispatch (in progress) — stale, no update in 15m", rendered)

    def test_a_stopped_product_mission_is_the_blocker_it_actually_is(self):
        self.ready()
        self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)
        claimed = self.lifecycle.store.claim(
            "test-stop", project_ids=("lodus-casino",))
        self.lifecycle.store.transition(
            claimed["id"], claimed["lease_token"], "refused",
            reason="PROVIDER_POLICY_VIOLATION: the profile is denied",
            release_lease=True)

        result = self.status()
        self.assertEqual(result.details["work_state"], "attention")
        self.assertIn("Attention: lodus-casino:build needs Owner review "
                      "(PROVIDER_POLICY_VIOLATION: the profile is denied).",
                      result.render())

    def test_a_settled_product_mission_reports_that_it_succeeded(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        claimed = self.lifecycle.store.claim(
            "test-settle", project_ids=("lodus-casino",))
        mission_id, token = claimed["id"], claimed["lease_token"]
        for state in ("dispatched", "candidate_verified", "evaluated",
                      "evidence_sealed", "completed"):
            self.lifecycle.store.transition(mission_id, token, state)

        result = self.status()
        self.assertEqual(result.details["work_state"], "complete")
        self.assertIn("lodus-casino:build in lodus-casino is settled; "
                      "it succeeded", result.render())

    def test_the_status_surface_never_mutates_the_mission_it_reports(self):
        """Status is a reading.  The live run's own safety depends on it."""

        self.ready()
        self.escalate_the_first_internal_slot()
        self.assertTrue(self.submit().ok)
        before = [dict(row) for row in self.missions()]

        for _ in range(3):
            self.status()

        self.assertEqual([dict(row) for row in self.missions()], before)


class ProductReviewTests(unittest.TestCase):
    """SF-158: what a finished product mission becomes.

    The execution layer could already mint an immutable artifact identity from
    a candidate commit, and `release.py` could already seal a Release Candidate
    and record a review deployment.  Nothing carried one to the other, so the
    terminal state of a successful product run was a commit on a lane reference
    plus an operator expected to hand-author a release bundle.
    """

    setUp = FactoryProductTests.setUp
    ready = FactoryProductTests.ready
    submit = FactoryProductTests.submit
    missions = FactoryProductTests.missions

    CANDIDATE = "7" * 40

    def finish_the_product_mission(self, changed_paths=("public/index.html",)):
        """Settle the product mission the way the engine settles one.

        Through the ordinary transitions with the ordinary result shape, so the
        review path reads the fields the engine actually writes rather than a
        shape invented for a test.
        """

        claimed = self.lifecycle.store.claim(
            "test-product", project_ids=("lodus-casino",))
        mission_id, token = claimed["id"], claimed["lease_token"]
        for state in ("dispatched", "candidate_verified", "evaluated",
                      "evidence_sealed"):
            self.lifecycle.store.transition(mission_id, token, state)
        self.lifecycle.store.transition(
            mission_id, token, "completed", release_lease=True, result={
                "dispatch": {"candidate_sha": self.CANDIDATE},
                "verification": {"verification": {
                    "candidate_sha": self.CANDIDATE,
                    "baseline_sha": CONTRACT.baseline_sha,
                    "verification_digest": "d" * 64}},
                "evaluation": {
                    "passed": True,
                    "changed_paths": (None if changed_paths is None
                                      else list(changed_paths)),
                    "gate_outcomes": [
                        {"gate_id": gate, "passed": True, "exit_code": 0}
                        for gate in CONTRACT.acceptance_gate_ids]},
                "evidence": {"accepted": True, "evidence_pointer": "e" * 64}})
        return mission_id

    def review(self):
        return self.lifecycle.dispatch("review")

    def candidates(self):
        # The lifecycle owns the schema, so asking it is what makes "no
        # release candidates" different from "no table yet".
        release.ReleaseLifecycle(self.lifecycle.store)
        with self.lifecycle.store.transaction() as db:
            return [dict(row) for row in
                    db.execute("SELECT * FROM release_candidates")]

    # -- refusals, and what they leave behind ----------------------------- #

    def test_a_review_before_any_product_mission_refuses(self):
        self.ready()
        result = self.review()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_MISSION_ABSENT")

    def test_an_unfinished_product_mission_seals_nothing(self):
        self.ready()
        self.assertTrue(self.submit().ok)

        result = self.review()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "PRODUCT_MISSION_UNFINISHED")
        self.assertEqual(self.candidates(), [])

    def test_a_candidate_that_rewrote_its_own_gate_source_is_refused(self):
        """The one QA question durable state can answer, and it must bite.

        The gates run from the candidate's own checkout, so a provider that
        relaxed the script it is judged by would pass gates that no longer mean
        what the contract declared -- with every other record still green.
        """

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission(
            changed_paths=("public/index.html", "dev"))

        result = self.review()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "DECISION_BOUNDARY_VIOLATED")
        self.assertEqual(self.candidates(), [])
        refused = [row for row in self.lifecycle.store.coordination(None, limit=50)
                   if row.get("reason") == "FACTORY_PRODUCT_QA_REFUSED"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["detail"]["violations"], ["dev"])

    def test_an_unrecorded_change_set_cannot_certify_the_boundary(self):
        """Absent is not clean: an unanswerable question is not a pass."""

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission(changed_paths=None)

        result = self.review()

        self.assertFalse(result.ok)
        self.assertEqual(result.details["code"], "DECISION_BOUNDARY_VIOLATED")
        self.assertEqual(self.candidates(), [])

    # -- the review itself ------------------------------------------------ #

    def test_a_finished_mission_becomes_a_sealed_reviewable_candidate(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        mission_id = self.finish_the_product_mission()

        result = self.review()

        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.state, "review-ready")
        self.assertEqual(result.details["rc_id"],
                         "rc-lodus-casino-" + self.CANDIDATE[:12])
        self.assertEqual(result.details["candidate_sha"], self.CANDIDATE)
        self.assertEqual(result.details["decision_boundary"], "held")
        self.assertTrue(result.details["artifact_digest"].startswith("sha256:"))
        self.assertEqual(result.details["review_url"], self.config.review_url)

        rows = self.candidates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_sha"], self.CANDIDATE)
        self.assertEqual(rows[0]["artifact_digest"],
                         result.details["artifact_digest"])
        self.assertEqual(json.loads(rows[0]["qa_refs_json"]),
                         ["qa://%s/decision-boundary" % mission_id])

    def test_the_reviewed_bytes_are_the_sealed_artifacts_own(self):
        """Not the working copy and not a fresh checkout: the sealed archive.

        The package's own invariant is that the promoted digest equals the
        validated one, which is only true if the Owner reviews the bytes the
        identity was taken over.
        """

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()

        result = self.review()

        served = Path(result.details["review_root"])
        self.assertTrue((served / "index.html").is_file())
        self.assertIn(self.CANDIDATE, (served / "index.html").read_text())

    def test_an_archive_that_escapes_itself_is_refused(self):
        """Planted, because a guard nobody has seen fire is not a guard.

        The archive is the Factory's own canonical repack, which is exactly why
        this is checked rather than trusted: the check is what makes that a
        property instead of an assumption.
        """

        import io
        import tarfile

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        hostile = Path(self.temp.name) / "hostile.tar"
        with tarfile.open(hostile, "w") as archive:
            body = b"x"
            info = tarfile.TarInfo("public/../../escaped")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        report = {"archive_path": str(hostile), "publish_prefix": "public/"}

        with self.assertRaises(Exception) as caught:
            self.lifecycle._materialize_review(report, "sha256:" + "a" * 64)
        self.assertEqual(caught.exception.code, "REVIEW_ARTIFACT_UNSAFE")

    def test_the_review_environment_is_registered_by_provisioning(self):
        """The environment the contract names, which nothing used to create."""

        self.ready()
        self.assertTrue(self.submit().ok)
        policy = self.lifecycle.production.environment(
            CONTRACT.review_environment_id)

        self.assertEqual(policy.environment_class, "staging")
        self.assertEqual(policy.project_id, "lodus-casino")
        self.assertEqual(policy.state, "enabled")
        self.assertTrue(policy.autonomous)

    def test_the_production_environment_is_deliberately_not_registered(self):
        """It is gated and unreachable until an Owner Validation exists."""

        self.ready()
        self.assertTrue(self.submit().ok)
        with self.assertRaises(Exception):
            self.lifecycle.production.environment(
                CONTRACT.production_environment_id)

    def test_reviewing_twice_seals_one_candidate_and_deploys_once(self):
        """With a clock that moves between them, which is what caught this.

        `bundle_digest` is taken over every bundle field, the RC id is derived
        from the candidate rather than the clock, and `seal` refuses one id
        offered different bytes.  So a provenance timestamp read from "now"
        made the second review of one candidate a hard refusal -- invisible to
        a test whose two calls landed in the same second, and immediate on a
        host.
        """

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        ticks = itertools.count(1_800_000_000.0, 9_999.0)
        self.lifecycle.clock = lambda: next(ticks)

        first = self.review()
        second = self.review()

        self.assertTrue(second.ok, second.render())
        self.assertEqual(first.details["rc_id"], second.details["rc_id"])
        self.assertEqual(first.details["artifact_digest"],
                         second.details["artifact_digest"])
        self.assertEqual(first.details["deployment_ref"],
                         second.details["deployment_ref"])
        self.assertEqual(len(self.candidates()), 1)
        with self.lifecycle.store.transaction() as db:
            deployments = db.execute("SELECT COUNT(*) n FROM deployments"
                                     " WHERE project_id='lodus-casino'").fetchone()["n"]
        self.assertEqual(deployments, 1)

    def test_a_candidate_already_sealed_under_other_bytes_is_reused(self):
        """The live host's own state, reproduced.

        A Release Candidate is immutable, so an RC sealed from a bundle whose
        fields have since changed shape can never be re-sealed. Recognising it
        by commit and artifact is what keeps a second review from being a
        permanent refusal; a different artifact still reaches `seal`.
        """

        self.ready()
        self.assertTrue(self.submit().ok)
        mission_id = self.finish_the_product_mission()
        # Seal first, from a bundle that differs only in when it says it was
        # built -- exactly the drift that produced the refusal.
        first = self.review()
        rc_id = first.details["rc_id"]
        lifecycle = release.ReleaseLifecycle(self.lifecycle.store)
        stale = production.ReleaseBundle.from_payload(product.release_bundle(
            CONTRACT, work_item_id="lodus-casino:build", mission_id=mission_id,
            repository="https://github.com/linc-abela/lodus-casino.git",
            candidate_sha=self.CANDIDATE,
            artifact={"kind": "static-bundle",
                      "identity": first.details["artifact_digest"]},
            evidence_pointer="e" * 64, provenance_at="1999-01-01T00:00:00Z"))
        self.assertNotEqual(stale.bundle_digest,
                            first.details["bundle_digest"])
        with self.assertRaises(release.ReleaseRefusal) as refused:
            lifecycle.seal(rc_id, stale, verification_refs=["v"], qa_refs=["q"])
        self.assertEqual(refused.exception.code, "RC_IDENTITY_MISMATCH")

        again = self.review()

        self.assertTrue(again.ok, again.render())
        self.assertEqual(again.details["rc_id"], rc_id)
        self.assertEqual(len(self.candidates()), 1)

    def test_a_different_artifact_for_one_candidate_is_still_refused(self):
        """The narrowing must not become a way past the immutability rule."""

        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        first = self.review()
        lifecycle = release.ReleaseLifecycle(self.lifecycle.store)

        self.assertIsNone(self.lifecycle._already_sealed(
            lifecycle, first.details["rc_id"], self.CANDIDATE,
            sealed_digest="sha256:" + "b" * 64))

    def test_the_review_is_recorded_as_an_owner_act(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        result = self.review()

        acts = [row for row in self.lifecycle.store.coordination(None, limit=100)
                if row.get("reason") == "FACTORY_OWNER_ACTION"
                and row["detail"].get("action") == "review"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["detail"]["rc_id"], result.details["rc_id"])

    def test_the_review_surface_is_a_local_target_the_contract_accepts(self):
        """`release._surface` takes HTTPS or explicit loopback and nothing else."""

        from factory_controller import release as release_mod

        self.assertEqual(release_mod._surface(self.config.review_url),
                         self.config.review_url)

    def test_status_names_the_review_verb_before_it_has_been_run(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()

        rendered = self.lifecycle.dispatch("status").render()
        self.assertIn("is settled; it succeeded", rendered)
        self.assertIn("Next: run './dev factory review'", rendered)

    def test_status_names_the_sealed_review_and_its_surface(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        self.finish_the_product_mission()
        result = self.review()

        rendered = self.lifecycle.dispatch("status").render()
        self.assertIn("Review: %s is ready at %s"
                      % (result.details["rc_id"], self.config.review_url),
                      rendered)
        self.assertIn(result.details["artifact_digest"], rendered)
        self.assertNotIn("Next: run './dev factory review'", rendered)

    def test_an_unfinished_mission_is_not_offered_for_review(self):
        self.ready()
        self.assertTrue(self.submit().ok)

        rendered = self.lifecycle.dispatch("status").render()
        self.assertNotIn("factory review", rendered)
        self.assertNotIn("Review:", rendered)

    def test_a_review_never_touches_the_mission_it_releases(self):
        self.ready()
        self.assertTrue(self.submit().ok)
        mission_id = self.finish_the_product_mission()
        before = dict(self.lifecycle.store.get(mission_id))

        self.review()

        self.assertEqual(dict(self.lifecycle.store.get(mission_id)), before)


if __name__ == "__main__":
    unittest.main()
