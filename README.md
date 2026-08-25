# Factory Controller

Pure-standard-library, single-host Controller runtime, executed through the
repository container. SQLite is authoritative
for live mission state; Notion is not read by runtime code.

```sh
./dev --db controller.db submit --key mission:1 --file mission.json
./dev --db controller.db work-once --worker local-1
./dev --db controller.db status
./dev --db controller.db history MISSION_ID
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
