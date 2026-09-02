"""SF-162: the real STALE_HEAD, against the real Broker and the real Bridge.

The Owner ran the documented sequence and `./dev factory revise` refused:

    BLOCKED: The Context Broker did not produce an admissible repository
    grounding package (STALE_HEAD).

Two correct invariants met.  The Context Broker grounds only the current
``HEAD`` of the checkout it is pointed at, because a manifest that is not
current is a manifest about nothing.  The revision base is a commit
descending from the rejected candidate on a Factory branch, deliberately not
on the product's own branch, because a revision must not move the product.
The registered checkout therefore can never be at the base.

Every other test of this path stubs one side or the other.  This one stubs
neither: it mints a base with ``factory_bridge.revision``, grounds it with
``factory_context_broker``, and asserts the refusal on the registered
checkout and the success on the checkout the Bridge opened.  It is skipped
where the sibling checkouts are not present -- inside the Controller's own
container, only this repository is mounted.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from factory_controller import context_adapter


SIBLINGS = Path(__file__).resolve().parent.parent.parent
BRIDGE = SIBLINGS / "factory-bridge" / "src"
BROKER = SIBLINGS / "factory-context-broker"
REMOTE = "https://example.invalid/a-product.git"


def broker_command() -> str:
    """The same CLI invocation `factory.py::_context_broker_command` builds."""

    code = ("import sys; sys.path.insert(0, %s); "
            "from factory_context_broker.cli import main; "
            "raise SystemExit(main())") % json.dumps(str(BROKER))
    return "%s -c %s" % (shlex.quote(sys.executable), shlex.quote(code))


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True).stdout.decode().strip()


@unittest.skipUnless(BRIDGE.is_dir() and BROKER.is_dir(),
                     "the sibling Bridge and Broker checkouts are not present")
class RealRevisionGroundingTests(unittest.TestCase):
    """One repository, one base, and the two grounding attempts."""

    @classmethod
    def setUpClass(cls):
        for path in (str(BRIDGE), str(BROKER)):
            if path not in sys.path:
                sys.path.insert(0, path)

    def setUp(self):
        from factory_bridge import registry as registry_mod, revision

        self.revision = revision
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = os.path.join(self.temporary.name, "product")
        os.makedirs(self.root)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "test")
        git(self.root, "remote", "add", "origin", REMOTE)
        Path(self.root, "MISSION.md").write_text("the original boundary\n")
        Path(self.root, "index.html").write_text("<!doctype html>\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "baseline")
        Path(self.root, "index.html").write_text("<!doctype html><title>v1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "candidate")
        self.candidate = git(self.root, "log", "-1", "--format=%H")
        # The product branch stays where the Owner's product is, which is the
        # whole reason the base cannot be grounded here.
        git(self.root, "reset", "-q", "--hard", "HEAD~1")
        self.project = registry_mod.Project(
            project_id="a-product", repository_remote_url=REMOTE,
            checkout=self.root, base="main",
            capabilities=("development",), disposable=False)
        self.cache = os.path.join(self.temporary.name, "broker-cache")

    def open_base(self):
        return self.revision.base(
            self.project, self.candidate, mission_path="MISSION.md",
            addendum_text="Remove decimal points from monetary displays.\n",
            ref="refs/heads/factory/revision/v2",
            grounding_root=os.path.join(self.temporary.name, "revisions"))

    def ground(self, repo, head):
        """Run the real Broker exactly as `_build_context` runs it."""

        from factory_context_broker.broker import repository_identity

        wire = {"repository_remote_url": REMOTE, "baseline_sha": head,
                "required_anchors": ["MISSION.md"],
                "mission_input_hash": "m", "corpus_identity": "c",
                "policy_identity": "p"}
        self.assertEqual(context_adapter.repo_identity(REMOTE),
                         repository_identity(repo))
        return context_adapter.build(
            wire, repo=repo, cache=self.cache, cwd=str(BROKER),
            command=broker_command())

    def test_the_registered_checkout_still_refuses_a_historical_head(self):
        """The exact SF-160 failure, reproduced end to end."""

        base = self.open_base()
        answer = self.ground(self.root, base["revision_sha"])
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["refusal_code"], "STALE_HEAD")

    def test_the_checkout_the_bridge_opened_grounds_the_same_base(self):
        base = self.open_base()
        answer = self.ground(base["revision_checkout"], base["revision_sha"])
        self.assertEqual(answer["status"], "built", answer)
        self.assertEqual(answer["measurement"]["head_sha"], base["revision_sha"])
        self.assertEqual(answer["measurement"]["repository_remote_url"], REMOTE)
        self.assertIn("MISSION.md", answer["receipt"]["selected_refs"])

    def test_the_grounded_statement_is_the_one_the_revision_added(self):
        """Not the product's statement: the base's, with the Owner's text."""

        base = self.open_base()
        self.assertIn(
            "Remove decimal points",
            git(base["revision_checkout"], "show", "HEAD:MISSION.md"))
        self.assertNotIn("Remove decimal points",
                         git(self.root, "show", "HEAD:MISSION.md"))

    def test_ordinary_freshness_is_untouched_by_the_repair(self):
        """A historical commit in the grounding checkout is still refused."""

        base = self.open_base()
        answer = self.ground(base["revision_checkout"], self.candidate)
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["refusal_code"], "STALE_HEAD")

    def test_the_product_branch_never_moved(self):
        before = git(self.root, "rev-parse", "main")
        base = self.open_base()
        self.ground(base["revision_checkout"], base["revision_sha"])
        self.assertEqual(git(self.root, "rev-parse", "main"), before)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
