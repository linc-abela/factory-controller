from __future__ import annotations

import os

from factory_v2.adapters.cursor_cli import CursorCLIExecutor
from factory_v2.adapters.grok_build import GrokBuildAdapter
from factory_v2.contracts import EngineeringExecutor

SUPPORTED = ("cursor", "grok")
DEFAULT = "cursor"


def selected_executor_kind(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    raw = (source.get("FACTORY_V2_ENGINEERING_EXECUTOR") or DEFAULT).strip().lower()
    if raw not in SUPPORTED:
        raise ValueError(
            f"unsupported FACTORY_V2_ENGINEERING_EXECUTOR={raw!r}; "
            f"supported: {', '.join(SUPPORTED)}"
        )
    return raw


def build_executor(
    kind: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> EngineeringExecutor:
    chosen = kind or selected_executor_kind(env)
    if chosen == "cursor":
        return CursorCLIExecutor(env=env)
    return GrokBuildAdapter(env=env)
