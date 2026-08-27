"""Phase-1 capacity: the contract, the narrowing, and what it refuses to do.

The properties worth proving here are mostly *negative*, and each one is
checked against the mechanism rather than the docstring:

* capacity can shrink the eligible set and has no expressible way to grow it;
* an unknown quota fact never becomes a positive one;
* a closed window on one runtime does not stall another project;
* a mission whose window closes before anything ran is not lost;
* a mission whose window closes after something *may* have run is not handed
  to a second runtime.
"""

from __future__ import annotations

import unittest

from factory_controller import capacity, portfolio, routing, supervisor
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore

from tests.support import Clock, PortfolioTestCase


ALPHA, BETA, GAMMA = "runtime-alpha", "runtime-beta", "runtime-gamma"


def observation(runtime_id=ALPHA, state="available", observed_at=1_000_000.0, **extra):
    fields = {"source": "execution_layer", "source_ref": "probe-1"}
    fields.update(extra)
    return capacity.CapacityObservation(runtime_id=runtime_id, state=state,
                                        observed_at=observed_at, **fields)


class QuotaLayer:
    """An execution layer whose harnesses can decline for quota, provably.

    ``silent`` is the case the Controller must treat as unsafe: the layer
    declines without saying whether a process began.  It is separate from
    ``refusals`` because the whole capacity design turns on that distinction.
    """

    def __init__(self, refusals=None, silent=(), gates_pass=True) -> None:
        self.refusals = dict(refusals or {})
        self.silent = set(silent)
        self.gates_pass = gates_pass
        self.routes: list[dict] = []

    def execute(self, step, operation_key, value):
        if step == "dispatch":
            route = value["route"]
            profile = route["provider_profile"]
            self.routes.append(dict(route))
            receipt = {"provider_profile": profile, "provider": "layer",
                       "execution_mode": "fixture", "duration_ms": 1,
                       "idempotency_key": route["idempotency_key"]}
            if profile in self.silent:
                return {"status": "provider_unavailable", "diagnostic": "LAYER_SILENT",
                        "receipt": receipt}
            if profile in self.refusals:
                return {"status": "provider_unavailable",
                        "diagnostic": self.refusals[profile],
                        "receipt": {**receipt, "process_started": False,
                                    "refusal_code": self.refusals[profile]}}
            return {"status": "completed", "candidate_sha": "a" * 40,
                    "execution_id": operation_key,
                    "receipt": {**receipt, "process_started": True}}
        if step == "verify":
            return {"verified": True}
        if step == "evaluate":
            declared = value["mission"].get("acceptance_gate_ids") or ["G"]
            return {"passed": self.gates_pass,
                    "gate_outcomes": [{"gate_id": g, "passed": self.gates_pass}
                                      for g in declared]}
        if step == "evidence":
            return {"accepted": True, "evidence_pointer": "e" * 64}
        return {"status": "unknown"}


class ObservationContractTests(unittest.TestCase):
    def test_capacity_states_never_collide_with_the_absence_vocabulary(self):
        """Six forks of the absence words are already on record.  Not a seventh.

        "We measured this window and could not read a figure" is an
        observation; "nobody recorded anything" is an absence.  Keeping the two
        value spaces disjoint is what stops a reader from treating one as the
        other.
        """

        self.assertEqual(set(capacity.CAPACITY_STATES) & capacity.CANONICAL_ABSENCE, set())
        self.assertEqual(capacity.CANONICAL_ABSENCE,
                         {"unknown", "not_applicable", "not_run", "not_measurable"})

    def test_an_observation_without_provenance_is_refused(self):
        for missing in ("source", "source_ref"):
            with self.assertRaises(capacity.PolicyError):
                observation(**{missing: "  "})

    def test_a_remainder_the_source_could_not_measure_is_not_a_number(self):
        with self.assertRaises(capacity.PolicyError):
            observation(remaining_units=5, unit="requests", precision="unknown")
        with self.assertRaises(capacity.PolicyError):
            observation(remaining_units=5, precision="exact")
        with self.assertRaises(capacity.PolicyError):
            observation(unit="requests")
        self.assertEqual(observation(remaining_units=5, unit="requests",
                                     precision="exact").remaining_units, 5)

    def test_absent_figures_render_as_absence_words_and_never_as_zero(self):
        row = observation(state="cooling").as_row()
        self.assertEqual(row["remaining_units"], "not_measurable")
        self.assertEqual(row["unit"], "not_measurable")
        self.assertEqual(row["expected_reset_at"], "unknown")
        self.assertEqual(row["reset_state"], "reset_unknown")
        self.assertNotEqual(row["remaining_units"], 0)


class ReadingTests(unittest.TestCase):
    policy = capacity.RuntimePolicy(runtime_id=ALPHA, max_observation_age_seconds=100.0,
                                    unknown_reset_backoff_seconds=60.0)

    def test_an_unmeasured_managed_runtime_is_not_capacity(self):
        reading = capacity.read(self.policy, None, 1_000_000.0)
        self.assertFalse(reading.usable)
        self.assertEqual(reading.reason, "CAPACITY_OBSERVATION_MISSING")

    def test_a_stale_measurement_is_not_capacity_either(self):
        reading = capacity.read(self.policy, observation(), 1_000_200.0)
        self.assertFalse(reading.usable)
        self.assertEqual(reading.reason, "CAPACITY_OBSERVATION_STALE")
        self.assertEqual(reading.resume_at, 1_000_200.0 + 60.0)

    def test_an_unregistered_and_unmeasured_runtime_is_never_narrowed(self):
        """An undeclared constraint must not behave like a closed gate."""

        reading = capacity.read(None, None, 1_000_000.0)
        self.assertTrue(reading.usable)
        self.assertEqual(reading.reason, "CAPACITY_NOT_MANAGED")

    def test_a_measured_window_is_honoured_even_with_no_registration(self):
        """The other half, and the one that keeps the default safe.

        Ignoring a measurement because nobody filled in a registry would be the
        positive assumption the whole design forbids.
        """

        reading = capacity.read(None, observation(state="cooling"), 1_000_000.0)
        self.assertFalse(reading.usable)
        self.assertEqual(reading.reason, "RUNTIME_COOLING")

    def test_an_explicit_exemption_beats_a_measurement(self):
        exempt = capacity.RuntimePolicy(runtime_id=ALPHA, managed=False)
        reading = capacity.read(exempt, observation(state="exhausted"), 1_000_000.0)
        self.assertTrue(reading.usable)
        self.assertEqual(reading.reason, "CAPACITY_NOT_MANAGED")

    def test_every_state_has_a_reason_and_only_two_are_usable(self):
        usable = {state for state in capacity.CAPACITY_STATES
                  if capacity.read(self.policy, observation(state=state),
                                   1_000_000.0).usable}
        self.assertEqual(usable, {"available", "constrained"})

    def test_an_unknown_reset_becomes_a_bounded_hold_never_a_permanent_one(self):
        reading = capacity.read(self.policy, observation(state="cooling"), 1_000_000.0)
        self.assertEqual(reading.reset_state, "reset_unknown")
        self.assertEqual(reading.resume_at, 1_000_060.0)


class WorkFitTests(unittest.TestCase):
    policy = capacity.RuntimePolicy(runtime_id=ALPHA)

    def reading(self, **extra):
        return capacity.read(self.policy, observation(**extra), 1_000_000.0)

    def test_measured_work_larger_than_the_measured_remainder_is_refused(self):
        reading = self.reading(state="constrained", remaining_units=3, unit="requests",
                               precision="exact")
        ok, reason, _ = capacity.fit(reading, capacity.WorkEstimate(
            size_class="small", expected_units=9, unit="requests"))
        self.assertFalse(ok)
        self.assertEqual(reason, "CAPACITY_INSUFFICIENT_FOR_WORK")

    def test_two_units_that_do_not_match_are_not_converted(self):
        """Converting between a vendor's units would be modelling its accounting."""

        reading = self.reading(state="available", remaining_units=3, unit="requests",
                               precision="exact")
        ok, reason, _ = capacity.fit(reading, capacity.WorkEstimate(
            size_class="small", expected_units=9, unit="tokens"))
        self.assertTrue(ok)
        self.assertEqual(reason, "WORK_FIT_UNIT_MISMATCH")

    def test_large_and_unestimated_work_is_not_begun_on_a_closing_window(self):
        reading = self.reading(state="constrained")
        for size in ("large", "unknown"):
            ok, reason, _ = capacity.fit(reading, capacity.WorkEstimate(size_class=size))
            self.assertFalse(ok, size)
            self.assertEqual(reason, "WORK_SIZE_EXCEEDS_CONSTRAINED_WINDOW")

    def test_the_same_work_is_admitted_the_moment_a_window_reopens(self):
        """Fails safe without starving: the bound is one window, not forever."""

        ok, _, _ = capacity.fit(self.reading(state="available"),
                                capacity.WorkEstimate(size_class="large"))
        self.assertTrue(ok)


class PlanTests(unittest.TestCase):
    def readings(self, **states):
        policies = {name: capacity.RuntimePolicy(runtime_id=name) for name in states}
        observations = {name: observation(runtime_id=name, state=state)
                        for name, state in states.items()}
        return capacity.readings(policies, observations, 1_000_000.0)

    def test_a_plan_can_only_ever_name_runtimes_it_was_handed(self):
        """The structural half of "capacity cannot widen".

        There is no field on a plan that could carry a runtime the mission did
        not declare, so no configuration of observations can produce one.
        """

        readings = self.readings(**{ALPHA: "cooling", BETA: "available", GAMMA: "available"})
        plan = capacity.plan((ALPHA, BETA), readings)
        self.assertEqual(set(plan.admitted) | set(plan.denied), {ALPHA, BETA})
        self.assertNotIn(GAMMA, plan.admitted + plan.denied)

    def test_a_mission_declaring_no_runtime_has_no_capacity_subject(self):
        plan = capacity.plan((), self.readings(**{ALPHA: "cooling"}))
        self.assertEqual(plan.reason, "CAPACITY_NOT_APPLICABLE")
        self.assertFalse(plan.exhausted)

    def test_every_runtime_cooling_yields_the_earliest_reset(self):
        policies = {name: capacity.RuntimePolicy(runtime_id=name) for name in (ALPHA, BETA)}
        observations = {
            ALPHA: observation(runtime_id=ALPHA, state="cooling",
                               expected_reset_at=1_020_000.0),
            BETA: observation(runtime_id=BETA, state="exhausted",
                              expected_reset_at=1_010_000.0)}
        plan = capacity.plan((ALPHA, BETA),
                             capacity.readings(policies, observations, 1_000_000.0))
        self.assertTrue(plan.exhausted)
        self.assertEqual(plan.resume_at, 1_010_000.0)


class SchedulerNarrowingTests(PortfolioTestCase, unittest.TestCase):
    """Capacity as a scheduler verdict, which is where scopes 3, 8 and 12 land."""

    def setup(self, clock=None):
        clock = clock or Clock()
        controller, store, clock, path = self.portfolio_store(QuotaLayer(), clock=clock)
        for name in (ALPHA, BETA):
            store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=name))
        return controller, store, clock, path

    def test_a_cooling_runtime_holds_its_own_work_and_stalls_nobody_else(self):
        controller, store, clock, _ = self.setup()
        self.register(store, "project-a")
        self.register(store, "project-b")
        self.submit(controller, "A", project_id="project-a",
                    provider_candidates=[ALPHA])
        beta_mission = self.submit(controller, "B", project_id="project-b",
                                   provider_candidates=[BETA])
        store.observe_capacity(observation(runtime_id=ALPHA, state="cooling",
                                           observed_at=clock.now,
                                           expected_reset_at=clock.now + 18_000))
        store.observe_capacity(observation(runtime_id=BETA, state="available",
                                           observed_at=clock.now))
        preview = store.schedule_preview()
        self.assertEqual(preview["selected"], beta_mission)
        refused = [item for item in preview["considered"] if not item["admitted"]]
        self.assertEqual([item["reason"] for item in refused], ["CAPACITY_UNAVAILABLE"])
        self.assertEqual(refused[0]["detail"]["resume_at"], clock.now + 18_000)

    def test_the_held_mission_runs_unchanged_after_the_window_resets(self):
        controller, store, clock, _ = self.setup()
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        store.observe_capacity(observation(runtime_id=ALPHA, state="cooling",
                                           observed_at=clock.now,
                                           expected_reset_at=clock.now + 18_000))
        self.assertIsNone(controller.work_once("w1"))
        self.assertEqual(store.get(mission_id)["state"], "admitted")
        self.assertEqual(store.get(mission_id)["attempt_count"], 0)
        clock.advance(18_000)
        store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                           observed_at=clock.now))
        self.assertEqual(controller.work_once("w1")["state"], "completed")

    def test_capacity_is_the_last_thing_said_about_an_unregistered_project(self):
        """A window closing is transient; an unregistered project is not."""

        controller, store, clock, _ = self.setup()
        self.submit(controller, "A", project_id="nobody", provider_candidates=[ALPHA])
        store.observe_capacity(observation(runtime_id=ALPHA, state="cooling",
                                           observed_at=clock.now))
        verdicts = store.schedule_preview()["considered"]
        self.assertEqual(verdicts[0]["reason"], "PROJECT_UNREGISTERED")

    def test_a_pre_capacity_database_narrows_nothing(self):
        controller, store, clock, _ = self.portfolio_store(QuotaLayer())
        self.register(store, "project-a")
        self.submit(controller, "A", project_id="project-a", provider_candidates=[ALPHA])
        self.assertEqual(controller.work_once("w1")["state"], "completed")

    def test_capacity_never_reopens_a_window_a_newer_reading_closed(self):
        """A late-arriving older observation is the direction that invents capacity."""

        _, store, clock, _ = self.setup()
        store.observe_capacity(observation(runtime_id=ALPHA, state="cooling",
                                           observed_at=clock.now))
        store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                           observed_at=clock.now - 500, source_ref="late"))
        self.assertFalse(store.capacity_readings()[ALPHA].usable)


class MidDispatchExhaustionTests(PortfolioTestCase, unittest.TestCase):
    """Scope 6 and the acceptance criterion a quota refusal must not lose work."""

    def build(self, layer, **policy):
        controller, store, clock, path = self.portfolio_store(layer)
        self.register(store, "project-a", **policy)
        return controller, store, clock, path

    def test_a_proven_quota_refusal_defers_rather_than_refusing_the_mission(self):
        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted", BETA: "rate_limited"})
        controller, store, clock, _ = self.build(layer)
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA, BETA])
        mission = controller.work_once("w1")
        self.assertEqual(mission["state"], "admitted")
        self.assertEqual(mission["deferrals"], 1)
        self.assertGreater(mission["next_run_at"], clock.now)
        kinds = [event["kind"] for event in store.history(mission_id)]
        self.assertIn("CAPACITY_CHECKPOINT", kinds)
        self.assertIn("CAPACITY_DEFERRED", kinds)

    def test_the_refusals_themselves_become_the_capacity_record(self):
        """No probe: the freshest statement about a window is the refusal."""

        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted", BETA: "rate_limited"})
        controller, store, _, _ = self.build(layer)
        self.submit(controller, "A", project_id="project-a",
                    provider_candidates=[ALPHA, BETA])
        controller.work_once("w1")
        states = {row["runtime_id"]: row["state"]
                  for row in store.capacity_observations()}
        self.assertEqual(states, {ALPHA: "exhausted", BETA: "cooling"})
        self.assertEqual({row["source"] for row in store.capacity_observations()},
                         {"provider_refusal"})

    def test_a_deferral_does_not_spend_an_attempt(self):
        """Five-hour windows would otherwise escalate a mission for waiting."""

        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted"})
        controller, store, clock, _ = self.build(layer)
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        for _ in range(6):
            controller.work_once("w1")
            clock.advance(10_000)
            store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                               observed_at=clock.now))
        mission = store.get(mission_id)
        self.assertNotEqual(mission["state"], "escalated")
        self.assertGreaterEqual(mission["deferrals"], 3)

    def test_a_failure_that_is_not_a_quota_fact_still_refuses(self):
        """A reset will not fix a mission whose provider is misconfigured."""

        layer = QuotaLayer(refusals={ALPHA: "PROFILE_UNAVAILABLE"})
        controller, store, _, _ = self.build(layer)
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        controller.work_once("w1")
        self.assertEqual(store.get(mission_id)["state"], "refused")

    def test_an_unproven_refusal_is_never_deferred(self):
        """The mission may be past the boundary, so it takes the uncertainty path."""

        layer = QuotaLayer(silent=[ALPHA])
        controller, store, _, _ = self.build(layer)
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA, BETA])
        controller.work_once("w1")
        mission = store.get(mission_id)
        self.assertEqual(mission["state"], "refused")
        self.assertEqual(mission["deferrals"], 0)
        self.assertIn("PROVIDER_SWITCH_AFTER_SIDE_EFFECT", mission["terminal_reason"])

    def test_the_store_refuses_a_deferral_past_the_boundary_itself(self):
        """The guard is in the store, so no second caller can skip it."""

        layer = QuotaLayer()
        controller, store, _, _ = self.build(layer)
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        controller.work_once("w1")
        with self.assertRaises(LeaseLostOrBoundary):
            store.defer(mission_id, "not-a-token", "CAPACITY_UNAVAILABLE", None)

    def test_a_cooling_window_never_substitutes_metered_capacity(self):
        """Scope 13, and it is structural rather than a policy switch.

        The declared gateway profile is a perfectly good candidate the selector
        would have reached.  Capacity stops the dispatch before selection, so a
        mission whose subscription runtimes are cooling waits for the window
        rather than spending money.
        """

        layer = QuotaLayer()
        controller, store, clock, _ = self.build(layer)
        store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=ALPHA))
        store.observe_capacity(observation(runtime_id=ALPHA, state="cooling",
                                           observed_at=clock.now))
        payload_extra = {
            "provider_candidates": [ALPHA],
            "gateway_profiles": [{"profile": "metered-1", "model_slug": "m/1",
                                  "capabilities": ["implement"]}],
            "gateway_policy": {"enabled": True, "allowed_model_slugs": ["m/1"]},
        }
        mission_id = self.submit(controller, "A", project_id="project-a", **payload_extra)
        # Claimed directly, because the scheduler would (correctly) not offer it.
        store.claim("w1", lease_seconds=5)
        controller.work_once("w1")
        self.assertEqual(layer.routes, [])
        self.assertEqual(store.get(mission_id)["state"], "admitted")


LeaseLostOrBoundary = (ValueError, RuntimeError)


class CheckpointTests(PortfolioTestCase, unittest.TestCase):
    def test_a_pre_dispatch_checkpoint_carries_every_declared_fact(self):
        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted", BETA: "quota_exhausted"})
        controller, store, clock, _ = self.portfolio_store(layer)
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA, BETA],
                                 baseline_sha="b" * 40,
                                 capacity_estimate={"size_class": "small"})
        controller.work_once("w1")
        facts = store.capacity_checkpoint(mission_id)
        self.assertEqual(set(facts), set(capacity.CHECKPOINT_FACTS))
        self.assertEqual(facts["safe_boundary"], "pre_dispatch")
        self.assertEqual(facts["uncertainty"]["irreversible_effect"], "none")
        self.assertEqual(facts["compatible_profiles"], [ALPHA, BETA])
        self.assertEqual(facts["baseline_sha"], "b" * 40)
        self.assertEqual(facts["next_safe_step"], "dispatch")
        self.assertEqual(facts["repository"], "repo://project-a")

    def test_an_uncertain_checkpoint_names_one_runtime_and_no_handoff(self):
        """The structural half of "no handoff duplicates an effect".

        There is no refusal to audit here: the list of runtimes another one
        could be chosen from is empty, because the only compatible runtime is
        the one that may already have run.
        """

        layer = QuotaLayer(silent=[ALPHA])
        controller, store, _, _ = self.portfolio_store(layer)
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA, BETA])
        controller.work_once("w1")
        facts = store.capacity_checkpoint(mission_id)
        self.assertEqual(facts["safe_boundary"], "post_dispatch_unreconciled")
        self.assertEqual(facts["uncertainty"]["irreversible_effect"], "unknown")
        self.assertEqual(facts["compatible_profiles"], [ALPHA])
        self.assertEqual(facts["resume_target"], "reconcile_uncertain_dispatch")
        self.assertIn("UNCERTAIN_DISPATCH_LEG", facts["unresolved_blockers"])

    def test_a_checkpoint_is_re_derived_and_never_a_second_copy(self):
        layer = QuotaLayer()
        controller, store, _, _ = self.portfolio_store(layer)
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        before = store.capacity_checkpoint(mission_id)
        controller.work_once("w1")
        after = store.capacity_checkpoint(mission_id)
        self.assertEqual(before["next_safe_step"], "dispatch")
        self.assertEqual(after["next_safe_step"], "not_applicable")
        self.assertEqual(after["mission_state"], "completed")

    def test_the_checkpoint_boundary_vocabulary_is_the_continuity_module_s(self):
        """One dialect across the seam, so a checkpoint lifts into a baton."""

        from factory_controller import continuity
        self.assertTrue(set(continuity.SAFE_BOUNDARIES)
                        <= set(capacity.CHECKPOINT_BOUNDARIES))


class CrashAndRestartTests(PortfolioTestCase, unittest.TestCase):
    def test_a_deferred_mission_survives_a_replacement_worker_process(self):
        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted"})
        controller, store, clock, path = self.portfolio_store(layer)
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        controller.work_once("w1")
        reopened = Controller(MissionStore(path, clock=clock),
                              QuotaLayer(), retry_policy=RetryPolicy(base_delay_seconds=0),
                              lease_seconds=0)
        clock.advance(20_000)
        reopened.store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                                    observed_at=clock.now))
        self.assertEqual(reopened.work_once("w2")["state"], "completed")
        self.assertEqual(reopened.store.get(mission_id)["deferrals"], 1)

    def test_repeated_deferral_and_resume_produces_exactly_one_dispatch(self):
        """The duplicate-irreversible-effect count for this whole path is zero."""

        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted"})
        controller, store, clock, _ = self.portfolio_store(layer)
        self.register(store, "project-a")
        mission_id = self.submit(controller, "A", project_id="project-a",
                                 provider_candidates=[ALPHA])
        for _ in range(4):
            controller.work_once("w1")
            clock.advance(60)
        layer.refusals.clear()
        clock.advance(20_000)
        store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                           observed_at=clock.now))
        controller.work_once("w1")
        controller.work_once("w1")
        started = [leg for leg in store.runs(mission_id)
                   if leg["process_started"] is True]
        self.assertEqual(len(started), 1)
        self.assertEqual(store.get(mission_id)["state"], "completed")


class OwnerBriefTests(PortfolioTestCase, unittest.TestCase):
    def test_the_brief_reads_durable_state_and_changes_nothing(self):
        layer = QuotaLayer(refusals={ALPHA: "quota_exhausted"})
        controller, store, clock, _ = self.portfolio_store(layer)
        self.register(store, "project-a")
        store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=ALPHA))
        store.set_runtime_policy(capacity.RuntimePolicy(runtime_id=BETA))
        store.observe_capacity(observation(runtime_id=BETA, state="available",
                                           observed_at=clock.now))
        store.observe_capacity(observation(runtime_id=ALPHA, state="available",
                                           observed_at=clock.now))
        self.submit(controller, "A", project_id="project-a", provider_candidates=[ALPHA])
        # The window closes under the mission: ALPHA is available at claim time
        # and refuses for quota at dispatch, which is what leaves a deferral and
        # an exhausted reading behind for the brief to report.
        controller.work_once("w1")
        watcher = supervisor.OperationsSupervisor(controller, clock=clock)
        before = store.counts()
        brief = watcher.capacity_brief()
        self.assertEqual(store.counts(), before)
        self.assertEqual(brief["usable_now"], [BETA])
        self.assertIn((ALPHA, "RUNTIME_QUOTA_EXHAUSTED"), brief["unusable"])
        self.assertEqual([row["mission_id"] for row in brief["resumable"]],
                         [store.all_missions()[0]["id"]])
        self.assertEqual(brief["resumable"][0]["safe_boundary"], "pre_dispatch")

    def test_the_brief_works_with_no_advisory_service_and_no_gateway(self):
        controller, store, clock, _ = self.portfolio_store(QuotaLayer())
        watcher = supervisor.OperationsSupervisor(controller, clock=clock)
        brief = watcher.capacity_brief()
        self.assertEqual(brief["usable_now"], [])
        self.assertEqual(brief["next_eligible"], "not_applicable")


class AuthorityTests(unittest.TestCase):
    def test_capacity_cannot_create_work_or_widen_a_permission(self):
        """The module has no verb that admits, approves, registers or promotes."""

        verbs = {name for name in dir(capacity) if not name.startswith("_")}
        for forbidden in ("admit", "approve", "promote", "submit", "register",
                          "deploy", "dispatch", "grant"):
            self.assertFalse([name for name in verbs if name.startswith(forbidden)],
                             forbidden)

    def test_capacity_narrowing_only_ever_grows_the_denied_list(self):
        """Applied through the Owner's own mechanism, which can only subtract."""

        policy = routing.ExecutionPolicy(denied_profiles=(GAMMA,))
        readings = {ALPHA: capacity.read(capacity.RuntimePolicy(runtime_id=ALPHA),
                                         observation(state="cooling"), 1_000_000.0)}
        plan = capacity.plan((ALPHA, BETA), readings)
        combined = policy.denied_profiles + plan.denied
        self.assertEqual(set(combined), {GAMMA, ALPHA})
        self.assertTrue(set(policy.denied_profiles) <= set(combined))

    def test_the_scheduler_verdict_can_only_refuse(self):
        candidate = portfolio.MissionCandidate(
            mission_id="m", project_id=None, state="admitted", created_at=0.0,
            ready_at=0.0, runtimes=(ALPHA,))
        readings = {ALPHA: capacity.read(capacity.RuntimePolicy(runtime_id=ALPHA),
                                         observation(state="cooling"), 1_000_000.0)}
        snapshot = portfolio.Snapshot(
            portfolio=portfolio.PortfolioPolicy(), projects={}, candidates=(candidate,),
            in_flight={}, portfolio_in_flight=0, now=1_000_000.0, capacity=readings)
        self.assertFalse(portfolio.evaluate(candidate, snapshot).admitted)
        self.assertTrue(portfolio.evaluate(
            candidate, portfolio.Snapshot(
                portfolio=portfolio.PortfolioPolicy(), projects={},
                candidates=(candidate,), in_flight={}, portfolio_in_flight=0,
                now=1_000_000.0)).admitted)

    def test_a_resume_is_never_held_by_a_closed_window(self):
        """Half-finished work has to finish; that rule is older than capacity."""

        candidate = portfolio.MissionCandidate(
            mission_id="m", project_id=None, state="dispatched", created_at=0.0,
            ready_at=0.0, runtimes=(ALPHA,))
        readings = {ALPHA: capacity.read(capacity.RuntimePolicy(runtime_id=ALPHA),
                                         observation(state="cooling"), 1_000_000.0)}
        verdict = portfolio.evaluate(candidate, portfolio.Snapshot(
            portfolio=portfolio.PortfolioPolicy(), projects={}, candidates=(candidate,),
            in_flight={}, portfolio_in_flight=0, now=1_000_000.0, capacity=readings))
        self.assertTrue(verdict.admitted)
        self.assertEqual(verdict.reason, "RESUME_AFTER_BOUNDARY")


if __name__ == "__main__":
    unittest.main()
