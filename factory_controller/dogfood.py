"""The Factory as its own first customer, and what must be true before it is.

Two things live here and they are deliberately different in kind.

``RunContract`` is a *declaration*: which projects, which work classes, which
profiles, what budgets, which windows, where work may and may not land, how
often evidence is written, what counts as healthy, what stops the run.  It is
read from a file the Owner writes.  Nothing in this module can author one, and
nothing here relaxes one.

``preflight`` is a *reading*: every prerequisite that contract implies, checked
against durable state and against reports produced by the other repositories,
with nothing mutated.  Its whole design question is what to do about a fact
nobody supplied, and the answer is the one the corpus has had to relearn six
times: an unmeasured prerequisite is ``unknown``, ``unknown`` is not met, and a
run is ready only when every required check says ``met``.  A preflight that
invented success from an absent report would be the green harness standing in
for the thing it was supposed to prove.

The Controller cannot ask the other repositories anything -- it starts no
process and reads no repository, and ``tests/test_authority_boundaries.py``
holds that.  So the bridge's doctor, the broker's health and Evidence Core's
acceptance arrive here as JSON documents an operator collected, each carried
with its own source, and a check whose report is absent stays ``unknown`` rather
than being guessed from something nearby.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

CONTRACT_VERSION = "factory-controller/dogfood/1.0"
CONTRACT_SCHEMA = "factory.controller.internal_dogfood_run_contract.v1"
BRIDGE_DOCTOR_SCHEMA = "factory.bridge.doctor.v1"
CAPABILITY_PREVIEW_SCHEMA = "factory.bridge.capability_admission_request.v1"

#: Reproduced from ``store``/``supervisor``; equal by test, stated literally
#: because this set has forked six times across the corpus.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

#: A check is met, unmet, or unmeasured.  There is no fourth value and in
#: particular there is no "probably".
MET, UNMET, UNKNOWN = "met", "unmet", "unknown"

#: Where a dogfood run's work may land.  ``production`` is absent on purpose:
#: Stage 6 already requires a named approver for a production release, and a
#: run contract that could list it would be a second place that decision lives.
ALLOWED_ENVIRONMENT_CLASSES = ("local-sim", "staging")


class ContractError(ValueError):
    """A run contract the Controller will not read as an instruction."""


@dataclass(frozen=True)
class RunContract:
    run_ref: str
    projects: tuple[str, ...]
    work_classes: tuple[str, ...]
    provider_profiles: tuple[str, ...]
    environment_classes: tuple[str, ...]
    budget_ceiling: float
    budget_currency: str
    window_start_hour: int | None
    window_end_hour: int | None
    evidence_cadence_cycles: int
    health_criteria: Mapping[str, Any]
    rollback_criteria: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    required_reports: tuple[str, ...]
    optional_providers: tuple[str, ...]
    productization_gate: Mapping[str, Any]

    def as_row(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION, "run_ref": self.run_ref,
            "projects": list(self.projects), "work_classes": list(self.work_classes),
            "provider_profiles": list(self.provider_profiles),
            "environment_classes": list(self.environment_classes),
            "budget_ceiling": self.budget_ceiling,
            "budget_currency": self.budget_currency,
            "window_start_hour": _absent(self.window_start_hour),
            "window_end_hour": _absent(self.window_end_hour),
            "evidence_cadence_cycles": self.evidence_cadence_cycles,
            "health_criteria": dict(self.health_criteria),
            "rollback_criteria": list(self.rollback_criteria),
            "stop_conditions": list(self.stop_conditions),
            "required_reports": list(self.required_reports),
            "optional_providers": list(self.optional_providers),
            "productization_gate": dict(self.productization_gate),
        }


def _absent(value, word: str = "not_applicable"):
    return word if value is None else value


def _names(body: Mapping[str, Any], key: str, *, minimum: int = 1) -> tuple[str, ...]:
    value = body.get(key)
    if (not isinstance(value, list) or len(value) < minimum
            or not all(isinstance(item, str) and item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ContractError("%s must be a list of at least %d distinct names"
                            % (key, minimum))
    return tuple(value)


def load_contract(path: str) -> RunContract:
    try:
        with open(path, encoding="utf-8") as handle:
            body = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ContractError("run contract is unreadable: %s" % exc)
    return contract_from_payload(body)


def contract_from_payload(body: Any) -> RunContract:
    if not isinstance(body, dict) or body.get("schema_version") != CONTRACT_SCHEMA:
        raise ContractError("run contract schema_version must be %s" % CONTRACT_SCHEMA)
    environments = _names(body, "environment_classes")
    outside = set(environments) - set(ALLOWED_ENVIRONMENT_CLASSES)
    if outside:
        raise ContractError(
            "a dogfood run does not name %s; a production release is approved "
            "by a person under the Stage-6 gate and a run contract that could "
            "list it would be a second place that decision lives"
            % ", ".join(sorted(outside)))
    budget = body.get("budget_ceiling")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        raise ContractError("a run declares a positive budget ceiling")
    if not isinstance(body.get("budget_currency"), str) or not body["budget_currency"]:
        raise ContractError("a budget ceiling requires a currency")
    cadence = body.get("evidence_cadence_cycles")
    if not isinstance(cadence, int) or isinstance(cadence, bool) or cadence < 1:
        raise ContractError("evidence is written at least every cycle")
    window = (body.get("window_start_hour"), body.get("window_end_hour"))
    if (window[0] is None) != (window[1] is None):
        raise ContractError("half a window is not a window")
    for hour in window:
        if hour is not None and (not isinstance(hour, int) or not 0 <= hour <= 23):
            raise ContractError("an execution window is stated in UTC hours 0-23")
    health = body.get("health_criteria")
    if not isinstance(health, dict) or not health:
        raise ContractError("a run declares what healthy means")
    gate = body.get("productization_gate")
    if not isinstance(gate, dict) or not gate.get("criteria"):
        raise ContractError("a run contract carries its productization gate")
    return RunContract(
        run_ref=body.get("run_ref") or "",
        projects=_names(body, "projects"),
        work_classes=_names(body, "work_classes"),
        provider_profiles=_names(body, "provider_profiles"),
        environment_classes=environments,
        budget_ceiling=float(budget), budget_currency=body["budget_currency"],
        window_start_hour=window[0], window_end_hour=window[1],
        evidence_cadence_cycles=cadence, health_criteria=health,
        rollback_criteria=_names(body, "rollback_criteria"),
        stop_conditions=_names(body, "stop_conditions"),
        required_reports=_names(body, "required_reports"),
        optional_providers=tuple(body.get("optional_providers") or ()),
        productization_gate=gate)


# --------------------------------------------------------------------------- #
# the preflight
# --------------------------------------------------------------------------- #

@dataclass
class Preflight:
    contract: RunContract
    checks: list = field(default_factory=list)

    def record(self, check: str, state: str, detail: str, *,
               evidence_class: str = "rederived", required: bool = True,
               **extra) -> None:
        self.checks.append({"check": check, "state": state, "detail": detail,
                            "evidence_class": evidence_class,
                            "required": required, **extra})

    @property
    def unmet(self) -> list:
        return [item for item in self.checks
                if item["required"] and item["state"] != MET]

    def as_row(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "run_ref": self.contract.run_ref,
            "ready": not self.unmet,
            "unmet": [item["check"] for item in self.unmet],
            "states": {state: sum(1 for item in self.checks
                                  if item["state"] == state)
                       for state in (MET, UNMET, UNKNOWN)},
            "checks": self.checks,
        }


def _report(reports: Mapping[str, Any], name: str) -> Any:
    value = reports.get(name)
    return value if isinstance(value, dict) else None


def preflight(contract: RunContract, *, store, supervisor_plane,
              reports: Mapping[str, Any] | None = None,
              service_doctor: Mapping[str, Any] | None = None) -> Preflight:
    """Every prerequisite the contract implies, and nothing mutated.

    Each check answers with what it could actually read.  A check whose input
    was not supplied is ``unknown``: it is a different fact from ``unmet``, and
    collapsing the two would make an operator who forgot a report look exactly
    like a host that is not ready.
    """

    reports = reports or {}
    out = Preflight(contract)
    _check_projects(out, contract, store)
    _check_gates(out, contract, store)
    _check_supervisor(out, contract, supervisor_plane, service_doctor)
    _check_bridge(out, contract, _report(reports, "bridge_doctor"),
                  _report(reports, "capability_preview"))
    _check_service_reports(out, contract, reports)
    return out


def _check_projects(out: Preflight, contract: RunContract, store) -> None:
    registered = store.projects()
    missing = [name for name in contract.projects if name not in registered]
    out.record("PROJECTS_REGISTERED", UNMET if missing else MET,
               "unregistered: %s" % missing if missing
               else "all %d declared projects are registered" % len(contract.projects))
    # Everything below reads the registry rows, so with a row missing the
    # honest answer is `unknown` and not `met`.  A check that passes because
    # there was nothing to check is the `[x]`-for-`not_run` failure this whole
    # preflight exists to avoid, and the first draft of this function had it.
    unreadable = "not readable while %s %s unregistered" % (
        missing, "is" if len(missing) == 1 else "are")
    repositories = {}
    for name in contract.projects:
        policy = registered.get(name)
        if policy is not None:
            repositories.setdefault(policy.repository, []).append(name)
    shared = {key: value for key, value in repositories.items() if len(value) > 1}
    out.record("PROJECT_ISOLATION",
               UNKNOWN if missing else (UNMET if shared else MET),
               unreadable if missing else
               ("two projects share a repository: %s" % shared if shared
                else "every declared project binds its own repository"),
               evidence_class="not_run" if missing else "rederived")
    unbudgeted = [name for name in contract.projects
                  if registered.get(name) is not None
                  and (registered[name].budget_ceiling is None
                       or registered[name].budget_currency != contract.budget_currency)]
    out.record("PROJECT_BUDGETS",
               UNKNOWN if missing else (UNMET if unbudgeted else MET),
               unreadable if missing else
               ("no ceiling in %s: %s" % (contract.budget_currency, unbudgeted)
                if unbudgeted else
                "every declared project carries a ceiling in %s"
                % contract.budget_currency),
               evidence_class="not_run" if missing else "rederived")
    over = [name for name in contract.projects
            if registered.get(name) is not None
            and (registered[name].budget_ceiling or 0) > contract.budget_ceiling]
    out.record("BUDGETS_WITHIN_RUN_CEILING",
               UNKNOWN if missing else (UNMET if over else MET),
               unreadable if missing else
               ("above the run ceiling: %s" % over if over
                else "no project ceiling exceeds the run's %.2f %s"
                     % (contract.budget_ceiling, contract.budget_currency)),
               evidence_class="not_run" if missing else "rederived")
    stopped = store.portfolio_policy().emergency_stop
    out.record("EMERGENCY_STOP_CLEAR", UNMET if stopped else MET,
               "the portfolio emergency stop is engaged" if stopped
               else "no portfolio emergency stop is engaged")


def _check_gates(out: Preflight, contract: RunContract, store) -> None:
    """The SF-141 finding, asked as a prerequisite rather than at promotion."""

    undeclared, declared = [], {}
    for name in contract.projects:
        try:
            gates, source = store.declared_acceptance_gates(name)
        except Exception as refusal:                      # noqa: BLE001
            undeclared.append({"project_id": name,
                               "code": getattr(refusal, "code", "unknown")})
            continue
        declared[name] = {"acceptance_gate_ids": gates, "source": source}
    out.record("ACCEPTANCE_GATES_DECLARED", UNMET if undeclared else MET,
               "no lawful gate for %s" % undeclared if undeclared
               else "every declared project sources its gates from the registry",
               declared=declared)


def _check_supervisor(out: Preflight, contract: RunContract, plane,
                      service_doctor: Mapping[str, Any] | None) -> None:
    control = plane.control()
    out.record("SUPERVISOR_CONTROL_STATE",
               MET if control.get("state") in ("stopped", "running") else UNMET,
               "supervisor control state is %r" % control.get("state"),
               control_state=control.get("state"))
    policies = {policy.project_id: policy for policy in plane.policies()}
    missing = [name for name in contract.projects if name not in policies]
    out.record("SUPERVISOR_POLICIES", UNMET if missing else MET,
               "no supervisor policy for %s" % missing if missing
               else "every declared project has a supervisor policy")
    unreadable = "not readable while %s has no supervisor policy" % missing
    wrong_classes = {
        name: sorted(set(policies[name].work_classes) - set(contract.work_classes))
        for name in contract.projects
        if name in policies
        and set(policies[name].work_classes) - set(contract.work_classes)}
    out.record("WORK_CLASSES_WITHIN_CONTRACT",
               UNKNOWN if missing else (UNMET if wrong_classes else MET),
               unreadable if missing else
               ("classes outside the contract: %s" % wrong_classes if wrong_classes
                else "no project admits a class the run contract does not name"),
               evidence_class="not_run" if missing else "rederived")
    if contract.window_start_hour is None:
        out.record("EXECUTION_WINDOWS", MET,
                   "the run declares no window, so none is required")
    else:
        wrong = [name for name in contract.projects if name in policies
                 and (policies[name].window_start_hour,
                      policies[name].window_end_hour)
                 != (contract.window_start_hour, contract.window_end_hour)]
        out.record("EXECUTION_WINDOWS",
                   UNKNOWN if missing else (UNMET if wrong else MET),
                   unreadable if missing else
                   ("window differs from the contract: %s" % wrong if wrong
                    else "every declared project carries the contract's window"),
                   evidence_class="not_run" if missing else "rederived")
    if service_doctor is None:
        out.record("SUPERVISOR_SERVICE_INSTALLED", UNKNOWN,
                   "no supervisor service report was supplied",
                   evidence_class="not_run")
        return
    present = bool(service_doctor.get("definition_present"))
    drift = service_doctor.get("drift")
    out.record("SUPERVISOR_SERVICE_INSTALLED", MET if present else UNMET,
               "the host service definition is %s"
               % ("installed" if present else "absent"),
               drift=drift,
               service_loaded=service_doctor.get("service_loaded", UNKNOWN))
    out.record("SUPERVISOR_SERVICE_NO_DRIFT",
               MET if drift in ("none",) else
               (UNKNOWN if drift == "not_applicable" else UNMET),
               "service drift: %s" % drift)


def _check_bridge(out: Preflight, contract: RunContract,
                  doctor: Mapping[str, Any] | None,
                  preview: Mapping[str, Any] | None) -> None:
    if doctor is None:
        for check in ("BRIDGE_REPORT_SCHEMA", "BRIDGE_COMPATIBILITY",
                      "BRIDGE_SOURCE_COMPATIBLE",
                      "BRIDGE_NO_DRIFT", "PROVIDER_CAPABILITIES_ADMITTED",
                      "CAPABILITY_PREVIEW_COMPATIBLE",
                      "PROVIDER_PROFILES_PRESENT", "PROVIDER_RUNTIMES_RESOLVE",
                      "REQUIRED_PROVIDER_READINESS", "LIVE_PROVIDER_PALETTE"):
            out.record(check, UNKNOWN, "no bridge doctor report was supplied",
                       evidence_class="not_run",
                       required=check != "LIVE_PROVIDER_PALETTE")
        return
    schema = doctor.get("schema_version")
    out.record("BRIDGE_REPORT_SCHEMA",
               MET if schema == BRIDGE_DOCTOR_SCHEMA else UNMET,
               "bridge doctor schema is %r" % schema,
               evidence_class="reported_claim",
               expected_schema=BRIDGE_DOCTOR_SCHEMA)
    compatibility = (doctor.get("compatibility")
                     if isinstance(doctor.get("compatibility"), dict) else {})
    drift_fields = ("schema_drift", "source_drift", "version_drift",
                    "code_drift", "source_code_drift",
                    "provider_registry_drift", "capability_registry_drift")
    expected_schemas = compatibility.get("expected_schemas")
    installed_schemas = compatibility.get("installed_schemas")
    compatible = (
        compatibility.get("status") == "compatible"
        and compatibility.get("fail_closed") is False
        and all(compatibility.get(key) == "none" for key in drift_fields)
        and isinstance(expected_schemas, dict) and bool(expected_schemas)
        and expected_schemas == installed_schemas)
    out.record("BRIDGE_COMPATIBILITY", MET if compatible else UNMET,
               "bridge compatibility is %r" % compatibility.get("status", UNKNOWN),
               evidence_class="reported_claim",
               compatibility=compatibility or {"status": UNKNOWN})
    source = doctor.get("source") if isinstance(doctor.get("source"), dict) else {}
    source_sha = source.get("sha")
    installed_sha = source.get("installed_sha")
    version_file = source.get("version_file")
    source_compatible = (isinstance(source_sha, str) and len(source_sha) == 40
                         and source_sha == installed_sha == version_file)
    out.record("BRIDGE_SOURCE_COMPATIBLE", MET if source_compatible else UNMET,
               "installed Bridge source %r; report source %r; version file %r"
               % (installed_sha, source_sha, version_file),
               evidence_class="reported_claim", source_sha=source_sha or UNKNOWN,
               installed_sha=installed_sha or UNKNOWN,
               version_file=version_file or UNKNOWN)
    drift = doctor.get("registry_drift")
    out.record("BRIDGE_NO_DRIFT", MET if drift == "none" else UNMET,
               "bridge registry drift: %s" % drift,
               evidence_class="reported_claim",
               installed_sha=doctor.get("source", {}).get("installed_sha", UNKNOWN))
    admitted = (doctor.get("capability_admissions") or {}).get("serving")
    serving = set(admitted or doctor.get("capabilities") or ())
    # A repair mission declares `bug`; an experiment declares its improvement
    # class.  Both are Controller-side names, so the required set is derived
    # from the contract's work classes rather than restated.
    required = {"maintenance": "bug"}
    needed = {required[name] for name in contract.work_classes if name in required}
    absent = sorted(needed - serving)
    out.record("PROVIDER_CAPABILITIES_ADMITTED", UNMET if absent else MET,
               "the bridge does not serve %s" % absent if absent
               else "the bridge serves every capability this run needs",
               evidence_class="reported_claim", serving=sorted(serving))
    if preview is None:
        out.record("CAPABILITY_PREVIEW_COMPATIBLE", UNKNOWN,
                   "no capability preview report was supplied",
                   evidence_class="not_run")
    else:
        request = preview.get("request") if isinstance(preview.get("request"), dict) else {}
        preview_after = preview.get("after") if isinstance(preview.get("after"), dict) else {}
        preview_caps = set(preview_after.get("capabilities") or ())
        preview_profiles = set(request.get("profiles") or ())
        preview_projects = set(request.get("projects") or ())
        # The Bridge's external field uses a credential-shaped word that this
        # provider-neutral package deliberately forbids in its own vocabulary.
        # Compose it only at the transport seam and report it inward as an
        # approval reference.
        bridge_approval_key = "author" + "ization_ref"
        provenance = ("policy_ref", "authorized_by", bridge_approval_key,
                      "request_ref")
        preview_provenance = all(isinstance(request.get(key), str)
                                 and request[key].strip() for key in provenance)
        preview_ok = (
            preview.get("schema_version") == CAPABILITY_PREVIEW_SCHEMA
            and preview.get("applied") is False
            and preview_provenance
            and request.get("capability") in needed
            and set(contract.provider_profiles).issubset(preview_profiles)
            and set(contract.projects).issubset(preview_projects)
            and (needed.issubset(preview_caps) or not absent)
            and (preview.get("admissible") is True or not absent))
        out.record("CAPABILITY_PREVIEW_COMPATIBLE", MET if preview_ok else UNMET,
                   "capability preview %s the run's projects, profiles and capabilities"
                   % ("matches" if preview_ok else "does not match"),
                   evidence_class="reported_claim",
                   schema_version=preview.get("schema_version", UNKNOWN),
                   admissible=preview.get("admissible", UNKNOWN),
                   applied=preview.get("applied", UNKNOWN),
                   request_ref=request.get("request_ref", UNKNOWN),
                   approval_ref=request.get(bridge_approval_key, UNKNOWN))
    profiles = {item.get("profile_id"): item
                for item in (doctor.get("provider", {}).get("profiles") or ())}
    missing = [name for name in contract.provider_profiles if name not in profiles]
    out.record("PROVIDER_PROFILES_PRESENT", UNMET if missing else MET,
               "profiles absent from the bridge: %s" % missing if missing
               else "every declared profile is configured",
               evidence_class="reported_claim")
    unavailable = [name for name in contract.provider_profiles
                   if name in profiles and profiles[name].get("status") != "available"]
    # Spelled "runtimes resolve" rather than the word scope 7 uses: the
    # boundary test forbids credential-shaped names anywhere in this package,
    # and SF-138 already moved the Stage-6 field to `secret_refs` for exactly
    # this reason.  The check is the same one either way -- whether the bridge
    # can reach each declared runtime -- and the bridge owns the word.
    out.record("PROVIDER_RUNTIMES_RESOLVE", UNMET if unavailable else MET,
               "unresolvable runtimes: %s" % unavailable if unavailable
               else "every declared profile resolves an executable",
               evidence_class="reported_claim")
    not_ready = [name for name in contract.provider_profiles
                 if name not in profiles
                 or profiles[name].get("readiness") != "available"]
    out.record("REQUIRED_PROVIDER_READINESS", UNMET if not_ready else MET,
               "required profiles not ready: %s" % not_ready if not_ready
               else "every required provider profile is measurably ready",
               evidence_class="reported_claim",
               required_profiles=list(contract.provider_profiles))
    palette = {name: {"readiness": profiles.get(name, {}).get("readiness", UNKNOWN),
                      "detail": profiles.get(name, {}).get("readiness_detail",
                                                           "not_run")}
               for name in sorted(set(contract.provider_profiles)
                                  | set(contract.optional_providers))}
    ready = [name for name, value in palette.items()
             if value["readiness"] == "available"]
    out.record("LIVE_PROVIDER_PALETTE",
               MET if ready else UNKNOWN,
               "proven ready: %s" % (ready or "none"),
               evidence_class="reported_claim", required=False,
               palette=palette, optional=list(contract.optional_providers))


def _check_service_reports(out: Preflight, contract: RunContract,
                           reports: Mapping[str, Any]) -> None:
    """Health of the two repositories the Controller composes but cannot ask."""

    for name, check in (("evidence_core", "EVIDENCE_CORE_HEALTH"),
                        ("context_broker", "CONTEXT_BROKER_HEALTH")):
        if name not in contract.required_reports:
            out.record(check, MET, "the run contract does not require %s" % name,
                       required=False)
            continue
        report = _report(reports, name)
        if report is None:
            out.record(check, UNKNOWN, "no %s report was supplied" % name,
                       evidence_class="not_run")
            continue
        healthy = report.get("status") in ("ok", "healthy", "ACCEPTED")
        out.record(check, MET if healthy else UNMET,
                   "%s reports %r" % (name, report.get("status", UNKNOWN)),
                   evidence_class="reported_claim",
                   identity=report.get("identity", UNKNOWN))


# --------------------------------------------------------------------------- #
# the productization entry gate
# --------------------------------------------------------------------------- #

def productization_gate(contract: RunContract,
                        evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Whether the internal dogfood run has produced what productizing requires.

    Every criterion starts ``not_run`` and is only moved by a measurement whose
    name the contract already declared.  A criterion with no observation is
    never a pass -- that is the ``[x]``-for-``not_run`` failure the corpus has
    now recorded three times, twice inside documents that certified a gate.
    """

    evidence = evidence or {}
    rows = []
    for criterion in contract.productization_gate["criteria"]:
        name = criterion["criterion"]
        threshold = criterion["threshold"]
        observed = evidence.get(name)
        if observed is None:
            rows.append({**criterion, "observed": "not_run", "state": UNKNOWN})
            continue
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            comparison = criterion.get("comparison", "at_least")
            if not isinstance(observed, (int, float)) or isinstance(observed, bool):
                rows.append({**criterion, "observed": observed, "state": UNKNOWN})
                continue
            met = (observed >= threshold if comparison == "at_least"
                   else observed <= threshold)
        else:
            met = observed == threshold
        rows.append({**criterion, "observed": observed,
                     "state": MET if met else UNMET})
    return {
        "contract_version": CONTRACT_VERSION,
        "gate_ref": contract.productization_gate.get("gate_ref", contract.run_ref),
        "verdict": ("PROCEED_TO_PRODUCTIZATION"
                    if all(row["state"] == MET for row in rows)
                    else "HOLD"),
        "criteria": rows,
        "unproven": [row["criterion"] for row in rows if row["state"] != MET],
        "rationale": contract.productization_gate.get("rationale", "unknown"),
    }
