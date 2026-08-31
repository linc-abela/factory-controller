"""Focused coverage for the one-step dogfood intake, `./dev factory run`.

The command's whole promise is that the Owner names nothing, so most of what is
checked here is what the Controller *derived*: the mission's identity, the live
admission document, the command behind each declared acceptance gate, and the
refusals that fire before anything is dispatched.
"""

from __future__ import annotations

import io
import json
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from factory_controller import dogfood_intake, routing, shift as shift_plane
from factory_controller.context import sha256_hex

from tests.support import LayerAdapter, RouteTestCase, mission_payload
from tests.test_factory_lifecycle import PROTOTYPE_SHA, FactoryLifecycleTests


PORTFOLIO = "contracts/first-dogfood-mission-portfolio.json"


def _self_hash(body: dict, field: str) -> str:
    return sha256_hex({name: value for name, value in body.items()
                       if name != field})


class FactoryRunTests(unittest.TestCase):
    """Reuses the lifecycle harness: the same fake host, one more verb."""

    setUp = FactoryLifecycleTests.setUp

    def ready(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        started = self.lifecycle.dispatch("start")
        self.assertTrue(started.ok, started.render())

    def missions(self):
        store = self.lifecycle.store
        return [store.get(row["id"]) for row in store.all_missions()]

    # -- refusals -------------------------------------------------------- #

    def test_run_from_off_refuses_once_and_submits_nothing(self):
        result = self.lifecycle.dispatch("run")

        self.assertFalse(result.ok)
        self.assertEqual(result.render().splitlines(),
                         ["BLOCKED: The Factory is not running. "
                          "Run './dev factory start' first."])
        self.assertEqual(self.missions(), [])

    def test_run_fails_closed_without_containment(self):
        self.ready()
        self.host.containment = False

        result = self.lifecycle.dispatch("run")

        self.assertFalse(result.ok)
        self.assertIn("containment", result.render().lower())
        self.assertEqual(self.missions(), [])

    # -- the intake itself ------------------------------------------------ #

    def test_first_run_submits_exactly_the_first_frozen_mission(self):
        self.ready()

        result = self.lifecycle.dispatch("run")

        self.assertTrue(result.ok, result.render())
        self.assertEqual(result.render().splitlines()[0], "DOGFOOD MISSION QUEUED")
        self.assertNotIn("fm_", result.render())
        missions = self.missions()
        self.assertEqual(len(missions), 1)
        payload = missions[0]["payload"]
        self.assertEqual(payload["work_item_id"], "DF-1")
        self.assertEqual(payload["project_id"], "factory-prototype-lab")
        self.assertEqual(payload["baseline_sha"], PROTOTYPE_SHA)
        self.assertEqual(payload["execution_mode"], "real")
        self.assertEqual(payload["capability"], "prototype")
        self.assertEqual([row["profile"] for row in payload["provider_candidates"]],
                         ["codex-primary"])
        self.assertEqual(
            missions[0]["idempotency_key"],
            routing.expected_idempotency_key(
                "DF-1", payload["context_manifest_hash"]))

    def test_the_mission_reaches_the_real_execution_seam_not_the_fixture(self):
        self.ready()
        self.assertTrue(self.lifecycle.dispatch("run").ok)

        stage1 = self.missions()[0]["payload"]["stage1"]

        self.assertEqual(stage1["mode"], "real")
        self.assertIs(stage1["operator_opt_in"], True)
        self.assertEqual(stage1["command"][1:],
                         ["-m", "src.cli.first_live"])
        self.assertEqual(stage1["repository"], "/labs/factory-prototype-lab")
        self.assertEqual(stage1["gate_commands"], {
            "dev-check": ["/labs/factory-prototype-lab/dev", "check"],
            "dev-test": ["/labs/factory-prototype-lab/dev", "test"]})

    def test_the_supervisor_service_dispatches_through_the_same_seam(self):
        """The fixture provider refuses a real mission, so naming it here
        would have made every dogfood mission terminal on its first leg."""

        self.ready()

        invocation = " ".join(self.lifecycle._service_plan().invocation)

        self.assertIn("factory_controller.stage1_adapter", invocation)
        self.assertNotIn("safe_provider", invocation)

    def test_run_reloads_a_supervisor_started_before_this_command(self):
        """A stale definition on disk is not the one launchd is holding."""

        self.ready()
        plan = self.lifecycle._service_plan()
        Path(plan.receipt_path).write_text(json.dumps({"plan_digest": "stale"}))
        booted = len([1 for command, _ in self.host.calls
                      if command[:2] == ("launchctl", "bootstrap")])

        self.assertTrue(self.lifecycle.dispatch("run").ok)

        reloaded = [command for command, _ in self.host.calls
                    if command[:2] == ("launchctl", "bootout")
                    and command[2].endswith(self.config.supervisor_label)]
        self.assertTrue(reloaded)
        self.assertGreater(len([1 for command, _ in self.host.calls
                                if command[:2] == ("launchctl", "bootstrap")]),
                           booted)
        self.assertIn(self.config.supervisor_label, self.host.loaded)

    def test_the_admission_document_is_self_consistent_and_live(self):
        self.ready()
        self.assertTrue(self.lifecycle.dispatch("run").ok)

        path = self.config.mission_dir / "df-1-admission.json"
        body = json.loads(path.read_text())
        request = body["request"]
        evidence = body["admission_evidence"]
        manifest = evidence["context_manifest"]

        self.assertEqual(request["idempotency_key"],
                         "DF-1:" + manifest["manifest_hash"])
        self.assertEqual(request["context_manifest_hash"],
                         manifest["manifest_hash"])
        self.assertEqual(manifest["manifest_hash"],
                         _self_hash(manifest, "manifest_hash"))
        self.assertEqual(evidence["trusted_dispatch"]["receipt_hash"],
                         _self_hash(evidence["trusted_dispatch"], "receipt_hash"))
        self.assertEqual(evidence["human_authority"]["assertion_hash"],
                         _self_hash(evidence["human_authority"], "assertion_hash"))
        # Anything weaker admits as a fixture, and the execution layer then
        # refuses to invoke the real transport at all.
        self.assertEqual(evidence["trusted_dispatch"]["authority_kind"],
                         "foundation_native_receipt")
        self.assertEqual(evidence["human_authority"]["authority_kind"],
                         "owner_ratification")
        self.assertEqual(evidence["project_registration"]["registry_hash"],
                         "d" * 64)
        self.assertEqual(evidence["admitted_baseline_sha"], PROTOTYPE_SHA)
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")

    # -- repetition ------------------------------------------------------- #

    def test_repeating_run_never_submits_a_second_mission(self):
        self.ready()
        first = self.lifecycle.dispatch("run")
        second = self.lifecycle.dispatch("run")
        third = self.lifecycle.dispatch("run")

        self.assertTrue(second.ok, second.render())
        self.assertTrue(third.ok, third.render())
        self.assertEqual(len(self.missions()), 1)
        self.assertEqual(first.details["mission_ref"], "DF-1")
        self.assertEqual(second.details["mission_ref"], "DF-1")
        self.assertEqual(second.render().splitlines()[0], "DOGFOOD MISSION QUEUED")

    def test_the_next_mission_waits_for_the_previous_one_to_settle(self):
        self.ready()
        self.assertTrue(self.lifecycle.dispatch("run").ok)
        mission_id = self.missions()[0]["id"]

        running = self.lifecycle.dispatch("run")
        self.assertEqual(len(self.missions()), 1)

        self.lifecycle.store.cancel(mission_id)
        settled = self.lifecycle.dispatch("run")

        self.assertEqual(running.details["mission_ref"], "DF-1")
        self.assertTrue(settled.ok, settled.render())
        self.assertEqual(settled.details["mission_ref"], "DF-2")
        self.assertEqual(len(self.missions()), 2)

    def test_status_reports_work_without_naming_an_internal_id(self):
        self.ready()
        before = self.lifecycle.dispatch("status")
        self.assertIn("Work: none started", before.render())

        self.assertTrue(self.lifecycle.dispatch("run").ok)
        after = self.lifecycle.dispatch("status")

        self.assertTrue(after.ok, after.render())
        self.assertIn("DF-1", after.render())
        self.assertIn("waiting to start", after.render())
        self.assertNotIn("fm_", after.render())
        self.assertEqual(len(self.missions()), 1)


class PortfolioOutcomeTests(unittest.TestCase):
    """A real mission's key is `ref:manifest`; the portfolio must still see it."""

    def test_a_real_missions_key_still_resolves_to_its_portfolio_mission(self):
        import tempfile
        from pathlib import Path

        from factory_controller.store import MissionStore

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = MissionStore(Path(temp.name) / "controller.db")
        plane = shift_plane.ShiftPlane(store)
        entry = shift_plane.load_portfolio(PORTFOLIO)
        store.submit({"work_item_id": "DF-1"}, "DF-1:" + "a" * 64)

        outcomes = plane.outcomes(entry)

        self.assertEqual(outcomes, {"DF-1": "admitted"})
        self.assertEqual(entry.next_mission(outcomes).mission_ref, "DF-1")


class IntakeRefusalTests(unittest.TestCase):
    """The three derivations that must refuse rather than guess."""

    def setUp(self):
        self.entry = shift_plane.load_portfolio(PORTFOLIO)
        self.registry = [
            {"project_id": "factory-prototype-lab",
             "repository_remote_url":
                 "https://github.com/linc-abela/factory-prototype-lab.git",
             "resolution": "resolved", "capabilities": ["prototype"],
             "checkout": "/labs/factory-prototype-lab"},
            {"project_id": "factory-bug-lab",
             "repository_remote_url":
                 "https://github.com/linc-abela/factory-bug-lab.git",
             "resolution": "resolved", "capabilities": ["bug"],
             "checkout": "/labs/factory-bug-lab"},
        ]

    def build(self, mission, **overrides):
        now = time.time()
        arguments = dict(
            portfolio_ref=self.entry.portfolio_ref, run_ref="run-1",
            registry=self.registry, registry_digest="d" * 64,
            provider_profiles=["primary"], corpus_identity="contract://x",
            owner="owner", approval_ref="approval-1", granted_at=now,
            expires_at=now + 60, now=now,
            stage1={"command": ["python", "-m", "runner"], "workdir": "."})
        arguments.update(overrides)
        return dogfood_intake.build(mission, **arguments)

    def mission(self, reference):
        return next(item for item in self.entry.missions
                    if item.mission_ref == reference)

    def test_a_mutating_mission_is_materialized_with_candidate_target(self):
        intake = self.build(self.mission("DF-3"))
        self.assertEqual(intake.mission_ref, "DF-3")
        self.assertTrue(intake.payload["stage1"]["mutates_repository"])

    def test_a_capability_the_evidence_layer_refuses_is_caught_first(self):
        self.registry[0] = {**self.registry[0], "capabilities": ["unknown_capability"]}
        with self.assertRaises(dogfood_intake.IntakeError) as raised:
            self.build(self.mission("DF-1"))

        self.assertEqual(raised.exception.code, "CAPABILITY_NOT_ADMISSIBLE")

    def test_a_gate_command_is_never_invented(self):
        undeclared = replace(self.mission("DF-1"),
                             acceptance_gate_ids=("make-check",))

        with self.assertRaises(dogfood_intake.IntakeError) as raised:
            self.build(undeclared)

        self.assertEqual(raised.exception.code, "ACCEPTANCE_GATE_NOT_DERIVABLE")

    def test_a_project_with_no_local_copy_cannot_run_its_own_gates(self):
        self.registry[0] = {**self.registry[0], "checkout": ""}

        with self.assertRaises(dogfood_intake.IntakeError) as raised:
            self.build(self.mission("DF-1"))

        self.assertEqual(raised.exception.code, "PROJECT_CHECKOUT_UNAVAILABLE")

    def test_the_derived_identity_does_not_move_between_invocations(self):
        first = self.build(self.mission("DF-1"), now=1.0, granted_at=0.0,
                           expires_at=100.0)
        later = self.build(self.mission("DF-1"), now=99.0, granted_at=0.0,
                           expires_at=100.0)

        self.assertEqual(first.idempotency_key, later.idempotency_key)
        self.assertEqual(first.payload, later.payload)


class AdapterSeamTests(unittest.TestCase):
    """One seam now serves both paths, so neither mission kind loses its own."""

    def answer(self, request):
        from factory_controller import stage1_adapter

        stream = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(request))), \
                redirect_stdout(stream):
            self.assertEqual(stage1_adapter.main(), 0)
        return json.loads(stream.getvalue())

    def test_a_mission_without_live_configuration_reaches_the_fixture(self):
        answer = self.answer({
            "step": "dispatch", "operation_key": "k",
            "input": {"mission": {"work_item_id": "W-1"},
                      "route": {"provider_profile": "primary"}}})

        self.assertEqual(answer["status"], "completed")
        self.assertEqual(answer["receipt"]["execution_mode"], "fixture")

    def test_the_fixture_still_refuses_a_real_mission_it_is_handed(self):
        answer = self.answer({
            "step": "dispatch", "operation_key": "k",
            "input": {"mission": {"work_item_id": "W-1"},
                      "route": {"provider_profile": "primary",
                                "execution_mode": "real"}}})

        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["diagnostic"],
                         "FIXTURE_PROVIDER_REFUSES_REAL_MISSION")


class RetryClassificationTests(unittest.TestCase):
    """The one rule that says whether a settled slot may be attempted again.

    The default is *final*.  Everything below is either the narrow allowlist
    that lets a slot out of that default, or one of the three ways back into it.
    """

    def test_an_infrastructure_refusal_before_the_boundary_is_retryable(self):
        """DF-1's own refusal, verbatim from the first live dispatch."""

        self.assertEqual(
            shift_plane.retry_classification(
                "refused", "EXECUTION_MODE_UNPROVEN: layer reported unknown", 1),
            "retryable_infrastructure")

    def test_every_seam_refusal_the_engine_can_raise_is_classified(self):
        for reason in ("EXECUTION_MODE_UNPROVEN: layer reported unknown",
                       "EXECUTION_MODE_MISMATCH: mission declares real, "
                       "layer reported fixture",
                       "IDEMPOTENCY_KEY_UNPROVEN: layer echoed no key",
                       "IDEMPOTENCY_KEY_DIVERGED: layer bound x, mission is y",
                       "NO_ADMISSIBLE_PROVIDER: considered 1 candidate(s)",
                       "PROVIDER_ROUTE_EXHAUSTED: considered 2 candidate(s)",
                       "CONTEXT_BROKER_UNAVAILABLE",
                       "RETRIES_EXHAUSTED"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    shift_plane.retry_classification("refused", reason, 1),
                    "retryable_infrastructure")

    def test_a_refusal_about_the_mission_itself_stays_settled(self):
        """A verdict on frozen inputs reaches the same verdict next time."""

        for reason in ("PROVIDER_POLICY_VIOLATION: beta is denied by this mission",
                       "INVALID_EXECUTION_MODE: 'sideways'",
                       "ACCEPTANCE_GATE_UNDECLARED",
                       "REAL_MISSION_CONTEXT_MANIFEST_MISSING",
                       "MISSION_BUDGET_EXHAUSTED: known spend 5 of ceiling 5",
                       "UNCERTAIN_DISPATCH_OUTCOME_UNRESOLVED",
                       "DISPATCHED_RESULT_UNRECOVERABLE: provider_unavailable",
                       ""):
            with self.subTest(reason=reason):
                self.assertEqual(
                    shift_plane.retry_classification("refused", reason, 1),
                    "deterministic_refusal")

    def test_an_infrastructure_refusal_that_may_have_run_stays_settled(self):
        """The engine refused a second dispatch; the portfolio may not reopen it."""

        for reason in ("PROVIDER_SWITCH_AFTER_SIDE_EFFECT: recovery changed "
                       "provider 'a' -> 'b'",
                       "PROVIDER_SWITCH_AFTER_UNCERTAIN_OUTCOME: alpha did not "
                       "prove no process started (UNKNOWABLE)"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    shift_plane.retry_classification("refused", reason, 1),
                    "side_effect_possible")

    def test_only_refused_is_eligible_at_all(self):
        infrastructure = "EXECUTION_MODE_UNPROVEN: layer reported unknown"
        for state in ("completed", "failed", "cancelled", "escalated"):
            with self.subTest(state=state):
                self.assertEqual(
                    shift_plane.retry_classification(state, infrastructure, 1),
                    "settled")
        for state in (None, "admitted", "dispatching", "dispatched"):
            with self.subTest(state=state):
                self.assertEqual(
                    shift_plane.retry_classification(state, "", 1),
                    "not_settled")

    def test_a_slot_stops_being_retryable_once_its_attempts_are_spent(self):
        reason = "EXECUTION_MODE_UNPROVEN: layer reported unknown"
        self.assertEqual(
            shift_plane.retry_classification(
                "refused", reason, shift_plane.MAX_SLOT_ATTEMPTS - 1),
            "retryable_infrastructure")
        self.assertEqual(
            shift_plane.retry_classification(
                "refused", reason, shift_plane.MAX_SLOT_ATTEMPTS),
            "attempts_exhausted")


class RefusalBoundaryTests(RouteTestCase, unittest.TestCase):
    """The safety property the retry rule rests on.

    ``engine.work_once`` writes ``refused`` only while the mission is still
    ``dispatching``.  Past that boundary the same non-retryable failure settles
    ``failed`` instead -- which the rule reads as ``settled`` and never retries.
    If that ever inverted a retry could duplicate a provider effect, so it is
    pinned here rather than assumed.
    """

    def test_a_refused_mission_never_recorded_a_candidate(self):
        controller, store, _ = self.build(LayerAdapter(verified=False))
        mission, _ = controller.submit(mission_payload(), "post-boundary")

        result = controller.work_once("w1")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["terminal_reason"], "ANCESTRY_FAILED")
        self.assertEqual(
            shift_plane.retry_classification(
                result["state"], result["terminal_reason"], 1),
            "settled")
        self.assertEqual(store.get(mission["id"])["state"], "failed")


class SlotReadingTests(unittest.TestCase):
    """A slot holds attempts; its state is the latest one, not an arbitrary one."""

    def plane(self):
        import tempfile
        from pathlib import Path

        from factory_controller.store import MissionStore

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = MissionStore(Path(temp.name) / "controller.db")
        return store, shift_plane.ShiftPlane(store), shift_plane.load_portfolio(PORTFOLIO)

    def settle(self, store, key, state, reason):
        mission, _ = store.submit({"work_item_id": key.split(":")[0]}, key)
        claimed = store.claim("w")
        store.transition(claimed["id"], claimed["lease_token"], state,
                         reason=reason, release_lease=True)
        return mission["id"]

    def test_the_latest_attempt_is_the_slots_state(self):
        store, plane, entry = self.plane()
        refusal = "EXECUTION_MODE_UNPROVEN: layer reported unknown"
        self.settle(store, "DF-1:" + "a" * 64, "refused", refusal)
        store.submit({"work_item_id": "DF-1"}, "DF-1:" + "b" * 64)

        reading = plane.slots(entry)["DF-1"]

        self.assertEqual(reading.attempts, 2)
        self.assertEqual(reading.state, "admitted")
        self.assertEqual(reading.retry_class, "not_settled")
        self.assertEqual(plane.outcomes(entry), {"DF-1": "admitted"})
        self.assertEqual(plane.retryable(entry), frozenset())

    def test_a_retryable_slot_is_offered_again_rather_than_the_next_one(self):
        store, plane, entry = self.plane()
        self.settle(store, "DF-1:" + "a" * 64, "refused",
                    "EXECUTION_MODE_UNPROVEN: layer reported unknown")

        outcomes, retryable = plane.outcomes(entry), plane.retryable(entry)

        # The ledger is unchanged: the attempt is settled and stays settled.
        self.assertEqual(outcomes, {"DF-1": "refused"})
        self.assertEqual(retryable, frozenset({"DF-1"}))
        self.assertEqual(entry.next_mission(outcomes).mission_ref, "DF-2")
        self.assertEqual(
            entry.next_mission(outcomes, retryable).mission_ref, "DF-1")
        self.assertFalse(entry.complete(outcomes, retryable))
        self.assertEqual(plane.slots(entry)["DF-1"].next_attempt, 2)

    def test_a_deterministic_refusal_lets_the_sequence_advance(self):
        store, plane, entry = self.plane()
        self.settle(store, "DF-1:" + "a" * 64, "refused",
                    "PROVIDER_POLICY_VIOLATION: codex-primary is denied")

        outcomes, retryable = plane.outcomes(entry), plane.retryable(entry)

        self.assertEqual(retryable, frozenset())
        self.assertEqual(
            entry.next_mission(outcomes, retryable).mission_ref, "DF-2")


class RetryRunTests(unittest.TestCase):
    """`./dev factory run` over a slot whose first attempt the layer refused."""

    setUp = FactoryLifecycleTests.setUp
    ready = FactoryRunTests.ready
    missions = FactoryRunTests.missions

    INFRASTRUCTURE = "EXECUTION_MODE_UNPROVEN: layer reported unknown"

    #: The ledger's own path from a claim to success.  A refusal is one step
    #: because that is exactly what makes it retryable: the mission stopped
    #: while it was still `dispatching`, before a candidate was recorded.
    TO_COMPLETED = ("dispatched", "candidate_verified", "evaluated",
                    "evidence_sealed", "completed")

    def settle(self, state, reason=None):
        """Settle the mission the Owner just started, the way the engine does."""

        store = self.lifecycle.store
        claimed = store.claim("supervisor")
        steps = self.TO_COMPLETED if state == "completed" else (state,)
        for index, step in enumerate(steps):
            store.transition(claimed["id"], claimed["lease_token"], step,
                             reason=reason,
                             release_lease=index == len(steps) - 1)
        return claimed["id"]

    def first_attempt(self, reason=None):
        self.ready()
        started = self.lifecycle.dispatch("run")
        self.assertTrue(started.ok, started.render())
        self.assertEqual(started.details["attempt"], 1)
        return started, self.settle("refused", reason or self.INFRASTRUCTURE)

    # -- the re-offer ----------------------------------------------------- #

    def test_an_infrastructure_refusal_is_re_offered_as_a_second_attempt(self):
        first, _ = self.first_attempt()

        retry = self.lifecycle.dispatch("run")

        self.assertTrue(retry.ok, retry.render())
        self.assertEqual(retry.details["mission_ref"], "DF-1")
        self.assertEqual(retry.details["attempt"], 2)
        self.assertTrue(retry.details["created"])
        self.assertIn("Retrying attempt 2", retry.render())
        self.assertEqual(len(self.missions()), 2)

    def test_the_retry_is_a_new_identity_bound_to_the_same_work_item(self):
        """A reused key would replay the stored refusal forever."""

        self.first_attempt()
        self.lifecycle.dispatch("run")

        keys = [row["idempotency_key"] for row in self.missions()]
        self.assertEqual(len(set(keys)), 2)
        for key in keys:
            work_item, _, manifest = key.partition(":")
            # The shape `routing.expected_idempotency_key` mandates and the
            # evidence layer refuses anything else for.
            self.assertEqual(work_item, "DF-1")
            self.assertEqual(len(manifest), 64)
        payloads = [row["payload"] for row in self.missions()]
        self.assertEqual(sorted(item["attempt"] for item in payloads), [1, 2])
        self.assertEqual({item["work_item_id"] for item in payloads}, {"DF-1"})

    def test_the_refused_attempt_and_its_admission_file_are_untouched(self):
        first, mission_id = self.first_attempt()
        before = dict(self.lifecycle.store.get(mission_id))
        original = self.lifecycle.config.mission_dir / "df-1-admission.json"
        original_body = original.read_text()

        self.lifecycle.dispatch("run")

        after = dict(self.lifecycle.store.get(mission_id))
        self.assertEqual(after["state"], "refused")
        self.assertEqual(after["terminal_reason"], self.INFRASTRUCTURE)
        self.assertEqual(after["idempotency_key"], before["idempotency_key"])
        self.assertEqual(after["payload_hash"], before["payload_hash"])
        self.assertEqual(original.read_text(), original_body)
        retry_file = self.lifecycle.config.mission_dir / "df-1-attempt-2-admission.json"
        self.assertTrue(retry_file.exists())
        self.assertNotEqual(json.loads(retry_file.read_text()),
                            json.loads(original_body))

    def test_repeating_run_while_the_retry_is_pending_submits_nothing_more(self):
        self.first_attempt()
        retry = self.lifecycle.dispatch("run")

        again = self.lifecycle.dispatch("run")
        third = self.lifecycle.dispatch("run")

        self.assertTrue(again.ok, again.render())
        self.assertTrue(third.ok, third.render())
        self.assertEqual(len(self.missions()), 2)
        self.assertEqual(again.details["mission_ref"], "DF-1")
        self.assertEqual(retry.details["attempt"], 2)

    # -- the ways back to settled ----------------------------------------- #

    def test_a_deterministic_refusal_advances_the_portfolio(self):
        self.first_attempt("PROVIDER_POLICY_VIOLATION: codex-primary is denied")

        after = self.lifecycle.dispatch("run")

        # DF-2 is registered for capability bug which is lawfully admitted,
        # so the portfolio advances and DF-2 is queued.
        self.assertTrue(after.ok, after.render())
        self.assertEqual(after.details["mission_ref"], "DF-2")
        self.assertEqual(len(self.missions()), 2)

    def test_a_completed_retry_advances_the_portfolio(self):
        self.first_attempt()
        self.assertTrue(self.lifecycle.dispatch("run").ok)
        self.settle("completed", None)

        after = self.lifecycle.dispatch("run")

        self.assertTrue(after.ok, after.render())
        self.assertEqual(after.details["mission_ref"], "DF-2")
        self.assertEqual(len(self.missions()), 3)

    def test_a_slot_is_not_retried_past_its_bound(self):
        self.first_attempt()
        for attempt in range(2, shift_plane.MAX_SLOT_ATTEMPTS + 1):
            offered = self.lifecycle.dispatch("run")
            self.assertEqual(offered.details["attempt"], attempt, offered.render())
            self.settle("refused", self.INFRASTRUCTURE)

        after = self.lifecycle.dispatch("run")

        self.assertTrue(after.ok, after.render())
        self.assertEqual(after.details["mission_ref"], "DF-2")
        self.assertEqual(len(self.missions()), shift_plane.MAX_SLOT_ATTEMPTS + 1)

    # -- what the Owner is told ------------------------------------------- #

    def test_status_says_the_slot_can_be_retried_and_keeps_the_refusal(self):
        self.first_attempt()

        status = self.lifecycle.dispatch("status")

        self.assertTrue(status.ok, status.render())
        rendered = status.render()
        self.assertIn("DF-1", rendered)
        self.assertIn("can be retried", rendered)
        self.assertIn(self.INFRASTRUCTURE, rendered)
        self.assertIn("attempt 2 of %d" % shift_plane.MAX_SLOT_ATTEMPTS, rendered)
        self.assertNotIn("fm_", rendered)


class FactoryAutopilotTests(unittest.TestCase):
    """The installed Factory advances the frozen portfolio without babysitting."""

    setUp = FactoryLifecycleTests.setUp

    def ready(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        started = self.lifecycle.dispatch("start")
        self.assertTrue(started.ok, started.render())
        self.lifecycle.controller.adapter = LayerAdapter(mode="real")

    def missions(self):
        return [self.lifecycle.store.get(row["id"])
                for row in self.lifecycle.store.all_missions()]

    def test_service_invokes_the_factory_handoff_cycle(self):
        self.ready()

        invocation = self.lifecycle._service_plan().invocation

        self.assertEqual(invocation[-2:], ("factory", "cycle"))

    def test_one_owner_run_then_cycles_advances_every_frozen_slot(self):
        self.ready()
        started = self.lifecycle.dispatch("run")
        self.assertTrue(started.ok, started.render())

        expected = ["DF-1", "DF-2", "DF-3", "DF-4"]
        for _ in expected:
            advanced = self.lifecycle.dispatch("cycle")
            self.assertTrue(advanced.ok, advanced.render())

        missions = self.missions()
        self.assertEqual([mission["payload"]["work_item_id"] for mission in missions],
                         expected)
        self.assertEqual([mission["state"] for mission in missions],
                         ["completed", "escalated", "completed", "completed"])
        self.assertEqual(
            [row["detail"]["action"] for row in self.lifecycle.store.coordination()
             if row["reason"] == "FACTORY_OWNER_ACTION"],
            ["install", "start", "run"],
        )

    def test_a_retryable_refusal_is_retried_inside_the_bounded_handoff(self):
        self.ready()
        self.assertTrue(self.lifecycle.dispatch("run").ok)
        first_id = self.missions()[0]["id"]
        claimed = self.lifecycle.store.claim("test-refusal")
        self.lifecycle.store.transition(
            claimed["id"], claimed["lease_token"], "refused",
            reason="EXECUTION_MODE_UNPROVEN: layer reported unknown",
            release_lease=True,
        )

        advanced = self.lifecycle.dispatch("cycle")

        self.assertTrue(advanced.ok, advanced.render())
        missions = self.missions()
        self.assertEqual(len(missions), 3)
        self.assertEqual(self.lifecycle.store.get(first_id)["state"], "refused")
        self.assertEqual(missions[1]["payload"]["work_item_id"], "DF-1")
        self.assertEqual(missions[1]["state"], "completed")
        self.assertEqual(missions[2]["payload"]["work_item_id"], "DF-2")
        self.assertEqual(missions[2]["state"], "admitted")

    def test_unexpected_refusal_stops_progression_for_owner_attention(self):
        self.ready()
        self.assertTrue(self.lifecycle.dispatch("run").ok)
        claimed = self.lifecycle.store.claim("test-refusal")
        self.lifecycle.store.transition(
            claimed["id"], claimed["lease_token"], "refused",
            reason="PROVIDER_POLICY_VIOLATION: codex-primary is denied",
            release_lease=True,
        )

        attention = self.lifecycle.dispatch("cycle")

        self.assertFalse(attention.ok)
        self.assertEqual(attention.state, "attention")
        self.assertIn("Owner attention", attention.render())
        self.assertEqual(len(self.missions()), 1)

        status = self.lifecycle.dispatch("status")
        self.assertTrue(status.ok, status.render())
        self.assertEqual(status.details["work_state"], "attention")
        self.assertIn("Owner attention", status.render())

    def test_status_watch_ctrl_c_does_not_stop_factory_work(self):
        self.ready()
        output = []

        def interrupt(_):
            raise KeyboardInterrupt

        result = self.lifecycle.watch(5, emit=output.append, sleep=interrupt)

        self.assertEqual(result, 0)
        self.assertEqual(len(output), 1)
        self.assertEqual(self.lifecycle.supervisor.control()["state"], "running")
        self.assertIn(self.config.supervisor_label, self.host.loaded)
        self.assertEqual(self.missions(), [])


if __name__ == "__main__":
    unittest.main()
