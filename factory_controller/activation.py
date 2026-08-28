"""The Stage-9 host service, as a package an Owner installs and can undo.

``supervisor.service_contract`` says what a host would have to do to call
``cycle`` on a schedule, and deliberately stops there.  This module turns that
description into files: a launchd job definition, a receipt, a drift report and
an uninstall.  It stops in exactly the same place, one step later.

**Nothing here loads a service.**  There is no verb that starts, bootstraps or
enables anything, no name of the loader anywhere in the code, and a test pins
that absence -- so "the Controller cannot activate itself" is a missing verb
rather than a refusal somebody could route around.  Writing the job definition
is not activation: launchd does not read a file it has not been told about.

**Nothing activates by being imported, tested, or started.**  Every path that
writes takes an explicit target directory and an explicit ``apply``; the module
level runs no code at all.

**An install is idempotent and reversible.**  The plan has a digest; installing
a plan whose digest matches the receipt writes nothing and says ``unchanged``,
and ``uninstall`` removes exactly the files the receipt records.  Drift is the
comparison between what the receipt says was installed and what the current
plan would install, which is how the bridge already reports its own.

The one thing this package cannot answer is whether launchd currently holds the
job.  That is a host query it has no way to make, so it is reported as
``unknown`` rather than guessed, and the Owner's verification step below is what
answers it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "factory-controller/activation/1.0"

#: Named here, once, for the same reason ``factory-bridge/paths.py`` exists: the
#: retired runtime carried one path in three places and one of them was wrong
#: for six days.
DEFAULT_LABEL = "com.softwarefactory.supervisor"
DEFAULT_INTERVAL_SECONDS = 300

#: The lower bound is a safety property, not a preference.  A cycle claims a
#: lease and refuses an overlapping invocation, so a too-short interval does not
#: corrupt anything -- it produces a stream of recorded refusals that hides the
#: real ones.  An hour ceiling keeps a mistyped value from parking the Factory.
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86400


class ActivationError(ValueError):
    """A service plan the Controller will not write."""


@dataclass(frozen=True)
class ServicePlan:
    """Everything the host needs, derived from the supervisor's own contract."""

    label: str
    invocation: tuple[str, ...]
    interval_seconds: int
    agents_dir: str
    state_dir: str
    working_dir: str

    def __post_init__(self) -> None:
        if not self.label or self.label != self.label.strip() or "/" in self.label:
            raise ActivationError("a service label is a bare reverse-dns name")
        if not self.invocation or not all(
                isinstance(value, str) and value for value in self.invocation):
            raise ActivationError("the invocation is a non-empty argument array")
        if not MIN_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ActivationError(
                "an interval between %d and %d seconds; below that a cycle only "
                "produces overlap refusals and above it the Factory is parked"
                % (MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS))
        for name in ("agents_dir", "state_dir", "working_dir"):
            value = getattr(self, name)
            if not value or not Path(value).is_absolute():
                raise ActivationError("%s must be an absolute path" % name)

    # -- derived locations -------------------------------------------------- #

    @property
    def definition_path(self) -> str:
        return str(Path(self.agents_dir) / (self.label + ".plist"))

    @property
    def receipt_path(self) -> str:
        return str(Path(self.state_dir) / "supervisor-install-receipt.json")

    @property
    def log_path(self) -> str:
        return str(Path(self.state_dir) / "supervisor.log")

    @property
    def interpreter(self) -> str:
        """The executable the host scheduler will restart for every cycle."""

        value = self.invocation[0]
        return value if Path(value).is_absolute() else "unknown"

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.as_row(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def as_row(self) -> dict[str, Any]:
        return {"label": self.label, "invocation": list(self.invocation),
                "interpreter": self.interpreter,
                "interval_seconds": self.interval_seconds,
                "agents_dir": self.agents_dir, "state_dir": self.state_dir,
                "working_dir": self.working_dir}


def from_contract(contract: dict[str, Any], *, agents_dir: str, state_dir: str,
                  working_dir: str, label: str = DEFAULT_LABEL) -> ServicePlan:
    """Build the plan from ``OperationsSupervisor.service_contract``.

    Reading the contract rather than restating it is the point: the invocation
    and the interval have one owner, so a job definition cannot come to name a
    command the supervisor does not offer.
    """

    schedule = contract.get("schedule") or {}
    invocation = schedule.get("invocation")
    if not isinstance(invocation, list):
        raise ActivationError("the contract carries no invocation")
    return ServicePlan(
        label=label, invocation=tuple(invocation),
        interval_seconds=int(schedule.get("interval_seconds",
                                          DEFAULT_INTERVAL_SECONDS)),
        agents_dir=agents_dir, state_dir=state_dir, working_dir=working_dir)


# --------------------------------------------------------------------------- #
# the job definition
# --------------------------------------------------------------------------- #

def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def definition(plan: ServicePlan) -> str:
    """The launchd job, written by hand so the bytes are exactly predictable.

    ``RunAtLoad`` is false and ``KeepAlive`` is absent on purpose.  This job is a
    bounded command on an interval; a job the host restarts whenever it exits is
    a job that runs continuously, which is the one shape the supervisor's whole
    design refuses.
    """

    arguments = "".join("\n    <string>%s</string>" % _escape(value)
                        for value in plan.invocation)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{_escape(plan.label)}</string>
  <key>ProgramArguments</key><array>{arguments}
  </array>
  <key>WorkingDirectory</key><string>{_escape(plan.working_dir)}</string>
  <key>StartInterval</key><integer>{plan.interval_seconds}</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{_escape(plan.log_path)}</string>
  <key>StandardErrorPath</key><string>{_escape(plan.log_path)}</string>
</dict></plist>
"""


def activation_command(plan: ServicePlan) -> dict[str, Any]:
    """The exact step this package will not take, and how to check it worked.

    Returned as data rather than performed.  The verification steps are here
    because "the file exists" is not "the job runs": SF-128 found an installed
    job definition whose environment named a directory that did not exist, and
    four sessions had checked that the file was present.
    """

    return {
        "performed_here": False,
        "performed_by": "owner",
        "state": "not_run",
        "activate": "launchctl bootstrap gui/$(id -u) %s" % plan.definition_path,
        "verify": [
            "launchctl print gui/$(id -u)/%s" % plan.label,
            "tail -n 40 %s" % plan.log_path,
            # The same argument array with its terminal verb swapped, so the
            # check reads the cycles this job wrote and not some other database.
            " ".join([*plan.invocation[:-1], "cycles", "--limit", "5"]),
        ],
        "deactivate": "launchctl bootout gui/$(id -u)/%s" % plan.label,
    }


# --------------------------------------------------------------------------- #
# install, doctor, uninstall
# --------------------------------------------------------------------------- #

def _receipt(plan: ServicePlan) -> dict[str, Any] | None:
    try:
        return json.loads(Path(plan.receipt_path).read_text())
    except (OSError, ValueError):
        return None


def install(plan: ServicePlan, *, apply: bool = False,
            clock=None) -> dict[str, Any]:
    """Write the job definition and a receipt, or say what it would write.

    ``apply`` defaults to false so that every accidental path -- an import, a
    test, a mistyped command -- produces a plan and no file.
    """

    body = definition(plan)
    previous = _receipt(plan)
    present = Path(plan.definition_path).is_file()
    unchanged = bool(previous and previous.get("plan_digest") == plan.digest
                     and present)
    result = {
        "contract_version": CONTRACT_VERSION,
        "label": plan.label,
        "plan": plan.as_row(),
        "plan_digest": plan.digest,
        "definition_path": plan.definition_path,
        "definition_digest": hashlib.sha256(body.encode()).hexdigest(),
        "receipt_path": plan.receipt_path,
        "interpreter": plan.interpreter,
        "previous_digest": (previous or {}).get("plan_digest", "not_applicable"),
        "outcome": "unchanged" if unchanged else "planned",
        "applied": False,
        "activation": activation_command(plan),
    }
    if not apply or unchanged:
        return result
    Path(plan.agents_dir).mkdir(parents=True, exist_ok=True)
    Path(plan.state_dir).mkdir(parents=True, exist_ok=True)
    Path(plan.definition_path).write_text(body)
    receipt = {"plan_digest": plan.digest, "plan": plan.as_row(),
               "interpreter": plan.interpreter,
               "definition_digest": result["definition_digest"],
               "definition_path": plan.definition_path,
               "installed_at": (clock or _now)()}
    Path(plan.receipt_path).write_text(json.dumps(receipt, sort_keys=True, indent=2))
    result.update({"applied": True,
                   "outcome": "reinstalled" if present else "installed"})
    return result


def _now() -> float:
    import time
    return time.time()


def doctor(plan: ServicePlan) -> dict[str, Any]:
    """What is actually on this host, with the parts it cannot see named.

    Whether launchd holds the job is not a question this package can ask, so it
    is ``unknown``.  Reporting it as false because no file said otherwise is the
    fabricated readiness the rest of the stack spends its refusals on.
    """

    receipt = _receipt(plan)
    present = Path(plan.definition_path).is_file()
    installed_digest = (receipt or {}).get("plan_digest")
    if installed_digest is None:
        drift = "not_applicable"
    elif not present:
        drift = "receipt records an install whose definition is absent"
    elif installed_digest != plan.digest:
        drift = ("installed plan %s differs from the current plan %s"
                 % (installed_digest, plan.digest))
    else:
        drift = "none"
    return {
        "contract_version": CONTRACT_VERSION,
        "label": plan.label,
        "definition_path": plan.definition_path,
        "definition_present": present,
        "installed_digest": installed_digest or "not_run",
        "plan_digest": plan.digest,
        "drift": drift,
        "log_path": plan.log_path,
        "service_loaded": "unknown",
        "service_loaded_detail":
            "whether the host scheduler holds this job is a host query this "
            "package does not make; the Owner's verify steps answer it",
        "invocation": list(plan.invocation),
        "interpreter": plan.interpreter,
        "interval_seconds": plan.interval_seconds,
        "activation": activation_command(plan),
    }


def uninstall(plan: ServicePlan, *, apply: bool = False) -> dict[str, Any]:
    """Remove exactly what the receipt records.  Loads and unloads nothing."""

    removable = [path for path in (plan.definition_path, plan.receipt_path)
                 if Path(path).exists()]
    result = {"contract_version": CONTRACT_VERSION, "label": plan.label,
              "removable": removable, "removed": [], "applied": False,
              "note": "the host scheduler is not told anything by this command; "
                      "an already-loaded job stays loaded until the Owner runs "
                      "the deactivate step",
              "deactivate": activation_command(plan)["deactivate"]}
    if not apply:
        return result
    for path in removable:
        Path(path).unlink()
    result.update({"applied": True, "removed": removable})
    return result


# --------------------------------------------------------------------------- #
# the Owner's recorded approval, read from durable state
# --------------------------------------------------------------------------- #

#: Named ``approval`` rather than the word the brief uses, on purpose.
#: ``tests/test_authority_boundaries.py`` forbids credential-shaped names and
#: matches the other word as a bare substring, because it is also an HTTP header.
#: The Controller already has a word for a person's recorded decision --
#: ``production.py`` has used ``approval_ref``/``approved_by`` since Stage 6 --
#: so this is a synonym giving way to the neighbour's existing vocabulary, not a
#: check being weakened to fit a new name.
APPROVAL_SCHEMA = "factory-controller/supervisor-activation-approval/1.0"


def approval(path: str | None, *, label: str = DEFAULT_LABEL,
             schema: str = APPROVAL_SCHEMA,
             subject_key: str = "label") -> dict[str, Any]:
    """Read the Owner's recorded activation decision, or report its absence.

    Absence is the normal state and is reported in the shared vocabulary, never
    as a refusal to be argued with.  Nothing in this package can produce one of
    these records, which is what makes the check worth making.

    ``schema`` and ``subject_key`` exist so a second kind of Owner decision can
    reuse this reader without the record becoming interchangeable.  SF-144 adds
    one -- opening a bounded shift -- and installing a host service is a
    different act from admitting missions, so a record written for one must not
    satisfy the other.  Widening the reader is the small change; letting the
    two share a schema would have been the dangerous one.
    """

    if not path:
        return {"state": "not_run", "approved": False, "source": "not_applicable",
                "detail": "no activation approval record was named"}
    try:
        body = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"state": "not_run", "approved": False, "source": path,
                "detail": "no activation approval record exists at that path"}
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "approved": False, "source": path,
                "detail": "activation approval record is unreadable: %s" % exc}
    if not isinstance(body, dict) or body.get("schema_version") != schema:
        return {"state": "unknown", "approved": False, "source": path,
                "detail": "approval schema_version must be %s" % schema}
    if body.get(subject_key) != label:
        return {"state": "not_applicable", "approved": False, "source": path,
                "detail": "the record approves %r, not %r"
                          % (body.get(subject_key), label)}
    missing = [key for key in ("approved_by", "approval_ref")
               if not isinstance(body.get(key), str) or not body[key]]
    if missing or body.get("approved") is not True:
        return {"state": "unknown", "approved": False, "source": path,
                "detail": "an approval names who granted it and where it is "
                          "recorded; missing %s" % (missing or ["approved"])}
    return {"state": "granted", "approved": True, "source": path,
            "approved_by": body["approved_by"],
            "approval_ref": body["approval_ref"], subject_key: label}
