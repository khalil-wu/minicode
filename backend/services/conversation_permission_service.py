from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.config import get_config_requirements
from backend.config_requirements import RequirementViolation
from backend.ws.utils import normalize_permission_mode


@dataclass(frozen=True)
class PermissionModePlan:
    requested: str
    source: str
    conversation_id: str
    session_only: bool
    error_event: AgentEvent | None = None
    auto_approve_reason: str = ""
    only_auto_allowed: bool = False


def plan_permission_mode_update(
    data: dict[str, Any],
    *,
    active_conversation_id: str = "",
) -> PermissionModePlan:
    requested = normalize_permission_mode(str(data.get("mode") or data.get("permission_mode") or ""))
    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if requested is None:
        return PermissionModePlan(
            requested="",
            source=source,
            conversation_id="",
            session_only=False,
            error_event=AgentEvent.error(
                "Invalid permission mode. Use one of: plan, confirm, auto, bypass.",
                recoverable=True,
                error_type="tool",
            ),
        )

    try:
        get_config_requirements().ensure_permission_mode(requested)
    except RequirementViolation as exc:
        return PermissionModePlan(
            requested=requested,
            source=source,
            conversation_id="",
            session_only=False,
            error_event=AgentEvent.error(
                str(exc),
                recoverable=True,
                error_type="tool",
            ),
        )

    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    active_id = str(active_conversation_id or "").strip()
    if not explicit_conversation_id and not active_id:
        return PermissionModePlan(
            requested=requested,
            source=source,
            conversation_id="",
            session_only=True,
        )

    conversation_id = str(explicit_conversation_id or active_id).strip()
    if not conversation_id:
        return PermissionModePlan(
            requested=requested,
            source=source,
            conversation_id="",
            session_only=False,
            error_event=AgentEvent.error("No active conversation to update", recoverable=True, error_type="tool"),
        )

    auto_approve_reason = ""
    only_auto_allowed = False
    if requested == "bypass":
        auto_approve_reason = "permission_mode_bypass"
    elif requested == "auto":
        auto_approve_reason = "permission_mode_auto"
        only_auto_allowed = True
    return PermissionModePlan(
        requested=requested,
        source=source,
        conversation_id=conversation_id,
        session_only=False,
        auto_approve_reason=auto_approve_reason,
        only_auto_allowed=only_auto_allowed,
    )
