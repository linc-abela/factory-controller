from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory_controller import context
from factory_controller.stage1_adapter import _execution_mode, execute


FAKE_STAGE1 = (
    "import json,sys\n"
    "p=sys.argv[sys.argv.index('--output')+1]\n"
    "json.dump({'status':'completed','fixture_only':True,"
    "'execution_envelope':{'candidate_sha':'a'*40,'execution_id':'e1',"
    "'execution_mode':'fixture'},"
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

    def test_revision_context_uses_the_verified_execution_checkout(self):
        checkout = "/state/revisions/lodus-casino/" + "b" * 40
        grounding = {
            "schema_version": context.REVISION_GROUNDING_SCHEMA,
            "kind": "revision",
            "source": "factory-bridge",
            "project_id": "lodus-casino",
            "repository_remote_url": "https://example.invalid/lodus-casino.git",
            "revision_sha": "b" * 40,
            "checkout": checkout,
        }
        mission = {
            "project_id": "lodus-casino",
            "repository_remote_url": grounding["repository_remote_url"],
            "baseline_sha": grounding["revision_sha"],
            "stage1": {"repository": checkout,
                        "revision_grounding": grounding},
        }
        with patch("factory_controller.stage1_adapter.context_adapter.build",
                   return_value={"status": "built"}) as broker:
            result = self._run("context", mission,
                               context_request={"baseline_sha": "b" * 40})
        self.assertEqual(result["status"], "built")
        self.assertEqual(broker.call_args.kwargs["repo"], checkout)

    def test_invalid_revision_grounding_refuses_before_the_broker(self):
        mission = self._mission(
            project_id="lodus-casino",
            repository_remote_url="https://example.invalid/lodus-casino.git",
            baseline_sha="b" * 40,
            stage1={
                "repository": "/state/revisions/lodus-casino/" + "b" * 40,
                "revision_grounding": {
                    "schema_version": context.REVISION_GROUNDING_SCHEMA,
                    "kind": "revision", "source": "factory-bridge",
                    "project_id": "lodus-casino",
                    "repository_remote_url":
                        "https://example.invalid/lodus-casino.git",
                    "revision_sha": "c" * 40,
                    "checkout": "/state/revisions/lodus-casino/" + "b" * 40,
                },
            })
        with patch("factory_controller.stage1_adapter.context_adapter.build") as broker:
            result = self._run("context", mission,
                               context_request={"baseline_sha": "b" * 40})
        self.assertEqual(result, {"status": "refused",
                                  "refusal_code": "REVISION_GROUNDING_INVALID"})
        broker.assert_not_called()

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

    def test_dispatch_projects_candidate_workspace_from_evidence_binding(self):
        proof = {
            "schema_version": "1.0",
            "lane_id": "lane-controller-dispatch",
            "worktree": "/tmp/controller-dispatch-worktree",
            "source_checkout": "/tmp/controller-dispatch-source",
            "candidate_ref": "refs/factory/lanes/lane-controller-dispatch",
            "baseline_sha": "b" * 40,
            "candidate_sha": "a" * 40,
            "head_sha": "a" * 40,
            "clean": True,
        }
        script = self.root / "proof_stage1.py"
        script.write_text(
            "import json,sys\n"
            "p=sys.argv[sys.argv.index('--output')+1]\n"
            f"proof={proof!r}\n"
            "json.dump({'status':'completed','fixture_only':True,"
            "'execution_envelope':{'candidate_sha':'a'*40,'execution_id':'e-proof',"
            "'execution_mode':'fixture'},"
            "'execution_binding':{'work_item_id':'SF-135-T',"
            "'context_manifest_hash':'c'*64,'candidate_workspace':proof},"
            "'candidate_commit_verification':{'verified':True},"
            "'evidence_result':{'status':'complete','artifact_hash':'b'*64}},"
            "open(p,'w'))\n"
        )
        mission = {"stage1": dict(self.config, command=[sys.executable, str(script)])}

        dispatch = self._run("dispatch", {"mission": mission})

        self.assertEqual(dispatch["candidate_workspace"], proof)

    def test_dry_run_result_is_reported_as_a_fixture(self):
        """The SF-134 laundering path: a dry run must never look real."""

        dispatch = self._run("dispatch", {"mission": self._mission()})
        self.assertEqual(dispatch["receipt"]["execution_mode"], "fixture")

    def test_real_mode_is_proven_by_the_nested_envelope(self):
        """A top-level real hint cannot stand in for Bridge execution proof."""

        self.assertEqual(
            _execution_mode({"execution_mode": "real", "fixture_only": False}),
            "unknown",
        )
        self.assertEqual(
            _execution_mode({
                "execution_mode": "real",
                "fixture_only": False,
                "execution_envelope": {"execution_mode": "real"},
            }),
            "real",
        )
        self.assertEqual(
            _execution_mode({
                "execution_mode": "real",
                "fixture_only": False,
                "execution_envelope": {"execution_mode": "fixture"},
            }),
            "unknown",
        )

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

    def test_a_gate_that_could_not_find_its_tooling_keeps_its_own_diagnostic(self):
        """The 127 that stopped DF-1, with the sentence that explains it.

        Both labs' evaluators are `exec docker compose ...`.  Run without a
        PATH that resolves the container runtime they exit 127, and the record
        used to carry nothing but that number -- indistinguishable from a
        mission whose own work failed.  The status is still recorded exactly as
        observed, because the portfolio forbids rewriting a non-zero gate as
        anything else; what changes is that the evaluator's last words travel
        with it.
        """

        gate = self.root / "dev"
        gate.write_text("#!/bin/sh\n"
                        "exec sf157-absent-container-runtime compose run lab\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        config = dict(self.config,
                      gate_commands={"dev-check": [str(gate), "check"]},
                      gate_workdir=str(self.root))
        mission = {"stage1": config, "acceptance_gate_ids": ["dev-check"]}
        dispatch = self._run("dispatch", {"mission": mission})

        answer = execute({"step": "evaluate", "operation_key": "m:evaluate",
                          "input": {"mission": mission, "dispatch": dispatch}},
                         )

        outcome = answer["gate_outcomes"][0]
        self.assertFalse(answer["passed"])
        self.assertEqual(outcome["exit_code"], 127)
        self.assertIn("sf157-absent-container-runtime", outcome["stderr_tail"])
        self.assertIn("not found", outcome["stderr_tail"])

    def test_a_quiet_gate_records_a_typed_absence_rather_than_an_empty_string(self):
        gate = self.root / "quiet.sh"
        gate.write_text("#!/bin/sh\nexit 0\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        config = dict(self.config, gate_commands={"G-PASS": [str(gate)]},
                      gate_workdir=str(self.root))
        mission = {"stage1": config, "acceptance_gate_ids": ["G-PASS"]}
        dispatch = self._run("dispatch", {"mission": mission})

        answer = execute({"step": "evaluate", "operation_key": "m:evaluate",
                          "input": {"mission": mission, "dispatch": dispatch}})

        self.assertEqual(answer["gate_outcomes"][0]["stderr_tail"],
                         "not_applicable")

    def test_mutating_gate_runs_against_the_verified_candidate_not_the_baseline(self):
        """A green candidate gate must not be a green baseline gate in disguise."""

        repository = self.root / "candidate-repository"
        repository.mkdir()
        self._git(repository, "init", "--quiet")
        self._git(repository, "config", "user.email", "factory-test@example.invalid")
        self._git(repository, "config", "user.name", "Factory Test")
        (repository / "state.txt").write_text("baseline\n")
        gate = repository / "gate.sh"
        gate.write_text(
            "#!/bin/sh\n"
            "test \"$(cat state.txt)\" = candidate\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        self._git(repository, "add", "state.txt", "gate.sh")
        self._git(repository, "commit", "--quiet", "-m", "baseline")
        baseline = self._git(repository, "rev-parse", "HEAD")

        (repository / "state.txt").write_text("candidate\n")
        self._git(repository, "commit", "--quiet", "-am", "candidate")
        candidate = self._git(repository, "rev-parse", "HEAD")
        self._git(repository, "checkout", "--quiet", "--detach", baseline)

        config = {
            "repository": str(repository),
            "gate_workdir": str(repository),
            "gate_commands": {"G1": ["{candidate_worktree}/gate.sh"]},
            "mutates_repository": True,
        }
        mission = {"stage1": config, "acceptance_gate_ids": ["G1"],
                   "mutates_repository": True}
        dispatch = {
            "candidate_sha": candidate,
            "stage1_result": {
                "execution_envelope": {"candidate_sha": candidate},
                # A carried pass is deliberately ignored for a mutating mission.
                "gate_outcomes": [{"gate_id": "G1", "passed": True}],
            },
        }

        evaluation = execute({
            "step": "evaluate", "operation_key": "m:evaluate-candidate",
            "input": {"mission": mission, "dispatch": dispatch},
        })

        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["target"], "candidate")
        self.assertEqual(evaluation["target_sha"], candidate)
        self.assertEqual(evaluation["gate_outcomes"][0]["target_sha"], candidate)
        self.assertEqual(evaluation["gate_outcomes"][0]["target"], "candidate")
        self.assertNotEqual((repository / "state.txt").read_text(), "candidate\n")
        self.assertEqual(self._git(repository, "rev-parse", "HEAD"), baseline)

    def test_mutating_gate_refuses_without_a_candidate_and_does_not_run_baseline(self):
        gate = self.root / "baseline-only.sh"
        gate.write_text("#!/bin/sh\nexit 0\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        config = {
            "repository": str(self.root),
            "gate_workdir": str(self.root),
            "gate_commands": {"G1": [str(gate)]},
            "mutates_repository": True,
        }
        mission = {"stage1": config, "acceptance_gate_ids": ["G1"],
                   "mutates_repository": True}

        evaluation = execute({
            "step": "evaluate", "operation_key": "m:evaluate-missing-candidate",
            "input": {"mission": mission, "dispatch": {}},
        })

        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["diagnostic"], "CANDIDATE_SHA_UNAVAILABLE")
        self.assertEqual(evaluation["gate_outcomes"][0]["detail"], "not_run")

    def test_mutating_gate_refuses_a_workspace_proof_bound_to_another_candidate(self):
        config = {
            "repository": str(self.root),
            "gate_workdir": str(self.root),
            "gate_commands": {"G1": ["/bin/false"]},
            "mutates_repository": True,
        }
        mission = {"stage1": config, "acceptance_gate_ids": ["G1"],
                   "mutates_repository": True}
        candidate = "a" * 40
        dispatch = {
            "candidate_sha": candidate,
            "candidate_workspace": {
                "schema_version": "1.0",
                "lane_id": "lane-controller-1",
                "worktree": "/tmp/controller-lane",
                "source_checkout": "/tmp/controller-source",
                "candidate_ref": "refs/factory/lanes/lane-controller-1",
                "baseline_sha": "b" * 40,
                "candidate_sha": "c" * 40,
                "head_sha": "c" * 40,
                "clean": True,
            },
            "stage1_result": {"execution_envelope": {"candidate_sha": candidate}},
        }

        evaluation = execute({
            "step": "evaluate", "operation_key": "m:evaluate-workspace-proof",
            "input": {"mission": mission, "dispatch": dispatch},
        })

        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["diagnostic"], "CANDIDATE_WORKSPACE_BINDING_FAILED")
        self.assertEqual(evaluation["gate_outcomes"][0]["detail"], "not_run")

    def test_explicit_expected_failure_preserves_nonzero_exit_and_satisfies_mission(self):
        gate = self.root / "expected-failure.sh"
        gate.write_text("#!/bin/sh\nexit 1\n")
        gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
        config = dict(
            self.config,
            gate_commands={"G-EXPECTED": [str(gate)]},
            gate_workdir=str(self.root),
            gate_expectations={"G-EXPECTED": {"passed": False, "exit_code": 1}},
        )
        mission = {"stage1": config, "acceptance_gate_ids": ["G-EXPECTED"]}

        evaluation = execute({
            "step": "evaluate", "operation_key": "m:evaluate-expected-failure",
            "input": {"mission": mission, "dispatch": {}},
        })

        self.assertTrue(evaluation["passed"])
        outcome = evaluation["gate_outcomes"][0]
        self.assertFalse(outcome["passed"])
        self.assertEqual(outcome["exit_code"], 1)
        self.assertTrue(outcome["expected_failure"])
        self.assertTrue(outcome["satisfied"])

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
