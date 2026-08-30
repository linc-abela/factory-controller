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
        self.assertFalse(settled.ok, settled.render())
        # DF-2 targets a project registered for a capability the evidence
        # layer does not admit, so the serial rule advances and the next
        # mission refuses on its own merits rather than being skipped.
        self.assertEqual(settled.details["code"], "CAPABILITY_NOT_ADMISSIBLE")
        self.assertEqual(len(self.missions()), 1)

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

    def test_a_mission_that_changes_a_repository_is_not_materialized(self):
        with self.assertRaises(dogfood_intake.IntakeError) as raised:
            self.build(self.mission("DF-3"))

        self.assertEqual(raised.exception.code, "MISSION_CHANGES_A_REPOSITORY")

    def test_a_capability_the_evidence_layer_refuses_is_caught_first(self):
        with self.assertRaises(dogfood_intake.IntakeError) as raised:
            self.build(self.mission("DF-2"))

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


if __name__ == "__main__":
    unittest.main()
