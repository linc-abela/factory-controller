"""The admitted current runtime tuple used by the selected Factory path.

Manager/Bridge pinning stays deterministic.  The pin is this admitted tuple,
not a leftover candidate SHA from an earlier Bridge PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "factory.controller.admitted_runtime_tuple.v1"
TUPLE_NAME = "admitted-runtime-tuple.json"
REQUIRED = (
    "controller_main",
    "bridge_main",
    "evidence_core_main",
    "context_broker_main",
)


class RuntimeTupleRefusal(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def default_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / TUPLE_NAME


def load(path: str | Path | None = None) -> dict[str, str]:
    target = Path(path) if path is not None else default_path()
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeTupleRefusal(
            "ADMITTED_RUNTIME_UNAVAILABLE",
            "the admitted runtime tuple could not be read: %s" % exc,
        ) from exc
    if not isinstance(body, Mapping) or body.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeTupleRefusal(
            "ADMITTED_RUNTIME_INVALID",
            "the admitted runtime tuple schema_version must be %s" % SCHEMA_VERSION,
        )
    out: dict[str, str] = {}
    for name in REQUIRED:
        value = body.get(name)
        if not isinstance(value, str) or len(value) != 40:
            raise RuntimeTupleRefusal(
                "ADMITTED_RUNTIME_INVALID",
                "admitted runtime field %s is missing or not a 40-character SHA" % name,
            )
        out[name] = value
    return out


def admitted_bridge_sha(path: str | Path | None = None) -> str:
    return load(path)["bridge_main"]


def as_row(body: Mapping[str, Any] | None = None) -> dict[str, str]:
    return dict(body or load())
