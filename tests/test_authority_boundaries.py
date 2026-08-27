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

from factory_controller import context, routing


PACKAGE = Path(__file__).resolve().parent.parent / "factory_controller"

#: The seam files. Only these may start a process, and each starts exactly one
#: kind: a JSON step adapter, or a command the *mission* declared.
ADAPTER_SEAM = {"adapter.py", "stage1_adapter.py", "context_adapter.py"}

#: The Controller's own runtime. Nothing here may execute anything.
CORE = {"engine.py", "store.py", "routing.py", "cli.py", "context.py", "__init__.py"}

#: The two files permitted to name an external system, on the same principle
#: as ADAPTER_SEAM: a seam is allowed to speak its counterpart's dialect, and
#: confining that to a named file is what makes the rest of the package
#: checkable.  `gateway.py` names the model gateway the Owner admitted;
#: `advisor.py` names the advisory service the Owner may point it at.  Both are
#: still scanned for everything else, and the list is pinned below so a third
#: file cannot join it quietly.
EXTERNAL_SEAM = {"advisor.py", "gateway.py"}

#: The modules that decide what a mission is and what context it may have.
#: None of them may touch a file system at all. `cli.py` is deliberately absent:
#: it reads the mission file the operator named on the command line, which is
#: the operator's own input and not repository content.
DECIDING = {"engine.py", "store.py", "routing.py", "context.py"}

#: Names that would mean the Controller had opened something itself. Selecting
#: repository content is the Context Broker's authority; the Controller states
#: an entitlement and checks the answer against it.
FILE_READ_TOKENS = ("read_text", "read_bytes", "iterdir", "rglob", "glob",
                    "walk", "listdir", "scandir", "open")

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
            if path.name in EXTERNAL_SEAM:
                continue
            code = code_text(text)
            for token in VENDOR_TOKENS:
                self.assertNotIn(token, code, "%s names %r" % (path.name, token))

    def test_the_external_seam_is_exactly_two_files(self):
        """An exemption nobody can extend without changing this line."""

        self.assertEqual(EXTERNAL_SEAM, {"advisor.py", "gateway.py"})
        present = {path.name for path, _ in sources()}
        self.assertTrue(EXTERNAL_SEAM <= present)

    def test_the_deciding_modules_never_import_the_external_seam(self):
        """A vendor name may live at the seam; it may not reach the core.

        This is the check the exemption is worth having.  `engine.py` composes
        the seam's *contracts*, and importing the seam into `store.py` or
        `routing.py` would put a named gateway inside durable state and routing.
        """

        for path, text in sources(DECIDING):
            if path.name == "engine.py":
                continue
            imported = set()
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom) and node.module is None:
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[-1] for alias in node.names)
            self.assertNotIn("advisor", imported, "%s imports the advisory seam" % path.name)
            self.assertNotIn("gateway", imported, "%s imports the gateway seam" % path.name)

    def test_the_external_seam_holds_no_credential_of_its_own(self):
        """The seam may send a credential it was handed.  It may not find one.

        Reading the environment belongs to the two execution-side modules and
        nowhere else: `context_adapter.py` takes the broker command and paths
        that way, and `safe_provider.py` takes its own local settings.  The
        external seam is deliberately not on that list, so a token can only
        arrive as an argument from the operator -- which is what keeps it out of
        durable state.  No module anywhere reads a keyring or a secrets file.
        """

        readers = set()
        for path, text in sources():
            code = code_text(text)
            for token in ("keyring", "netrc", "credentials.json", "id_rsa"):
                self.assertNotIn(token, code, "%s sources a secret (%r)" % (path.name, token))
            # Whole identifiers, not substrings.  `environ` inside
            # `environment_id` is the Stage-6 domain noun, and a scan that
            # cannot tell those apart would force the wrong name on the code
            # rather than catch the thing it exists to catch.
            if {"environ", "environb", "getenv"} & set(code.split("\n")):
                readers.add(path.name)
        self.assertEqual(readers, {"context_adapter.py", "safe_provider.py"},
                         "unexpected environment readers: %s" % readers)
        self.assertFalse(readers & EXTERNAL_SEAM)

    def test_the_environment_reader_scan_would_actually_catch_one(self):
        """Narrowing it to whole identifiers must not make it unable to fire."""

        for planted in ("import os\nkey = os.environ['X']\n",
                        "from os import environ\n",
                        "import os\nkey = os.getenv('X')\n"):
            self.assertTrue(
                {"environ", "getenv"} & set(code_text(planted).split("\n")),
                planted)
        self.assertFalse(
            {"environ", "getenv"}
            & set(code_text("environment_id = row['environment_id']\n").split("\n")))

    def test_no_credential_value_can_reach_the_coordination_ledger(self):
        """An advisor's token is never an argument to anything durable."""

        import inspect
        from factory_controller import advisor as advisor_module
        signature = inspect.signature(advisor_module.HermesAdvisor.__init__)
        self.assertEqual(signature.parameters["token"].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(signature.parameters["token"].default)
        self.assertNotIn("token", advisor_module.HermesAdvisor().probe())

    def test_no_credential_shaped_name_appears_anywhere(self):
        for path, text in sources():
            if path.name in EXTERNAL_SEAM:
                continue
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


class ContextAuthorityTests(unittest.TestCase):
    """The Context Broker selects; the Controller declares and checks.

    SF-136 draws this line and it is the one most easily crossed by accident:
    a single `read_text` in the wrong module would make the Controller a
    retrieval engine that also happens to run missions.
    """

    def test_the_deciding_modules_never_read_a_file(self):
        for path, text in sources(DECIDING):
            code = code_text(text)
            for token in FILE_READ_TOKENS:
                self.assertNotIn(token, code, "%s reads files (%r)" % (path.name, token))

    def test_the_file_read_scan_would_actually_catch_one(self):
        """A boundary check that can never fire is not a boundary."""

        planted = code_text("data = Path(p).read_text()\nfor f in os.walk(root): pass\n")
        for token in ("read_text", "walk"):
            self.assertIn(token, planted)

    def test_the_controller_never_reorders_what_the_broker_selected(self):
        """Ranking is the capability this seam exists to keep out."""

        request = context.ContextRequest.from_payload({
            "work_item_id": "SF-136", "context_request": {
                "corpus_identity": "c", "policy_identity": "p"}})
        scrambled = ("z.py", "a.py", "m.py")
        unhashed = {"schema_version": context.CONTEXT_SCHEMA_VERSION,
                    "mission_input_hash": request.mission_input_hash,
                    "corpus_identity": "c", "policy_identity": "p",
                    "selected_refs": list(scrambled), "unresolved_questions": []}
        package = context.ContextPackage.from_response({
            "status": "built",
            "manifest": {**unhashed, "manifest_hash": context.sha256_hex(unhashed)},
            "measurement": {}})
        self.assertIsNone(context.verify(request, package))
        self.assertEqual(package.manifest.selected_refs, scrambled)
        self.assertEqual(tuple(package.as_row()["manifest"]["selected_refs"]), scrambled)
        self.assertEqual(tuple(context.explain(request, package)["selected_refs"]),
                         scrambled)

    def test_the_controller_holds_no_selection_rule_of_its_own(self):
        """No scoring, ranking, relevance or embedding vocabulary lives here.

        Stage 5 adds a scheduler, which is the first thing in this package that
        orders anything -- so this check matters more than it did, not less.  It
        still passes because the scheduler orders *missions* by two durable
        numbers and never orders repository content by anything.
        """

        for path, text in sources():
            code = code_text(text)
            for token in ("embedding", "vector", "similarity", "relevance",
                          "rank", "score", "tokenize", "tokenizer"):
                self.assertNotIn(token, code, "%s names %r" % (path.name, token))

    def test_the_manifest_digest_rule_belongs_to_evidence_core(self):
        """Reproduced from `src/evidence/validation.py`, not defined here."""

        value = {"schema_version": "1.0", "mission_input_hash": "a" * 64,
                 "corpus_identity": "c", "policy_identity": "p",
                 "selected_refs": [], "unresolved_questions": []}
        self.assertEqual(context.canonical_bytes(value)[-1:], b"\n")
        self.assertNotEqual(context.canonical_bytes({"x": "\u00e9"}),
                            b'{"x":"\\u00e9"}\n')

    def test_bytes_are_never_converted_into_tokens_anywhere(self):
        """The Controller has no tokenizer and must never act as though it does."""

        budget = context.ContextBudget(max_reported_input_tokens=1)
        measurement = context.ContextMeasurement(selected_context_bytes=10 ** 9)
        self.assertIsNone(context.budget_refusal(
            context.ContextBudget(max_reported_input_tokens=1), measurement))
        self.assertIsNone(context.reported_token_refusal(budget, "unknown"))


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
