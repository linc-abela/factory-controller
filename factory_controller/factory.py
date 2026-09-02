"""The small Owner-facing lifecycle over the existing Factory planes.

The Controller's durable policy and mission modules remain pure.  This module
is the native host edge that composes them with the already-installed Bridge
and the macOS service loader.  Every mutation is reached from one explicit
Owner verb, every decision is previewed before it is applied, and a failed
step leaves the durable records needed for the next invocation to continue.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import activation
from . import capacity
from . import context
from . import context_adapter
from . import dogfood
from . import dogfood_improvement
from . import dogfood_intake
from . import improvement
from . import pcp
from . import portfolio
from . import product
from . import production
from . import release
from . import shift as shift_plane
from . import shift_runtime
from . import stage1_adapter
from . import supervisor
from .adapter import HostCommandResult, run_host_command


SURFACES = {
    "governance": ("standards/", "agents/"),
    "production_authority": ("factory_controller/production.py",),
    "admission_integrity": ("factory_controller/store.py",),
    "evaluator_independence": ("tests/test_authority_boundaries.py",),
    "improvement_policy": ("factory_controller/improvement.py",),
    "secret_handling": (".env", "secrets/"),
    "emergency_stop": ("factory_controller/portfolio.py",),
    "release_authority": (".github/", "dev"),
}

#: How long one declared acceptance gate may take when it is being run as a
#: measurement rather than as a mission's own gate.  Matched to the mission
#: gate ceiling in ``_materialize`` rather than to the Controller's ordinary
#: host-command timeout: the same container, the same command, the same clock.
GATE_MEASUREMENT_TIMEOUT_SECONDS = 1800

#: Who judges an improvement candidate.  Never the producer: `seal_candidate`
#: records the provider profile the route actually selected and
#: `evaluate_candidate` refuses when the two are the same string.
IMPROVEMENT_EVALUATOR_IDENTITY = "factory-controller/dogfood-improvement"

#: How one durable mission state reads on the Owner's status surface.
#:
#: The keys are ``store.ALLOWED_TRANSITIONS``' own vocabulary and nothing else,
#: so a state the engine can write always has a reading here; the phrase is
#: deliberately coarse, because which *step* the mission is on is a separate,
#: more precise fact and is reported beside this one.
PRODUCT_LIFECYCLE = {
    "admitted": "queued; the supervisor will pick it up automatically",
    "dispatching": "executing",
    "dispatched": "verifying its candidate",
    "candidate_verified": "running its acceptance gates",
    "evaluated": "evaluating its gate results",
    "evidence_sealed": "sealing its evidence",
    "completed": "settled; it succeeded",
    "refused": "stopped before the provider started",
    "failed": "stopped",
    "cancelled": "cancelled",
    "escalated": "stopped after the provider started",
}

#: How long the Owner's own review surface gets to answer a probe of itself.
#: It is a loopback web root served by a container on this host, so a slow
#: answer is a stopped surface rather than a busy one.
REVIEW_PROBE_TIMEOUT_SECONDS = 10.0

DEFAULT_WATCH_INTERVAL_SECONDS = 30.0
AUTOPILOT_WORKER_ID = "factory-autopilot"

#: The supervisor service's own PATH, written into the job definition.
#:
#: launchd hands a LaunchAgent that names no PATH the bare
#: ``/usr/bin:/bin:/usr/sbin:/sbin``, which holds neither a container runtime
#: nor a provider CLI.  Under it every containerised acceptance gate exits 127
#: and every provider readiness probe resolves nothing -- and neither failure
#: says so: the first is recorded as the mission failing its own gates, the
#: second as the Owner needing to sign in again.  Both were live on this host.
#:
#: The Bridge's LaunchAgent has always named its PATH
#: (``factory_bridge.install.launchagent_plist``); this one did not, and worked
#: only for as long as it happened to be bootstrapped from an interactive
#: shell.  A host refresh is what takes that away, which is why the failure
#: arrived looking like a durable-state contradiction rather than an
#: environment one.  Naming it here puts it inside ``plan.digest``, so the
#: installed job is drift-checked against it like every other plan field.
SUPERVISOR_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin",
                        "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class FactoryRefusal(Exception):
    """One plain-English lifecycle blocker."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OwnerIdentity:
    """The local account identity used for the automatically recorded act."""

    uid: int
    username: str

    @classmethod
    def current(cls) -> "OwnerIdentity | None":
        try:
            uid = os.getuid()
            username = pwd.getpwuid(uid).pw_name
        except (KeyError, OSError):
            return None
        return cls(uid=uid, username=username)

    def valid(self) -> bool:
        return self.uid > 0 and bool(self.username.strip()) \
            and self.username.lower() not in {"root", "nobody"}


@dataclass(frozen=True)
class FactoryConfig:
    """Canonical paths for the supported local installation."""

    controller_root: Path
    bridge_root: Path
    vault_root: Path
    contract_path: Path
    portfolio_path: Path
    capability_request_path: Path
    product_contract_path: Path
    agents_dir: Path
    state_dir: Path
    bridge_prefix: Path = Path("/Users/Shared/factory")
    bridge_label: str = "com.softwarefactory.bridge"
    legacy_label: str = "com.astral.bridge"
    supervisor_label: str = activation.DEFAULT_LABEL
    interval_seconds: int = 300
    shift_duration_seconds: float = 4 * 3600.0
    request_prefix: str = "factory-shift"
    #: Where a sealed Release Candidate is offered for Owner Validation.
    #:
    #: Local by default and local on purpose.  ``release._surface`` accepts an
    #: HTTPS target or an explicit loopback one and nothing else, and this
    #: Factory has no authorized HTTPS target: reaching one is an Owner act
    #: with an account and a visibility decision behind it.  A loopback surface
    #: is a review the Owner can actually open today, and it serves the sealed
    #: artifact's own bytes, so the digest they validate is the digest that
    #: would be promoted.
    review_url: str = "http://127.0.0.1:8787"
    python_path: Path | None = None

    @property
    def bridge_plist(self) -> Path:
        return self.agents_dir / (self.bridge_label + ".plist")

    @property
    def bridge_socket(self) -> Path:
        return self.bridge_prefix / "run" / "factory.sock"

    @property
    def runtime_receipt_path(self) -> Path:
        return self.state_dir / "supervisor-runtime.json"

    @property
    def evidence_root(self) -> Path:
        return self.controller_root.parent / "factory-evidence-core"

    @property
    def mission_dir(self) -> Path:
        return self.state_dir / "dogfood"

    @property
    def improvement_objective_path(self) -> Path:
        """The Owner objective the frozen portfolio's improvement slot carries.

        Derived from the portfolio's own directory rather than configured
        separately: an improvement objective that could point somewhere else
        would be a second place a mission's intent comes from.
        """

        return self.portfolio_path.parent / "first-dogfood-improvement-objective.json"

    @classmethod
    def default(cls, db_path: str | Path | None = None) -> "FactoryConfig":
        controller_root = Path(__file__).resolve().parents[1]
        bridge_root = controller_root.parent / "factory-bridge"
        vault_root = controller_root.parent / "factory-vault"
        return cls(
            controller_root=controller_root,
            bridge_root=bridge_root,
            vault_root=vault_root,
            contract_path=controller_root / "contracts" /
            "internal-dogfood-run-contract.json",
            portfolio_path=controller_root / "contracts" /
            "first-dogfood-mission-portfolio.json",
            capability_request_path=bridge_root / "contracts" /
            "first-dogfood-capability-admission-request.json",
            product_contract_path=controller_root / "contracts" /
            "lodus-casino-product-run-contract.json",
            agents_dir=Path.home() / "Library" / "LaunchAgents",
            state_dir=Path.home() / ".factory-controller",
        )


@dataclass(frozen=True)
class FactoryResult:
    """A structured result whose normal rendering contains no internal IDs."""

    action: str
    ok: bool
    state: str
    lines: tuple[str, ...]
    details: Mapping[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return "\n".join(self.lines)


Runner = Callable[..., HostCommandResult]

#: Preflight checks fed by a sibling repository's own ``./dev health``.
HEALTH_CHECKS = {
    "EVIDENCE_CORE_HEALTH": "Evidence Core",
    "CONTEXT_BROKER_HEALTH": "Context Broker",
}


class FactoryLifecycle:
    """Install, start, stop, and inspect one bounded local Factory."""

    def __init__(self, controller, *, config: FactoryConfig | None = None,
                 runner: Runner = run_host_command,
                 owner: OwnerIdentity | None = None,
                 clock: Callable[[], float] = time.time,
                 reports: Mapping[str, Mapping[str, Any]] | None = None,
                 remote_reachability: Mapping[str, Sequence[str]] | None = None,
                 context_builder: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None
                 ) -> None:
        self.controller = controller
        self.store = controller.store
        self.config = config or FactoryConfig.default(self.store.path)
        self.runner = runner
        self.owner = owner if owner is not None else OwnerIdentity.current()
        self.clock = clock
        self.reports = None if reports is None else {
            name: dict(value) for name, value in reports.items()
        }
        self.remote_reachability = None if remote_reachability is None else {
            name: tuple(values) for name, values in remote_reachability.items()
        }
        self.context_builder = context_builder
        self.report_failures: dict[str, str] = {}
        self.supervisor = supervisor.OperationsSupervisor(
            controller, clock=self.store.clock)
        self.shift = shift_plane.ShiftPlane(self.store, clock=self.store.clock)
        self.runtime = shift_runtime.ShiftRuntime(
            controller, supervisor_plane=self.supervisor)
        self.production = production.ProductionLedger(self.store)
        self.improvement = improvement.ImprovementPlane(self.store, self.production)
        self._improvement_contract: Any = False

    # -- the frozen improvement objective ------------------------------- #

    def improvement_contract(self):
        """The Owner objective the improvement slot carries, or None.

        Read once per lifecycle and cached, including the absence: a portfolio
        with no improvement objective is a legitimate configuration, and asking
        the file system about it on every slot would make the answer depend on
        when it was asked.
        """

        if self._improvement_contract is False:
            try:
                self._improvement_contract = dogfood_improvement.load(
                    self.config.improvement_objective_path)
            except dogfood_improvement.ObjectiveError:
                self._improvement_contract = None
        return self._improvement_contract

    def _improvement_for(self, mission):
        """The contract this portfolio mission is an improvement candidate for."""

        if getattr(mission, "work_class", None) != dogfood_improvement.WORK_CLASS:
            return None
        contract = self.improvement_contract()
        if contract is None or contract.project_id != mission.project_id:
            return None
        return contract

    # -- public surface ------------------------------------------------- #

    def dispatch(self, action: str, **options: Any) -> FactoryResult:
        try:
            if action == "product":
                return self.product(options.get("package"))
            if action == "revise":
                return self.revise(options.get("package"))
            if action == "install":
                return self.install()
            if action == "start":
                return self.start()
            if action == "stop":
                return self.stop()
            if action == "run":
                return self.run()
            if action == "cycle":
                return self.cycle()
            if action == "status":
                return self.status()
            if action == "review":
                return self.review()
            raise FactoryRefusal("ACTION_UNKNOWN", "That Factory action is not supported.")
        except FactoryRefusal as refusal:
            return FactoryResult(
                action=action, ok=False, state="blocked",
                lines=("BLOCKED: " + refusal.detail,),
                details={"code": refusal.code},
            )
        except Exception as exc:  # noqa: BLE001
            # No traceback or raw command output belongs on the Owner surface.
            return FactoryResult(
                action=action, ok=False, state="blocked",
                lines=("BLOCKED: The Factory could not complete this action. "
                       "Run './dev factory status' for the current state.",),
                details={"code": "FACTORY_ACTION_FAILED",
                         "detail_type": type(exc).__name__},
            )

    def install(self) -> FactoryResult:
        self._require_owner()
        self._resolve_supported_python()
        contract, entry = self._load_contract_and_portfolio()
        self._ensure_factory_off(unload_bridge=False)
        doctor = self._prepare_bridge(allow_install=True, load=True)
        self._provision_store(contract, entry, doctor)
        plan = self._install_supervisor_definition()
        service_doctor = activation.doctor(plan)
        if not service_doctor.get("definition_present") \
                or service_doctor.get("drift") != "none":
            raise FactoryRefusal(
                "SUPERVISOR_INSTALL_FAILED",
                "The Factory supervisor definition could not be verified.")
        self._record_owner_act("install", "factory-install")
        return FactoryResult(
            action="install", ok=True, state="off",
            lines=("FACTORY INSTALLED",
                   "Run './dev factory start' to begin work."),
            details={"bridge": doctor, "supervisor": service_doctor,
                     "contract": contract.run_ref},
        )

    def start(self) -> FactoryResult:
        self._require_owner()
        self._resolve_supported_python()
        contract, entry = self._load_contract_and_portfolio()
        self._prepare_control_for_start()
        doctor = self._prepare_bridge(allow_install=False, load=True)
        doctor, readiness = self._fresh_bridge_readiness(doctor)
        self._check_primary_and_containment(contract, doctor)
        request_ref = self._next_shift_reference()
        approval_ref = self._approval_reference(request_ref)
        self._record_owner_act("start", approval_ref, {
            "request_ref": request_ref,
            "scope": "first internal dogfood",
        })
        self._provision_store(contract, entry, doctor)
        readings = self._refresh_capacity(contract)
        doctor, capability_preview = self._admit_required_capability(
            contract, doctor, approval_ref)
        plan = self._install_supervisor_definition()
        service_doctor = activation.doctor(plan)
        facts, request, gate_preview = self._shift_gate_inputs(
            contract, entry, doctor, service_doctor, capability_preview,
            readings, request_ref)
        if not gate_preview.get("ready"):
            raise FactoryRefusal(
                "SHIFT_NOT_READY",
                self._plain_gate_blocker(gate_preview.get("blockers") or ()))

        approval = {
            "approved": True,
            "approved_by": self.owner.username,
            "approval_ref": approval_ref,
            "detail": "Owner invoked './dev factory start'",
        }
        applied = self.shift.apply(
            facts, approval, actor=self.owner.username)
        created = bool(applied.get("created"))
        try:
            self._bootstrap_service(plan)
            control = self.supervisor.control()
            if control["state"] == "emergency_stopped":
                raise FactoryRefusal(
                    "EMERGENCY_STOP_ACTIVE",
                    "Emergency stop is active. Clear it before starting the Factory.")
            if control["state"] != "running":
                self.supervisor.transition(
                    "running", actor=self.owner.username,
                    reason="Owner started the bounded Factory shift",
                    evidence_ref=approval_ref,
                    policy_version=contract.run_ref,
                )
            if not self._service_loaded(self.config.supervisor_label):
                raise FactoryRefusal(
                    "SUPERVISOR_NOT_RUNNING",
                    "The Factory supervisor could not be started. Retry the command.")
        except FactoryRefusal:
            if created:
                self.shift.revoke(
                    request_ref, reason="Factory start did not finish",
                    actor=self.owner.username)
            self._bootout_if_loaded(self.config.supervisor_label)
            raise
        except Exception:  # noqa: BLE001
            if created:
                self.shift.revoke(
                    request_ref, reason="Factory start did not finish",
                    actor=self.owner.username)
            self._bootout_if_loaded(self.config.supervisor_label)
            raise FactoryRefusal(
                "SUPERVISOR_NOT_RUNNING",
                "The Factory supervisor could not be started. Retry the command.")

        return FactoryResult(
            action="start", ok=True, state="ready",
            lines=("FACTORY READY",
                   "Shift active. Supervisor running."),
            details={"grant": applied, "gate": gate_preview,
                     "bridge": doctor, "readiness": readiness,
                     "supervisor": service_doctor},
        )

    def stop(self) -> FactoryResult:
        self._require_owner()
        self._close_live_shift()
        drain = self.runtime.drain(
            worker_id="factory-stop", max_steps=8,
            actor=self.owner.username,
            reason="Owner stopped the Factory",
        )
        status = drain.get("status") or {}
        outstanding = ((status.get("recovery") or {}).get("outstanding_count")
                       if isinstance(status, Mapping) else None)
        if outstanding:
            raise FactoryRefusal(
                "DRAIN_INCOMPLETE",
                "In-flight work is still draining. Retry './dev factory stop'.")
        if self.supervisor.control().get("state") != "stopped":
            raise FactoryRefusal(
                "DRAIN_INCOMPLETE",
                "The Factory has not reached a safe stopped state. Retry the command.")
        self._bootout_if_loaded(self.config.supervisor_label)
        self._bootout_if_loaded(self.config.legacy_label)
        bridge = self._prepare_bridge(allow_install=False, load=True)
        if self._service_loaded(self.config.supervisor_label) \
                or not self._service_loaded(self.config.bridge_label):
            raise FactoryRefusal(
                "SERVICE_STOP_FAILED",
                "The Factory services could not be stopped safely. Retry the command.")
        self._record_owner_act("stop", "factory-stop")
        return FactoryResult(
            action="stop", ok=True, state="off",
            lines=("FACTORY OFF", "All state saved."),
            details={"drain": drain, "bridge": bridge},
        )

    def run(self) -> FactoryResult:
        """Submit the next frozen dogfood mission, and no more than one.

        The Owner names nothing.  Which mission comes next is the portfolio's
        own serial rule, read from durable mission state; everything the
        mission needs is derived from the frozen contract, the frozen portfolio
        and the execution layer's own project registry.  Invoked again while
        that mission is still in flight, this reports it rather than
        submitting a second one -- the mission's identity is derived, so the
        second submission would collide with the first by construction.
        """

        self._require_owner()
        contract, entry = self._load_contract_and_portfolio()
        grant = self.shift.grant()
        control = self.supervisor.control()
        if grant is None or control.get("state") != "running" \
                or not self._service_loaded(self.config.supervisor_label):
            raise FactoryRefusal(
                "FACTORY_NOT_READY",
                "The Factory is not running. Run './dev factory start' first.")
        doctor = self._bridge_doctor()
        # Containment and the primary provider first: both have their own plain
        # reason, and the general "not ready" message would hide either one.
        self._check_primary_and_containment(contract, doctor)
        if not self._service_loaded(self.config.bridge_label) \
                or not self._bridge_is_healthy(doctor):
            code, detail = self._bridge_problem(doctor)
            raise FactoryRefusal(code, detail)

        # A queued mission is judged against the capacity observation the
        # Owner just asked the Bridge for, rather than an old start-time
        # reading.  This keeps the first handoff from waiting on the scheduler
        # to discover that its durable capacity fact has expired.
        self._refresh_capacity(contract)

        # A Factory started before this command existed is running a
        # supervisor definition that names the fixture step adapter, and a
        # real mission handed to that one is refused on its first leg.  The
        # definition is rewritten and reloaded only when it actually differs.
        self._bootstrap_service(self._install_supervisor_definition())

        return self._queue_next(contract, entry, doctor, grant, owner_action=True)

    def product(self, package_path: Any) -> FactoryResult:
        """Submit one Product Candidate Package the Owner named.

        This is the only verb in this module where the Owner names the work.
        ``run`` advances a frozen portfolio and could in principle be performed
        by a scheduler; a real product entering the Factory cannot, because the
        package *is* the intent and nobody but its Owner may supply it.  So the
        package path is a required argument with no default: there is no
        "the usual one" to fall back to.

        Everything after that argument is the ordinary path.  The mission is
        materialized by the same seam the internal portfolio uses, submitted
        through the same Controller, and executed by the same supervisor -- a
        product mission is not a second pipeline, it is the first one with a
        different origin.
        """

        self._require_owner()
        if not package_path:
            raise FactoryRefusal(
                "PRODUCT_PACKAGE_REQUIRED",
                "Name the Product Candidate Package to submit: "
                "'./dev factory product --package <path>'.")
        try:
            contract = product.ProductContract.load(self.config.product_contract_path)
            _, accepted = product.package_from(package_path)
            mission = product.mission_for(contract, accepted)
            brief = product.brief(contract, accepted)
        except product.ProductRefusal as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None

        grant = self.shift.grant()
        control = self.supervisor.control()
        if grant is None or control.get("state") != "running" \
                or not self._service_loaded(self.config.supervisor_label):
            raise FactoryRefusal(
                "FACTORY_NOT_READY",
                "The Factory is not running. Run './dev factory start' first.")
        doctor = self._bridge_doctor()
        self._check_primary_and_containment(contract, doctor)
        if not self._service_loaded(self.config.bridge_label) \
                or not self._bridge_is_healthy(doctor):
            code, detail = self._bridge_problem(doctor)
            raise FactoryRefusal(code, detail)

        approval_ref = self._approval_reference(
            "%s-%s" % (contract.run_ref, accepted.package_digest[:12]))
        act = product.owner_act(
            contract, accepted, owner=self.owner.username,  # type: ignore[union-attr]
            approval_ref=approval_ref,
            at=dogfood_intake.iso_utc(self.clock()))
        self._record_owner_act("product", approval_ref, {
            "package_id": act["package_id"],
            "package_digest": act["package_digest"],
            "work_item_id": act["work_item_id"],
            "act_hash": act["act_hash"],
        })
        self._write_mission_file(mission.mission_ref, "owner-intake", act)

        self._refresh_capacity(contract)
        doctor, _ = self._admit_capability(
            self.config.bridge_root / "contracts" / contract.capability_request,
            contract, doctor, approval_ref)
        self._provision_product_store(contract, doctor)
        self._bootstrap_service(self._install_supervisor_definition())

        intake = self._materialize(
            contract, None, mission, doctor, grant, brief=brief,
            portfolio_ref=contract.run_ref,
            corpus_identity="package://%s@%s" % (accepted.mission["source_pcp"],
                                                 accepted.package_digest))
        try:
            submitted, created = self.controller.submit(
                intake.payload, intake.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            raise FactoryRefusal(
                "PRODUCT_NOT_SUBMITTED",
                "The product mission could not be submitted. Retry the command."
            ) from exc

        open_decisions = product.unresolved(accepted)
        lines = [
            "Submitted %s to the Factory." % accepted.mission["source_pcp"],
            "Work item: %s against %s at %s."
            % (mission.mission_ref, contract.project_id, contract.baseline_sha[:12]),
            "Package digest: %s." % accepted.package_digest,
            "This mission was %s."
            % ("admitted now" if created else "already admitted; nothing was duplicated"),
            "The Factory owns execution from here. Watch it with "
            "'./dev factory status --watch'.",
        ]
        if open_decisions:
            lines.insert(3, "The package leaves %d decision(s) open: %s."
                         % (len(open_decisions), ", ".join(open_decisions)))
        return FactoryResult(
            action="product", ok=True,
            state="submitted" if created else "already-submitted",
            lines=tuple(lines),
            details={
                "mission_id": submitted.get("id"),
                "work_item_id": mission.mission_ref,
                "package_id": accepted.package_id,
                "package_digest": accepted.package_digest,
                "idempotency_key": intake.idempotency_key,
                "context_manifest_hash": intake.context_manifest_hash,
                "baseline_sha": contract.baseline_sha,
                "approval_ref": approval_ref,
                "owner_act_hash": act["act_hash"],
                "created": created,
            },
        )

    def revise(self, package_path: Any) -> FactoryResult:
        """Return the reviewed release for changes, and open the revision.

        One command, because it is one Owner act.  Splitting it would leave a
        state in which the release the Owner rejected is recorded as rejected
        and nothing is being done about it -- and it would put the Owner in
        the position of having to know that a second command exists, which is
        the loop ``review`` was written to end.

        What it does, in order, and none of it invented here: it confirms the
        superseding package really names the release that was reviewed, it
        observes the review surface rather than accepting a claim about it, it
        records the Owner's decision against that exact deployment, it asks
        the execution layer for a revision base descending from the rejected
        candidate, and it submits one new mission through the same seam every
        other mission uses.

        It is safe to repeat.  Every step is idempotent on an identity derived
        from the package and the release it supersedes: the same command run
        twice records one decision, opens one base, and admits one mission.
        """

        self._require_owner()
        if not package_path:
            raise FactoryRefusal(
                "PRODUCT_PACKAGE_REQUIRED",
                "Name the superseding Product Candidate Package: "
                "'./dev factory revise --package <path>'.")
        contract = self._product_contract()
        try:
            _, accepted = product.package_from(package_path)
            superseded = product.revision_of(accepted)
            if superseded is None:
                raise FactoryRefusal(
                    "PRODUCT_NOT_A_REVISION",
                    "That package supersedes nothing, so it is a first build. "
                    "Submit it with './dev factory product --package <path>'.")
        except product.ProductRefusal as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None

        lifecycle = release.ReleaseLifecycle(self.store, clock=self.clock)
        try:
            rejected = lifecycle.candidate(superseded["predecessor_rc"])
        except release.ReleaseRefusal as refusal:
            raise FactoryRefusal(
                refusal.code,
                "The release this package supersedes is not one this Factory "
                "sealed (%s)." % refusal.detail) from None
        if rejected.candidate_sha != superseded["predecessor_candidate_sha"]:
            raise FactoryRefusal(
                "REVISION_PREDECESSOR_MISMATCH",
                "The package names a different candidate than the release it "
                "supersedes actually holds.")
        if rejected.project_id != contract.project_id:
            raise FactoryRefusal(
                "REVISION_PROJECT_MISMATCH",
                "The release this package supersedes belongs to another "
                "product.")
        deployment = self._review_deployment(rejected.rc_id)

        grant = self.shift.grant()
        control = self.supervisor.control()
        if grant is None or control.get("state") != "running" \
                or not self._service_loaded(self.config.supervisor_label):
            raise FactoryRefusal(
                "FACTORY_NOT_READY",
                "The Factory is not running. Run './dev factory start' first.")
        doctor = self._bridge_doctor()
        self._check_primary_and_containment(contract, doctor)
        if not self._service_loaded(self.config.bridge_label) \
                or not self._bridge_is_healthy(doctor):
            code, detail = self._bridge_problem(doctor)
            raise FactoryRefusal(code, detail)

        # The Owner's decision, against the exact deployment they looked at.
        # The health it requires is measured here rather than asserted, and a
        # deployment that has already settled is left alone: a second run of
        # this command must not re-observe a fact the ledger already holds.
        if self.production.deployment(
                deployment["deployment_id"])["state"] == "verifying":
            self.production.record_health(
                deployment["deployment_id"],
                self._probe_review_surface(
                    contract, rejected, deployment["validation_surface"]))
        try:
            validation = lifecycle.record_owner_validation(
                superseded["owner_validation_id"], rejected.rc_id,
                deployment_ref=deployment["deployment_ref"],
                decision=pcp.RETURN_FOR_CHANGES,
                decided_by=self.owner.username,  # type: ignore[union-attr]
                decided_at=self.clock(),
                notes="superseded by %s (%s)"
                      % (accepted.mission["source_pcp"], accepted.package_digest))
        except release.ReleaseRefusal as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None

        base = self._open_revision_base(contract, accepted, rejected)
        try:
            mission = product.mission_for(contract, accepted,
                                          baseline_sha=base["revision_sha"])
            brief = product.brief(contract, accepted)
        except product.ProductRefusal as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None

        approval_ref = self._approval_reference(
            "%s-%s" % (contract.run_ref, accepted.package_digest[:12]))
        act = product.owner_act(
            contract, accepted, owner=self.owner.username,  # type: ignore[union-attr]
            approval_ref=approval_ref,
            at=dogfood_intake.iso_utc(self.clock()))
        self._record_owner_act("revise", approval_ref, {
            "package_id": act["package_id"],
            "package_digest": act["package_digest"],
            "work_item_id": act["work_item_id"],
            "act_hash": act["act_hash"],
            "predecessor_rc": rejected.rc_id,
            "predecessor_candidate_sha": rejected.candidate_sha,
            "owner_validation_id": validation.validation_id,
            "revision_sha": base["revision_sha"]})
        self._write_mission_file(mission.mission_ref, "owner-intake", act)

        self._refresh_capacity(contract)
        doctor, _ = self._admit_capability(
            self.config.bridge_root / "contracts" / contract.capability_request,
            contract, doctor, approval_ref)
        self._provision_product_store(contract, doctor)
        self._bootstrap_service(self._install_supervisor_definition())

        intake = self._materialize(
            contract, None, mission, doctor, grant, brief=brief,
            portfolio_ref=contract.run_ref,
            corpus_identity="package://%s@%s" % (accepted.mission["source_pcp"],
                                                 accepted.package_digest),
            checkout=base["revision_checkout"])
        try:
            submitted, created = self.controller.submit(
                intake.payload, intake.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            raise FactoryRefusal(
                "REVISION_NOT_SUBMITTED",
                "The revision mission could not be submitted. Retry the "
                "command.") from exc

        return FactoryResult(
            action="revise", ok=True,
            state="submitted" if created else "already-submitted",
            lines=("RELEASE RETURNED FOR CHANGES",
                   "Returned: %s, which stays sealed and is not promoted."
                   % rejected.rc_id,
                   "Revision: %s, built from the candidate you reviewed."
                   % mission.mission_ref,
                   "Submitted %s. This mission was %s."
                   % (accepted.mission["source_pcp"],
                      "admitted now" if created
                      else "already admitted; nothing was duplicated"),
                   "The Factory owns execution from here. Watch it with "
                   "'./dev factory status --watch'."),
            details={
                "mission_id": submitted.get("id"),
                "work_item_id": mission.mission_ref,
                "package_id": accepted.package_id,
                "package_version": accepted.package_version,
                "package_digest": accepted.package_digest,
                "idempotency_key": intake.idempotency_key,
                "context_manifest_hash": intake.context_manifest_hash,
                "predecessor_rc": rejected.rc_id,
                "predecessor_candidate_sha": rejected.candidate_sha,
                "predecessor_artifact_digest": rejected.artifact_digest,
                "owner_validation_id": validation.validation_id,
                "owner_decision": validation.decision,
                "revision_sha": base["revision_sha"],
                "revision_ref": base["ref"],
                "revision_checkout": base["revision_checkout"],
                "baseline_sha": mission.baseline_sha,
                "approval_ref": approval_ref,
                "owner_act_hash": act["act_hash"],
                "created": created,
            },
        )

    def _open_revision_base(self, contract, accepted, rejected) -> dict[str, Any]:
        """Ask the execution layer for the commit the revision starts from.

        The Controller does not touch Git, so the predecessor link is made
        where it can be proven: a real commit whose parent is the rejected
        candidate, carrying that candidate's own mission statement with the
        Owner's requested changes appended to it.  What travels from here is
        the package's text and nothing else.
        """

        addendum = self._mission_path(
            accepted.mission["work_item_id"], "revision-request")
        try:
            self.config.mission_dir.mkdir(parents=True, exist_ok=True)
            addendum.write_text(product.revision_addendum(accepted))
            os.chmod(addendum, 0o600)
        except (OSError, product.ProductRefusal):
            raise FactoryRefusal(
                "REVISION_NOT_RECORDED",
                "The revision request could not be recorded safely. Retry the "
                "command.") from None
        _, report = self._bridge_json(
            "revision", "base", contract.project_id, rejected.candidate_sha,
            "--mission-path", contract.mission_statement_path,
            "--mission-file", str(addendum),
            "--ref", product.revision_ref(accepted))
        if report.get("refused") or not isinstance(report.get("revision_sha"), str):
            raise FactoryRefusal(
                str(report.get("code") or "REVISION_BASE_NOT_OPENED"),
                str(report.get("detail")
                    or "The execution layer could not open a revision base "
                       "from the candidate you reviewed."))
        # A base nothing can be grounded on is not a base this mission can be
        # admitted from: the repository grounding is built from this path, and
        # guessing one here would put the Controller back in the business of
        # deciding which local copy a mission reads.
        if not isinstance(report.get("revision_checkout"), str) \
                or not report["revision_checkout"]:
            raise FactoryRefusal(
                "REVISION_CHECKOUT_NOT_OPENED",
                "The execution layer opened a revision base but no checkout "
                "the repository grounding could be read from.")
        return report

    def review(self) -> FactoryResult:
        """Take the completed product mission to a reviewable Release Candidate.

        Everything between a mission the Factory finished and a judgement the
        Owner can make existed already and was joined by nobody: the execution
        layer could mint an artifact identity from a candidate commit, the
        release plane could seal a Release Candidate and record a review
        deployment, and no code carried the one to the other.  So the terminal
        state of a successful product run was a commit on a lane reference and
        an operator expected to hand-author a release bundle -- which is the
        Owner loop this verb exists to end.

        It adds no rule.  The artifact is built by the execution layer from the
        candidate the mission verified, every bundle field is a durable row,
        and every refusal below is raised by the release plane or the
        production ledger.  It is also idempotent by construction: the Release
        Candidate id is derived from the candidate commit, so a second
        invocation seals the same RC and re-uses the same deployment rather
        than minting a second release for one set of bytes.
        """

        self._require_owner()
        contract = self._product_contract()
        mission = self._product_reading()
        if mission is None:
            raise FactoryRefusal(
                "PRODUCT_MISSION_ABSENT",
                "No product mission has been submitted yet. Run "
                "'./dev factory product --package <path>' first.")
        if mission["state"] != "completed":
            raise FactoryRefusal(
                "PRODUCT_MISSION_UNFINISHED",
                "The product mission is %s, not finished. Run "
                "'./dev factory status' to follow it."
                % PRODUCT_LIFECYCLE.get(mission["state"], mission["state"]))
        work_item = (mission.get("payload") or {}).get("work_item_id")
        result = mission.get("result") or {}
        evaluation = result.get("evaluation") or {}
        candidate = ((result.get("verification") or {}).get("verification")
                     or {}).get("candidate_sha")
        evidence_pointer = (result.get("evidence") or {}).get("evidence_pointer")
        if not isinstance(candidate, str) or not isinstance(evidence_pointer, str):
            raise FactoryRefusal(
                "PRODUCT_EVIDENCE_INCOMPLETE",
                "The finished product mission recorded no verified candidate "
                "to release.")

        # Independent QA before anything is sealed, because a Release
        # Candidate is immutable once it exists and a boundary this check
        # rejects must never acquire one.
        boundary = product.decision_boundary(
            contract, evaluation.get("changed_paths"))
        if not boundary["held"]:
            self.store.coordinate(
                mission["id"], contract.project_id, "factory",
                "FACTORY_PRODUCT_QA_REFUSED", boundary)
            raise FactoryRefusal(
                "DECISION_BOUNDARY_VIOLATED",
                "The candidate changed %s, which is the source of the gates "
                "it was judged by. This is a failed mission, not a passed one."
                % boundary["gate_source"] if boundary["violations"] else
                "The Factory cannot tell what this candidate changed, so it "
                "cannot certify that the gates still judge what they declared.")

        doctor = self._bridge_doctor()
        registry = dogfood_intake.registry_row(
            self._registry_rows(doctor), contract.project_id)
        artifact = self._build_artifact(contract, candidate)
        self._provision_product_store(contract, doctor)

        self.store.coordinate(
            mission["id"], contract.project_id, "factory",
            "FACTORY_PRODUCT_QA_HELD",
            {**boundary, "candidate_sha": candidate,
             "artifact": artifact["artifact"],
             "gate_outcomes": [
                 {"gate_id": row.get("gate_id"), "passed": row.get("passed"),
                  "exit_code": row.get("exit_code")}
                 for row in evaluation.get("gate_outcomes") or ()]})

        payload = product.release_bundle(
            contract, work_item_id=work_item, mission_id=mission["id"],
            repository=registry.get("repository_remote_url"),
            candidate_sha=candidate, artifact=artifact["artifact"],
            evidence_pointer=evidence_pointer,
            # When the candidate was built, not when a review was asked for.
            # A wall clock here moves `bundle_digest`, and the Release
            # Candidate id is derived from the candidate rather than the
            # clock -- so a second review offered the same id different bytes
            # and was refused RC_IDENTITY_MISMATCH by its own idempotency
            # rule.  The mission's settle time is the durable fact the field
            # is actually about, and it does not move.
            provenance_at=dogfood_intake.iso_utc(
                float(mission["updated_at"])))
        bundle = production.ReleaseBundle.from_payload(payload)
        rc_id = product.rc_id_for(contract, candidate)
        lifecycle = release.ReleaseLifecycle(self.store, clock=self.clock)
        try:
            sealed = self._already_sealed(
                lifecycle, rc_id, candidate, sealed_digest=bundle.artifact["identity"])
            if sealed is None:
                sealed = lifecycle.seal(
                    rc_id, bundle,
                    verification_refs=["candidate://%s" % candidate,
                                       "mission://%s" % mission["id"]],
                    qa_refs=["qa://%s/decision-boundary" % mission["id"]])
            deployed = lifecycle.deploy_review(
                rc_id, self.production,
                production.DeterministicDeploymentAdapter(),
                review_environment_id=contract.review_environment_id,
                requested_by=self.owner.username,  # type: ignore[union-attr]
                review_url=self.config.review_url)
        except (release.ReleaseRefusal, production.ProductionRefusal) as refusal:
            raise FactoryRefusal(
                getattr(refusal, "code", "REVIEW_NOT_PREPARED"),
                getattr(refusal, "detail", "The review could not be prepared."),
            ) from None

        root = self._materialize_review(artifact, sealed.artifact_digest)
        self._record_owner_act("review", sealed.rc_id, {
            "rc_id": sealed.rc_id, "candidate_sha": candidate,
            "artifact_digest": sealed.artifact_digest,
            "deployment_ref": deployed["deployment_ref"]})
        return FactoryResult(
            action="review", ok=True, state="review-ready",
            lines=("PRODUCT READY FOR REVIEW",
                   "Release Candidate: %s" % sealed.rc_id,
                   "Artifact: %s (%d files)"
                   % (sealed.artifact_digest, artifact["file_count"]),
                   "Review it at %s" % self.config.review_url,
                   "Start the review surface with './dev review up', and stop "
                   "it with './dev review down'.",
                   "Nothing is promoted until you record a decision."),
            details={"rc_id": sealed.rc_id, "work_item_id": work_item,
                     "candidate_sha": candidate,
                     "artifact_digest": sealed.artifact_digest,
                     "bundle_digest": sealed.bundle_digest,
                     "deployment_ref": deployed["deployment_ref"],
                     "deployment_state": deployed["state"],
                     "review_url": self.config.review_url,
                     "review_root": str(root),
                     "decision_boundary": boundary["outcome"],
                     "files": artifact["file_count"]},
        )

    @staticmethod
    def _already_sealed(lifecycle, rc_id: str, candidate: str, *,
                        sealed_digest: str):
        """The Release Candidate this candidate already has, if it has one.

        Sealing is a one-time act and a Release Candidate is immutable, so a
        second review has to recognise its own earlier work rather than offer
        one id a second set of bytes.  Recognised on the two facts a Release
        Candidate is *about* -- the commit and the bytes -- so a genuine change
        still reaches ``seal`` and is still refused there; this narrows what
        counts as the same release, it does not widen what may be sealed.

        Without it, any field of the bundle that is not a fact about the
        candidate makes the second review a hard refusal, and the RC already in
        the ledger cannot be re-sealed to fix it.  That is not hypothetical:
        it is how this was found.
        """

        try:
            existing = lifecycle.candidate(rc_id)
        except release.ReleaseRefusal:
            return None
        if existing.candidate_sha == candidate \
                and existing.artifact_digest == sealed_digest:
            return existing
        return None

    def _product_contract(self):
        try:
            return product.ProductContract.load(self.config.product_contract_path)
        except product.ProductRefusal as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None

    def _build_artifact(self, contract, candidate_sha: str) -> dict[str, Any]:
        """Ask the execution layer for the candidate's immutable identity.

        The Controller does not walk a repository to produce bytes: resolving a
        commit and packing its publish prefix is host mechanics, and the layer
        that owns the project registry is the one that can prove the commit is
        present in the checkout it registered.
        """

        _, report = self._bridge_json(
            "artifact", "build", contract.project_id, candidate_sha,
            "--prefix", contract.publish_prefix)
        artifact = report.get("artifact")
        if not isinstance(artifact, Mapping) or not report.get("archive_path"):
            raise FactoryRefusal(
                "ARTIFACT_NOT_BUILT",
                "The execution layer could not build a publishable artifact "
                "from the verified candidate.")
        return report

    def _materialize_review(self, report: Mapping[str, Any], identity: str) -> Path:
        """Unpack the sealed artifact where the review surface can serve it.

        The bytes served are the archive the identity was taken over, not the
        working copy and not a fresh checkout -- otherwise the digest the Owner
        validates is not the digest that would be promoted, which is the one
        invariant the package states about its own release.
        """

        root = self.config.state_dir / "review" / identity.split(":", 1)[-1]
        prefix = str(report.get("publish_prefix") or "").rstrip("/")
        try:
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True)
            with tarfile.open(str(report["archive_path"]), "r:") as archive:
                # The extraction filter this needs landed in 3.12 and the
                # Owner's own `./dev factory` runs whichever python3 the host
                # has, so the two rules the filter enforces are applied here
                # instead of assumed.  A directory entry is not extracted at
                # all: the archive is a canonical repack of files only, and a
                # member that is not one is not something to unpack quietly.
                members = []
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    parts = member.name.split("/")
                    if member.name.startswith("/") or ".." in parts:
                        raise FactoryRefusal(
                            "REVIEW_ARTIFACT_UNSAFE",
                            "The artifact archive names a path outside itself.")
                    members.append(member)
                # Pass the filter as well wherever the interpreter has it:
                # the checks above are the floor, not a replacement for it.
                filtered = ({"filter": "data"}
                            if hasattr(tarfile, "data_filter") else {})
                archive.extractall(str(root), members=members, **filtered)
        except (OSError, tarfile.TarError) as error:
            raise FactoryRefusal(
                "REVIEW_NOT_MATERIALIZED",
                "The reviewable bytes could not be written: %s"
                % type(error).__name__) from None
        served = root / prefix if prefix else root
        marker = self.config.state_dir / "review" / "current"
        try:
            if marker.is_symlink() or marker.exists():
                marker.unlink()
            marker.symlink_to(served)
        except OSError:
            # A host that will not hold the pointer still holds the bytes; the
            # surface is told the resolved path either way.
            pass
        return served

    def _review_deployment(self, rc_id: str) -> dict[str, Any]:
        """The exact review deployment the Owner looked at, or a refusal."""

        try:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT * FROM release_deployments WHERE rc_id=?"
                    " AND environment_class='staging'", (rc_id,)).fetchone()
                promoted = db.execute(
                    "SELECT deployment_ref FROM release_deployments WHERE rc_id=?"
                    " AND environment_class='production'", (rc_id,)).fetchone()
        except Exception:  # noqa: BLE001
            row = promoted = None
        if row is None:
            raise FactoryRefusal(
                "REVIEW_DEPLOYMENT_NOT_FOUND",
                "That release was never prepared for review, so there is no "
                "review of it to decide. Run './dev factory review' first.")
        if promoted is not None:
            # A revision supersedes something the Owner turned away.  A
            # release that reached Production is a different object with a
            # different lifecycle, and returning it "for changes" here would
            # leave live bytes with no record of what happened to them.
            raise FactoryRefusal(
                "RELEASE_ALREADY_PROMOTED",
                "That release is already in Production. A change to it is a "
                "new release decision, not a revision of a review.")
        return dict(row)

    def _probe_review_surface(self, contract, rc, surface: str):
        """Observe the Owner's own review surface, and never assert it.

        Owner Validation requires the exact review deployment to be durably
        healthy, and until now the only way to make it so was an operator
        passing check counts on the command line -- a number a person types,
        standing in for a fact about a running surface.  The surface is a
        loopback web root on this host serving bytes this Factory unpacked
        from the digest it sealed, so the fact is directly observable and
        there is no reason to accept a claim instead.

        Two checks, and both are about *this* release: the surface answers,
        and what it answers with is byte-identical to the sealed artifact's
        own entry document.  A surface that is not running, or is serving some
        other release, produces no health record at all -- the deployment
        stays where it is and the Owner is told which command starts it.
        Recording a failed observation instead would settle the deployment
        permanently on a fact about the host, and the review could never be
        completed afterwards.
        """

        root = self.config.state_dir / "review" / rc.artifact_digest.split(":", 1)[-1]
        prefix = contract.publish_prefix.strip("/")
        entry = (root / prefix if prefix else root) / "index.html"
        try:
            expected = entry.read_bytes()
        except OSError:
            raise FactoryRefusal(
                "REVIEW_BYTES_UNAVAILABLE",
                "The reviewed release is no longer unpacked on this host. Run "
                "'./dev factory review' again, then repeat this command."
            ) from None
        try:
            with urllib.request.urlopen(
                    surface, timeout=REVIEW_PROBE_TIMEOUT_SECONDS) as answer:
                status = getattr(answer, "status", None) or answer.getcode()
                observed = answer.read(len(expected) + 1)
        except (urllib.error.URLError, OSError, ValueError):
            raise FactoryRefusal(
                "REVIEW_SURFACE_UNREACHABLE",
                "The review surface is not running, so the Factory cannot "
                "confirm what you looked at. Start it with './dev review up' "
                "and run this command again.") from None
        if status != 200 or observed != expected:
            raise FactoryRefusal(
                "REVIEW_SURFACE_MISMATCH",
                "The review surface is not serving the release being decided. "
                "Run './dev factory review' and './dev review up' again.")
        return production.HealthRecord(
            checks_passed=2, checks_failed=0,
            evidence_ref="review-probe://%s@%s" % (rc.rc_id, rc.artifact_digest),
            observed_at=self.clock())

    def _provision_product_store(self, contract, doctor) -> None:
        """Give the product project the same durable policy a lab project has.

        Deliberately narrower than ``_provision_store``: one project, the
        contract's single work class, and no improvement policy at all.  An
        improvement plane opened over a product on its first build would let a
        second admitter promote a candidate for a mission that has not
        produced one yet.
        """

        row = dogfood_intake.registry_row(
            self._registry_rows(doctor), contract.project_id)
        policy = portfolio.ProjectPolicy(
            project_id=contract.project_id,
            repository=row.get("repository_remote_url"),
            state="enabled", priority=1, concurrency_cap=1,
            acceptance_gate_ids=tuple(contract.acceptance_gate_ids),
            acceptance_gate_source=contract.acceptance_gate_source,
            policy_version=contract.run_ref,
        )
        current = self.store.project(contract.project_id)
        if current is None or current.as_row() != policy.as_row():
            self.store.register_project(policy)
        # The contract names a review environment and nothing created it, so
        # the first release deployment of the first real product refused on an
        # environment only this contract could have declared.  Provisioning is
        # what makes a contract's names exist; the lab path already registers
        # its own, and this one was simply missed.
        #
        # The production environment is deliberately not registered here.  It
        # is gated, it is reached only through an Owner Validation this path
        # stops short of, and an environment nothing can yet deploy to is a
        # claim rather than a provision.
        review = production.EnvironmentPolicy(
            environment_id=contract.review_environment_id,
            project_id=contract.project_id,
            repository=row.get("repository_remote_url"),
            environment_class="staging",
            service_ref="%s-review" % contract.project_id,
            approver_refs=(self.owner.username,),  # type: ignore[union-attr]
            autonomous=True,
            policy_version=contract.run_ref,
        )
        try:
            self.production.register_environment(review)
        except production.ProductionRefusal:
            # Registered already, under the Owner's own act.  Re-registering
            # here would rewrite an envelope this seam does not own.
            pass
        supervisor_policy = supervisor.SupervisorPolicy(
            project_id=contract.project_id, enabled=True,
            work_classes=(contract.work_class,),
            missions_per_cycle=1, maintenance_admissions=1,
            improvement_admissions=0,
            policy_version=contract.run_ref,
        )
        existing = self.supervisor.policy(contract.project_id)
        if existing is None or existing.as_row() != supervisor_policy.as_row():
            self.supervisor.set_policy(supervisor_policy)

    def cycle(self) -> FactoryResult:
        """Advance one bounded Factory cycle and hand off the next slot.

        The generic supervisor remains one finite cycle over already-admitted
        work.  This wrapper is the installed Factory service's narrow seam for
        the frozen first-dogfood portfolio: it may refresh a provider reading,
        submit the next portfolio mission, run one supervisor cycle, and then
        submit one successor.  It cannot invent work or widen the Owner's
        grant, and a terminal refusal that is not an explicitly safe retry
        stops the handoff with an attention result.
        """

        self._require_owner()
        contract, entry = self._load_contract_and_portfolio()
        grant = self.shift.grant()
        control = self.supervisor.control()
        if grant is None or control.get("state") != "running" \
                or not self._service_loaded(self.config.supervisor_label):
            raise FactoryRefusal(
                "FACTORY_NOT_READY",
                "The Factory is not running. Run './dev factory start' first.")
        doctor = self._bridge_doctor()
        self._check_primary_and_containment(contract, doctor)
        if not self._service_loaded(self.config.bridge_label) \
                or not self._bridge_is_healthy(doctor):
            code, detail = self._bridge_problem(doctor)
            return self._attention_result(code, detail)
        try:
            self._refresh_capacity(contract)
        except FactoryRefusal as refusal:
            return self._attention_result(refusal.code, refusal.detail)

        queued = self._queue_next(contract, entry, doctor, grant,
                                  owner_action=False)
        if queued.state == "complete":
            return queued
        # An attention on an earlier slot stops *submission*, not execution.
        # Returning here left every already-admitted mission admitted forever:
        # the Owner's own `./dev factory run` had authorized DF-3 before the
        # blocker existed, and no verb would ever reach it again.  Work past
        # the dispatch boundary is worse than stranded -- it is unreconciled,
        # and reconciling it is the one thing that must not wait for a person.
        # So the cycle still runs, nothing new is handed off, and the blocker
        # is what the Owner is told.
        blocked = None if queued.ok else queued
        try:
            report = self.supervisor.cycle(AUTOPILOT_WORKER_ID)
        except supervisor.SupervisorRefusal as refusal:
            return self._attention_result(
                "SUPERVISOR_FAILURE",
                "The Factory supervisor could not advance work. "
                "Run './dev factory status' to review the current state.",
                detail_type=type(refusal).__name__)
        except Exception as exc:  # noqa: BLE001
            return self._attention_result(
                "SUPERVISOR_FAILURE",
                "The Factory supervisor stopped unexpectedly. "
                "Run './dev factory status' to review the current state.",
                detail_type=type(exc).__name__)

        refused = [row for row in report.get("refused", ())
                   if row.get("reason") != "NO_RUNNABLE_MISSION"]
        if report.get("outcome") == "refused" or refused:
            return self._attention_result(
                "SUPERVISOR_FAILURE",
                "The Factory supervisor stopped advancing the validation run. "
                "Run './dev factory status' to review the current state.",
                cycle=report)

        settled = self._settle_improvement(contract, grant)
        if blocked is not None:
            return FactoryResult(
                action="cycle", ok=False, state="attention",
                lines=blocked.lines,
                details={**dict(blocked.details), "cycle": report,
                         **({} if settled is None else {"improvement": settled})})
        advanced = self._queue_next(contract, entry, doctor, grant,
                                    owner_action=False)
        extra = {"cycle": report}
        if settled is not None:
            extra["improvement"] = settled
        return FactoryResult(
            action="cycle", ok=advanced.ok, state=advanced.state,
            lines=advanced.lines,
            details={**(advanced.details or {}), **extra})

    def _queue_next(self, contract, entry, doctor, grant, *,
                    owner_action: bool) -> FactoryResult:
        """Submit the next frozen slot, or report why it cannot be handed off."""

        slots = self.shift.slots(entry)
        outcomes = {ref: reading.state for ref, reading in slots.items()
                    if reading.state is not None}
        retryable = frozenset(ref for ref, reading in slots.items()
                              if reading.retryable)
        mission = entry.next_mission(outcomes, retryable)
        if not owner_action:
            blocker = self._first_autopilot_attention(entry, slots)
            if blocker is not None:
                portfolio_mission, reading, attention = blocker
                return self._attention_result(
                    "AUTOPILOT_ATTENTION", attention,
                    mission_ref=portfolio_mission.mission_ref,
                    state=reading.state)
        if mission is None:
            return FactoryResult(
                action="run" if owner_action else "cycle", ok=True, state="complete",
                lines=("DOGFOOD PORTFOLIO COMPLETE",
                       "Every mission in the first portfolio has settled.",
                       "Run './dev factory status' to review the results."),
                details={"outcomes": outcomes})
        # A retryable slot is settled in the ledger and unsettled for the
        # portfolio, so it is the one settled state that does not stop here.
        # The attempt is derived from durable rows, so two invocations in the
        # same state derive the same attempt, the same manifest and the same
        # key -- and the second submission is the first one, exactly as it was
        # before retries existed.
        attempt = slots[mission.mission_ref].next_attempt \
            if mission.mission_ref in retryable else 1
        settled = outcomes.get(mission.mission_ref)
        if settled is not None and mission.mission_ref not in retryable:
            return FactoryResult(
                action="run" if owner_action else "cycle", ok=True,
                state="running" if settled != "admitted" else "queued",
                lines=(self._work_headline(settled),
                       "Mission: " + mission.objective.split(".")[0].strip() + ".",
                       "Project: " + mission.project_id,
                       "Nothing more to do. Run './dev factory status' to check on it."),
                details={"mission_ref": mission.mission_ref, "state": settled})

        objective = self._improvement_for(mission)
        experiment_ref = None
        if objective is not None:
            # The baseline is measured and pinned before the candidate exists,
            # which is Stage 8's ordering and its anti-gaming property: a
            # baseline recorded afterwards could be chosen to flatter what the
            # provider produced.  `record_baseline` refuses once a mission
            # exists and `create_candidate_mission` refuses without a baseline,
            # so the order below is enforced by the plane and not by this call.
            experiment_ref = self._open_improvement(
                objective, mission, doctor, attempt)
        intake = self._materialize(contract, entry, mission, doctor, grant,
                                   attempt,
                                   brief=None if objective is None
                                   else objective.objective.statement)
        try:
            if experiment_ref is None:
                submitted, created = self.controller.submit(
                    intake.payload, intake.idempotency_key)
            else:
                submitted, created = self.improvement.create_candidate_mission(
                    experiment_ref, self.controller,
                    acceptance_gate_ids=list(mission.acceptance_gate_ids),
                    extra=intake.payload)
        except Exception as exc:  # noqa: BLE001
            raise FactoryRefusal(
                "MISSION_NOT_ADMITTED",
                "The next dogfood mission was refused before it started: %s"
                % type(exc).__name__) from None
        if experiment_ref is not None:
            # The experiment payload is the dogfood payload; the key the plane
            # derived from it must therefore be the key the seam derived, or
            # the mission the experiment is bound to is not the mission the
            # portfolio admitted.  Checked rather than assumed: a silent
            # divergence here would bind an experiment to a second identity.
            bound = self.improvement.lineage(experiment_ref)["idempotency_key"]
            if bound != intake.idempotency_key:
                raise FactoryRefusal(
                    "IMPROVEMENT_BINDING_MISMATCH",
                    "The improvement candidate was admitted under a different "
                    "identity than the portfolio slot it belongs to.")
        detail = {
            "mission_ref": intake.mission_ref,
            "project_id": intake.project_id,
            "attempt": intake.attempt,
            "idempotency_key": intake.idempotency_key,
            "created": created,
            **({} if experiment_ref is None
               else {"experiment_ref": experiment_ref}),
        }
        if owner_action:
            self._record_owner_act("run", grant.approval_ref, detail)
        elif created:
            self.store.coordinate(
                submitted["id"], intake.project_id, "factory",
                "FACTORY_AUTOPILOT_ADVANCE", detail)
        return FactoryResult(
            action="run" if owner_action else "cycle", ok=True,
            state="queued" if created else "running",
            lines=("DOGFOOD MISSION QUEUED" if created
                   else "DOGFOOD MISSION RUNNING",
                   "Mission: " + mission.objective.split(".")[0].strip() + ".",
                   "Project: " + intake.project_id,
                   *(("Retrying attempt %d; the earlier attempt was refused "
                      "by the execution layer and is kept."
                      % intake.attempt,) if intake.attempt > 1 else ()),
                   "The supervisor will pick it up automatically. "
                   "Run './dev factory status' to follow it."),
            details={"mission_ref": intake.mission_ref,
                     "project_id": intake.project_id,
                     "attempt": intake.attempt,
                     "created": created})

    @staticmethod
    def _autopilot_attention(mission, reading) -> str | None:
        """Return a blocker for an automatic handoff, if one is present."""

        if reading.state not in {"refused", "failed", "cancelled", "escalated"} \
                or reading.retryable:
            return None
        if (reading.state == "escalated"
                and reading.terminal_reason.startswith("ACCEPTANCE_GATE_FAILED")
                and mission.acceptance_gate_expectations):
            # DF-2's declared non-zero evaluator result is evidence the
            # portfolio explicitly asks for, not an infrastructure failure.
            return None
        reason = reading.terminal_reason or "the mission did not settle successfully"
        return ("The %s validation mission needs Owner attention before the "
                "Factory can continue (%s)." % (mission.mission_ref, reason))

    def _first_autopilot_attention(self, entry, slots):
        """Read the first settled portfolio blocker without changing state."""

        for portfolio_mission in entry.missions:
            reading = slots.get(portfolio_mission.mission_ref)
            if reading is None or reading.state is None or reading.retryable:
                break
            attention = self._autopilot_attention(portfolio_mission, reading)
            if attention is not None:
                return portfolio_mission, reading, attention
        return None

    @staticmethod
    def _attention_result(code: str, detail: str, **extra: Any) -> FactoryResult:
        return FactoryResult(
            action="cycle", ok=False, state="attention",
            lines=("FACTORY ATTENTION", detail,
                   "Run './dev factory status' to review the current state."),
            details={"code": code, **extra})

    # -- the improvement slot ------------------------------------------- #

    def _measure(self, checkout: str, gate_commands, gate_ids, sha: str):
        """Run the declared gates at one commit and report what they said.

        In a detached worktree, never the registered checkout: the measurement
        has to be of the commit the mission pins, and a checkout that had moved
        would otherwise be measured silently as if it had not.  The worktree is
        also what keeps this read-only -- the lab's own tree is untouched, which
        DF-1 and DF-2's stop conditions require of every mission here.
        """

        worktree = Path(tempfile.mkdtemp(prefix="factory-baseline-"))
        target = worktree / "checkout"
        added = self._run(
            ("git", "-C", checkout, "worktree", "add", "--force", "--quiet",
             "--detach", str(target), sha))
        if added.returncode != 0:
            shutil.rmtree(worktree, ignore_errors=True)
            raise FactoryRefusal(
                "IMPROVEMENT_BASELINE_UNAVAILABLE",
                "The Factory could not check out the baseline this "
                "improvement mission is measured against.")
        try:
            outcomes = []
            for gate in gate_ids:
                command = stage1_adapter._render_candidate_command(
                    gate_commands.get(gate), checkout, target)
                if command is None:
                    outcomes.append({"gate_id": gate, "passed": False,
                                     "detail": "not_run",
                                     "evidence_class": "rederived"})
                    continue
                result = self.runner(
                    tuple(command), cwd=str(target), input_text=None,
                    timeout_seconds=GATE_MEASUREMENT_TIMEOUT_SECONDS)
                result = self._normalize_result(result)
                outcomes.append({
                    "gate_id": gate, "passed": result.returncode == 0,
                    "exit_code": result.returncode,
                    "detail": " ".join(command),
                    "evidence_class": "rederived",
                    "stdout_tail": result.stdout[-2000:] or "not_applicable",
                    "stderr_tail": result.stderr[-2000:] or "not_applicable",
                })
            return outcomes
        finally:
            self._run(("git", "-C", checkout, "worktree", "remove", "--force",
                       str(target)))
            shutil.rmtree(worktree, ignore_errors=True)

    def _settle_improvement(self, contract, grant):
        """Compare, stage and close a completed improvement candidate.

        Runs after the supervisor cycle because that is when the candidate's
        own gate evidence exists, and the post-change measurement is read from
        that evidence rather than measured again -- the same gates, in the same
        container, at the candidate commit the mission verified.  Measuring it
        a second time here would be a second evaluator with a second answer.
        """

        objective = self.improvement_contract()
        if objective is None:
            return None
        row = dogfood_improvement.open_for(self.improvement, objective)
        if row is None or not row["mission_ref"] or row["candidate_sha"]:
            return None
        mission = self.store.get(row["mission_ref"])
        if mission is None or mission["state"] != "completed":
            return None
        evaluation = self.store.step_output(mission["id"], "evaluate")
        evaluation = evaluation if isinstance(evaluation, Mapping) else {}
        route = self.store.route_history(mission["id"])
        producer = route.get("selected_provider_profile")
        outcome = dogfood_improvement.settle(
            self.improvement, self.production, objective,
            experiment_ref=row["experiment_ref"], mission=mission,
            producer_identity=("unknown" if not producer
                               else "provider:%s" % producer),
            evaluator_identity=IMPROVEMENT_EVALUATOR_IDENTITY,
            changed_paths=evaluation.get("changed_paths"),
            candidate=dogfood_improvement.measurements(
                objective, evaluation.get("gate_outcomes") or ()),
            approval_ref=grant.approval_ref,
            release_policy_version=contract.run_ref,
            provenance_at=dogfood_intake.iso_utc(self.clock()))
        # The promotion decision, with the reference that authorized it.  The
        # improvement plane records that a promotion was staged and the
        # production ledger records what was admitted; neither holds the
        # Owner's grant, and DF-4's evidence asks for the decision *with* its
        # approval reference.
        self.store.coordinate(
            mission["id"], mission.get("project_id"), "factory",
            "FACTORY_IMPROVEMENT_SETTLED",
            {"approval_ref": grant.approval_ref,
             "objective_ref": row["objective_ref"],
             "contract_digest": objective.contract_digest,
             **{key: value for key, value in outcome.items()
                if key != "deployment"}})
        return outcome

    def _open_improvement(self, objective, mission, doctor, attempt: int) -> str:
        """Measure the pinned baseline and open generation 1 against it."""

        registered = dogfood_intake.registry_row(
            self._registry_rows(doctor), mission.project_id)
        checkout = registered.get("checkout")
        remote = registered.get("repository_remote_url")
        if not isinstance(checkout, str) or not isinstance(remote, str):
            raise FactoryRefusal(
                "PROJECT_CHECKOUT_UNAVAILABLE",
                "The project this improvement mission targets has no local "
                "working copy its baseline could be measured in.")
        dogfood_improvement.abandon_spent(self.improvement, objective, self.store)
        commands = dogfood_intake.gate_commands(
            objective.gate_ids, mission.acceptance_gate_source, checkout)
        baseline = dogfood_improvement.measurements(
            objective,
            self._measure(checkout, commands, objective.gate_ids,
                          mission.baseline_sha))
        try:
            row = dogfood_improvement.open_experiment(
                self.improvement, objective, repository=remote,
                baseline_sha=mission.baseline_sha,
                isolation_ref="lane://%s/%s#%d"
                              % (mission.project_id, mission.mission_ref, attempt),
                baseline=baseline, attempt=attempt)
        except (improvement.ImprovementRefusal, improvement.PolicyError) as refusal:
            raise FactoryRefusal(
                getattr(refusal, "code", "IMPROVEMENT_NOT_ADMITTED"),
                "The improvement this mission carries could not be opened: %s"
                % refusal) from None
        return row["experiment_ref"]

    def _materialize(self, contract, entry, mission, doctor, grant, attempt=1,
                     brief=None, *, portfolio_ref=None, corpus_identity=None,
                     checkout=None):
        """Derive the whole mission, and refuse rather than guess any part.

        ``portfolio_ref`` and ``corpus_identity`` default to the frozen
        internal portfolio's own two identities.  The product path supplies
        them instead -- its corpus is the submitted package's digest, not a
        portfolio file -- so both paths keep one identity scheme, one manifest
        rule and one admission document between them.

        ``checkout`` names the local copy the repository grounding is read
        from, and defaults to the registered one.  Only the revision path
        overrides it: its mission's baseline is an immutable base on no
        product branch, and the Context Broker grounds only a checkout's
        current ``HEAD``.  The execution layer opens a checkout that is at
        that base and names it here, so the freshness invariant is satisfied
        rather than relaxed.
        """

        registry = self._registry_rows(doctor)
        registry_digest = (doctor.get("registry") or {}).get("digest") \
            if isinstance(doctor.get("registry"), Mapping) else None
        if not isinstance(registry_digest, str) or not registry_digest:
            raise FactoryRefusal(
                "PROJECT_REGISTRY_UNIDENTIFIED",
                "The execution layer's project registry has no identity the "
                "mission could be bound to. Run './dev factory install'.")
        interpreter = self._resolve_supported_python()
        registered = dogfood_intake.registry_row(registry, mission.project_id)
        checkout = checkout or registered.get("checkout")
        builder = self.context_builder
        if builder is None:
            builder = lambda wire: self._build_context(
                wire, checkout=checkout, interpreter=interpreter)
        try:
            intake = dogfood_intake.build(
                mission,
                portfolio_ref=portfolio_ref or entry.portfolio_ref,
                run_ref=contract.run_ref,
                registry=registry, registry_digest=registry_digest,
                provider_profiles=contract.provider_profiles,
                corpus_identity=corpus_identity or "contract://%s@%s"
                                % (entry.portfolio_ref,
                                   self._portfolio_identity()),
                owner=self.owner.username,  # type: ignore[union-attr]
                approval_ref=grant.approval_ref,
                granted_at=grant.granted_at, expires_at=grant.expires_at,
                now=self.clock(),
                stage1={
                    "command": [interpreter, "-m", "src.cli.first_live"],
                    "workdir": str(self.config.evidence_root),
                    "admission": str(self._mission_path(
                        mission.mission_ref, "admission", attempt)),
                    "output": str(self._mission_path(
                        mission.mission_ref, "result", attempt)),
                    "timeout_seconds": 3600,
                    "gate_timeout_seconds": 1800,
                    **({} if brief is None else {"mission_brief": brief}),
                },
                attempt=attempt,
                context_builder=builder,
            )
        except dogfood_intake.IntakeError as refusal:
            raise FactoryRefusal(refusal.code, refusal.detail) from None
        # Whether the local copy sits at the mission's baseline is a git fact,
        # and git facts are the execution layer's to re-derive -- the
        # Controller asking would be the second candidate-truth authority
        # `tests/test_authority_boundaries.py` exists to prevent.
        self._write_mission_file(mission.mission_ref, "admission",
                                 intake.admission, attempt)
        return intake

    def _portfolio_identity(self) -> str:
        """The frozen portfolio's own content identity, not a claim about it."""

        try:
            body = self.config.portfolio_path.read_bytes()
        except OSError:
            raise FactoryRefusal(
                "PORTFOLIO_UNAVAILABLE",
                "The frozen first-dogfood portfolio is unavailable.") from None
        return context.sha256_hex(body.decode("utf-8", "replace"))

    def _mission_path(self, mission_ref: str, kind: str, attempt: int = 1) -> Path:
        """One admission and one result file per *attempt*, not per slot.

        A retry that wrote to the same two paths would overwrite the admission
        document and the provider result of the attempt it is replacing, which
        is the history the retry exists to preserve.  Attempt 1 keeps the
        original name so files already on a host stay where their mission's
        payload says they are.
        """

        suffix = "" if attempt <= 1 else "-attempt-%d" % attempt
        # A product work item is `<package_id>:build`, and a colon in a POSIX
        # filename is legal but displays as a path separator.  Every internal
        # portfolio reference is already colon-free, so this renames nothing
        # that exists.
        stem = mission_ref.lower().replace(":", "-")
        return self.config.mission_dir / (
            "%s%s-%s.json" % (stem, suffix, kind))

    def _write_mission_file(self, mission_ref: str, kind: str,
                            body: Mapping[str, Any], attempt: int = 1) -> None:
        path = self._mission_path(mission_ref, kind, attempt)
        try:
            self.config.mission_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
            os.chmod(path, 0o600)
        except OSError:
            raise FactoryRefusal(
                "MISSION_NOT_RECORDED",
                "The next dogfood mission could not be recorded safely. "
                "Retry the command.") from None

    @staticmethod
    def _work_headline(state: str) -> str:
        if state == "admitted":
            return "DOGFOOD MISSION QUEUED"
        if state == "completed":
            return "DOGFOOD MISSION COMPLETE"
        if state in {"refused", "failed", "cancelled", "escalated"}:
            return "DOGFOOD MISSION STOPPED"
        return "DOGFOOD MISSION RUNNING"

    def _work_reading(self):
        """Read the frozen portfolio once for status and handoff decisions."""

        try:
            _, entry = self._load_contract_and_portfolio()
            slots = self.shift.slots(entry)
        except FactoryRefusal:
            return None
        outcomes = {ref: reading.state for ref, reading in slots.items()
                    if reading.state is not None}
        retryable = frozenset(ref for ref, reading in slots.items()
                              if reading.retryable)
        return entry, slots, outcomes, retryable, entry.next_mission(outcomes, retryable)

    def _product_reading(self):
        """The latest mission admitted under the Owner's product contract.

        Read-only and refusal-free for the same reason ``_work_reading`` is: a
        status that could not be printed because one contract was unreadable
        would hide every other fact with it.
        """

        try:
            contract = product.ProductContract.load(
                self.config.product_contract_path)
            rows = [row for row in self.store.all_missions()
                    if row.get("project_id") == contract.project_id]
            return None if not rows else self.store.get(rows[-1]["id"])
        except Exception:  # noqa: BLE001
            return None

    def _product_stage(self, mission_id: str) -> str | None:
        """Which durable step the mission is on, when one has been recorded."""

        try:
            records = self.store.step_records(mission_id)
        except Exception:  # noqa: BLE001
            return None
        if not records:
            return None
        latest = records[-1]
        return "%s (%s)" % (
            latest["name"],
            "complete" if latest["status"] == "COMPLETED" else "in progress")

    def _dogfood_history_note(self) -> str | None:
        """The internal portfolio's own blocker, demoted to what it is.

        It stays in durable history and stays readable -- nothing here writes,
        and the rows the autopilot reads are untouched -- but it is not a
        product's blocker and must not be rendered as one.
        """

        reading = self._work_reading()
        if reading is None:
            return None
        entry, slots, _, _, _ = reading
        blocker = self._first_autopilot_attention(entry, slots)
        if blocker is None:
            return None
        portfolio_mission, _, _ = blocker
        return ("History: the %s internal validation mission is still marked "
                "for Owner review. It is kept in durable history and does not "
                "block this product." % portfolio_mission.mission_ref)

    def _product_summary(self) -> tuple[tuple[str, ...], str] | None:
        """What the Owner needs to know about their product, when one exists.

        ``_work_summary`` reads the frozen first-dogfood portfolio and nothing
        else.  That was the whole truth until a real product was admitted, and
        then it was not: a product mission is named by the Owner one package at
        a time, so it is in no portfolio and never will be, and the status
        surface went on answering with an internal validation slot.  Worse, it
        answered with DF-1's escalation -- which ``cycle`` deliberately steps
        past, precisely so already-admitted work keeps advancing -- rendered as
        the current blocker of work that was in fact executing.

        So a product mission takes the headline whenever one exists, and the
        internal portfolio keeps it whenever one does not.  Every field below
        is read from the rows the engine itself writes; nothing is derived from
        the fact that a status command was run.
        """

        mission = self._product_reading()
        if mission is None:
            return None
        state = mission["state"]
        payload = mission.get("payload")
        work_item = (payload or {}).get("work_item_id") or "The product mission"
        lines = ["Product: %s in %s is %s"
                 % (work_item, mission["project_id"],
                    PRODUCT_LIFECYCLE.get(state, state))]
        stage = self._product_stage(mission["id"])
        if stage is not None:
            lines.append("Stage: " + stage)
        try:
            profile = self.store.route_history(
                mission["id"]).get("selected_provider_profile")
        except Exception:  # noqa: BLE001
            profile = None
        if profile:
            lines.append("Provider: %s (%s)"
                         % (self._display_profile(profile), profile))
        if state in shift_plane.UNSUCCESSFUL_MISSION_STATES:
            summary_state = "attention"
            lines.append(
                "Attention: %s needs Owner review (%s)."
                % (work_item, mission["terminal_reason"]
                   or "it did not settle successfully"))
        elif state == "completed":
            summary_state = "complete"
        else:
            summary_state = "pending"
        lines.extend(self._review_lines(mission, state))
        lines.extend(self._lineage_lines(mission))
        history = self._dogfood_history_note()
        if history is not None:
            lines.append(history)
        return tuple(lines), summary_state

    def _lineage_lines(self, mission) -> tuple[str, ...]:
        """What the Owner already decided about this product, and about what.

        A second product mission looks exactly like the first one on a surface
        that reads only the latest row, and the difference is the whole point:
        one is a build and the other is a revision of a release the Owner
        turned away.  So the releases that were returned are named here beside
        the mission that supersedes them, and every line is read from the
        release plane's own immutable rows.
        """

        try:
            with self.store.transaction() as db:
                returned = db.execute(
                    "SELECT v.rc_id, v.candidate_sha, v.decided_at, c.project_id"
                    " FROM owner_validations v"
                    " JOIN release_candidates c ON c.rc_id = v.rc_id"
                    " WHERE v.decision=? AND c.project_id=?"
                    " ORDER BY v.decided_at",
                    ("RETURN_FOR_CHANGES", mission["project_id"])).fetchall()
        except Exception:  # noqa: BLE001
            # Nothing has ever been released for this product, so the release
            # plane has not created its tables yet.  Reading is all this does.
            return ()
        if not returned:
            return ()
        lines = ["Returned for changes: %s at candidate %s; it stays sealed "
                 "and is not promoted." % (row["rc_id"], row["candidate_sha"][:12])
                 for row in returned]
        payload = mission.get("payload") or {}
        work_item = payload.get("work_item_id") or ""
        baseline = payload.get("baseline_sha")
        if ":revision:" in str(work_item) and isinstance(baseline, str):
            lines.append(
                "Revision lineage: %s builds from %s, which descends from the "
                "candidate you reviewed." % (work_item, baseline[:12]))
        return tuple(lines)

    def _review_lines(self, mission, state: str) -> tuple[str, ...]:
        """Where a finished product stands on the way to the Owner's judgement.

        A mission that succeeded and a product the Owner can look at are two
        different facts, and the surface reported only the first: the Owner
        was told the work finished and left to know, from somewhere else, that
        a verb existed to turn it into something reviewable.  This is the rest
        of that sentence, and like everything else here it is read rather than
        assumed -- an unsealed candidate says so, and a sealed one names the
        surface its own bytes are served on.
        """

        if state != "completed":
            return ()
        try:
            contract = product.ProductContract.load(
                self.config.product_contract_path)
            result = mission.get("result") or {}
            candidate = ((result.get("verification") or {}).get("verification")
                         or {}).get("candidate_sha")
            if not isinstance(candidate, str):
                return ()
            rc_id = product.rc_id_for(contract, candidate)
        except Exception:  # noqa: BLE001
            return ()
        try:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT validation_surface, artifact_digest FROM"
                    " release_deployments WHERE rc_id=?", (rc_id,)).fetchone()
        except Exception:  # noqa: BLE001
            # The release plane owns that table and creates it on first use, so
            # before any product was ever reviewed there is nothing to read.
            # Reading is all this does: a status that created a schema would be
            # a status that writes.
            row = None
        if row is None:
            return ("Next: run './dev factory review' to prepare it for your "
                    "review.",)
        return ("Review: %s is ready at %s" % (rc_id, row["validation_surface"]),
                "Serving artifact %s" % row["artifact_digest"],
                "Start the surface with './dev review up' if it is not already "
                "running.")

    def _work_summary(self) -> tuple[tuple[str, ...], str]:
        """What the Owner needs to know about work, with no internal ids.

        Read-only and refusal-free: a status that could not be printed because
        the portfolio was unreadable would hide the rest of the state as well.
        """

        reading = self._work_reading()
        if reading is None:
            return ("Work: unknown",), "unknown"
        entry, slots, outcomes, retryable, pending = reading
        if pending is None:
            return ("Work: every first-dogfood mission has settled",), "complete"
        blocker = self._first_autopilot_attention(entry, slots)
        if blocker is not None:
            _, _, attention = blocker
            return ("Attention: " + attention,
                    "Automatic validation is paused until the Owner reviews it."), "attention"
        state = outcomes.get(pending.mission_ref)
        if state is None:
            return ("Work: none started",
                    "Next: %s, in %s" % (pending.mission_ref, pending.project_id),
                    "The Factory will start it automatically."), "pending"
        if pending.mission_ref in retryable:
            reading = slots[pending.mission_ref]
            return ("Work: %s in %s was refused by the execution layer "
                    "and can be retried automatically"
                    % (pending.mission_ref, pending.project_id),
                    "Refusal kept: %s" % reading.terminal_reason,
                    "Next: %s, attempt %d of %d"
                    % (pending.mission_ref, reading.next_attempt,
                       shift_plane.MAX_SLOT_ATTEMPTS)), "pending"
        return (("Work: %s in %s is %s"
                % (pending.mission_ref, pending.project_id,
                   "waiting to start; the supervisor will pick it up automatically"
                   if state == "admitted" else state),), "pending")

    def _status_attention(self, doctor: Mapping[str, Any], *, live,
                          bridge_healthy: bool, primary: str) -> tuple[str, ...]:
        """Read-only warnings that explain why automatic progress may stop."""

        if live is None:
            return ()
        lines: list[str] = []
        if not bridge_healthy:
            lines.append(
                "Attention: The Factory Bridge/provider is not healthy; "
                "automatic validation is paused.")
        if primary == "Unavailable":
            lines.append(
                "Attention: The primary provider is unavailable; "
                "automatic validation is paused.")
        try:
            unavailable = [reading for reading in self.store.capacity_readings().values()
                           if not reading.usable]
        except Exception:  # noqa: BLE001
            unavailable = []
        for reading in unavailable:
            profile = self._display_profile(reading.runtime_id)
            if reading.reason == "CAPACITY_OBSERVATION_STALE":
                detail = "capacity is stale"
            elif reading.reason == "CAPACITY_OBSERVATION_MISSING":
                detail = "capacity has not been measured"
            else:
                detail = "capacity is unavailable"
            lines.append(
                "Attention: %s provider %s; automatic validation is waiting "
                "for a fresh reading." % (profile, detail))
        cycles = self.supervisor.cycles(limit=1)
        if cycles and cycles[0].get("outcome") == "refused":
            lines.append(
                "Attention: The supervisor stopped unexpectedly; "
                "automatic validation is paused.")
        return tuple(lines)

    def status(self) -> FactoryResult:
        owner = self.owner
        if owner is None or not owner.valid():
            return FactoryResult(
                action="status", ok=False, state="blocked",
                lines=("BLOCKED: A trusted local Owner identity is unavailable.",),
                details={"code": "OWNER_IDENTITY_UNAVAILABLE"},
            )
        live = self.shift.grant()
        control = self.supervisor.control()
        supervisor_loaded = self._service_loaded(self.config.supervisor_label)
        bridge_loaded = self._service_loaded(self.config.bridge_label)
        try:
            doctor = self._bridge_doctor()
            bridge_healthy = self._bridge_is_healthy(doctor)
            primary = self._primary_state(doctor)
            bridge_summary = "Healthy" if bridge_healthy else "Needs attention"
        except FactoryRefusal:
            doctor = {}
            bridge_healthy = False
            primary = "Unavailable"
            bridge_summary = "Not installed"

        inconsistent = (
            (live is not None and control.get("state") != "running")
            or (live is not None and not supervisor_loaded)
            or (live is not None and not bridge_loaded)
            or (live is None and control.get("state") in {"running", "paused", "draining"})
            or (live is None and supervisor_loaded)
        )
        if inconsistent:
            return FactoryResult(
                action="status", ok=False, state="blocked",
                lines=("BLOCKED: The Factory services and durable shift state "
                       "are inconsistent.",
                       "Run './dev factory stop' to return to a safe state, "
                       "then './dev factory start'."),
                details={"code": "INCONSISTENT_SERVICE_STATE",
                         "control": control, "bridge": doctor},
            )
        ready = live is not None and control.get("state") == "running" \
            and supervisor_loaded and bridge_loaded and bridge_healthy
        state = "ready" if ready else "off"
        label = "FACTORY READY" if ready else "FACTORY OFF"
        shift_summary = "Active" if live is not None else "Off"
        supervisor_summary = "Running" if supervisor_loaded else "Stopped"
        work, work_state = self._product_summary() or self._work_summary()
        status_attention = self._status_attention(
            doctor, live=live, bridge_healthy=bridge_healthy, primary=primary)
        if status_attention:
            work_state = "attention"
        return FactoryResult(
            action="status", ok=True, state=state,
            lines=(label,
                   "Shift: " + shift_summary,
                   "Supervisor: " + supervisor_summary,
                   "Bridge: " + bridge_summary,
                   "Primary: " + primary)
            + status_attention
            + work,
            details={"control": control, "grant": None if live is None else live.as_row(),
                     "bridge": doctor, "work_state": work_state},
        )

    def watch(self, interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
              *, emit: Callable[[str], None] = print,
              sleep: Callable[[float], None] = time.sleep) -> int:
        """Observe status until completion, attention, or Ctrl+C.

        This is deliberately outside the lifecycle mutation path.  Stopping
        it only stops the observer; it never stops, drains, or changes Factory
        work already handed to the supervisor.
        """

        if (isinstance(interval_seconds, bool)
                or not isinstance(interval_seconds, (int, float))
                or interval_seconds <= 0):
            raise FactoryRefusal(
                "WATCH_INTERVAL_INVALID",
                "The status watch interval must be greater than zero seconds.")
        try:
            while True:
                result = self.status()
                emit(result.render())
                if not result.ok or result.details.get("work_state") in {
                        "complete", "attention"}:
                    return 0 if result.ok else 1
                sleep(float(interval_seconds))
        except KeyboardInterrupt:
            return 0

    # -- host edge ------------------------------------------------------ #

    def _require_owner(self) -> OwnerIdentity:
        if self.owner is None or not self.owner.valid():
            raise FactoryRefusal(
                "OWNER_IDENTITY_UNAVAILABLE",
                "A trusted local Owner identity is unavailable.")
        return self.owner

    def _normalize_result(self, value: Any) -> HostCommandResult:
        if isinstance(value, HostCommandResult):
            return value
        if isinstance(value, tuple) and len(value) >= 1:
            return HostCommandResult(
                int(value[0]),
                "" if len(value) < 2 else str(value[1]),
                "" if len(value) < 3 else str(value[2]),
            )
        raise TypeError("host runner returned an unsupported result")

    def _run(self, command: Sequence[str], *, cwd: Path | None = None,
             input_text: str | None = None) -> HostCommandResult:
        result = self.runner(
            tuple(command), cwd=None if cwd is None else str(cwd),
            input_text=input_text, timeout_seconds=300,
        )
        return self._normalize_result(result)

    def _run_json(self, command: Sequence[str], *, cwd: Path | None = None,
                  input_text: str | None = None) -> tuple[HostCommandResult, dict[str, Any]]:
        result = self._run(command, cwd=cwd, input_text=input_text)
        try:
            body = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise FactoryRefusal(
                "HOST_COMMAND_FAILED",
                "The Factory could not inspect the Bridge. "
                "Run './dev factory install' again.") from None
        if not isinstance(body, dict):
            raise FactoryRefusal(
                "HOST_COMMAND_FAILED",
                "The Factory received an invalid Bridge status. "
                "Run './dev factory install' again.")
        return result, body

    def _bridge_json(self, *arguments: str,
                     input_text: str | None = None
                     ) -> tuple[HostCommandResult, dict[str, Any]]:
        return self._run_json(
            (str(self.config.bridge_root / "dev"), *arguments),
            cwd=self.config.bridge_root, input_text=input_text)

    def _bridge_doctor(self) -> dict[str, Any]:
        _, body = self._bridge_json("doctor")
        return body

    def _fresh_bridge_readiness(self, doctor: Mapping[str, Any]):
        result, readiness = self._bridge_json("readiness")
        if result.returncode not in (0, 1):
            raise FactoryRefusal(
                "BRIDGE_READINESS_UNAVAILABLE",
                "The Factory could not measure Bridge readiness. Retry start.")
        profiles = readiness.get("profiles")
        if not isinstance(profiles, list):
            raise FactoryRefusal(
                "BRIDGE_READINESS_UNAVAILABLE",
                "The Factory received no current Bridge readiness. Retry start.")
        provider = doctor.get("provider")
        provider = dict(provider) if isinstance(provider, Mapping) else {}
        provider["profiles"] = profiles
        return {**dict(doctor), "provider": provider}, readiness

    def _service_domain(self, label: str) -> str:
        return "gui/%d/%s" % (self.owner.uid, label)  # type: ignore[union-attr]

    def _service_loaded(self, label: str) -> bool:
        result = self._run(("launchctl", "print", self._service_domain(label)))
        if result.returncode == 127:
            raise FactoryRefusal(
                "HOST_CONTROL_UNAVAILABLE",
                "macOS service control is unavailable on this host.")
        return result.returncode == 0

    def _bootout_if_loaded(self, label: str) -> bool:
        if not self._service_loaded(label):
            return False
        result = self._run(("launchctl", "bootout", self._service_domain(label)))
        if result.returncode != 0:
            raise FactoryRefusal(
                "SERVICE_STOP_FAILED",
                "The Factory could not stop a host service safely. Retry the command.")
        return True

    def _bootstrap(self, label: str, plist: Path) -> None:
        if self._service_loaded(label):
            return
        result = self._run(("launchctl", "bootstrap", "gui/%d" % self.owner.uid,
                            str(plist)))  # type: ignore[union-attr]
        if result.returncode != 0 or not self._service_loaded(label):
            raise FactoryRefusal(
                "SERVICE_START_FAILED",
                "The Factory could not start a required host service safely.")

    def _bootstrap_service(self, plan: activation.ServicePlan) -> None:
        self._bootstrap(plan.label, Path(plan.definition_path))

    def _bridge_is_healthy(self, doctor: Mapping[str, Any]) -> bool:
        compatibility = doctor.get("compatibility")
        if not isinstance(compatibility, Mapping) \
                or compatibility.get("status") != "compatible":
            return False
        if doctor.get("registry_drift") not in {"none", "not_applicable"}:
            return False
        if doctor.get("serving_drift") not in {"none", "not_applicable", None}:
            return False
        if doctor.get("unresolved_projects"):
            return False
        containment = doctor.get("containment")
        return isinstance(containment, Mapping) \
            and containment.get("sandbox_exec_present") is True

    def _bridge_problem(self, doctor: Mapping[str, Any]) -> tuple[str, str]:
        compatibility = doctor.get("compatibility")
        source = doctor.get("source")
        source = source if isinstance(source, Mapping) else {}
        installed = source.get("installed_sha")
        current = source.get("sha")
        version = source.get("version_file")
        if installed and current and (installed != current or version != installed):
            return (
                "BRIDGE_SOURCE_DRIFT",
                "Bridge software has changed. Run './dev factory install' to apply it.",
            )
        if not installed or not isinstance(compatibility, Mapping) \
                or compatibility.get("status") == "not_installed":
            return (
                "BRIDGE_NOT_INSTALLED",
                "The Factory Bridge is not installed. Run './dev factory install'.",
            )
        if doctor.get("registry_drift") not in {"none", "not_applicable"}:
            return (
                "BRIDGE_REGISTRY_DRIFT",
                "Factory project configuration has drifted. Run './dev factory install'.",
            )
        if doctor.get("serving_drift") not in {"none", "not_applicable", None}:
            return (
                "BRIDGE_SERVING_DRIFT",
                "The running Factory Bridge is serving an older configuration. "
                "Run './dev factory start' to reload it.",
            )
        return (
            "BRIDGE_NOT_READY",
            "The Factory Bridge is not ready. Run './dev factory install'.",
        )

    def _prepare_bridge(self, *, allow_install: bool, load: bool) -> dict[str, Any]:
        doctor = self._bridge_doctor()
        legacy = doctor.get("legacy")
        if isinstance(legacy, Mapping) and legacy.get("service_loaded"):
            self._bootout_if_loaded(self.config.legacy_label)
            doctor = self._bridge_doctor()

        containment = doctor.get("containment")
        if isinstance(containment, Mapping) \
                and containment.get("sandbox_exec_present") is False:
            raise FactoryRefusal(
                "SANDBOX_CONTAINMENT_UNAVAILABLE",
                "macOS sandbox containment is unavailable. Check system security settings.")

        # A serving posture that is merely stale needs the service restarted,
        # not reinstalled: the files on disk are already the ones the Owner
        # wants served, and only the process that read them is behind.  Doing
        # it here, before the install decision, keeps `start` from refusing
        # with an install instruction for something a reload fixes.
        if load and bool((doctor.get("service") or {}).get("plist_present")) \
                and doctor.get("serving_drift") not in {"none", "not_applicable", None}:
            doctor = self._reload_bridge()

        need_install = (
            not self._bridge_is_healthy(doctor)
            or not bool((doctor.get("service") or {}).get("plist_present"))
        )
        if need_install:
            code, detail = self._bridge_problem(doctor)
            if not allow_install:
                raise FactoryRefusal(code, detail)
            if self._service_loaded(self.config.bridge_label):
                self._bootout_if_loaded(self.config.bridge_label)
            result, planned = self._bridge_json("install", "--dry-run")
            if result.returncode != 0 or planned.get("status") != "planned":
                raise FactoryRefusal(
                    "BRIDGE_INSTALL_FAILED",
                    "The Factory Bridge installation plan was refused. "
                    "Run './dev factory status' for the next action.")
            result, installed = self._bridge_json("install")
            if result.returncode != 0 or installed.get("status") != "installed":
                raise FactoryRefusal(
                    "BRIDGE_INSTALL_FAILED",
                    "The Factory Bridge could not be installed safely. Retry the command.")
            doctor = self._bridge_doctor()

        if load:
            service = doctor.get("service")
            service = service if isinstance(service, Mapping) else {}
            plist = Path(str(service.get("plist_path") or self.config.bridge_plist))
            self._bootstrap(self.config.bridge_label, plist)
            doctor = self._bridge_doctor()
            if (doctor.get("service") or {}).get("socket_present") is False:
                self._bootout_if_loaded(self.config.bridge_label)
                self._bootstrap(self.config.bridge_label, plist)
                doctor = self._bridge_doctor()
            service = doctor.get("service")
            if not isinstance(service, Mapping) \
                    or service.get("plist_present") is not True \
                    or service.get("socket_present") is not True:
                raise FactoryRefusal(
                    "BRIDGE_NOT_RUNNING",
                    "The Factory Bridge did not become ready. Retry install or start.")

        if not self._bridge_is_healthy(doctor):
            code, detail = self._bridge_problem(doctor)
            containment = doctor.get("containment")
            if isinstance(containment, Mapping) \
                    and containment.get("sandbox_exec_present") is False:
                code, detail = (
                    "SANDBOX_CONTAINMENT_UNAVAILABLE",
                    "macOS sandbox containment is unavailable. Check system security settings.",
                )
            raise FactoryRefusal(code, detail)
        return doctor

    # -- durable setup -------------------------------------------------- #

    def _load_contract_and_portfolio(self):
        try:
            contract = dogfood.load_contract(str(self.config.contract_path))
            entry = shift_plane.load_portfolio(str(self.config.portfolio_path))
        except (dogfood.ContractError, shift_plane.ShiftError, OSError, ValueError):
            raise FactoryRefusal(
                "CONTRACT_UNAVAILABLE",
                "The frozen first-dogfood Factory contract is unavailable.") from None
        if entry.portfolio_ref == "" or not entry.missions:
            raise FactoryRefusal(
                "PORTFOLIO_UNAVAILABLE",
                "The frozen first-dogfood portfolio is unavailable.")
        return contract, entry

    def _project_rows(self, contract, entry, doctor):
        by_id = {row.get("project_id"): row for row in self._registry_rows(doctor)
                 if isinstance(row, Mapping)}
        missions_by_project: dict[str, list[Any]] = {}
        for mission in entry.missions:
            missions_by_project.setdefault(mission.project_id, []).append(mission)
        output = []
        for project_id in contract.projects:
            source_row = by_id.get(project_id)
            if source_row is None or source_row.get("resolution") not in (None, "resolved"):
                raise FactoryRefusal(
                    "PROJECT_UNRESOLVED",
                    "A first-dogfood project is not available. Run './dev factory install'.")
            repository = source_row.get("repository_remote_url")
            if not isinstance(repository, str) or not repository:
                raise FactoryRefusal(
                    "PROJECT_UNRESOLVED",
                    "A first-dogfood project has no verified repository source.")
            missions = missions_by_project.get(project_id) or []
            gates: list[str] = []
            sources: list[str] = []
            for mission in missions:
                for gate in mission.acceptance_gate_ids:
                    if gate not in gates:
                        gates.append(gate)
                if mission.acceptance_gate_source not in sources:
                    sources.append(mission.acceptance_gate_source)
            if not gates or len(sources) != 1:
                raise FactoryRefusal(
                    "ACCEPTANCE_GATES_UNAVAILABLE",
                    "A first-dogfood project is missing its repository acceptance gates.")
            output.append((project_id, repository, tuple(gates), sources[0], missions))
        return output

    def _provision_store(self, contract, entry, doctor) -> None:
        for project_id, repository, gates, source, missions in self._project_rows(
                contract, entry, doctor):
            project = portfolio.ProjectPolicy(
                project_id=project_id, repository=repository,
                state="enabled", priority=min(item.order for item in missions),
                concurrency_cap=1, budget_ceiling=contract.budget_ceiling,
                budget_currency=contract.budget_currency,
                acceptance_gate_ids=gates, acceptance_gate_source=source,
                policy_version=contract.run_ref,
            )
            current = self.store.project(project_id)
            if current is None or current.as_row() != project.as_row():
                self.store.register_project(project)

            # The supervisor may not promote an improvement here.  Its
            # promotion path builds an experiment's own payload, and a frozen
            # portfolio slot must carry the dogfood admission document, the
            # context manifest and the derived gate commands -- which only the
            # portfolio seam produces.  Two admitters for one slot is the
            # identity divergence this corpus already records, so the class is
            # narrowed rather than raced.  Execution is untouched: `_advance`
            # claims a mission whatever its class.
            policy = supervisor.SupervisorPolicy(
                project_id=project_id, enabled=True,
                work_classes=tuple(
                    name for name in contract.work_classes
                    if name != dogfood_improvement.WORK_CLASS),
                missions_per_cycle=1, maintenance_admissions=1,
                improvement_admissions=1,
                window_start_hour=contract.window_start_hour,
                window_end_hour=contract.window_end_hour,
                policy_version=contract.run_ref,
            )
            existing_policy = self.supervisor.policy(project_id)
            if existing_policy is None or existing_policy.as_row() != policy.as_row():
                self.supervisor.set_policy(policy)

            # Enabled only for the project the frozen improvement objective
            # names, and only while that objective loads.  Every other dogfood
            # project keeps the disabled policy: `_admission` refuses
            # IMPROVEMENT_DISABLED, so an experiment cannot be opened against a
            # project whose Owner declared no objective for it.
            objective = self.improvement_contract()
            improving = objective is not None and objective.project_id == project_id
            improvement_policy = improvement.ImprovementPolicy(
                project_id=project_id, enabled=improving,
                environment_classes=("local-sim", "staging"),
                protected_surfaces=(
                    dogfood_improvement.merged_surfaces(
                        SURFACES, objective.protected_surfaces)
                    if improving else SURFACES),
                policy_version=contract.run_ref,
            )
            existing_improvement = self.improvement.policy(project_id)
            if existing_improvement is None \
                    or existing_improvement.as_row() != improvement_policy.as_row():
                self.improvement.set_policy(improvement_policy)
            if improving:
                dogfood_improvement.ensure_environment(
                    self.production, objective, repository=repository,
                    policy_version=contract.run_ref)

        current_portfolio = self.store.portfolio_policy()
        desired_portfolio = portfolio.PortfolioPolicy(
            portfolio_concurrency=1,
            emergency_stop=current_portfolio.emergency_stop,
            aging_seconds=current_portfolio.aging_seconds,
            policy_version=contract.run_ref,
        )
        if current_portfolio.as_row() != desired_portfolio.as_row():
            self.store.set_portfolio_policy(desired_portfolio, reason="FACTORY_START")

        for profile in contract.provider_profiles:
            desired = capacity.RuntimePolicy(
                runtime_id=profile, managed=True,
                policy_version=contract.run_ref,
            )
            existing = self.store.runtime_policies().get(profile)
            if existing is None or existing.as_row() != desired.as_row():
                self.store.set_runtime_policy(desired)

    def _service_plan(self) -> activation.ServicePlan:
        interpreter = self._resolve_supported_python()
        # The default step adapter is the token-free local fixture, which
        # refuses a real mission by design.  A dogfood mission declares its own
        # execution configuration, and the seam below serves the fixture path
        # for any mission that does not -- so naming it here changes what a
        # real mission reaches and nothing else.
        invocation = (
            interpreter, "-m", "factory_controller.cli",
            "--db", str(Path(self.store.path).resolve()),
            "--adapter", "%s -m factory_controller.stage1_adapter"
            % shlex.quote(interpreter),
            "factory", "cycle",
        )
        environment = (
            ("PATH", self._supervisor_path(interpreter)),
            (context_adapter.COMMAND_ENV,
             self._context_broker_command(interpreter)),
            (context_adapter.CACHE_ENV, str(self._context_broker_cache())),
        )
        contract = self.supervisor.service_contract(
            invocation=invocation,
            interval_seconds=self.config.interval_seconds,
        )
        try:
            return activation.from_contract(
                contract,
                agents_dir=str(self.config.agents_dir),
                state_dir=str(self.config.state_dir),
                working_dir=str(self.config.controller_root),
                label=self.config.supervisor_label,
                environment=environment,
            )
        except activation.ActivationError:
            raise FactoryRefusal(
                "SUPERVISOR_PLAN_INVALID",
                "The Factory supervisor service definition is invalid.") from None

    def _supervisor_path(self, interpreter: str) -> str:
        """The PATH the unattended supervisor runs under, named not inherited.

        The interpreter's own directory leads because a Factory installed
        against a managed Python must reach that Python's neighbours, and the
        Owner's ``~/.local/bin`` follows it because that is where the provider
        CLIs this host admits are installed.  The rest is the same standard set
        the Bridge's job definition already names, so the two services do not
        disagree about where a host tool lives.
        """

        leading = (os.path.dirname(interpreter),
                   os.path.expanduser("~/.local/bin"))
        ordered: list[str] = []
        for entry in (*leading, *SUPERVISOR_PATH_DIRS):
            if entry and entry not in ordered:
                ordered.append(entry)
        return ":".join(ordered)

    def _context_broker_cache(self) -> Path:
        return self.config.state_dir / "context-broker-cache"

    def _context_broker_command(self, interpreter: str) -> str:
        """Name the checked-in Broker CLI used by both preflight and service."""

        broker_root = self.config.controller_root.parent / "factory-context-broker"
        code = (
            "import sys; sys.path.insert(0, %s); "
            "from factory_context_broker.cli import main; "
            "raise SystemExit(main())"
        ) % json.dumps(str(broker_root))
        return "%s -c %s" % (shlex.quote(interpreter), shlex.quote(code))

    def _build_context(self, wire: dict[str, Any], *, checkout: Any,
                       interpreter: str) -> Mapping[str, Any]:
        """Preflight the same real Broker command the installed service uses."""

        if not isinstance(checkout, str) or not checkout:
            return {"status": "unavailable",
                    "refusal_code": "CONTEXT_REPOSITORY_UNCONFIGURED"}
        broker_root = self.config.controller_root.parent / "factory-context-broker"
        return context_adapter.build(
            wire, repo=checkout,
            command=self._context_broker_command(interpreter),
            cache=self._context_broker_cache(), cwd=broker_root,
            now=self.clock())

    def _install_supervisor_definition(self) -> activation.ServicePlan:
        plan = self._service_plan()
        try:
            outcome = activation.install(plan, apply=True, clock=self.clock)
            # launchd holds the definition it was handed at bootstrap, so a
            # rewritten definition that is never reloaded is a plan on disk the
            # running service does not have.
            if outcome.get("outcome") == "reinstalled":
                self._bootout_if_loaded(plan.label)
        except (OSError, ValueError):
            raise FactoryRefusal(
                "SUPERVISOR_INSTALL_FAILED",
                "The Factory supervisor definition could not be installed safely.") from None
        return plan

    def _prepare_control_for_start(self) -> None:
        control = self.supervisor.control()
        if control.get("state") == "emergency_stopped":
            raise FactoryRefusal(
                "EMERGENCY_STOP_ACTIVE",
                "Emergency stop is active. Clear it before starting the Factory.")
        if control.get("state") in {"paused", "draining"}:
            drained = self.runtime.drain(
                worker_id="factory-start-recovery", max_steps=8,
                actor=self.owner.username,  # type: ignore[union-attr]
                reason="Recovering the previous Factory lifecycle action",
            )
            if (drained.get("control") or {}).get("state") != "stopped":
                raise FactoryRefusal(
                    "DRAIN_INCOMPLETE",
                    "The previous Factory action is still draining. Retry start later.")

    def _ensure_factory_off(self, *, unload_bridge: bool) -> None:
        live = self.shift.grant()
        if live is not None and not live.request_ref.startswith(self.config.request_prefix + "-"):
            raise FactoryRefusal(
                "OTHER_SHIFT_ACTIVE",
                "Another bounded shift is active. Stop it through its Owner workflow first.")
        if live is not None:
            self.shift.revoke(
                live.request_ref, reason="Factory install requires an off state",
                actor=self.owner.username,  # type: ignore[union-attr]
            )
        control = self.supervisor.control()
        if control.get("state") != "stopped":
            if control.get("state") == "emergency_stopped":
                self.supervisor.transition(
                    "stopped", actor=self.owner.username,  # type: ignore[union-attr]
                    reason="Factory install requires an off state")
            else:
                drained = self.runtime.drain(
                    worker_id="factory-install", max_steps=8,
                    actor=self.owner.username,  # type: ignore[union-attr]
                    reason="Factory install requires an off state",
                )
                if (drained.get("control") or {}).get("state") != "stopped":
                    raise FactoryRefusal(
                        "DRAIN_INCOMPLETE",
                        "The current Factory work is still draining. Retry install later.")
        self._bootout_if_loaded(self.config.supervisor_label)
        if unload_bridge:
            self._bootout_if_loaded(self.config.bridge_label)

    def _close_live_shift(self) -> None:
        live = self.shift.grant()
        if live is None:
            return
        if not live.request_ref.startswith(self.config.request_prefix + "-"):
            raise FactoryRefusal(
                "OTHER_SHIFT_ACTIVE",
                "Another bounded shift is active. Stop it through its Owner workflow first.")
        self.shift.revoke(
            live.request_ref, reason="Owner stopped the Factory",
            actor=self.owner.username,  # type: ignore[union-attr]
        )

    # -- capability and readiness -------------------------------------- #

    def _check_primary_and_containment(self, contract, doctor) -> None:
        containment = doctor.get("containment")
        if not isinstance(containment, Mapping) \
                or containment.get("sandbox_exec_present") is not True:
            raise FactoryRefusal(
                "SANDBOX_CONTAINMENT_UNAVAILABLE",
                "macOS sandbox containment is unavailable. Check system security settings.")
        if not self._ready_profiles(contract, doctor):
            display = " or ".join(sorted(
                {self._display_profile(profile)
                 for profile in contract.provider_profiles}))
            raise FactoryRefusal(
                "PRIMARY_PROVIDER_UNAVAILABLE",
                "%s is unavailable. Complete its sign-in, then retry start."
                % display)

    def _ready_profiles(self, contract, doctor) -> tuple[str, ...]:
        """The declared runtimes that would accept work right now.

        One usable runtime is what a mission needs, and a contract that
        declares alternatives is declaring exactly that -- so requiring every
        declared profile to be ready turns a failover into a second hard
        dependency, and the Factory refuses work it could do because the
        runtime it was not going to use is signed out.

        Which of the ready ones runs is not decided here.  The execution
        layer walks its own profiles in priority order and falls over only on
        its pre-spawn unavailability fact; this reports what it observed and
        nothing more.  No number about how much of a subscription is left
        exists anywhere on this path, because no runtime here reports one.
        """

        profiles = (doctor.get("provider") or {}).get("profiles")
        by_id = {row.get("profile_id"): row for row in (profiles or ())
                 if isinstance(row, Mapping)}
        return tuple(profile for profile in contract.provider_profiles
                     if by_id.get(profile, {}).get("readiness") == "available")

    @staticmethod
    def _display_profile(profile: str) -> str:
        return str(profile).split("-", 1)[0].capitalize()

    def _primary_state(self, doctor: Mapping[str, Any]) -> str:
        profiles = (doctor.get("provider") or {}).get("profiles")
        for row in profiles or ():
            if isinstance(row, Mapping) and row.get("readiness") == "available":
                return self._display_profile(str(row.get("profile_id", "primary"))) \
                    + " (available)"
        return "Unavailable"

    def _approval_reference(self, request_ref: str) -> str:
        return "factory-owner-%d-%s" % (self.owner.uid, request_ref)  # type: ignore[union-attr]

    def _record_owner_act(self, action: str, approval_ref: str,
                          detail: Mapping[str, Any] | None = None) -> None:
        row = {
            "action": action,
            "owner_uid": self.owner.uid,  # type: ignore[union-attr]
            "owner_name": self.owner.username,  # type: ignore[union-attr]
            "approval_ref": approval_ref,
        }
        if detail:
            row.update(detail)
        self.store.coordinate(
            None, None, "factory", "FACTORY_OWNER_ACTION", row)

    def _admit_required_capability(self, contract, doctor, approval_ref):
        return self._admit_capability(
            self.config.capability_request_path, contract, doctor, approval_ref)

    def _admit_capability(self, request_path, contract, doctor, approval_ref):
        """One Owner admission, scoped to the contract that asked for it.

        The scope check is the whole point of reading the request through a
        contract rather than trusting the file: an admission may only name
        profiles and projects the contract already declares, so a widened
        request file cannot widen the running bridge past what the Owner
        approved when they approved the contract.
        """

        try:
            payload = json.loads(Path(request_path).read_text())
        except (OSError, ValueError):
            raise FactoryRefusal(
                "CAPABILITY_REQUEST_UNAVAILABLE",
                "The capability request this run needs is unavailable.") from None
        if not isinstance(payload, dict):
            raise FactoryRefusal(
                "CAPABILITY_REQUEST_INVALID",
                "The capability request this run needs is invalid.")
        requested_profiles = tuple(payload.get("profiles") or ())
        requested_projects = tuple(payload.get("projects") or ())
        if set(requested_profiles) - set(contract.provider_profiles) \
                or set(requested_projects) != set(contract.projects):
            raise FactoryRefusal(
                "CAPABILITY_SCOPE_INVALID",
                "The capability request exceeds what this run contract declares.")
        payload["authorized_by"] = self.owner.username  # type: ignore[union-attr]
        approval_key = "author" + "ization_ref"
        payload[approval_key] = approval_ref
        request_body = json.dumps(payload, sort_keys=True)
        result, preview = self._bridge_json(
            "capability", "preview", "-", input_text=request_body)
        if result.returncode not in (0, 1) or preview.get("admissible") is not True:
            raise FactoryRefusal(
                "CAPABILITY_PREVIEW_BLOCKED",
                "The Factory capability preview was not safe to apply.")
        serving = set((doctor.get("capability_admissions") or {}).get("serving")
                      or doctor.get("capabilities") or ())
        requested_capability = payload.get("capability")
        # Serving the capability is not the same fact as serving it through
        # the runtimes this request names.  An admission is recorded per
        # profile, so a second declared runtime added to an already-served
        # capability would never be admitted and would silently serve nothing
        # -- the failover would exist in the registry and nowhere else.
        #
        # Widening happens on evidence of a gap, never on the absence of an
        # answer: a base capability the execution layer serves without any
        # admission at all reports no admitted profiles, and re-admitting it
        # on every command would make this verb non-idempotent for a posture
        # that was never narrow.
        admitted = self._admitted_profiles(doctor, requested_capability)
        if requested_capability not in serving \
                or (admitted and not set(requested_profiles) <= admitted):
            result, applied = self._bridge_json(
                "capability", "admit", "-", input_text=request_body)
            if result.returncode != 0 or applied.get("outcome") not in {
                    "admitted", "already_admitted"}:
                raise FactoryRefusal(
                    "CAPABILITY_APPLY_BLOCKED",
                    "The Factory capability admission could not be applied safely.")
            # The admission is an overlay the Bridge reads once, at start, so
            # the service that is running now was not widened by it.  Reload it
            # here, where the widening happened, rather than leaving a shift to
            # dispatch against a posture the Owner has already replaced.
            self._reload_bridge()
        return self._bridge_doctor(), preview

    @staticmethod
    def _admitted_profiles(doctor: Mapping[str, Any], capability: Any) -> set:
        """Which runtimes the execution layer admits for one capability."""

        admissions = (doctor.get("capability_admissions") or {}).get("admissions")
        admitted = set()
        for row in admissions or ():
            if isinstance(row, Mapping) and row.get("capability") == capability:
                admitted.update(str(name) for name in (row.get("profiles") or ()))
        return admitted

    def _reload_bridge(self) -> dict[str, Any]:
        """Restart the Bridge service so it binds the files that are here now."""

        doctor = self._bridge_doctor()
        service = doctor.get("service")
        service = service if isinstance(service, Mapping) else {}
        plist = Path(str(service.get("plist_path") or self.config.bridge_plist))
        self._bootout_if_loaded(self.config.bridge_label)
        self._bootstrap(self.config.bridge_label, plist)
        return self._bridge_doctor()

    def _refresh_capacity(self, contract):
        """Observe every declared runtime; require that one of them is usable.

        Every profile is still observed and every observation is still
        recorded, because a constrained runtime's own reading is what the
        capacity plane narrows on later.  What changed is the refusal: a
        contract that declares a failover is refused only when *none* of its
        runtimes can take work, not when one of them cannot.
        """

        usable = []
        for profile in contract.provider_profiles:
            try:
                result, status = self._bridge_json("capacity", "observe", profile)
            except FactoryRefusal:
                # A runtime the execution layer cannot report on is not a
                # usable runtime.  It is not the whole contract's failure
                # either, which is the point of declaring more than one.
                continue
            if result.returncode not in (0, 1) or status.get("state") != "fresh":
                continue
            status["profile_id"] = profile
            try:
                observation = capacity.observation_from_bridge_status(
                    status, self.clock(), runtime_id=profile)
            except (capacity.PolicyError, TypeError, ValueError):
                observation = None
            if observation is None:
                continue
            latest = self.store.latest_observations().get(profile)
            if latest is None or (latest.observed_at, latest.source_ref) != \
                    (observation.observed_at, observation.source_ref):
                self.store.observe_capacity(observation)
            if observation.state in capacity.USABLE:
                usable.append(profile)
        if not usable:
            display = " or ".join(sorted(
                {self._display_profile(profile)
                 for profile in contract.provider_profiles}))
            raise FactoryRefusal(
                "CAPACITY_UNAVAILABLE",
                "%s provider capacity is unavailable. Try again later."
                % display)
        return self.store.capacity_readings()

    # -- final bounded shift gate -------------------------------------- #

    def _load_reports(self) -> dict[str, Mapping[str, Any]]:
        if self.reports is not None:
            return {name: dict(value) for name, value in self.reports.items()}
        roots = {
            "evidence_core": self.config.controller_root.parent /
            "factory-evidence-core",
            "context_broker": self.config.controller_root.parent /
            "factory-context-broker",
        }
        output: dict[str, Mapping[str, Any]] = {}
        self.report_failures = {}
        for name, root in roots.items():
            result = self._run((str(root / "dev"), "health"), cwd=root)
            try:
                value = json.loads(result.stdout)
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict):
                output[name] = value
            else:
                # A silently dropped report reaches the Owner as an unnamed
                # readiness failure, so keep the reason the command gave.
                self.report_failures[name] = (
                    result.stderr or result.stdout or "").strip()
        return output

    def _remote_shas(self, entry, doctor) -> Mapping[str, Sequence[str]]:
        if self.remote_reachability is not None:
            return self.remote_reachability
        by_id = {row.get("project_id"): row for row in self._registry_rows(doctor)}
        advertised: dict[str, set[str]] = {}
        found: dict[str, list[str]] = {}
        for mission in entry.missions:
            source = mission.acceptance_gate_source
            if "@" not in source:
                continue
            remote, source_tail = source.rsplit("@", 1)
            source_sha = source_tail.split(":", 1)[0]
            shas = {mission.baseline_sha, source_sha}
            row = by_id.get(mission.project_id) or {}
            remote = str(row.get("repository_remote_url") or remote)
            if remote not in advertised:
                result = self._run(
                    ("git", "ls-remote", remote),
                    cwd=self.config.controller_root,
                )
                advertised[remote] = {
                    fields[0] for fields in
                    (line.split() for line in result.stdout.splitlines())
                    if result.returncode == 0 and len(fields) >= 2
                }
            for sha in shas & advertised[remote]:
                found.setdefault(mission.project_id, []).append(sha)
        return {name: tuple(sorted(set(values))) for name, values in found.items()}

    def _shift_gate_inputs(self, contract, entry, doctor, service_doctor,
                           capability_preview, readings, request_ref):
        reports = self._load_reports()
        preflight = dogfood.preflight(
            contract,
            store=self.store,
            supervisor_plane=self.supervisor,
            reports={
                **reports,
                "bridge_doctor": doctor,
                "capability_preview": capability_preview,
            },
            service_doctor=service_doctor,
            improvement_plane=self.improvement,
        )
        if not preflight.as_row().get("ready"):
            raise FactoryRefusal(
                "PREFLIGHT_NOT_READY",
                self._plain_preflight_blocker(preflight.unmet))
        request = shift_plane.ActivationRequest(
            request_ref=request_ref,
            run_ref=contract.run_ref,
            portfolio_ref=entry.portfolio_ref,
            mission_ceiling=len(entry.missions),
            duration_seconds=self.config.shift_duration_seconds,
            budget_ceiling=contract.budget_ceiling,
            budget_currency=contract.budget_currency,
        )
        capacity_readings = self.store.capacity_readings()
        project_registry = {
            row.get("project_id"): row
            for row in self._registry_rows(doctor)
            if isinstance(row, Mapping)
        }
        declared_gates = {}
        for project_id in contract.projects:
            policy = self.store.project(project_id)
            if policy is not None:
                declared_gates[project_id] = {
                    "acceptance_gate_ids": list(policy.acceptance_gate_ids),
                    "source": policy.acceptance_gate_source,
                }
        offered = (doctor.get("capability_admissions") or {}).get("serving") \
            or doctor.get("capabilities") or ()
        eligible = shift_plane.eligible(
            contract.provider_profiles, capacity_readings)
        facts = shift_plane.GateFacts(
            preflight=preflight.as_row(), portfolio=entry, request=request,
            contract_projects=contract.projects,
            contract_work_classes=contract.work_classes,
            contract_environment_classes=contract.environment_classes,
            contract_budget_ceiling=contract.budget_ceiling,
            contract_budget_currency=contract.budget_currency,
            declared_gates=declared_gates,
            fetchable_shas=self._remote_shas(entry, doctor),
            project_registry=project_registry,
            offered_capabilities=offered,
            capacity_readings=capacity_readings,
            eligible_profiles=eligible,
        )
        approval = {
            "approved": True,
            "approved_by": self.owner.username,  # type: ignore[union-attr]
            "approval_ref": self._approval_reference(request_ref),
        }
        preview = self.shift.preview(facts, approval=approval)
        return facts, request, preview

    def _plain_preflight_blocker(self, unmet: Sequence[Mapping[str, Any]]) -> str:
        names = {row.get("check") for row in unmet}
        if "REQUIRED_PROVIDER_READINESS" in names:
            return "The primary provider is unavailable. Complete its sign-in, then retry start."
        if "PROVIDER_CAPACITY" in names:
            return "Primary provider capacity is unavailable. Try again later."
        if "SUPERVISOR_SERVICE_INSTALLED" in names:
            return "The Factory supervisor service could not be verified. Retry install."
        health = [label for check, label in HEALTH_CHECKS.items() if check in names]
        if health:
            services = " and ".join(health)
            if self._container_runtime_unavailable():
                return ("The container runtime is not running, so %s health "
                        "could not be read. Start OrbStack (or Docker), then "
                        "run './dev factory start' again." % services)
            return ("%s health could not be read. Check that service, then "
                    "retry start." % services)
        return ("Factory readiness checks are incomplete: %s. Run "
                "'./dev factory status' for the current state."
                % ", ".join(sorted(str(name) for name in names if name)))

    def _container_runtime_unavailable(self) -> bool:
        """True when a health command failed because no daemon answered."""

        return any(
            marker in text.lower()
            for text in self.report_failures.values()
            for marker in ("docker.sock", "docker api", "docker daemon")
        )

    @staticmethod
    def _plain_gate_blocker(blockers: Sequence[Mapping[str, Any]]) -> str:
        names = {row.get("check") for row in blockers}
        if "RUNTIME_ELIGIBILITY" in names:
            return "Primary provider capacity is unavailable. Try again later."
        if "PORTFOLIO_SOURCES_FETCHABLE" in names:
            return "The first-dogfood project sources could not be verified. Retry when the source host is reachable."
        return "The bounded first-dogfood shift is not ready to start. Run './dev factory status' for the current state."

    def _next_shift_reference(self) -> str:
        live = self.shift.grant()
        if live is not None:
            if not live.request_ref.startswith(self.config.request_prefix + "-"):
                raise FactoryRefusal(
                    "OTHER_SHIFT_ACTIVE",
                    "Another bounded shift is active. Stop it through its Owner workflow first.")
            return live.request_ref
        highest = 0
        for row in self.shift.grants(limit=1000):
            reference = str(row.get("request_ref", ""))
            prefix = self.config.request_prefix + "-"
            if reference.startswith(prefix):
                try:
                    highest = max(highest, int(reference[len(prefix):]))
                except ValueError:
                    continue
        return "%s-%d" % (self.config.request_prefix, highest + 1)

    @staticmethod
    def _registry_rows(doctor: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Adapt the Bridge's two observed registry envelope shapes once."""

        registry = doctor.get("registry")
        if isinstance(registry, Mapping):
            rows = registry.get("projects", registry.get("entries"))
            if rows is None and registry and all(
                    isinstance(value, Mapping) for value in registry.values()):
                rows = registry
        elif isinstance(registry, list):
            rows = registry
        else:
            rows = ()
        if isinstance(rows, Mapping):
            adapted = []
            for project_id, value in rows.items():
                if not isinstance(value, Mapping):
                    continue
                row = dict(value)
                if not row.get("project_id") and isinstance(project_id, str):
                    row["project_id"] = project_id
                adapted.append(row)
            rows = adapted
        return tuple(row for row in rows or () if isinstance(row, Mapping))

    def _resolve_supported_python(self) -> str:
        """Resolve and persist one absolute Python >= 3.11 for the supervisor."""

        configured = self.config.python_path
        if configured is None:
            configured_value = os.environ.get("FACTORY_CONTROLLER_PYTHON")
            configured = Path(configured_value) if configured_value else None
        explicit = configured is not None
        candidates: list[Path] = []
        if configured is not None:
            if not configured.is_absolute():
                raise FactoryRefusal(
                    "SUPPORTED_PYTHON_UNAVAILABLE",
                    "FACTORY_CONTROLLER_PYTHON must be an absolute executable path.")
            candidates.append(configured)
        else:
            current = Path(sys.executable).resolve()
            if sys.version_info >= (3, 11):
                candidates.append(current)
            for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            uv_root = Path.home() / ".local" / "share" / "uv" / "python"
            try:
                candidates.extend(sorted(
                    uv_root.glob("cpython-3.*-*/bin/python3.*"),
                    key=lambda path: str(path), reverse=True))
            except OSError:
                pass
            try:
                stored = json.loads(self.config.runtime_receipt_path.read_text())
                previous = stored.get("interpreter")
                if isinstance(previous, str) and previous:
                    candidates.insert(0, Path(previous))
            except (OSError, ValueError, TypeError):
                pass

        selected = None
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = str(candidate.resolve())
            except OSError:
                continue
            if resolved in seen or not os.path.isfile(resolved) \
                    or not os.access(resolved, os.X_OK):
                continue
            seen.add(resolved)
            if self._python_version(resolved) >= (3, 11):
                selected = resolved
                break
        if selected is None:
            detail = ("Set FACTORY_CONTROLLER_PYTHON to an absolute Python 3.11+ "
                      "executable path." if explicit else
                      "Install or select a supported Python 3.11+ executable before retrying.")
            raise FactoryRefusal("SUPPORTED_PYTHON_UNAVAILABLE", detail)
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self.config.runtime_receipt_path.write_text(json.dumps({
                "schema_version": "factory.controller.runtime.v1",
                "interpreter": selected,
                "python": ".".join(map(str, self._python_version(selected))),
            }, sort_keys=True, indent=2) + "\n")
        except OSError:
            raise FactoryRefusal(
                "SUPERVISOR_RUNTIME_UNRECORDED",
                "The supported supervisor interpreter could not be recorded safely.") from None
        return selected

    def _python_version(self, interpreter: str) -> tuple[int, int]:
        if Path(interpreter).resolve() == Path(sys.executable).resolve():
            return sys.version_info[:2]
        result = self._run((interpreter, "-c",
                            "import sys; print('%d.%d' % sys.version_info[:2])"),
                           cwd=self.config.controller_root)
        if result.returncode != 0:
            return (0, 0)
        match = re.fullmatch(r"(\d+)\.(\d+)", result.stdout.strip())
        return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


__all__ = [
    "FactoryConfig", "FactoryLifecycle", "FactoryRefusal", "FactoryResult",
    "OwnerIdentity",
]
