"""The work-source seam: packets of Factory-maintenance work, as data.

Stage 9 cannot create work.  It only advances durable rows some earlier stage
admitted.  This module is that earlier stage's *input* contract: a work packet
is a structured object a directory adapter (or any other adapter behind the
same protocol) can produce.  The chat work exchange is one such adapter in the
Owner's world; it is not a type in this package.

Four shapes are deliberately absent.

There is **no prompt field**.  A packet names identities, a sequence, an
owner-only flag, and a mission payload ``Controller.validate`` already
accepts.  A sentence from a model has nowhere to go.

There is **no claim here**.  Exclusive claim, admission and Done/Blocked
detection live on the intake plane's SQLite ledger, so two adapters pointing
at the same files cannot double-execute by racing on the filesystem.

There is **no vendor or harness name**.  Capability routing is a later seam.
A packet that needs a person says so with ``owner_only``; it does not name
who should run it.

There is **no production verb**.  A packet that would promote, bill, disclose
a credential or rewrite history is marked owner-only.  The intake plane
refuses to submit it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .store import canonical_json, payload_hash


CONTRACT_VERSION = "factory-controller/work-source/1.0"
PACKET_SCHEMA = "factory.controller.work_packet.v1"
AUTHORITY_SCHEMA = "factory.controller.intake_authority.v1"
AUTHORITY_NAME = "authority.json"
GRANT_ISSUER = "factory-controller"
GRANT_POLICY = "factory.controller.scheduled_source.v1"
GRANT_SOURCE = "factory-owned-grant"
MAX_PACKET_BYTES = 64_000
MAX_SOURCE_FILES = 64
MAX_SOURCE_BYTES = 512_000
MAX_AUTHORITY_BYTES = 16_000

#: Reproduced from ``store`` / ``routing``; stated locally so a fork is a
#: failing test rather than a silent drift.
CANONICAL_ABSENCE = frozenset({"unknown", "not_applicable", "not_run",
                               "not_measurable"})

#: Boundaries a packet may name when it cannot be autonomous.  These are the
#: same nondelegable classes the queue contract already escalates to a person.
OWNER_REASONS = (
    "production_promotion",
    "protected_disclosure",
    "billing",
    "destructive_history",
    "product_scope",
    "owner_judgment",
)


class PacketError(ValueError):
    """A work packet this seam will not load."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkPacket:
    """One Controller-legal Factory-maintenance unit, or a named Owner stop."""

    work_item_id: str
    lineage_id: str
    sequence: int
    source_kind: str
    source_ref: str
    owner_only: bool
    owner_reason: str
    blocked: bool
    payload: dict[str, Any]
    source_revision: str = "unknown"
    authority_digest: str = "unknown"

    def as_row(self) -> dict[str, Any]:
        return {"work_item_id": self.work_item_id, "lineage_id": self.lineage_id,
                "sequence": self.sequence, "source_kind": self.source_kind,
                "source_ref": self.source_ref, "owner_only": self.owner_only,
                "owner_reason": self.owner_reason, "blocked": self.blocked,
                "payload": dict(self.payload),
                "payload_hash": payload_hash(self.payload),
                "source_revision": self.source_revision,
                "authority_digest": self.authority_digest,
                "contract_version": CONTRACT_VERSION}

    def digest(self) -> str:
        return payload_hash(self.as_row())


class WorkSource(Protocol):
    """Anything that can list packets.  Claim is not this object's job."""

    def packets(self) -> Sequence[WorkPacket]:
        ...


def load_packet(raw: Mapping[str, Any], *, source_kind: str,
                source_ref: str) -> WorkPacket:
    """Fail closed on a packet that is not Controller-legal as written."""

    if not isinstance(raw, Mapping):
        raise PacketError("WORK_PACKET_NOT_AN_OBJECT", source_ref)
    schema = raw.get("schema_version")
    if schema != PACKET_SCHEMA:
        raise PacketError(
            "WORK_PACKET_SCHEMA_UNSUPPORTED",
            "%s carries %r" % (source_ref, schema))
    work_item_id = _required_str(raw, "work_item_id", source_ref)
    lineage_id = raw.get("lineage_id") or work_item_id
    if not isinstance(lineage_id, str) or not lineage_id:
        raise PacketError("WORK_PACKET_LINEAGE_MISSING", source_ref)
    sequence = raw.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise PacketError(
            "WORK_PACKET_SEQUENCE_INVALID",
            "%s sequence must be a positive integer" % source_ref)
    owner_only = raw.get("owner_only", False)
    if not isinstance(owner_only, bool):
        raise PacketError("WORK_PACKET_OWNER_ONLY_INVALID", source_ref)
    blocked = raw.get("blocked", False)
    if not isinstance(blocked, bool):
        raise PacketError("WORK_PACKET_BLOCKED_INVALID", source_ref)
    owner_reason = raw.get("owner_reason") or "not_applicable"
    if not isinstance(owner_reason, str) or not owner_reason:
        raise PacketError("WORK_PACKET_OWNER_REASON_INVALID", source_ref)
    if owner_only:
        if owner_reason not in OWNER_REASONS:
            raise PacketError(
                "WORK_PACKET_OWNER_REASON_UNKNOWN",
                "%s names %r" % (source_ref, owner_reason))
    elif owner_reason not in CANONICAL_ABSENCE:
        raise PacketError(
            "WORK_PACKET_OWNER_REASON_UNEXPECTED",
            "%s is autonomous but names %r" % (source_ref, owner_reason))
    payload = raw.get("payload")
    if owner_only:
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise PacketError("WORK_PACKET_PAYLOAD_INVALID", source_ref)
    else:
        if not isinstance(payload, dict) or not payload:
            raise PacketError("WORK_PACKET_PAYLOAD_MISSING", source_ref)
        payload_work = payload.get("work_item_id")
        if payload_work != work_item_id:
            raise PacketError(
                "WORK_PACKET_WORK_ITEM_MISMATCH",
                "%s payload work_item_id %r != %r"
                % (source_ref, payload_work, work_item_id))
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise PacketError("WORK_PACKET_PROJECT_MISSING", source_ref)
    return WorkPacket(
        work_item_id=work_item_id, lineage_id=lineage_id, sequence=sequence,
        source_kind=source_kind, source_ref=source_ref, owner_only=owner_only,
        owner_reason=owner_reason, blocked=blocked, payload=dict(payload))


def _required_str(raw: Mapping[str, Any], name: str, source_ref: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise PacketError("WORK_PACKET_%s_MISSING" % name.upper(), source_ref)
    return value


def _read_authority_bytes(root: Path) -> bytes | None:
    path = root / AUTHORITY_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PacketError("WORK_SOURCE_SYMLINK_REFUSED", str(path))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PacketError("MANAGEMENT_SOURCE_UNAUTHORIZED", str(exc)) from exc
    try:
        return os.read(fd, MAX_AUTHORITY_BYTES + 1)
    finally:
        os.close(fd)


def inspect_inbox(root: str | Path) -> None:
    """Inbox files are data. A self-attested grant file is not authority."""

    root_path = Path(root)
    if root_path.is_symlink():
        raise PacketError("WORK_SOURCE_SYMLINK_REFUSED", str(root_path))
    data = _read_authority_bytes(root_path)
    if data is None:
        return
    if len(data) > MAX_AUTHORITY_BYTES:
        raise PacketError(
            "MANAGEMENT_AUTHORITY_TOO_LARGE",
            "%s exceeds %s bytes" % (AUTHORITY_NAME, MAX_AUTHORITY_BYTES))
    try:
        raw = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PacketError("MANAGEMENT_AUTHORITY_NOT_JSON", str(exc)) from exc
    if not isinstance(raw, dict):
        raise PacketError("MANAGEMENT_AUTHORITY_NOT_JSON", str(root_path / AUTHORITY_NAME))
    if raw.get("prompt") is True:
        raise PacketError(
            "MANAGEMENT_OWNER_PROMPT_POPULATED",
            "authority admits a prompt populated this inbox")
    if any(raw.get(field) not in (None, "", False)
           for field in ("granted_by", "source", "source_revision")):
        raise PacketError(
            "MANAGEMENT_SELF_ATTESTED_AUTHORITY",
            "caller-written granted_by/source/source_revision is not Factory authority")


def packet_digest(root: str | Path) -> str:
    """Controller-computed identity of packet files. Inbox grant fields are ignored."""

    root_path = Path(root)
    inspect_inbox(root_path)
    rows: list[dict[str, Any]] = []
    for path in sorted(root_path.iterdir()):
        if path.suffix != ".json" or path.name == AUTHORITY_NAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise PacketError("WORK_SOURCE_SYMLINK_REFUSED", path.name)
        rows.append({"name": path.name, "bytes": path.read_bytes().hex()})
    return payload_hash({"root": str(root_path.resolve()), "packets": rows})


def load_authority(root: str | Path) -> dict[str, Any]:
    """Safety scan only. Never returns a grant minted from inbox fields."""

    inspect_inbox(root)
    raise PacketError(
        "MANAGEMENT_SELF_ATTESTED_AUTHORITY",
        "inbox files cannot issue scheduled-source authority")


class DirectoryWorkSource:
    """JSON files in one directory, sorted by sequence then identity.

    The directory is a git-backed exchange: files are the packets, the ledger
    is the claim.  A second worker reading the same directory is expected.
    """

    def __init__(self, root: str | Path, *, source_kind: str = "directory",
                 source_revision: str = "unknown",
                 authority_digest: str = "unknown") -> None:
        self.root = Path(root)
        self.source_kind = source_kind
        self.source_revision = source_revision
        self.authority_digest = authority_digest

    def packets(self) -> tuple[WorkPacket, ...]:
        if self.root.is_symlink():
            raise PacketError("WORK_SOURCE_SYMLINK_REFUSED", str(self.root))
        if not self.root.is_dir():
            raise PacketError(
                "WORK_SOURCE_DIRECTORY_MISSING",
                str(self.root))
        inspect_inbox(self.root)
        loaded: list[WorkPacket] = []
        seen: set[str] = set()
        file_count = 0
        total_bytes = 0
        for path in sorted(self.root.iterdir()):
            if path.suffix != ".json":
                continue
            if path.is_symlink() or not path.is_file():
                raise PacketError(
                    "WORK_SOURCE_SYMLINK_REFUSED",
                    path.name)
            size = path.stat().st_size
            file_count += 1
            total_bytes += size
            if file_count > MAX_SOURCE_FILES:
                raise PacketError(
                    "WORK_SOURCE_FILE_BUDGET",
                    "more than %s JSON files" % MAX_SOURCE_FILES)
            if size > MAX_PACKET_BYTES:
                raise PacketError(
                    "WORK_SOURCE_PACKET_TOO_LARGE",
                    path.name)
            if total_bytes > MAX_SOURCE_BYTES:
                raise PacketError(
                    "WORK_SOURCE_BYTE_BUDGET",
                    "source exceeds %s bytes" % MAX_SOURCE_BYTES)
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise PacketError(
                    "WORK_PACKET_NOT_JSON",
                    "%s: %s" % (path.name, exc)) from exc
            if raw.get("schema_version") == AUTHORITY_SCHEMA:
                continue
            packet = load_packet(
                raw, source_kind=self.source_kind, source_ref=path.name)
            packet = replace(
                packet,
                source_revision=self.source_revision,
                authority_digest=self.authority_digest)
            if packet.work_item_id in seen:
                raise PacketError(
                    "WORK_SOURCE_DUPLICATE_IDENTITY",
                    packet.work_item_id)
            seen.add(packet.work_item_id)
            loaded.append(packet)
        loaded.sort(key=lambda packet: (packet.sequence, packet.work_item_id))
        return tuple(loaded)


def packet_json(packet: WorkPacket) -> str:
    body = {
        "schema_version": PACKET_SCHEMA,
        "work_item_id": packet.work_item_id,
        "lineage_id": packet.lineage_id,
        "sequence": packet.sequence,
        "owner_only": packet.owner_only,
        "owner_reason": packet.owner_reason,
        "blocked": packet.blocked,
        "payload": packet.payload,
    }
    return canonical_json(body)
