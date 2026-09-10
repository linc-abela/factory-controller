"""Focused tests for the read-only AWE projection startup diagnostic.

Baseline (before this command): HeadlessDispatchResolver compares a Dispatch
pointer to physical ancestry for one task, but it does not inspect Dashboard
Current/Status/model assignment. Duplicate Current rows and Dashboard-vs-physical
status drift can therefore survive until a later Checkpoint reconciliation.

These fixtures lock the four SF-238 cases: stale EXECUTABLE after Review,
duplicate Current on one harness, Dashboard status mismatch, and a clean PASS.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from awe_worker.cli import main
from awe_worker.ledger import AWELedger
from awe_worker.model import AWEWorkItem, ExecutionSlot
from awe_worker.observation import MemoryTaskSource
from awe_worker.projection import (
    DASHBOARD_STATUS_MISMATCH,
    DUPLICATE_DASHBOARD_CURRENT,
    FAIL_CLOSED,
    NO_CURRENT_POINTER,
    NO_EXECUTABLE_TASK,
    PASS,
    SELECTED_TASK_MISMATCH,
    STALE_DISPATCH_EXECUTABLE,
    UNKNOWN_PHYSICAL_TRUTH,
    UNKNOWN_PROJECTION_SNAPSHOT,
    diagnose_projection,
    diagnose_snapshot,
    evaluate_claim_preflight,
)
from awe_worker.worker import AWEAutonomousWorker
from awe_worker.harness import MockHarnessAdapter


def _physical_review():
    return {
        "task_id": "SF-236",
        "lane": "Antigravity",
        "status": "Review",
        "execution_profile": "Antigravity → Gemini 3.1 Flash / High",
    }


def _dashboard_queue_current():
    return {
        "task_id": "SF-236",
        "status": "Queue",
        "current": True,
        "lane": "Antigravity",
        "model_effort": "Antigravity → Gemini 3.1 Flash / High",
    }


def _dispatch_executable(task_id="SF-236", lifecycle="Queue"):
    return {
        "harness": "antigravity",
        "dispatch_state": "EXECUTABLE",
        "task_id": task_id,
        "expected_lifecycle_state": lifecycle,
        "execution_profile": "Antigravity → Gemini 3.1 Flash / High",
    }


class ProjectionDiagnosticTests(unittest.TestCase):
    def test_stale_dispatch_executable_on_physical_review(self):
        report = diagnose_snapshot(
            {
                "physical": [_physical_review()],
                "dashboard": [_dashboard_queue_current()],
                "dispatch": _dispatch_executable(),
            }
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        codes = {finding.code for finding in report.findings}
        self.assertIn(STALE_DISPATCH_EXECUTABLE, codes)
        self.assertIn(DASHBOARD_STATUS_MISMATCH, codes)
        self.assertFalse(report.ok)
        self.assertIn("Do not claim work", report.as_text())

    def test_duplicate_dashboard_current_for_one_harness(self):
        report = diagnose_projection(
            physical=[
                {
                    "task_id": "SF-229",
                    "lane": "Codex",
                    "status": "Queue",
                    "execution_profile": "Codex → GPT-5.6 Sol / High",
                },
                {
                    "task_id": "SF-230",
                    "lane": "Codex",
                    "status": "In Progress",
                    "execution_profile": "Codex → GPT-5.6 Luna / Max",
                },
            ],
            dashboard=[
                {
                    "task_id": "SF-229",
                    "status": "Queue",
                    "current": True,
                    "lane": "Codex",
                    "model_effort": "Codex → GPT-5.6 Sol / High",
                },
                {
                    "task_id": "SF-230",
                    "status": "In Progress",
                    "current": True,
                    "lane": "Codex",
                    "model_effort": "Codex → GPT-5.6 Luna / Max",
                },
            ],
            dispatch={
                "harness": "codex",
                "dispatch_state": "EXECUTABLE",
                "task_id": "SF-229",
                "expected_lifecycle_state": "Queue",
                "execution_profile": "Codex → GPT-5.6 Sol / High",
            },
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        dup = [finding for finding in report.findings if finding.code == DUPLICATE_DASHBOARD_CURRENT]
        self.assertEqual(len(dup), 1)
        self.assertIn("codex", dup[0].detail.lower())
        self.assertIn("SF-229", dup[0].detail)
        self.assertIn("SF-230", dup[0].detail)

    def test_dashboard_status_disagrees_with_physical(self):
        report = diagnose_snapshot(
            {
                "physical": [
                    {
                        "task_id": "SF-238",
                        "lane": "Cursor",
                        "status": "In Progress",
                        "execution_profile": "Cursor → Grok 4.6 / Medium",
                    }
                ],
                "dashboard": [
                    {
                        "task_id": "SF-238",
                        "status": "Queue",
                        "current": True,
                        "lane": "Cursor",
                        "model_effort": "Cursor → Grok 4.6 / Medium",
                    }
                ],
                "dispatch": {
                    "harness": "cursor",
                    "dispatch_state": "EXECUTABLE",
                    "task_id": "SF-238",
                    "expected_lifecycle_state": "In Progress",
                    "execution_profile": "Cursor → Grok 4.6 / Medium",
                },
            }
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertEqual(
            [finding.code for finding in report.findings],
            [DASHBOARD_STATUS_MISMATCH],
        )

    def test_clean_consistent_state_passes(self):
        report = diagnose_snapshot(
            {
                "physical": [
                    {
                        "task_id": "SF-238",
                        "lane": "Cursor",
                        "status": "Queue",
                        "execution_profile": "Cursor → Grok 4.6 / Medium",
                    }
                ],
                "dashboard": [
                    {
                        "task_id": "SF-238",
                        "status": "Queue",
                        "current": True,
                        "lane": "Cursor",
                        "model_effort": "Cursor → Grok 4.6 / Medium",
                    }
                ],
                "dispatch": {
                    "harness": "cursor",
                    "dispatch_state": "EXECUTABLE",
                    "task_id": "SF-238",
                    "expected_lifecycle_state": "Queue",
                    "execution_profile": "Cursor → Grok 4.6 / Medium",
                },
            }
        )
        self.assertEqual(report.verdict, PASS)
        self.assertTrue(report.ok)
        self.assertEqual(report.findings, ())
        payload = report.as_dict()
        self.assertFalse(payload["mutates"])
        self.assertEqual(payload["authoritative"], "physical_awe")

    def test_cli_exits_nonzero_on_drift_and_does_not_claim(self):
        snapshot = {
            "physical": [_physical_review()],
            "dashboard": [_dashboard_queue_current()],
            "dispatch": _dispatch_executable(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("awe_worker.cli.AWELedger") as ledger, \
                    patch("sys.stdout", stdout), \
                    patch("sys.stderr", stderr):
                code = main(["diagnose-projection", "--snapshot", str(path)])
            ledger.assert_not_called()
            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["mutates"])
            self.assertIn(STALE_DISPATCH_EXECUTABLE, {item["code"] for item in payload["findings"]})

    def test_cli_passes_clean_snapshot(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-238",
                    "lane": "Cursor",
                    "status": "Queue",
                    "execution_profile": "Cursor → Grok 4.6 / Medium",
                }
            ],
            "dashboard": [
                {
                    "task_id": "SF-238",
                    "status": "Queue",
                    "current": True,
                    "lane": "Cursor",
                    "model_effort": "Cursor → Grok 4.6 / Medium",
                }
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "EXECUTABLE",
                "task_id": "SF-238",
                "expected_lifecycle_state": "Queue",
                "execution_profile": "Cursor → Grok 4.6 / Medium",
            },
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(snapshot))), \
                patch("sys.stdout", stdout), \
                patch("sys.stderr", stderr):
            code = main(["diagnose-projection", "--snapshot", "-"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdict"], PASS)

    def test_current_dashboard_without_physical_fails_closed(self):
        report = diagnose_snapshot(
            {
                "physical": [],
                "dashboard": [_dashboard_queue_current()],
                "dispatch": {
                    "harness": "antigravity",
                    "dispatch_state": "NO_EXECUTABLE_TASK",
                    "task_id": "",
                },
            }
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertIn(UNKNOWN_PHYSICAL_TRUTH, {finding.code for finding in report.findings})

    def test_executable_dispatch_without_physical_fails_closed(self):
        report = diagnose_snapshot(
            {
                "physical": [],
                "dashboard": [],
                "dispatch": _dispatch_executable(),
            }
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertIn(UNKNOWN_PHYSICAL_TRUTH, {finding.code for finding in report.findings})

    def test_missing_snapshot_fails_closed_before_claim(self):
        report = evaluate_claim_preflight(None)
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertEqual(report.findings[0].code, UNKNOWN_PROJECTION_SNAPSHOT)

    def test_cli_claim_without_snapshot_does_not_claim(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("awe_worker.cli.AWELedger") as ledger_cls, \
                patch("sys.stdout", stdout), \
                patch("sys.stderr", stderr):
            code = main(
                [
                    "claim",
                    "--task-id",
                    "SF-238",
                    "--slot",
                    "cursor/grok-4.6/medium",
                ]
            )
        ledger_cls.return_value.claim.assert_not_called()
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["verdict"], FAIL_CLOSED)
        self.assertIn(UNKNOWN_PROJECTION_SNAPSHOT, {item["code"] for item in payload["findings"]})

    def test_cli_claim_rejects_unknown_physical_without_ledger_claim(self):
        snapshot = {
            "physical": [],
            "dashboard": [_dashboard_queue_current()],
            "dispatch": _dispatch_executable(),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with patch("awe_worker.cli.AWELedger") as ledger_cls, \
                    patch("sys.stdout", stdout), \
                    patch("sys.stderr", stderr):
                code = main(
                    [
                        "claim",
                        "--task-id",
                        "SF-236",
                        "--slot",
                        "antigravity/gemini-3.1-flash/high",
                        "--snapshot",
                        str(path),
                    ]
                )
        ledger_cls.return_value.claim.assert_not_called()
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertIn(UNKNOWN_PHYSICAL_TRUTH, {item["code"] for item in payload["findings"]})

    def test_worker_cycle_does_not_claim_on_projection_drift(self):
        task = AWEWorkItem(
            task_id="SF-236",
            title="stale",
            lane="Antigravity",
            role="developer",
            status="Queue",
            model="Gemini 3.1 Flash",
            effort="High",
            sequence=236,
        )
        source = MemoryTaskSource(
            [task],
            projection_snapshot={
                "physical": [_physical_review()],
                "dashboard": [_dashboard_queue_current()],
                "dispatch": _dispatch_executable(),
            },
        )
        ledger = AWELedger(":memory:")
        sor = MagicMock()
        adapter = MockHarnessAdapter("antigravity")
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=source,
            source_of_record=sor,
            harness_adapters={"antigravity": adapter},
            target_repo=".",
        )
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.1-flash", effort="high")
        summary = worker.run_cycle(worker_id="w1", target_slot=slot, dry_run=False)
        self.assertEqual(summary.health, "refused")
        self.assertIn("PROJECTION_FAIL_CLOSED", summary.detail)
        self.assertIsNone(summary.claimed_task)
        self.assertIsNone(ledger.get_claim("SF-236"))
        sor.claim_task.assert_not_called()
        self.assertEqual(adapter.woken_tasks, [])

    def test_worker_cycle_does_not_claim_on_unknown_physical_truth(self):
        task = AWEWorkItem(
            task_id="SF-236",
            title="missing physical",
            lane="Antigravity",
            role="developer",
            status="Queue",
            model="Gemini 3.1 Flash",
            effort="High",
            sequence=236,
        )
        source = MemoryTaskSource(
            [task],
            projection_snapshot={
                "physical": [],
                "dashboard": [_dashboard_queue_current()],
                "dispatch": _dispatch_executable(),
            },
        )
        ledger = AWELedger(":memory:")
        sor = MagicMock()
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=source,
            source_of_record=sor,
            harness_adapters={"antigravity": MockHarnessAdapter("antigravity")},
            target_repo=".",
        )
        slot = ExecutionSlot(harness="antigravity", model="gemini-3.1-flash", effort="high")
        summary = worker.run_cycle(worker_id="w1", target_slot=slot, dry_run=False)
        self.assertEqual(summary.health, "refused")
        self.assertIsNone(ledger.get_claim("SF-236"))
        sor.claim_task.assert_not_called()
        codes = {finding.code for finding in evaluate_claim_preflight(source.projection_snapshot()).findings}
        self.assertIn(UNKNOWN_PHYSICAL_TRUTH, codes)


CURSOR_MEDIUM = "cursor/grok-4.6/medium"
CURSOR_PROFILE = "Cursor → Grok 4.6 / Medium"


def _sf238_review_current_yes_sf239_queue_current_no():
    return {
        "physical": [
            {
                "task_id": "SF-238",
                "lane": "Cursor",
                "status": "Review",
                "execution_profile": CURSOR_PROFILE,
            },
            {
                "task_id": "SF-239",
                "lane": "Cursor",
                "status": "Queue",
                "execution_profile": CURSOR_PROFILE,
            },
        ],
        "dashboard": [
            {
                "task_id": "SF-238",
                "status": "Review",
                "current": True,
                "lane": "Cursor",
                "model_effort": CURSOR_PROFILE,
            },
            {
                "task_id": "SF-239",
                "status": "Queue",
                "current": False,
                "lane": "Cursor",
                "model_effort": CURSOR_PROFILE,
            },
        ],
        "dispatch": {
            "harness": "cursor",
            "dispatch_state": "NO_EXECUTABLE_TASK",
            "task_id": "",
            "expected_lifecycle_state": "",
            "execution_profile": CURSOR_PROFILE,
        },
    }


class AuthorizedPointerBindingTests(unittest.TestCase):
    def test_review_current_does_not_authorize_queue_sibling(self):
        snapshot = _sf238_review_current_yes_sf239_queue_current_no()
        self.assertEqual(diagnose_snapshot(snapshot).verdict, PASS)
        report = evaluate_claim_preflight(
            snapshot,
            selected_task_id="SF-239",
            slot=CURSOR_MEDIUM,
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        codes = {finding.code for finding in report.findings}
        self.assertIn(SELECTED_TASK_MISMATCH, codes)
        self.assertIn(NO_EXECUTABLE_TASK, codes)

    def test_queue_claim_requires_unique_current_and_executable_pointer(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-238",
                    "lane": "Cursor",
                    "status": "Queue",
                    "execution_profile": CURSOR_PROFILE,
                }
            ],
            "dashboard": [
                {
                    "task_id": "SF-238",
                    "status": "Queue",
                    "current": True,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                }
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "EXECUTABLE",
                "task_id": "SF-238",
                "expected_lifecycle_state": "Queue",
                "execution_profile": CURSOR_PROFILE,
            },
        }
        report = evaluate_claim_preflight(
            snapshot,
            selected_task_id="SF-238",
            slot=CURSOR_MEDIUM,
        )
        self.assertEqual(report.verdict, PASS)
        self.assertTrue(report.ok)

    def test_in_progress_resume_allowed_without_executable_dispatch(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-238",
                    "lane": "Cursor",
                    "status": "In Progress",
                    "execution_profile": CURSOR_PROFILE,
                }
            ],
            "dashboard": [
                {
                    "task_id": "SF-238",
                    "status": "In Progress",
                    "current": True,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                }
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "NO_EXECUTABLE_TASK",
                "task_id": "",
                "execution_profile": CURSOR_PROFILE,
            },
        }
        report = evaluate_claim_preflight(
            snapshot,
            selected_task_id="SF-238",
            slot=CURSOR_MEDIUM,
        )
        self.assertEqual(report.verdict, PASS)

    def test_in_progress_resume_rejects_queue_sibling(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-238",
                    "lane": "Cursor",
                    "status": "In Progress",
                    "execution_profile": CURSOR_PROFILE,
                },
                {
                    "task_id": "SF-239",
                    "lane": "Cursor",
                    "status": "Queue",
                    "execution_profile": CURSOR_PROFILE,
                },
            ],
            "dashboard": [
                {
                    "task_id": "SF-238",
                    "status": "In Progress",
                    "current": True,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                },
                {
                    "task_id": "SF-239",
                    "status": "Queue",
                    "current": False,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                },
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "NO_EXECUTABLE_TASK",
                "task_id": "",
                "execution_profile": CURSOR_PROFILE,
            },
        }
        report = evaluate_claim_preflight(
            snapshot,
            selected_task_id="SF-239",
            slot=CURSOR_MEDIUM,
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertIn(SELECTED_TASK_MISMATCH, {finding.code for finding in report.findings})

    def test_no_current_pointer_fails_closed(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-239",
                    "lane": "Cursor",
                    "status": "Queue",
                    "execution_profile": CURSOR_PROFILE,
                }
            ],
            "dashboard": [
                {
                    "task_id": "SF-239",
                    "status": "Queue",
                    "current": False,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                }
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "EXECUTABLE",
                "task_id": "SF-239",
                "expected_lifecycle_state": "Queue",
                "execution_profile": CURSOR_PROFILE,
            },
        }
        report = evaluate_claim_preflight(
            snapshot,
            selected_task_id="SF-239",
            slot=CURSOR_MEDIUM,
        )
        self.assertEqual(report.verdict, FAIL_CLOSED)
        self.assertIn(NO_CURRENT_POINTER, {finding.code for finding in report.findings})

    def test_worker_does_not_claim_sf239_when_sf238_is_review_current(self):
        snapshot = _sf238_review_current_yes_sf239_queue_current_no()
        tasks = [
            AWEWorkItem(
                task_id="SF-238",
                title="review current",
                lane="Cursor",
                role="developer",
                status="Review",
                model="Grok 4.6",
                effort="Medium",
                sequence=238,
                current=True,
            ),
            AWEWorkItem(
                task_id="SF-239",
                title="queue sibling",
                lane="Cursor",
                role="developer",
                status="Queue",
                model="Grok 4.6",
                effort="Medium",
                sequence=239,
                current=False,
            ),
        ]
        source = MemoryTaskSource(tasks, projection_snapshot=snapshot)
        ledger = AWELedger(":memory:")
        sor = MagicMock()
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=source,
            source_of_record=sor,
            harness_adapters={"cursor": MockHarnessAdapter("cursor")},
            target_repo=".",
        )
        slot = ExecutionSlot.parse(CURSOR_MEDIUM)
        summary = worker.run_cycle(worker_id="w1", target_slot=slot, dry_run=False)
        self.assertEqual(summary.health, "refused")
        self.assertIsNone(summary.claimed_task)
        self.assertIsNone(ledger.get_claim("SF-239"))
        self.assertIsNone(ledger.get_claim("SF-238"))
        sor.claim_task.assert_not_called()

    def test_worker_resumes_in_progress_current_without_claiming_queue_sibling(self):
        snapshot = {
            "physical": [
                {
                    "task_id": "SF-238",
                    "lane": "Cursor",
                    "status": "In Progress",
                    "execution_profile": CURSOR_PROFILE,
                },
                {
                    "task_id": "SF-239",
                    "lane": "Cursor",
                    "status": "Queue",
                    "execution_profile": CURSOR_PROFILE,
                },
            ],
            "dashboard": [
                {
                    "task_id": "SF-238",
                    "status": "In Progress",
                    "current": True,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                },
                {
                    "task_id": "SF-239",
                    "status": "Queue",
                    "current": False,
                    "lane": "Cursor",
                    "model_effort": CURSOR_PROFILE,
                },
            ],
            "dispatch": {
                "harness": "cursor",
                "dispatch_state": "NO_EXECUTABLE_TASK",
                "task_id": "",
                "execution_profile": CURSOR_PROFILE,
            },
        }
        tasks = [
            AWEWorkItem(
                task_id="SF-238",
                title="in progress current",
                lane="Cursor",
                role="developer",
                status="In Progress",
                model="Grok 4.6",
                effort="Medium",
                sequence=238,
                current=True,
            ),
            AWEWorkItem(
                task_id="SF-239",
                title="queue sibling",
                lane="Cursor",
                role="developer",
                status="Queue",
                model="Grok 4.6",
                effort="Medium",
                sequence=239,
                current=False,
            ),
        ]
        source = MemoryTaskSource(tasks, projection_snapshot=snapshot)
        ledger = AWELedger(":memory:")
        sor = MagicMock()
        sor.claim_task.return_value = (True, "")
        worker = AWEAutonomousWorker(
            ledger=ledger,
            source=source,
            source_of_record=sor,
            harness_adapters={"cursor": MockHarnessAdapter("cursor")},
            target_repo=".",
        )
        slot = ExecutionSlot.parse(CURSOR_MEDIUM)
        summary = worker.run_cycle(worker_id="w1", target_slot=slot, dry_run=False)
        self.assertNotEqual(summary.health, "refused")
        self.assertEqual(summary.claimed_task, "SF-238")
        self.assertIsNotNone(ledger.get_claim("SF-238"))
        self.assertIsNone(ledger.get_claim("SF-239"))
        sor.claim_task.assert_not_called()

    def test_cli_claim_rejects_sf239_regression_without_ledger_claim(self):
        snapshot = _sf238_review_current_yes_sf239_queue_current_no()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with patch("awe_worker.cli.AWELedger") as ledger_cls, \
                    patch("sys.stdout", stdout), \
                    patch("sys.stderr", stderr):
                code = main(
                    [
                        "claim",
                        "--task-id",
                        "SF-239",
                        "--slot",
                        CURSOR_MEDIUM,
                        "--snapshot",
                        str(path),
                    ]
                )
        ledger_cls.return_value.claim.assert_not_called()
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        codes = {item["code"] for item in payload["findings"]}
        self.assertIn(SELECTED_TASK_MISMATCH, codes)
        self.assertIn(NO_EXECUTABLE_TASK, codes)


if __name__ == "__main__":
    unittest.main()
