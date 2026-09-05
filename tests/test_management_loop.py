"""Unattended management loop: scheduled intake, judgment, eligibility, dispatch."""

from __future__ import annotations

import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path

from factory_controller import advisor, management, portfolio
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore
from tests.support import ALPHA, BETA, Clock, LayerAdapter
from tests.test_authority_boundaries import code_text

MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "management.py"
INBOX = Path(__file__).resolve().parent / "fixtures" / "scheduled_inbox"
WORK_ITEM = "factory-maintenance:SF-202"
PRIORS = {ALPHA: 0.4, BETA: 1.0}


def judgment(**extra):
    body = {
        "reasoning": "Select the eligible implementer; keep an independent reviewer.",
        "proposals": [{
            "kind": "specialist_profile",
            "mission_id": "pending",
            "profile": ALPHA,
        }],
        "observed_identity": {"profile": "scripted-advisor", "effort": "recorded"},
    }
    body.update(extra)
    return body


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "controller.db"
        self.clock = Clock()
        self.store = MissionStore(str(self.path), clock=self.clock)
        self.adapter = LayerAdapter()
        self.controller = Controller(
            self.store, self.adapter,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            lease_seconds=5)
        self.plane = management.ManagementPlane(self.store, clock=self.clock)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id="factory", repository="https://example.invalid/factory.git",
            state="enabled", priority=100, concurrency_cap=4,
            acceptance_gate_ids=("suite",),
            acceptance_gate_source="repo://factory@baseline:dev",
            policy_version="1.0"))
        self.port = advisor.StaticAdvisor(judgment())

    def cycle(self, **kwargs):
        return self.plane.cycle(
            "mgr", source_dir=INBOX, controller=self.controller,
            manager=kwargs.pop("manager", self.port),
            priors=kwargs.pop("priors", PRIORS),
            **kwargs)


class TerminationTests(unittest.TestCase):
    def test_the_cycle_never_sleeps(self):
        self.assertNotIn("sleep", code_text(MODULE.read_text()))

    def test_the_module_never_names_a_vendor(self):
        code = code_text(MODULE.read_text())
        for token in ("hermes", "openai", "anthropic", "codex", "gemini"):
            self.assertNotIn(token, code)


class LoopTests(Case):
    def test_a_scheduled_inbox_completes_one_unattended_loop(self):
        report = self.cycle()
        self.assertEqual(report["outcome"], "completed")
        self.assertTrue(report["export"]["judgment_reasoning_present"])
        self.assertEqual(report["export"]["selected_executor"], ALPHA)
        self.assertEqual(report["export"]["reviewer_requirement"], BETA)
        self.assertEqual(report["export"]["execution_receipt"]["requested_executor"], ALPHA)
        self.assertEqual(report["export"]["execution_receipt"]["observed_executor"], ALPHA)
        self.assertEqual(
            report["export"]["independent_outcome"]["verdict"], "accepted")
        self.assertEqual(report["next_action"], "not_applicable")
        self.assertEqual(self.store.counts().get("completed"), 1)
        status = self.plane.status()
        self.assertEqual(status["last_decision"]["selected"], ALPHA)
        self.assertEqual(status["selected_observed_profiles"]["requested_executor"], ALPHA)
        self.assertEqual(status["selected_observed_profiles"]["observed_executor"], ALPHA)
        lines = management.status_lines(status)
        self.assertTrue(any(line.startswith("Management:") for line in lines))
        self.assertTrue(any("reviewer" in line for line in lines))
        records = self.plane.export_records()
        self.assertEqual(records[-1]["schema_version"], management.EXPORT_SCHEMA)
        self.assertEqual(records[-1]["source_kind"], "scheduled")

    def test_cli_manage_cycle_replays_a_recorded_judgment(self):
        from factory_controller.cli import main as cli_main
        proposals = Path(self.tmp.name) / "judgment.json"
        proposals.write_text(json.dumps(judgment()))
        priors = Path(self.tmp.name) / "priors.json"
        priors.write_text(json.dumps(PRIORS))
        rc = cli_main([
            "--db", str(self.path),
            "--adapter", "python -m factory_controller.safe_provider",
            "manage", "cycle",
            "--source-dir", str(INBOX),
            "--proposals", str(proposals),
            "--priors", str(priors),
            "--worker", "cli-mgr",
        ])
        self.assertEqual(rc, 0)

    def test_preference_cannot_rescue_an_ineligible_profile(self):
        report = self.cycle(readiness={ALPHA: "unknown", BETA: "admissible"})
        self.assertNotEqual(report.get("export", {}).get("selected_executor"), ALPHA)
        eligibility = report["export"]["hard_eligibility"]
        self.assertNotIn(ALPHA, eligibility["eligible"])
        self.assertTrue(any(row["reason"] == "readiness_unknown"
                            for row in eligibility["rejected"]))

    def test_illegal_proposals_are_recorded_and_not_applied(self):
        port = advisor.StaticAdvisor(judgment(proposals=[
            {"kind": "approve_production"},
            {"kind": "set_budget", "budget_ceiling": 999},
            {"kind": "specialist_profile", "mission_id": "nope", "profile": ALPHA},
        ]))
        report = self.cycle(manager=port)
        kinds = [row["kind"] for row in json.loads(
            self.plane.latest_record()["selection_json"])["illegal"]]
        self.assertIn("approve_production", kinds)
        self.assertIn("set_budget", kinds)
        self.assertEqual(report["export"]["selected_executor"], ALPHA)

    def test_a_prompt_populated_inbox_is_refused(self):
        root = Path(self.tmp.name) / "prompted"
        root.mkdir()
        (root / "authority.json").write_text(json.dumps({
            "schema_version": management.AUTHORITY_SCHEMA,
            "granted_by": "owner_policy", "source": "scheduled_inbox",
            "prompt": True,
        }))
        with self.assertRaises(management.ManagementRefusal) as raised:
            management.load_authority(root)
        self.assertEqual(raised.exception.code, "MANAGEMENT_OWNER_PROMPT_POPULATED")

    def test_overlapping_cycles_are_refused(self):
        first = self.plane._claim_cycle("a", 30)
        with self.assertRaises(management.ManagementRefusal) as raised:
            self.plane._claim_cycle("b", 30)
        self.assertEqual(raised.exception.code, "MANAGEMENT_CYCLE_IN_FLIGHT")
        self.assertEqual(raised.exception.extra["cycle_id"], first["cycle_id"])

    def test_manager_unavailability_blocks_new_judgment(self):
        class Down:
            def judge(self, snapshot):
                raise PermissionError("ADVISOR_CREDENTIAL_ABSENT")

            def observed_identity(self, body=None):
                return {"requested_profile": "advisory-endpoint",
                        "requested_effort": "unknown",
                        "observed_profile": "unknown",
                        "observed_effort": "unknown"}

        report = self.cycle(manager=Down())
        self.assertEqual(report["reason"], "EXTERNAL_OWNER_AUTH_REQUIRED")
        self.assertEqual(report["next_action"], "OWNER_AUTH")
        self.assertEqual(report["owner_attention"]["code"], "EXTERNAL_OWNER_AUTH_REQUIRED")
        self.assertEqual(self.store.counts().get("completed", 0), 0)

    def test_a_missing_inference_provider_is_an_adapter_block_not_owner_auth(self):
        class Down:
            def judge(self, snapshot):
                raise PermissionError("ADVISOR_MODEL_ABSENT")

            def observed_identity(self, body=None):
                return {"requested_profile": "advisory-endpoint",
                        "requested_effort": "unknown",
                        "observed_profile": "unknown",
                        "observed_effort": "unknown"}

        report = self.cycle(manager=Down())
        self.assertEqual(report["reason"], "MANAGER_PROVIDER_ADAPTER_BLOCKED")
        self.assertEqual(report["next_action"], "WAIT_MANAGER")
        self.assertIsNone(report.get("owner_attention"))
        self.assertEqual(self.store.counts().get("completed", 0), 0)

    def test_elapsed_time_during_judgment_is_not_a_stale_decision(self):
        clock = self.clock

        class Slow:
            def judge(self, snapshot):
                clock.advance(90)
                return judgment()

            def observed_identity(self, body=None):
                return advisor.StaticAdvisor().observed_identity(body)

        report = self.cycle(manager=Slow())
        self.assertEqual(report["outcome"], "completed")
        self.assertTrue(report["export"]["judgment_reasoning_present"])

    def test_a_stale_judgment_is_refused_before_dispatch(self):
        plane = self.plane
        store = self.store

        class Mutating:
            def judge(self, snapshot):
                store.submit({"work_item_id": "noise", "project_id": "factory",
                              "execution_mode": "fixture",
                              "acceptance_gate_ids": ["G-BUILD"],
                              "provider_candidates": [{"profile": ALPHA}]},
                             "noise-key")
                return judgment()

            def observed_identity(self, body=None):
                return advisor.StaticAdvisor().observed_identity(body)

        report = plane.cycle(
            "mgr", source_dir=INBOX, controller=self.controller,
            manager=Mutating(), priors=PRIORS)
        self.assertEqual(report["reason"], "MANAGEMENT_STALE_DECISION")
        self.assertEqual(self.store.counts().get("completed", 0), 0)

    def test_child_budget_widening_is_refused(self):
        parent = {"execution_policy": {"budget_ceiling": 10, "budget_currency": "USD",
                                       "max_route_legs": 2, "allowed_profiles": [ALPHA]}}
        with self.assertRaises(management.ManagementRefusal) as raised:
            management.inherit_envelope(parent, {
                "execution_policy": {"budget_ceiling": 50, "budget_currency": "USD"}})
        self.assertEqual(raised.exception.code, "MANAGEMENT_ENVELOPE_WIDENED")

    def test_presence_without_reasoning_is_not_a_judgment(self):
        port = advisor.StaticAdvisor({"version": "0.19.0", "gateway_running": True,
                                      "proposals": []})
        with self.assertRaises(ValueError):
            port.judge({"controller_state_version": "x"})


class ConcurrencyTests(Case):
    def test_two_workers_cannot_claim_the_same_cycle(self):
        hits = []

        def run():
            try:
                hits.append(self.plane._claim_cycle("w", 30)["cycle_id"])
            except management.ManagementRefusal as exc:
                hits.append(exc.code)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(item.startswith("mgc_") for item in hits).count(True), 1)
        self.assertIn("MANAGEMENT_CYCLE_IN_FLIGHT", hits)
