"""SF-163: recover one refused revision without changing its mission identity."""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from factory_controller import context, portfolio, product
from factory_controller.engine import Controller
from factory_controller.store import MissionStore
from tests.test_context_binding import BrokerAdapter, manifest_hash_for
from tests.support import ALPHA, ProcessDeath, mission_payload


REMOTE = "https://example.invalid/lodus-casino.git"
BASELINE = "b" * 40
PREDECESSOR = "a" * 40
REGISTERED = "/products/lodus-casino"
REVISION_CHECKOUT = "/Users/karlosabay/.factory-bridge/revisions/lodus-casino/" + BASELINE
REF = "refs/heads/factory/revision/v2"
CANDIDATE = "c" * 40


def old_revision_payload() -> dict:
    """The SF-162 admission shape: the registered checkout is still declared."""

    value = mission_payload(
        work_item_id="lodus-casino:revision:2",
        project_id="lodus-casino",
        repository_remote_url=REMOTE,
        baseline_sha=BASELINE,
        context_request={
            "corpus_identity": "package://lodus-casino@revision",
            "policy_identity": "SF-163",
            "required_anchors": ["MISSION.md"],
        },
        stage1={
            "repository": REGISTERED,
            "gate_workdir": REGISTERED,
            "gate_commands": {"G": [REGISTERED + "/dev", "check"]},
        },
    )
    return value


def revision_binding() -> dict:
    grounding = {
        "schema_version": context.REVISION_GROUNDING_SCHEMA,
        "kind": "revision",
        "source": "factory-bridge",
        "project_id": "lodus-casino",
        "repository_remote_url": REMOTE,
        "revision_sha": BASELINE,
        "predecessor_sha": PREDECESSOR,
        "revision_ref": REF,
        "checkout": REVISION_CHECKOUT,
    }
    stage1 = {
        "repository": REVISION_CHECKOUT,
        "gate_workdir": REVISION_CHECKOUT,
        "gate_commands": {"G": [REVISION_CHECKOUT + "/dev", "check"]},
        "revision_grounding": grounding,
    }
    return {
        "schema_version": context.REVISION_CONTEXT_BINDING_SCHEMA,
        "kind": "revision",
        "project_id": "lodus-casino",
        "repository_remote_url": REMOTE,
        "revision_sha": BASELINE,
        "predecessor_sha": PREDECESSOR,
        "revision_ref": REF,
        "checkout": REVISION_CHECKOUT,
        "grounding": grounding,
        "stage1": stage1,
    }


class RecoveryAdapter(BrokerAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recovery_repositories: list[str] = []

    def execute(self, step, operation_key, value):
        if step == "context":
            repository = value["mission"]["stage1"]["repository"]
            self.context_calls.append(dict(value["context_request"]))
            if repository == REGISTERED:
                return {"status": "refused", "refusal_code": "STALE_HEAD"}
            self.recovery_repositories.append(repository)
            return self.build(value["context_request"])
        return super().execute(step, operation_key, value)


class CapabilityAdapter(RecoveryAdapter):
    """The real SF-164 shape: the layer refuses dispatch before starting one.

    ``serves`` is the bridge's admitted posture.  While the capability is not
    served the layer answers exactly as ``factory-bridge`` did for
    ``lodus-casino:revision:2``: ``status: refused``, no candidate, and
    ``process_started: False`` -- a refusal it can only make because nothing
    ran.
    """

    def __init__(self, *, serves=False):
        super().__init__(mode="real")
        self.serves = serves
        self.dispatch_keys: list[str] = []

    def _dispatch(self, operation_key, value):
        self.dispatch_keys.append(operation_key)
        if self.serves:
            return super()._dispatch(operation_key, value)
        self.dispatches.append(dict(value["route"]))
        return {"status": "refused", "candidate_sha": None, "execution_id": None,
                "diagnostic": "UNSUPPORTED_CAPABILITY",
                "receipt": {"provider_profile": value["route"]["provider_profile"],
                            "provider": "factory-evidence-core/first-live",
                            "execution_mode": "unknown", "duration_ms": 226,
                            "process_started": False, "idempotency_key": None,
                            "refusal_code": "UNSUPPORTED_CAPABILITY",
                            "usage": {"cost_state": "unknown"}}}


class RevisionLifecycleTests(unittest.TestCase):
    """SF-162, SF-163 and SF-164 as the one revision they actually were.

    Each was found separately and fixed separately, and each time the next
    blocker was one command away.  This is that whole path in one test, so a
    change that reopens any of the three fails here rather than on the host.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "controller.db"
        self.store = MissionStore(self.path)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id="lodus-casino", repository=REMOTE))

    def payload(self):
        """The real SF-164 admission: mode ``real``, so every guard applies."""

        value = mission_payload(**{**old_revision_payload(),
                                   "execution_mode": "real",
                                   "acceptance_gate_ids": ["G"]})
        value["context_manifest_hash"] = manifest_hash_for(value)
        return value

    @staticmethod
    def key(value):
        """Derived, never chosen: the Bridge refuses any other value."""

        return "%s:%s" % (value["work_item_id"], value["context_manifest_hash"])

    def test_one_revision_survives_stale_head_then_an_unserved_capability(self):
        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))

        # SF-162 -- grounded against the registered checkout, which can never
        # be at the revision base.
        stale = controller.work_once("shift-13")
        self.assertEqual(stale["state"], "refused")
        self.assertIn("STALE_HEAD", stale["terminal_reason"])
        self.assertEqual(self.store.runs(mission["id"]), [])

        # SF-163 -- rebind the execution checkout, keep the admission identity.
        self.assertTrue(
            self.store.resume_pre_provider(mission["id"], revision_binding())["changed"])

        # SF-164 -- the layer refuses at dispatch, having started nothing.
        unserved = CapabilityAdapter(serves=False)
        refused = Controller(MissionStore(self.path), unserved).work_once("shift-14")
        self.assertEqual(refused["id"], mission["id"])
        self.assertEqual(refused["state"], "refused")
        # The layer's own refusal, not the mode symptom it used to be reported
        # as.  This assertion is the whole of the one-bug-at-a-time loop.
        self.assertEqual(refused["terminal_reason"], "UNSUPPORTED_CAPABILITY")
        self.assertNotIn("EXECUTION_MODE_UNPROVEN", refused["terminal_reason"])
        legs = self.store.runs(mission["id"])
        self.assertEqual([leg["process_started"] for leg in legs], [False])

        # The Owner's one lifecycle command, run again after the capability is
        # served.  Same mission, same binding, no second admission.
        resumed = self.store.resume_pre_provider(mission["id"], revision_binding())
        self.assertTrue(resumed["changed"])
        self.assertEqual(resumed["mission"]["id"], mission["id"])

        served = CapabilityAdapter(serves=True)
        completed = Controller(MissionStore(self.path), served).work_once("shift-15")

        self.assertEqual(completed["id"], mission["id"])
        self.assertEqual(completed["state"], "completed")
        # One provider leg per attempt, and only the served one ever started.
        legs = self.store.runs(mission["id"])
        self.assertEqual([leg["process_started"] for leg in legs], [False, True])
        self.assertEqual(len(served.dispatch_keys), 1)

        # Every refusal on the way is still on the record.
        steps = {row["name"]: row for row in self.store.step_records(mission["id"])}
        self.assertEqual(steps["context"]["output"]["refusal_code"], "STALE_HEAD")
        self.assertEqual(steps["dispatch"]["output"]["diagnostic"],
                         "UNSUPPORTED_CAPABILITY")
        self.assertEqual(steps["dispatch-recovery"]["status"], "COMPLETED")
        self.assertEqual(steps["dispatch-recovery"]["output"]["status"], "completed")
        # And the admission identity was never rewritten.
        self.assertEqual(self.store.get(mission["id"])["payload"]["stage1"]["repository"],
                         REGISTERED)

    def test_the_refused_dispatch_does_not_replay_and_does_not_double_dispatch(self):
        """The memo is why this needed a second identity, not a rerun."""

        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))
        controller.work_once("shift-13")
        self.store.resume_pre_provider(mission["id"], revision_binding())
        first = CapabilityAdapter(serves=False)
        Controller(MissionStore(self.path), first).work_once("shift-14")
        self.assertEqual(len(first.dispatch_keys), 1)

        self.store.resume_pre_provider(mission["id"], revision_binding())
        second = CapabilityAdapter(serves=False)
        again = Controller(MissionStore(self.path), second).work_once("shift-15")

        # The layer was asked again -- the refusal was not replayed from the
        # memo -- and it was asked exactly once.
        self.assertEqual(len(second.dispatch_keys), 1)
        self.assertNotEqual(second.dispatch_keys[0], first.dispatch_keys[0])
        self.assertEqual(again["terminal_reason"], "UNSUPPORTED_CAPABILITY")
        self.assertEqual([leg["process_started"]
                          for leg in self.store.runs(mission["id"])], [False, False])

    def test_recovered_dispatch_reuses_the_runtime_overlay_input_identity(self):
        """A recovery must hash the same revision payload the first leg used."""

        value = self.payload()
        mission, _ = Controller(
            self.store, RecoveryAdapter(mode="real"), lease_seconds=0
        ).submit(value, self.key(value))
        Controller(self.store, RecoveryAdapter(mode="real"), lease_seconds=0).work_once("s1")
        self.store.resume_pre_provider(mission["id"], revision_binding())

        crashing = RecoveryAdapter(mode="real", crash_on="verify")
        with self.assertRaises(ProcessDeath):
            Controller(self.store, crashing, lease_seconds=0).work_once("s2")
        self.assertEqual(self.store.get(mission["id"])["state"], "dispatched")

        replacement = Controller(
            MissionStore(self.path), RecoveryAdapter(mode="real"), lease_seconds=0
        )
        recovered = replacement.work_once("s3")
        self.assertEqual(recovered["id"], mission["id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(crashing.dispatches), 1)

    def test_a_layer_that_cannot_prove_it_started_nothing_is_never_reopened(self):
        """The eligible class is the proof, and an absence is not a proof."""

        class Silent(CapabilityAdapter):
            def _dispatch(self, operation_key, value):
                response = super()._dispatch(operation_key, value)
                response["receipt"].pop("process_started")
                return response

        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))
        controller.work_once("shift-13")
        self.store.resume_pre_provider(mission["id"], revision_binding())
        refused = Controller(MissionStore(self.path), Silent(serves=False)).work_once("s14")
        self.assertEqual(refused["state"], "refused")

        replay = self.store.resume_pre_provider(mission["id"], revision_binding())

        self.assertFalse(replay["changed"])
        self.assertEqual(self.store.get(mission["id"])["state"], "refused")

    def test_the_layers_refusal_is_still_a_host_fact_not_a_verdict(self):
        """The classification used to be right by accident.

        ``EXECUTION_MODE_UNPROVEN`` is an infrastructure reason, so while the
        real code was being overwritten by it the supervisor and the shift
        plane happened to treat the mission correctly.  Surfacing the true code
        must not turn "this host cannot serve it" into "this work failed".
        """

        from factory_controller import store as ledger

        for code in ("UNSUPPORTED_CAPABILITY", "ADAPTER_UNAVAILABLE",
                     "PROJECT_NOT_REGISTERED", "BASELINE_SHA_UNKNOWN"):
            with self.subTest(code=code):
                self.assertTrue(
                    any(code.startswith(prefix)
                        for prefix in ledger.INFRASTRUCTURE_REASON_PREFIXES))
        # A malformed request is the Controller's own defect, and stays one.
        for code in ("INVALID_BASELINE_SHA", "IDEMPOTENCY_CONFLICT"):
            with self.subTest(code=code):
                self.assertFalse(
                    any(code.startswith(prefix)
                        for prefix in ledger.INFRASTRUCTURE_REASON_PREFIXES))
        # None of them may unlock a reroute: only a proven side effect does.
        self.assertEqual(ledger.SIDE_EFFECT_POSSIBLE_PREFIXES,
                         ("PROVIDER_SWITCH_AFTER_",))

    def test_start_sees_the_stopped_dispatch_as_recoverable(self):
        """`factory start` must find this mission, and widen before it resumes.

        `start` admits the *run* contract's capability.  The product's own is
        admitted by `product` and `revise`, so before SF-164 a resume queued by
        `start` alone ran against a posture that had never been widened for it:
        the mission woke up and was refused UNSUPPORTED_CAPABILITY again.  The
        selector is read-only precisely so `start` can ask this question before
        it widens anything.
        """

        from factory_controller import factory as lifecycle

        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))
        controller.work_once("shift-13")
        self.store.resume_pre_provider(mission["id"], revision_binding())
        Controller(MissionStore(self.path), CapabilityAdapter(serves=False)
                   ).work_once("shift-14")

        factory = lifecycle.FactoryLifecycle(
            Controller(MissionStore(self.path), RecoveryAdapter()),
            owner=lifecycle.OwnerIdentity(username="owner", uid=501))
        contract = SimpleNamespace(project_id="lodus-casino", package_id="lodus-casino")

        found = factory._recoverable_revision_missions(contract)

        self.assertEqual([row["id"] for row in found], [mission["id"]])
        # And the product contract `start` now admits from names a capability
        # request of its own -- the seam that was never reached from `start`.
        product_contract = product.ProductContract.load(
            lifecycle.FactoryConfig.default().product_contract_path)
        self.assertEqual(product_contract.project_id, "lodus-casino")
        self.assertTrue(product_contract.capability_request)

    def test_a_completed_mission_is_never_recoverable(self):
        """Recovery reopens stopped work, never work that had effects."""

        from factory_controller import factory as lifecycle

        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))
        controller.work_once("shift-13")
        self.store.resume_pre_provider(mission["id"], revision_binding())
        done = Controller(MissionStore(self.path),
                          CapabilityAdapter(serves=True)).work_once("shift-14")
        self.assertEqual(done["state"], "completed")

        factory = lifecycle.FactoryLifecycle(
            Controller(MissionStore(self.path), RecoveryAdapter()),
            owner=lifecycle.OwnerIdentity(username="owner", uid=501))
        contract = SimpleNamespace(project_id="lodus-casino", package_id="lodus-casino")

        self.assertEqual(factory._recoverable_revision_missions(contract), [])

    def test_a_genuinely_unproven_execution_mode_still_refuses(self):
        """The security invariant SF-164 was told not to weaken."""

        controller = Controller(self.store, RecoveryAdapter())
        value = self.payload()
        mission, _ = controller.submit(value, self.key(value))
        controller.work_once("shift-13")
        self.store.resume_pre_provider(mission["id"], revision_binding())

        # A layer that completes, hands back a candidate, and states no mode.
        refused = Controller(MissionStore(self.path),
                             RecoveryAdapter(mode="unstated")).work_once("shift-14")

        self.assertEqual(refused["state"], "refused")
        self.assertIn("EXECUTION_MODE_UNPROVEN", refused["terminal_reason"])
        # And that refusal is *not* the reopenable class: something ran.
        self.assertFalse(
            self.store.resume_pre_provider(mission["id"], revision_binding())["changed"])

    def test_candidate_workspace_mismatch_is_never_masked_by_idempotency_key_unproven(self):
        """Regression 4: CANDIDATE_WORKSPACE_MISMATCH refusal is never masked by IDEMPOTENCY_KEY_UNPROVEN."""
        class MismatchAdapter(RecoveryAdapter):
            def execute(self, step, operation_key, value):
                if step == "dispatch":
                    route = value.get("route", {})
                    key = route.get("idempotency_key")
                    return {
                        "status": "refused",
                        "candidate_sha": None,
                        "execution_id": "e-mismatch",
                        "diagnostic": "CANDIDATE_WORKSPACE_MISMATCH",
                        "receipt": {
                            "provider_profile": route.get("provider_profile", "codex-product"),
                            "provider": "factory-evidence-core/first-live",
                            "execution_mode": "real",
                            "duration_ms": 100,
                            "process_started": True,
                            "idempotency_key": key,
                            "refusal_code": "CANDIDATE_WORKSPACE_MISMATCH",
                            "usage": {"cost_state": "unknown"},
                        },
                    }
                return super().execute(step, operation_key, value)

        value = self.payload()
        mission, _ = Controller(self.store, RecoveryAdapter(), lease_seconds=0).submit(value, self.key(value))
        Controller(self.store, RecoveryAdapter(), lease_seconds=0).work_once("worker-1")  # context -> STALE_HEAD
        self.store.resume_pre_provider(mission["id"], revision_binding())

        mismatch_controller = Controller(self.store, MismatchAdapter(mode="real"), lease_seconds=0)
        refused = mismatch_controller.work_once("worker-2")

        # Must report CANDIDATE_WORKSPACE_MISMATCH, NEVER IDEMPOTENCY_KEY_UNPROVEN
        self.assertEqual(refused["state"], "refused")
        self.assertEqual(refused["terminal_reason"], "CANDIDATE_WORKSPACE_MISMATCH")
        self.assertNotIn("IDEMPOTENCY_KEY_UNPROVEN", refused["terminal_reason"])

    def test_revision_replay_recovery_reopens_and_recovers_without_duplicate_provider(self):
        """Regressions 3 & 5: Revision replay recovery resumes stopped mission and recovers candidate."""
        class InitialMismatchAdapter(RecoveryAdapter):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.provider_runs = 0

            def execute(self, step, operation_key, value):
                if step == "dispatch":
                    route = value.get("route", {})
                    if not route.get("recover_only"):
                        self.provider_runs += 1
                    key = route.get("idempotency_key")
                    return {
                        "status": "refused",
                        "candidate_sha": None,
                        "execution_id": "e-mismatch",
                        "diagnostic": "CANDIDATE_WORKSPACE_MISMATCH",
                        "stage1_result": {
                            "execution_envelope": {
                                "candidate_sha": CANDIDATE,
                                "candidate_workspace": {"source_checkout": REGISTERED, "candidate_sha": CANDIDATE},
                            },
                        },
                        "receipt": {
                            "provider_profile": route.get("provider_profile", "codex-product"),
                            "provider": "factory-evidence-core/first-live",
                            "execution_mode": "real",
                            "duration_ms": 100,
                            "process_started": True,
                            "idempotency_key": key,
                            "refusal_code": "CANDIDATE_WORKSPACE_MISMATCH",
                            "usage": {"cost_state": "unknown"},
                        },
                    }
                return super().execute(step, operation_key, value)

        value = self.payload()
        mission, _ = Controller(self.store, RecoveryAdapter(), lease_seconds=0).submit(value, self.key(value))
        Controller(self.store, RecoveryAdapter(), lease_seconds=0).work_once("worker-1")
        self.store.resume_pre_provider(mission["id"], revision_binding())

        initial_adapter = InitialMismatchAdapter(mode="real")
        Controller(self.store, initial_adapter, lease_seconds=0).work_once("worker-2")
        self.assertEqual(self.store.get(mission["id"])["state"], "refused")
        self.assertEqual(initial_adapter.provider_runs, 1)

        # Now resume the mission using resume_pre_provider
        resumed = self.store.resume_pre_provider(mission["id"], revision_binding())
        self.assertTrue(resumed["changed"])
        self.assertEqual(self.store.get(mission["id"])["state"], "admitted")

        # Recovering adapter returns candidate on recovery
        class ReplayRecoveringAdapter(RecoveryAdapter):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.recover_calls = []

            def execute(self, step, operation_key, value):
                if step == "dispatch":
                    route = value.get("route", {})
                    self.recover_calls.append(dict(route))
                    key = route.get("idempotency_key")
                    return {
                        "status": "completed",
                        "candidate_sha": CANDIDATE,
                        "execution_id": "e-rebound",
                        "diagnostic": None,
                        "candidate_workspace": {"source_checkout": REVISION_CHECKOUT, "candidate_sha": CANDIDATE},
                        "receipt": {
                            "provider_profile": route.get("provider_profile", "codex-product"),
                            "provider": "factory-evidence-core/first-live",
                            "execution_mode": "real",
                            "duration_ms": 50,
                            "process_started": False,
                            "idempotency_key": key,
                            "refusal_code": None,
                            "usage": {"cost_state": "unknown"},
                        },
                    }
                return super().execute(step, operation_key, value)

        recovering_adapter = ReplayRecoveringAdapter(mode="real")
        completed = Controller(self.store, recovering_adapter, lease_seconds=0).work_once("worker-3")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(len(recovering_adapter.recover_calls), 1)
        self.assertTrue(recovering_adapter.recover_calls[0].get("recover_only"))


class RevisionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "controller.db"

    def register(self, store):
        store.register_project(portfolio.ProjectPolicy(
            project_id="lodus-casino", repository=REMOTE))

    def test_the_existing_mission_restarts_from_the_same_checkout_once(self):
        first_adapter = RecoveryAdapter()
        store = MissionStore(self.path)
        self.register(store)
        controller = Controller(store, first_adapter)
        mission, _ = controller.submit(old_revision_payload(), "revision:old")

        refused = controller.work_once("old-worker")
        self.assertEqual(refused["state"], "refused")
        self.assertIn("STALE_HEAD", refused["terminal_reason"])
        self.assertEqual(store.runs(mission["id"]), [])
        self.assertEqual(first_adapter.dispatches, [])

        original = store.get(mission["id"])
        rebound = store.resume_pre_provider(mission["id"], revision_binding())
        self.assertTrue(rebound["changed"])
        self.assertEqual(rebound["mission"]["id"], mission["id"])
        self.assertEqual(rebound["mission"]["attempt_count"], 1)
        self.assertEqual(store.runs(mission["id"]), [])
        self.assertEqual(store.step_output(mission["id"], "context")["refusal_code"],
                         "STALE_HEAD")

        replacement = RecoveryAdapter()
        restarted = Controller(MissionStore(self.path), replacement)
        completed = restarted.work_once("replacement-worker")
        self.assertEqual(completed["id"], mission["id"])
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(replacement.recovery_repositories, [REVISION_CHECKOUT])
        self.assertEqual(len(replacement.dispatches), 1)
        self.assertEqual(len(store.runs(mission["id"])), 1)
        self.assertEqual(store.context_history(mission["id"])["context_state"],
                         "bound")
        self.assertEqual(store.context_history(mission["id"])["context_refusals"],
                         [{"attempt": 1, "code": "STALE_HEAD",
                           "broker_status": "refused",
                           "context_manifest_hash": None}])
        self.assertEqual(original["payload"]["stage1"]["repository"], REGISTERED)
        self.assertEqual(store.get(mission["id"])["payload"]["stage1"]["repository"],
                         REGISTERED)

    def test_recovery_is_append_only_and_a_second_resume_is_a_noop(self):
        store = MissionStore(self.path)
        self.register(store)
        controller = Controller(store, RecoveryAdapter())
        mission, _ = controller.submit(old_revision_payload(), "revision:append-only")
        controller.work_once("worker")

        binding = revision_binding()
        first = store.resume_pre_provider(mission["id"], binding)
        second = store.resume_pre_provider(mission["id"], binding)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(store.context_binding(mission["id"]), binding)
        kinds = [event["kind"] for event in store.history(mission["id"])]
        self.assertEqual(kinds.count("REVISION_CONTEXT_REBOUND"), 1)
        self.assertEqual(kinds.count("PRE_PROVIDER_MISSION_RESUMED"), 1)

    def test_malformed_binding_cannot_reopen_or_mutate_the_stopped_mission(self):
        store = MissionStore(self.path)
        self.register(store)
        controller = Controller(store, RecoveryAdapter())
        mission, _ = controller.submit(old_revision_payload(), "revision:invalid")
        refused = controller.work_once("worker")
        before = store.history(mission["id"])

        invalid = revision_binding()
        invalid["stage1"]["repository"] = REGISTERED
        with self.assertRaises(ValueError):
            store.resume_pre_provider(mission["id"], invalid)

        self.assertEqual(store.get(mission["id"])["state"], "refused")
        self.assertIsNone(store.context_binding(mission["id"]))
        self.assertEqual(store.history(mission["id"]), before)
        self.assertEqual(refused["id"], mission["id"])

    def test_same_binding_is_idempotent_even_after_a_second_context_refusal(self):
        class AlwaysStale(RecoveryAdapter):
            def execute(self, step, operation_key, value):
                if step == "context":
                    return {"status": "refused", "refusal_code": "STALE_HEAD"}
                return super().execute(step, operation_key, value)

        store = MissionStore(self.path)
        self.register(store)
        controller = Controller(store, RecoveryAdapter())
        mission, _ = controller.submit(old_revision_payload(), "revision:retry")
        controller.work_once("worker")
        binding = revision_binding()
        store.resume_pre_provider(mission["id"], binding)

        restarted = Controller(MissionStore(self.path), AlwaysStale())
        second_refusal = restarted.work_once("replacement")
        before = store.history(mission["id"])
        replay = store.resume_pre_provider(mission["id"], binding)

        self.assertEqual(second_refusal["state"], "refused")
        self.assertFalse(replay["changed"])
        self.assertEqual(store.history(mission["id"]), before)


if __name__ == "__main__":
    unittest.main()
