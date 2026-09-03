"""SF-169: the reconciliation proof body reaches the runner, or nothing runs.

The Controller derives the proof digest from a durable row and the proof body
from the append-only store, and until now only the digest travelled.  The
consumer needs the body: it has to prove the sealed response against the
*original* request identity and digest the proof records, which the digest
names without carrying.

So the body travels beside the digest, and the two are cross-checked at both
ends.  A ``dispatch-reconcile`` leg with a missing or disagreeing body refuses
before a subprocess is started, because a lookup that cannot be proved is not
a lookup that may be attempted.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from factory_controller.stage1_adapter import execute


PROOF_DIGEST = "d" * 64

#: Enough of a runner to prove what argv it was handed, and nothing else.
FAKE_STAGE1 = (
    "import json,sys\n"
    "argv=sys.argv[1:]\n"
    "p=argv[argv.index('--output')+1]\n"
    "proof=argv[argv.index('--reconciliation-proof')+1] "
    "if '--reconciliation-proof' in argv else None\n"
    "json.dump({'status':'completed','argv':argv,"
    "'reconciliation_proof':json.load(open(proof)) if proof else None,"
    "'execution_envelope':{'candidate_sha':'a'*40,'execution_id':'e1',"
    "'execution_mode':'real','idempotency_key':'SF-169-T:'+'c'*64},"
    "'execution_binding':{'work_item_id':'SF-169-T'},"
    "'candidate_commit_verification':{'verified':True},"
    "'evidence_result':{'status':'complete','artifact_hash':'b'*64}},open(p,'w'))\n"
)


class ReconcileProofTransportTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / "fake_stage1.py"
        script.write_text(FAKE_STAGE1)
        self.config = {
            "command": [sys.executable, str(script)],
            "mode": "real",
            "operator_opt_in": True,
            "output": str(self.root / "result.json"),
        }
        self.proof = {"schema_version": "factory.bridge.revision_reconciliation.v1",
                      "idempotency_key": "SF-169-T:" + "c" * 64,
                      "candidate_sha": "a" * 40, "proof_digest": PROOF_DIGEST}

    def _reconcile(self, route):
        return execute({"step": "dispatch-reconcile",
                        "operation_key": "m:dispatch-reconcile",
                        "input": {"mission": {"stage1": self.config},
                                  "route": route}})

    def test_the_proof_body_is_handed_to_the_runner_beside_its_digest(self):
        result = self._reconcile({"reconcile_proof": PROOF_DIGEST,
                                  "reconcile_proof_record": self.proof})
        self.assertEqual(result["status"], "completed")
        argv = result["stage1_result"]["argv"]
        self.assertIn("--reconcile-replay", argv)
        self.assertEqual(argv[argv.index("--reconcile-replay") + 1], PROOF_DIGEST)
        # The body the runner read is the body the durable row held.
        self.assertEqual(result["stage1_result"]["reconciliation_proof"], self.proof)
        written = Path(argv[argv.index("--reconciliation-proof") + 1])
        self.assertTrue(written.exists())
        self.assertEqual(json.loads(written.read_text()), self.proof)
        # It is written beside the result, which this process reads back from
        # the same filesystem -- so the runner provably shares it.
        self.assertEqual(written.parent, Path(self.config["output"]).parent)

    def test_a_missing_or_disagreeing_body_refuses_before_anything_runs(self):
        for label, route in (
            ("no record", {"reconcile_proof": PROOF_DIGEST}),
            ("empty record", {"reconcile_proof": PROOF_DIGEST,
                              "reconcile_proof_record": None}),
            ("record for another proof",
             {"reconcile_proof": PROOF_DIGEST,
              "reconcile_proof_record": {**self.proof, "proof_digest": "e" * 64}}),
            ("no digest", {"reconcile_proof_record": self.proof}),
        ):
            with self.subTest(label=label):
                result = self._reconcile(route)
                self.assertEqual(result["diagnostic"], "RECONCILE_PROOF_MISSING")
                self.assertIs(result["receipt"]["process_started"], False)
                self.assertFalse(Path(self.config["output"]).exists())


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
