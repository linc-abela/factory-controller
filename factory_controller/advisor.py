"""The advisory coordination port, intended for Hermes.

An advisor may *propose*.  It may never decide.  Everything it returns passes
through :func:`review` before any durable state moves, and review compares each
proposal against the Owner's policy and against facts the Controller already
holds -- so the advisor's authority is exactly the authority the Owner wrote
down in advance, and nothing it says can widen that.

The port is optional in the strongest sense available: :func:`consult` has no
failure mode that reaches the scheduler.  Silence, a malformed body, a
connection refused, an exception from the adapter -- each becomes an outcome
row and the Factory schedules deterministically without it.  That is checked by
running the same missions with an advisor, with a broken advisor, and with none
at all, and comparing the schedules.

Hermes is present on this host and is *not* usable without an Owner credential;
see :class:`HermesAdvisor` for the measured facts.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import portfolio


#: Where the advisory service answers on this host.  An address, not a
#: dependency: nothing fails if it is absent.
DEFAULT_ENDPOINT = "http://127.0.0.1:9119"

PROPOSAL_KINDS = ("decompose", "dependency_edge", "project_priority",
                  "specialist_profile", "next_mission")

#: An advisor never gets these, under any policy.  They are not omissions from
#: the allowlist -- they are the authority boundary itself, so they are refused
#: before the allowlist is even consulted.
FORBIDDEN_KINDS = ("create_project", "set_budget", "set_acceptance_gates",
                   "seal_evidence", "admit_execution", "set_execution_mode",
                   "set_context_manifest", "cancel_portfolio",
                   "activate_shift", "revoke_shift", "resume_shift",
                   "clear_blocker", "assert_readiness", "assert_capacity",
                   "widen_capability", "approve_production")


@dataclass(frozen=True)
class AdvisorPolicy:
    """The Owner's grant to the advisor.  Empty by default: no grant at all."""

    enabled: bool = False
    allowed_kinds: tuple[str, ...] = ()
    priority_min: int = 0
    priority_max: int = 1000
    allowed_profiles: tuple[str, ...] = ()
    max_proposals: int = 8

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "AdvisorPolicy":
        raw = (payload or {}).get("advisor_policy")
        if not isinstance(raw, dict):
            return cls()
        kinds = tuple(item for item in raw.get("allowed_kinds") or () if isinstance(item, str))
        return cls(
            enabled=bool(raw.get("enabled", False)),
            allowed_kinds=kinds,
            priority_min=int(raw.get("priority_min", 0)),
            priority_max=int(raw.get("priority_max", 1000)),
            allowed_profiles=tuple(item for item in raw.get("allowed_profiles") or ()
                                   if isinstance(item, str)),
            max_proposals=int(raw.get("max_proposals", 8)),
        )


@dataclass(frozen=True)
class Facts:
    """What the Controller already knows, for checking proposals against."""

    projects: tuple[str, ...] = ()
    missions: tuple[str, ...] = ()
    edges: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    index: int
    kind: str
    accepted: bool
    code: str
    proposal: dict[str, Any]
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"index": self.index, "kind": self.kind, "accepted": self.accepted,
                "code": self.code, "proposal": self.proposal, "detail": self.detail}


@dataclass(frozen=True)
class Outcome:
    status: str
    verdicts: tuple[Verdict, ...] = ()
    refusal_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.accepted)

    def as_row(self) -> dict[str, Any]:
        return {"status": self.status, "refusal_code": self.refusal_code,
                "detail": self.detail,
                "verdicts": [verdict.as_row() for verdict in self.verdicts]}


class AdvisoryPort(Protocol):
    def advise(self, request: dict[str, Any]) -> Any: ...


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def review(proposals: Any, policy: AdvisorPolicy, facts: Facts) -> tuple[Verdict, ...]:
    """Adjudicate every proposal.  Nothing here mutates anything."""

    if not isinstance(proposals, list):
        return (Verdict(0, "unknown", False, "ADVISOR_MALFORMED_RESPONSE", {}),)
    verdicts: list[Verdict] = []
    # Proposed edges are checked against the graph *plus the edges accepted
    # earlier in this same batch*, so an advisor cannot smuggle a cycle past the
    # check by splitting it across two proposals.
    edges = {key: list(value) for key, value in facts.edges.items()}
    for index, raw in enumerate(proposals):
        if not isinstance(raw, dict):
            verdicts.append(Verdict(index, "unknown", False, "ADVISOR_MALFORMED_PROPOSAL", {}))
            continue
        kind = raw.get("kind")
        proposal = {key: value for key, value in raw.items() if key != "kind"}
        if not isinstance(kind, str):
            verdicts.append(Verdict(index, "unknown", False, "ADVISOR_MALFORMED_PROPOSAL", proposal))
            continue
        if index >= policy.max_proposals:
            verdicts.append(Verdict(index, kind, False, "ADVISOR_PROPOSAL_LIMIT_EXCEEDED", proposal,
                                    {"max_proposals": policy.max_proposals}))
            continue
        verdicts.append(_review_one(index, kind, proposal, policy, facts, edges))
    return tuple(verdicts)


def _review_one(index: int, kind: str, proposal: dict[str, Any], policy: AdvisorPolicy,
                facts: Facts, edges: dict[str, list[str]]) -> Verdict:
    if kind in FORBIDDEN_KINDS:
        return Verdict(index, kind, False, "ADVISOR_AUTHORITY_BOUNDARY", proposal)
    if not policy.enabled:
        return Verdict(index, kind, False, "ADVISOR_DISABLED", proposal)
    if kind not in PROPOSAL_KINDS:
        return Verdict(index, kind, False, "ADVISOR_UNKNOWN_KIND", proposal)
    if kind not in policy.allowed_kinds:
        return Verdict(index, kind, False, "ADVISOR_KIND_NOT_PERMITTED", proposal)

    if kind == "project_priority":
        project = proposal.get("project_id")
        priority = proposal.get("priority")
        if project not in facts.projects:
            # An advisor naming an unregistered project is proposing to create
            # one, which is the boundary, not a typo.
            return Verdict(index, kind, False, "ADVISOR_UNKNOWN_PROJECT", proposal)
        if not isinstance(priority, int) or isinstance(priority, bool):
            return Verdict(index, kind, False, "ADVISOR_MALFORMED_PROPOSAL", proposal)
        if not policy.priority_min <= priority <= policy.priority_max:
            return Verdict(index, kind, False, "ADVISOR_PRIORITY_OUT_OF_BOUNDS", proposal,
                           {"bounds": [policy.priority_min, policy.priority_max]})
        return Verdict(index, kind, True, "ADVISOR_PROPOSAL_ACCEPTED", proposal)

    if kind == "specialist_profile":
        mission = proposal.get("mission_id")
        profile = proposal.get("profile")
        if mission not in facts.missions:
            return Verdict(index, kind, False, "ADVISOR_UNKNOWN_MISSION", proposal)
        if not isinstance(profile, str) or not profile:
            return Verdict(index, kind, False, "ADVISOR_MALFORMED_PROPOSAL", proposal)
        if profile not in policy.allowed_profiles:
            # The palette stays lean by construction: a specialist profile the
            # Owner has not admitted cannot be introduced by an advisor.
            return Verdict(index, kind, False, "ADVISOR_PROFILE_NOT_ALLOWLISTED", proposal)
        return Verdict(index, kind, True, "ADVISOR_PROPOSAL_ACCEPTED", proposal)

    if kind == "dependency_edge":
        mission = proposal.get("mission_id")
        depends_on = proposal.get("depends_on")
        on_failure = proposal.get("on_failure", "block")
        if mission not in facts.missions or depends_on not in facts.missions:
            return Verdict(index, kind, False, "ADVISOR_UNKNOWN_MISSION", proposal)
        if on_failure not in portfolio.ON_FAILURE:
            return Verdict(index, kind, False, "ADVISOR_MALFORMED_PROPOSAL", proposal)
        cycle = portfolio.cycle_path(edges, mission, depends_on)
        if cycle:
            return Verdict(index, kind, False, "ADVISOR_DEPENDENCY_CYCLE", proposal,
                           {"cycle": list(cycle)})
        edges.setdefault(mission, []).append(depends_on)
        return Verdict(index, kind, True, "ADVISOR_PROPOSAL_ACCEPTED", proposal)

    if kind == "decompose":
        children = proposal.get("children")
        if not isinstance(children, list) or not children:
            return Verdict(index, kind, False, "ADVISOR_MALFORMED_PROPOSAL", proposal)
        for child in children:
            if not isinstance(child, dict) or not isinstance(child.get("work_item_id"), str):
                return Verdict(index, kind, False, "ADVISOR_MALFORMED_PROPOSAL", proposal)
            project = child.get("project_id")
            if project is not None and project not in facts.projects:
                return Verdict(index, kind, False, "ADVISOR_UNKNOWN_PROJECT", proposal)
            # A decomposition is a proposal for *work*, not for permission.  Any
            # child carrying its own admission is refused whole, because the
            # cheapest way to bypass a gate is to arrive already past it.
            for forbidden in ("execution_mode", "acceptance_gate_ids", "context_manifest_hash",
                              "idempotency_key", "gateway_policy", "advisor_policy"):
                if forbidden in child:
                    return Verdict(index, kind, False, "ADVISOR_ADMISSION_FIELD_FORBIDDEN",
                                   proposal, {"field": forbidden})
        return Verdict(index, kind, True, "ADVISOR_PROPOSAL_ACCEPTED", proposal)

    mission = proposal.get("mission_id")
    if mission not in facts.missions:
        return Verdict(index, kind, False, "ADVISOR_UNKNOWN_MISSION", proposal)
    # `next_mission` is a hint and nothing more: the scheduler is a pure
    # function of durable numbers and does not read it.  Accepting it records
    # that the advisor had an opinion and that the opinion changed no order.
    return Verdict(index, kind, True, "ADVISOR_PROPOSAL_ACCEPTED", proposal)


def consult(port: AdvisoryPort | None, request: dict[str, Any],
            policy: AdvisorPolicy, facts: Facts) -> Outcome:
    """Ask the advisor and adjudicate the answer.  Never raises."""

    if not policy.enabled:
        return Outcome("skipped", (), "ADVISOR_DISABLED")
    if port is None:
        return Outcome("absent", (), "ADVISOR_ABSENT")
    try:
        response = port.advise(dict(request))
    except Exception as exc:  # the advisor is untrusted; its failure is a fact
        return Outcome("unavailable", (), "ADVISOR_UNAVAILABLE",
                       {"error": type(exc).__name__, "detail": str(exc)[:200]})
    if response is None:
        return Outcome("silent", (), "ADVISOR_SILENT")
    if not isinstance(response, dict):
        return Outcome("malformed", (), "ADVISOR_MALFORMED_RESPONSE")
    verdicts = review(response.get("proposals"), policy, facts)
    status = "advised" if any(verdict.accepted for verdict in verdicts) else "rejected"
    return Outcome(status, verdicts, None if status == "advised" else "ADVISOR_ALL_REJECTED")



# --------------------------------------------------------------------------- #
# the operator step
# --------------------------------------------------------------------------- #

def coordinate(store, port: AdvisoryPort | None, policy: AdvisorPolicy | dict | None = None,
               *, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask an advisor, adjudicate the answer, apply what the Owner allowed.

    This lives here rather than on the Controller, and nothing in ``engine.py``
    imports this module -- ``tests/test_authority_boundaries.py`` enforces that.
    The Factory must run deterministically when no advisor exists, and the
    cheapest way to make that true rather than merely tested is for the
    scheduling path to have no way to reach an advisor at all.  Advice is an
    operator step that edits the graph between missions; the scheduler then
    reads the graph exactly as it does when nobody advised anything.

    Of the five proposal kinds only two can move durable state: a dependency
    edge, and a project priority inside the Owner's bounds.  The other three are
    recorded and change nothing, and each for a structural reason rather than a
    policy one.  A specialist profile cannot be applied because
    ``factory-bridge`` selects from its own registry and the Controller has no
    wire field naming a profile.  A decomposition cannot be applied because a
    child mission needs admission fields the advisor is forbidden to supply.
    ``next_mission`` cannot be applied because the scheduler is a pure function
    of two durable numbers and does not read it.  So the advisor's entire
    effective authority is two edges and one number.
    """

    policy = (AdvisorPolicy.from_payload({"advisor_policy": policy})
              if isinstance(policy, dict) else (policy or AdvisorPolicy()))
    projects = store.projects()
    missions = tuple(sorted(row["id"] for row in store.all_missions()))
    facts = Facts(projects=tuple(sorted(projects)), missions=missions,
                  edges={key: tuple(value) for key, value in store.dependency_graph().items()})
    outcome = consult(port, request or {"missions": list(missions), "projects": list(projects)},
                      policy, facts)
    applied, refused = [], []
    for verdict in outcome.accepted:
        try:
            applied.append(_apply(store, verdict, projects))
        except Exception as exc:
            # An accepted proposal can still lose to a fact that changed between
            # review and application -- a mission finished, an edge now closes a
            # cycle.  Review is not a lock, so the store's own refusal wins.
            refused.append({"index": verdict.index, "kind": verdict.kind,
                            "code": "ADVISOR_APPLICATION_REFUSED", "detail": str(exc)[:200]})
    row = {**outcome.as_row(), "applied": applied, "application_refused": refused}
    store.coordinate(None, None, "advisor", outcome.refusal_code or "ADVISOR_CONSULTED", row)
    return row


def _apply(store, verdict: Verdict, projects: Mapping[str, Any]) -> dict[str, Any]:
    proposal = verdict.proposal
    if verdict.kind == "dependency_edge":
        store.add_dependency(proposal["mission_id"], proposal["depends_on"],
                             on_failure=proposal.get("on_failure", "block"))
        return {"kind": verdict.kind, "effect": "edge_added", "proposal": proposal}
    if verdict.kind == "project_priority":
        current = projects[proposal["project_id"]]
        store.register_project(dataclasses.replace(current, priority=proposal["priority"]))
        return {"kind": verdict.kind, "effect": "priority_set",
                "from": current.priority, "to": proposal["priority"]}
    return {"kind": verdict.kind, "effect": "recorded_only", "proposal": proposal}

# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #

class StaticAdvisor:
    """A deterministic advisor.  Replays a fixed script, records what it saw."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def advise(self, request: dict[str, Any]) -> Any:
        self.requests.append(request)
        if not self.responses:
            return None
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def judge(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        body = self.advise(snapshot)
        if not isinstance(body, dict):
            raise ValueError("ADVISOR_MALFORMED_RESPONSE")
        reasoning = body.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("ADVISOR_REASONING_ABSENT")
        if "proposals" not in body:
            raise ValueError("ADVISOR_PROPOSALS_ABSENT")
        return body

    def observed_identity(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        reported = (body or {}).get("observed_identity") if isinstance(body, Mapping) else None
        observed = reported if isinstance(reported, dict) else {}
        return {
            "requested_profile": "scripted-advisor",
            "requested_effort": "recorded",
            "observed_profile": observed.get("profile", "scripted-advisor"),
            "observed_effort": observed.get("effort", "recorded"),
            "present": True,
            "credential_held": False,
        }


def runtime_session(explicit: str | None = None) -> str | None:
    """Use an operator-supplied session, or a local HTTP-session grant if present.

    Provider model keys in the same store are not an advisory HTTP session and
    are never returned.  The value is not written to durable Controller state.
    """

    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    path = Path.home() / ".hermes" / "auth.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for body in providers.values():
            found = _http_session_grant(body)
            if found:
                return found
    pool = raw.get("credential_pool")
    if isinstance(pool, dict):
        for entries in pool.values():
            if not isinstance(entries, list):
                continue
            for body in entries:
                found = _http_session_grant(body)
                if found:
                    return found
    return None


def _http_session_grant(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    if body.get("auth_type") not in ("session", "http_session"):
        return None
    base = body.get("base_url")
    if not isinstance(base, str) or (
            "127.0.0.1:9119" not in base and "localhost:9119" not in base):
        return None
    secret = body.get("session")
    if isinstance(secret, str) and secret.strip():
        return secret.strip()
    return None


def endpoint_advisor(base_url: str | None = None, *, token: str | None = None):
    """Build the HTTP advisory adapter without naming its vendor at the call site.

    The exemption that lets this file hold a vendor name is worth keeping narrow:
    `cli.py` and `engine.py` stay scannable because they ask for *an advisory
    endpoint*, and only this module knows which one is running here.
    """

    return HermesAdvisor(base_url or DEFAULT_ENDPOINT, token=token)


class HermesAdvisor:
    """The narrowest real adapter for the Hermes surface on this host.

    Hermes 0.19.0 is running here (``127.0.0.1:9119``) and its kanban
    orchestration plugin exposes exactly the advisory verbs this port names --
    ``/api/plugins/kanban/tasks/{id}/decompose``, ``/specify``, ``/reassign``,
    and ``/api/plugins/kanban/orchestration``.  It is not usable from the
    Controller as it stands: **every** ``/api`` route answers
    ``401 {"detail":"Unauthorized"}``, including the ones its own unauthenticated
    ``/api/status`` describes, and ``/api/status`` reports ``auth_required:
    false`` while gating them anyway.  So the missing thing is not an executable
    or an interface -- both exist and are measured below -- it is an Owner
    session credential, which stays outside Controller durable state.

    This adapter therefore does the half that is real today: it probes the
    unauthenticated ``/api/status`` to establish presence and version, and
    fails closed with ``ADVISOR_CREDENTIAL_ABSENT`` rather than inventing a
    Hermes runtime.  Given a token it POSTs one bounded request and returns the
    body for :func:`review` to adjudicate like any other advisor's -- Hermes
    gets no more authority than the deterministic fake does.
    """

    #: Measured on this host, 2026-08-26.
    PROBE_PATH = "/api/status"
    ORCHESTRATION_PATH = "/api/plugins/kanban/orchestration"
    ADVISORY_PATHS = ("/api/plugins/kanban/tasks/{task_id}/decompose",
                      "/api/plugins/kanban/tasks/{task_id}/specify",
                      "/api/plugins/kanban/tasks/{task_id}/reassign")

    def __init__(self, base_url: str = "http://127.0.0.1:9119", *, token: str | None = None,
                 timeout: float = 5.0, opener=urllib.request.urlopen) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = runtime_session(token)
        self.timeout = timeout
        self.opener = opener

    def probe(self) -> dict[str, Any]:
        """Presence, version, and whether a credential is held.  Read-only."""

        try:
            body = self._get(self.PROBE_PATH)
        except Exception as exc:
            return {"present": False, "reason": "ADVISOR_ENDPOINT_UNREACHABLE",
                    "error": type(exc).__name__, "base_url": self.base_url}
        return {"present": True, "base_url": self.base_url,
                "version": body.get("version"),
                "advertised_auth_required": body.get("auth_required"),
                "gateway_running": body.get("gateway_running"),
                "credential_held": self.token is not None,
                "orchestration_path": self.ORCHESTRATION_PATH}

    def advise(self, request: dict[str, Any]) -> Any:
        if self.token is None:
            raise PermissionError("ADVISOR_CREDENTIAL_ABSENT")
        return self._post(self.ORCHESTRATION_PATH, request)

    def judge(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Ask for a management judgment.  Presence/status is not this method.

        The body must carry a non-empty ``reasoning`` string plus ``proposals``.
        Probe-shaped answers (version, gateway_running, HTTP status) are
        refused here so a later plane cannot treat liveness as judgment.
        """

        if self.token is None:
            raise PermissionError("ADVISOR_CREDENTIAL_ABSENT")
        body = self._post(self.ORCHESTRATION_PATH, {"kind": "manage", "snapshot": snapshot})
        if not isinstance(body, dict):
            raise ValueError("ADVISOR_MALFORMED_RESPONSE")
        reasoning = body.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("ADVISOR_REASONING_ABSENT")
        if "proposals" not in body:
            raise ValueError("ADVISOR_PROPOSALS_ABSENT")
        return body

    def observed_identity(self, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """What this adapter can honestly say about who answered."""

        probe = self.probe()
        reported = (body or {}).get("observed_identity") if isinstance(body, Mapping) else None
        observed = reported if isinstance(reported, dict) else {}
        return {
            "requested_profile": "advisory-endpoint",
            "requested_effort": "unknown",
            "observed_profile": observed.get("profile", probe.get("version") or "unknown"),
            "observed_effort": observed.get("effort", "unknown"),
            "present": probe.get("present"),
            "credential_held": probe.get("credential_held"),
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        return headers

    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, headers=self._headers())
        with self.opener(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode()
        headers = {**self._headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise PermissionError("ADVISOR_HTTP_%d" % exc.code) from exc
