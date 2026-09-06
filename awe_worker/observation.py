"""Task observation and queue filtering for Agent Work Exchange."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, Sequence

from .model import AWEStatus, AWEWorkItem, ExecutionSlot


class TaskSource(Protocol):
    """Protocol for observing AWE tasks from an exchange source."""

    def fetch_tasks(self) -> Sequence[AWEWorkItem]:
        ...


class MemoryTaskSource:
    """In-memory task source for testing and local simulation."""

    def __init__(self, tasks: Sequence[AWEWorkItem]) -> None:
        self._tasks = list(tasks)

    def fetch_tasks(self) -> Sequence[AWEWorkItem]:
        return list(self._tasks)


class DirectoryTaskSource:
    """Task source reading JSON task packets from an exchange directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def fetch_tasks(self) -> list[AWEWorkItem]:
        if not self.root.is_dir():
            return []
        items: list[AWEWorkItem] = []
        for path in sorted(self.root.iterdir()):
            if path.suffix != ".json" or path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # Handle both flat work-item format and controller work packet format
                    if "schema_version" in data and "payload" in data:
                        payload = data.get("payload", {})
                        work_item_id = data.get("work_item_id", path.stem)
                        item = AWEWorkItem(
                            task_id=work_item_id,
                            title=payload.get("title", work_item_id),
                            lane=payload.get("lane", ""),
                            role=payload.get("role", ""),
                            status=payload.get("status", AWEStatus.QUEUE.value),
                            model=payload.get("model", ""),
                            effort=payload.get("effort", "medium"),
                            sequence=int(data.get("sequence", 999999)),
                            owner_only=bool(data.get("owner_only", False)),
                            owner_reason=str(data.get("owner_reason", "not_applicable")),
                            notes=payload.get("notes", ""),
                            body_markdown=payload.get("body_markdown", ""),
                        )
                    else:
                        item = AWEWorkItem.from_dict(data)
                    items.append(item)
            except Exception:
                continue
        items.sort(key=lambda x: (x.sequence, x.task_id))
        return items


class NotionTaskSource:
    """Notion AWE task source that reads pages from AWE folder hierarchy."""

    def __init__(self, notion_client_or_pages: Any) -> None:
        self.client_or_pages = notion_client_or_pages

    def fetch_tasks(self) -> list[AWEWorkItem]:
        # If passed a static list or mocked response
        if isinstance(self.client_or_pages, list):
            return [AWEWorkItem.from_dict(p) if isinstance(p, dict) else p for p in self.client_or_pages]
        # Otherwise query Notion API via callable client
        if callable(self.client_or_pages):
            raw_pages = self.client_or_pages()
            return [AWEWorkItem.from_dict(p) for p in raw_pages]
        return []


class AWEObservationService:
    """Observes tasks from a source and applies canonical execution-slot filtering."""

    def __init__(self, source: TaskSource) -> None:
        self.source = source

    def observe_all(self) -> list[AWEWorkItem]:
        tasks = list(self.source.fetch_tasks())
        tasks.sort(key=lambda x: (x.sequence, x.task_id))
        return tasks

    def filter_eligible(
        self,
        slot: ExecutionSlot | None = None,
        tasks: Sequence[AWEWorkItem] | None = None,
    ) -> list[AWEWorkItem]:
        """Filter tasks eligible for execution under the given slot.

        Rules:
        1. Strict execution-profile matching: (harness, model, effort).
        2. If an exact-slot task is already 'In Progress', return it first (resumption priority).
        3. Ignore other-slot In Progress tasks.
        4. Select lowest-numbered executable Queue task matching exact slot.
        5. Filter out owner_only tasks (they require Owner escalation).
        """
        all_tasks = list(tasks if tasks is not None else self.source.fetch_tasks())

        if slot is None:
            # Return all non-owner Queue or In Progress tasks sorted
            eligible = [
                t for t in all_tasks
                if t.status in (AWEStatus.QUEUE.value, AWEStatus.IN_PROGRESS.value)
                and not t.owner_only
            ]
            eligible.sort(key=lambda x: (0 if x.status == AWEStatus.IN_PROGRESS.value else 1, x.sequence, x.task_id))
            return eligible

        # Slot specified: exact matching
        matching_in_progress = [
            t for t in all_tasks
            if t.status == AWEStatus.IN_PROGRESS.value
            and t.slot.matches(slot)
            and not t.owner_only
        ]
        if matching_in_progress:
            # Priority to resuming existing in-progress task for this slot
            return matching_in_progress

        # Matching queue tasks
        matching_queue = [
            t for t in all_tasks
            if t.status == AWEStatus.QUEUE.value
            and t.slot.matches(slot)
            and not t.owner_only
        ]
        matching_queue.sort(key=lambda x: (x.sequence, x.task_id))
        return matching_queue
