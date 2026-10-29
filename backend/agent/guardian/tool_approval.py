"""Frozen execution requests entering the Source approval reviewer."""
from __future__ import annotations

from dataclasses import replace

from backend.agent.guardian.action import GuardianAction
from backend.mcp.registry import MCPToolProxy
from backend.permissions.grants import permission_context_with_grants


async def review_tool_request(request, context, registry):
    service = context.run_context.guardian_review_service if context.run_context is not None else None
    if service is None:
        return None
    permission = permission_context_with_grants(context.permission, context)
    parent = context.model_execution
    settings = parent.config.config_layer_stack.effective_config() if parent is not None and parent.config.config_layer_stack is not None else {}
    requirements = parent.config.config_layer_stack.requirements if parent is not None and parent.config.config_layer_stack is not None else None
    model = parent.model_info.id if parent is not None and parent.model_info is not None else parent.model if parent is not None else ""
    require_guardian = requirements is not None and (requirements.auto_review_required_for_model(model) or not requirements.can_select_user_reviewer())
    if not permission.strict_auto_review and not require_guardian and not (
        permission.approval_policy in {"on-request", "granular"}
        and settings.get("approvals_reviewer", "user") == "auto_review"
    ):
        return None
    tool = request.prepared_tool if request.prepared_tool is not None else registry.get_tool(request.tool_name)
    mcp_metadata = None
    if isinstance(tool, MCPToolProxy):
        mcp_metadata = {"server": tool.server_name, "tool_name": tool._tool_def.name,
            "tool_title": tool._tool_def.title, "tool_description": tool.description,
            "annotations": {"read_only_hint": tool._read_only_hint,
                "destructive_hint": tool._destructive_hint, "open_world_hint": tool._open_world_hint}}
    elif request.tool_name not in {"run_command", "apply_patch"}:
        return None
    owned = replace(context, tool_call_id=request.tool_call_id, authorization_digest=request.digest,
        authorization_policy_identity=request.policy_identity,
        permission=permission)
    action = GuardianAction.from_tool_request(request, owned, mcp_metadata=mcp_metadata)
    if isinstance(tool, MCPToolProxy):
        action = replace(action, tool_descriptions=tool.description)
    receipt = await service.decide(action, owned, strict_auto_review=owned.permission.strict_auto_review)
    return receipt.approval_response() if receipt is not None else None
