# SF-144 — Owner-gated internal dogfood activation

What is here, and what each file is evidence *of*. Everything was produced on
2026-08-28 against `factory-controller` `892c6f1` plus the follow-up commit that
adds the dispatchability checks, using the real host wherever the fact was a
host fact.

## `bridge-doctor-2026-08-28.json`

`factory-bridge/./dev doctor` at bridge repo `01867e6`, captured verbatim. It is
the source of every host claim below, and it disagrees with the corpus in four
places:

| Fact | Value |
|---|---|
| `compatibility.status` | `incompatible`, `fail_closed: true` |
| `source.installed_sha` | `a914db5f3ad6579937585a8ce7623f773f90a9b9` |
| `source.sha` | `01867e6bdf04533fd510c7c562145fc2a59fc398` |
| `schema_drift` | `incompatible` (installed `providers.json` is v2, repo is v3) |
| `capabilities` | `["prototype"]` only |
| `capability_admissions.present` | `false`, `admitted_capabilities: []` |
| `registry.projects` | `factory-prototype-lab` only |
| `codex-primary` readiness | `available`, probe exited 0 |
| `cursor-secondary` readiness | `auth_required`, probe exited 1 |
| `claude-secondary` readiness | `auth_required`, probe exited 1 |

## `gate-real-host-unprovisioned.json`

The composed Dogfood Activation Gate against the real bridge doctor and an
empty Controller store.

**17 met / 11 unmet / 9 unknown, `ready: false`.**

## `gate-real-host-provisioned.json`

The same gate after the Controller store is provisioned to the run contract —
both projects registered with their declared gates and budgets, supervisor
policies, improvement policies with all eight mandatory protected surfaces, and
capacity policies carrying the readings the bridge doctor actually reported.

**28 met / 7 unmet / 2 unknown, `ready: false`.**

The number that matters is not 28. It is that **all nine remaining rows are
host facts and none is a Controller fact**: everything the Controller owns can
be provisioned to `met` without any Owner act, and everything that is left is
something only the Owner can do to the host. That is scope 5 — host preparation
is separate from mission authority — measured rather than asserted.

| Remaining | Owner act it names |
|---|---|
| `BRIDGE_COMPATIBILITY`, `BRIDGE_SOURCE_COMPATIBLE` | reinstall the bridge from `01867e6` |
| `PROVIDER_CAPABILITIES_ADMITTED`, `CAPABILITY_PREVIEW_COMPATIBLE` | admit the `bug` capability |
| `PORTFOLIO_PROJECTS_DISPATCHABLE` | register `factory-bug-lab` in the bridge registry |
| `REQUIRED_PROVIDER_READINESS` | authenticate `claude-secondary`, or drop it from the run contract |
| `SUPERVISOR_SERVICE_INSTALLED`, `SUPERVISOR_SERVICE_NO_DRIFT` | install the supervisor service definition |
| `PORTFOLIO_SOURCES_FETCHABLE` | push `factory-bug-lab` `961a4c97` to its remote |

## `owner-activation-traces.json`

Nineteen command outputs from the full Owner flow, run against a store
provisioned as above and a **fixture** bridge doctor derived from the real one
by moving exactly those measured blockers to their resolved value. The fixture
is labelled as one inside the file (`fixture_note`) and exists because the real
host cannot reach `ready` without Owner acts this task is forbidden from
performing.

On that fixture host the gate reads **37 met / 0 unmet / 0 unknown**, digest
`a18a01c9bab91ea9…`.

| Trace | What it shows |
|---|---|
| `preview` | every blocker and the exact effects, with nothing written |
| `apply-unapproved` | `SHIFT_UNAPPROVED` — a green gate is not a decision |
| `apply` | one grant, 4 missions, 25.00 USD, a 14400-second window |
| `apply-again` | `created: false`, byte-identical grant — idempotent |
| `status-active` | `active`, no drain reasons, eligible `codex-primary` |
| `admit` | `DF-1` offered, and only `DF-1` |
| `brief` | the eleven Owner answers and the next act |
| `status-exhausted` | one exhausted runtime ⇒ `off`, `CAPACITY_EXHAUSTED_NO_ELIGIBLE_RUNTIME` |
| `status-restored` | the reset restores eligibility ⇒ `active` again |
| `suspend-noref` | `SHIFT_RESUME_REF_REQUIRED` |
| `suspend` / `resume` | parked and unparked; `expires_at` unchanged |
| `revoke` | `OWNER_STOP`, reason recorded |
| `status-post-revoke-reset` | **the same capacity reset now leaves it `off`** |
| `apply-after-revoke` | `SHIFT_GRANT_REVOKED` — reversible means a new decision |
| `events` | `granted, suspended, resumed, revoked`, append-only |

The pair `status-restored` / `status-post-revoke-reset` is the whole capacity
rule in two rows: an identical capacity reset revives a shift that was only
narrowed, and cannot revive one the Owner ended.

## What is not here

No real dogfood mission ran. No host service was loaded, installed or unloaded
outside the scratch directory this evidence was produced in, no capability was
widened, and no repository was mutated. The bridge is `incompatible` and
fail-closed on this host, which is the correct state for it to be in until the
Owner reinstalls it.
