# Factory Controller

Pure-standard-library, single-host Controller runtime, executed through the
repository container. SQLite is authoritative
for live mission state; Notion is not read by runtime code.

```sh
./dev --db controller.db submit --key mission:1 --file mission.json
./dev --db controller.db work-once --worker local-1
./dev --db controller.db status
./dev --db controller.db history MISSION_ID
./dev --db controller.db route MISSION_ID
./dev --db controller.db telemetry MISSION_ID
./dev --db controller.db context MISSION_ID
./dev --db controller.db economics [--corpus CORPUS_IDENTITY]
./dev --db harness --missions 10
./dev test
```

The default adapter is a token-free safe local process. Supply `--adapter` with
a JSON process that composes the frozen admission/bridge/verification/Evidence
Core seams. Each request contains `step`, deterministic `operation_key`, and
`input`; bridge/evidence implementations must replay an operation key
idempotently. A genuine provider remains an explicit operator choice.

`python -m factory_controller.stage1_adapter` is the supplied Stage-1 runner.
Its mission payload names Evidence Core's public `first-live` command, working
directory, output, admission fixture, and target repository. `mode: real`
refuses unless `operator_opt_in: true`; the Controller itself never selects a
provider or invents verification/evidence results.

Lifecycle follows the landed `factory-controller/1.0` seam: `admitted ->
dispatching -> dispatched -> candidate_verified -> evaluated -> evidence_sealed
-> completed`, with `refused`, `failed`, `cancelled`, and human `escalated`
outcomes. Retry exhaustion becomes an explicit escalation.
The append-only event ledger is the history; the mission row is its operational
projection. Claims use `BEGIN IMMEDIATE`, expiring leases, and fencing tokens.
Started external steps survive restart and reuse the same operation key.

## Providers

The Controller requests a capability and records which provider served it. It
holds no provider CLI, SDK, credential, or availability logic; a *profile* is an
opaque string the execution layer minted, and `tests/test_authority_boundaries.py`
enforces that by reading this package's own AST.

A mission may declare candidate profiles and an Owner policy over them:

```json
{
  "work_item_id": "SF-1",
  "execution_mode": "real",
  "context_manifest_hash": "<64 hex>",
  "acceptance_gate_ids": ["GATE-1"],
  "provider_candidates": [{"profile": "alpha", "capabilities": ["implement"]},
                          {"profile": "beta"}],
  "execution_policy": {"allowed_profiles": [], "denied_profiles": [],
                       "required_capability": "implement", "no_fallback": false,
                       "max_route_legs": 3,
                       "budget_ceiling": 25.0, "budget_currency": "USD"}
}
```

Selection is deterministic: the first candidate the policy admits, in declared
order. A mission that declares no candidates keeps the Stage-2 behaviour, with
the layer choosing; that still records one route leg.

### The side-effect boundary

Fallback is legal only while nothing can have run. The execution layer answers a
leg with `provider_unavailable` plus a receipt; the Controller falls back only
when that receipt carries `process_started: false`. A receipt that does not say
is treated as "may have run", and the mission fails closed with
`PROVIDER_SWITCH_AFTER_SIDE_EFFECT` rather than handing the same work to a
second provider. After dispatch, restart recovers the existing result by
idempotency key on the same profile and never re-selects.

### Budgets and usage

`budget_ceiling` is a hard ceiling on *measured* spend, checked before every new
dispatch. Unknown provider cost stays `unknown` -- one of Evidence Core's four
canonical absence words -- and is never estimated, never zeroed, and never
counted toward the ceiling. A leg priced in another currency is not converted;
it fails the next dispatch closed with `MISSION_BUDGET_CURRENCY_MISMATCH`.

Provider figures are reported claims (`evidence_class: reported_claim`). They
never decide candidate validity: Git and Evidence Core remain authoritative.

### Real missions

A mission is a fixture mission unless it declares `execution_mode: real`, and the
Controller compares the declared mode against the mode the layer reports. The
check is an equality in both directions, so a dry-run result cannot complete a
real mission and a real run cannot be filed as a fixture. A real mission must
additionally:

* carry `idempotency_key` equal to `work_item_id:context_manifest_hash`, which is
  the only value `factory-evidence-core` will bind (`IDEMPOTENCY_BINDING_MISMATCH`
  otherwise), so the key reaching `factory-bridge` is the Controller's own;
* declare its `acceptance_gate_ids`, every one of which must be executed and pass
  before evidence is sealed -- an evaluator naming some other gate does not count;
* receive a receipt echoing that same key, or the mission refuses.

### Reconciliation with factory-bridge

Read against `factory-bridge` `c9787d5`. The bridge selects from its own
priority-ordered registry, and `BridgeRequest` carries no field naming a
requested profile, so the Controller cannot steer the choice -- it enforces the
Owner's allow/deny list against the profile that actually ran
(`PROVIDER_POLICY_VIOLATION`). Receipt field names are the bridge's own:
`provider_profile`, `provider`, `selection_trace`.

Two drifts are held by `tests/test_bridge_reconciliation.py` rather than
described in a document:

* `ADAPTER_UNAVAILABLE` is raised from twelve sites in the bridge, at least four
  of them after the provider process ran, so no client can safely re-route on
  it. The Controller does not.
* the bridge's `selection_trace` is not on a refusal frame, so the route
  explanation is missing on exactly the path that needs it. The Controller
  records the trace when given one and records its absence otherwise.

`./dev route MISSION_ID` explains which provider ran, why, what else was
considered, whether a fallback occurred, where the boundary was crossed, and why
any later switch was refused. `./dev telemetry MISSION_ID` is the Stage-4 seam:
provider, elapsed time, fallback count, retries, reported usage and cost, Owner
intervention, and context references -- measured values only.

## Context

A mission may declare what repository context it is entitled to. The Controller
never opens, ranks, or scores a repository file: it states the entitlement,
hands it to a Context Broker, and checks the answer against it.

```json
{
  "context_request": {
    "corpus_identity": "repo://factory-prototype-lab@8155d65...",
    "policy_identity": "SF-136:STAGE-4-CONTEXT",
    "required_anchors": ["MISSION.md"],
    "allowed_paths": [], "denied_paths": [],
    "max_age_seconds": 900
  },
  "context_budget": {"max_bytes": 200000, "max_files": 40,
                     "max_reported_input_tokens": 200000}
}
```

The manifest is a durable memoized step. A restart after dispatch reuses the
manifest the mission ran on and the broker is never asked again; freshness is
only evaluated before that boundary. For a real mission the idempotency key is
already `work_item_id:context_manifest_hash`, so the same work against a
different manifest is a different mission identity rather than a replay.

`ContextManifest` and its digest rule are `factory-evidence-core`'s
(`src/contracts/mvp.py`, `src/evidence/validation.py`), reproduced here and
pinned by a test, so a manifest the Controller admits is one Evidence Core
admits. Measured bytes and files are exact or explicitly absent; nothing here
converts bytes into tokens.

`python -m factory_controller.context_adapter` is the supplied reconciliation
adapter for `factory-context-broker`. It translates between the two dialects,
selects nothing itself, and delegates every non-context step to the safe local
provider:

```sh
FACTORY_CONTEXT_BROKER_COMMAND="python3 -m factory_context_broker.cli" \
FACTORY_CONTEXT_BROKER_REPO=/path/to/target-repo \
FACTORY_CONTEXT_BROKER_CACHE=/path/to/cache \
./dev --adapter "python3 -m factory_controller.context_adapter" work-once --worker w1
```

The broker resolves context at the mission's own `baseline_sha` and refuses a
manifest whose head is not the checkout's `HEAD`, so the target checkout must be
at that commit.
