"""Live Notion Agent Work Exchange client, task source, and source-of-record manager.

Provides live observation, pagination, and fail-closed physical AWE plus
dashboard reconciliation after the Controller-owned claim fence.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import AWEStatus, AWEWorkItem, ExecutionSlot

DEFAULT_AWE_DATABASE_ID = "91e1d4f0-9caf-4eae-9978-574f0af0930f"
NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"


class NotionAPIError(RuntimeError):
    """Exception raised for errors in Notion API interactions."""

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(f"Notion API error {status_code}: {message} ({body[:200]})")
        self.status_code = status_code
        self.message = message
        self.body = body


def resolve_notion_token(token: str | None = None) -> str:
    """Resolve Notion authentication token strictly from environment or argument.

    Never inspects or scavenges another agent harness's configuration files,
    tokens, or credential stores.
    """
    if token and token.strip():
        return token.strip()
    env_token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if env_token and env_token.strip():
        return env_token.strip()
    return ""


AWE_ROOT_PAGE_ID = "3c5690f6-eb14-81cc-810e-e9ffaa1dc3e5"
AWE_PROCESSED_PAGE_ID = "3c5690f6-eb14-814f-8623-e5834cddc247"

AWE_LANE_FOLDERS: dict[str, dict[str, str]] = {
    "antigravity": {
        "lane_id": "3c5690f6-eb14-8153-8e0d-db7cc6d2120a",
        "queue": "3c5690f6-eb14-815c-a767-d6952b58f0de",
        "in_progress": "3c5690f6-eb14-817e-ba22-f57ea996fec0",
        "blocked": "3c5690f6-eb14-8180-8d85-e791738e1d45",
        "done": "3c5690f6-eb14-81b7-b578-c9fad99c2e6a",
    },
    "codex": {
        "lane_id": "3c5690f6-eb14-8156-bada-f5ce9ac2cdda",
        "queue": "3c5690f6-eb14-8150-9704-e986d213f4f7",
        "in_progress": "3c5690f6-eb14-818f-a036-e1a2634f5408",
        "blocked": "3c5690f6-eb14-81ba-baf3-fe26ac7e8d83",
        "done": "3c5690f6-eb14-8169-82e1-fb99cb96f474",
    },
    "cursor": {
        "lane_id": "3d1690f6-eb14-810d-808d-d7f11667ddca",
        "queue": "3d1690f6-eb14-819a-88b4-c0c020d0f75b",
        "in_progress": "3d1690f6-eb14-8162-aadd-f7084de14b29",
        "blocked": "3d1690f6-eb14-8186-905d-fbc76be99deb",
        "done": "3d1690f6-eb14-8159-9acb-fb86a03bdd0f",
    },
    "claude": {
        "lane_id": "3c5690f6-eb14-81af-ab59-da2f4aa94c23",
        "queue": "3c5690f6-eb14-81a2-83c7-d68206398dea",
        "in_progress": "3c5690f6-eb14-816c-b8d4-d0af7827e801",
        "blocked": "3c5690f6-eb14-8189-be89-ec802c22fdc3",
        "done": "3c5690f6-eb14-8124-9b14-d1dec66edd6c",
    },
}


def resolve_physical_folder_status(parent_id: str, lane: str = "") -> str | None:
    """Resolve canonical physical folder lifecycle status from parent page ID.

    Returns one of: "Queue", "In Progress", "Blocked", "Done", "Processed", or None.
    """
    clean_id = parent_id.replace("-", "").lower()
    if clean_id == AWE_PROCESSED_PAGE_ID.replace("-", "").lower():
        return AWEStatus.PROCESSED.value

    for l_name, folders in AWE_LANE_FOLDERS.items():
        if lane and l_name != lane.lower():
            continue
        for status_key, folder_id in folders.items():
            if status_key == "lane_id":
                continue
            if clean_id == folder_id.replace("-", "").lower():
                if status_key == "queue":
                    return AWEStatus.QUEUE.value
                elif status_key == "in_progress":
                    return AWEStatus.IN_PROGRESS.value
                elif status_key == "blocked":
                    return AWEStatus.BLOCKED.value
                elif status_key == "done":
                    return AWEStatus.DONE.value
    return None


def get_lane_folder_id(lane: str, status_name: str) -> str | None:
    """Get physical folder UUID for a given harness lane and lifecycle status."""
    l_key = lane.lower() if lane else "antigravity"
    folders = AWE_LANE_FOLDERS.get(l_key)
    if not folders:
        folders = AWE_LANE_FOLDERS.get("antigravity", {})
    key_map = {
        AWEStatus.QUEUE.value.lower(): "queue",
        AWEStatus.IN_PROGRESS.value.lower(): "in_progress",
        "in progress": "in_progress",
        AWEStatus.BLOCKED.value.lower(): "blocked",
        AWEStatus.DONE.value.lower(): "done",
        AWEStatus.PROCESSED.value.lower(): "processed",
        "queue": "queue",
        "in_progress": "in_progress",
        "blocked": "blocked",
        "done": "done",
        "processed": "processed",
    }
    lookup = key_map.get(status_name.lower())
    if lookup == "processed":
        return AWE_PROCESSED_PAGE_ID
    return folders.get(lookup) if lookup else None


class NotionClient:
    """Direct HTTPS client for Notion REST API v1 using standard library urllib."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = NOTION_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.token = resolve_notion_token(token)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, Any] | None = None,
        headers_extra: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            raise NotionAPIError(401, "Notion token not configured in environment or config")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data_bytes = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_API_VERSION,
            "User-Agent": "software-factory-awe-worker/1.0",
        }
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if headers_extra:
            headers.update(headers_extra)

        req = urllib.request.Request(
            url=url,
            data=data_bytes,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                if not resp_bytes:
                    return {}
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise NotionAPIError(exc.code, exc.reason, body) from exc
        except urllib.error.URLError as exc:
            raise NotionAPIError(503, str(exc.reason)) from exc

    def query_database(
        self,
        database_id: str,
        filter_dict: Mapping[str, Any] | None = None,
        sorts: Sequence[Mapping[str, Any]] | None = None,
        page_size: int = 100,
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"page_size": min(page_size, 100)}
        if filter_dict:
            payload["filter"] = filter_dict
        if sorts:
            payload["sorts"] = list(sorts)
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", f"databases/{database_id}/query", payload)

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"pages/{page_id}")

    def update_page(
        self,
        page_id: str,
        properties: Mapping[str, Any],
        in_trash: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"properties": properties}
        if in_trash is not None:
            payload["in_trash"] = in_trash
        return self._request("PATCH", f"pages/{page_id}", payload)

    def create_page(
        self,
        parent: Mapping[str, Any],
        properties: Mapping[str, Any],
        children: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            payload["children"] = list(children)
        return self._request("POST", "pages", payload)

    def move_page(
        self,
        page_id: str,
        parent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Move a page to a new parent location via POST /v1/pages/{page_id}/move."""
        return self._request("POST", f"pages/{page_id}/move", {"parent": parent})

    def retrieve_block_children(
        self,
        block_id: str,
        page_size: int = 100,
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        endpoint = f"blocks/{block_id}/children?page_size={min(page_size, 100)}"
        if start_cursor:
            endpoint += f"&start_cursor={urllib.parse.quote(start_cursor)}"
        return self._request("GET", endpoint)

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return self._request("PATCH", f"blocks/{block_id}/children", {"children": list(children)})

    def delete_block(self, block_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"blocks/{block_id}")


class LiveNotionTaskSource:
    """Task source that fetches, parses, and paginates tasks from live Notion AWE database."""

    def __init__(
        self,
        client: NotionClient | None = None,
        database_id: str = DEFAULT_AWE_DATABASE_ID,
    ) -> None:
        self.client = client or NotionClient()
        self.database_id = os.environ.get("AWE_NOTION_DATABASE_ID", database_id)

    def verify_physical_status(self, task: AWEWorkItem) -> AWEWorkItem:
        """Bind canonical physical lifecycle status from the task page's parent folder."""
        if not self.client.is_configured or not task.task_page_id:
            return task

        try:
            page_obj = self.client.retrieve_page(task.task_page_id)
            parent_info = page_obj.get("parent", {})
            if parent_info.get("type") == "page_id":
                parent_page_id = parent_info.get("page_id", "")
                phys_status = resolve_physical_folder_status(parent_page_id, lane=task.lane)
                if phys_status and phys_status != task.status:
                    import dataclasses
                    return dataclasses.replace(task, status=phys_status)
        except Exception:
            pass
        return task

    def fetch_tasks(self, filter_status: str | None = None) -> list[AWEWorkItem]:
        """Fetch all pages from Notion AWE database, handling pagination and physical ancestry."""
        if not self.client.is_configured:
            return []

        all_rows: list[dict[str, Any]] = []
        cursor: str | None = None
        filter_dict: dict[str, Any] | None = None

        if filter_status:
            filter_dict = {
                "property": "Status",
                "select": {"equals": filter_status},
            }

        while True:
            resp = self.client.query_database(
                database_id=self.database_id,
                filter_dict=filter_dict,
                page_size=100,
                start_cursor=cursor,
            )
            results = resp.get("results", [])
            all_rows.extend(results)
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]

        items: list[AWEWorkItem] = []
        for row in all_rows:
            try:
                item = self._parse_row_to_work_item(row)
                if item:
                    # Canonical truth: verify physical folder ancestry
                    item = self.verify_physical_status(item)
                    if filter_status and item.status.lower() != filter_status.lower():
                        continue
                    items.append(item)
            except Exception:
                continue

        items.sort(key=lambda x: (x.sequence, x.task_id))
        return items

    def fetch_tasks_from_physical_folder(
        self,
        lane: str,
        folder_name: str = "queue",
    ) -> list[dict[str, Any]]:
        """Fetch child task pages physically residing in a specific lane folder."""
        if not self.client.is_configured:
            return []
        folder_id = get_lane_folder_id(lane, folder_name)
        if not folder_id:
            return []

        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            resp = self.client.retrieve_block_children(
                folder_id, page_size=100, start_cursor=cursor
            )
            for b in resp.get("results", []):
                if b.get("type") == "child_page":
                    results.append({
                        "page_id": b.get("id"),
                        "title": b.get("child_page", {}).get("title", ""),
                        "parent_id": folder_id,
                        "lane": lane,
                        "folder": folder_name,
                    })
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]
        return results

    def _parse_row_to_work_item(self, row: dict[str, Any]) -> AWEWorkItem | None:
        props = row.get("properties", {})
        row_id = row.get("id", "")

        # Extract Task ID
        task_id = ""
        task_id_rich = props.get("Task ID", {}).get("rich_text", [])
        if task_id_rich:
            task_id = task_id_rich[0].get("plain_text", "").strip()

        # Extract Title
        title = ""
        title_list = props.get("Task", {}).get("title", [])
        if title_list:
            title = title_list[0].get("plain_text", "").strip()

        if not task_id:
            match = re.match(r"(SF-\d+)", title)
            if match:
                task_id = match.group(1)
            else:
                task_id = row_id[:8]

        # Extract Status
        status = props.get("Status", {}).get("select", {})
        status_name = status.get("name", AWEStatus.QUEUE.value) if status else AWEStatus.QUEUE.value

        # Extract Role
        role_obj = props.get("Role", {}).get("select", {})
        role_name = role_obj.get("name", "") if role_obj else ""

        # Extract Lane
        lane_obj = props.get("Lane", {}).get("select", {})
        lane_name = lane_obj.get("name", "") if lane_obj else ""

        # Extract Effort
        effort_obj = props.get("Effort", {}).get("select", {})
        effort_name = effort_obj.get("name", "medium").lower() if effort_obj else "medium"

        # Extract Model from "Model / Effort" rich text
        model_str = ""
        model_rich = props.get("Model / Effort", {}).get("rich_text", [])
        if model_rich:
            me_text = model_rich[0].get("plain_text", "")
            # e.g. "Gemini 3.8 Flash / High" -> "gemini-3.8-flash"
            parts = me_text.split("/")
            if parts:
                model_str = parts[0].strip()
                if len(parts) > 1 and not effort_name:
                    effort_name = parts[1].strip().lower()

        # Extract Task Page URL
        task_page_url = props.get("Task Page", {}).get("url") or ""
        task_page_id = ""
        if task_page_url:
            # Extract uuid hex from url e.g. https://app.notion.com/p/3d3690f6eb14814b979aecb3471d2788
            m = re.search(r"([0-9a-f]{32})", task_page_url.replace("-", ""))
            if m:
                h = m.group(1)
                task_page_id = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

        # Extract Notes
        notes = ""
        notes_rich = props.get("Notes", {}).get("rich_text", [])
        if notes_rich:
            notes = "".join(t.get("plain_text", "") for t in notes_rich)

        # Extract Verdict
        verdict = ""
        verdict_rich = props.get("Verdict", {}).get("rich_text", [])
        if verdict_rich:
            verdict = "".join(t.get("plain_text", "") for t in verdict_rich)

        # Extract Needs Owner / Current
        owner_only = bool(props.get("Needs Owner", {}).get("checkbox", False))
        current = bool(props.get("Current", {}).get("checkbox", False))

        # Extract sequence number
        seq_match = re.search(r"\d+", task_id)
        sequence = int(seq_match.group(0)) if seq_match else 999999

        return AWEWorkItem(
            task_id=task_id,
            title=title or f"Task {task_id}",
            lane=lane_name,
            role=role_name,
            status=status_name,
            model=model_str,
            effort=effort_name,
            sequence=sequence,
            task_page_url=task_page_url,
            task_page_id=task_page_id,
            dashboard_page_id=row_id,
            notes=notes,
            verdict=verdict,
            owner_only=owner_only,
            owner_reason="needs_owner_flag" if owner_only else "not_applicable",
            current=current,
        )


class NotionSourceOfRecord:
    """Manages authoritative task lifecycle transitions and write-back in Notion AWE."""

    def __init__(self, client: NotionClient | None = None, dispatch: Any = None) -> None:
        self.client = client or NotionClient()
        if dispatch is None:
            from .dispatch import DispatchMaintainer
            dispatch = DispatchMaintainer(client=self.client)
        self.dispatch = dispatch

    def claim_task(
        self,
        task: AWEWorkItem,
        worker_id: str,
        slot_key: str,
        lease_seconds: float = 120.0,
    ) -> tuple[bool, str]:
        """Fail-closed physical AWE + dashboard reconciliation (Queue -> In Progress).

        Canonical lifecycle truth is the task page's physical folder ancestry.
        This is not Notion atomic CAS and not one ACID transaction across Notion:
        1. Verify the physical task page is currently in the lane's Queue folder.
        2. Move the physical task page to the lane's In Progress folder.
        3. Reconcile the dashboard projection row. If either Notion write fails,
           return an error so the Controller fence can roll back the local lease.
        """
        if not self.client.is_configured:
            return False, "NOTION_NOT_CONFIGURED"

        lane_key = task.lane.lower() if task.lane else "antigravity"
        expected_queue_id = get_lane_folder_id(lane_key, "queue")
        target_in_progress_id = get_lane_folder_id(lane_key, "in_progress")

        # 1. Verify physical task page ancestry
        if task.task_page_id and expected_queue_id:
            try:
                page_obj = self.client.retrieve_page(task.task_page_id)
                parent_info = page_obj.get("parent", {})
                parent_page_id = parent_info.get("page_id", "") if parent_info.get("type") == "page_id" else ""
                clean_parent = parent_page_id.replace("-", "").lower()
                clean_queue = expected_queue_id.replace("-", "").lower()
                clean_in_prog = target_in_progress_id.replace("-", "").lower() if target_in_progress_id else ""

                if clean_parent == clean_in_prog:
                    # Resumption allowed if already In Progress
                    pass
                elif clean_parent != clean_queue:
                    actual_status = resolve_physical_folder_status(parent_page_id, lane=lane_key) or "Unknown"
                    return False, f"PHYSICAL_ANCESTRY_CONFLICT: task {task.task_id} page is physically in '{actual_status}' ({parent_page_id}), not Queue"
            except NotionAPIError as exc:
                return False, f"NOTION_API_ERROR checking physical page: {exc}"

        # 2. Check dashboard row state
        page_id = task.dashboard_page_id
        if page_id:
            try:
                current_page = self.client.retrieve_page(page_id)
                current_status = (
                    current_page.get("properties", {})
                    .get("Status", {})
                    .get("select", {})
                    .get("name", "")
                )
                if current_status != AWEStatus.QUEUE.value:
                    current_notes_list = current_page.get("properties", {}).get("Notes", {}).get("rich_text", [])
                    current_notes_text = "".join(t.get("plain_text", "") for t in current_notes_list)
                    if current_status == AWEStatus.IN_PROGRESS.value and f"Claimed by {worker_id}" in current_notes_text:
                        pass  # Resumption by same worker allowed
                    else:
                        return False, f"SOURCE_OF_RECORD_CONFLICT: task {task.task_id} dashboard status is '{current_status}' (not Queue), held by another worker"
            except NotionAPIError as exc:
                return False, f"NOTION_API_ERROR checking dashboard page: {exc}"

        # 3. Execute physical move to In Progress folder
        if task.task_page_id and target_in_progress_id:
            move_err = self._move_physical(task.task_page_id, target_in_progress_id)
            if move_err:
                return False, move_err

        # 4. Reconcile dashboard projection row
        now_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        claim_note = f"Claimed by {worker_id} ({slot_key}) at {now_iso}; lease {lease_seconds:.0f}s"
        if page_id:
            try:
                self.client.update_page(
                    page_id=page_id,
                    properties={
                        "Status": {"select": {"name": AWEStatus.IN_PROGRESS.value}},
                        "Current": {"checkbox": True},
                        "Notes": {
                            "rich_text": [
                                {"type": "text", "text": {"content": claim_note[:2000]}}
                            ]
                        },
                    },
                )
            except NotionAPIError as exc:
                return False, f"NOTION_API_ERROR updating dashboard projection: {exc}"

        dispatch_err = self._write_dispatch(task, AWEStatus.IN_PROGRESS.value)
        if dispatch_err:
            return False, dispatch_err
        return True, ""

    def _write_dispatch(
        self,
        task: AWEWorkItem,
        lifecycle_state: str,
        dispatch_state: str = "EXECUTABLE",
        frozen_head: str = "",
    ) -> str:
        if self.dispatch is None:
            return ""
        return self.dispatch.write_for_task(
            task,
            lifecycle_state=lifecycle_state,
            dispatch_state=dispatch_state,
            frozen_head=frozen_head,
        )

    def _move_physical(self, task_page_id: str, folder_id: str) -> str:
        """Move a physical AWE page. Returns empty on success, error text on failure."""
        try:
            self.client.move_page(
                task_page_id,
                parent={"type": "page_id", "page_id": folder_id},
            )
            return ""
        except NotionAPIError as exc:
            return f"NOTION_API_ERROR moving task page: {exc}"

    def complete_task(
        self,
        task: AWEWorkItem,
        verdict: str,
        evidence_ref: str = "",
        notes: str = "",
    ) -> bool:
        """Move task to Done: physical folder first, then dashboard projection.

        Fail-closed: if the physical move fails, do not update the dashboard.
        """
        if not self.client.is_configured:
            return False

        lane_key = task.lane.lower() if task.lane else "antigravity"
        done_folder_id = get_lane_folder_id(lane_key, "done")

        if task.task_page_id and done_folder_id:
            if self._move_physical(task.task_page_id, done_folder_id):
                return False

        # 2. Update dashboard projection row
        if task.dashboard_page_id:
            now_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            full_note = notes or f"Completed by worker. Evidence: {evidence_ref}"
            try:
                self.client.update_page(
                    page_id=task.dashboard_page_id,
                    properties={
                        "Status": {"select": {"name": AWEStatus.DONE.value}},
                        "Verdict": {
                            "rich_text": [
                                {"type": "text", "text": {"content": verdict[:2000]}}
                            ]
                        },
                        "Notes": {
                            "rich_text": [
                                {"type": "text", "text": {"content": full_note[:2000]}}
                            ]
                        },
                        "Last Checkpoint": {"date": {"start": now_iso}},
                    },
                )
            except Exception:
                return False
        return not self._write_dispatch(
            task, AWEStatus.DONE.value, dispatch_state="NO_EXECUTABLE_TASK"
        )

    def block_task(
        self,
        task: AWEWorkItem,
        reason: str,
        detail: str = "",
    ) -> bool:
        """Move task to Blocked: physical folder first, then dashboard projection.

        Fail-closed: if the physical move fails, do not update the dashboard.
        """
        if not self.client.is_configured:
            return False

        lane_key = task.lane.lower() if task.lane else "antigravity"
        blocked_folder_id = get_lane_folder_id(lane_key, "blocked")

        if task.task_page_id and blocked_folder_id:
            if self._move_physical(task.task_page_id, blocked_folder_id):
                return False

        # 2. Update dashboard projection row
        if task.dashboard_page_id:
            block_note = f"BLOCKED: {reason} — {detail}"
            try:
                self.client.update_page(
                    page_id=task.dashboard_page_id,
                    properties={
                        "Status": {"select": {"name": AWEStatus.BLOCKED.value}},
                        "Needs Owner": {"checkbox": True},
                        "Notes": {
                            "rich_text": [
                                {"type": "text", "text": {"content": block_note[:2000]}}
                            ]
                        },
                    },
                )
            except Exception:
                return False
        return not self._write_dispatch(
            task, AWEStatus.BLOCKED.value, dispatch_state="NO_EXECUTABLE_TASK"
        )

    def route_rework(
        self,
        task: AWEWorkItem,
        verdict: str,
        defects: Sequence[str],
    ) -> bool:
        """Route rework to the same producer lineage: physical Queue, then dashboard.

        Fail-closed: if the physical move fails, do not update the dashboard.
        """
        if not self.client.is_configured:
            return False

        lane_key = task.lane.lower() if task.lane else "antigravity"
        queue_folder_id = get_lane_folder_id(lane_key, "queue")

        if task.task_page_id and queue_folder_id:
            if self._move_physical(task.task_page_id, queue_folder_id):
                return False

        # 2. Update dashboard projection row
        if task.dashboard_page_id:
            defect_summary = "; ".join(defects) if defects else "Rework required by certifier"
            rework_note = f"REWORK_REQUIRED: {defect_summary}"
            try:
                self.client.update_page(
                    page_id=task.dashboard_page_id,
                    properties={
                        "Status": {"select": {"name": AWEStatus.QUEUE.value}},
                        "Verdict": {
                            "rich_text": [
                                {"type": "text", "text": {"content": verdict[:2000]}}
                            ]
                        },
                        "Notes": {
                            "rich_text": [
                                {"type": "text", "text": {"content": rework_note[:2000]}}
                            ]
                        },
                    },
                )
            except Exception:
                return False
        return not self._write_dispatch(task, AWEStatus.QUEUE.value)
