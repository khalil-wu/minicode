from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.config import AppConfig, get_config_requirements, load_config
from backend.permissions.approval_policy import policy_for_mode
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


def plan_permission_mode_update(
    data: dict[str, Any],
    *,
    active_conversation_id: str = "",
    config: AppConfig | None = None,
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
        effective_config = config if config is not None else load_config(resolve_credentials=False)
        requirements = effective_config.config_layer_stack.requirements if effective_config.config_layer_stack is not None else get_config_requirements()
        requirements.ensure_permission_mode(requested, approval_policy=policy_for_mode(requested, effective_config.permissions.approval_policy))
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

    auto_approve_reason = ""
    if requested == "bypass":
        auto_approve_reason = "permission_mode_bypass"
    elif requested == "auto":
        auto_approve_reason = "permission_mode_auto"
    return PermissionModePlan(
        requested=requested,
        source=source,
        conversation_id=conversation_id,
        session_only=False,
        auto_approve_reason=auto_approve_reason,
    )
