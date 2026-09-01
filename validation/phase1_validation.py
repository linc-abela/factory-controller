#!/usr/bin/env python3
"""Repeat the SF-157 internal Phase-1 validation without interpreting anything.

The frozen dogfood portfolio can only be carried once per portfolio reference:
its reference is an input to every mission's context identity, and therefore to
the idempotency key the Bridge memoises one response against.  So a second
end-to-end proof cannot reuse ``SF-144-first-internal-dogfood-1`` -- it would
replay stored responses instead of executing -- and it must not rewrite the
historical rows that carry them either.

This module is the way out of that: it runs the *same four missions*, byte for
byte, under a validation portfolio reference, against an isolated state
directory and an isolated ledger, through the real production codepath.  The
Bridge, its unix socket, the provider, the Context Broker and the labs'
containerised evaluators are all the real ones.  Only the identities and the
ledger are new.

    python3 validation/phase1_validation.py <state-dir> verify
    python3 validation/phase1_validation.py <state-dir> run
    python3 validation/phase1_validation.py <state-dir> teardown

``verify`` answers whether the host can carry the run at all, ``run`` carries
it, ``teardown`` removes the validation service.  Every one of them prints
either machine-readable facts and exits 0, or a single ``BLOCKED: <reason>``
line and exits 1.  Nothing here needs a person to read a log.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factory_controller import activation                              # noqa: E402
from factory_controller.adapter import JsonProcessAdapter              # noqa: E402
from factory_controller.engine import Controller, RetryPolicy          # noqa: E402
from factory_controller.factory import (FactoryConfig, FactoryLifecycle,  # noqa: E402
                                        FactoryRefusal)
from factory_controller.store import MissionStore                      # noqa: E402

#: Distinct from the frozen reference on purpose -- see the module docstring.
#: The state directory's own name completes it, so two validation runs never
#: share a mission identity and neither can replay the other's memoised Bridge
#: responses.  A repeat of one run is therefore a fresh directory, and a resume
#: of an interrupted run is the same one.
VALIDATION_PREFIX = "SF-157-phase-1-validation"
LABEL = "com.softwarefactory.supervisor.sf157"
#: One bounded cycle can settle at most one mission, and the portfolio's own
#: slot ceiling is three attempts each, so this is the portfolio's worst case
#: plus room for the handoff cycles between slots.
MAX_CYCLES = 24
REPOSITORIES = ("factory-controller", "factory-bridge", "factory-context-broker",
                "factory-evidence-core", "factory-bug-lab", "factory-prototype-lab")


class Blocked(Exception):
    """One reason the validation cannot proceed, in the Owner's words."""


def _run(argv, cwd=None, timeout=300):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _git(repo: Path, *arguments: str) -> str:
    result = _run(("git", "-C", str(repo), *arguments))
    if result.returncode != 0:
        raise Blocked("git %s failed in %s" % (arguments[0], repo.name))
    return result.stdout.strip()


def heads() -> dict[str, str]:
    """The exact commit each repository is validated at, and its cleanliness."""

    answer = {}
    for name in REPOSITORIES:
        repo = ROOT.parent / name
        if not (repo / ".git").exists():
            raise Blocked("%s is not a checkout on this host" % name)
        dirty = _git(repo, "status", "--porcelain")
        answer[name] = {"head": _git(repo, "rev-parse", "HEAD"),
                        "clean": not dirty}
    return answer


def portfolio_body(state_dir: Path) -> dict:
    """The validation portfolio: the frozen missions under a new reference."""

    frozen = json.loads(
        (ROOT / "contracts" / "first-dogfood-mission-portfolio.json").read_text())
    frozen["portfolio_ref"] = "%s-%s" % (VALIDATION_PREFIX, state_dir.name)
    frozen["rationale"] = (
        "SF-157 validation copy. Every mission below is byte-identical to "
        "first-dogfood-mission-portfolio.json; only portfolio_ref and this "
        "paragraph differ, because the reference is an input to each mission's "
        "context identity and therefore to the idempotency key the Bridge "
        "memoises against. " + frozen["rationale"])
    return frozen


def config(state_dir: Path) -> FactoryConfig:
    return replace(
        FactoryConfig.default(),
        agents_dir=state_dir / "LaunchAgents",
        state_dir=state_dir / "state",
        portfolio_path=state_dir / "portfolio.json",
        supervisor_label=LABEL,
        # This run drives every cycle itself; a job that also fired on a timer
        # would claim the same missions from a second process.
        interval_seconds=86400,
    )


def lifecycle(cfg: FactoryConfig) -> FactoryLifecycle:
    adapter = JsonProcessAdapter(shlex.split(
        "%s -m factory_controller.stage1_adapter" % shlex.quote(sys.executable)))
    store = MissionStore(cfg.state_dir / "factory-controller.db")
    return FactoryLifecycle(
        Controller(store, adapter, retry_policy=RetryPolicy()), config=cfg)


def prepare(state_dir: Path) -> tuple[FactoryConfig, FactoryLifecycle]:
    cfg = config(state_dir)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.agents_dir.mkdir(parents=True, exist_ok=True)
    cfg.portfolio_path.write_text(
        json.dumps(portfolio_body(state_dir), indent=2, sort_keys=True))
    life = lifecycle(cfg)
    # The step adapter is a child process, and in production every variable it
    # needs arrives from the installed job definition.  Reading them off the
    # plan rather than restating them here is what keeps this driver and the
    # installed service from ever disagreeing.
    plan = life._service_plan()
    for name, value in plan.environment:
        os.environ[name] = value
    # The installed job declares a WorkingDirectory; the step
    # adapter is reached as `-m factory_controller.stage1_adapter`
    # and cannot be found from anywhere else.
    os.chdir(plan.working_dir)
    return cfg, life


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def verify(state_dir: Path) -> dict:
    """Everything the run needs, checked before anything is written."""

    facts: dict = {"heads": heads()}
    dirty = sorted(name for name, row in facts["heads"].items() if not row["clean"])
    if dirty:
        raise Blocked("uncommitted changes in " + ", ".join(dirty))

    runtime = shutil.which("docker")
    if runtime is None:
        raise Blocked("no container runtime on PATH; the labs' own evaluators "
                      "are `docker compose` and cannot run without one")
    if _run((runtime, "info"), timeout=60).returncode != 0:
        raise Blocked("the container runtime is installed but not running; "
                      "start OrbStack or Docker Desktop")
    facts["container_runtime"] = runtime

    bridge = ROOT.parent / "factory-bridge"
    doctor = _run((str(bridge / "dev"), "doctor"), cwd=bridge)
    try:
        report = json.loads(doctor.stdout)
    except ValueError:
        raise Blocked("the Bridge did not answer its own doctor") from None
    if (report.get("compatibility") or {}).get("status") != "compatible":
        raise Blocked("the Bridge is not compatible; run './dev factory install'")
    if (report.get("source") or {}).get("installed_sha") != \
            (report.get("source") or {}).get("sha"):
        raise Blocked("Bridge software has changed; run './dev factory install'")
    facts["bridge"] = {"source_sha": (report.get("source") or {}).get("sha"),
                       "serving": (report.get("capability_admissions") or {}).get("serving")}

    readiness = _run((str(bridge / "dev"), "readiness"), cwd=bridge)
    try:
        profiles = json.loads(readiness.stdout).get("profiles") or []
    except ValueError:
        raise Blocked("the Bridge did not answer its own readiness") from None
    available = [row["profile_id"] for row in profiles
                 if row.get("readiness") == "available"]
    if not available:
        raise Blocked("no provider is available; complete the primary "
                      "provider's sign-in")
    facts["providers_available"] = available

    body = portfolio_body(state_dir)
    for mission in body["missions"]:
        repo = ROOT.parent / mission["project_id"]
        head = _git(repo, "rev-parse", "HEAD")
        if head != mission["baseline_sha"]:
            raise Blocked("%s is at %s, not the baseline %s that %s names"
                          % (mission["project_id"], head[:12],
                             mission["baseline_sha"][:12], mission["mission_ref"]))
    facts["portfolio_ref"] = body["portfolio_ref"]
    facts["missions"] = [row["mission_ref"] for row in body["missions"]]
    return facts


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def slots(life: FactoryLifecycle) -> list[dict]:
    rows = []
    for row in life.store.all_missions():
        mission = life.store.get(row["id"])
        rows.append({"key": mission["idempotency_key"],
                     "work_item": mission["payload"].get("work_item_id"),
                     "state": mission["state"],
                     "attempts": mission["attempt_count"],
                     "terminal_reason": mission["terminal_reason"] or "not_applicable"})
    return rows


def emit(step: str, result, life: FactoryLifecycle) -> None:
    print(json.dumps({"step": step, "ok": result.ok, "state": result.state,
                      "lines": list(result.lines), "slots": slots(life)},
                     sort_keys=True), flush=True)


def run(state_dir: Path) -> dict:
    cfg, life = prepare(state_dir)
    for verb in ("install", "start"):
        result = life.dispatch(verb)
        emit(verb, result, life)
        if not result.ok:
            raise Blocked(result.render().replace("BLOCKED: ", ""))
    for index in range(MAX_CYCLES):
        result = life.dispatch("cycle")
        emit("cycle-%d" % (index + 1), result, life)
        if result.state == "complete":
            return {"outcome": "complete", "cycles": index + 1,
                    "slots": slots(life)}
        if not result.ok:
            raise Blocked(result.render().replace("BLOCKED: ", ""))
    raise Blocked("the portfolio did not settle within %d cycles" % MAX_CYCLES)


# --------------------------------------------------------------------------- #
# teardown
# --------------------------------------------------------------------------- #

def teardown(state_dir: Path) -> dict:
    cfg = config(state_dir)
    domain = "gui/%d/%s" % (os.getuid(), cfg.supervisor_label)
    loaded = _run(("launchctl", "print", domain)).returncode == 0
    if loaded:
        _run(("launchctl", "bootout", domain))
    plist = Path(cfg.agents_dir) / (cfg.supervisor_label + ".plist")
    removed = plist.exists()
    if removed:
        plist.unlink()
    return {"service_was_loaded": loaded, "definition_removed": removed,
            "ledger_kept_at": str(cfg.state_dir / "factory-controller.db")}


ACTIONS = {"verify": verify, "run": run, "teardown": teardown}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[1] not in ACTIONS:
        print("usage: phase1_validation.py <state-dir> {%s}"
              % "|".join(sorted(ACTIONS)), file=sys.stderr)
        return 2
    if sys.version_info < (3, 11):
        print("BLOCKED: run this with the same Python the Factory supervisor "
              "uses; 3.11 or newer is required")
        return 1
    state_dir = Path(argv[0]).expanduser()
    if not state_dir.is_absolute():
        print("BLOCKED: the validation state directory must be an absolute path")
        return 1
    try:
        answer = ACTIONS[argv[1]](state_dir)
    except Blocked as blocker:
        print("BLOCKED: %s" % blocker)
        return 1
    except FactoryRefusal as refusal:
        print("BLOCKED: %s" % refusal.detail)
        return 1
    print(json.dumps({"action": argv[1], "result": answer}, indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
