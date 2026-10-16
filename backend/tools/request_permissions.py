"""Model permission request routed through the host's existing approval owner."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from backend.agent.loop_preflight import await_preflight
from backend.agent.final_tool_request import canonical_tool_request_digest
from backend.permissions.context import ToolExecutionContext
from backend.permissions.grants import (
    PermissionGrantResponse, normalize_permission_profile,
    normalize_permission_response, permission_profile_payload,
    local_permission_temporary_directories,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class RequestPermissionsTool(BaseTool):
    requires_execution_environment = True
    name = "request_permissions"
    description = (
        "Request additional filesystem or network permissions from the user and wait for the client to grant a subset of the requested permission profile. "
        "Use environment_id to target a specific attached environment; omit it to use the primary environment. "
        "Relative filesystem paths resolve against the selected environment cwd. "
        "Granted permissions apply automatically to later shell-like commands in the current turn, or for the rest of the session if the client approves them at session scope."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    projection_visibility = "debug"
    max_result_chars = None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(name=self.name, description=self.description, parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Optional short explanation for why additional permissions are needed."},
                "environment_id": {"type": "string", "description": "Environment id from <environment_context>. Omit to use the primary environment."},
                "permissions": {"type": "object", "properties": {
                    "network": {"type": "object", "description": "Network access request.", "properties": {
                        "enabled": {"type": "boolean", "description": "True requests network access; false or omitted requests none."},
                    }, "additionalProperties": False},
                    "file_system": {"type": "object", "description": "Filesystem access request.", "properties": {
                        "read": {"type": "array", "items": {"type": "string"}, "description": "Absolute paths to grant read access; omit when none are needed."},
                        "write": {"type": "array", "items": {"type": "string"}, "description": "Absolute paths to grant write access; omit when none are needed."},
                    }, "additionalProperties": False},
                }, "additionalProperties": False},
            }, "required": ["permissions"], "additionalProperties": False,
        })

    def check_permission(self, args=None, context=None):
        # Permission to ask does not grant execution authority. The tool asks
        # for an immutable profile once, within the configured approval policy.
        return PermissionLevel.AUTO

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        if context is None or context.turn_environment is None and context.workspace_root is None:
            return ToolResult("request_permissions requires a primary environment", is_error=True)
        environment_id = args.get("environment_id", context.environment_id)
        if environment_id != context.environment_id:
            return ToolResult(f"Unknown environment id: {environment_id}", is_error=True)
        scope = context.permission.environment_scope
        if scope is not None:
            from backend.workspace.executor_paths import ExecutorPath

            cwd = scope.resolve_path(scope.cwd_uri)
            roots = tuple(ExecutorPath.parse(path, platform_os=scope.platform_os) for path in scope.workspace_root_uris)
            temporary_directories = tuple(ExecutorPath.parse(path, platform_os=scope.platform_os)
                for path in scope.temporary_directory_uris or ())
        else:
            cwd = Path(context.metadata.get("cwd") or context.workspace_root)
            roots = (Path(context.workspace_root),)
            temporary_directories = local_permission_temporary_directories()
        try:
            requested = normalize_permission_profile(args["permissions"], cwd=cwd, workspace_roots=roots)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        if requested.is_empty:
            return ToolResult("request_permissions requires at least one permission", is_error=True)
        permission = context.permission
        if permission.approval_policy == "never" or (
            permission.approval_policy == "granular"
            and not (permission.granular_approval or {}).get("request_permissions", False)
        ):
            return ToolResult(json.dumps(PermissionGrantResponse().payload(), ensure_ascii=False))
        digest = context.authorization_digest or canonical_tool_request_digest(self.policy_identity, args)
        profile = permission_profile_payload(requested)
        service = context.run_context.guardian_review_service if context.run_context is not None else None
        if service is not None:
            from backend.agent.guardian.action import GuardianAction

            action = GuardianAction.freeze({"tool": self.name, "turn_id": context.turn_id,
                "reason": args.get("reason"), "permissions": profile}, call_id=context.tool_call_id,
                turn_id=context.turn_id, request_digest=digest,
                policy_identity=context.authorization_policy_identity or self.policy_identity,
                environment_id=environment_id)
            receipt = await service.decide(action, context)
            if receipt is not None and not receipt.require_user:
                normalized = PermissionGrantResponse(requested) if receipt.status == "approved" else PermissionGrantResponse()
                context.permission_grants.record(normalized, environment_id, context.turn_id)
                return ToolResult(json.dumps(normalized.payload(), ensure_ascii=False))
        if context.emit_event is None or context.approval_handler is None:
            return ToolResult("request_permissions requires the host's permission approval handler", is_error=True)
        await context.emit_event("approval_request", {
            "subtype": "request_permissions", "tool_call_id": context.tool_call_id,
            "tool_name": self.name, "args": args, "permissions": profile,
            "reason": args.get("reason"), "cwd": str(cwd), "environment_id": environment_id,
            "started_at_ms": int(time.time() * 1000),
            "request_digest": digest, "policy_identity": context.authorization_policy_identity or self.policy_identity,
            "turn_id": context.turn_id, "conversation_id": context.conversation_id,
            "message_id": str(context.metadata.get("assistant_message_id") or ""),
            "workspace_root": str(roots[0]) if roots else "", "permission_mode": permission.mode,
            "workspace_scope": permission.workspace_scope,
        })
        response = await await_preflight(
            context.approval_handler(context.tool_call_id), deadline=context.deadline_monotonic,
            cancel_event=context.cancel_event, run_context=context.run_context, llm=context.llm,
        )
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise asyncio.CancelledError
        if response.get("request_digest", digest) != digest:
            return ToolResult("The permission response did not match the requested permissions", is_error=True)
        action = response.get("action")
        if action in {"cancel", "abort"}:
            return ToolResult("request_permissions was cancelled before receiving a response", is_error=True, status="cancelled")
        if action in {"reject", "deny", "decline"}:
            normalized = PermissionGrantResponse()
        else:
            try:
                normalized = normalize_permission_response(requested, response, cwd=cwd, workspace_roots=roots, temporary_directories=temporary_directories)
            except ValueError as exc:
                return ToolResult(str(exc), is_error=True)
        context.permission_grants.record(normalized, environment_id, context.turn_id)
        return ToolResult(json.dumps(normalized.payload(), ensure_ascii=False))
