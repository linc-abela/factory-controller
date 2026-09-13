from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.adapters.grok_build import GrokBuildAdapter
from factory_v2.adapters.hermes import NousHermesAdapter
from factory_v2.adapters.selection import build_executor, selected_executor_kind
from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import MissionContext, WorkItem


WORK = WorkItem(objective="write hello.txt with hello")


def _ctx(workspace: Path) -> MissionContext:
    return MissionContext(
        mission_id="msn-cursor-test",
        lineage_id="msn-cursor-test",
        pcp_hash="a" * 64,
        pcp={"product": {"objective": "write hello.txt with hello"}},
        workspace_path=str(workspace),
    )


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class CursorCLIExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "sandbox"
        self.workspace.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root / "home"),
        }
        (self.root / "home").mkdir()

    def test_satisfies_engineering_executor_contract(self):
        executor = CursorCLIExecutor(env=self.env, binary="missing-agent")
        self.assertIsInstance(executor, EngineeringExecutor)
        self.assertEqual(executor.name, "Cursor CLI")
        self.assertEqual(executor.harness_mode, "real")
        self.assertEqual(executor.executor_type, "cursor_cli")
        self.assertEqual(executor.requested_model, "auto")

    def test_missing_cli_fails_closed(self):
        result = CursorCLIExecutor(env=self.env, binary="agent").implement(
            _ctx(self.workspace), WORK
        )
        self.assertTrue(result.blocked)
        self.assertFalse(result.simulated)
        self.assertIn("unavailable", result.reason)

    def test_unauthenticated_cli_fails_closed(self):
        _script(
            self.bin,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Not logged in'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            "exit 0\n",
        )
        result = CursorCLIExecutor(env=self.env).implement(_ctx(self.workspace), WORK)
        self.assertTrue(result.blocked)
        self.assertIn("unauthenticated", result.reason)

    def test_nonzero_exit_fails_closed(self):
        _script(
            self.bin,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Logged in as test'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            "echo boom >&2\n"
            "exit 3\n",
        )
        result = CursorCLIExecutor(env=self.env).implement(_ctx(self.workspace), WORK)
        self.assertTrue(result.blocked)
        self.assertIn("cursor cli failed", result.reason)

    def test_malformed_result_fails_closed(self):
        _script(
            self.bin,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Logged in as test'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            "echo not-json\n"
            "exit 0\n",
        )
        result = CursorCLIExecutor(env=self.env).implement(_ctx(self.workspace), WORK)
        self.assertTrue(result.blocked)
        self.assertIn("no structured candidate", result.reason)

    def test_unbound_workspace_fails_closed(self):
        ctx = _ctx(self.workspace)
        ctx = MissionContext(
            mission_id=ctx.mission_id,
            lineage_id=ctx.lineage_id,
            pcp_hash=ctx.pcp_hash,
            pcp=ctx.pcp,
            workspace_path="",
        )
        result = CursorCLIExecutor(env=self.env).implement(ctx, WORK)
        self.assertTrue(result.blocked)
        self.assertIn("workspace unbound", result.reason)

    def test_successful_json_candidate_is_bound(self):
        candidate = {
            "candidate_id": "cand-cursor",
            "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "artifact_hash": "sha256:" + "b" * 64,
            "artifact_uri": "sandbox://msn-cursor-test/cand-cursor",
        }
        _script(
            self.bin,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Logged in as test'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            f"echo '{json.dumps(candidate)}'\n"
            "exit 0\n",
        )
        executor = CursorCLIExecutor(env=self.env)
        result = executor.implement(_ctx(self.workspace), WORK)
        self.assertFalse(result.blocked)
        self.assertEqual(result.candidate.candidate_id, "cand-cursor")
        self.assertEqual(len(result.candidate.key()), 4)
        self.assertFalse(result.simulated)
        provenance = json.loads(
            (self.workspace / "cursor-executor-provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["executor_type"], "cursor_cli")
        self.assertEqual(provenance["requested_model"], "auto")
        self.assertEqual(provenance["auth_mode"], "cli_session")
        self.assertNotIn("CURSOR_API_KEY", json.dumps(provenance))

    def test_default_selection_is_cursor_and_grok_remains_selectable(self):
        env = dict(self.env)
        env.pop("FACTORY_V2_ENGINEERING_EXECUTOR", None)
        self.assertEqual(selected_executor_kind(env), "cursor")
        self.assertIsInstance(build_executor(env=env), CursorCLIExecutor)
        env["FACTORY_V2_ENGINEERING_EXECUTOR"] = "grok"
        self.assertEqual(selected_executor_kind(env), "grok")
        self.assertIsInstance(build_executor("grok", env=env), GrokBuildAdapter)
        self.assertTrue(hasattr(GrokBuildAdapter(), "implement"))

    def test_hermes_delegates_coding_through_cursor_executor(self):
        candidate = {
            "candidate_id": "cand-hermes-cursor",
            "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "artifact_hash": "sha256:" + "c" * 64,
            "artifact_uri": "sandbox://msn-cursor-test/cand-hermes-cursor",
        }
        _script(
            self.bin,
            "hermes",
            "#!/bin/sh\n"
            "in=\".\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --in ]; then in=\"$2\"; shift 2; continue; fi\n"
            "  shift\n"
            "done\n"
            "printf '%s\\n' '{\"hermes_session_id\":\"hermes-live\",\"work\":{\"objective\":\"write hello\"}}' > \"$in/hermes-result.json\"\n"
            "exit 0\n",
        )
        _script(
            self.bin,
            "agent",
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then echo 'Logged in as test'; exit 0; fi\n"
            "if [ \"$1\" = --version ]; then echo '2026.08.11-test'; exit 0; fi\n"
            f"echo '{json.dumps(candidate)}'\n"
            "exit 0\n",
        )
        executor = CursorCLIExecutor(env=self.env)
        manager = NousHermesAdapter(env=self.env, executor=executor)
        result = manager.run_campaign(_ctx(self.workspace))
        self.assertFalse(result.blocked)
        self.assertTrue(result.executor_called)
        self.assertEqual(result.executor_name, "Cursor CLI")
        self.assertEqual(result.candidate.candidate_id, "cand-hermes-cursor")
        self.assertEqual(result.manager_name, "Nous Hermes Agent")
