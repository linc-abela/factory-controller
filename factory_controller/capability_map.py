"""Canonical Capability Mapping as the single Hermes routing policy source.

Runtime code classifies work as a capability, then reads eligible execution
profiles from FACTORY/roadmap/phase-2-agent-capability-mapping.md (or an
injected parse of that document). Model names and fallback policy live in
the mapping, not in golden-path orchestration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

MAP_RELPATH = Path("FACTORY/roadmap/phase-2-agent-capability-mapping.md")
CAP_ARCHITECTURE = "architecture / technical design"
CAP_IMPLEMENTATION = "developer fleet"

_CELL = re.compile(r"[*_`]+")
_EFFORT = re.compile(r"\b(low|medium|high|max|xhigh)\b", re.I)
_HARNESS = {
    "cursor": "cursor",
    "codex": "codex",
    "antigravity": "antigravity",
}


@dataclass(frozen=True)
class Profile:
    capability: str
    role: str
    harness: str
    model: str
    effort: str
    purpose: str
    quota_continuity: bool

    @property
    def key(self) -> str:
        return "%s/%s/%s" % (self.harness, self.model, self.effort)

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "capability": self.capability,
            "role": self.role,
            "harness": self.harness,
            "model": self.model,
            "effort": self.effort,
            "purpose": self.purpose,
            "quota_continuity": self.quota_continuity,
        }


@dataclass(frozen=True)
class CapabilityMap:
    profiles: tuple[Profile, ...]
    source: str = str(MAP_RELPATH)

    def for_capability(self, capability: str) -> tuple[Profile, ...]:
        needle = capability.strip().lower()
        return tuple(
            profile for profile in self.profiles
            if profile.capability == needle or profile.capability.startswith(needle)
        )


def load(vault_root: str | Path) -> CapabilityMap:
    path = Path(vault_root) / MAP_RELPATH
    if not path.is_file():
        raise FileNotFoundError("CAPABILITY_MAP_MISSING: %s" % path)
    parsed = parse(path.read_text(encoding="utf-8"))
    return CapabilityMap(parsed.profiles, source=str(MAP_RELPATH))


def parse(text: str) -> CapabilityMap:
    profiles: list[Profile] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|") and "capability" in line.lower() and "runner" in line.lower():
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("|---") or line.startswith("| --"):
            continue
        if not line.startswith("|"):
            break
        cells = [_cell(part) for part in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        capability, runner, model, effort_cell = cells[:4]
        purpose = cells[4] if len(cells) > 4 else ""
        harness = _HARNESS.get(runner.lower())
        effort = _effort(effort_cell)
        if not harness or not model or not effort:
            continue
        cap = capability.lower()
        if "continuity fallback" in cap:
            role = "continuity_fallback"
        elif "primary" in cap:
            role = "primary"
        else:
            role = "member"
        policy = ("%s %s" % (effort_cell, purpose)).lower()
        profiles.append(Profile(
            capability=_capability_key(cap),
            role=role,
            harness=harness,
            model=_ident(model),
            effort=effort,
            purpose=purpose,
            quota_continuity=(
                role == "continuity_fallback"
                or ("quota" in policy and "exhaust" in policy)
            ),
        ))
    return CapabilityMap(tuple(profiles))


def _capability_key(capability: str) -> str:
    for prefix in (CAP_ARCHITECTURE, CAP_IMPLEMENTATION):
        if capability.startswith(prefix):
            return prefix
    return capability


def _effort(cell: str) -> str:
    match = _EFFORT.search(cell)
    if match:
        return match.group(1).lower()
    if "runtime" in cell.lower():
        return "runtime"
    return ""


def _cell(value: str) -> str:
    return _CELL.sub("", value).strip()


def _ident(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().lower())


def profile_keys(profiles: Sequence[Profile]) -> tuple[str, ...]:
    return tuple(profile.key for profile in profiles)
