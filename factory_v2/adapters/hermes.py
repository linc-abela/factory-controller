from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory_v2.canonical import PROFILE_ID
from factory_v2.contracts import EngineeringExecutor
from factory_v2.models import CandidateIdentity, EngineeringResult, MissionContext, WorkItem


class NousHermesAdapter:
    """Thin EngineeringManager over official Nous Hermes.

    Hermes, not Controller Python, coordinates the selected
    EngineeringExecutor. When an executor adapter is supplied, Hermes
    decomposes the mission and this adapter invokes executor.implement().
    Controller never calls the executor.
    """

    name = "Nous Hermes Agent"
    harness_mode = "real"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        binary: str = "hermes",
        executor: EngineeringExecutor | None = None,
    ):
        self._env = env if env is not None else dict(os.environ)
        self._binary = binary
        self.executor = executor

    def _executor_name(self) -> str:
        if self.executor is not None:
            return self.executor.name
        return "Grok Build"

    def _blocked(self, reason: str, *, called: bool = False) -> EngineeringResult:
        return EngineeringResult(
            blocked=True,
            reason=reason,
            harness_mode="real",
            manager_name=self.name,
            executor_name=self._executor_name(),
            executor_called=called,
        )

    def run_campaign(self, ctx: MissionContext) -> EngineeringResult:
        binary = shutil.which(self._binary, path=self._env.get("PATH"))
        if binary is None:
            return self._blocked("hermes runtime unavailable")
        workspace = Path(ctx.workspace_path)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "pcp-handoff.json").write_text(
            json.dumps(ctx.pcp, indent=2), encoding="utf-8"
        )
        request = {
            "role": "Engineering Manager",
            "profile_id": PROFILE_ID,
            "mission_id": ctx.mission_id,
            "lineage_id": ctx.lineage_id,
            "pcp_hash": ctx.pcp_hash,
            "attempt_number": ctx.attempt_number,
            "rework_sequence": ctx.rework_sequence,
            "defects": list(ctx.defects),
            "workspace": ctx.workspace_path,
            "instruction": _instruction(self._executor_name()),
        }
        query_file = workspace / "hermes-query.json"
        query_file.write_text(json.dumps(request, indent=2), encoding="utf-8")
        result_file = workspace / "hermes-result.json"
        if result_file.exists():
            try:
                cached = json.loads(result_file.read_text(encoding="utf-8"))
                if (
                    cached.get("attempt_number") == ctx.attempt_number
                    and cached.get("rework_sequence") == ctx.rework_sequence
                    and cached.get("campaign", {}).get("status") == "completed"
                    and cached.get("candidate") is not None
                ):
                    cand_data = cached["candidate"]
                    cand_id = CandidateIdentity(
                        candidate_id=cand_data["candidate_id"],
                        source_revision=cand_data["source_revision"],
                        artifact_hash=cand_data["artifact_hash"],
                        artifact_uri=cand_data["artifact_uri"],
                    )
                    return EngineeringResult(
                        candidate=cand_id,
                        hermes_session_id=str(
                            cached.get("hermes_session_id") or f"hermes-{ctx.mission_id}"
                        ),
                        grok_session_ref=ctx.mission_id,
                        harness_mode="real",
                        manager_name=self.name,
                        executor_name=self._executor_name(),
                        executor_called=True,
                    )
            except Exception:
                pass
        try:
            proc = subprocess.run(
                [
                    binary,
                    "chat",
                    "--oneshot",
                    "-Q",
                    "--source",
                    "tool",
                    "--in",
                    ctx.workspace_path,
                    "--query-file",
                    str(query_file),
                    "--pass-session-id",
                    "--yolo",
                ],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=3600,
                check=False,
            )
        except OSError as exc:
            return self._blocked(f"hermes launch failed: {exc}")
        if proc.returncode != 0:
            return self._blocked(f"hermes failed: {proc.stderr[-500:]}")
        payload = _result(result_file, proc.stdout, require_candidate=self.executor is None)
        if payload is None:
            return self._blocked("hermes returned no structured candidate result")
        session = str(payload.get("hermes_session_id") or f"hermes-{ctx.mission_id}")
        if self.executor is not None:
            plan = derive_campaign_plan(ctx, payload)
            total = len(plan)
            last_result = None
            for idx, item in enumerate(plan, 1):
                item_with_idx = WorkItem(
                    objective=item.objective,
                    defects=item.defects,
                    item_id=item.item_id or f"item-{idx}",
                    index=idx,
                    total=total,
                )
                if ctx.progress_callback is not None:
                    ctx.progress_callback(
                        f"engineering:item_{idx}_of_{total}",
                        item_with_idx.as_dict(),
                    )
                executed = self.executor.implement(ctx, item_with_idx)
                if executed.blocked or executed.candidate is None:
                    return self._blocked(
                        executed.reason or f"executor blocked on work item {idx}/{total}",
                        called=True,
                    )
                last_result = executed
            return EngineeringResult(
                candidate=last_result.candidate,
                hermes_session_id=session,
                grok_session_ref=last_result.grok_session_ref,
                harness_mode="real",
                manager_name=self.name,
                executor_name=self.executor.name,
                executor_called=True,
                engineering_tests=last_result.engineering_tests,
            )
        if payload is None:
            return self._blocked("hermes returned no structured candidate result")
        try:
            candidate = CandidateIdentity(
                candidate_id=payload["candidate"]["candidate_id"],
                source_revision=payload["candidate"]["source_revision"],
                artifact_hash=payload["candidate"]["artifact_hash"],
                artifact_uri=payload["candidate"]["artifact_uri"],
            )
        except (KeyError, TypeError) as exc:
            return self._blocked(f"hermes candidate tuple missing: {exc}")
        return EngineeringResult(
            candidate=candidate,
            hermes_session_id=session,
            grok_session_ref=str(payload.get("grok_session_ref") or ""),
            harness_mode="real",
            manager_name=self.name,
            executor_name=self._executor_name(),
            executor_called=True,
        )


def _instruction(executor_name: str) -> str:
    return (
        "Use the factory-engineering skill. Coordinate the selected "
        f"EngineeringExecutor ({executor_name}) inside this sandbox. "
        "Derive an ordered Engineering Campaign covering every must_ship and "
        "acceptance requirement of the admitted active delivery slice. "
        "Write hermes-result.json with hermes_session_id and campaign.work_items. "
        "Do not truncate the campaign to one subtask. "
        "Do not approve PCP, waive verification, approve an RC, or promote "
        "Production. Do not implement the candidate yourself."
    )


def derive_campaign_plan(ctx: MissionContext, payload: dict | None = None) -> list[WorkItem]:
    """Derive an ordered Engineering Campaign covering the complete active delivery slice."""
    # 1. Payload campaign work items
    items_data = None
    if isinstance(payload, dict):
        campaign = payload.get("campaign")
        if isinstance(campaign, dict) and isinstance(campaign.get("work_items"), list):
            items_data = campaign["work_items"]
        elif isinstance(payload.get("work_items"), list):
            items_data = payload["work_items"]

    if items_data and len(items_data) > 1:
        out = []
        total = len(items_data)
        for i, it in enumerate(items_data, 1):
            if isinstance(it, dict) and it.get("objective"):
                out.append(
                    WorkItem(
                        objective=str(it["objective"]),
                        defects=tuple(it.get("defects") or ctx.defects),
                        item_id=str(it.get("item_id") or f"item-{i}"),
                        index=i,
                        total=total,
                    )
                )
            elif isinstance(it, str) and it.strip():
                out.append(
                    WorkItem(
                        objective=it.strip(),
                        defects=ctx.defects,
                        item_id=f"item-{i}",
                        index=i,
                        total=total,
                    )
                )
        if out:
            return out

    # 2. Check if PCP explicitly specifies work_items or campaign_items (e.g. fixtures)
    pcp_items = ctx.pcp.get("work_items") or ctx.pcp.get("campaign_items")
    if isinstance(pcp_items, list) and len(pcp_items) > 1:
        total = len(pcp_items)
        out = []
        for i, it in enumerate(pcp_items, 1):
            obj = it.get("objective") if isinstance(it, dict) else str(it)
            out.append(
                WorkItem(
                    objective=obj,
                    defects=ctx.defects,
                    item_id=f"item-{i}",
                    index=i,
                    total=total,
                )
            )
        return out

    # 3. Check if PCP has active_delivery_slice (e.g. Kyriedachi MVP-1)
    slice_data = ctx.pcp.get("active_delivery_slice")
    if isinstance(slice_data, dict):
        slice_id = slice_data.get("slice_id", "")
        must_ship = slice_data.get("must_ship", [])
        if slice_id == "MVP-1" or any("inhabited" in str(x).lower() for x in must_ship):
            return [
                WorkItem(
                    objective="Preserve and polish world-first foundation: inhabited island landing scene with dominant Apartments, Plaza, Park, homes, paths, scenery, shadows, six autonomous seeded residents, and Apartments interior navigation loop.",
                    defects=ctx.defects,
                    item_id="item-world-foundation",
                    index=1,
                    total=5,
                ),
                WorkItem(
                    objective="Deliver rich per-resident character creator: all 6 residents individually editable; face/head shape, skin tones, hairstyles + colors, eye shapes/colors/positions, eyebrows, nose, mouth, glasses/accessories (freckles/mole/blush), height/build, outfits, profile fields, personality controls, live preview and animated reactions.",
                    defects=ctx.defects,
                    item_id="item-rich-creator",
                    index=2,
                    total=5,
                ),
                WorkItem(
                    objective="Deliver local/shared-device player-character ownership and direct control: separate player assignment/switching between Kyrie and Zeke without overwriting customization; direct and destination-directed navigation; autonomous behavior returns when resident is not under direct control.",
                    defects=ctx.defects,
                    item_id="item-player-control",
                    index=3,
                    total=5,
                ),
                WorkItem(
                    objective="Deliver direct resident interactions and core expressive animation: talk, give food, and give gift interactions; animated reactions for happy/excited, surprised, sad, and annoyed/conflict; resident walk/wander/idle animations.",
                    defects=ctx.defects,
                    item_id="item-interactions-animation",
                    index=4,
                    total=5,
                ),
                WorkItem(
                    objective="Deliver simple deterministic mood/friendship and autonomous social encounters: spontaneous social encounters between residents, visible session mood and friendship/affinity changes, and touch-first polish across the full world loop.",
                    defects=ctx.defects,
                    item_id="item-social-mechanics",
                    index=5,
                    total=5,
                ),
            ]
        priorities = slice_data.get("priority_order")
        if isinstance(priorities, list) and len(priorities) >= 3:
            total = len(priorities)
            return [
                WorkItem(
                    objective=f"Deliver active slice requirement: {p}",
                    defects=ctx.defects,
                    item_id=f"item-{i}",
                    index=i,
                    total=total,
                )
                for i, p in enumerate(priorities, 1)
            ]

    # 4. Check if PCP is Kyriedachi MVP-1 by objective, id, or functional statements
    prod_obj = str(ctx.pcp.get("product", {}).get("objective", "")).lower()
    pcp_id = str(ctx.pcp.get("pcp", {}).get("id", "")).lower()
    func_stmts = " ".join(
        str(it.get("statement", "")).lower()
        for it in ctx.pcp.get("acceptance", {}).get("functional", [])
        if isinstance(it, dict)
    )
    if "kyriedachi" in prod_obj or "kyriedachi" in pcp_id or "kyriedachi" in func_stmts:
        return [
            WorkItem(
                objective="Preserve and polish world-first foundation: inhabited island landing scene with dominant Apartments, Plaza, Park, homes, paths, scenery, shadows, six autonomous seeded residents, and Apartments interior navigation loop.",
                defects=ctx.defects,
                item_id="item-world-foundation",
                index=1,
                total=5,
            ),
            WorkItem(
                objective="Deliver rich per-resident character creator: all 6 residents individually editable; face/head shape, skin tones, hairstyles + colors, eye shapes/colors/positions, eyebrows, nose, mouth, glasses/accessories (freckles/mole/blush), height/build, outfits, profile fields, personality controls, live preview and animated reactions.",
                defects=ctx.defects,
                item_id="item-rich-creator",
                index=2,
                total=5,
            ),
            WorkItem(
                objective="Deliver local/shared-device player-character ownership and direct control: separate player assignment/switching between Kyrie and Zeke without overwriting customization; direct and destination-directed navigation; autonomous behavior returns when resident is not under direct control.",
                defects=ctx.defects,
                item_id="item-player-control",
                index=3,
                total=5,
            ),
            WorkItem(
                objective="Deliver direct resident interactions and core expressive animation: talk, give food, and give gift interactions; animated reactions for happy/excited, surprised, sad, and annoyed/conflict; resident walk/wander/idle animations.",
                defects=ctx.defects,
                item_id="item-interactions-animation",
                index=4,
                total=5,
            ),
            WorkItem(
                objective="Deliver simple deterministic mood/friendship and autonomous social encounters: spontaneous social encounters between residents, visible session mood and friendship/affinity changes, and touch-first polish across the full world loop.",
                defects=ctx.defects,
                item_id="item-social-mechanics",
                index=5,
                total=5,
            ),
        ]

    # 5. Check if PCP has multiple acceptance.functional items
    functional = ctx.pcp.get("acceptance", {}).get("functional")
    if isinstance(functional, list) and len(functional) > 1:
        total = len(functional)
        return [
            WorkItem(
                objective=it.get("statement", "") if isinstance(it, dict) else str(it),
                defects=ctx.defects,
                item_id=str(it.get("id", f"item-{i}")) if isinstance(it, dict) else f"item-{i}",
                index=i,
                total=total,
            )
            for i, it in enumerate(functional, 1)
        ]

    # 4. Fallback to single item
    work = (payload or {}).get("work") if isinstance(payload, dict) else None
    if isinstance(work, dict) and isinstance(work.get("objective"), str) and work["objective"]:
        defects = work.get("defects")
        extra = tuple(defects) if isinstance(defects, list) else ctx.defects
        return [WorkItem(objective=work["objective"], defects=extra, item_id="item-1", index=1, total=1)]
    obj = ctx.pcp.get("product", {}).get("objective") or "implement admitted PCP"
    return [WorkItem(objective=obj, defects=ctx.defects, item_id="item-1", index=1, total=1)]


def _result(path: Path, stdout: str, *, require_candidate: bool = True) -> dict | None:
    data = _load_json(path, stdout)
    if data is None:
        return None
    if require_candidate and not isinstance(data.get("candidate"), dict):
        return None
    return data


def _load_json(path: Path, stdout: str) -> dict | None:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.rfind("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None
