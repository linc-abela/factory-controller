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
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import activation
from . import capacity
from . import dogfood
from . import improvement
from . import portfolio
from . import production
from . import shift as shift_plane
from . import shift_runtime
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
    agents_dir: Path
    state_dir: Path
    bridge_prefix: Path = Path("/Users/Shared/factory")
    bridge_label: str = "com.softwarefactory.bridge"
    legacy_label: str = "com.astral.bridge"
    supervisor_label: str = activation.DEFAULT_LABEL
    interval_seconds: int = 300
    shift_duration_seconds: float = 4 * 3600.0
    request_prefix: str = "factory-shift"
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


class FactoryLifecycle:
    """Install, start, stop, and inspect one bounded local Factory."""

    def __init__(self, controller, *, config: FactoryConfig | None = None,
                 runner: Runner = run_host_command,
                 owner: OwnerIdentity | None = None,
                 clock: Callable[[], float] = time.time,
                 reports: Mapping[str, Mapping[str, Any]] | None = None,
                 remote_reachability: Mapping[str, Sequence[str]] | None = None) -> None:
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
        self.supervisor = supervisor.OperationsSupervisor(
            controller, clock=self.store.clock)
        self.shift = shift_plane.ShiftPlane(self.store, clock=self.store.clock)
        self.runtime = shift_runtime.ShiftRuntime(
            controller, supervisor_plane=self.supervisor)
        self.improvement = improvement.ImprovementPlane(
            self.store, production.ProductionLedger(self.store))

    # -- public surface ------------------------------------------------- #

    def dispatch(self, action: str) -> FactoryResult:
        try:
            if action == "install":
                return self.install()
            if action == "start":
                return self.start()
            if action == "stop":
                return self.stop()
            if action == "status":
                return self.status()
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
                       "Run './dev factory start' to repair the state."),
                details={"code": "INCONSISTENT_SERVICE_STATE",
                         "control": control, "bridge": doctor},
            )
        ready = live is not None and control.get("state") == "running" \
            and supervisor_loaded and bridge_loaded and bridge_healthy
        state = "ready" if ready else "off"
        label = "FACTORY READY" if ready else "FACTORY OFF"
        shift_summary = "Active" if live is not None else "Off"
        supervisor_summary = "Running" if supervisor_loaded else "Stopped"
        return FactoryResult(
            action="status", ok=True, state=state,
            lines=(label,
                   "Shift: " + shift_summary,
                   "Supervisor: " + supervisor_summary,
                   "Bridge: " + bridge_summary,
                   "Primary: " + primary),
            details={"control": control, "grant": None if live is None else live.as_row(),
                     "bridge": doctor},
        )

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

            policy = supervisor.SupervisorPolicy(
                project_id=project_id, enabled=True,
                work_classes=tuple(contract.work_classes),
                missions_per_cycle=1, maintenance_admissions=1,
                improvement_admissions=1,
                window_start_hour=contract.window_start_hour,
                window_end_hour=contract.window_end_hour,
                policy_version=contract.run_ref,
            )
            existing_policy = self.supervisor.policy(project_id)
            if existing_policy is None or existing_policy.as_row() != policy.as_row():
                self.supervisor.set_policy(policy)

            improvement_policy = improvement.ImprovementPolicy(
                project_id=project_id, enabled=False,
                environment_classes=("local-sim", "staging"),
                protected_surfaces=SURFACES,
                policy_version=contract.run_ref,
            )
            existing_improvement = self.improvement.policy(project_id)
            if existing_improvement is None \
                    or existing_improvement.as_row() != improvement_policy.as_row():
                self.improvement.set_policy(improvement_policy)

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
        invocation = (
            interpreter, "-m", "factory_controller.cli",
            "--db", str(Path(self.store.path).resolve()),
            "supervisor", "cycle",
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
            )
        except activation.ActivationError:
            raise FactoryRefusal(
                "SUPERVISOR_PLAN_INVALID",
                "The Factory supervisor service definition is invalid.") from None

    def _install_supervisor_definition(self) -> activation.ServicePlan:
        plan = self._service_plan()
        try:
            activation.install(plan, apply=True, clock=self.clock)
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
        profiles = (doctor.get("provider") or {}).get("profiles")
        by_id = {row.get("profile_id"): row for row in (profiles or ())
                 if isinstance(row, Mapping)}
        unavailable = [profile for profile in contract.provider_profiles
                       if by_id.get(profile, {}).get("readiness") != "available"]
        if unavailable:
            display = self._display_profile(unavailable[0])
            raise FactoryRefusal(
                "PRIMARY_PROVIDER_UNAVAILABLE",
                "%s is unavailable. Complete its sign-in, then retry start." % display)

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
        try:
            payload = json.loads(self.config.capability_request_path.read_text())
        except (OSError, ValueError):
            raise FactoryRefusal(
                "CAPABILITY_REQUEST_UNAVAILABLE",
                "The first-dogfood capability request is unavailable.") from None
        if not isinstance(payload, dict):
            raise FactoryRefusal(
                "CAPABILITY_REQUEST_INVALID",
                "The first-dogfood capability request is invalid.")
        requested_profiles = tuple(payload.get("profiles") or ())
        requested_projects = tuple(payload.get("projects") or ())
        if set(requested_profiles) - set(contract.provider_profiles) \
                or set(requested_projects) != set(contract.projects):
            raise FactoryRefusal(
                "CAPABILITY_SCOPE_INVALID",
                "The first-dogfood capability request exceeds the frozen portfolio.")
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
        if requested_capability not in serving:
            result, applied = self._bridge_json(
                "capability", "admit", "-", input_text=request_body)
            if result.returncode != 0 or applied.get("outcome") not in {
                    "admitted", "already_admitted"}:
                raise FactoryRefusal(
                    "CAPABILITY_APPLY_BLOCKED",
                    "The Factory capability admission could not be applied safely.")
        return self._bridge_doctor(), preview

    def _refresh_capacity(self, contract):
        for profile in contract.provider_profiles:
            result, status = self._bridge_json("capacity", "observe", profile)
            if result.returncode not in (0, 1) or status.get("state") != "fresh":
                raise FactoryRefusal(
                    "CAPACITY_UNAVAILABLE",
                    "%s provider capacity is unavailable. Try again later."
                    % self._display_profile(profile))
            status["profile_id"] = profile
            try:
                observation = capacity.observation_from_bridge_status(
                    status, self.clock(), runtime_id=profile)
            except (capacity.PolicyError, TypeError, ValueError):
                observation = None
            if observation is None or observation.state not in capacity.USABLE:
                raise FactoryRefusal(
                    "CAPACITY_UNAVAILABLE",
                    "%s provider capacity is unavailable. Try again later."
                    % self._display_profile(profile))
            latest = self.store.latest_observations().get(profile)
            if latest is None or (latest.observed_at, latest.source_ref) != \
                    (observation.observed_at, observation.source_ref):
                self.store.observe_capacity(observation)
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
        for name, root in roots.items():
            result = self._run((str(root / "dev"), "health"), cwd=root)
            try:
                value = json.loads(result.stdout)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                output[name] = value
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

    @staticmethod
    def _plain_preflight_blocker(unmet: Sequence[Mapping[str, Any]]) -> str:
        names = {row.get("check") for row in unmet}
        if "REQUIRED_PROVIDER_READINESS" in names:
            return "The primary provider is unavailable. Complete its sign-in, then retry start."
        if "PROVIDER_CAPACITY" in names:
            return "Primary provider capacity is unavailable. Try again later."
        if "SUPERVISOR_SERVICE_INSTALLED" in names:
            return "The Factory supervisor service could not be verified. Retry install."
        return "Factory readiness checks are incomplete. Run './dev factory status' for the current state."

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
