# SF-145 — first-dogfood activation adjudication

Produced 2026-08-28 against `factory-controller` `062ca5e` plus this task's own
changes. The adjudication itself is `factory-vault`
`active/software-factory/implementation/factory-first-dogfood-activation-adjudication.md`.

## `bridge-doctor-2026-08-28.json`

`./dev doctor` run from a **clean clone of `factory-bridge` checked out at
`01867e6bdf04533fd510c7c562145fc2a59fc398`**, not from the shared working tree.
That matters here: a sibling was holding uncommitted changes to `projects.json`,
`dev`, `cli.py` and `install.py` in the shared checkout while this was taken, so
a doctor read from that tree describes a state matching no SHA. The first
capture in this task did exactly that and reported
`registry_drift: installed digest … differs from source digest …` and two
registered projects; the pinned capture reports `registry_drift: none` and one.

| Fact | Value |
|---|---|
| `source.sha` | `01867e6bdf04533fd510c7c562145fc2a59fc398` |
| `source.installed_sha` | `a914db5f3ad6579937585a8ce7623f773f90a9b9` |
| `compatibility.status` | `incompatible`, `fail_closed: true` |
| `dual_service` | `refused` — `com.astral.bridge` is loaded beside the Factory bridge |
| `capabilities` | `["prototype"]` |
| `capability_admissions.present` | `false` |
| `registry.projects` | `factory-prototype-lab` only, `registry_drift: none` |
| readiness | `codex-primary` `available`; `cursor-secondary`, `claude-secondary` `auth_required` |

## `gate-real-host-unprovisioned.json`

The Dogfood Activation Gate against that doctor and an empty Controller store.

**15 met / 8 unmet / 14 unknown, `ready: false`,
digest `94fa51c0fa0d9909c74d9f8727ff2a2f00de2ca79923241b77ff35d4ad6e40ae`.**

Before this task's run-contract correction the same reading was **14 / 9 / 14**,
digest `268434171e60c0818d0e4d94b3a0c45cce0ec87d38bb917039cd0d97f94675dd`.
Exactly one row moved — `REQUIRED_PROVIDER_READINESS`, from `unmet` to `met` —
because the required profile set narrowed to `codex-primary`, the only profile
this host can measure as ready.

Fourteen `unknown` rows are not fourteen near-passes. They are facts nobody
supplied: no evidence-core report, no context-broker report, no capacity
readings, no reachability reading, no execution-layer registry, no capability
preview, and a store with no projects in it. A required check in `unknown`
leaves the gate `ready: false`.

Of the eight `unmet` rows, four are Controller-store facts that provisioning to
the run contract closes with no Owner act — `PROJECTS_REGISTERED`,
`ACCEPTANCE_GATES_DECLARED`, `SUPERVISOR_POLICIES`,
`PROTECTED_SURFACES_DECLARED` — and four are host facts only the Owner can
close: `BRIDGE_COMPATIBILITY`, `BRIDGE_SOURCE_COMPATIBLE`,
`PROVIDER_CAPABILITIES_ADMITTED`, `SUPERVISOR_SERVICE_INSTALLED`. That split is
the whole of § 6 of the adjudication, measured rather than argued.

## What is not here

No host service was installed, loaded or unloaded. No provider was
authenticated. No capability was admitted. No grant was written and no mission
was dispatched. The bridge is `incompatible` and fail-closed on this host, which
is the correct state for it until the Owner reinstalls it.
