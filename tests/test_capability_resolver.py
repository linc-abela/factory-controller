"""Hermes routes by capability; mapping changes do not require golden_path edits."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factory_controller import capability_map, capability_resolver, golden_path
from factory_controller.capability_map import CAP_ARCHITECTURE, CAP_IMPLEMENTATION
from factory_controller.fleet_harness import (
    COMPLETED,
    HarnessReceipt,
    QUOTA_EXHAUSTED,
    classify_provider_output,
    provider_model_id,
)


ALT_MAP = """
| Capability / role | Runner | Model | Effort / policy | Purpose |
|---|---|---|---|---|
| **Architecture / technical design — primary** | Codex | **Atlas 1** | **High** | Primary architecture route |
| **Architecture / technical design — continuity fallback** | Cursor | **Orion 9** | **High; only after explicit token/quota exhaustion** | Resume architecture after quota |
| **Developer Fleet** | Codex | **Alpha** | **Max** | Main developer peer |
| **Developer Fleet** | Cursor | **Nova 2** | **High** | difficult debugging recovery high-risk implementation |
| **Developer Fleet** | Cursor | **Beta** | **Medium** | normal bounded well-specified implementation |
"""

ALT_MAP_SWAPPED = """
| Capability / role | Runner | Model | Effort / policy | Purpose |
|---|---|---|---|---|
| **Architecture / technical design — primary** | Cursor | **Helios** | **High** | Primary architecture route |
| **Architecture / technical design — continuity fallback** | Codex | **Vesper** | **Max; only after explicit token/quota exhaustion** | Resume architecture after quota |
| **Developer Fleet** | Cursor | **Quill** | **High** | difficult recovery implementation |
"""

CANONICAL_VAULT = Path("/Users/Shared/Projects/factory-vault-SF-272")


class _ScriptedHarness:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def run(self, profile, prompt, cwd: Path) -> HarnessReceipt:
        self.calls.append(profile.key)
        status, side_effect = self.outcomes.pop(0)
        if side_effect is not None:
            side_effect(profile, cwd)
        return HarnessReceipt(
            status=status,
            harness=profile.harness,
            model=profile.model,
            effort=profile.effort,
        )


class _Mission:
    mission_key = "test/mission"
    package_id = "lodus-widget"
    canonical_path = "PRODUCTS/widget/pcp.json"
    package_digest = "abc"
    evidence = None


def _write_arch(profile, cwd: Path) -> None:
    (cwd / "architecture.json").write_text(
        json.dumps({"producer": profile.model}), encoding="utf-8")


def _commit_delta(profile, cwd: Path) -> None:
    (cwd / "delta.txt").write_text("post-intake %s\n" % profile.model, encoding="utf-8")
    golden_path._git(["add", "-A"], cwd)
    golden_path._git(["commit", "-m", "architecture delta"], cwd)
    (cwd / "implementation.json").write_text(json.dumps({
        "head": golden_path._git_head(cwd),
        "branch": golden_path._git_branch(cwd),
        "packages": [{
            "id": "core",
            "harness": profile.harness,
            "model": profile.model,
            "effort": profile.effort,
            "head": golden_path._git_head(cwd),
        }],
    }), encoding="utf-8")


class CapabilityResolverTests(unittest.TestCase):
    def test_parses_roles_not_hardcoded_model_pairs(self):
        catalog = capability_map.parse(ALT_MAP)
        arch = catalog.for_capability(CAP_ARCHITECTURE)
        impl = catalog.for_capability(CAP_IMPLEMENTATION)
        self.assertEqual([p.model for p in arch], ["atlas-1", "orion-9"])
        self.assertEqual(arch[0].role, "primary")
        self.assertEqual(arch[1].role, "continuity_fallback")
        self.assertTrue(arch[1].quota_continuity)
        self.assertEqual([p.model for p in impl], ["alpha", "nova-2", "beta"])
        self.assertTrue(all(p.role == "member" for p in impl))

    def test_architecture_uses_primary_then_continuity_after_quota(self):
        catalog = capability_map.parse(ALT_MAP)
        first = capability_resolver.resolve(CAP_ARCHITECTURE, catalog, {})
        self.assertEqual(first.profile.model, "atlas-1")
        self.assertEqual(first.reason, "primary_available")
        again = capability_resolver.resolve(
            CAP_ARCHITECTURE, catalog,
            {first.profile.key: QUOTA_EXHAUSTED})
        self.assertEqual(again.profile.model, "orion-9")
        self.assertIn("quota_exhausted", again.reason)

    def test_architecture_policy_follows_swapped_mapping_names(self):
        catalog = capability_map.parse(ALT_MAP_SWAPPED)
        first = capability_resolver.resolve(CAP_ARCHITECTURE, catalog, {})
        self.assertEqual(first.profile.model, "helios")
        again = capability_resolver.resolve(
            CAP_ARCHITECTURE, catalog,
            {first.profile.key: QUOTA_EXHAUSTED})
        self.assertEqual(again.profile.model, "vesper")

    def test_developer_fleet_has_no_primary_backup_pair(self):
        catalog = capability_map.parse(ALT_MAP)
        high = capability_resolver.resolve(
            CAP_IMPLEMENTATION, catalog, {}, context={"difficulty": "high"})
        self.assertEqual(high.profile.model, "nova-2")
        self.assertEqual(high.reason, "best_eligible_fleet")
        after = capability_resolver.resolve(
            CAP_IMPLEMENTATION, catalog,
            {high.profile.key: QUOTA_EXHAUSTED},
            context={"difficulty": "high"})
        self.assertNotEqual(after.profile.model, "nova-2")
        self.assertIn(after.profile.model, {"alpha", "beta"})

    def test_quota_reruns_fleet_resolution_excluding_exhausted_member(self):
        catalog = capability_map.parse(ALT_MAP)
        live = {"codex/alpha/max": QUOTA_EXHAUSTED}
        choice = capability_resolver.resolve(
            CAP_IMPLEMENTATION, catalog, live,
            context={"difficulty": "high", "incumbent": "codex/alpha/max"})
        self.assertEqual(choice.profile.model, "nova-2")
        self.assertIn("codex/alpha/max", choice.excluded)

    def test_incumbent_continues_until_exhausted(self):
        catalog = capability_map.parse(ALT_MAP)
        choice = capability_resolver.resolve(
            CAP_IMPLEMENTATION, catalog, {},
            context={"difficulty": "high", "incumbent": "codex/alpha/max"})
        self.assertEqual(choice.profile.model, "alpha")
        self.assertEqual(choice.reason, "incumbent_continue")

    def test_canonical_vault_mapping_feeds_architecture_and_fleet_pools(self):
        mapping = CANONICAL_VAULT / capability_map.MAP_RELPATH
        if not mapping.is_file():
            self.skipTest("SF-272 vault mapping is not on this host")
        catalog = capability_map.load(CANONICAL_VAULT)
        arch = catalog.for_capability(CAP_ARCHITECTURE)
        impl = catalog.for_capability(CAP_IMPLEMENTATION)
        self.assertGreaterEqual(len(arch), 2)
        self.assertEqual(arch[0].role, "primary")
        self.assertEqual(arch[1].role, "continuity_fallback")
        self.assertGreaterEqual(len(impl), 3)
        self.assertTrue(all(p.role == "member" for p in impl))
        qa = catalog.for_capability(capability_map.CAP_QA)
        self.assertGreaterEqual(len(qa), 1)
        self.assertEqual(qa[0].harness, "antigravity")
        self.assertEqual(qa[0].effort, "medium")


class HarnessNormalizationTests(unittest.TestCase):
    def test_provider_usage_limit_is_quota_exhausted(self):
        text = "ERROR: You've hit your usage limit. Upgrade to Pro and try again at 9:23 PM."
        self.assertEqual(classify_provider_output(text, 1), QUOTA_EXHAUSTED)

    def test_not_logged_in_is_temporarily_unavailable(self):
        self.assertEqual(
            classify_provider_output("Error: Not logged in", 1),
            "TEMPORARILY_UNAVAILABLE")

    def test_antigravity_gemini_medium_uses_flash_model_id(self):
        profile = capability_map.Profile(
            capability=capability_map.CAP_QA, role="member",
            harness="antigravity", model="gemini-3.8", effort="medium",
            purpose="e2e", quota_continuity=False)
        self.assertEqual(provider_model_id(profile), "gemini-3.8-flash-medium")


class GoldenPathRoutingAbstractionTests(unittest.TestCase):
    def test_golden_path_source_has_no_model_roster(self):
        src = Path(golden_path.__file__).read_text(encoding="utf-8")
        resolver = Path(capability_resolver.__file__).read_text(encoding="utf-8")
        for needle in (
            "gpt-5.6-sol", "gpt-5.6-luna", "grok-4.6", "claude-opus-5",
            "ARCH_ROUTES", "IMPL_ROUTES", "ARCH_PRIMARY", "IMPL_ROUTE",
        ):
            self.assertNotIn(needle, src)
            self.assertNotIn(needle, resolver)

    def test_architecture_quota_reselects_from_injected_mapping(self):
        catalog = capability_map.parse(ALT_MAP)
        harness = _ScriptedHarness([
            (QUOTA_EXHAUSTED, None),
            (COMPLETED, _write_arch),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            executors = golden_path.FleetExecutors(
                vault_root=root, state_dir=root, catalog=catalog, harness=harness)
            result = executors.architecture(_Mission(), {"prototype_input": ""}, work)
        self.assertEqual(result["model"], "orion-9")
        self.assertEqual(harness.calls, ["codex/atlas-1/high", "cursor/orion-9/high"])
        self.assertEqual(result["attempts"][0]["availability_result"], QUOTA_EXHAUSTED)
        self.assertEqual(result["attempts"][0]["next_selected"]["model"], "orion-9")
        self.assertEqual(result["attempts"][1]["reselection_reason"],
                         "continuity_after_quota_exhausted")

    def test_implementation_quota_reselects_best_remaining_fleet_member(self):
        catalog = capability_map.parse(ALT_MAP)
        harness = _ScriptedHarness([
            (QUOTA_EXHAUSTED, lambda profile, cwd: (cwd / "wip.txt").write_text("wip\n")),
            (COMPLETED, _commit_delta),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            executors = golden_path.FleetExecutors(
                vault_root=root, state_dir=root, catalog=catalog, harness=harness)
            executors._prepare_work(_Mission(), work)
            executors._set_incumbent(work, "codex/alpha/max")
            architecture = {"intake_head": golden_path._git_head(work), "artifact": ""}
            result = executors.implementation(_Mission(), architecture, work)
        self.assertTrue(result.get("head"))
        self.assertEqual(result["packages"][0]["model"], "nova-2")
        self.assertEqual(harness.calls[0], "codex/alpha/max")
        self.assertIn("nova-2", harness.calls[1])
        self.assertEqual(result["attempts"][0]["capability"], CAP_IMPLEMENTATION)
        self.assertEqual(result["live"]["codex/alpha/max"], QUOTA_EXHAUSTED)

    def test_architecture_does_not_fallback_on_generic_failure(self):
        catalog = capability_map.parse(ALT_MAP)
        harness = _ScriptedHarness([("FAILED", None)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            executors = golden_path.FleetExecutors(
                vault_root=root, state_dir=root, catalog=catalog, harness=harness)
            result = executors.architecture(_Mission(), {}, work)
        self.assertNotIn("artifact", result)
        self.assertEqual(harness.calls, ["codex/atlas-1/high"])

    def test_swapped_mapping_is_followed_without_golden_path_edits(self):
        catalog = capability_map.parse(ALT_MAP_SWAPPED)
        harness = _ScriptedHarness([
            (QUOTA_EXHAUSTED, None),
            (COMPLETED, _write_arch),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            executors = golden_path.FleetExecutors(
                vault_root=root, state_dir=root, catalog=catalog, harness=harness)
            result = executors.architecture(_Mission(), {}, work)
        self.assertEqual(result["model"], "vesper")
        self.assertEqual(harness.calls, ["cursor/helios/high", "codex/vesper/max"])


if __name__ == "__main__":
    unittest.main()
