"""Software Factory v2 Controller/runtime spine.

Deterministic Controller authority. Thin harness adapters. No Notion runtime.
"""

from factory_v2.canonical import ContractError, load_pcp
from factory_v2.machine import Controller, GateError, InvariantError
from factory_v2.models import CandidateIdentity, MissionSnapshot
from factory_v2.states import MissionState

__all__ = [
    "CandidateIdentity",
    "ContractError",
    "Controller",
    "GateError",
    "InvariantError",
    "MissionSnapshot",
    "MissionState",
    "load_pcp",
]
