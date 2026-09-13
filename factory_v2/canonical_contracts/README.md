# Factory v2 contracts

These contracts are the durable boundaries between the v2 lifecycle layers.
They are intentionally JSON Schema documents so a Controller or a replacement
implementation can validate them without depending on Hermes, Grok, Notion,
or an agent conversation.

## Contract set

| Contract | Direction | Purpose |
| --- | --- | --- |
| `pcp-handoff.schema.json` | Laboratory -> Engineering | The sole approved Product Candidate Package handoff. |
| `engineering-mission.schema.json` | Controller <-> Engineering | Durable mission, candidate, attempt, verification, and Owner-verdict state. |
| `verification.schema.json` | Verifier -> Engineering | Separate exact-candidate code/security and QA/E2E verdicts. |
| `verified-rc.schema.json` | Engineering -> Owner Gate 2 | The only candidate shape eligible for Owner validation. |
| `distribution-handoff.schema.json` | Engineering -> Distribution | The sole approved immutable RC release handoff. |

`common.schema.json` contains reusable definitions for consumers that want a
shared resolver. Each contract carries local `$defs` aliases and uses the
co-located common file through standard relative JSON Schema references.

## Identity rule

Every candidate identity is the tuple `(candidate_id, source_revision,
artifact_hash, artifact_uri)`. A verifier, Verified RC, and Distribution
handoff are valid only when every repeated identity field is byte-for-byte
equal. The schemas describe the fields; `validate_contracts.py` enforces the
cross-field equality and state-machine rules.

## Status rule

`PASS` means the exact candidate named in the record passed that particular
verdict. A failed verdict may carry blocking defects, but it can never make a
Verified RC. Code/security review and QA/regression/E2E remain separate even
when the same Antigravity installation performs both.

## Fixture gate

Run the deterministic schema and invariant gate from the repository root:

```text
python3 SOFTWARE-FACTORY-V2/contracts/validate_contracts.py
```

The manifest includes admissible records and deliberate invalid vectors for
missing Owner approval, a stale verifier candidate, and distribution artifact
substitution. A recorded verification failure is structurally valid when its
blocking defects are explicit, but it cannot produce a Verified RC.
