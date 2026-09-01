"""Reconciliation seam between the Controller and ``factory-context-broker``.

The two halves of Stage 4 were written in parallel and do not share a dialect.
The broker speaks ``repo_identity`` / ``head`` / ``manifest_digest`` and refuses
any request field it does not recognise; the Controller speaks the Evidence Core
contract its idempotency key and evidence chain are already bound to.  Neither
name is wrong and neither should be renamed, so the translation lives here --
in one file, in the adapter seam, where a process may be started.

What this module is *not* is a second broker.  It selects nothing, opens no
repository file, and re-derives no measurement: every number it emits was
measured by the broker and every path it emits was chosen by the broker.  Its
whole job is to restate one program's answer in the other's vocabulary, and to
fail closed wherever the restatement cannot be shown to be faithful.

Two translations are load-bearing:

* **Manifest identity.**  The broker's ``manifest_digest`` covers its own rich
  materialization record.  The Controller's manifest is Evidence Core's
  seven-field ``ContextManifest``, whose hash is what
  ``src/evidence/validation.py`` re-derives and what the mission's idempotency
  key names.  Both are kept: the Evidence Core manifest is the identity, and the
  broker digest travels beside it as the opaque content-addressed reference.
* **Absence.**  The broker reports ``token_count: "unavailable"``, which is not
  one of the four words ``src/contracts/replay.py`` owns.  It is translated to
  ``not_measurable`` here rather than carried inward, because a fifth spelling
  of "we do not know" is the divergence the corpus already records four times.

Usage::

    ./dev --adapter "python3 -m factory_controller.context_adapter" work-once ...

with ``FACTORY_CONTEXT_BROKER_COMMAND`` naming the broker CLI, and
``FACTORY_CONTEXT_BROKER_REPO`` / ``FACTORY_CONTEXT_BROKER_CACHE`` naming the
checkout and cache directory the operator admits.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import safe_provider
from .context import CONTEXT_SCHEMA_VERSION, canonical_absence, sha256_hex


COMMAND_ENV = "FACTORY_CONTEXT_BROKER_COMMAND"
REPO_ENV = "FACTORY_CONTEXT_BROKER_REPO"
CACHE_ENV = "FACTORY_CONTEXT_BROKER_CACHE"
REQUEST_DIR_ENV = "FACTORY_CONTEXT_BROKER_REQUEST_DIR"
TIMEOUT_SECONDS = 120


def broker_request(wire: dict[str, Any]) -> dict[str, Any]:
    """The Controller's entitlement, in the only field set the broker accepts.

    Anything the broker does not know about is dropped rather than sent: it
    refuses ``UNKNOWN_REQUEST_FIELD`` outright, so a Controller-only concept
    like freshness or purpose must stay on this side of the seam.
    """

    revision = wire.get("baseline_sha") or "HEAD"
    request: dict[str, Any] = {
        "repo_identity": repo_identity(wire.get("repository_remote_url")),
        "baseline": revision,
        "head": revision,
        "required_anchors": list(wire.get("required_anchors") or []),
        "always_include": list(wire.get("required_anchors") or []),
        "denied_paths": list(wire.get("denied_paths") or []),
    }
    allowed = list(wire.get("allowed_paths") or [])
    if allowed:
        request["allowed_paths"] = allowed
    for name in ("max_bytes", "max_files"):
        if isinstance(wire.get(name), int):
            request[name] = wire[name]
    overview = list(wire.get("overview") or [])
    if overview:
        request["overview"] = overview
    return request


def repo_identity(remote_url: Any) -> str:
    """Mirror the broker's own normalization of a remote, and nothing more.

    If this guess is wrong the broker answers ``REPO_IDENTITY_MISMATCH`` and the
    mission refuses, which is the correct outcome: the adapter is not entitled
    to decide which repository a mission targets.
    """

    if not isinstance(remote_url, str) or not remote_url:
        return ""
    return remote_url.removesuffix(".git").rstrip("/")


def evidence_core_manifest(wire: dict[str, Any], selected: list[str]) -> dict[str, Any]:
    """Derive the identity Evidence Core validates, in the broker's own order."""

    unhashed = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "mission_input_hash": wire["mission_input_hash"],
        "corpus_identity": wire["corpus_identity"],
        "policy_identity": wire["policy_identity"],
        "selected_refs": selected,
        "unresolved_questions": [],
    }
    return {**unhashed, "manifest_hash": sha256_hex(unhashed)}


def translate(wire: dict[str, Any], answer: dict[str, Any], *,
              now: float | None = None) -> dict[str, Any]:
    """Restate one broker answer as a Controller context package."""

    if not answer.get("ok"):
        error = answer.get("error") or {}
        return {"status": "refused",
                "refusal_code": error.get("code") or "CONTEXT_SELECTION_REFUSED"}
    manifest = answer.get("manifest") or {}
    economics = manifest.get("economics") or {}
    selected = [item["path"] for item in manifest.get("selected") or []]
    excluded = [item.get("path") for item in
                (manifest.get("denied") or []) + (manifest.get("omitted") or [])
                if isinstance(item, dict) and item.get("path")]
    derived = evidence_core_manifest(wire, selected)
    declared = repo_identity(wire.get("repository_remote_url"))
    served = manifest.get("repo_identity")
    return {
        "status": "built",
        "manifest": derived,
        "receipt": {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "context_manifest_hash": derived["manifest_hash"],
            "selected_refs": selected,
            "excluded_refs": excluded,
            "mandatory_fact_coverage": list(wire.get("required_anchors") or []),
            "refusal_code": None,
        },
        "measurement": {
            "baseline_context_bytes": economics.get("full_eligible_bytes"),
            "baseline_context_files": economics.get("full_eligible_files"),
            "selected_context_bytes": economics.get("selected_bytes"),
            "selected_context_files": economics.get("selected_files"),
            "manifest_build_ms": int(answer.get("build_latency_ms") or 0),
            "cache_state": "hit" if answer.get("cache_hit") else "miss",
            "cache_identity": manifest.get("cache_identity"),
            # The manifest carries no wall clock by design, so the moment of
            # materialization is this adapter's own observation, not a field
            # read back out of the broker's deterministic record.
            "built_at": time.time() if now is None else now,
            "head_sha": manifest.get("head"),
            # Echo the mission's own remote only when the broker agrees it is
            # the repository it read.  Otherwise hand back what the broker
            # actually served, so the Controller refuses the mismatch.
            "repository_remote_url": wire.get("repository_remote_url")
            if served == declared else served,
            "broker_manifest_digest": (answer.get("manifest_ref") or {}).get("digest")
            or manifest.get("manifest_digest"),
            "policy_digest": manifest.get("policy_digest"),
            "context_token_count": canonical_absence(economics.get("token_count")),
            "repository_overview": manifest.get("overview")
            if isinstance(manifest.get("overview"), dict) else None,
        },
    }


def build(wire: dict[str, Any], *, repo: str | Path | None = None,
          command: str | None = None, cache: str | Path | None = None,
          request_dir: str | Path | None = None, cwd: str | Path | None = None,
          now: float | None = None) -> dict[str, Any]:
    """Run the broker the operator admitted, and restate what it said."""

    command = command or os.environ.get(COMMAND_ENV)
    repo = repo or os.environ.get(REPO_ENV)
    cache = cache or os.environ.get(CACHE_ENV)
    if not command or not repo or not cache:
        # Not configured is not the same as refused: a later attempt may run
        # with the broker in place, so this must not become durable context.
        return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNCONFIGURED"}
    request = broker_request(wire)
    # The broker reads `--request` as a path, not from stdin, and the path has
    # to be one both processes can name. The cache directory is the only
    # location the operator has already admitted to both, so the request is
    # staged there and removed again.
    staging = Path(request_dir or os.environ.get(REQUEST_DIR_ENV) or cache)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(dir=staging, prefix="request-", suffix=".json")
    except OSError:
        return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNAVAILABLE"}
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(request, stream, sort_keys=True)
        try:
            argv = shlex.split(command) + [
                "build", "--repo", str(repo), "--cache-dir", str(cache),
                "--request", path]
        except ValueError:
            return {"status": "unavailable",
                    "refusal_code": "CONTEXT_BROKER_UNCONFIGURED"}
        try:
            completed = subprocess.run(
                argv, text=True, capture_output=True,
                timeout=TIMEOUT_SECONDS, check=False,
                cwd=None if cwd is None else str(cwd))
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNAVAILABLE"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        answer = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNREADABLE"}
    if not isinstance(answer, dict):
        return {"status": "unavailable", "refusal_code": "CONTEXT_BROKER_UNREADABLE"}
    return translate(wire, answer, now=now)


def main() -> int:
    request = json.load(sys.stdin)
    if request["step"] == "context":
        result = build(request["input"]["context_request"])
        json.dump(result, sys.stdout, sort_keys=True)
        return 0
    return safe_provider.main_with(request)


if __name__ == "__main__":
    raise SystemExit(main())
