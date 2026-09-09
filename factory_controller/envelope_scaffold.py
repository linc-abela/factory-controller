"""Factory-derived bootstrap for a Phase-2.1 envelope app.

The Owner never authors these files.  Stubs throw ``NOT_IMPLEMENTED`` so the
provider, not this Controller session, implements the product.  ``evaluate.mjs``
is the frozen decision boundary for the first build.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import adapter, owner_app, pcp


NODE_IMAGE = (
    "node:22-bookworm-slim@"
    "sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5"
)
GITHUB_ORG = "linc-abela"
CHECKOUT_ROOT = Path("/Users/Shared/Projects/software-factory")


def looks_like_new_app(text: str) -> bool:
    lowered = text.lower().strip()
    return lowered.startswith("build ") or "i need to" in lowered


def follow_on_package_id(text: str, bound_package_id: str | None) -> str | None:
    if bound_package_id and not looks_like_new_app(text):
        return bound_package_id
    return None


def remote_url(package_id: str) -> str:
    return "https://github.com/%s/%s.git" % (GITHUB_ORG, package_id)


def checkout_path(package_id: str) -> Path:
    return CHECKOUT_ROOT / package_id


def firebase_review_url(package_id: str) -> str:
    return "https://%s-review.web.app" % package_id


def capability_request(package_id: str, *, run_ref: str,
                       profiles: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": "factory.bridge.capability_admission_request.v1",
        "request_ref": "%s-development" % run_ref,
        "capability": "development",
        "policy_ref": "factory://owner-brief/%s" % package_id,
        "profiles": list(profiles),
        "projects": [package_id],
        "accepted_unknowns": [],
    }


def bridge_project_row(package_id: str, *, checkout: str) -> dict[str, Any]:
    return {
        "repository_remote_url": remote_url(package_id),
        "checkout": checkout,
        "base": "main",
        "capabilities": ["development"],
        "disposable": False,
        "note": (
            "Phase-2.1 envelope product derived from an Owner brief. "
            "The application is the mission output; this checkout starts as stubs."
        ),
    }


def bind_active_contract(state_dir: str | Path, contract_path: str | Path) -> Path:
    pointer = owner_app.active_contract_pointer(state_dir)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps({"path": str(Path(contract_path).resolve())}, indent=2) + "\n",
        encoding="utf-8")
    return pointer


def write_bootstrap(accepted: owner_app.AcceptedBrief, root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in scaffold_files(accepted).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if relative == "dev":
            path.chmod(0o755)
    git_dir = root / ".git"
    if git_dir.exists():
        result = adapter.run_host_command(
            ("git", "log", "-1", "--format=%H"), cwd=str(root))
        sha = result.stdout.strip()
        if result.returncode == 0 and len(sha) == 40:
            return sha
    return adapter.commit_new_repository(
        str(root),
        "Factory-derived Phase-2.1 bootstrap. The application is not implemented.")


def revision_package(previous: Mapping[str, Any], change_text: str, *,
                     created_at: str, predecessor_rc: str,
                     predecessor_candidate_sha: str,
                     owner_validation_id: str) -> dict[str, Any]:
    body = json.loads(json.dumps(previous))
    version = int(body["package_version"]) + 1
    body["package_version"] = version
    body["supersedes"] = "%s@v%s" % (body["package_id"], version - 1)
    body["created_at"] = created_at
    body["origin"] = "owner-brief-revision"
    body["problem"]["statement"] = owner_app._normalize(change_text)
    body["revision"] = {
        "predecessor_rc": predecessor_rc,
        "predecessor_candidate_sha": predecessor_candidate_sha,
        "owner_validation_id": owner_validation_id,
        "owner_decision": pcp.RETURN_FOR_CHANGES,
        "requested_changes": [owner_app._normalize(change_text)],
    }
    extra = {
        "outcome_id": "FASTPATH-OUTCOME-%03d" % (len(body["outcome_criteria"]) + 1),
        "statement": owner_app._normalize(change_text),
        "measurable_by": "independent product behavior evaluation against the revision brief",
    }
    body["outcome_criteria"] = list(body["outcome_criteria"]) + [extra]
    pcp.validate(body)
    return body


def scaffold_files(accepted: owner_app.AcceptedBrief) -> dict[str, str]:
    ident = accepted.package_id
    title = ident.replace("-", " ").title()
    return {
        ".gitignore": "node_modules/\ndist/\n",
        ".dockerignore": ".git\nnode_modules/\ndist/\n",
        "Dockerfile": "FROM %s\n\nWORKDIR /workspace\nCOPY . .\n\nCMD [\"node\", \"--test\"]\n"
                      % NODE_IMAGE,
        "compose.yaml": "services:\n  app:\n    build: .\n    working_dir: /workspace\n"
                        "    volumes:\n      - .:/workspace\n",
        "dev": (
            "#!/bin/sh\n"
            "# Factory-derived acceptance gates. The application is not implemented.\n"
            "set -eu\n"
            "command=${1:-test}\n"
            "shift || true\n"
            "case \"$command\" in\n"
            "  check)\n"
            "    exec docker compose run --rm app sh -c \\\n"
            "      'set -eu; for f in public/*.mjs tests/*.mjs evaluate.mjs;"
            " do node --check \"$f\"; done'\n"
            "    ;;\n"
            "  test)     exec docker compose run --rm app node --test \"$@\" ;;\n"
            "  evaluate) exec docker compose run --rm app node evaluate.mjs ;;\n"
            "  *) echo \"usage: ./dev {check|test|evaluate}\" >&2; exit 2 ;;\n"
            "esac\n"
        ),
        "README.md": (
            "# %s\n\nFactory-derived Phase-2.1 envelope bootstrap. "
            "The application is the mission's output.\n" % title
        ),
        "MISSION.md": accepted_mission_with_boundary(accepted),
        "evaluate.mjs": EVALUATE_JS % {"package_id": ident},
        "public/health.json": json.dumps(
            {"app": ident, "status": "ok"}, indent=2) + "\n",
        "public/index.html": INDEX_HTML % {"title": title},
        "public/styles.css": STYLES_CSS,
        "public/storage.mjs": STORAGE_JS % {"package_id": ident},
        "public/inventory.mjs": INVENTORY_JS,
        "public/ui.mjs": UI_JS,
        "tests/contract.test.mjs": CONTRACT_TEST_JS,
    }


def accepted_mission_with_boundary(accepted: owner_app.AcceptedBrief) -> str:
    return owner_app.mission_statement(accepted) + (
        "## Frozen decision boundary\n\n"
        "The mission is complete when all three gates exit 0 in a clean checkout:\n\n"
        "```plain text\n"
        "./dev check\n"
        "./dev test\n"
        "./dev evaluate\n"
        "```\n\n"
        "At the mission's baseline `./dev check` and `./dev test` pass and "
        "`./dev evaluate` exits 1, because the product is a stub. "
        "**`./dev evaluate` exiting 0 is the mission.**\n\n"
        "`evaluate.mjs` is frozen. Changing it to make it pass is a failed "
        "mission, not a passed one.\n\n"
        "## Implementation notes\n\n"
        "Every export named in `public/inventory.mjs` and `public/storage.mjs` "
        "already exists as a stub that throws `NOT_IMPLEMENTED`. Implement "
        "those modules, the UI, and persistence. Do not add a package.json "
        "dependency, bundler, framework, account, payment, or backend.\n\n"
        "`public/` is the deployable web root.\n"
    )


EVALUATE_JS = r'''// Frozen outcome evaluator for a Phase-2.1 envelope app.
// Changing this file to make it pass is a failed mission.

import assert from "node:assert/strict";
import fs from "node:fs";
import * as inventory from "./public/inventory.mjs";
import * as storageModule from "./public/storage.mjs";

const results = [];

function criterion(id, statement, body) {
  try {
    body();
    results.push({ outcome_id: id, statement, result: "met", detail: "" });
  } catch (error) {
    results.push({
      outcome_id: id,
      statement,
      result: "unmet",
      detail: String(error && error.message ? error.message : error),
    });
  }
}

criterion(
  "FASTPATH-OUTCOME-001",
  "A person can add, edit, delete, and search records named in the Owner brief.",
  () => {
    let items = inventory.emptyList();
    const added = inventory.addItem(items, {
      name: "Flour", quantity: 2, note: "kitchen",
    });
    items = added.items;
    assert.equal(items.length, 1);
    assert.equal(items[0].name, "Flour");
    assert.equal(items[0].quantity, 2);
    const edited = inventory.updateItem(items, added.id, {
      name: "Flour", quantity: 5, note: "restocked",
    });
    items = edited.items;
    assert.equal(items[0].quantity, 5);
    const found = inventory.searchItems(items, "flo");
    assert.equal(found.length, 1);
    items = inventory.deleteItem(items, added.id);
    assert.equal(items.length, 0);
  },
);

criterion(
  "FASTPATH-OUTCOME-002",
  "Records remain after refresh or reopen without an account.",
  () => {
    const memory = storageModule.memoryStorage();
    const items = inventory.addItem(inventory.emptyList(), {
      name: "Rice", quantity: 1, note: "",
    }).items;
    storageModule.save(memory, items);
    const loaded = storageModule.load(memory);
    assert.equal(loaded.length, 1);
    assert.equal(loaded[0].name, "Rice");
    assert.equal(loaded[0].quantity, 1);
  },
);

criterion(
  "FASTPATH-OUTCOME-003",
  "The UI is usable on desktop and mobile viewports.",
  () => {
    const html = fs.readFileSync(new URL("./public/index.html", import.meta.url), "utf8");
    const css = fs.readFileSync(new URL("./public/styles.css", import.meta.url), "utf8");
    assert.match(html, /width=device-width/);
    assert.match(css, /@media/);
    const added = inventory.addItem(inventory.emptyList(), {
      name: "Tea", quantity: 3, note: "cupboard",
    });
    assert.equal(typeof added.id, "string");
    assert.ok(added.id.length > 0);
  },
);

const unmet = results.filter((row) => row.result !== "met");
for (const row of results) {
  const mark = row.result === "met" ? "MET" : "UNMET";
  console.log(`${mark} ${row.outcome_id}: ${row.statement}`);
  if (row.detail) console.log(`  ${row.detail}`);
}
process.exit(unmet.length === 0 ? 0 : 1);
'''

INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>%(title)s</title>
    <link rel="stylesheet" href="./styles.css" />
  </head>
  <body>
    <main id="app" aria-live="polite">
      <h1>%(title)s</h1>
      <p>The application is not implemented yet.</p>
    </main>
    <script type="module" src="./ui.mjs"></script>
  </body>
</html>
"""

STYLES_CSS = """:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 1rem; }
main { max-width: 40rem; }
@media (max-width: 40rem) {
  body { margin: 0.75rem; }
}
"""

STORAGE_JS = """// Seed state: every export below throws NOT_IMPLEMENTED.

const notImplemented = (name) => {
  throw new Error(`NOT_IMPLEMENTED: ${name}`);
};

export const STORAGE_KEY = "%(package_id)s/v1";

export function memoryStorage() {
  return notImplemented("memoryStorage");
}

export function browserStorage(globalObject) {
  return notImplemented("browserStorage");
}

export function save(storage, state) {
  return notImplemented("save");
}

export function load(storage) {
  return notImplemented("load");
}
"""

INVENTORY_JS = """// Seed state: every export below throws NOT_IMPLEMENTED.

const notImplemented = (name) => {
  throw new Error(`NOT_IMPLEMENTED: ${name}`);
};

export function emptyList() {
  return notImplemented("emptyList");
}

export function addItem(items, fields) {
  return notImplemented("addItem");
}

export function updateItem(items, id, fields) {
  return notImplemented("updateItem");
}

export function deleteItem(items, id) {
  return notImplemented("deleteItem");
}

export function searchItems(items, query) {
  return notImplemented("searchItems");
}
"""

UI_JS = """export function mount(root) {
  return root;
}

if (typeof document !== "undefined") {
  mount(document.getElementById("app"));
}
"""

CONTRACT_TEST_JS = """import test from "node:test";
import assert from "node:assert/strict";

import * as inventory from "../public/inventory.mjs";
import * as storage from "../public/storage.mjs";

test("inventory exposes the record surface the evaluator reads", () => {
  for (const name of ["emptyList", "addItem", "updateItem", "deleteItem", "searchItems"]) {
    assert.equal(typeof inventory[name], "function", `inventory.${name} is missing`);
  }
});

test("storage exposes a browser store and a memory fallback", () => {
  for (const name of ["memoryStorage", "browserStorage", "save", "load"]) {
    assert.equal(typeof storage[name], "function", `storage.${name} is missing`);
  }
  assert.equal(typeof storage.STORAGE_KEY, "string");
});
"""
