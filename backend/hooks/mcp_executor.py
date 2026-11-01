"""Connected, policy-filtered MCP tools used by configured lifecycle hooks."""
from __future__ import annotations

import asyncio
import json
import re
import time
from copy import deepcopy
from typing import Any

from backend.hooks.runners import HookExecutionError

_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")


def expand_mcp_input(template: dict[str, Any], hook_input: dict[str, Any]) -> dict[str, Any]:
    def path_value(path: str) -> Any:
        current = hook_input
        for field in path.split("."):
            if not isinstance(current, dict) or field not in current:
                raise HookExecutionError(f"hook input placeholder `${{{path}}}` was not found")
            current = current[field]
        return deepcopy(current)

    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            return path_value(whole[1])

        def substitute(match: re.Match[str]) -> str:
            resolved = path_value(match[1])
            return (
                resolved if isinstance(resolved, str)
                else json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))
            )

        return _PLACEHOLDER.sub(substitute, value)

    return resolve(template)


class HookMcpExecutor:
    def __init__(self, manager: Any):
        self.manager = manager

    async def execute(
        self, *, server: str, tool: str, input: dict[str, Any], timeout: float, tool_context: Any,
    ) -> str:
        config = self.manager.get_server_config(server)
        if config is None or not config.enabled:
            raise HookExecutionError(f"MCP hook server is not enabled: {server}")
        # Hooks share the ordinary tool path: a cached server may still be
        # dormant or starting, so wait for its admitted startup the way
        # prepare_for_execution does (Source hook_mcp_executor passes
        # wait_for_server=true).
        try:
            await self.manager.prepare_server_call(server)
        except ConnectionError as error:
            raise HookExecutionError(f"MCP hook server is not connected: {server}: {error}") from error
        client = self.manager.get_client(server)
        if not any(item.name == tool for item in self.manager.get_all_tools().get(server, ())):
            raise HookExecutionError(f"MCP hook tool is not enabled: {server}/{tool}")
        context = tool_context
        run = context.run_context
        capabilities = run.environment_capabilities
        environment_runtime = capabilities.runtime if capabilities is not None else None
        scope = environment_runtime.permission_scope(config.environment_id) if environment_runtime is not None else None
        owner = {
            "session_id": run.mcp_owner_session_id or context.session_id,
            "conversation_id": context.conversation_id,
            "task_id": context.task_id,
            "turn_id": context.turn_id,
            "run_id": str(context.metadata.get("run_id") or ""),
            "conversation_run_generation": context.metadata.get("conversation_run_generation"),
            "rollout_budget": context.metadata.get("_rollout_budget"),
            "mcp_manager": self.manager,
            "hook_manager": run.hook_manager,
            "cancel_event": context.cancel_event,
            "permission_context_provider": run.permission_context_provider or (lambda: context.permission),
            "mcp_permission_profile": (
                context.sandbox_policy.resolve(cwd=context.workspace_root)
                if config.environment_id == "local" and scope is None
                and context.sandbox_policy is not None else None
            ),
            "mcp_permission_scope": scope,
            "deadline_monotonic": time.monotonic() + timeout,
        }
        try:
            async with asyncio.timeout(timeout):
                result = await client.call_tool(
                    tool, input, request_owner=owner,
                    request_meta={"threadId": context.conversation_id},
                )
        except TimeoutError as error:
            raise HookExecutionError(f"MCP hook timed out after {timeout:g}s") from error
        text = "\n".join(
            item["text"] for item in result.content
            if item.get("type") == "text" and isinstance(item.get("text"), str)
        )
        if result.is_error:
            raise HookExecutionError(f"MCP tool returned an error: {text}")
        return text
