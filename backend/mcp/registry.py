"""
Dynamic MCP tool registration.

This module exposes MCP server tools through the main ToolRegistry so the
agent can call them like built-in tools. Tool names follow:
    mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import base64
import copy
import hashlib
import logging
import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class MCPAcceptedResultIdentity:
    """Actual registered executor identity; never decoded from MCP wire data."""
    server: str
    tool: str

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.permissions.patterns import legacy_mcp_allow_aliases, mcp_policy_identity
from backend.tools.base import (
    BaseTool,
    PermissionLevel,
    TOOL_SIDE_EFFECT_DESTRUCTIVE,
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_NONE,
    ToolResult,
    ToolSchema,
)
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def normalize_name_for_mcp(name: str) -> str:
    """Sanitize the model name while keeping raw MCP identities for routing."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or ""))


def _model_tool_names(proxies: list[MCPToolProxy], used_names: set[str]) -> dict[tuple[str, str], str]:
    """Codex's catalog-wide namespace/name collision hashing and 128-byte cap."""
    namespaces: dict[str, set[str]] = {}
    tools: dict[tuple[str, str], set[str]] = {}
    candidates: list[tuple[str, str, str, str, tuple[str, str]]] = []
    by_identity = {(proxy._server_name, proxy._tool_def.name): proxy for proxy in proxies}
    for proxy in proxies:
        server, raw_name = proxy._server_name, proxy._tool_def.name
        namespace = f"mcp__{normalize_name_for_mcp(server)}"
        namespace_identity = f"{server}\0{server}\0"
        raw_identity = f"{namespace_identity}\0{raw_name}\0{raw_name}"
        namespaces.setdefault(namespace, set()).add(namespace_identity)
        candidates.append((namespace, normalize_name_for_mcp(raw_name), namespace_identity, raw_identity, (server, raw_name)))

    def suffix(identity: str) -> str:
        return "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]

    resolved: list[tuple[str, str, str, tuple[str, str]]] = []
    for namespace, tool_name, namespace_identity, raw_identity, identity in candidates:
        if len(namespaces[namespace]) > 1:
            namespace += suffix(namespace_identity)
        tools.setdefault((namespace, tool_name), set()).add(raw_identity)
        resolved.append((namespace, tool_name, raw_identity, identity))

    names: dict[tuple[str, str], str] = {}
    for namespace, tool_name, raw_identity, identity in sorted(resolved, key=lambda item: item[2]):
        if len(tools[(namespace, tool_name)]) > 1:
            tool_name += suffix(raw_identity)
        name = f"{namespace}__{tool_name}"
        allocated_namespace, allocated_tool_name = namespace, tool_name
        attempt = 0
        while len(name) > 128 or name in used_names:
            hash_suffix = suffix(raw_identity if attempt == 0 else f"{raw_identity}\0{attempt}")
            room = 128 - len(namespace) - 2
            if room >= len(hash_suffix):
                allocated_namespace, allocated_tool_name = namespace, tool_name[:room - len(hash_suffix)] + hash_suffix
            else:
                allocated_namespace, allocated_tool_name = namespace[:128 - len(hash_suffix) - 2], hash_suffix
            name = f"{allocated_namespace}__{allocated_tool_name}"
            attempt += 1
        used_names.add(name)
        names[identity] = name
        by_identity[identity].model_namespace = allocated_namespace
        by_identity[identity].model_tool_name = allocated_tool_name
    return names


def _model_visible(tool_def: MCPToolDef) -> bool:
    ui = (tool_def.meta or {}).get("ui")
    visibility = ui.get("visibility") if isinstance(ui, dict) else None
    return not isinstance(visibility, list) or "model" in visibility

class MCPToolProxy(BaseTool):
    """Adapter that exposes an MCP tool through the local tool registry.

    Stores a *manager reference* instead of a direct client pointer so that
    reconnection (which creates a new MCPClient instance) is transparently
    picked up on the next ``execute()`` call.
    """

    def __init__(
        self,
        server_name: str,
        tool_def: MCPToolDef,
        client_or_manager: MCPClient | Any,
        artifact_store: ArtifactStore | None = None,
        manager: Any | None = None,
        prepare_call=None,
    ) -> None:
        self._server_name = server_name
        # Model schema and permission hints must describe the same admitted
        # catalog, even when the manager's next refresh changes nested fields.
        tool_def = copy.deepcopy(tool_def)
        self._tool_def = tool_def
        self._artifact_store = artifact_store

        # Prefer the manager reference for dynamic client lookup (reconnection-safe).
        # Fall back to a direct client pointer for backward compatibility.
        self._manager = manager
        self._prepare_call = prepare_call
        self._static_client: MCPClient | None = client_or_manager if manager is None else None
        self._catalog_revision = (
            int(getattr(manager, "registry_version", 0) or 0)
            if manager is not None
            else 0
        )
        self._server_contract = manager.get_server_contract(server_name) if manager is not None else None

        self.model_namespace = f"mcp__{normalize_name_for_mcp(server_name)}"
        self.model_tool_name = normalize_name_for_mcp(tool_def.name)
        self.name = f"{self.model_namespace}__{self.model_tool_name}"
        self.policy_aliases = (
            self.policy_identity,
            *legacy_mcp_allow_aliases(server_name, tool_def.name),
        )
        self.policy_deny_aliases = tuple(dict.fromkeys((
            f"mcp__{server_name}__{tool_def.name}", self.name,
        )))
        self.description = tool_def.description
        properties = tool_def.input_schema.get("properties")
        self.search_hint = " ".join(part for part in (
            self.name, tool_def.name, server_name, tool_def.title or "", tool_def.description,
            *sorted(properties if isinstance(properties, dict) else ()),
        ) if part.strip())
        # Keep the qualified protocol name for execution and policy matching,
        # while projecting MCP calls the same way Codex does in its activity UI.
        self.activity_kind = "mcpToolCall"
        self.display_label = f"{server_name}.{tool_def.name}"

        # Map MCP annotations/_meta to local capability hints (Phase 3.2).
        ann = getattr(tool_def, "annotations", {}) or {}
        meta = getattr(tool_def, "meta", {}) or {}
        self._destructive_hint = ann.get("destructiveHint")
        self._open_world_hint = ann.get("openWorldHint")
        self._read_only_hint = ann.get("readOnlyHint")
        self.destructive = self._destructive_hint is True
        self.open_world = self._open_world_hint is True
        self.read_only = self._read_only_hint is True
        # MCP _meta vendor extensions are namespaced; "anthropic/alwaysLoad" is
        # the only key with a cross-harness convention (cc Tool.ts).
        self.always_load = bool(meta.get("anthropic/alwaysLoad"))

        server_config = (
            manager.get_server_config(server_name)
            if manager is not None and hasattr(manager, "get_server_config")
            else None
        )
        self._supports_parallel_tool_calls = bool(
            getattr(server_config, "supports_parallel_tool_calls", False)
        )
        tool_modes = getattr(server_config, "tool_approval_modes", {}) or {}
        self._output_token_limit = server_config.tool_output_token_limits.get(tool_def.name) if server_config is not None else None
        approval_mode = tool_modes.get(tool_def.name) or getattr(
            server_config,
            "default_tools_approval_mode",
            None,
        ) or "auto"
        self._approval_mode = approval_mode
        self._approval_config = copy.deepcopy(server_config)
        runtime = getattr(manager, "environment_runtime", None)
        self._permission_scope = runtime.permission_scope(server_config.environment_id) if runtime is not None else None
        self._plugin_id = server_config.source.removeprefix("plugin:") if server_config is not None and server_config.source.startswith("plugin:") else None

        # Source Auto preserves Option<bool>: D=true first, then R=true,
        # otherwise only an explicit D=false and O=false need no approval.
        auto_requires_approval = self.destructive or not self.read_only and not (
            self._destructive_hint is False and self._open_world_hint is False)
        if approval_mode == "approve":
            requires_approval = False
        elif approval_mode == "prompt":
            requires_approval = True
        elif approval_mode == "writes":
            requires_approval = not self.read_only
        else:
            requires_approval = auto_requires_approval
        self.permission = (
            PermissionLevel.CONFIRM if requires_approval else PermissionLevel.AUTO
        )

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def environment_id(self) -> str:
        return self._approval_config.environment_id if self._approval_config is not None else "local"

    @property
    def permission_scope(self):
        """The issuing server's captured attachment, retained by this proxy."""
        return self._permission_scope

    @property
    def policy_identity(self) -> str:
        return mcp_policy_identity(self._server_name, self._tool_def.name)

    @property
    def _client(self) -> MCPClient | None:
        """Dynamic client lookup — survives MCP server reconnections."""
        if self._manager is not None:
            return self._manager.get_client(self._server_name)
        return self._static_client

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        # Source read-only and parallel routing use this hint independently
        # from Auto's destructive/open-world approval decision.
        return self.read_only

    def check_permission(
        self, args: dict[str, Any] | None = None, context: Any | None = None,
    ) -> PermissionLevel | None:
        # Source strict review precedes configured Approve, annotations,
        # remembered approvals and Never's actual-profile preapproval.
        if context is not None and context.strict_auto_review:
            return PermissionLevel.CONFIRM
        if self._approval_mode == "approve":
            return PermissionLevel.AUTO
        profile = context.captured_mcp_permission_profile if context is not None else None
        if context is not None and context.approval_policy == "never" and self._permission_scope is not None:
            if self._permission_scope.has_full_disk_write_access():
                return PermissionLevel.AUTO
        elif context is not None and context.approval_policy == "never" and self.environment_id == "local" and profile is not None:
            from backend.sandbox.policy import SandboxEnforcement

            if profile.enforcement in {SandboxEnforcement.DISABLED, SandboxEnforcement.EXTERNAL} or profile.full_disk_write:
                return PermissionLevel.AUTO
        key = self.session_approval_key()
        store = context.captured_mcp_approval_store if context is not None else None
        if key is not None and store is not None and key in store.session_approved:
            return PermissionLevel.AUTO
        return self.permission

    def session_approval_key(self):
        # mcp_tool_call.rs::session_mcp_tool_approval_key: only Auto can
        # remember a tool; Prompt and Writes always normalize to one approval.
        if self._approval_mode != "auto":
            return None
        # Hosted Apps keys require trusted account/connector metadata. This
        # local MCP facade has no hosted Apps account producer; raw server meta
        # is not account authority. Custom tools have no connector/link key.
        if self._server_name == "codex_apps":
            return None
        return (self._server_name, self._plugin_id, None, None, self._tool_def.name)

    def approval_snapshot(self) -> dict[str, Any]:
        return {"server": self._server_name, "tool_name": self._tool_def.name,
            "approval_mode": self._approval_mode, "allow_session_remember": self.session_approval_key() is not None,
            "allow_persistent_approval": self.session_approval_key() is not None and self.persistent_approval_target() is not None}

    def persistent_approval_target(self) -> dict[str, str] | None:
        config = self._approval_config
        if config is None or self._manager is None:
            return None
        if self._plugin_id is not None:
            return {"kind": "plugin", "plugin_id": self._plugin_id,
                "server": self._server_name.removeprefix(f"plugin:{self._plugin_id}:")}
        if config.source in {"user", "project"} and config.config_path:
            return {"kind": "custom", "path": config.config_path, "server": config.name}
        return None

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        # Remote tools that are not positively identified as reads are external
        # mutations. MCP annotations may advertise a read, but an absent hint
        # must never become side-effect-free through the BaseTool default.
        if self.destructive:
            return TOOL_SIDE_EFFECT_DESTRUCTIVE
        if self.is_read_only(args):
            return TOOL_SIDE_EFFECT_NONE
        return TOOL_SIDE_EFFECT_EXTERNAL

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        return self._supports_parallel_tool_calls or self.read_only

    def get_schema(self) -> ToolSchema:
        """Build a ToolSchema from the MCP tool definition."""
        params = copy.deepcopy(self._tool_def.input_schema or {"type": "object", "properties": {}})
        # tools/mcp_tool.rs inserts an object for missing or null root
        # properties. The same admitted schema serves model and execution.
        if params.get("properties") is None:
            params["properties"] = {}
        description = (
            f"[MCP:{self._server_name}] {self.description}\n"
            f"Original tool: {self._tool_def.name}"
        )
        return ToolSchema(
            name=self.name,
            description=description,
            parameters=params,
            namespace=self.model_namespace,
            namespace_description=f"Tools in the {self.model_namespace} namespace.",
            namespace_tool_name=self.model_tool_name,
            # tools/mcp_tool.rs::mcp_call_tool_result_output_schema. This is
            # Code Mode's result declaration, not a provider wire extension.
            output_schema={"type": "object", "properties": {
                "content": {"type": "array", "items": {"type": "object"}},
                "structuredContent": copy.deepcopy(self._tool_def.output_schema or {}),
                "isError": {"type": "boolean"}, "_meta": {"type": "object"},
            }, "required": ["content"], "additionalProperties": False},
        )

    def model_schema(self) -> ToolSchema:
        schema = self.get_schema()
        parameters = dict(schema.parameters)
        # OpenAI requires the root properties object even for no-argument MCP
        # tools; Codex adapts this at its model schema boundary too.
        parameters.setdefault("properties", {})
        return ToolSchema(name=self.name, description=self.description, parameters=parameters,
            output_schema=schema.output_schema, namespace=schema.namespace,
            namespace_description=schema.namespace_description, namespace_tool_name=schema.namespace_tool_name)

    def to_runtime_metadata(self) -> dict[str, Any]:
        metadata = super().to_runtime_metadata()
        metadata["title"] = self._tool_def.title
        metadata["output_schema"] = copy.deepcopy(self._tool_def.output_schema)
        return metadata

    def get_spec(self):
        from backend.mcp.tool_spec_adapter import MCPToolSpecAdapter

        spec = MCPToolSpecAdapter.from_tool_def(self._server_name, self._tool_def).build_spec(
            self.name, policy_aliases=self.policy_aliases,
        )
        return replace(
            spec,
            policy_identity=self.policy_identity,
            policy_deny_aliases=self.policy_deny_aliases,
        )

    def _catalog_is_current(self, context=None) -> bool:
        """Reject a call whose model-visible MCP schema crossed a catalog change.

        Codex binds prepared MCP calls to a catalog revision.  Keep the same
        fail-closed rule while allowing status-only reconnects when the tool
        definition is unchanged.
        """
        if self._manager is None:
            return True
        if self._manager.get_server_contract(self._server_name) != self._server_contract:
            return False
        current_revision = int(getattr(self._manager, "registry_version", 0) or 0)
        if current_revision == self._catalog_revision:
            return True
        get_all_tools = getattr(self._manager, "get_all_tools", None)
        if not callable(get_all_tools):
            return False
        current_tools = (get_all_tools() or {}).get(self._server_name, ())
        current = next(
            (item for item in current_tools if str(getattr(item, "name", "")) == self._tool_def.name),
            None,
        )
        if current is None:
            return False
        current_server_config = (
            self._manager.get_server_config(self._server_name)
            if hasattr(self._manager, "get_server_config")
            else None
        )
        current_tool_modes = getattr(current_server_config, "tool_approval_modes", {}) or {}
        current_approval_mode = current_tool_modes.get(self._tool_def.name) or getattr(
            current_server_config,
            "default_tools_approval_mode",
            None,
        ) or "auto"
        from backend.permissions.context import ToolExecutionContext
        # Persisting this exact approved request changes its own policy to
        # Approve before dispatch. Its bound receipt survives that edit; other
        # requests and any schema/connection change still need fresh admission.
        own_persistent_approval = current_approval_mode == "approve" and isinstance(context, ToolExecutionContext) and (
            context.authorization_policy_identity == self.policy_identity
            and context.authorization_digest in context.metadata.get("_approved_request_digests", set()))
        same_contract = (
            dict(getattr(current, "input_schema", {}) or {})
            == dict(getattr(self._tool_def, "input_schema", {}) or {})
            and str(getattr(current, "description", "") or "")
            == str(getattr(self._tool_def, "description", "") or "")
            and dict(getattr(current, "annotations", {}) or {})
            == dict(getattr(self._tool_def, "annotations", {}) or {})
            and dict(getattr(current, "meta", {}) or {})
            == dict(getattr(self._tool_def, "meta", {}) or {})
            and bool(getattr(current_server_config, "supports_parallel_tool_calls", False))
            == self._supports_parallel_tool_calls
            and (current_approval_mode == self._approval_mode or own_persistent_approval)
            and current.title == self._tool_def.title
            and current.output_schema == self._tool_def.output_schema
            and (current_server_config.tool_output_token_limits.get(self._tool_def.name) if current_server_config is not None else None)
            == self._output_token_limit
        )
        if same_contract:
            self._catalog_revision = current_revision
        return same_contract

    async def execute(self, args: dict[str, Any], *, context: Any | None = None) -> ToolResult:
        """Execute the MCP tool and normalize the result into ToolResult.

        Tool annotations describe permission risk, not result immutability.
        Every invocation reaches the server so mutable remote state stays fresh.
        """
        if self._prepare_call is not None:
            await self._prepare_call(self._server_name)
        if not self._catalog_is_current(context):
            return self._error_result(
                f"MCP catalog changed for '{self._server_name}'. Refresh tools before calling '{self._tool_def.name}'."
            )
        client = self._client
        if client is None or not client.connected:
            return self._error_result(
                f"MCP server '{self._server_name}' is not connected. "
                "Check .mcp.json or restart the server."
            )

        try:
            request_owner = None
            if context is not None:
                metadata = getattr(context, "metadata", {}) or {}
                run_context = getattr(context, "run_context", None)
                request_owner = {
                    "session_id": str(
                        getattr(run_context, "mcp_owner_session_id", "")
                        or getattr(context, "session_id", "")
                        or ""
                    ),
                    "conversation_id": str(getattr(context, "conversation_id", "") or ""),
                    "task_id": str(getattr(context, "task_id", "") or ""),
                    "run_id": str(metadata.get("run_id") or ""),
                    "conversation_run_generation": metadata.get(
                        "conversation_run_generation"
                    ),
                    "mcp_manager": (
                        self._manager or getattr(run_context, "mcp_manager", None)
                    ),
                    "hook_manager": (
                        run_context.hook_manager
                        if run_context is not None
                        else None
                    ),
                    "rollout_budget": metadata.get("_rollout_budget"),
                    "deadline_monotonic": getattr(context, "deadline_monotonic", None),
                    # Server-initiated MCP requests inherit the exact tool-call
                    # owner.  Keeping the cancellation object here lets sampling
                    # and elicitation stop with the originating turn instead of
                    # outliving a cancelled tool call in the shared MCP client.
                    "cancel_event": getattr(context, "cancel_event", None),
                    "permission_context_provider": (
                        run_context.permission_context_provider
                        if run_context is not None and run_context.permission_context_provider is not None
                        else lambda: context.permission
                    ),
                    # The prepared call publishes its actual sandbox authority;
                    # Never alone does not preapprove an MCP permission prompt.
                    "mcp_permission_profile": (
                        context.sandbox_policy.resolve(cwd=context.workspace_root)
                        if self.environment_id == "local" and self._permission_scope is None
                        and context.sandbox_policy is not None else None
                    ),
                    "mcp_permission_scope": self._permission_scope,
                }
                request_owner.update({key: metadata[key] for key in ("message_id", "turn_id") if key in metadata})
            call_started = time.monotonic()
            result: MCPCallResult = await client.call_tool(
                self._tool_def.name,
                args,
                request_owner=request_owner,
            )
        except Exception as exc:
            return self._error_result(f"MCP tool '{self._tool_def.name}' failed: {exc}")

        wall_time = time.monotonic() - call_started
        images: list[dict[str, str]] = []
        audios: list[dict[str, str]] = []
        resource_artifacts: list[dict[str, Any]] = []
        for block in result.content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip().lower()
            if block_type in {"image", "audio"}:
                data = str(block.get("data") or "").strip()
                if data:
                    (images if block_type == "image" else audios).append({
                        "media_type": str(
                            block.get("mimeType")
                            or block.get("media_type")
                            or ("image/png" if block_type == "image" else "")
                        ),
                        "data": data,
                    })
                continue
            if block_type == "resource_link":
                continue
            if block_type != "resource":
                continue
            resource = block.get("resource")
            if not isinstance(resource, dict):
                continue
            uri = str(resource.get("uri") or "resource").strip() or "resource"
            media_type = str(
                resource.get("mimeType") or resource.get("media_type") or ""
            ).strip()
            text_content = resource.get("text")
            if isinstance(text_content, str):
                continue
            blob = resource.get("blob")
            if isinstance(blob, str) and self._artifact_store is not None:
                body = base64.b64decode(blob, validate=True)
                artifact_id = self._artifact_store.save(
                    blob,
                    source=f"mcp:{self._server_name}:{uri}",
                    type="mcp_resource",
                    media_type=media_type or "application/octet-stream",
                )
                resource_artifacts.append({
                    "artifact_id": artifact_id,
                    "uri": uri,
                    "media_type": media_type or "application/octet-stream",
                    "bytes": len(body),
                })

        from backend.mcp.result_projection import mcp_log_output

        public_result = result.public_result()
        plain_log = mcp_log_output(public_result)
        typed_content = f"Wall time: {wall_time:.4f} seconds\nOutput:" + ("\n" + plain_log if plain_log else "")
        first_artifact = resource_artifacts[0] if resource_artifacts else None

        # Raw CallToolResult is retained for Code Mode. ContextBuilder admits
        # a separate model body with the issuing Source truncation policy.
        return ToolResult(
            content=typed_content,
            mcp_result=public_result,
            is_error=result.is_error,
            images=images,
            audios=audios,
            artifact_id=first_artifact["artifact_id"] if first_artifact else None,
            artifact_kind=(
                "image" if first_artifact["media_type"].startswith("image/")
                else "file" if first_artifact["media_type"].startswith("audio/") or first_artifact["media_type"] == "application/pdf"
                else "binary"
            ) if first_artifact else None,
            artifact_media_type=first_artifact["media_type"] if first_artifact else None,
            artifact_bytes=first_artifact["bytes"] if first_artifact else None,
            status="failed" if result.is_error else "success",
            runtime_metadata={
                "mcp_accepted_result_identity": MCPAcceptedResultIdentity(self._server_name, self._tool_def.name),
                "mcp": {"structuredContent": result.structured_content, "_meta": result.meta},
                "mcp_output_token_limit": self._output_token_limit,
                "mcp_wall_time_seconds": wall_time,
                "mcp_resource_artifacts": resource_artifacts,
            },
        )


class MCPToolRegistry:
    """Manage MCP tool proxy registration and cleanup."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        artifact_store: ArtifactStore | None = None,
        mcp_manager: Any | None = None,
        prepare_call=None,
    ) -> None:
        self._tool_registry = tool_registry
        self._artifact_store = artifact_store
        self._mcp_manager = mcp_manager
        self._prepare_call = prepare_call
        self._server_tools: dict[str, list[str]] = {}
        self._wire_name_owner: dict[str, str] = {}
        self._version = mcp_manager.registry_version if mcp_manager is not None else 0
        tool_registry.mcp_tool_registry = self

    def fork(self, tool_registry: ToolRegistry) -> "MCPToolRegistry":
        clone = MCPToolRegistry(tool_registry, self._artifact_store, self._mcp_manager, self._prepare_call)
        clone._server_tools = {name: list(tools) for name, tools in self._server_tools.items()}
        clone._wire_name_owner = dict(self._wire_name_owner)
        clone._version = self._version
        return clone

    def sync(self) -> None:
        manager = self._mcp_manager
        if manager is None or self._version == manager.registry_version:
            return
        tools_by_server = manager.get_all_tools()
        proxies = [
            proxy for server_name, tools in tools_by_server.items()
            for proxy in self._admitted_proxies(server_name, tools, manager.get_client(server_name))
        ]
        self._publish_catalog(proxies)
        self._version = manager.registry_version

    def register_server_tools(
        self,
        server_name: str,
        tools: list[MCPToolDef],
        client: MCPClient,
    ) -> int:
        """Register all tools exposed by one MCP server."""
        proxies = [
            self._tool_registry.get_tool(name)
            for owner, names in self._server_tools.items() if owner != server_name
            for name in names
        ]
        proxies.extend(self._admitted_proxies(server_name, tools, client))
        self._publish_catalog(proxies)
        return len(self._server_tools.get(server_name, []))

    def _admitted_proxies(self, server_name: str, tools: list[MCPToolDef], client: MCPClient) -> list[MCPToolProxy]:
        proxies: list[MCPToolProxy] = []
        seen: set[str] = set()
        for tool_def in tools:
            if not _model_visible(tool_def) or tool_def.name in seen:
                continue
            seen.add(tool_def.name)
            proxy = MCPToolProxy(
                server_name=server_name,
                tool_def=tool_def,
                client_or_manager=client,
                artifact_store=self._artifact_store,
                manager=self._mcp_manager,
                prepare_call=self._prepare_call,
            )
            schema = proxy.get_schema().parameters
            if schema.get("type", "object") != "object":
                logger.warning("[MCPRegistry] Skipped %s: input schema must describe an object", proxy.name)
                continue
            try:
                validator_for(schema).check_schema(schema)
            except SchemaError as exc:
                logger.warning("[MCPRegistry] Skipped %s: invalid input schema: %s", proxy.name, exc.message)
                continue
            proxies.append(proxy)
        return proxies

    def _publish_catalog(self, proxies: list[MCPToolProxy]) -> None:
        current_names = set(self._wire_name_owner)
        previous = {
            (proxy._server_name, proxy._tool_def.name): proxy
            for name in current_names
            if (proxy := self._tool_registry.get_tool(name)) is not None
        }
        names = _model_tool_names(proxies, set(self._tool_registry.list_tools()) - current_names)
        desired: dict[str, MCPToolProxy] = {}
        for proxy in proxies:
            identity = (proxy._server_name, proxy._tool_def.name)
            name = names[identity]
            old = previous.get(identity)
            deny_aliases = tuple(dict.fromkeys((
                *proxy.policy_deny_aliases,
                *(old.policy_deny_aliases if old is not None else ()),
                *((old.name,) if old is not None else ()),
                name,
            )))
            if name != proxy.name or deny_aliases != proxy.policy_deny_aliases:
                # In-flight forks share tool instances. Rename a catalog copy
                # so their already-admitted schemas keep their exact identity.
                proxy = copy.copy(proxy)
                proxy.name = name
                proxy.policy_deny_aliases = deny_aliases
            desired[name] = proxy
        for name in current_names:
            if name not in desired or self._tool_registry.get_tool(name) is not desired[name]:
                self._tool_registry.unregister(name)
        self._server_tools = {}
        self._wire_name_owner = {}
        for name, proxy in desired.items():
            server = proxy._server_name
            if self._tool_registry.get_tool(name) is not proxy:
                self._tool_registry.register(proxy, owner=f"mcp:{server}")
            self._wire_name_owner[name] = server
            self._server_tools.setdefault(server, []).append(name)

    def unregister_server_tools(self, server_name: str) -> None:
        """Unregister all tools that came from one MCP server."""
        tool_names = self._server_tools.pop(server_name, [])
        for name in tool_names:
            if self._wire_name_owner.get(name) != server_name:
                continue
            self._tool_registry.unregister(name)
            self._wire_name_owner.pop(name, None)
            logger.info("[MCPRegistry] Unregistered tool: %s", name)

    def get_server_tool_count(self, server_name: str) -> int:
        """Return the number of tools registered for one server."""
        return len(self._server_tools.get(server_name, []))

    def get_all_mcp_tools(self) -> dict[str, list[str]]:
        """Return the current server-to-tool mapping."""
        return dict(self._server_tools)
