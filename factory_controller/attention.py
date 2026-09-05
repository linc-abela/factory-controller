"""Stage 10 / Operations Plane: Owner attention delivery, sink seams, and liveness.

Follows the Factory Owner Attention Budget (skills/software-factory-owner-attention).
Owner attention is a scarce Factory resource. It must only be summoned for genuine
Owner-only blockers (such as required human approval, unapproved host service changes,
missing secrets, manual product validation, or an unrecoverable supervisor
failure). Normal, reversible engineering failures that agents or deterministic retry
can resolve must never escalate to Owner attention.

Key invariants:
1. Pure contract and delivery seam: separate from lifecycle authority. Delivering an
   attention event does not approve a release, widen policy, or mutate mission state.
2. Zero-cost macOS-visible sink: uses native osascript notification banners without
   requiring external services or paid infrastructure.
3. Pluggable channel seam: supports extensible channels (macOS notification, local
   durable file, recording sink for tests/simulation, and external HTTP/email stubs).
4. Deterministic deduplication and rate limiting: prevents supervisor cycle flapping from
   spamming the Owner. Repeated unresolved events for the same blocker are coalesced;
   changed, resolved, and reopened states are tracked predictably.
5. Strict authority boundaries:
   - Zero vendor tokens
   - Zero credential-shaped tokens
   - No direct process launching in core (uses injected runner)
   - No environment variable reads in core
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


CONTRACT_VERSION = "factory-controller/attention/1.0"

# Attention Categories
CATEGORY_OWNER_ACTION = "OWNER_ACTION_REQUIRED"
CATEGORY_AUTHORITY = "AUTHORITY_REQUIRED"
CATEGORY_SECRETS = "SECRETS_REQUIRED"
CATEGORY_SUPERVISOR_FAILURE = "SUPERVISOR_FAILURE"
CATEGORY_INFRASTRUCTURE = "INFRASTRUCTURE_BLOCKED"
CATEGORY_ENGINEERING_REVERSIBLE = "ENGINEERING_REVERSIBLE"

# Deliverable Categories (categories that actually escalate to Owner)
OWNER_ATTENTION_CATEGORIES = frozenset({
    CATEGORY_OWNER_ACTION,
    CATEGORY_AUTHORITY,
    CATEGORY_SECRETS,
    CATEGORY_SUPERVISOR_FAILURE,
    CATEGORY_INFRASTRUCTURE,
})

# Codes that indicate genuine Owner attention
KNOWN_OWNER_ATTENTION_CODES = frozenset({
    "AUTOPILOT_ATTENTION",
    "SUPERVISOR_FAILURE",
    "SUPERVISOR_ACTIVATION_UNAPPROVED",
    "OWNER_IDENTITY_UNAVAILABLE",
    "OWNER_VALIDATION_REQUIRED",
    "RELEASE_AUTHORITY_REQUIRED",
    "PROMOTION_UNAPPROVED",
    "SUPERVISOR_DEAD",
    "LIVENESS_HEARTBEAT_EXPIRED",
    "BRIDGE_PROBLEM",
    "CAPACITY_UNAVAILABLE",
})

# Delivery States
DELIVERY_STATE_INITIAL = "delivered_initial"
DELIVERY_STATE_UPDATED = "delivered_updated"
DELIVERY_STATE_REMINDER = "delivered_reminder"
DELIVERY_STATE_SUPPRESSED = "suppressed_cooldown"
DELIVERY_STATE_RESOLVED = "delivered_resolved"
DELIVERY_STATE_REOPENED = "delivered_reopened"
DELIVERY_STATE_SKIPPED = "skipped_reversible"

DEFAULT_COOLDOWN_SECONDS = 3600.0  # 1 hour cooldown between duplicate reminders
DEFAULT_LIVENESS_TOLERANCE_SECONDS = 600.0  # 2 * 300s supervisor interval


def compute_fingerprint(category: str, code: str, target_ref: str) -> str:
    """Deterministic, bounded identifier for a blocker across supervisor cycles."""
    raw = f"{category}:{code}:{target_ref}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AttentionEvent:
    """An Owner attention event representing a genuine human-required blocker."""

    event_id: str
    fingerprint: str
    category: str
    code: str
    headline: str
    message: str
    action_required: str
    target_ref: str
    observed_at: float
    state: str = "active"  # "active" or "resolved"
    severity: str = "blocker"  # "blocker", "warning", "info"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "event_id": self.event_id,
            "fingerprint": self.fingerprint,
            "category": self.category,
            "code": self.code,
            "headline": self.headline,
            "message": self.message,
            "action_required": self.action_required,
            "target_ref": self.target_ref,
            "observed_at": self.observed_at,
            "state": self.state,
            "severity": self.severity,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DeliveryReceipt:
    """Record of an attempt to deliver an attention event through a sink."""

    event_id: str
    fingerprint: str
    channel: str
    delivered: bool
    delivery_state: str
    timestamp: float
    detail: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "event_id": self.event_id,
            "fingerprint": self.fingerprint,
            "channel": self.channel,
            "delivered": self.delivered,
            "delivery_state": self.delivery_state,
            "timestamp": self.timestamp,
            "detail": self.detail,
            "error": self.error,
        }


def classify_attention(
    code: str,
    detail: str,
    *,
    retryable: bool = False,
    work_state: str | None = None,
    expected_gate_failure: bool = False,
    target_ref: str = "",
) -> tuple[bool, str, str]:
    """Distinguish genuine Owner-only blockers from reversible engineering failures.

    Returns (is_owner_attention, category, reason).
    """
    if retryable:
        return False, CATEGORY_ENGINEERING_REVERSIBLE, "Failure is marked retryable by scheduler"

    if expected_gate_failure:
        return False, CATEGORY_ENGINEERING_REVERSIBLE, "Evaluator outcome is declared expectation"

    # Category matching
    if code in {"SUPERVISOR_ACTIVATION_UNAPPROVED", "OWNER_VALIDATION_REQUIRED",
                "RELEASE_AUTHORITY_REQUIRED", "PROMOTION_UNAPPROVED"}:
        return True, CATEGORY_AUTHORITY, "Requires explicit human authority or approval"

    if code in {"OWNER_IDENTITY_UNAVAILABLE"}:
        return True, CATEGORY_OWNER_ACTION, "Local trusted identity required"

    if code in {"SUPERVISOR_FAILURE", "SUPERVISOR_DEAD", "LIVENESS_HEARTBEAT_EXPIRED"}:
        return True, CATEGORY_SUPERVISOR_FAILURE, "Supervisor execution layer failure"

    if "SECRET" in code or "KEY" in code or "TOKEN" in code:
        return True, CATEGORY_SECRETS, "Missing or unconfigured secret"

    if code == "AUTOPILOT_ATTENTION":
        # Non-retryable mission settled in failed/escalated/refused
        return True, CATEGORY_OWNER_ACTION, "Autopilot portfolio mission settled unrecoverably"

    if code in KNOWN_OWNER_ATTENTION_CODES:
        return True, CATEGORY_INFRASTRUCTURE, "Known system blocker requiring attention"

    # Check detail text heuristics for genuine Owner-only language
    lower_detail = detail.lower()
    if "owner attention" in lower_detail or "owner review" in lower_detail:
        return True, CATEGORY_OWNER_ACTION, "Detail explicitly requires Owner review"

    return False, CATEGORY_ENGINEERING_REVERSIBLE, "Unclassified failure defaults to agent-reversible"


class AttentionSink(Protocol):
    """Channel interface for delivering Owner attention events."""

    channel_name: str

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        ...


class RecordingAttentionSink:
    """In-memory recording sink for deterministic testing and simulation."""

    def __init__(self, channel_name: str = "recording") -> None:
        self.channel_name = channel_name
        self.events: list[AttentionEvent] = []
        self.receipts: list[DeliveryReceipt] = []

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        self.events.append(event)
        delivery_state = str(event.metadata.get("delivery_state", DELIVERY_STATE_INITIAL))
        receipt = DeliveryReceipt(
            event_id=event.event_id,
            fingerprint=event.fingerprint,
            channel=self.channel_name,
            delivered=True,
            delivery_state=delivery_state,
            timestamp=time.time(),
            detail="Recorded in memory",
        )
        self.receipts.append(receipt)
        return receipt


class FileAttentionSink:
    """Zero-cost local durable file sink."""

    def __init__(self, target_path: Path, *, channel_name: str = "file") -> None:
        self.channel_name = channel_name
        self.target_path = target_path

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        try:
            self.target_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "contract_version": CONTRACT_VERSION,
                "current_event": event.as_dict(),
                "delivered_at": time.time(),
            }
            self.target_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
            delivery_state = str(event.metadata.get("delivery_state", DELIVERY_STATE_INITIAL))
            return DeliveryReceipt(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                channel=self.channel_name,
                delivered=True,
                delivery_state=delivery_state,
                timestamp=time.time(),
                detail=str(self.target_path),
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                channel=self.channel_name,
                delivered=False,
                delivery_state="failed",
                timestamp=time.time(),
                error=str(exc),
            )


class MacOSNotificationSink:
    """Zero-cost macOS Notification Center banner sink using osascript."""

    def __init__(self, runner: Callable[..., Any] | None = None,
                 *, channel_name: str = "macos_notification") -> None:
        self.channel_name = channel_name
        self.runner = runner

    @staticmethod
    def _escape(text: str) -> str:
        """Escape text safely for AppleScript string literals."""
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    def build_script(self, event: AttentionEvent) -> str:
        title = self._escape(event.headline)
        msg = self._escape(event.message)
        sub = self._escape(event.action_required)
        return (
            f'display notification "{msg}" '
            f'with title "{title}" '
            f'subtitle "{sub}" '
            f'sound name "default"'
        )

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        script = self.build_script(event)
        cmd = ("osascript", "-e", script)
        delivery_state = str(event.metadata.get("delivery_state", DELIVERY_STATE_INITIAL))
        if self.runner is None:
            return DeliveryReceipt(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                channel=self.channel_name,
                delivered=False,
                delivery_state="skipped_no_runner",
                timestamp=time.time(),
                detail="No runner provided",
            )
        try:
            result = self.runner(cmd, timeout_seconds=10)
            exit_code = getattr(result, "returncode", 0) if not isinstance(result, tuple) else result[0]
            if exit_code == 0:
                return DeliveryReceipt(
                    event_id=event.event_id,
                    fingerprint=event.fingerprint,
                    channel=self.channel_name,
                    delivered=True,
                    delivery_state=delivery_state,
                    timestamp=time.time(),
                    detail=f"Displayed notification: {event.headline}",
                )
            err = getattr(result, "stderr", "") if not isinstance(result, tuple) else (result[2] if len(result) > 2 else "")
            return DeliveryReceipt(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                channel=self.channel_name,
                delivered=False,
                delivery_state="failed",
                timestamp=time.time(),
                error=f"osascript returned {exit_code}: {err}",
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(
                event_id=event.event_id,
                fingerprint=event.fingerprint,
                channel=self.channel_name,
                delivered=False,
                delivery_state="failed",
                timestamp=time.time(),
                error=str(exc),
            )


class CompositeAttentionSink:
    """Dispatches to multiple sinks."""

    def __init__(self, sinks: Sequence[AttentionSink], *, channel_name: str = "composite") -> None:
        self.channel_name = channel_name
        self.sinks = tuple(sinks)

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        receipts = [sink.deliver(event) for sink in self.sinks]
        any_delivered = any(r.delivered for r in receipts)
        errors = [r.error for r in receipts if r.error]
        delivery_state = str(event.metadata.get("delivery_state", DELIVERY_STATE_INITIAL))
        return DeliveryReceipt(
            event_id=event.event_id,
            fingerprint=event.fingerprint,
            channel=self.channel_name,
            delivered=any_delivered,
            delivery_state=delivery_state,
            timestamp=time.time(),
            detail=f"Dispatched to {len(self.sinks)} sinks",
            error="; ".join(errors) if errors else None,
        )


class ExternalChannelStub:
    """Pluggable channel seam for later email / app / HTTP-callback delivery."""

    def __init__(self, endpoint_label: str = "stub_external",
                 channel_name: str = "external_stub") -> None:
        self.channel_name = channel_name
        self.endpoint_label = endpoint_label

    def deliver(self, event: AttentionEvent) -> DeliveryReceipt:
        return DeliveryReceipt(
            event_id=event.event_id,
            fingerprint=event.fingerprint,
            channel=self.channel_name,
            delivered=True,
            delivery_state="staged_pluggable_seam",
            timestamp=time.time(),
            detail=f"Pluggable seam ready for endpoint: {self.endpoint_label}",
        )


class AttentionLedger:
    """Durable state tracker for deduplication, cooldowns, and resolution."""

    def __init__(self, state_file: Path | None = None) -> None:
        self.state_file = state_file
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self._entries = data.get("entries", {})
            except Exception:  # noqa: BLE001
                self._entries = {}

    def _save(self) -> None:
        if self.state_file:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "contract_version": CONTRACT_VERSION,
                    "updated_at": time.time(),
                    "entries": self._entries,
                }
                self.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True))
            except Exception:  # noqa: BLE001
                pass

    def evaluate(self, event: AttentionEvent, now: float,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS) -> tuple[bool, str]:
        """Determine whether event should be delivered or suppressed.

        Returns (should_deliver, delivery_state).
        """
        entry = self._entries.get(event.fingerprint)
        if entry is None:
            # First time seen
            self._entries[event.fingerprint] = {
                "fingerprint": event.fingerprint,
                "first_seen_at": now,
                "last_notified_at": now,
                "last_message": event.message,
                "state": "active",
                "notify_count": 1,
                "suppress_count": 0,
            }
            self._save()
            return True, DELIVERY_STATE_INITIAL

        prev_state = entry.get("state", "active")
        if prev_state == "resolved":
            # Reopened!
            entry["state"] = "active"
            entry["last_notified_at"] = now
            entry["last_message"] = event.message
            entry["notify_count"] = entry.get("notify_count", 0) + 1
            self._save()
            return True, DELIVERY_STATE_REOPENED

        # Already active: check if message materially changed
        if entry.get("last_message") != event.message:
            entry["last_message"] = event.message
            entry["last_notified_at"] = now
            entry["notify_count"] = entry.get("notify_count", 0) + 1
            self._save()
            return True, DELIVERY_STATE_UPDATED

        # Same message: check cooldown
        last_notified = entry.get("last_notified_at", 0.0)
        if (now - last_notified) >= cooldown_seconds:
            entry["last_notified_at"] = now
            entry["notify_count"] = entry.get("notify_count", 0) + 1
            self._save()
            return True, DELIVERY_STATE_REMINDER

        # Suppressed under cooldown
        entry["suppress_count"] = entry.get("suppress_count", 0) + 1
        self._save()
        return False, DELIVERY_STATE_SUPPRESSED

    def resolve(self, fingerprint: str, now: float) -> bool:
        """Mark an active blocker as resolved."""
        entry = self._entries.get(fingerprint)
        if entry and entry.get("state") == "active":
            entry["state"] = "resolved"
            entry["resolved_at"] = now
            self._save()
            return True
        return False

    def active_entries(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._entries.items() if v.get("state") == "active"}

    def all_entries(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)


class AttentionRouter:
    """Coordinates classification, deduplication, and sink delivery."""

    def __init__(
        self,
        sink: AttentionSink,
        *,
        ledger: AttentionLedger | None = None,
        clock: Callable[[], float] = time.time,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.sink = sink
        self.ledger = ledger or AttentionLedger()
        self.clock = clock
        self.cooldown_seconds = cooldown_seconds

    def emit(
        self,
        code: str,
        detail: str,
        *,
        target_ref: str = "factory",
        headline: str | None = None,
        action_required: str = "./dev factory status",
        retryable: bool = False,
        expected_gate_failure: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> DeliveryReceipt:
        """Process a candidate failure or condition and deliver if appropriate."""
        now = self.clock()
        is_owner, category, reason = classify_attention(
            code, detail, retryable=retryable,
            expected_gate_failure=expected_gate_failure, target_ref=target_ref
        )

        event_id = hashlib.sha256(f"{now}:{code}:{target_ref}:{detail}".encode()).hexdigest()[:16]
        fingerprint = compute_fingerprint(category, code, target_ref)

        if not is_owner:
            # Reversible engineering failure - do not escalate
            return DeliveryReceipt(
                event_id=event_id,
                fingerprint=fingerprint,
                channel="classifier",
                delivered=False,
                delivery_state=DELIVERY_STATE_SKIPPED,
                timestamp=now,
                detail=f"Reversible failure not escalated: {reason}",
            )

        event_headline = headline or f"FACTORY ATTENTION: {target_ref}"
        event = AttentionEvent(
            event_id=event_id,
            fingerprint=fingerprint,
            category=category,
            code=code,
            headline=event_headline,
            message=detail,
            action_required=action_required,
            target_ref=target_ref,
            observed_at=now,
            state="active",
            severity="blocker",
            metadata=dict(metadata or {}),
        )

        should_deliver, delivery_state = self.ledger.evaluate(
            event, now, cooldown_seconds=self.cooldown_seconds
        )

        if not should_deliver:
            return DeliveryReceipt(
                event_id=event_id,
                fingerprint=fingerprint,
                channel=self.sink.channel_name,
                delivered=False,
                delivery_state=delivery_state,
                timestamp=now,
                detail="Suppressed: identical event within cooldown window",
            )

        annotated_event = AttentionEvent(
            event_id=event.event_id,
            fingerprint=event.fingerprint,
            category=event.category,
            code=event.code,
            headline=event.headline,
            message=event.message,
            action_required=event.action_required,
            target_ref=event.target_ref,
            observed_at=event.observed_at,
            state=event.state,
            severity=event.severity,
            metadata={**dict(event.metadata), "delivery_state": delivery_state},
        )
        return self.sink.deliver(annotated_event)

    def resolve(self, fingerprint: str) -> bool:
        return self.ledger.resolve(fingerprint, self.clock())


def check_supervisor_liveness(
    receipt_path: Path,
    *,
    tolerance_seconds: float = DEFAULT_LIVENESS_TOLERANCE_SECONDS,
    clock: Callable[[], float] = time.time,
) -> tuple[bool, str, float]:
    """Check whether supervisor runtime receipt indicates a stalled or dead service.

    Returns (is_healthy, detail, age_seconds).
    """
    now = clock()
    if not receipt_path.exists():
        return False, f"Supervisor runtime receipt absent at {receipt_path}", -1.0

    try:
        data = json.loads(receipt_path.read_text())
    except Exception as exc:  # noqa: BLE001
        return False, f"Supervisor runtime receipt corrupted: {exc}", -1.0

    last_timestamp = data.get("timestamp") or data.get("ended_at") or data.get("started_at")
    if last_timestamp is None:
        return False, "Supervisor runtime receipt contains no timestamp", -1.0

    age = now - float(last_timestamp)
    if age > tolerance_seconds:
        return (
            False,
            f"Supervisor has not cycled in {int(age)}s (tolerance: {int(tolerance_seconds)}s)",
            age,
        )

    return True, f"Supervisor healthy (last cycle {int(age)}s ago)", age
