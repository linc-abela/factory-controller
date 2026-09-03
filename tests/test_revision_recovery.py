"""SF-163: recover one refused revision without changing its mission identity."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factory_controller import context, portfolio
from factory_controller.engine import Controller
from factory_controller.store import MissionStore
from tests.test_context_binding import BrokerAdapter
from tests.support import ALPHA, mission_payload


REMOTE = "https://example.invalid/lodus-casino.git"
BASELINE = "b" * 40
PREDECESSOR = "a" * 40
REGISTERED = "/products/lodus-casino"
REVISION_CHECKOUT = "/Users/karlosabay/.factory-bridge/revisions/lodus-casino/" + BASELINE
REF = "refs/heads/factory/revision/v2"


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
    def __init__(self):
        super().__init__()
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
