"""CLI tests for factory-controller commands."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller.cli import main
from factory_controller.store import MissionStore


class ControllerCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "cli_test.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_harness_command(self) -> None:
        rc = main(["--db", self.db_path, "harness", "--missions", "5"])
        self.assertEqual(rc, 0)
        store = MissionStore(self.db_path)
        counts = store.counts()
        self.assertEqual(counts.get("DONE"), 5)

    def test_submit_and_work_once_lifecycle(self) -> None:
        # 1. Submit via JSON file
        file_path = Path(self.temp_dir.name) / "payload.json"
        file_path.write_text(json.dumps({"task": "cli-test"}))
        rc_submit = main(["--db", self.db_path, "submit", "--key", "cli-key-1", "--file", str(file_path)])
        self.assertEqual(rc_submit, 0)

        # 2. Status counts
        store = MissionStore(self.db_path)
        self.assertEqual(store.counts().get("READY"), 1)

        # 3. Work once
        rc_work = main(["--db", self.db_path, "work-once", "--worker", "cli-worker"])
        self.assertEqual(rc_work, 0)
        self.assertEqual(store.counts().get("DONE"), 1)


if __name__ == "__main__":
    unittest.main()
