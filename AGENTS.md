# Software Factory agent bootstrap

This file is intentionally small. It exists so coding agents do not spend time discovering their Agent Work Exchange assignment.

## `Process your Queue.` — mandatory first action

When the Owner says exactly `Process your Queue.` (or an equivalent instruction to process the Factory queue):

1. Identify only the harness you are currently running in: **Cursor**, **Codex**, or **Antigravity**.
2. **Before any Notion search, GitHub search, repository inspection, filesystem search, lifecycle-folder scan, task-history read, or planning**, fetch the fixed Dispatch page for that harness directly:
   - Cursor — page ID `3d3690f6-eb14-8178-b51c-f1baa2fd4d32` — https://app.notion.com/p/3d3690f6eb148178b51cf1baa2fd4d32
   - Codex — page ID `3d3690f6-eb14-8100-984b-d66fb476fe81` — https://app.notion.com/p/3d3690f6eb148100984bd66fb476fe81
   - Antigravity — page ID `3d3690f6-eb14-8193-b5d4-ccf1e81c08db` — https://app.notion.com/p/3d3690f6eb148193b5d4ccf1e81c08db
3. If Dispatch says `NO_EXECUTABLE_TASK`, stop. Do not search for work elsewhere.
4. If Dispatch says `EXECUTABLE`, follow only its direct Task Page pointer and verify the stated physical lifecycle/profile. Do not discover a task by workspace search.
5. If Dispatch disagrees with the pointed task, report `DISPATCH_POINTER_STALE` and stop. Never hunt for a replacement task.

### Startup performance gate

Task routing must be constant-time: the Dispatch fetch is the first external/tool call, with **zero workspace searches before it**. Normal task discovery must require at most the fixed Dispatch fetch plus the directly pointed Task Page fetch.

After the task is resolved, follow the canonical Factory skills and task packet for execution, authority, evidence, PR, containment, and lifecycle rules.
