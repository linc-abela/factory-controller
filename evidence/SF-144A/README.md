# SF-144A — Crash-safe on-demand shift runtime

This evidence describes the Controller-owned runtime for an on-demand shift.
The activation and governance plane is separate; this runtime only projects
durable mission facts, drains resumable work, and prepares a cold-start resume.
No host service, provider credential, real mission, or target repository is
used here.

## Contract

`factory-controller/shift-runtime/1.0` emits an immutable,
content-addressed `factory.controller.shift_checkpoint.v1` checkpoint. Its
required fields are:

`mission_id`, `project_id`, `work_item_id`, `repository`, `baseline_sha`,
`candidate_sha`, `mission_state`, `recovery_class`, `completed_steps`,
`next_safe_step`, `safe_boundary`, `resume_target`, `idempotency_key`,
`operation_keys`, `step_states`, `context`, `evidence`,
`capacity_observation`, `repository_pin`, `runtime`, `lane`, `work_baton`,
`uncertainty`, `unresolved_blockers`, and `source_updated_at`.

The boundary vocabulary is shared with the existing capacity and continuity
contracts:

| Boundary | Effect | Resume action |
|---|---|---|
| `pre_dispatch` | `none` | `resume_next_step` |
| `post_dispatch_reconciled` | `reconciled` | `resume_next_step` |
| `post_dispatch_unreconciled` | `unknown` | `reconcile_uncertain_dispatch` |

An uncertain dispatch records the selected profile and operation key before the
provider call, then resumes with the same profile and `recover_only: true`.
A proven no-process-started refusal remains safely reroutable after a capacity
reset. A corrupted step, context reference, or baton is surfaced as
`repair_required`; it is never treated as clean work.

## Measured verification

The Controller container produced these results from the exact SF-144A baseline
`7e77384aa2d87814c29bbf90660f21dac873488a`:

| Proof | Result |
|---|---:|
| SF-144A shift-runtime tests | 9 / 9 passed |
| capacity control plane | 66 / 66 passed |
| Stage 9 supervisor and long horizon | 111 / 111 passed |
| complete Controller suite | 1,075 / 1,075 passed |
| duplicate irreversible effects in the virtual-time capacity proof | 0 |

The shift tests cover deterministic checkpoints, bounded suspend with zero
fresh claims, stale-lease recovery, same-runtime uncertain dispatch, fresh
capacity after reset, project isolation, selective context/baton references,
tamper detection, and repair-required durable corruption.

No transcript or conversational state is included in a checkpoint or resume
package. Resume preview is observational and does not claim work.
