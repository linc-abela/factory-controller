# SF-267 — Secure Notion Credential Bootstrap

Return: `SF267_SECURE_NOTION_BOOTSTRAP_READY`

Task: [SF-267](https://app.notion.com/p/3d9690f6eb1481faacbae49a6396d3a1)
Repository: `linc-abela/factory-controller`
Base: `main@e6593b3811b6a2bf3608a2f919c26ed6aa82004d`
Branch: `sf/SF-267/notion-service-credential-bootstrap`
Head: `0dcde46f9b24bf34cc3e5bf8c128b4a78706794e`
PR: https://github.com/linc-abela/factory-controller/pull/9

## Root cause confirmed on host

SF-263's continuation supervisor correctly refuses fixture fallback, but the
installed long-lived process only inherited `NOTION_TOKEN` / `NOTION_API_KEY`
from the parent environment at `Popen`. The live service on this host was
started without either variable.

Observed live failure (existing service log, no secret present):

- `work_state`: `projection_refused`
- `last_error`: `cursor: NOTION_NOT_CONFIGURED; codex: NOTION_NOT_CONFIGURED; antigravity: NOTION_NOT_CONFIGURED`
- `cycles_run`: 0
- `last_claimed_task`: null

The installed command in `.awe-worker/awe-continuation-1.service.json` contains
no token and no `--notion-token` flag. Restart after the original shell exits
therefore still starts an unauthenticated child.

## Credential sources inspected (names/types only)

| Source | Result |
| --- | --- |
| Process env `NOTION_TOKEN` | UNSET |
| Process env `NOTION_API_KEY` | UNSET |
| Factory Keychain `factory-controller.notion` / `awe-continuation` | missing (`security` rc=44) |
| Keychain `factory-controller` / `notion` | missing |
| Keychain `Notion` / `factory` | missing |
| `com.softwarefactory.supervisor` launchd `EnvironmentVariables` | PATH + Context Broker only; no Notion token key |
| `com.softwarefactory.hermes` launchd | unrelated dashboard session token; not used |
| `com.softwarefactory.bridge` launchd | bridge paths only; no Notion token key |
| `~/.factory-controller` receipts/runtime JSON | no Notion token keys |
| Cursor/ChatGPT Notion MCP connector | present in the coding session; **not** treated as a host runtime credential |
| Continuation service log | no `NOTION_TOKEN=`, `Bearer `, or `ntn_` material |

No approved persistent Factory Notion credential existed on the host. A new
competing secret subsystem was not added on top of a working one.

## Chosen provider

`env_then_keychain`:

1. Explicit CLI argument (interactive/debug only; refused in the service command).
2. `NOTION_TOKEN` or `NOTION_API_KEY` in the child environment (non-persistent).
3. Factory macOS Keychain item `factory-controller.notion` / `awe-continuation`,
   read inside the child via Security.framework (`ctypes`), never via
   `security -w` (which would place the password on argv).

Missing, unreadable, or malformed Keychain entries fail closed as
`NOTION_NOT_CONFIGURED`, `KEYCHAIN_UNREADABLE`, or `KEYCHAIN_MALFORMED`.
Live Notion observation still uses `LiveNotionTaskSource` with no fixture
directory fallback.

## Changed files

- `awe_worker/credentials.py` (new)
- `awe_worker/notion.py`
- `awe_worker/service.py`
- `awe_worker/cli.py`
- `tests/test_awe_credentials.py` (new)
- `tests/test_awe_continuation.py`
- `tests/test_awe_worker.py`
- `README.md`

## Tests

```sh
python3 -m unittest tests.test_awe_credentials tests.test_awe_continuation \
  tests.test_awe_worker tests.test_authority_boundaries
```

Result: OK (credential suite + 91 existing continuation/worker/authority tests).

Coverage mapped to the packet:

1. Env credential works without persistence — pass
2. Keychain works across a fresh process (Darwin) — pass
3. Missing credential fails closed with zero claims/wakes — pass
4. Manifest contains no credential value — pass
5. Logs/status/evidence contain no credential value — pass
6. Argv/process command contains no credential value — pass (`--notion-token` refused)
7. Malformed/missing secure-store entry fails closed — pass
8. Provider cannot silently fall back to fixture/directory mode — pass
9. Duplicate service fencing/idempotency remains green — pass
10. Existing SF-263 continuation tests remain green — pass

## Bounded live proof

No approved host Notion credential exists, so live Dispatch/AWE observation was
**not** fabricated.

From this branch:

```
./dev awe-worker supervisor credentials-status
```

returned `configured: false`, `code: NOTION_NOT_CONFIGURED`,
`provider: env_then_keychain`, `source: none`. Exit status 2. No secret in the
payload. SF-262 was not woken or moved.

## Single minimal Owner action

Unavoidable one-time host setup (do not paste the token into Notion or git):

```sh
printf '%s' "$NOTION_TOKEN" | ./dev awe-worker supervisor credentials-set
./dev awe-worker supervisor credentials-status
./dev awe-worker --db awe_worker.db supervisor restart
```

`credentials-set` reads stdin or a hidden prompt and never echoes the token.
After that, unattended start/restart resolve the Factory Keychain item inside
the child without depending on the original shell.

## Freeze for review

Do not merge `main`. Independent security/invariant review:
**Antigravity Gemini 3.8 / High**.
