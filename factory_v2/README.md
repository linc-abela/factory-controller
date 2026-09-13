# Software Factory v2 Controller — runbook

Status: **bootstrap spine, not production-ready.**

This package is the deterministic Factory v2 Controller on branch `factory-v2`.
Cursor is a bootstrap tool only. Target architecture:

`Laboratory -> approved PCP -> Controller -> Nous Hermes -> Grok Build -> Antigravity review + QA -> Verified RC -> Owner Gate -> Antigravity Distribution`

## Run

From this repository (Python 3.11+, stdlib only):

```sh
python3 -m unittest tests.test_factory_v2_lifecycle
python3 -m factory_v2 admit-pcp path/to/approved-pcp.json
python3 -m factory_v2 --simulated admit-pcp path/to/approved-pcp.json
python3 -m factory_v2 tick msn-<pcp-hash-prefix>
python3 -m factory_v2 status msn-<pcp-hash-prefix>
python3 -m factory_v2 approve msn-<pcp-hash-prefix>
python3 -m factory_v2 reject msn-<pcp-hash-prefix> --reason "..."
python3 -m factory_v2 distribute msn-<pcp-hash-prefix>
```

Ledger default: `$FACTORY_V2_HOME/ledger.sqlite` (falls back to `~/.factory-v2`).
Sandboxes: `$FACTORY_V2_HOME/sandboxes/<mission_id>/`.

`./dev v2 …` and `./dev v2-test` wrap the same commands without Docker.

Default CLI adapters are **real** and fail closed. `--simulated` is a labeled test double, never silent success.

## States

`PCP_APPROVED -> ENGINEERING -> VERIFYING -> VERIFIED_RC -> OWNER_VALIDATION -> DISTRIBUTION_READY`

Rework:

- `VERIFYING` review or QA FAIL -> `ENGINEERING` (same mission, new candidate, full re-verify)
- `OWNER_VALIDATION` REJECT -> `ENGINEERING` (same mission, new candidate, full re-verify)
- Extra state `BLOCKED` is fail-closed (missing Hermes/Grok/Antigravity runtime or credentials)
- Extra state `DISTRIBUTED` is recorded after an immutable handoff

## Adapters (thin, replaceable)

| Contract | Target | Official interface |
|---|---|---|
| `EngineeringManager` | NousResearch Hermes Agent | `hermes chat --oneshot -Q --source tool --in <sandbox> --query-file …` |
| `EngineeringExecutor` | Grok Build | `grok -p … --cwd <sandbox> --output-format json --session-id <mission>` |
| `Verifier.review` / `Verifier.qa` | Antigravity (separate verdicts) | `antigravity review\|qa --artifact <id>` |
| `DistributionExecutor` | Antigravity Production | `antigravity distribute --profile production --artifact <id>` |

Lifecycle code in `factory_v2/machine.py` does not contain vendor CLI strings.
Hermes is not reimplemented (no session/memory/subagent/skills runtime here).

Grok auth (real): `XAI_API_KEY` or `GROK_DEPLOYMENT_KEY` or `~/.grok/auth.json`.
Absence is a truthful `BLOCKED` state, not a simulated PASS.

## Sandbox boundary

OS/workspace containment: each mission gets `sandboxes/<mission_id>/`. Real Hermes is launched with `--in` that directory. Real Grok is launched with `--cwd` there. The Controller does not pass Hermes `--yolo` or Grok `--always-approve`.

Prompt-level allow/deny rules are **not** the security boundary. They are advisory. The actual bound is the per-mission workspace directory plus host OS permissions on that tree. The general Hermes process is not granted unrestricted host authority by this adapter.

## Real vs simulated (this slice)

| Harness | Used in deterministic tests 111–124 |
|---|---|
| Controller / SQLite ledger | **real** in-process code |
| Nous Hermes CLI | **simulated** (`ScriptedHermes`), labeled `harness_mode=simulated` |
| Grok Build CLI | **simulated** except test 124, which uses the **real** `GrokBuildAdapter` with credentials stripped and asserts fail-closed `BLOCKED` |
| Antigravity review/QA/distribution | **simulated** (`ScriptedVerifier`, `ScriptedDistributor`) |

No production-readiness claim. A real PCP → verified RC run still needs working Hermes, Grok credentials, and Antigravity review + E2E both green.

## Out of scope (do not add here)

Watcher UI, Notion/AWE runtime, v1 cleanup, Kyriedachi, Profit Guard, benchmarking, rebuilding Hermes.
