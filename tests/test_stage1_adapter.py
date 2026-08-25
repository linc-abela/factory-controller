from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from factory_controller.stage1_adapter import execute


FAKE_STAGE1 = (
    "import json,sys\n"
    "p=sys.argv[sys.argv.index('--output')+1]\n"
    "json.dump({'status':'completed','fixture_only':True,"
    "'execution_envelope':{'candidate_sha':'a'*40,'execution_id':'e1'},"
    "'execution_binding':{'work_item_id':'SF-135-T','context_manifest_hash':'c'*64},"
    "'candidate_commit_verification':{'verified':True},"
    "'gate_outcomes':[{'gate_id':'G1','passed':True}],"
    "'evidence_result':{'status':'complete','artifact_hash':'b'*64}},open(p,'w'))\n"
)


class Stage1AdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / "fake_stage1.py"
        script.write_text(FAKE_STAGE1)
        self.config = {
            "command": [sys.executable, str(script)],
            "mode": "dry_run",
            "output": str(self.root / "result.json"),
        }

    def _mission(self, **extra):
        return {"stage1": self.config, **extra}

    def _run(self, step, mission, **rest):
        return execute({"step": step, "operation_key": f"m:{step}",
                        "input": {"mission": mission, **rest}})

    def test_real_mode_refuses_without_explicit_operator_authority(self):
        request = {"step": "dispatch", "operation_key": "m:dispatch",
                   "input": {"mission": {"mission": {"stage1": {"command": ["unused"], "mode": "real"}}}}}
        result = execute(request)
        self.assertEqual(result["diagnostic"], "MISSING_OPERATOR_OPT_IN")
        self.assertIs(result["receipt"]["process_started"], False)

    def test_dispatch_then_projects_verification_and_evidence(self):
        mission = self._mission(acceptance_gate_ids=["G1"])
        dispatch = self._run("dispatch", {"mission": mission})
        self.assertEqual(dispatch["status"], "completed")
        verify = self._run("verify", mission, dispatch=dispatch)
        evaluation = self._run("evaluate", mission, dispatch=dispatch, verification=verify)
        evidence = self._run("evidence", mission, dispatch=dispatch, verification=verify,
                             evaluation=evaluation)
        self.assertTrue(verify["verified"])
        self.assertTrue(evaluation["passed"])
        self.assertTrue(evidence["accepted"])
        self.assertEqual(evidence["evidence_pointer"], "b" * 64)

    def test_dry_run_result_is_reported_as_a_fixture(self):
        """The SF-134 laundering path: a dry run must never look real."""

        dispatch = self._run("dispatch", {"mission": self._mission()})
        self.assertEqual(dispatch["receipt"]["execution_mode"], "fixture")

    def test_bound_idempotency_key_is_rederived_from_the_binding(self):
        """Evidence Core's own rule, re-derived rather than restated."""

        dispatch = self._run("dispatch", {"mission": self._mission()})
        self.assertEqual(dispatch["receipt"]["idempotency_key"], "SF-135-T:" + "c" * 64)

    def test_undeclared_gates_never_pass(self):
        mission = self._mission()
        dispatch = self._run("dispatch", {"mission": mission})
        evaluation = self._run("evaluate", mission, dispatch=dispatch)
        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["diagnostic"], "ACCEPTANCE_GATE_UNDECLARED")

    def test_declared_gate_without_a_command_is_not_run_and_fails(self):
        mission = self._mission(acceptance_gate_ids=["G1", "G-MISSING"])
        dispatch = self._run("dispatch", {"mission": mission})
        evaluation = self._run("evaluate", mission, dispatch=dispatch)
        outcomes = {item["gate_id"]: item for item in evaluation["gate_outcomes"]}
        self.assertFalse(evaluation["passed"])
        self.assertEqual(outcomes["G-MISSING"]["detail"], "not_run")
        self.assertEqual(outcomes["G-MISSING"]["diagnostic"], "ACCEPTANCE_GATE_COMMAND_UNDECLARED")

    def test_declared_gate_runs_the_target_repository_evaluator(self):
        """The gate outcome comes from a real exit code, not a placeholder."""

        gate = self.root / "evaluate.sh"
        gate.write_text("#!/bin/sh\nexit ${GATE_EXIT:-0}\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        failing = self.root / "fail.sh"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
        config = dict(self.config,
                      gate_commands={"G-PASS": [str(gate)], "G-FAIL": [str(failing)]},
                      gate_workdir=str(self.root))
        mission = {"stage1": config, "acceptance_gate_ids": ["G-PASS"]}
        dispatch = self._run("dispatch", {"mission": mission})
        passing = execute({"step": "evaluate", "operation_key": "m:evaluate",
                           "input": {"mission": mission, "dispatch": dispatch}})
        self.assertTrue(passing["passed"])
        self.assertEqual(passing["gate_outcomes"][0]["exit_code"], 0)
        self.assertEqual(passing["gate_outcomes"][0]["evidence_class"], "rederived")

        mission = {"stage1": config, "acceptance_gate_ids": ["G-FAIL"]}
        failed = execute({"step": "evaluate", "operation_key": "m:evaluate",
                          "input": {"mission": mission, "dispatch": dispatch}})
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["gate_outcomes"][0]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
