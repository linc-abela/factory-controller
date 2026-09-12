"""Adversarial coverage for the SF-267 Notion credential bootstrap."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from awe_worker.cli import _resolve_task_source, main
from awe_worker.credentials import (
    DarwinKeychainStore,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    MemorySecretStore,
    NullSecretStore,
    credential_status,
    resolve_notion_credential,
    set_default_secret_store,
    store_host_credential,
)
from awe_worker.model import AWEWorkItem
from awe_worker.notion import resolve_notion_token
from awe_worker.service import ContinuationService


SECRET = "sf267-test-secret-not-for-production"


class EnvAndFailClosedTests(unittest.TestCase):
    def setUp(self):
        set_default_secret_store(MemorySecretStore())
        self.addCleanup(lambda: set_default_secret_store(None))

    def test_env_credential_works_without_persistence(self):
        store = MemorySecretStore()
        with patch.dict(os.environ, {"NOTION_TOKEN": SECRET, "NOTION_API_KEY": ""}, clear=False):
            resolved = resolve_notion_credential(store=store)
        self.assertEqual(resolved.source, "env")
        self.assertTrue(resolved.configured)
        self.assertEqual(store.get()[1], "KEYCHAIN_MISSING")
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            after = resolve_notion_credential(store=store)
        self.assertFalse(after.configured)
        self.assertEqual(after.code, "NOTION_NOT_CONFIGURED")

    def test_missing_credential_fails_closed_with_zero_claims(self):
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            source, sor = _resolve_task_source(
                "notion",
                "tests/fixtures/work_exchange",
                "database-id",
            )
            resolved = resolve_notion_credential()
        self.assertEqual(source.__class__.__name__, "LiveNotionTaskSource")
        self.assertEqual(source.fetch_tasks(), [])
        self.assertFalse(resolved.configured)
        self.assertEqual(sor.claim_task(
            AWEWorkItem(
                task_id="SF-999",
                title="SF-999",
                lane="Cursor",
                role="Developer",
                status="Queue",
                model="Grok 4.6",
                effort="High",
                sequence=999,
                task_page_id="abc",
            ),
            "worker",
            "cursor/grok-4.6/high",
        )[1], "NOTION_NOT_CONFIGURED")

    def test_malformed_secure_store_fails_closed(self):
        store = MemorySecretStore(initial="   ")
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            resolved = resolve_notion_credential(store=store)
        self.assertFalse(resolved.configured)
        self.assertEqual(resolved.code, "KEYCHAIN_MALFORMED")

    def test_provider_does_not_read_foreign_files(self):
        with patch("pathlib.Path.read_text") as read_text:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_notion_token(), "")
        read_text.assert_not_called()

    def test_status_never_includes_the_secret(self):
        store = MemorySecretStore()
        store_host_credential(SECRET, store=store)
        status = credential_status(store=store).as_dict()
        dumped = json.dumps(status)
        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "keychain")
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("secret", dumped)


class KeychainAndServiceTests(unittest.TestCase):
    def tearDown(self):
        set_default_secret_store(None)

    def test_memory_store_survives_a_fresh_resolver(self):
        store = MemorySecretStore()
        store.put(SECRET)
        set_default_secret_store(store)
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            first = resolve_notion_credential()
            second = resolve_notion_credential()
        self.assertEqual(first.source, "keychain")
        self.assertEqual(second.source, "keychain")
        self.assertEqual(first.secret, SECRET)

    @unittest.skipUnless(sys.platform == "darwin", "Factory Keychain is macOS-only")
    def test_keychain_store_works_across_a_fresh_process(self):
        store = DarwinKeychainStore(
            service="factory-controller.notion.sf267-test",
            account="sf267-test",
        )
        store.put(SECRET)
        try:
            script = (
                "import json,os,sys\n"
                "from awe_worker.credentials import DarwinKeychainStore, resolve_notion_credential\n"
                "store = DarwinKeychainStore(service=sys.argv[1], account=sys.argv[2])\n"
                "resolved = resolve_notion_credential(store=store)\n"
                "out = {'source': resolved.source, 'configured': resolved.configured, "
                "'matches': resolved.secret == sys.argv[3]}\n"
                "json.dump(out, sys.stdout)\n"
            )
            env = {k: v for k, v in os.environ.items() if k not in {"NOTION_TOKEN", "NOTION_API_KEY"}}
            proc = subprocess.run(
                [sys.executable, "-c", script, store.service, store.account, SECRET],
                check=True,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            payload = json.loads(proc.stdout)
            self.assertEqual(payload, {"source": "keychain", "configured": True, "matches": True})
            self.assertNotIn(SECRET, proc.stdout)
            self.assertNotIn(SECRET, proc.stderr)
        finally:
            store.delete()

    def test_service_restart_resolves_env_inside_the_child(self):
        repo = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "resolved.json"
            child = [
                sys.executable,
                "-c",
                (
                    "import json,sys\n"
                    "from awe_worker.credentials import resolve_notion_credential\n"
                    "r = resolve_notion_credential()\n"
                    "json.dump({'source': r.source, 'configured': r.configured, 'code': r.code}, "
                    "open(sys.argv[1], 'w'))\n"
                ),
                str(out_path),
            ]
            service = ContinuationService(db_path=str(Path(tmp) / "worker.db"), state_dir=tmp)
            service.install(child, working_dir=repo, interval_seconds=1, apply=True)
            pythonpath = repo + os.pathsep + os.environ.get("PYTHONPATH", "")
            with patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": SECRET,
                    "NOTION_API_KEY": "",
                    "PYTHONPATH": pythonpath,
                },
                clear=False,
            ):
                started = service.start()
                self.assertTrue(started["ok"], started)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not out_path.exists():
                    time.sleep(0.05)
                service.restart()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not out_path.exists():
                    time.sleep(0.05)
                service.stop()
            payload = json.loads(out_path.read_text())
            self.assertEqual(payload["source"], "env")
            self.assertTrue(payload["configured"])
            self.assertNotIn(SECRET, out_path.read_text())
            self.assertNotIn(SECRET, json.dumps(started))
            self.assertNotIn(SECRET, " ".join(service.status()["installed_command"]))
            log_text = Path(service.log_path).read_text() if Path(service.log_path).exists() else ""
            self.assertNotIn(SECRET, log_text)
            self.assertNotIn(SECRET, service.manifest_path.read_text())

    def test_credentials_set_cli_reads_stdin_and_never_echoes(self):
        from io import StringIO

        store = MemorySecretStore()
        set_default_secret_store(store)
        stdout = StringIO()
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_API_KEY": ""}, clear=False):
            with patch("awe_worker.cli.sys.stdin") as stdin:
                stdin.isatty.return_value = False
                stdin.read.return_value = SECRET + "\n"
                with patch("awe_worker.cli.sys.stdout", stdout):
                    code = main(["supervisor", "credentials-set"])
        self.assertEqual(code, 0)
        self.assertEqual(store.get()[0], SECRET)
        self.assertNotIn(SECRET, stdout.getvalue())

    def test_null_store_does_not_invent_a_host_credential(self):
        with patch.dict(os.environ, {}, clear=True):
            resolved = resolve_notion_credential(store=NullSecretStore())
        self.assertFalse(resolved.configured)
        self.assertEqual(resolved.code, "NOTION_NOT_CONFIGURED")
        self.assertEqual(KEYCHAIN_SERVICE, "factory-controller.notion")
        self.assertEqual(KEYCHAIN_ACCOUNT, "awe-continuation")


if __name__ == "__main__":
    unittest.main()
