# SF-263 — Autonomous Cross-Lane Continuation Activation

**Status:** `SF263_AUTONOMOUS_CONTINUATION_READY`

**Repository:** `factory-controller`

**Base:** `76645ff1a3daec96739e142affcae158e6af8ce3`

**Implementation head:** `64c4f0baf66969a5cdbc09b390e99301a45974b1`

## Root cause

The AWE worker already had a claim ledger, Notion task source, dispatch
adapters, cadence coordinator, and a bounded scheduler, but the live path was
not wired as a continuation service:

- the scheduler required one manually supplied slot and could not observe all
  fixed harness Dispatch pages;
- the live resolver only understood the canonical pointer format, while the
  current Dispatch projection used a legacy executable assignment and split
  `Lane` plus `Model / Effort` fields;
- missing Notion authentication could silently select the fixture directory;
- protected Owner-only work disappeared as idle instead of reaching the
  escalation gate;
- Cursor's authenticated wake path returned success without starting its CLI;
- runtime status exposed only basic liveness, not observation, service,
  per-harness wake, no-work, projection, or Owner-gated state;
- there was no safe install/start/stop/restart wrapper for exactly one
  foreground continuation process.

## Change and acceptance mapping

| Acceptance | Implemented evidence |
| --- | --- |
| A — live source and fixed Dispatch wiring | `LiveNotionTaskSource` remains the source of record; `LiveDispatchSlotProvider` reads only the three fixed Dispatch page IDs and resolves all complete pointers. No workspace search or fixture fallback is used in live mode. |
| B — exact slots and fences | Dispatch resolution requires task-page URL, physical ancestry, expected lifecycle, exact profile, and harness match before a slot is emitted. Existing SQLite claim, projection preflight, and lease fencing remain on the worker path. Duplicate exact slots are recorded as stale/ambiguous. |
| C — cross-lane continuation | `AWEContinuationSupervisor` runs every observed exact slot in one poll and performs one bounded immediate re-observation after a `DONE` settlement. It observes and wakes only; it does not activate downstream work or merge lanes. |
| D — Cursor fail-closed behavior | Cursor reports `HARNESS_WAKE_PATH_UNAVAILABLE:cursor` when its binary or login path is unavailable. An authenticated CLI wake uses `cursor agent -p "Process your Queue."` and reports subprocess failure truthfully. |
| E — Owner protection | Owner-only Queue/In Progress items are surfaced as escalation/blocked work, never woken. The worker's normal wake prompt is still `Process your Queue.` only for an admitted non-Owner task; no Owner command is used as a normal dispatch path. |
| F — lifecycle safety | `ContinuationService` owns one manifest, one PID receipt, and one foreground supervisor process. Install is idempotent and does not start a process or persist credentials; duplicate start is refused; stop and restart use the owned PID and durable liveness record. |
| G — observability and recovery | Durable `awe_worker_runtime` records service state, observation/cycle times, exact slots, last claim, per-harness wake receipts, work state, errors, and last summary. CLI `supervisor status` and heartbeat output expose it; existing liveness and crash recovery remain intact. |

## Verification

All project commands ran in the configured repository container.

```text
./dev test
Ran 1758 tests in 207.789s
OK (skipped=6)

docker compose run --rm controller python -m unittest \
  tests.test_awe_continuation tests.test_awe_worker \
  tests.test_projection_diagnostic tests.test_supervisor_activation
Ran 127 tests in 1.020s
OK
```

Additional bounded checks:

- targeted Python compilation passed for the changed AWE modules;
- unauthenticated live mode completed one bounded poll with zero cycles,
  durable `work_state: projection_refused`, and no claim or wake;
- service install regression proves idempotence, no PID/process start, and no
  Notion token in the manifest;
- multi-slot regression proves two slots run in one poll and a settled task
  causes one downstream re-observation;
- owner-gated and Cursor wake regressions prove no false wake and the explicit
  unavailable code.

## Operations and remaining Owner action

The schema is additive and created with `CREATE TABLE IF NOT EXISTS`; no
destructive migration or existing claim/liveness rewrite is required. The
documented lifecycle is:

```text
./dev awe-worker --db awe_worker.db supervisor install --repo "$PWD"
./dev awe-worker --db awe_worker.db supervisor install --repo "$PWD" --apply
./dev awe-worker --db awe_worker.db supervisor start
./dev awe-worker --db awe_worker.db supervisor status
./dev awe-worker --db awe_worker.db supervisor stop
./dev awe-worker --db awe_worker.db supervisor restart
```

`NOTION_TOKEN` or `NOTION_API_KEY` is read at process start only; it is not
written to the manifest or evidence. Cursor requires the one-time interactive
CLI authentication and status check described in the README. This task did not
start a long-lived service or claim live work. The branch is ready for review;
Factory integration and downstream activation remain outside this change.
