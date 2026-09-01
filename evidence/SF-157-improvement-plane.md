# SF-157 rework — the DF-4 improvement plane, wired

`evidence/SF-157.md` is the frozen record of the Phase-1 sweep and its first
execution proof. It is not edited here. This document records the one thing
that proof named and left: DF-4 completed its acceptance gates and never
entered the improvement plane, so three of the five things its own
`evidence_required` lists were not produced.

    In the validation ledger `experiments` is 0, `deployments` is 0, and the
    improvement policy is provisioned `enabled: false`. The `factory cycle`
    dogfood seam carries every slot through the ordinary mission pipeline; it
    never touches the improvement plane.
    -- evidence/SF-157.md, "DF-4: gates honest, improvement plane never reached"

## 1. What was actually missing

Two absences, one under the other.

**The improvement plane had no caller.** Stage 8 is complete and, by design,
inert: its own module docstring says there is no improvement process, nothing
in it polls or wakes up, and an experiment exists because somebody called
`admit_experiment`. In the dogfood run nobody did. `factory cycle` materializes
every frozen slot the same way — `dogfood_intake.build` then
`controller.submit` — and DF-4's `work_class: improvement` reached no different
code than DF-1's `backlog`.

**The slot had nothing to be about.** This is the one the first proof could not
have found by reading. A provider's only instruction is the repository's own
`MISSION.md`: every profile's argv begins *"Read MISSION.md first and treat it
as authoritative."* At `factory-prototype-lab` `229b923b` that file describes
M3, which is implemented — `dev check`, `dev test` and `dev evaluate` all exit 0
there, and the frozen portfolio's own rationale records it. So the first live
DF-4 dispatched a real provider against a repository with nothing left to do
and came back with an empty commit. `git diff 229b923b..ee816e5c` is empty.
That was honest and useless, and no amount of wiring inside the Controller
would have changed it: the instruction had no way to reach the provider.

Wiring the plane without closing the second absence would have produced a
different failure with the same shape — an experiment opened, a baseline
measured, and a candidate that changed nothing, refused
`IMPROVEMENT_CHANGE_SET_UNKNOWN` at sealing. Correct, and still no promotion.

## 2. The instruction channel

One bounded string, on a field the wire already carries.

`BridgeRequest.metadata` exists, is bounded to eight sorted pairs of at most 64
and 256 characters, and is covered by `request_digest` — so a brief travels
sealed and cannot be added to a frame in flight. It is *not* part of
`request_identity_hash`, which stays the eight admitted fields, so a briefed
mission keeps the idempotency key the whole stack binds it by.

| layer | commit | what it does |
|---|---|---|
| `factory-bridge` | `d589e227bbad39ec1309d46499461f0cb4202506` | `{mission_brief}` placeholder, read from request metadata, rendered into each profile's argv |
| `factory-evidence-core` | `8029a57c7aa824a3d8b7e25625ea43552be6c1d6` | `first_live --mission-brief`, carried to `build_bridge_request(metadata=...)` |
| `factory-controller` | `70b7f41da3047b49657f4829c394d755c0695eca` | the frozen objective's statement into `stage1.mission_brief` |

**The first version of this got it wrong, and the run caught it.** It
substituted only the brief's *text* into a fixed sentence, so every unbriefed
mission's prompt gained *"This mission's own brief is: none; MISSION.md is the
whole brief. Where the brief names work, that work is the mission..."* The claim
that unbriefed missions were unchanged was true of the wire frame and false of
the prompt, and it was measured changing one — see § 8.1.

The whole clause is now the substitution and the placeholder sits at the end of
the task string, so an absent brief renders the empty string. Proven rather than
argued: stripping `{mission_brief}` from each installed profile's argv gives
back the argv at `597bae73` exactly, for all three profiles.

Three properties are held by test rather than by intent. An absent brief is an
empty tuple and not an empty value, so every mission without one produces the
frame it produced before the field existed — re-derived and identical at
evidence-core `7c7b0659`: `ACCEPTED dffb93a7…`, `ENVELOPES 955d5ea1…`,
`FIRST LIVE DRY RUN 3192b2e2…`.

Two of those three are not the values the corpus quotes, and the difference is
not this change. `ENVELOPES 06a15454…` is pinned in five evidence documents up
to SF-134 and `FIRST LIVE DRY RUN 55c8ec15…` in three; both were superseded by
later hardening and neither is what the *untouched* baseline produces. Both
were re-derived here from a clean `git archive` extraction of `7c7b0659` before
and after, so the pair above is the current fact and the older pair should stop
being quoted unmarked.

An over-long brief is refused at the Controller
edge rather than truncated at the far end, because half an instruction is a
different instruction. And a request carrying no brief renders an explicit
absence into the prompt, because a dangling sentence is worse than a stated one.

## 3. The objective

`contracts/first-dogfood-improvement-objective.json`, an Owner act, digest
`9d627f7ee11905410865b5f5f5048ee89ec360225695d1fbc28ff49b53d3c170`; the
objective it declares digests to
`dc49284064c72b3d8e423119cbbc7350ff0ec526fec857542791225bc31da212`, which is
what the experiment pins.

It is deliberately not an accuracy objective. The lab's evaluator already
reports 5 of 5 correct with 0 false matches at the baseline, so an accuracy
metric could only ever read `not_improved`, and stating one would be asking the
Factory to prove something already true. What is genuinely absent is test
coverage of the matcher: two tests exist and both are about the evaluation
contract, neither about `lab.prototype.match`.

| metric | role | direction | bound | read from |
|---|---|---|---|---|
| `passing_tests` | objective | increase | `min_delta_ratio` 0.5 | `dev-test`, `unittest_ran` |
| `evaluate_correct` | non_regression | increase | `tolerance_ratio` 0.0 | `dev-evaluate`, `json_field correct` |
| `evaluate_false_matches` | non_regression | decrease | `tolerance_ratio` 0.0 | `dev-evaluate`, `json_field false_matches` |

The ceiling of the objective metric is that it counts tests rather than what
they assert. The two non-regression metrics are what make a test asserting
nothing worthless: the candidate must still link every label correctly and
introduce no false match, both read from the lab's own frozen decision
boundary. `evaluate_false_matches` has a zero baseline, where a relative
tolerance has no meaning, so the comparison falls back to the sign of the change
and any false match at all is a regression — the correct reading.

Every metric is read from a gate the mission already declares and runs. Nothing
in the Controller measures the lab by a command the project did not declare in
its own gate source, and a gate that produced no readable value is
`not_measurable`, which Stage 8 never reads as improvement.

## 4. What the seam does, and what it refuses to do

`factory_controller/dogfood_improvement.py`. The ordering is Stage 8's, not
this module's, and that is the anti-gaming property: `record_baseline` refuses
once a mission exists, `create_candidate_mission` refuses without a baseline.
So no caller can produce a candidate first and choose the number it is compared
against afterwards.

1. **Baseline.** The declared gates are run at the mission's pinned
   `baseline_sha`, in a detached worktree — never the registered checkout, so a
   checkout that had moved cannot be measured silently as if it had not, and
   the lab's own tree is untouched.
2. **Open.** Objective registered, generation 1 admitted, baseline recorded.
   Returns before any mission is submitted.
3. **Candidate.** The dogfood payload is submitted *through*
   `create_candidate_mission`, so the experiment binds the mission the
   portfolio admitted. The key the plane derives from that payload is checked
   against the key the seam derived — a silent divergence there would bind an
   experiment to a second identity.
4. **Settle.** Seal with the provider profile the route actually selected and
   the change set the candidate seam derived from git; compare against the
   pinned baseline with this seam as evaluator; stage the promotion through the
   Stage-6 ledger; close.

Nothing here is a second authority. The producer is the provider and the
evaluator is the seam, so `evaluate_candidate`'s independence check compares two
values written at different times by different callers. The promotion is
`admit_release`'s decision, which applies the same admission a person's release
gets and refuses a gated class outright. The Owner's shift grant is recorded
beside it as `FACTORY_IMPROVEMENT_SETTLED`, because DF-4 asks for the promotion
decision *with* its approval reference and neither plane holds that grant.

Nothing deploys. This lab declares no deployment target, and the only
`DeploymentPort` in the corpus reports that no environment was contacted.

## 5. Two wedges found while wiring it

Both are states the plane can reach on its own and neither had an exit.

**An experiment admitted but never baselined holds the project's one
concurrency slot forever.** `admit_experiment` succeeds and `record_baseline`
then refuses `IMPROVEMENT_BASELINE_NOT_MEASURABLE` — leaving an open experiment
that can never be sealed, so every later attempt refuses
`IMPROVEMENT_CONCURRENCY_EXCEEDED`. It now closes `abandoned` with its reason
and the refusal still reaches the caller.

**A refused attempt leaves an experiment holding a mission it can never seal.**
`experiment_reference` is derived from the objective, the generation and the
baseline, which is exactly right for a replay and exactly wrong for a retry.
A spent attempt is abandoned, and attempt N is admitted under an objective
suffixed `#N` — the same way an attempt already reaches a mission's identity
through `policy_identity`, and nowhere else. A generation is not used for this:
a generation means the parent was accepted and its candidate became the next
baseline, and a retry means neither.

## 6. One admitter

`_promote_experiments` in the supervisor promotes any experiment sitting in
`baseline_measured` with no mission, using `experiment_payload` — which builds
the experiment's own payload and knows nothing about the dogfood admission
document, the context manifest or the derived gate commands. Two admitters for
one slot is two mission identities, and a refused promotion attempt would also
land in `report.refused`, which `factory cycle` turns into Owner attention.

The supervisor's `improvement` work class is therefore narrowed off the dogfood
projects. Execution is untouched: `_advance` claims a mission whatever its
class.

## 7. Tests

| repository | at intake | now | tests |
|---|---|---|---|
| `factory-controller` | `c13dc650` | this commit | 1236 OK (was 1180) |
| `factory-bridge` | `597bae73` | `d589e227` | 313 OK (was 304) |
| `factory-evidence-core` | `7c7b0659` | `8029a57c` | 451 OK, 1 skipped (was 443) |
| `factory-context-broker` | `f144b48e` | `f144b48e` | unchanged |
| `factory-bug-lab` | `4072bfd7` | `4072bfd7` | frozen baseline |
| `factory-prototype-lab` | `229b923b` | `229b923b` | frozen baseline |

56 of the Controller's new tests are the seam's: 43 in
`tests/test_dogfood_improvement.py` and 13 in
`tests/test_factory_run.py::FactoryImprovementSlotTests`.

Three of them exist because a check that cannot fire is not a check.

* `test_an_unparseable_evaluator_reading_is_never_a_zero` runs the reader
  against five unreadable stdout shapes, because a regression that read as zero
  would be recorded as an improvement on a decreasing metric.
* `test_the_reader_uses_the_stream_each_tool_actually_writes_to` swaps the two
  streams and asserts both readings change, because `unittest` counts on
  standard error and the evaluator prints on standard output, and a reader that
  searched both indiscriminately would pass this file and read the wrong number
  from a real gate.
* `test_an_empty_candidate_cannot_be_sealed` is the first live DF-4, as a test:
  gates green, nothing changed, and `IMPROVEMENT_CHANGE_SET_UNKNOWN` rather
  than a promotion.

The measurement fixtures are the labs' own output, copied from a real run
rather than composed. `test_every_declared_gate_is_one_the_frozen_portfolio_runs`
pins the objective to the frozen portfolio's own improvement slot, and
`test_its_statement_fits_the_channel_that_carries_it` pins it to the bound the
bridge and Evidence Core both enforce -- so an objective the provider could
never be told fails here, as an editing mistake, rather than four cycles later
as a missing measurement.


## 8. The live proof

Both runs below carry the four frozen missions byte-identical under a validation
portfolio reference, against an isolated state directory and ledger, through the
real Bridge over its real unix socket, the real Codex provider, and the labs'
real containerised evaluators. Only the identities and the ledger are new.

### 8.1 The first run, and the defect it found in this change

`/tmp/sf157b-1`, bridge `f070cdf9`.

```plain text
install   FACTORY INSTALLED
start     FACTORY READY
cycle-1   DF-1 completed  (attempt 1)     DF-2 admitted
cycle-2   DF-2 completed  (attempt 1)     DF-3 admitted
cycle-3   DF-3 escalated  ACCEPTANCE_GATE_FAILED: dev-reproduce
```

DF-1 and DF-2 completed on one attempt each with their semantics intact —
DF-2's `dev-reproduce` recorded `passed=False, exit_code=1,
expected_failure=True, satisfied=True`, and its two passing gates recorded
separately. DF-3 then escalated, and the Factory was right to escalate it: the
candidate's own `dev-reproduce` gate exited 1.

**The provider's work was correct.** Candidate `1ef02e6d`, change set
`lab/bookings.py` and `tests/test_bookings.py`, root cause stated as *"batch
validation reused the initial seat count for every request"* — the same defect
SF-157's run found, diagnosed the same way, with a regression added. `dev-check`
and `dev-test` both exit 0 against it.

What it did not do is rewrite `lab/reproduce.py`. That script calls
`ledger.reserve_batch(requests)` uncaught, so once the fix starts refusing an
over-booked batch the reproduction crashes with a `ValueError` traceback and
exits 1. SF-157's run passed this gate only because that provider *also* chose
to rewrite the script; the bug lab's own `MISSION.md` never asks for it.

**The cause was this change.** The brief clause was rendered unconditionally, so
DF-3 — which carries no brief — was told *"This mission's own brief is: none;
MISSION.md is the whole brief. Where the brief names work, that work is the
mission; MISSION.md still bounds what this repository is for."* That sentence
emphasises MISSION.md as the bound, and MISSION.md lists the completion evidence
without mentioning the reproduction script. Fixed at `d589e227`: an absent brief
renders nothing, and the unbriefed prompt is byte-identical to `597bae73`'s for
all three profiles.

**Two pieces of the new wiring were verified by that run even though it
stopped.** The gate outcomes now carry both streams, and DF-1's `dev-test`
stderr ends exactly `Ran 2 tests in 0.001s` / `OK` — which the readers turn into
`passing_tests: 2` for the prototype lab and `3` for the bug lab, with
`evaluate_correct` and `evaluate_false_matches` reading `not_measurable` for
both because neither mission declares `dev-evaluate`. That is the typed absence
doing its job on live evidence rather than a zero. And DF-3's evaluation carried
`changed_paths: ["lab/bookings.py", "tests/test_bookings.py"]`, derived by the
candidate seam from git — so the change set the protected-surface check reads is
a real one.

**A second thing this exposed is not mine and is not fixed here.** The frozen
DF-3 declares `dev-reproduce` as a gate that must pass, and after a correct fix
that gate can only pass if `lab/reproduce.py` is also rewritten — which the
lab's `MISSION.md` does not ask for and DF-3's own `evidence_required` does not
list. So DF-3 passes or escalates on a choice the provider is never told to
make. SF-157's proof recorded a pass here; this run recorded an escalation; both
providers did the diagnostic work correctly. Changing the frozen portfolio or
the lab's reproduction script is outside this task's scope, and the honest
reading is that DF-3's gate expectation is under-specified rather than that
either provider failed.

### 8.2 The second run

`/tmp/sf157b-2`, bridge `d589e227`, exit 0.

```plain text
install   FACTORY INSTALLED
start     FACTORY READY
cycle-1   DF-1 completed  (attempt 1)     DF-2 admitted
cycle-2   DF-2 completed  (attempt 1)     DF-3 admitted
cycle-3   DF-3 completed  (attempt 1)     DF-4 admitted
cycle-4   DOGFOOD PORTFOLIO COMPLETE
```

Four missions, four distinct idempotency keys, one attempt each. DF-2's
`dev-reproduce` again recorded `passed=False, exit_code=1,
expected_failure=True, satisfied=True` beside its two passing gates. DF-3's
candidate `4f9392e2` changed `lab/bookings.py`, `lab/reproduce.py` and
`tests/test_bookings.py`, and all three of its gates exit 0 at the candidate --
the same provider, the same prompt as `597bae73`, and the opposite choice about
the reproduction script from run 8.1. That is the under-specification above,
observed from both sides.

**DF-4 is the only mission that carried a brief.** Read back off its stored
payload: `stage1.mission_brief` is present on DF-4 and absent on the other
three.

#### The improvement lineage

```plain text
experiment          imp_95d35bcdd3c769015dc05cae
objective           SF-144-DF-4-prototype-lab-quality-1
objective_digest    dc49284064c72b3d8e423119cbbc7350ff0ec526fec857542791225bc31da212
state               closed          disposition  accepted        verdict  improved
baseline            passing_tests 2   evaluate_correct 5   evaluate_false_matches 0
candidate           passing_tests 5   evaluate_correct 5   evaluate_false_matches 0
candidate_sha       dd9fcd92e41c533e0b2b60c169345e5db4e55040
change_set          lab/prototype.py, tests/test_prototype.py
producer_identity   provider:codex-primary
evaluator_identity  factory-controller/dogfood-improvement
promotion           dep_3c97464e24c24e56 -> factory-prototype-lab-staging (approved)
approval_ref        factory-owner-501-factory-shift-1
```

The provider's own summary of what it did: *"Hardened matching for
reordered/noisy labels and ambiguous ties. Added `tests/test_prototype.py`.
`dev check`, `dev test`, and `dev evaluate`: all exit 0. Evaluation: 5/5
correct, 0 false matches. `lab/evaluate.py` and `fixtures/` unchanged."*

#### DF-4's `evidence_required`, item by item

| required | produced |
|---|---|
| baseline measurement recorded before the candidate ran | `passing_tests 2`, `evaluate_correct 5`, `evaluate_false_matches 0`, state `baseline_measured` before `mission_created` |
| post-change measurement for the same metric | `passing_tests 5`, `evaluate_correct 5`, `evaluate_false_matches 0` — the same three metric ids |
| `dev-evaluate` exit 0 preserved at the fix commit | `dev-evaluate` `passed=True`, `exit_code=0`, `target: candidate`, `target_sha dd9fcd92` |
| the promotion decision with its approval reference | `dep_3c97464e24c24e56`, state `approved`, `release_admitted`, recorded with `factory-owner-501-factory-shift-1` |
| cost and context accounting | `cost_state: unknown` (Codex returns no usage — a typed absence, not a zero); context `built`, manifest `51ae89a8…`, 8079 bytes over 12 files, broker digest `b2d5343c…` |

Each of DF-4's four `stop_conditions` also held. `dev-evaluate` did not move off
0. Nothing was promoted before a baseline was recorded — Stage 8 refuses that
outright and the transition order shows it did not have to. No protected surface
was touched: the change set is the implementation and its tests. And the
candidate changed the implementation rather than the evaluator or the fixture,
which the change set states and the protected-surface check would have refused
either way.

#### Rollback boundaries

Both labs are byte-identical to their frozen baselines, locally and on the
remote, with clean trees. Every candidate exists only as
`refs/factory/lanes/<lane_id>` in the registered checkout, on no branch, and
`factory-bridge/src` contains no `git push` at all.

The demotion half was exercised against a copy of the validation ledger, so the
accepted promotion in the real one stays intact:

```plain text
before  reverted_to not_applicable   rollback_target 229b923b050f
revert  reverted_to 229b923b050f     == baseline_sha  == rollback_target
events  promotion_staged -> experiment_closed -> promotion_reverted
```

It is deterministic because the target was recorded at admission: reverting is a
lookup, never a new decision about what "before" meant. The other half of DF-4's
boundary -- deleting the branch after demoting -- is deleting that lane ref,
which is where the code exclusively lives; it was not executed here because that
ref *is* the recorded candidate.

#### Restart and reinstall persistence

Every cycle is a separate process on the same durable ledger. After completion,
a further `install`, `start` and cycle produced `DOGFOOD PORTFOLIO COMPLETE`
again, and the ledger still holds **4 missions, 4 distinct idempotency keys, 1
experiment, 1 deployment**. No duplicate mission, no duplicate experiment, no
settled mission regressed into attention, and the closed experiment stayed
closed.

#### Historical evidence

Every run in this task used an isolated state directory; the live ledger was
never opened for writing. Measured rather than asserted: its last event is
DF-3's own `SUBMITTED_ADMITTED` at `2026-09-01 04:05:39`, no row's `updated_at`
is later than that, and the file has been still for the eleven hours before this
task began and throughout it. `events` is append-only, still 34 rows, and its
digest `2a0fdcbaf3264519` is byte-identical to the value SF-157 recorded — so no
mission has changed state since. The dispositions SF-157 recorded are all
present: DF-1 `escalated` with `ACCEPTANCE_GATE_FAILED: dev-check, dev-test`,
three DF-2 attempts `refused`, DF-3 `admitted`.

**One discrepancy, reported rather than smoothed.** The `missions` and `steps`
digests are `416b8c19b81d02b4` (6 rows) and `802edc385d558961` (7 rows), and
SF-157 recorded `c6fad4651156b801` and `269813c11ff78ffb` for the same row
counts. The `events` digest matching exactly means the hashing method is the
same one, so those two tables genuinely differ from that snapshot. The
difference predates this task by eleven hours and no state transition can have
produced it, but this task took no baseline hash at intake and therefore cannot
say when or why they moved. Hashing the live ledger at intake is what SF-157
did and what this rework should have repeated.

## 9. Found and deliberately not fixed

**Every detached-worktree gate run shares one container image, across both
labs.** `stage1_adapter._candidate_worktree` and the baseline measurement both
materialize into a directory named `checkout`, so `docker compose` derives the
project name `checkout` and the image `checkout-lab:latest` for whichever lab
ran last:

```plain text
checkout-lab:latest                 2026-09-01 12:05:35
factory-bug-lab-lab:latest          2026-09-01 10:52:49
factory-prototype-lab-lab:latest    2026-09-01 10:42:20
```

It is harmless today and the reason is worth stating rather than assuming: both
Dockerfiles are byte-identical (`01f49c9de9f873b43f8558ee11aa8b3430f4c47fb68afd05fa2c11ff32c29b5e`
for each), pinned to one base image digest, and each lab's own source arrives
through the `.:/workspace` bind mount, which overrides the `COPY . .` layer. So
the effective code under test is always the worktree's. The hazard appears the
moment one lab's Dockerfile diverges: the second lab would then run its tests
inside the first lab's image and the gate would still exit 0.

Naming the worktree after its project fixes it in one line, in a file this
task's live proof is currently executing from. Changing it mid-proof would
leave the recorded run at a source state no head names, so it is recorded here
with its evidence instead.
