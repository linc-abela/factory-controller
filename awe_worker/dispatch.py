"""Deterministic harness Dispatch pointers and fail-closed headless resolution.

Dispatch is a routing projection only. Physical AWE ancestry remains lifecycle
truth. Headless startup fetches a fixed Dispatch page ID and the pointed Task
Page; it never searches the workspace, scans lifecycle folders, or tries
candidate task IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import AWEWorkItem, ExecutionSlot
from .notion import NotionAPIError, NotionClient, resolve_physical_folder_status

OWNER_QUEUE_COMMAND = "Process your Queue."

DISPATCH_PAGE_IDS: dict[str, str] = {
    "cursor": "3d3690f6-eb14-8178-b51c-f1baa2fd4d32",
    "codex": "3d3690f6-eb14-8100-984b-d66fb476fe81",
    "antigravity": "3d3690f6-eb14-8193-b5d4-ccf1e81c08db",
}

DISPATCH_STALE = "DISPATCH_POINTER_STALE"
NO_EXECUTABLE_TASK = "NO_EXECUTABLE_TASK"
EXECUTABLE = "EXECUTABLE"

_POINTER_MARKERS = (
    "task id:",
    "task page:",
    "execution profile:",
    "expected lifecycle state:",
    "dispatch state:",
    "physical state:",
    "current pointer",
    "executable",
)


@dataclass(frozen=True)
class DispatchPointer:
    task_id: str
    execution_profile: str
    expected_lifecycle_state: str
    task_page_url: str
    dispatch_state: str
    frozen_head: str = ""
    harness: str = ""

    @property
    def executable(self) -> bool:
        return self.dispatch_state.upper() == EXECUTABLE and bool(self.task_id)

    @property
    def task_page_id(self) -> str:
        return page_id_from_url(self.task_page_url)

    @property
    def slot(self) -> ExecutionSlot:
        try:
            if self.execution_profile:
                return ExecutionSlot.parse(self.execution_profile)
        except ValueError:
            pass
        harness = (self.harness or "").strip().lower()
        return ExecutionSlot(harness=harness or "unknown", model="unparsed", effort="unparsed")


@dataclass(frozen=True)
class DispatchResolveResult:
    ok: bool
    code: str
    pointer: DispatchPointer | None = None
    detail: str = ""
    searched: bool = False


def page_id_from_url(url: str) -> str:
    compact = re.sub(r"[^0-9a-f]", "", (url or "").lower())
    if len(compact) < 32:
        return ""
    h = compact[-32:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def format_pointer_text(pointer: DispatchPointer) -> str:
    lines = [
        "## Current pointer",
        f"- Task ID: `{pointer.task_id or 'none'}`",
        f"- Execution profile: `{pointer.execution_profile}`",
        f"- Expected lifecycle state: `{pointer.expected_lifecycle_state}`",
        f"- Task page: {pointer.task_page_url or 'none'}",
        f"- Dispatch state: `{pointer.dispatch_state}`",
    ]
    if pointer.frozen_head:
        lines.append(f"- Frozen head: `{pointer.frozen_head}`")
    return "\n".join(lines)


def parse_pointers(text: str, harness: str = "") -> list[DispatchPointer]:
    if not text or not str(text).strip():
        return []
    chunks = re.split(r"(?=##\s+Current pointer)", text, flags=re.IGNORECASE)
    pointers: list[DispatchPointer] = []
    for chunk in chunks:
        if "task id:" not in chunk.lower() and "dispatch state:" not in chunk.lower():
            continue
        pointers.append(
            DispatchPointer(
                task_id=_field(chunk, r"Task ID:\s*`?([^`\n]+)`?"),
                execution_profile=_field(chunk, r"Execution profile:\s*`?([^`\n]+)`?"),
                expected_lifecycle_state=_field(chunk, r"Expected lifecycle state:\s*`?([^`\n]+)`?"),
                task_page_url=_task_page_url(chunk),
                dispatch_state=_field(chunk, r"Dispatch state:\s*`?([^`\n]+)`?") or EXECUTABLE,
                frozen_head=_field(chunk, r"Frozen head:\s*`?([^`\n]+)`?"),
                harness=harness,
            )
        )
    if pointers or re.search(
        r"(?im)^\s*(?:[-*+]\s*)?\**(?:EXECUTABLE|NO_EXECUTABLE_TASK)\b", text
    ) is None:
        return pointers

    # The live Dispatch pages were created before the canonical pointer block
    # format existed.  They are still authoritative assignment projections,
    # so accept that shape only when all of the same binding facts can be
    # recovered.  We never search for a task or infer a profile from a title.
    legacy_chunks = re.split(
        r"(?im)(?=^\s*(?:[-*+]\s*)?\**(?:EXECUTABLE|NO_EXECUTABLE_TASK)\b)",
        text,
    )
    for chunk in legacy_chunks:
        if not re.search(
            r"(?im)^\s*(?:[-*+]\s*)?\**(?:EXECUTABLE|NO_EXECUTABLE_TASK)\b",
            chunk,
        ):
            continue
        task_id = _field(chunk, r"Task ID:\s*`?([^`\n]+)`?")
        if not task_id:
            match = re.search(r"\b(SF-\d+)\b", chunk, flags=re.IGNORECASE)
            task_id = match.group(1).upper() if match else ""
        execution_profile = _field(chunk, r"Execution Profile:\s*`?([^`\n]*)`?")
        if not execution_profile:
            execution_profile = _profile_from_fields(chunk)
        lifecycle = _field(chunk, r"Expected Lifecycle State:\s*`?([^`\n]+)`?")
        if not lifecycle:
            lifecycle = _field(chunk, r"Physical State:\s*`?([^`\n]+)`?")
        dispatch_state = _field(chunk, r"Dispatch State:\s*`?([^`\n]+)`?")
        if not dispatch_state:
            dispatch_state = (
                NO_EXECUTABLE_TASK
                if re.search(
                    r"(?im)^\s*(?:[-*+]\s*)?\**NO_EXECUTABLE_TASK\b", chunk
                )
                else EXECUTABLE
            )
        pointers.append(
            DispatchPointer(
                task_id=task_id,
                execution_profile=execution_profile,
                expected_lifecycle_state=lifecycle,
                task_page_url=_task_page_url(chunk),
                dispatch_state=dispatch_state,
                frozen_head=_field(chunk, r"Frozen Head:\s*`?([^`\n]+)`?"),
                harness=harness,
            )
        )
    return pointers


def _task_page_url(chunk: str) -> str:
    mention = re.search(
        r'<mention-page\s+url="([^"]+)"',
        chunk,
        flags=re.IGNORECASE,
    )
    if mention:
        return mention.group(1).strip()
    href = re.search(
        r"Task page:\s*(?:\[.*?\]\()?(https?://[^\s)<>]+)",
        chunk,
        flags=re.IGNORECASE,
    )
    if href:
        return href.group(1).strip().rstrip(").,")
    raw = _field(chunk, r"Task page:\s*`?(\S+)`?")
    if raw.lower().startswith("<mention-page"):
        return ""
    return raw


def _field(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip().rstrip(".")
    value = value.strip("*").strip()
    if value.lower() in {"none", "n/a", "<mention-page/>"}:
        return ""
    return value


def _profile_from_fields(text: str) -> str:
    """Build a profile only from explicit assignment fields."""
    lane = _field(text, r"Lane:\s*`?([^`\n]+)`?")
    model_effort = _field(text, r"Model\s*/\s*Effort:\s*`?([^`\n]+)`?")
    if lane and model_effort:
        candidate = f"{lane} -> {model_effort}"
        try:
            ExecutionSlot.parse(candidate)
        except ValueError:
            return ""
        return candidate
    return ""


def _block_plain_text(block: Mapping[str, Any]) -> str:
    if not isinstance(block, Mapping):
        return ""
    block_type = str(block.get("type") or "")
    payload = block.get(block_type) if isinstance(block.get(block_type), Mapping) else {}
    rich = payload.get("rich_text") or payload.get("text") or []
    if isinstance(rich, str):
        return rich
    if not isinstance(rich, list):
        return ""
    parts: list[str] = []
    for item in rich:
        if not isinstance(item, Mapping):
            continue
        mention = item.get("mention")
        if isinstance(mention, Mapping):
            href = str(item.get("href") or "")
            page = mention.get("page") if mention.get("type") == "page" else None
            page_id = ""
            if isinstance(page, Mapping):
                page_id = str(page.get("id") or "")
            if href:
                parts.append(href)
            elif page_id:
                parts.append(f"https://app.notion.com/p/{page_id.replace('-', '')}")
            continue
        parts.append(str(item.get("plain_text") or item.get("text", {}).get("content") or ""))
    return "".join(parts)


def _paragraph_block(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
        },
    }


class DispatchMaintainer:
    """Writes the stable harness Dispatch pointer after a lifecycle transition."""

    def __init__(self, client: NotionClient | None = None) -> None:
        self.client = client or NotionClient()

    def page_id_for(self, harness: str) -> str:
        return DISPATCH_PAGE_IDS.get((harness or "").strip().lower(), "")

    def write_pointer(self, pointer: DispatchPointer) -> str:
        if not self.client.is_configured:
            return "NOTION_NOT_CONFIGURED"
        harness = pointer.harness or ExecutionSlot.parse(pointer.execution_profile).harness
        page_id = self.page_id_for(harness)
        if not page_id:
            return f"{DISPATCH_STALE}: no Dispatch page for harness '{harness}'"
        try:
            resp = self.client.retrieve_block_children(page_id)
        except NotionAPIError as exc:
            return f"DISPATCH_WRITE_FAILED: {exc}"
        children = resp.get("results") if isinstance(resp, dict) else None
        if not isinstance(children, list):
            children = []
        for block in children:
            if not isinstance(block, dict):
                continue
            text = _block_plain_text(block)
            if any(marker in text.lower() for marker in _POINTER_MARKERS):
                block_id = block.get("id")
                if block_id:
                    try:
                        self.client.delete_block(str(block_id))
                    except NotionAPIError as exc:
                        return (
                            f"{DISPATCH_STALE}: stale-pointer deletion failed for "
                            f"block {block_id}: {exc}"
                        )
        try:
            self.client.append_block_children(
                page_id,
                [_paragraph_block(line) for line in format_pointer_text(pointer).splitlines()],
            )
        except NotionAPIError as exc:
            return f"DISPATCH_WRITE_FAILED: {exc}"
        return ""

    def write_for_task(
        self,
        task: AWEWorkItem,
        lifecycle_state: str,
        dispatch_state: str = EXECUTABLE,
        frozen_head: str = "",
    ) -> str:
        harness = (task.lane or task.slot.harness or "").strip().lower()
        profile = f"{harness.capitalize()} -> {task.model} / {task.effort}"
        return self.write_pointer(
            DispatchPointer(
                task_id=task.task_id if dispatch_state == EXECUTABLE else "",
                execution_profile=profile,
                expected_lifecycle_state=lifecycle_state,
                task_page_url=task.task_page_url
                or (f"https://app.notion.com/p/{task.task_page_id.replace('-', '')}" if task.task_page_id else ""),
                dispatch_state=dispatch_state,
                frozen_head=frozen_head,
                harness=harness,
            )
        )


class HeadlessDispatchResolver:
    """Resolve the current task from a fixed Dispatch URL. Never searches."""

    def __init__(self, client: NotionClient | None = None) -> None:
        self.client = client or NotionClient()

    def resolve(
        self,
        harness: str,
        slot: ExecutionSlot | None = None,
    ) -> DispatchResolveResult:
        if getattr(self.client, "search", None) is not None:
            # Tests may attach a search spy. Production NotionClient has no search.
            pass
        page_id = DISPATCH_PAGE_IDS.get((harness or "").strip().lower(), "")
        if not page_id:
            return DispatchResolveResult(False, DISPATCH_STALE, detail=f"unknown harness '{harness}'")
        if not self.client.is_configured:
            return DispatchResolveResult(False, DISPATCH_STALE, detail="NOTION_NOT_CONFIGURED")
        pointers, read_error = self._read_pointers(harness)
        if read_error:
            return read_error
        if not pointers:
            return DispatchResolveResult(False, DISPATCH_STALE, detail="missing Dispatch pointer")

        selected = self._select_pointer(pointers, slot)
        if selected is None:
            if len(pointers) > 1 and slot is None:
                return DispatchResolveResult(
                    False,
                    DISPATCH_STALE,
                    detail="duplicate-slot Dispatch requires exact execution profile",
                )
            return DispatchResolveResult(False, DISPATCH_STALE, detail="no matching Dispatch pointer")
        return self._validate_pointer(selected, harness.lower(), slot=slot)

    def resolve_all(self, harness: str) -> list[DispatchResolveResult]:
        """Resolve every pointer on one fixed harness Dispatch page.

        A continuation supervisor needs all independently authorized slots, but
        the same physical ancestry and profile checks apply to each pointer.
        An empty/error result is represented explicitly so status reporting can
        distinguish ``NO_EXECUTABLE_TASK`` from an unreadable projection.
        """
        pointers, read_error = self._read_pointers(harness)
        if read_error:
            return [read_error]
        if not pointers:
            return [DispatchResolveResult(False, DISPATCH_STALE, detail="missing Dispatch pointer")]
        return [self._validate_pointer(pointer, harness.lower()) for pointer in pointers]

    def _read_pointers(
        self,
        harness: str,
    ) -> tuple[list[DispatchPointer], DispatchResolveResult | None]:
        page_id = DISPATCH_PAGE_IDS.get((harness or "").strip().lower(), "")
        if not page_id:
            return [], DispatchResolveResult(
                False, DISPATCH_STALE, detail=f"unknown harness '{harness}'"
            )
        if not self.client.is_configured:
            return [], DispatchResolveResult(
                False, DISPATCH_STALE, detail="NOTION_NOT_CONFIGURED"
            )
        try:
            children = self._all_children(page_id)
        except NotionAPIError as exc:
            return [], DispatchResolveResult(False, DISPATCH_STALE, detail=str(exc))
        text = "\n".join(_block_plain_text(b) for b in children if isinstance(b, Mapping))
        return parse_pointers(text, harness=(harness or "").strip().lower()), None

    def _validate_pointer(
        self,
        selected: DispatchPointer,
        harness: str,
        slot: ExecutionSlot | None = None,
    ) -> DispatchResolveResult:
        if selected.dispatch_state.upper() == NO_EXECUTABLE_TASK or not selected.executable:
            return DispatchResolveResult(True, NO_EXECUTABLE_TASK, pointer=selected, detail="no executable task")
        if not selected.task_page_id:
            return DispatchResolveResult(False, DISPATCH_STALE, pointer=selected, detail="missing Task Page URL")

        try:
            page = self.client.retrieve_page(selected.task_page_id)
        except NotionAPIError as exc:
            return DispatchResolveResult(False, DISPATCH_STALE, pointer=selected, detail=str(exc))
        parent = page.get("parent") if isinstance(page, dict) else {}
        parent_id = parent.get("page_id", "") if isinstance(parent, Mapping) else ""
        if not parent_id:
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail="UNKNOWN_PHYSICAL_ANCESTRY: Task Page parent missing",
            )
        physical = resolve_physical_folder_status(parent_id, lane=harness)
        if physical is None:
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail=f"UNKNOWN_PHYSICAL_ANCESTRY: parent {parent_id}",
            )
        from .model import canonical_lifecycle_status

        expected = selected.expected_lifecycle_state
        if not expected:
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail="missing expected lifecycle state",
            )
        expected_canonical = canonical_lifecycle_status(expected)
        if expected_canonical and physical.lower() != expected_canonical.lower():
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail=f"physical ancestry '{physical}' != Dispatch '{expected}'",
            )
        if not selected.execution_profile:
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail="missing execution profile",
            )
        if selected.slot.harness != harness:
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail="execution profile harness mismatch",
            )
        if slot is not None and not selected.slot.matches(slot):
            return DispatchResolveResult(
                False,
                DISPATCH_STALE,
                pointer=selected,
                detail="execution profile mismatch",
            )
        return DispatchResolveResult(True, "OK", pointer=selected)

    def _select_pointer(
        self,
        pointers: Sequence[DispatchPointer],
        slot: ExecutionSlot | None,
    ) -> DispatchPointer | None:
        if slot is not None:
            matches = [p for p in pointers if p.slot.matches(slot)]
            return matches[0] if len(matches) == 1 else None
        executable = [p for p in pointers if p.executable or p.dispatch_state.upper() == NO_EXECUTABLE_TASK]
        if len(pointers) == 1:
            return pointers[0]
        if len(executable) == 1:
            return executable[0]
        return None

    def _all_children(self, page_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            resp = self.client.retrieve_block_children(page_id, start_cursor=cursor)
            if not isinstance(resp, dict):
                break
            chunk = resp.get("results")
            if isinstance(chunk, list):
                results.extend(b for b in chunk if isinstance(b, dict))
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = str(resp["next_cursor"])
        return results
