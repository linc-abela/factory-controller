# Software Factory v2 Controller — runbook

Status: **bootstrap spine, not production-ready.**

This package is the deterministic Factory v2 Controller on branch `factory-v2`.
Cursor is a bootstrap tool only. Target architecture:

`Laboratory -> approved PCP -> Controller -> Nous Hermes -> Grok Build -> Antigravity review + QA -> Verified RC -> Owner Gate -> Antigravity Distribution`

Canonical contracts consumed (not edited): SFV2-002
`factory-vault` PR #75 head `727882072a382f3146654d3fdb2b9b18bea19825`, snapshotted at
`factory_v2/canonical_contracts/`. Override with `FACTORY_V2_CONTRACTS_DIR`.

Internal states stay `PCP_APPROVED` … `DISTRIBUTED`. Emitted contracts use the
canonical names `ADMITTED`, `BUILDING`, `VERIFYING`, `REWORK_REQUIRED`,
`OWNER_REVIEW`, `DISTRIBUTION_READY`, `CLOSED`. See `factory_v2/canonical.py`.

## Run

From this repository (Python 3.11+, stdlib only):

```sh
python3 -m unittest tests.test_factory_v2_lifecycle tests.test_factory_v2_contracts
python3 -m factory_v2 admit-pcp path/to/canonical-pcp-handoff.json
python3 -m factory_v2 --simulated admit-pcp path/to/canonical-pcp-handoff.json
python3 -m factory_v2 tick msn-<pcp-hash-prefix>
python3 -m factory_v2 status msn-<pcp-hash-prefix>
python3 -m factory_v2 approve msn-<pcp-hash-prefix>
python3 -m factory_v2 reject msn-<pcp-hash-prefix> --reason "..."
python3 -m factory_v2 distribute msn-<pcp-hash-prefix>
```

A PCP must validate `pcp-handoff.schema.json` with Owner `APPROVE` evidence.
`{"title","intent"}` is not admitted.

Ledger default: `$FACTORY_V2_HOME/ledger.sqlite` (falls back to `~/.factory-v2`).
Sandboxes: `$FACTORY_V2_HOME/sandboxes/<mission_id>/`.

`./dev v2 …` and `./dev v2-test` wrap the same commands without Docker.

Default CLI adapters are **real** and fail closed. `--simulated` is a labeled test double, never silent success.

## States

`PCP_APPROVED -> ENGINEERING -> VERIFYING -> VERIFIED_RC -> OWNER_VALIDATION -> DISTRIBUTION_READY`

Rework:

- `VERIFYING` review or QA FAIL -> `ENGINEERING` (same mission/lineage, new candidate tuple, full re-verify)
- `OWNER_VALIDATION` REJECT -> `ENGINEERING` (same mission/lineage, new candidate tuple, full re-verify)
- Extra state `BLOCKED` is fail-closed (missing Hermes/Grok/Antigravity runtime or credentials)
- Extra state `DISTRIBUTED` is recorded after an immutable handoff

Candidate identity is always `(candidate_id, source_revision, artifact_hash, artifact_uri)`.

## Adapters (thin, replaceable)

| Contract | Target | Official interface |
|---|---|---|
| `EngineeringManager` | NousResearch Hermes Agent | `hermes chat --oneshot` in the admitted sandbox with the SFV2-002 `factory-engineering` profile. Hermes coordinates Grok via the official grok skill/terminal and writes `hermes-result.json`. Controller does not call Grok. |
| `EngineeringExecutor` | Grok Build | Invoked **by Hermes** (or a labeled simulated Hermes campaign): `grok --no-auto-update -p … --cwd <sandbox> --output-format json` |
| `Verifier.review` / `Verifier.qa` | Antigravity (separate verdicts) | `antigravity review\|qa` bound to the candidate tuple |
| `DistributionExecutor` | Antigravity Production | `antigravity distribute --profile production` bound to the approved tuple |

Lifecycle code in `factory_v2/machine.py` does not contain vendor CLI strings.
Hermes is not reimplemented (no session/memory/subagent/skills runtime here).

Grok auth (real): `XAI_API_KEY` or `GROK_DEPLOYMENT_KEY` or `~/.grok/auth.json`.
Absence is a truthful `BLOCKED` state, not a simulated PASS.

## Sandbox boundary

OS/workspace containment: each mission gets `sandboxes/<mission_id>/`. Real Hermes is launched with `--in` that directory. Real Grok is launched with `--cwd` there. The Controller does not pass Hermes `--yolo` or Grok `--always-approve`.

Prompt-level allow/deny rules are **not** the security boundary. They are advisory. The actual bound is the per-mission workspace directory plus host OS permissions on that tree. The general Hermes process is not granted unrestricted host authority by this adapter.

## Real vs simulated (this slice)

| Harness | Used in deterministic tests |
|---|---|
| Controller / SQLite ledger / schema validator | **real** in-process code |
| Nous Hermes CLI | **simulated** (`ScriptedHermes` campaign that still delegates to Grok), labeled `harness_mode=simulated`. Real `NousHermesAdapter` is fail-closed without a `hermes` binary and does not call Grok from Controller Python. |
| Grok Build CLI | **simulated** except lifecycle test 124, which uses the **real** `GrokBuildAdapter` with credentials stripped and asserts fail-closed `BLOCKED` |
| Antigravity review/QA/distribution | **simulated** (`ScriptedVerifier`, `ScriptedDistributor`) |
| SFV2-002 schemas/fixtures | **real** consumed snapshot of PR #75 `72788207…` |

No production-readiness claim. A real PCP → verified RC run still needs working Hermes, Grok credentials, and Antigravity review + E2E both green. SFV2-003/004 remain blocked until this head freezes.

## Out of scope (do not add here)

Watcher UI, Notion/AWE runtime, v1 cleanup, Kyriedachi, Profit Guard, benchmarking, rebuilding Hermes, editing Luna-owned `SOFTWARE-FACTORY-V2/*`.
