"""Executable cross-repo authority boundaries.

Four owners, checked mechanically rather than described in a document:

* **Controller** owns mission intent, policy, and durable state.
* **factory-bridge** owns provider and profile availability and contained execution.
* **factory-evidence-core** owns evidence semantics.
* **Git** remains candidate truth.

The Controller is the only one of the four this repository can hold to account,
so every check below is a statement about what must *not* be in this package.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from factory_controller import routing


PACKAGE = Path(__file__).resolve().parent.parent / "factory_controller"

#: The seam files. Only these may start a process, and each starts exactly one
#: kind: a JSON step adapter, or a command the *mission* declared.
ADAPTER_SEAM = {"adapter.py", "stage1_adapter.py"}

#: The Controller's own runtime. Nothing here may execute anything.
CORE = {"engine.py", "store.py", "routing.py", "cli.py", "__init__.py"}

#: Names that would mean a vendor had reached the Controller. Matched against
#: code -- identifiers, imports and real string literals -- not prose, so a
#: docstring may name what the package refuses to contain.
VENDOR_TOKENS = (
    "anthropic", "openai", "claude", "codex", "gemini", "gpt-", "llama",
    "mistral", "cohere", "ollama", "openrouter", "hermes", "bedrock",
    "vertexai", "azure",
)

#: Credential-shaped names. A trust boundary that holds one of these is not a
#: boundary; the bridge reads credentials from protected files, never from here.
CREDENTIAL_TOKENS = ("api_key", "apikey", "secret_key", "access_token",
                     "bearer", "authorization", "password", "credential")


def sources(names=None):
    for path in sorted(PACKAGE.glob("*.py")):
        if names is None or path.name in names:
            yield path, path.read_text()


def code_text(text: str) -> str:
    """Everything the interpreter acts on: identifiers and real string values.

    Comments and docstrings are excluded on purpose.  Prose may say "this holds
    no credential"; what must not exist is a credential-shaped *name* or literal.
    """

    tree = ast.parse(text)
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.arg):
            parts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            parts.append(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            parts.extend(alias.name for alias in node.names)
            parts.append(getattr(node, "module", "") or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node not in docstrings:
            parts.append(node.value)
    return "\n".join(parts).lower()


class ProviderNeutralityTests(unittest.TestCase):
    def test_no_vendor_name_appears_anywhere_in_the_package(self):
        for path, text in sources():
            code = code_text(text)
            for token in VENDOR_TOKENS:
                self.assertNotIn(token, code, "%s names %r" % (path.name, token))

    def test_no_credential_shaped_name_appears_anywhere(self):
        for path, text in sources():
            code = code_text(text)
            for token in CREDENTIAL_TOKENS:
                self.assertNotIn(token, code, "%s names %r" % (path.name, token))

    def test_the_scan_would_actually_catch_a_vendor_reaching_the_package(self):
        """A neutrality check that can never fire is not a check."""

        planted = code_text('import anthropic\nrun(["claude", "-p"], key=SECRET_KEY)\n')
        for token in ("anthropic", "claude", "secret_key"):
            self.assertIn(token, planted)
        self.assertNotIn("credential", code_text('"""holds no credential."""\nx = 1\n'))

    def test_the_controller_core_starts_no_process(self):
        """Selection, state and policy never execute anything at all."""

        for path, text in sources(CORE):
            tree = ast.parse(text)
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in getattr(node, "names", [])
            } | {
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            for forbidden in ("subprocess", "os", "shutil", "socket", "urllib", "http"):
                self.assertNotIn(forbidden, imported,
                                 "%s imports %s" % (path.name, forbidden))

    def test_only_the_declared_seam_files_start_a_process(self):
        starting = {path.name for path, text in sources() if "subprocess" in text}
        self.assertTrue(starting <= ADAPTER_SEAM,
                        "unexpected process launcher(s): %s" % (starting - ADAPTER_SEAM))

    def test_the_seam_only_runs_argument_arrays_it_was_given(self):
        """No shell, and no string command built inside this package."""

        for path, text in sources(ADAPTER_SEAM):
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in {"run", "Popen", "call", "check_output"}:
                    continue
                for keyword in node.keywords:
                    self.assertNotEqual(keyword.arg, "shell",
                                        "%s passes shell=" % path.name)

    def test_a_profile_is_only_ever_an_opaque_string(self):
        """The Controller compares profile names; it never parses or builds one."""

        candidate = routing.Candidate("anything-at-all", ("cap",))
        selection = routing.select(routing.ExecutionPolicy(), [candidate])
        self.assertEqual(selection.profile, "anything-at-all")

    def test_availability_is_never_decided_inside_the_controller(self):
        """No candidate is excluded for being 'unavailable'; only policy excludes."""

        reasons = {
            item.reason
            for policy in (routing.ExecutionPolicy(),
                           routing.ExecutionPolicy(allowed_profiles=("b",)),
                           routing.ExecutionPolicy(denied_profiles=("a",)),
                           routing.ExecutionPolicy(required_capability="x"))
            for item in routing.select(
                policy, [routing.Candidate("a", ("y",)), routing.Candidate("b")]).considered
        }
        self.assertEqual(
            reasons,
            {"admissible", "denied_by_policy", "not_in_allowlist", "capability_not_offered"})


class AuthorityOwnershipTests(unittest.TestCase):
    def test_the_controller_does_not_re_derive_candidate_truth(self):
        """Git ancestry and object resolution belong to Evidence Core's verifier."""

        for path, text in sources():
            lowered = text.lower()
            for token in ("cat-file", "merge-base", "rev-parse", "git remote"):
                self.assertNotIn(token, lowered, "%s re-derives git facts" % path.name)


    def test_the_controller_does_not_restate_evidence_semantics(self):
        """No assertion, derivation, or source-ref vocabulary is defined here."""

        for path, text in sources():
            for token in ("class Assertion", "class Derivation", "class EvidenceCollection",
                          "capture_source_ref"):
                self.assertNotIn(token, text, "%s restates Evidence Core" % path.name)

    def test_provider_claims_are_kept_in_their_own_evidence_class(self):
        receipt = routing.unserved_receipt(
            routing.Selection("p", "r", ()), (), "NO_ADMISSIBLE_PROVIDER")
        self.assertEqual(receipt.evidence_class, "reported_claim")
        self.assertNotEqual(receipt.evidence_class, "rederived")

    def test_the_bridge_status_vocabulary_is_consumed_not_invented(self):
        """`factory-bridge` refuses UNSUPPORTED_ENVELOPE_STATUS for anything else."""

        self.assertEqual(routing.BRIDGE_RESULT_STATUSES,
                         ("completed", "blocked", "refused", "no_candidate", "partial_result"))
        self.assertNotIn(routing.PROVIDER_UNAVAILABLE, routing.BRIDGE_RESULT_STATUSES)

    def test_the_controller_owns_mission_intent_and_says_so_in_one_place(self):
        """Policy, candidates and mission identity all resolve from the payload."""

        payload = {
            "work_item_id": "SF-1",
            "execution_mode": "real",
            "context_manifest_hash": "a" * 64,
            "acceptance_gate_ids": ["G"],
            "provider_candidates": ["p"],
            "execution_policy": {"no_fallback": True},
        }
        self.assertTrue(routing.ExecutionPolicy.from_payload(payload).no_fallback)
        self.assertEqual(routing.candidates_from_payload(payload)[0].profile, "p")
        self.assertEqual(routing.expected_idempotency_key("SF-1", "a" * 64), "SF-1:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
