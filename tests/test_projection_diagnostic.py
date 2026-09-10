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
from unittest.mock import patch

from awe_worker.cli import main
from awe_worker.projection import (
    DASHBOARD_STATUS_MISMATCH,
    DUPLICATE_DASHBOARD_CURRENT,
    FAIL_CLOSED,
    PASS,
    STALE_DISPATCH_EXECUTABLE,
    diagnose_projection,
    diagnose_snapshot,
)


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


if __name__ == "__main__":
    unittest.main()
