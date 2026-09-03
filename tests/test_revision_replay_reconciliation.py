"""SF-168: a stopped revision recovers from proof, never from a candidate hint.

The mission this describes really happened.  A provider ran, produced a real
commit, and the run stopped anyway: the lane had been cloned from the
registered checkout, so the candidate workspace proof named a checkout that was
not the one Stage-1 verification reads, and Evidence Core refused
``CANDIDATE_WORKSPACE_MISMATCH``.  That refusal is correct and stays.

What SF-167 got wrong was the recovery.  It reopened the mission on a candidate
hint plus a list of refusal prefixes -- including every generic infrastructure
reason and ``IDEMPOTENCY_KEY_UNPROVEN``, which are exactly the states where the
provider's effect is *not* established -- and then re-ran the dispatch adapter
over the settled COMPLETED row, overwriting the output that recorded why the
first attempt stopped.

Here the only thing that reopens the mission is a durable reconciliation proof
derived by the execution layer from the immutable sealed response, and the leg
it authorizes is a lookup under its own adapter operation and its own durable
step identity.  The original row is never touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factory_controller import portfolio, store as store_mod
from factory_controller.engine import Controller
from factory_controller.store import (ConflictError, MissionStore,
                                      DISPATCH_STEP, DISPATCH_RECOVERY_STEP,
                                      RECONCILE_STEP)
from tests.support import mission_payload
from tests.test_context_binding import manifest_hash_for
from tests.test_revision_recovery import (BASELINE, CANDIDATE, PREDECESSOR,
                                          REGISTERED, REMOTE,
                                          REVISION_CHECKOUT, RecoveryAdapter,
                                          old_revision_payload,
                                          revision_binding, CapabilityAdapter)


PROOF_DIGEST = "9" * 64


def reconciliation_proof(**overrides) -> dict:
    """What ``factory-bridge revision reconcile`` returns, in its own shape."""

    proof = {
        "schema_version": "factory.bridge.revision_reconciliation.v1",
        "project_id": "lodus-casino",
        "repository_remote_url": REMOTE,
        "work_item_id": "lodus-casino:revision:2",
        "idempotency_key": "",
        "original_request_identity": "d" * 64,
        "original_response_digest": "e" * 64,
        "original_source_checkout": REGISTERED,
        "original_lane_id": "fln_lodus-casino_" + "0" * 32,
        "candidate_ref": "refs/factory/lanes/fln_lodus-casino_" + "0" * 32,
        "candidate_sha": CANDIDATE,
        "baseline_sha": BASELINE,
        "predecessor_sha": PREDECESSOR,
        "revision_sha": BASELINE,
        "revision_ref": "refs/heads/factory/revision/v2",
        "revision_checkout": REVISION_CHECKOUT,
        "imported_ref": "refs/factory/lanes/fln_lodus-casino_" + "0" * 32,
        "imported_sha": CANDIDATE,
        "candidate_already_present": False,
        "reconciled_at": 1.0,
        "proof_digest": PROOF_DIGEST,
    }
    proof.update(overrides)
    return proof


class _MismatchAdapter(RecoveryAdapter):
    """The layer's real answer: a provider ran, and its candidate is sealed.

    ``process_started: True`` is the honest fact and is what makes every
    pre-provider recovery class correctly refuse this mission.  The candidate
    lives in the execution envelope, not in the adapter's own projection: a
    refused result carries no ``candidate_sha`` of its own, by SF-167's one
    change worth keeping.
    """

    def __init__(self, **kwargs):
        super().__init__(mode="real", **kwargs)
        self.provider_runs = 0
        self.reconcile_calls: list[dict] = []
        self.reconciled = False

    def execute(self, step, operation_key, value):
        if step == RECONCILE_STEP:
            self.reconcile_calls.append(dict(value.get("route") or {}))
            return self._sealed(value, completed=True)
        if step == DISPATCH_STEP:
            self.provider_runs += 1
            return self._sealed(value, completed=False)
        return super().execute(step, operation_key, value)

    def _sealed(self, value, *, completed: bool) -> dict:
        route = value.get("route") or {}
        envelope = {"candidate_sha": CANDIDATE,
                    "idempotency_key": route.get("idempotency_key"),
                    "execution_mode": "real"}
        workspace = {
            "schema_version": "1.0",
            "lane_id": "fln_lodus-casino_" + "0" * 32,
            "worktree": "/lanes/worktree",
            "source_checkout": REVISION_CHECKOUT if completed else REGISTERED,
            "candidate_ref": "refs/factory/lanes/fln_lodus-casino_" + "0" * 32,
            "baseline_sha": BASELINE,
            "candidate_sha": CANDIDATE,
            "head_sha": CANDIDATE,
            "clean": True,
        }
        return {
            "status": "completed" if completed else "refused",
            "candidate_sha": CANDIDATE if completed else None,
            "candidate_workspace": workspace if completed else None,
            "execution_id": "e-revision-2",
            "diagnostic": None if completed else "CANDIDATE_WORKSPACE_MISMATCH",
            "stage1_result": {
                "status": "completed" if completed else "refused",
                "refusal_code": None if completed else "CANDIDATE_WORKSPACE_MISMATCH",
                "fixture_only": False,
                "execution_envelope": envelope,
                "execution_binding": {**envelope,
                                      "candidate_workspace": workspace},
                "candidate_commit_verification": {"verified": True,
                                                  "candidate_sha": CANDIDATE},
                "evidence_result": {"status": "complete",
                                    "artifact_hash": "f" * 64},
            },
            "receipt": {
                "provider_profile": route.get("provider_profile", "codex-product"),
                "provider": "factory-evidence-core/first-live",
                "execution_mode": "real",
                "duration_ms": 120,
                "process_started": not completed,
                "idempotency_key": route.get("idempotency_key"),
                "refusal_code": None if completed else "CANDIDATE_WORKSPACE_MISMATCH",
                "usage": {"cost_state": "unknown"},
            },
        }


class RevisionReplayReconciliationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "controller.db"
        self.store = MissionStore(self.path)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id="lodus-casino", repository=REMOTE))
        self.mission = self._stop_at_mismatch()

    # -- fixture ---------------------------------------------------------- #

    def payload(self):
        value = mission_payload(**{**old_revision_payload(),
                                   "execution_mode": "real",
                                   "acceptance_gate_ids": ["G"]})
        value["context_manifest_hash"] = manifest_hash_for(value)
        return value

    @staticmethod
    def key(value):
        return "%s:%s" % (value["work_item_id"], value["context_manifest_hash"])

    def _stop_at_mismatch(self) -> dict:
        value = self.payload()
        self.value = value
        mission, _ = Controller(self.store, RecoveryAdapter(),
                                lease_seconds=0).submit(value, self.key(value))
        Controller(self.store, RecoveryAdapter(),
                   lease_seconds=0).work_once("worker-1")     # STALE_HEAD
        self.store.resume_pre_provider(mission["id"], revision_binding())
        self.adapter = _MismatchAdapter()
        Controller(self.store, self.adapter,
                   lease_seconds=0).work_once("worker-2")
        stopped = self.store.get(mission["id"])
        self.assertEqual(stopped["state"], "refused")
        self.assertEqual(self.adapter.provider_runs, 1)
        return stopped

    def proof(self, **overrides):
        return reconciliation_proof(
            **{"idempotency_key": self.mission["idempotency_key"], **overrides})

    def dispatch_row(self, name=DISPATCH_STEP):
        return self.store.step_record(self.mission["id"], name)

    # -- eligibility ------------------------------------------------------ #

    def test_without_a_proof_the_mission_stays_closed(self):
        """Matrix 11: a candidate is never recovery proof by itself."""
        result = self.store.resume_pre_provider(
            self.mission["id"], revision_binding())
        self.assertFalse(result["changed"])
        self.assertEqual(self.store.get(self.mission["id"])["state"], "refused")

    def test_an_idempotency_key_unproven_stop_is_never_recoverable(self):
        """Matrix 11: only the one refusal a lookup can answer."""
        self.store.record_reconciliation(self.mission["id"], self.proof())
        with self.store.connect() as db:
            db.execute(
                "UPDATE steps SET output_json=replace(output_json,"
                "'CANDIDATE_WORKSPACE_MISMATCH','IDEMPOTENCY_KEY_UNPROVEN')"
                " WHERE mission_id=? AND name=?",
                (self.mission["id"], DISPATCH_STEP))
        with self.store.connect() as db:
            self.assertIsNone(
                self.store._pre_provider_recovery_class(db, self.mission["id"]))

    def test_a_proof_for_another_candidate_is_refused_at_recording(self):
        """Matrix 4: wrong candidate SHA fails closed."""
        with self.assertRaises(ValueError):
            self.store.record_reconciliation(
                self.mission["id"], self.proof(candidate_sha="1" * 40))

    def test_a_proof_for_another_key_base_or_project_is_refused(self):
        """Matrix 4: wrong idempotency key, base, work item or project."""
        for field, value in (("idempotency_key", "somebody-elses-key"),
                             ("revision_sha", "2" * 40),
                             ("work_item_id", "lodus-casino:revision:9"),
                             ("project_id", "another-product")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.store.record_reconciliation(
                        self.mission["id"], self.proof(**{field: value}))

    def test_a_second_different_proof_conflicts_rather_than_overwrites(self):
        self.store.record_reconciliation(self.mission["id"], self.proof())
        with self.assertRaises(ConflictError):
            self.store.record_reconciliation(
                self.mission["id"],
                self.proof(original_response_digest="a" * 64))
        self.assertEqual(
            self.store.reconciliation(self.mission["id"])[
                "original_response_digest"], "e" * 64)

    def test_recording_the_same_proof_twice_is_a_no_op(self):
        first = self.store.record_reconciliation(self.mission["id"], self.proof())
        second = self.store.record_reconciliation(self.mission["id"], self.proof())
        self.assertEqual(first, second)

    # -- the recovery itself ---------------------------------------------- #

    def _recover(self):
        self.store.record_reconciliation(self.mission["id"], self.proof())
        resumed = self.store.resume_pre_provider(
            self.mission["id"], revision_binding())
        self.assertTrue(resumed["changed"])
        return Controller(self.store, self.adapter,
                          lease_seconds=0).work_once("worker-3")

    def test_a_proof_reopens_the_mission_and_finishes_it_without_a_provider(self):
        """Matrix 7, 8 and 9 together."""
        before = self.dispatch_row()
        finished = self._recover()
        self.assertEqual(finished["state"], "completed")
        # Matrix 8: no second provider leg, and the operation performed was the
        # reconcile one -- which the Bridge refuses unless the sealed response
        # is already there to return.
        self.assertEqual(self.adapter.provider_runs, 1)
        self.assertEqual(len(self.adapter.reconcile_calls), 1)
        self.assertEqual(self.adapter.reconcile_calls[0]["reconcile_proof"],
                         PROOF_DIGEST)
        # Matrix 7: the settled row is untouched and the repaired attempt has
        # its own durable identity.
        self.assertEqual(self.dispatch_row(), before)
        self.assertIsNone(self.dispatch_row(DISPATCH_RECOVERY_STEP))
        lookup = self.dispatch_row(RECONCILE_STEP)
        self.assertIsNotNone(lookup)
        self.assertEqual(lookup["output"]["status"], "completed")

    def test_the_original_refusal_history_survives_the_recovery(self):
        """Matrix 12: the true mismatch stays visible after it is answered."""
        self._recover()
        row = self.dispatch_row()
        self.assertEqual(row["output"]["diagnostic"],
                         "CANDIDATE_WORKSPACE_MISMATCH")
        self.assertEqual(row["output"]["receipt"]["refusal_code"],
                         "CANDIDATE_WORKSPACE_MISMATCH")
        self.assertIs(row["output"]["receipt"]["process_started"], True)
        with self.store.connect() as db:
            kinds = [row["kind"] for row in db.execute(
                "SELECT kind FROM events WHERE mission_id=?",
                (self.mission["id"],))]
        self.assertIn("REVISION_REPLAY_RECONCILED", kinds)

    def test_a_repeated_recovery_starts_no_further_work(self):
        """Matrix 9: the second call is the memo, not a second lookup."""
        self._recover()
        again = self.store.resume_pre_provider(
            self.mission["id"], revision_binding())
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.adapter.reconcile_calls), 1)
        self.assertEqual(self.adapter.provider_runs, 1)

    def test_a_restart_before_the_recovery_preserves_both_records(self):
        """Matrix 10: crash between the proof and the leg it authorizes."""
        self.store.record_reconciliation(self.mission["id"], self.proof())
        reopened = MissionStore(self.path)
        self.assertEqual(reopened.reconciliation(self.mission["id"])[
            "proof_digest"], PROOF_DIGEST)
        self.assertTrue(reopened.resume_pre_provider(
            self.mission["id"], revision_binding())["changed"])
        finished = Controller(reopened, self.adapter,
                              lease_seconds=0).work_once("worker-4")
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(self.adapter.provider_runs, 1)

    def test_a_restart_after_the_recovery_preserves_both_records(self):
        """Matrix 10: the derived proof and the original both outlive a reopen."""
        self._recover()
        reopened = MissionStore(self.path)
        self.assertEqual(
            reopened.step_record(self.mission["id"], DISPATCH_STEP)[
                "output"]["diagnostic"], "CANDIDATE_WORKSPACE_MISMATCH")
        self.assertEqual(reopened.reconciliation(self.mission["id"]),
                         self.proof())

    def test_the_proof_table_is_append_only(self):
        """The trigger, not the caller, is what makes it append-only."""
        self.store.record_reconciliation(self.mission["id"], self.proof())
        for statement in (
                "UPDATE replay_reconciliations SET candidate_sha='x'",
                "DELETE FROM replay_reconciliations"):
            with self.subTest(statement=statement):
                with self.store.connect() as db:
                    with self.assertRaises(Exception):
                        db.execute(statement)

    def test_the_whole_revision_chain_is_one_mission_and_one_provider_leg(self):
        """Matrix 15: SF-162 -> 163 -> 164 -> 166 -> 167's rejection -> 168.

        Every wall this revision actually met, in order, against one work item,
        one idempotency key and one revision base -- and all three durable
        dispatch identities, because a mission that spends the recovery name on
        SF-164 must still have a free name for the reconciled lookup.  Two
        identities did not provide that, which is why there are three.
        """

        value = self.payload()
        store = MissionStore(Path(self.temp.name) / "chain.db")
        store.register_project(portfolio.ProjectPolicy(
            project_id="lodus-casino", repository=REMOTE))
        mission, _ = Controller(store, RecoveryAdapter(),
                                lease_seconds=0).submit(value, self.key(value))

        # SF-162/163: the context step refuses STALE_HEAD against the
        # registered checkout, and the rebinding is what reopens it.
        Controller(store, RecoveryAdapter(), lease_seconds=0).work_once("w1")
        self.assertEqual(store.step_record(mission["id"], "context")[
            "output"]["refusal_code"], "STALE_HEAD")
        self.assertTrue(store.resume_pre_provider(
            mission["id"], revision_binding())["changed"])

        # SF-164: the layer refuses dispatch and proves it started nothing.
        capability = CapabilityAdapter(serves=False)
        Controller(store, capability, lease_seconds=0).work_once("w2")
        self.assertEqual(store.get(mission["id"])["state"], "refused")
        self.assertTrue(store.resume_pre_provider(
            mission["id"], revision_binding())["changed"])

        # SF-166/167: a provider runs, produces a real candidate, and the
        # candidate workspace cannot be proved to be the revision checkout.
        adapter = _MismatchAdapter()
        Controller(store, adapter, lease_seconds=0).work_once("w3")
        self.assertEqual(store.get(mission["id"])["state"], "refused")
        self.assertEqual(adapter.provider_runs, 1)
        settled = store.step_record(mission["id"], DISPATCH_RECOVERY_STEP)
        self.assertEqual(settled["output"]["diagnostic"],
                         "CANDIDATE_WORKSPACE_MISMATCH")
        # SF-167's rejection: the candidate alone reopens nothing.
        self.assertFalse(store.resume_pre_provider(
            mission["id"], revision_binding())["changed"])

        # SF-168: the derived proof, and a lookup under the third identity.
        store.record_reconciliation(mission["id"], reconciliation_proof(
            idempotency_key=mission["idempotency_key"]))
        self.assertTrue(store.resume_pre_provider(
            mission["id"], revision_binding())["changed"])
        finished = Controller(store, adapter, lease_seconds=0).work_once("w4")

        self.assertEqual(finished["state"], "completed")
        self.assertEqual(adapter.provider_runs, 1)
        self.assertEqual(len(adapter.reconcile_calls), 1)
        # One mission, one identity, three settled rows, none rewritten.
        self.assertEqual(finished["payload"]["work_item_id"],
                         "lodus-casino:revision:2")
        self.assertEqual(finished["idempotency_key"], self.key(value))
        self.assertEqual(finished["payload"]["baseline_sha"], BASELINE)
        self.assertEqual(
            store.step_record(mission["id"], DISPATCH_STEP)["output"]["diagnostic"],
            "UNSUPPORTED_CAPABILITY")
        self.assertEqual(settled,
                         store.step_record(mission["id"], DISPATCH_RECOVERY_STEP))
        self.assertEqual(
            store.step_record(mission["id"], RECONCILE_STEP)["output"]["status"],
            "completed")

    def test_the_dispatch_identity_moves_only_once_the_proof_exists(self):
        """``effective_dispatch_step`` is what keeps the settled row settled."""
        rows = self.store.step_records(self.mission["id"])
        self.assertEqual(
            store_mod.effective_dispatch_step(rows)[0], DISPATCH_STEP)
        self.assertEqual(
            store_mod.effective_dispatch_step(rows, reconciled=True)[0],
            RECONCILE_STEP)


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()
