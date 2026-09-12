"""Regression coverage for provider-backed cross-lane AWE continuation."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from awe_worker.cli import _resolve_task_source
from awe_worker.credentials import MemorySecretStore, set_default_secret_store
from awe_worker.dispatch import (
    DispatchPointer,
    HeadlessDispatchResolver,
    parse_pointers,
)
from awe_worker.ledger import AWELedger
from awe_worker.model import AWEWorkItem, CompletionReport, CycleSummary, ExecutionSlot, WakeReceipt
from awe_worker.notion import AWE_LANE_FOLDERS, parse_physical_execution_profile
from awe_worker.scheduler import AWEContinuationSupervisor, AWEScheduledRunner
from awe_worker.service import ContinuationService
from awe_worker.harness import MockHarnessAdapter
from awe_worker.harness import CursorHarnessAdapter
from awe_worker.observation import MemoryTaskSource
from awe_worker.worker import AWEAutonomousWorker


def _paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "plain_text": text}]},
    }


class DispatchPageClient:
    is_configured = True

    def __init__(self, text: str, parent_id: str) -> None:
        self.text = text
        self.parent_id = parent_id

    def retrieve_block_children(self, page_id: str, **kwargs):
        return {"results": [_paragraph(line) for line in self.text.splitlines()]}

    def retrieve_page(self, page_id: str):
        return {"parent": {"type": "page_id", "page_id": self.parent_id}}


class DispatchCompatibilityTests(unittest.TestCase):
    def test_live_legacy_assignment_resolves_from_fixed_page(self):
        task_id = "3d3690f6eb1481c6a7b5d8bc01ff27b3"
        text = "\n".join(
            [
                "**EXECUTABLE — FACTORY CORE AUTONOMY FIX**",
                "## SF-263 — Autonomous Cross-Lane Continuation Activation",
                "- **Execution profile:** Codex → GPT-5.6 Luna / Max",
                f'- **Task Page:** <mention-page url="https://app.notion.com/p/{task_id}"/>',
                "- **Physical state:** In Progress",
            ]
        )
        client = DispatchPageClient(
            text,
            AWE_LANE_FOLDERS["codex"]["in_progress"],
        )
        result = HeadlessDispatchResolver(client=client).resolve("codex")
        self.assertTrue(result.ok)
        self.assertEqual(result.pointer.task_id, "SF-263")
        self.assertEqual(result.pointer.slot.key, "codex/gpt-5.6-luna/max")
        self.assertEqual(result.pointer.expected_lifecycle_state, "In Progress")

    def test_legacy_parser_still_requires_complete_binding_facts(self):
        pointers = parse_pointers(
            "\n".join(
                [
                    "**EXECUTABLE — incomplete**",
                    "## SF-263 — missing profile",
                    "- **Physical state:** In Progress",
                ]
            ),
            harness="codex",
        )
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0].execution_profile, "")
        self.assertEqual(pointers[0].task_page_url, "")

    def test_physical_profile_can_use_split_explicit_fields(self):
        text = "\n".join(
            [
                "## Execution profile",
                "- **Lane:** Codex",
                "- **Model / Effort:** GPT-5.6 Luna / Max",
            ]
        )
        self.assertEqual(
            parse_physical_execution_profile(text),
            "Codex -> GPT-5.6 Luna / Max",
        )


class SequenceProvider:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = 0
        self.last_errors = {}

    def observe_slots(self):
        self.calls += 1
        index = min(self.calls - 1, len(self.observations) - 1)
        return self.observations[index]

    def diagnostics(self):
        return {"errors": self.last_errors}


class FakeWorker:
    def __init__(self, ledger: AWELedger, settled_key: str = "", settled_keys=None):
        self.ledger = ledger
        self.settled_keys = set(settled_keys or ())
        if settled_key:
            self.settled_keys.add(settled_key)
        self.calls = []

    def run_cycle(self, worker_id, target_slot, dry_run=False):
        self.calls.append(target_slot.key)
        wake = WakeReceipt(
            success=True,
            harness=target_slot.harness,
            slot_key=target_slot.key,
        )
        completion = None
        if target_slot.key in self.settled_keys:
            completion = CompletionReport(state="DONE", task_id="SF-263")
        return CycleSummary(
            worker_id=worker_id,
            cycle_id=f"cycle-{len(self.calls)}",
            observed_tasks=1,
            claimed_task="SF-263",
            grounding_source="test",
            wake_receipt=wake,
            completion_report=completion,
            gate_decision=None,
            escalation=None,
            health="healthy",
            detail="test",
        )


class ContinuationSupervisorTests(unittest.TestCase):
    def test_cross_lane_happy_path_reenters_without_owner_command(self):
        cursor = ExecutionSlot("cursor", "grok-4.6", "high")
        codex = ExecutionSlot("codex", "gpt-5.6-luna", "max")
        antigravity = ExecutionSlot("antigravity", "gemini-3.8-flash", "high")
        # The changing observations model an upstream integration checkpoint
        # publishing the next exact Dispatch binding after each ACCEPT.  The
        # supervisor only re-observes and wakes; no Owner command is involved.
        provider = SequenceProvider([[cursor], [codex], [antigravity], []])
        worker = FakeWorker(
            AWELedger(":memory:"),
            settled_keys={cursor.key, codex.key, antigravity.key},
        )
        runner = AWEContinuationSupervisor(
            worker=worker,
            worker_id="happy-path-test",
            slot_provider=provider,
            settlement_rechecks=1,
        )

        summaries = runner.run(interval_seconds=0, max_cycles=2, dry_run=False)

        self.assertEqual(worker.calls, [cursor.key, codex.key, antigravity.key])
        self.assertEqual([summary.claimed_task for summary in summaries], ["SF-263"] * 3)
        self.assertEqual(provider.calls, 4)

    def test_one_poll_handles_multiple_slots_and_one_settlement_recheck(self):
        first = ExecutionSlot("codex", "gpt-5.6-luna", "max")
        second = ExecutionSlot("antigravity", "gemini-3.8-flash", "high")
        downstream = ExecutionSlot("cursor", "grok-4.6", "high")
        provider = SequenceProvider([[first, second], [downstream]])
        worker = FakeWorker(AWELedger(":memory:"), first.key)
        runner = AWEScheduledRunner(
            worker=worker,
            worker_id="continuation-test",
            slot_provider=provider,
            settlement_rechecks=1,
        )

        summaries = runner.run(interval_seconds=0, max_cycles=1, dry_run=False)

        self.assertEqual(provider.calls, 2)
        self.assertEqual(worker.calls, [first.key, second.key, downstream.key])
        self.assertEqual(len(summaries), 3)
        self.assertEqual(
            runner.get_runtime_status().last_wake_by_harness["cursor"]["slot_key"],
            downstream.key,
        )

    def test_projection_error_is_durable_and_never_wakes_a_harness(self):
        provider = SequenceProvider([[]])
        provider.last_errors = {"cursor": "HARNESS_WAKE_PATH_UNAVAILABLE:cursor"}
        worker = FakeWorker(AWELedger(":memory:"), "never")
        runner = AWEScheduledRunner(
            worker=worker,
            worker_id="cursor-unavailable-test",
            slot_provider=provider,
        )

        summaries = runner.run(interval_seconds=0, max_cycles=1)
        runtime = runner.get_runtime_status()

        self.assertEqual(summaries, [])
        self.assertEqual(worker.calls, [])
        self.assertEqual(runtime.work_state, "projection_refused")
        self.assertIn("HARNESS_WAKE_PATH_UNAVAILABLE:cursor", runtime.last_error)

    def test_owner_gated_item_is_reported_without_a_harness_wake(self):
        task = AWEWorkItem(
            task_id="SF-999",
            title="SF-999 — Owner decision",
            lane="Codex",
            role="Owner",
            status="Queue",
            model="GPT-5.6 Luna",
            effort="Max",
            sequence=999,
            owner_only=True,
            owner_reason="owner_judgment",
        )
        adapter = MockHarnessAdapter("codex")
        worker = AWEAutonomousWorker(
            ledger=AWELedger(":memory:"),
            source=MemoryTaskSource([task]),
            harness_adapters={"codex": adapter},
        )
        summary = worker.run_cycle(
            worker_id="owner-gate-test",
            target_slot=task.slot,
        )

        self.assertEqual(summary.health, "escalated")
        self.assertEqual(summary.escalation.reason_code, "owner_judgment")
        self.assertEqual(adapter.woken_tasks, [])

    def test_authenticated_cursor_uses_the_headless_command(self):
        task = AWEWorkItem(
            task_id="SF-998",
            title="SF-998",
            lane="Cursor",
            role="Developer",
            status="In Progress",
            model="Grok 4.6",
            effort="High",
            sequence=998,
        )
        adapter = CursorHarnessAdapter(cursor_bin="/bin/true")
        fake_process = type("Process", (), {"pid": 4242})()
        with patch.object(adapter, "check_availability", return_value=(True, "HARNESS_READY", "ok")):
            with patch("awe_worker.harness.subprocess.Popen", return_value=fake_process) as popen:
                receipt = adapter.wake(task)
        self.assertTrue(receipt.success)
        self.assertEqual(receipt.pid, 4242)
        self.assertEqual(receipt.command[-2:], ["-p", "Process your Queue."])
        popen.assert_called_once()


class LiveSourceSafetyTests(unittest.TestCase):
    def test_notion_source_never_falls_back_to_fixture_directory(self):
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            source, sor = _resolve_task_source(
                "notion",
                "tests/fixtures/work_exchange",
                "database-id",
            )
        self.assertEqual(source.__class__.__name__, "LiveNotionTaskSource")
        self.assertIsNotNone(sor)
        self.assertEqual(source.fetch_tasks(), [])


class ServiceLifecycleTests(unittest.TestCase):
    def test_install_is_idempotent_and_does_not_start_a_process_or_store_tokens(self):
        from awe_worker.credentials import MemorySecretStore, set_default_secret_store

        set_default_secret_store(MemorySecretStore())
        try:
            with patch.dict(os.environ, {"NOTION_TOKEN": ""}, clear=False):
                with tempfile.TemporaryDirectory() as tmp:
                    service = ContinuationService(
                        db_path="/tmp/sf263-worker.db",
                        state_dir=tmp,
                    )
                    command = ["python", "-m", "awe_worker.cli", "supervisor", "run", "--live"]
                    first = service.install(
                        command,
                        working_dir=tmp,
                        interval_seconds=10,
                        apply=True,
                        now=1.0,
                    )
                    second = service.install(
                        command,
                        working_dir=tmp,
                        interval_seconds=10,
                        apply=True,
                        now=2.0,
                    )
                    self.assertEqual(first["outcome"], "installed")
                    self.assertEqual(second["outcome"], "unchanged")
                    self.assertFalse(service.status()["pid"])
                    manifest_text = service.manifest_path.read_text()
                    self.assertNotIn("NOTION_TOKEN", manifest_text)
                    self.assertNotIn("secret_", manifest_text)
                    self.assertIn("env_then_keychain", manifest_text)
                    self.assertEqual(
                        first["manifest"]["credential_provider"],
                        "env_then_keychain",
                    )
                    self.assertNotIn("secret", first["credential"])
                    self.assertFalse(first["credential"]["configured"])
        finally:
            set_default_secret_store(None)

    def test_duplicate_start_remains_fenced_and_does_not_rewrite_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemorySecretStore(initial="keep-me")
            set_default_secret_store(store)
            try:
                service = ContinuationService(db_path=str(Path(tmp) / "w.db"), state_dir=tmp)
                service.install(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    working_dir=tmp,
                    interval_seconds=1,
                    apply=True,
                )
                first = service.start()
                second = service.start()
                self.assertTrue(first["ok"], first)
                self.assertEqual(second["code"], "SERVICE_ALREADY_RUNNING")
                self.assertEqual(store.get()[0], "keep-me")
                service.stop()
                pid = int(first.get("pid") or 0)
                deadline = time.monotonic() + 2
                while pid and service._pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                set_default_secret_store(None)

    def test_install_refuses_notion_token_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ContinuationService(db_path="/tmp/sf267-worker.db", state_dir=tmp)
            with self.assertRaises(Exception) as raised:
                service.install(
                    ["python", "-m", "awe_worker.cli", "--notion-token", "should-not-be-here"],
                    working_dir=tmp,
                    interval_seconds=10,
                    apply=True,
                )
            self.assertIn("notion-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
