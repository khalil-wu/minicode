"""Plugin requests use the existing owned elicitation and actual installer."""
from __future__ import annotations

import asyncio
import json

from backend.async_cleanup import to_thread_cancel_safe
from backend.plugins.install_suggestions import disable_install_suggestion, installed_plugin
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.subagent_context import resolve_agent_execution_profile


class ListAvailablePluginsToInstallTool(BaseTool):
    name = "list_available_plugins_to_install"
    read_only = True
    description = ("# List plugin/connector install candidates\n\nUse this tool only when both are true:\n"
        "- The user explicitly asks to use a specific plugin or connector that is not already available in the current context or active `tools` list.\n"
        "- `tool_search` is not available, or it has already been called and did not find or make the requested tool callable.\n\n"
        "Returns known plugins and connectors that can be passed to `request_plugin_install`. When both a plugin and a connector match, "
        "prefer the plugin; use the connector only when its corresponding plugin is already installed.\n")

    def __init__(self, candidates):
        self.candidates = candidates

    def get_spec(self):
        return ToolSpec(name=self.name, capability="plugins.discover", toolset="core", exposure="direct_model_only")

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}, "additionalProperties": False}, strict=False)

    async def execute(self, args, context=None):
        return ToolResult(json.dumps({"tools": [entry.entry() for entry in self.candidates]}, ensure_ascii=False))


class RequestPluginInstallTool(BaseTool):
    name = "request_plugin_install"
    description = ("# Request plugin/connector install\n\nUse this tool only after `list_available_plugins_to_install` returns a plugin or connector "
        "that exactly matches the user's explicit request.\n\nDo not use it for adjacent capabilities, broad recommendations, or tools that merely seem useful. "
        "Pass the returned `tool_type` through directly, and pass the returned `id` as `tool_id`.\n\n"
        "IMPORTANT: DO NOT call this tool in parallel with other tools.")
    idempotent = False
    # This invocation asks the client; the explicit client install action owns
    # the mutation. Like request_permissions, it must not ask twice.
    read_only = True

    def is_concurrency_safe(self, args=None):
        return False

    def __init__(self, candidates):
        self.candidates = candidates

    def get_spec(self):
        return ToolSpec(name=self.name, capability="plugins.install", toolset="core", exposure="direct_model_only",
            required_args=("tool_type", "action_type", "tool_id", "suggest_reason"))

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {
            "tool_type": {"type": "string", "description": 'Type of discoverable tool to suggest. Use "connector" or "plugin".'},
            "action_type": {"type": "string", "description": 'Suggested action for the tool. Use "install".'},
            "tool_id": {"type": "string", "description": "Connector or plugin id to suggest."},
            "suggest_reason": {"type": "string", "description": "Concise one-line user-facing reason why this plugin or connector can help with the current request."},
        }, "required": ["tool_type", "action_type", "tool_id", "suggest_reason"], "additionalProperties": False}, strict=False)

    async def execute(self, args, context=None):
        if context is None or context.emit_event is None or context.approval_handler is None:
            return self._error_result("request_plugin_install requires a client that can complete plugin installation")
        if context.run_context.is_subagent or resolve_agent_execution_profile(context.permission, context.metadata) is not None:
            return self._error_result("request_plugin_install can only be used by the root thread")
        if args["action_type"] != "install":
            return self._error_result('plugin install requests currently support only action_type="install"')
        reason = args["suggest_reason"].strip()
        if not reason:
            return self._error_result("suggest_reason must not be empty")
        candidate = next((entry for entry in self.candidates if entry.id == args["tool_id"] and args["tool_type"] == "plugin"), None)
        if candidate is None:
            return self._error_result("tool_id must match one of the discoverable tools returned by list_available_plugins_to_install")
        request_id = context.tool_call_id
        suggestion_id = f"request_plugin_install_{request_id}"
        meta = {"codex_approval_kind": "tool_suggestion", "persist": "always", "tool_type": "plugin", "suggest_type": "install",
            "suggest_reason": reason, "tool_id": candidate.id, "tool_name": candidate.name, "suggestion_id": suggestion_id,
            "app_connector_ids": [], "marketplace": candidate.marketplace, "plugin_name": candidate.plugin_name}
        meta["source"] = candidate.source
        await context.emit_event("ask_user", {"tool_call_id": request_id, "tool_name": self.name, "question": reason,
            "schema": {"type": "object", "properties": {}}, "mode": "form", "_meta": meta,
            "workspace_root": str(context.workspace_root or "")})
        response = await context.approval_handler(request_id)
        confirmed = response.get("action") == "accept"
        completed = False
        if response.get("action") == "decline" and response.get("_meta", response.get("meta", {})).get("persist") == "always":
            await to_thread_cancel_safe(disable_install_suggestion, candidate.id)
            from backend.config import load_config_layer_stack
            context.run_context.plugin_capability_config = await to_thread_cancel_safe(load_config_layer_stack, cwd=context.workspace_root)
            context.run_context.plugin_install_candidates = None
            context.run_context.plugin_install_tools = None
        if confirmed:
            stack, completed = await to_thread_cancel_safe(installed_plugin, candidate, context.workspace_root)
            if completed:
                context.run_context.plugin_capability_config = stack
                context.run_context.plugin_capabilities_changed = True
        return ToolResult(json.dumps({"completed": completed, "user_confirmed": confirmed, "tool_type": "plugin",
            "action_type": "install", "tool_id": candidate.id, "tool_name": candidate.name, "suggest_reason": reason}, ensure_ascii=False))


async def bind_plugin_install_tools(registry, context):
    from backend.plugins.install_suggestions import discover_install_candidates

    snapshot = context.model_execution
    if context.approval_handler is None:
        return
    if snapshot is None or not all(snapshot.config.feature_flags.enabled(name, True) for name in ("apps", "plugins", "tool_suggest")):
        return
    if context.run_context.is_subagent:
        return
    stack = context.run_context.plugin_capability_config or snapshot.config.config_layer_stack
    if stack is None:
        return
    owner = context.run_context
    if owner.plugin_install_candidates is None:
        owner.plugin_install_candidates = await asyncio.to_thread(discover_install_candidates, stack, context.workspace_root)
        owner.plugin_install_tools = (ListAvailablePluginsToInstallTool(owner.plugin_install_candidates),
            RequestPluginInstallTool(owner.plugin_install_candidates))
    if owner.plugin_install_candidates:
        for tool in owner.plugin_install_tools:
            registry.register(tool, owner="plugin-install-suggestions")
