"""Factory-maintenance work intake: what may be admitted, claimed, and repeated.

The happy path exists so the safety tests are not vacuously true: a real
directory packet must become a Controller mission and complete without a
person typing a wake-up command.  Everything else states an impossibility.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path

from factory_controller import portfolio, supervisor, work_intake, work_source
from factory_controller.cli import main as cli_main
from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore
from factory_controller.work_source import PACKET_SCHEMA, DirectoryWorkSource, load_packet

from tests.support import ALPHA, BETA, Clock, LayerAdapter
from tests.test_authority_boundaries import code_text

MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "work_intake.py"
SOURCE_MODULE = Path(__file__).resolve().parent.parent / "factory_controller" / "work_source.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "work_exchange"

PROJECT = "factory"
REPO = "https://example.invalid/factory.git"
GATES = ["G-BUILD"]
CANDIDATES = [{"profile": ALPHA, "capabilities": ["implement"]},
              {"profile": BETA, "capabilities": ["implement"]}]
WORK_ITEM = "factory-maintenance:SF-196"


def packet_body(work_item_id=WORK_ITEM, sequence=196, *, owner_only=False,
                owner_reason="not_applicable", blocked=False, **payload_extra):
    payload = {"work_item_id": work_item_id, "project_id": PROJECT,
               "execution_mode": "fixture", "acceptance_gate_ids": GATES,
               "provider_candidates": CANDIDATES}
    payload.update(payload_extra)
    body = {"schema_version": PACKET_SCHEMA, "work_item_id": work_item_id,
            "lineage_id": work_item_id, "sequence": sequence,
            "owner_only": owner_only, "owner_reason": owner_reason,
            "blocked": blocked, "payload": {} if owner_only else payload}
    if owner_only:
        body["payload"] = payload_extra.get("payload", {})
    return body


def packet(**kwargs):
    return load_packet(packet_body(**kwargs), source_kind="memory",
                       source_ref="memory")


def write_authority(root, revision="work-exchange:test"):
    (Path(root) / "authority.json").write_text(json.dumps({
        "schema_version": work_source.AUTHORITY_SCHEMA,
        "granted_by": "owner_policy",
        "source": "scheduled_inbox",
        "prompt": False,
        "source_revision": revision,
    }))


class MemorySource:
    def __init__(self, items):
        self.items = list(items)

    def packets(self):
        return tuple(self.items)


class IntakeCase(unittest.TestCase):
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
        self.plane = work_intake.WorkIntakePlane(self.store, clock=self.clock)
        self.store.register_project(portfolio.ProjectPolicy(
            project_id=PROJECT, repository=REPO, state="enabled",
            priority=100, concurrency_cap=4, acceptance_gate_ids=("suite",),
            acceptance_gate_source="repo://factory@baseline:dev",
            policy_version="1.0"))

    def ingest(self, *packets):
        return self.plane.observe(MemorySource(packets))


class TerminationTests(unittest.TestCase):
    def test_the_cycle_never_sleeps(self):
        self.assertNotIn("sleep", code_text(MODULE.read_text()))

    def test_a_cycle_cannot_reach_itself(self):
        tree = ast.parse(MODULE.read_text())
        graph = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            called = set()
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and isinstance(inner.func.value, ast.Name)
                        and inner.func.value.id == "self"):
                    called.add(inner.func.attr)
            graph.setdefault(node.name, set()).update(called)
        seen = set()
        stack = ["cycle"]
        while stack:
            name = stack.pop()
            for called in graph.get(name, ()):
                if called not in seen:
                    seen.add(called)
                    stack.append(called)
        self.assertNotIn("cycle", seen)

    def test_the_plane_declares_no_production_verb(self):
        text = MODULE.read_text() + SOURCE_MODULE.read_text()
        for token in ("def approve", "def deploy", "def rollback",
                      "def promote_to", "def register_environment"):
            self.assertNotIn(token, text)


class PacketTests(unittest.TestCase):
    def test_a_prompt_shaped_field_has_nowhere_to_go(self):
        signature = inspect.signature(load_packet)
        self.assertEqual(list(signature.parameters), ["raw", "source_kind", "source_ref"])

    def test_an_owner_only_packet_without_a_known_reason_is_refused(self):
        body = packet_body(owner_only=True, owner_reason="please_ship_it")
        with self.assertRaises(work_source.PacketError) as raised:
            load_packet(body, source_kind="memory", source_ref="bad.json")
        self.assertEqual(raised.exception.code, "WORK_PACKET_OWNER_REASON_UNKNOWN")

    def test_a_directory_lists_packets_in_sequence_order(self):
        root = Path(tempfile.mkdtemp())
        write_authority(root)
        later = packet_body("factory-maintenance:B", sequence=2)
        earlier = packet_body("factory-maintenance:A", sequence=1)
        (root / "z.json").write_text(json.dumps(later))
        (root / "a.json").write_text(json.dumps(earlier))
        loaded = DirectoryWorkSource(root).packets()
        self.assertEqual([item.work_item_id for item in loaded],
                         ["factory-maintenance:A", "factory-maintenance:B"])
        self.assertEqual(loaded[0].source_revision, "work-exchange:test")

    def test_a_directory_without_authority_is_refused(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.json").write_text(json.dumps(packet_body()))
        with self.assertRaises(work_source.PacketError) as raised:
            DirectoryWorkSource(root).packets()
        self.assertEqual(raised.exception.code, "MANAGEMENT_SOURCE_UNAUTHORIZED")

    def test_duplicate_identities_in_one_directory_are_refused(self):
        root = Path(tempfile.mkdtemp())
        write_authority(root)
        (root / "a.json").write_text(json.dumps(packet_body()))
        (root / "b.json").write_text(json.dumps(packet_body()))
        with self.assertRaises(work_source.PacketError) as raised:
            DirectoryWorkSource(root).packets()
        self.assertEqual(raised.exception.code, "WORK_SOURCE_DUPLICATE_IDENTITY")


class AdmissionTests(IntakeCase):
    def test_observe_does_not_submit(self):
        self.ingest(packet())
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "queued")
        self.assertEqual(self.store.counts().get("admitted", 0), 0)

    def test_a_blocked_packet_is_not_eligible(self):
        self.ingest(packet(blocked=True))
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "blocked")
        self.assertEqual(self.plane.eligible(), ())

    def test_unblocking_the_same_packet_resumes_the_same_identity(self):
        self.ingest(packet(blocked=True))
        self.ingest(packet(blocked=False))
        row = self.plane.item(WORK_ITEM)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["lineage_id"], WORK_ITEM)
        self.assertEqual(len(self.plane.items()), 1)

    def test_a_second_identity_on_the_same_lineage_is_refused(self):
        self.ingest(packet())
        other = load_packet(
            packet_body("factory-maintenance:SF-196-b", sequence=197),
            source_kind="memory", source_ref="other")
        # Force the colliding lineage.
        other = work_source.WorkPacket(
            work_item_id=other.work_item_id, lineage_id=WORK_ITEM,
            sequence=other.sequence, source_kind="memory", source_ref="other",
            owner_only=False, owner_reason="not_applicable", blocked=False,
            payload=other.payload)
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.ingest(other)
        self.assertEqual(raised.exception.code, "WORK_INTAKE_LINEAGE_COLLISION")

    def test_owner_only_work_never_becomes_a_mission(self):
        self.ingest(packet(owner_only=True, owner_reason="production_promotion"))
        claimed = self.plane.claim(WORK_ITEM, "w1")
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.plane.admit(WORK_ITEM, self.controller)
        self.assertEqual(raised.exception.code, "WORK_INTAKE_OWNER_REQUIRED")
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "owner_required")
        self.assertIsNone(claimed["mission_ref"] if claimed else None)
        self.assertEqual(self.store.counts().get("admitted", 0), 0)

    def test_an_emergency_stop_refuses_admission(self):
        self.ingest(packet())
        self.plane.claim(WORK_ITEM, "w1")
        self.store.emergency_stop(True)
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.plane.admit(WORK_ITEM, self.controller)
        self.assertEqual(raised.exception.code, "WORK_INTAKE_EMERGENCY_STOP")


class ClaimTests(IntakeCase):
    def test_two_workers_cannot_claim_the_same_item(self):
        self.ingest(packet())
        first = self.plane.claim(WORK_ITEM, "alpha")
        second = self.plane.claim(WORK_ITEM, "beta")
        self.assertEqual(first["claimed_by"], "alpha")
        self.assertIsNone(second)

    def test_the_same_worker_may_retry_a_live_claim(self):
        self.ingest(packet())
        first = self.plane.claim(WORK_ITEM, "alpha")
        again = self.plane.claim(WORK_ITEM, "alpha")
        self.assertEqual(first["claim_token"], again["claim_token"])

    def test_concurrent_claims_leave_exactly_one_winner(self):
        self.ingest(packet())
        winners = []
        barrier = threading.Barrier(2)

        def attempt(name):
            barrier.wait()
            winners.append(self.plane.claim(WORK_ITEM, name))

        threads = [threading.Thread(target=attempt, args=("a",)),
                   threading.Thread(target=attempt, args=("b",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        held = [row for row in winners if row is not None]
        self.assertEqual(len(held), 1)
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "claimed")

    def test_an_overlapping_cycle_is_refused(self):
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO work_intake_cycles (cycle_id, sequence, worker_id,"
                " lease_expires_at, started_at) VALUES ('open', 1, 'ghost', ?, ?)",
                (self.clock.now + 100, self.clock.now))
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.plane.cycle("w1", controller=self.controller)
        self.assertEqual(raised.exception.code, "WORK_INTAKE_CYCLE_IN_FLIGHT")


class ExecutionTests(IntakeCase):
    def test_a_real_factory_maintenance_packet_completes_without_a_wake_command(self):
        source = DirectoryWorkSource(FIXTURE_DIR)
        report = self.plane.cycle("worker", source=source, controller=self.controller)
        self.assertEqual(report["admitted"], WORK_ITEM)
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "done")
        mission_ref = self.plane.item(WORK_ITEM)["mission_ref"]
        self.assertEqual(self.store.get(mission_ref)["state"], "completed")
        self.assertEqual(len(self.adapter.dispatches), 1)

    def test_supervisor_advances_an_admitted_packet_without_the_intake_executing(self):
        self.ingest(packet())
        self.plane.claim(WORK_ITEM, "w1")
        admitted = self.plane.admit(WORK_ITEM, self.controller)
        ops = supervisor.OperationsSupervisor(self.controller, clock=self.clock)
        ops.set_policy(supervisor.SupervisorPolicy(
            project_id=PROJECT, policy_version="sp-1"))
        ops.transition("running", actor="owner", reason="start")
        report = ops.cycle("supervisor")
        self.assertGreaterEqual(report["missions_advanced"], 1)
        self.assertEqual(self.store.get(admitted["mission"]["id"])["state"],
                         "completed")
        self.plane.reconcile(WORK_ITEM, self.controller)
        self.assertEqual(self.plane.item(WORK_ITEM)["state"], "done")

    def test_a_done_item_is_not_executed_again(self):
        source = DirectoryWorkSource(FIXTURE_DIR)
        self.plane.cycle("w1", source=source, controller=self.controller)
        report = self.plane.cycle("w2", source=source, controller=self.controller)
        self.assertEqual(report["reason"], "WORK_INTAKE_EMPTY")
        self.assertEqual(len(self.adapter.dispatches), 1)
        self.assertEqual(len(self.plane.items("done")), 1)

    def test_admit_is_idempotent_under_retry(self):
        self.ingest(packet())
        self.plane.claim(WORK_ITEM, "w1")
        first = self.plane.admit(WORK_ITEM, self.controller)
        second = self.plane.admit(WORK_ITEM, self.controller)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["mission"]["id"], second["mission"]["id"])
        self.assertEqual(self.store.counts()["admitted"], 1)

    def test_a_cycle_surfaces_owner_only_work_and_stops(self):
        self.ingest(packet(owner_only=True, owner_reason="billing"))
        report = self.plane.cycle("w1", controller=self.controller)
        self.assertEqual(report["reason"], "WORK_INTAKE_OWNER_REQUIRED")
        self.assertEqual(report["next_action"], "OWNER_REQUIRED")
        self.assertEqual(report["owner_required"][0]["owner_reason"], "billing")
        self.assertEqual(self.store.counts().get("admitted", 0), 0)

    def test_lowest_numbered_work_runs_first(self):
        self.ingest(packet(work_item_id="factory-maintenance:B", sequence=2),
                    packet(work_item_id="factory-maintenance:A", sequence=1))
        report = self.plane.cycle("w1", controller=self.controller)
        self.assertEqual(report["admitted"], "factory-maintenance:A")
        self.assertEqual(report["next_action"], "WORK_INTAKE_CONTINUE")


class AbsenceLiteralTests(unittest.TestCase):
    def test_absence_words_match_the_other_layers(self):
        from factory_controller import maintenance, production
        self.assertEqual(work_source.CANONICAL_ABSENCE, production.CANONICAL_ABSENCE)
        self.assertEqual(work_source.CANONICAL_ABSENCE, maintenance.CANONICAL_ABSENCE)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "controller.db")

    def run_cli(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--db", self.db, *argv])
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None)

    def test_observe_and_cycle_through_the_owner_surface(self):
        self.run_cli("project", "register", "--id", PROJECT, "--repository", REPO)
        code, observed = self.run_cli(
            "work-intake", "observe", "--source-dir", str(FIXTURE_DIR))
        self.assertEqual(code, 0)
        self.assertEqual(observed[0]["work_item_id"], WORK_ITEM)
        code, report = self.run_cli(
            "work-intake", "cycle", "--worker", "cli",
            "--source-dir", str(FIXTURE_DIR))
        self.assertEqual(code, 0)
        self.assertEqual(report["admitted"], WORK_ITEM)
        code, item = self.run_cli("work-intake", "status", WORK_ITEM)
        self.assertEqual(item["state"], "done")


class IntegrityTests(IntakeCase):
    def test_duplicate_identities_in_one_snapshot_are_refused(self):
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.plane.observe(MemorySource([packet(), packet()]))
        self.assertEqual(raised.exception.code, "WORK_INTAKE_DUPLICATE_IDENTITY")

    def test_observed_payload_identity_is_immutable(self):
        self.ingest(packet())
        changed = packet_body()
        changed["payload"]["priority"] = 9
        with self.assertRaises(work_intake.WorkIntakeRefusal) as raised:
            self.plane.observe(MemorySource([load_packet(
                changed, source_kind="memory", source_ref="later")]))
        self.assertEqual(raised.exception.code, "WORK_INTAKE_PACKET_IMMUTABLE")

    def test_a_stale_cycle_close_cannot_rewrite_a_recovered_cycle(self):
        claim = self.plane._claim_cycle("a", 1)
        self.clock.advance(2)
        recovered = self.plane._claim_cycle("b", 30)
        stale = work_intake.CycleReport(
            cycle_id=claim["cycle_id"], sequence=claim["sequence"],
            outcome="completed", started_at=claim["started_at"],
            ended_at=claim["started_at"], lease_token=claim["lease_token"])
        row = self.plane._close_cycle(stale)
        self.assertEqual(row["reason"], "WORK_INTAKE_STALE_CYCLE")
        with self.store.transaction() as db:
            first = db.execute(
                "SELECT outcome FROM work_intake_cycles WHERE cycle_id=?",
                (claim["cycle_id"],)).fetchone()
        self.assertEqual(first["outcome"], "idle")
        self.assertEqual(recovered["cycle_id"] != claim["cycle_id"], True)

    def test_cycle_execution_is_bound_to_the_claimed_item(self):
        old, _ = self.controller.submit(
            {"work_item_id": "old", "project_id": PROJECT,
             "execution_mode": "fixture", "acceptance_gate_ids": GATES,
             "provider_candidates": CANDIDATES}, "old-key")
        self.ingest(packet())
        report = self.plane.cycle("worker", controller=self.controller)
        self.assertEqual(report["admitted"], WORK_ITEM)
        advanced = report["advanced"]
        self.assertEqual(len(advanced), 1)
        self.assertEqual(advanced[0]["work_item_id"], WORK_ITEM)
        self.assertNotEqual(advanced[0]["mission_id"], old["id"])
        self.assertEqual(self.store.get(old["id"])["state"], "admitted")


if __name__ == "__main__":
    unittest.main()
