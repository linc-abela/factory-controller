# Software Factory — Antigravity fixed dispatch

When the Owner says `Process your Queue.`:

1. Your first external/tool action MUST fetch this exact Notion page directly:
   - Antigravity Dispatch page ID: `3d3690f6-eb14-8193-b5d4-ccf1e81c08db`
   - URL: https://app.notion.com/p/3d3690f6eb148193b5d4ccf1e81c08db
2. Do not search Notion, GitHub, repository files, lifecycle folders, task history, or the workspace before that fetch.
3. If it says `NO_EXECUTABLE_TASK`, stop.
4. If it says `EXECUTABLE`, follow only its direct Task Page pointer.
5. If the pointer is stale or contradictory, return `DISPATCH_POINTER_STALE`; never hunt for another task.

Startup routing target: zero searches and at most two Notion fetches (Dispatch + directly pointed Task Page).
