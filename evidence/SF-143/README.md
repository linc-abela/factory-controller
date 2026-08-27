# SF-143 — Phase-1 capacity control plane

What is here, and what each file is evidence *of*.

## `capacity-window-simulation.json`

Produced by `tests/test_capacity_windows.py` running against the real store,
the real scheduler and the real engine on a hand-wound clock. The simulated
part is only the harness's own quota accounting, which is the one fact the
Controller is not entitled to compute for itself.

Three harnesses, five-hour windows staggered by 1.5 hours, six missions of
allowance per window, one virtual day, five projects.

| Run | Completed | Lost | Duplicate irreversible effects |
|---|---|---|---|
| naive, one runtime, wait for reset | 30 | 0 | 0 |
| capacity-aware, three staggered windows | 40 (whole backlog) | 0 | 0 |
| capacity-aware, one runtime unavailable | 40 | 0 | 0 |

The first comparison clears its whole backlog, which flatters it. On a
saturated backlog of 200 missions, where neither run runs out of work:

| Run | Completed | Lost | Duplicate irreversible effects |
|---|---|---|---|
| naive, one runtime | 30 | 0 | 0 |
| capacity-aware | 102 | 0 | 0 |

**3.4x**, and that ratio is the capacity effect rather than the backlog's size.

## The two numbers worth reading twice

`lost` is `refused + failed + escalated`. It is zero in every run including the
naive one, because not losing a mission comes from the *refusal codes* and not
from the capacity record: a Factory that never registers a runtime still stops
throwing missions away when a window closes. What the capacity record buys on
top is not having made the doomed dispatch at all — measurable as the refused
legs the harness never had to answer (5 against 2 over six hours, one runtime).

`duplicate_irreversible_effects` counts provider invocations beyond one per
mission that actually ran. It is zero everywhere, and it is zero structurally:
a checkpoint taken where any leg failed to prove nothing started reports
`post_dispatch_unreconciled` and names exactly one compatible runtime — the one
that may already have run — so there is no second runtime a handoff could pick.
