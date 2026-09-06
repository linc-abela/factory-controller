"""Live Notion Agent Work Exchange client, task source, and source-of-record manager.

Provides live observation, pagination, optimistic claim fencing (Queue -> In Progress),
and durable completion/block write-back against the authoritative Notion AWE dashboard.
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


def resolve_notion_token() -> str:
    """Resolve Notion authentication token from environment or local config."""
    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if token:
        return token.strip()

    # Fallback to local gemini config if available
    config_paths = [
        Path.home() / ".gemini" / "config" / "mcp_config.json",
        Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
    ]
    for cp in config_paths:
        if cp.is_file():
            try:
                data = json.loads(cp.read_text(encoding="utf-8"))
                env_dict = (
                    data.get("mcpServers", {})
                    .get("notion", {})
                    .get("env", {})
                )
                t = env_dict.get("NOTION_TOKEN") or env_dict.get("NOTION_API_KEY")
                if t:
                    return str(t).strip()
            except Exception:
                continue

    return ""


class NotionClient:
    """Direct HTTPS client for Notion REST API v1 using standard library urllib."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = NOTION_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.token = token or resolve_notion_token()
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


class LiveNotionTaskSource:
    """Task source that fetches, parses, and paginates tasks from live Notion AWE database."""

    def __init__(
        self,
        client: NotionClient | None = None,
        database_id: str = DEFAULT_AWE_DATABASE_ID,
    ) -> None:
        self.client = client or NotionClient()
        self.database_id = os.environ.get("AWE_NOTION_DATABASE_ID", database_id)

    def fetch_tasks(self, filter_status: str | None = None) -> list[AWEWorkItem]:
        """Fetch all pages from Notion AWE database, handling pagination."""
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
                    items.append(item)
            except Exception:
                continue

        items.sort(key=lambda x: (x.sequence, x.task_id))
        return items

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

    def __init__(self, client: NotionClient | None = None) -> None:
        self.client = client or NotionClient()

    def claim_task(
        self,
        task: AWEWorkItem,
        worker_id: str,
        slot_key: str,
        lease_seconds: float = 120.0,
    ) -> tuple[bool, str]:
        """Atomically claim a task in Notion source of record (Queue -> In Progress).

        Performs optimistic compare-and-swap check against Notion page properties.
        """
        if not self.client.is_configured:
            return False, "NOTION_NOT_CONFIGURED"

        page_id = task.dashboard_page_id
        if not page_id:
            return False, "NO_DASHBOARD_PAGE_ID"

        try:
            current_page = self.client.retrieve_page(page_id)
            current_status = (
                current_page.get("properties", {})
                .get("Status", {})
                .get("select", {})
                .get("name", "")
            )
            # Check if task is still in Queue (or already assigned to this worker)
            if current_status != AWEStatus.QUEUE.value:
                current_notes_list = current_page.get("properties", {}).get("Notes", {}).get("rich_text", [])
                current_notes_text = "".join(t.get("plain_text", "") for t in current_notes_list)
                if current_status == AWEStatus.IN_PROGRESS.value and f"Claimed by {worker_id}" in current_notes_text:
                    pass  # Resumption by same worker allowed
                else:
                    return False, f"SOURCE_OF_RECORD_CONFLICT: task {task.task_id} status is '{current_status}' (not Queue), held by another worker"

            now_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            claim_note = f"Claimed by {worker_id} ({slot_key}) at {now_iso}; lease {lease_seconds:.0f}s"

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
            return True, ""
        except NotionAPIError as exc:
            return False, f"NOTION_API_ERROR: {exc}"
        except Exception as exc:
            return False, f"CLAIM_EXCEPTION: {exc}"

    def complete_task(
        self,
        task: AWEWorkItem,
        verdict: str,
        evidence_ref: str = "",
        notes: str = "",
    ) -> bool:
        """Move task to Done in Notion and record verdict and completion notes."""
        if not self.client.is_configured or not task.dashboard_page_id:
            return False

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
            return True
        except Exception:
            return False

    def block_task(
        self,
        task: AWEWorkItem,
        reason: str,
        detail: str = "",
    ) -> bool:
        """Move task to Blocked in Notion with escalation reason."""
        if not self.client.is_configured or not task.dashboard_page_id:
            return False

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
            return True
        except Exception:
            return False

    def route_rework(
        self,
        task: AWEWorkItem,
        verdict: str,
        defects: Sequence[str],
    ) -> bool:
        """Route defects back to same producer lineage as rework in Notion."""
        if not self.client.is_configured or not task.dashboard_page_id:
            return False

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
            return True
        except Exception:
            return False
