"""Owner escalation gate for nondelegable authorities and policy boundaries."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .model import AWEWorkItem, EscalationReport

NONDELEGABLE_REASONS = (
    "production_promotion",
    "protected_disclosure",
    "billing",
    "destructive_history",
    "product_scope",
    "owner_judgment",
)

ESCALATION_KEYWORDS = {
    "production_promotion": ["deploy to production", "live release", "release gate", "production custody"],
    "protected_disclosure": ["rotate credential", "export api key", "delete secret", "password reset"],
    "billing": ["update billing", "change subscription", "credit card", "increase spend"],
    "destructive_history": ["drop database", "wipe database", "delete repository", "force push main"],
}


class OwnerEscalationGate:
    """Detects and enforces strict human review gates."""

    def check_escalation(
        self,
        task: AWEWorkItem,
        context: Mapping[str, Any] | None = None,
    ) -> EscalationReport:
        """Evaluate if the task requires explicit Owner authority."""
        # 1. Explicit owner_only flag on task
        if task.owner_only:
            reason = task.owner_reason if task.owner_reason in NONDELEGABLE_REASONS else "owner_judgment"
            return EscalationReport(
                escalated=True,
                task_id=task.task_id,
                reason_code=reason,
                detail=f"Task {task.task_id} explicitly marked owner_only: {task.notes or reason}",
                action_required=f"Owner manual review and execution required for category '{reason}'",
            )

        # 2. Check title / markdown body for nondelegable operations
        text = f"{task.title} {task.notes} {task.body_markdown}".lower()
        for reason, keywords in ESCALATION_KEYWORDS.items():
            for kw in keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", text):
                    return EscalationReport(
                        escalated=True,
                        task_id=task.task_id,
                        reason_code=reason,
                        detail=f"Task matches nondelegable keyword '{kw}' under category '{reason}'",
                        action_required=f"Strict human review gate: {reason} cannot be executed autonomously",
                    )

        # 3. Context flags
        if context:
            if context.get("requires_live_production"):
                return EscalationReport(
                    escalated=True,
                    task_id=task.task_id,
                    reason_code="production_promotion",
                    detail="Execution context requires live production release authority",
                    action_required="Owner authorization required for production promotion",
                )
            if context.get("destructive_operation"):
                return EscalationReport(
                    escalated=True,
                    task_id=task.task_id,
                    reason_code="destructive_history",
                    detail="Context signals destructive infrastructure or data operation",
                    action_required="Owner approval required for irreversible destructive operation",
                )

        return EscalationReport(
            escalated=False,
            task_id=task.task_id,
        )
