from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from factory_controller.stage1_adapter import execute


class Stage1AdapterTest(unittest.TestCase):
    def test_real_mode_refuses_without_explicit_operator_authority(self):
        request = {"step": "dispatch", "operation_key": "m:dispatch", "input": {"mission": {"mission": {"stage1": {"command": ["unused"], "mode": "real"}}}}}
        self.assertEqual(execute(request)["diagnostic"], "MISSING_OPERATOR_OPT_IN")

    def test_dispatch_then_projects_verification_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "result.json"
            script = root / "fake_stage1.py"
            script.write_text(
                "import json,sys\n"
                "p=sys.argv[sys.argv.index('--output')+1]\n"
                "json.dump({'status':'completed','execution_envelope':{'candidate_sha':'a'*40,'execution_id':'e1'},'candidate_commit_verification':{'verified':True},'gate_outcomes':[{'gate_id':'G1','passed':True}],'evidence_result':{'status':'complete','artifact_hash':'b'*64}},open(p,'w'))\n"
            )
            config = {"command": [sys.executable, str(script)], "mode": "dry_run", "output": str(output)}
            dispatch_request = {"step": "dispatch", "operation_key": "m:dispatch", "input": {"mission": {"mission": {"stage1": config}}}}
            dispatch = execute(dispatch_request)
            self.assertEqual(dispatch["status"], "completed")
            verify = execute({"step": "verify", "operation_key": "m:verify", "input": {"mission": {"stage1": config}, "dispatch": dispatch}})
            evaluation = execute({"step": "evaluate", "operation_key": "m:evaluate", "input": {"mission": {"stage1": config}, "dispatch": dispatch, "verification": verify}})
            evidence = execute({"step": "evidence", "operation_key": "m:evidence", "input": {"mission": {"stage1": config}, "dispatch": dispatch, "verification": verify, "evaluation": evaluation}})
            self.assertTrue(verify["verified"])
            self.assertTrue(evaluation["passed"])
            self.assertTrue(evidence["accepted"])
            self.assertEqual(evidence["evidence_pointer"], "b" * 64)


if __name__ == "__main__":
    unittest.main()
