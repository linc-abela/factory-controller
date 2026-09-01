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
./dev --db controller.db project register --id P --repository repo://P [--priority N]
./dev --db controller.db project state --id P --state paused|draining|enabled|stopped
./dev --db controller.db portfolio [--concurrency N] [--aging S] [--emergency-stop|--resume]
./dev --db controller.db depend MISSION --on PREREQUISITE [--on-failure block|cancel|ignore]
./dev --db controller.db deps MISSION
./dev --db controller.db schedule
./dev --db controller.db coordination [MISSION]
./dev --db controller.db portfolio-economics [--project P]
./dev --db controller.db advise --probe | --proposals FILE [--policy FILE]
./dev --db harness --missions 10
./dev --db controller.db supervisor status | brief | cycle --worker host
./dev --db controller.db shift-runtime status
./dev --db controller.db shift-runtime resume-preview
./dev test
./dev stage9
```

### Owner lifecycle

The supported local Factory workflow is intentionally five commands. They are
host-facing because Bridge and the supervisor use native macOS service
integration; provider work remains contained by Bridge and verification stays
in the repository containers.

```text
./dev factory install
./dev factory start
./dev factory run
./dev factory status --watch [--interval SECONDS]
./dev factory stop
./dev factory status
```

`install` validates and repairs the canonical Bridge, bootstraps its service,
and writes the supervisor definition while leaving the Factory off. `start`
refreshes the required primary runtime and capacity facts, previews the
first-dogfood capability and bounded shift, then applies both only when every
gate is met. `run` submits the next mission of the frozen first-dogfood
portfolio and no more than one: which mission is next is the portfolio's own
serial rule read from durable state, and the mission's identity, live admission
document, provider candidates and per-gate commands are all derived from the
frozen contract, the frozen portfolio and Bridge's project registry, so the
Owner names nothing. The installed Factory supervisor refreshes capacity,
advances one existing mission, and hands off the next frozen mission after a
settlement; repeating `run` while a mission is in flight reports it rather than
submitting a second. `stop` revokes admission, checkpoints and drains resumable
work, then unloads the Factory supervisor while leaving a healthy Bridge loaded.
Repeating any command is safe. `status` is a one-shot read-only snapshot;
`status --watch` repeats that observation every 30 seconds by default (or the
supplied `--interval`) and Ctrl+C stops only the observer. Normal output is
`FACTORY INSTALLED`, `FACTORY READY`, `FACTORY OFF`, `DOGFOOD MISSION QUEUED`,
`DOGFOOD MISSION RUNNING`, or one actionable `BLOCKED: ...` line.

The default adapter is a token-free safe local process. Supply `--adapter` with
a JSON process that composes the frozen admission/bridge/verification/Evidence
Core seams. Each request contains `step`, deterministic `operation_key`, and
`input`; bridge/evidence implementations must replay an operation key
idempotently. A genuine provider remains an explicit operator choice.

`python -m factory_controller.stage1_adapter` is the supplied Stage-1 runner.
Its mission payload names Evidence Core's public `first-live` command, working
directory, output, admission fixture, and target repository. `mode: real`
refuses unless `operator_opt_in: true`; the Controller itself never selects a
provider or invents verification/evidence results. A mission that declares no
`stage1` configuration is served by the token-free fixture instead, which is
what lets the supervisor service name this one adapter for both kinds of work.
`./dev factory run` writes the admission document a dogfood mission names to
`~/.factory-controller/dogfood/`.

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

### Capacity observations and Work Batons

`factory_controller.capacity` records provider-neutral, sourced capacity facts.
Managed runtimes treat stale, unknown, and unmeasurable observations as no
positive capacity.  Its plan only denies profiles from a mission's already
declared set, so it cannot add a metered fallback or become a second scheduler.

`factory_controller.continuity` serializes a Work Baton at either a
`pre_dispatch` or explicitly reconciled post-dispatch boundary.  Its immutable
identity binds repository source and head, project, run, lane, worktree,
branch, idempotency key, capabilities, compatible profiles, capacity,
evaluator, and effect uncertainty.  The SQLite issue/consume ledger is
restart-safe and exactly-once.  Operators can read its compact report with:

```sh
./dev baton inspect [--baton-id wb_...]
```

This is handoff plumbing for the capacity control plane; it cannot widen a
capability, select a metered fallback, or act as a scheduler.

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
    "overview": ["authoritative", "runtime", "execution", "tests"],
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

The installed first-dogfood service carries the checked-in Broker command and a
Factory-owned cache directory in its service environment. The intake preflights
that same command against the registered project checkout, and the Stage-1
adapter binds the context step to the mission's registered checkout again. A
missing Broker, wrong checkout, stale head, or refused overview blocks the
mission before provider dispatch; there is no synthetic repository fallback on
the real path.

## The portfolio

Stage 4 asked whether one mission may proceed.  Stage 5 adds the second
question -- *which* one, across projects competing for one host -- and
`portfolio.py` is the whole of the answer.  It dispatches nothing, opens
nothing, and talks to nothing; it is a decision over durable rows.

A project is an identity, a repository binding, a state, a priority, a
concurrency cap, and optional spending and context ceilings, under a named Owner
policy version.  A mission with no `project_id` keeps the Stage-4 meaning: it is
scheduled under portfolio limits alone.

```sh
./dev --db c.db project register --id urgent --repository repo://urgent \
      --priority 1 --cap 1 --budget 25 --currency USD --policy-version SF-137:v1
./dev --db c.db depend fm_child --on fm_parent
./dev --db c.db schedule          # what the scheduler would pick, and why not the rest
```

### Ordering and fairness

Priority is an ordering position: lower runs first, compared and never
multiplied.  Fairness is unbounded ageing -- a mission's effective position
improves by one step for every `aging_seconds` it has waited, without limit --
so for any pair of priorities there is a finite wait after which the lower one
wins.  Permanent starvation is therefore impossible rather than unlikely, and
because the rule is a pure function of two durable numbers, two workers reading
the same database reach the same answer.

Scheduling happens *inside* the claiming transaction.  The Stage-2 guarantee
that no two workers claim one mission is inherited rather than re-implemented,
and a second scheduler could not produce a duplicate claim if one existed.

### Dependencies

Edges point from a mission to what it waits for.  There is no `blocked` mission
state: ready/waiting/blocked is derived from the edges every time it is asked,
so the reading cannot drift away from the graph.  A cycle is refused at
declaration with the path that would have closed it.  Release is guarded on
`released_at IS NULL` inside the transaction, so an edge releases exactly once.

Only `completed` satisfies a prerequisite; a cancelled mission produced no
artifact either.  Each edge declares `block` (the default, and a pure
derivation that writes nothing), `cancel`, or `ignore`.

### Pause, drain, and emergency stop

`paused`, `draining` and `stopped` all stop *new* claims and differ only in
declared intent, which is honest: `drained` is the one place they diverge.
Portfolio-wide emergency stop is one boolean checked ahead of every other gate.

None of them touch a mission row, and all of them still allow a mission past the
dispatch boundary to be resumed -- orphaning a provider run that already had
effects is the durable-state corruption a stop exists to prevent, not the cure.

### Advisory coordination

`advisor.py` holds the port intended for Hermes.  An advisor may propose; it
may never decide.  `coordinate()` lives there and nothing in `engine.py`
imports it, which is enforced by `tests/test_authority_boundaries.py`: the
scheduling path has no way to reach an advisor, so "the Factory schedules
deterministically without one" is structural rather than tested.

Of five proposal kinds, two can move durable state -- a dependency edge and a
project priority inside the Owner's bounds.  The others are recorded and change
nothing, each for a structural reason: the bridge selects profiles from its own
registry, a child mission needs admission fields an advisor may not supply, and
the scheduler is a pure function that does not read a hint.

Hermes 0.19.0 is running on this host and its kanban orchestration plugin
exposes the advisory verbs this port names.  It is not usable from here: every
`/api` route answers `401`, while its own unauthenticated `/api/status` reports
`auth_required: false`.  The missing thing is an Owner session credential, which
stays outside Controller durable state.  `./dev advise --probe` reports presence
without consulting anything.

## The model gateway

`gateway.py` admits an OpenRouter-backed execution profile.  A gateway supplies
inference; a bounded execution adapter performs admitted repository actions --
the gateway never reaches a filesystem, a shell, a Git admission, evidence, or
a deployment.

An admitted gateway profile is an ordinary candidate placed after the direct
harnesses in declared order, so "prefer a direct harness, fall back to the
gateway before dispatch" is the existing selector plus the existing side-effect
boundary, not a second rule.  `openrouter/auto` is never an implicit default: an
implicit model cannot be allowlisted, priced, or reproduced.  Cross-model
fallback must be declared by Factory policy, and a reported model outside the
allowlist and the declared fallback order fails the mission
(`GATEWAY_UNDECLARED_MODEL_SUBSTITUTION`) whoever performed the substitution.

`may_reroute` adds the one gate the side-effect boundary lacked.  A refusal code
that *names an unknowable outcome* -- a timeout, a malformed body -- loses to
the layer's `process_started: false` claim, because a request that timed out may
have been served.

Gateway receipts carry the gateway, requested and actual model, actual provider,
generation identity, reported tokens, exact cost with its currency, retries and
declared fallbacks.  Anything the gateway did not report stays one of Evidence
Core's four canonical absence words: never `0`, never an estimate, and never an
echo of the request -- an echoed `actual_model` would hide a failover entirely.

Disabling or removing the gateway configuration does not break direct-harness
operation, and the same logical mission runs either way without a change to the
mission or evidence contracts.

## Portfolio economics

`./dev portfolio-economics` combines two fact classes that are never blended:
measured context bytes from the broker, and priced provider spend from receipts.
A leg's cost is counted once -- the provider-neutral `usage` figure is preferred
and a gateway's own priced figure is used only where `usage` reported none, since
they describe the same money.  Unpriced legs are counted and contribute nothing.
Two currencies are reported side by side and never converted.

## Recursive improvement

Stage 8 is a separate contract from Stage 7 because a repair and an improvement
are different acts.  A repair restores an intended behaviour that already
existed.  An improvement changes what "intended" means, so it has to be measured
against a baseline pinned before it ran, by an identity that did not produce it.

```sh
./dev --db controller.db improvement policy --project P --file policy.json
./dev --db controller.db improvement objective --file objective.json
./dev --db controller.db improvement admit --objective OBJ --source OBJ \
    --repository repo://P --baseline-sha SHA --isolation lane://P/1
./dev --db controller.db improvement baseline --experiment IMP --file baseline.json
./dev --db controller.db improvement mission --experiment IMP --gate G
./dev --db controller.db improvement seal --experiment IMP \
    --producer NAME --path src/a.py --path tests/test_a.py
./dev --db controller.db improvement evaluate --experiment IMP \
    --evaluator OTHER --file candidate.json
./dev --db controller.db improvement promote --experiment IMP \
    --bundle bundle.json --environment P-staging
./dev --db controller.db improvement close --experiment IMP --disposition accepted
./dev --db controller.db improvement generation --parent IMP \
    --baseline-sha CANDIDATE --isolation lane://P/2
./dev --db controller.db improvement lineage --experiment IMP
./dev --db controller.db improvement generations --lineage IMP
```

There is no `run` verb, and nothing polls: generation N+1 exists because
somebody asked for it, never because generation N finished.

A policy declares the whole envelope, and the two safety-critical parts of it --
the protected surfaces and the frozen metrics -- are read from files rather than
squeezed onto flags, so the two hardest things to get right are not the two
hardest things to review:

```json
{
  "enabled": true,
  "improvement_classes": ["performance", "cost", "reliability"],
  "trigger_classes": ["owner_objective", "maintenance_history"],
  "environment_classes": ["local-sim", "staging"],
  "protected_surfaces": {
    "governance": ["standards/", "agents/"],
    "production_authority": ["factory_controller/production.py"],
    "admission_integrity": ["factory_controller/store.py"],
    "evaluator_independence": ["tests/test_authority_boundaries.py"],
    "improvement_policy": ["factory_controller/improvement.py"],
    "secret_handling": [".env", "secrets/"],
    "emergency_stop": ["factory_controller/portfolio.py"],
    "release_authority": [".github/", "dev"]
  },
  "self_target_repositories": ["repo://factory-controller"],
  "generation_ceiling": 2,
  "experiment_budget": 2,
  "concurrent_experiments": 1,
  "risk_class": "low",
  "policy_version": "sf140-1"
}
```

Every name in `MANDATORY_SURFACES` must be present *and* cover at least one path
prefix, so a policy that leaves a surface unprotected is not stored rather than
being stored and checked later.  A policy may add surfaces; it may not drop one.

An objective is the only container in Stage 8 that holds a human sentence, and
registering one is an explicit Owner act.  Its digest is pinned into every
experiment admitted under it, so revising the objective ends that lineage
instead of retargeting a running experiment: metrics frozen before execution
cannot be revised after.

A candidate reaches a verdict only through a comparison it cannot influence.  It
must pass its own ordinary acceptance gates, then clear each objective metric's
declared relative threshold, and a regression on any non-regression metric ends
it whatever the objective metrics did.  An unmeasurable reading is
`not_measurable` and is never read as improvement.

Promotion goes through the Stage-6 ledger, so autonomous improvement inherits
emergency stop, drain, concurrency and the gated-class refusal rather than
restating any of them.  A self-target experiment -- the Factory improving itself
-- is refused a promotion outright: an accepted self-improvement candidate is a
commit in an isolated lane and an evidence record, and installing it is an Owner
act with no representation in this package.

## Always-on operation

Stage 9 is the caller every plane above was missing.  Stages 2 to 8 each end at
a verb an operator invokes, and nothing in the package invoked any of them; the
supervisor does, and the whole design question is how much of a caller it is
allowed to be.

**One bounded cycle per invocation.**  `supervisor cycle` performs a finite
amount of work and returns.  It never sleeps, never loops on a constant and
never calls itself, and all three are checked structurally rather than by
running it and hoping -- a runaway is the failure a timed test would not catch.
A host scheduler may invoke it repeatedly; installing that scheduler is an Owner
act, and `supervisor service` prints the exact step without taking it.

```sh
./dev --db controller.db supervisor policy --project P \
    --missions-per-cycle 2 --class backlog --class maintenance \
    --window-start 8 --window-end 20
./dev --db controller.db supervisor start --reason "night shift"
./dev --db controller.db supervisor cycle --worker host-1
./dev --db controller.db supervisor pause|resume|drain|stop --reason WHY
./dev --db controller.db supervisor emergency-stop --reason WHY
./dev --db controller.db supervisor hold --project P
./dev --db controller.db supervisor brief
./dev --db controller.db supervisor cycles [--limit N]
./dev --db controller.db supervisor selections [--cycle CYC]
./dev --db controller.db supervisor service [--interval-seconds 300]
```

Seven absences carry the design, and each is an enforcement rather than a
decision deferred: no supervisor process, no second mission runtime, no way to
create work, no approval verb, no drain of its own, no emergency stop of its
own, and no project hold of its own.  The last three are the Stage-5 primitives
the scheduler, maintenance and improvement already honour -- a supervisor-local
stop would have been a second stop the other planes never read.

A cycle can promote exactly two things, and both were admitted by an earlier
stage against a durable fact: a repair whose production failure this ledger
recorded, and an experiment whose baseline was pinned before any candidate
existed.  It cannot admit either.  It calls three methods on the maintenance
plane, two on the improvement plane, and none at all on the production ledger,
which is pinned by an AST walk in `tests/test_stage9_supervisor.py` -- so "the
supervisor cannot deploy" is an absent verb rather than a refused one.

Control state is durable and restart-safe: `stopped`, `running`, `paused`,
`draining`, `emergency_stopped`.  A drain is `resume_only` on the existing
claim, narrowing candidates inside the transaction the scheduler already runs
in, so there is no window in which a drain could still start something new.
Every other bound -- a closed execution window, a hold, a disabled policy, a
suppression -- narrows the same claim by project, with a resume exempt ahead of
it because half-finished work has to finish.

Overlapping invocations refuse rather than queue.  An abandoned cycle is settled
on the next claim as `recovered_replayable` or `recovered_uncertain`, and while
any mission sits past the dispatch boundary the next cycle advances work but
promotes none: finishing what may already have run comes before opening anything
new.

Repeated *infrastructure* failure -- a provider that is not there, a broker that
cannot be read -- suppresses one project for a declared window and then
escalates it to wait for the Owner.  A spent budget is deliberately not
infrastructure: it is a policy fact that will still be true next cycle, and
backing off from it would hide an Owner decision behind a timer.

`tests/test_stage9_long_horizon.py` runs 72 virtual hours over four projects
with outages, budget pressure, incidents, a failed staging deployment, a quiet
window, an Owner pause and a host restart.  It is a fixture rather than a soak
test: the same 288 cycles in the same order every time, and a failure names a
virtual hour.  `evidence/SF-141/long_horizon_summary.py` re-derives its numbers
from durable state after the run.
