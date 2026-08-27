"""Capability registry for tools and command metadata."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from copy import deepcopy
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import PermissionContext
    from backend.tools.toolsets import ToolsetPolicy

from backend.permissions.context import ToolExecutionContext
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain,
    cancel_and_drain_receipt,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, validate_tool_input

logger = logging.getLogger(__name__)

class CapabilityRegistry:
    """In-memory registry for tools, commands, and skills."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._commands: dict[str, Any] = {}
        self._skills: dict[str, dict[str, Any]] = {}
        self._tool_owners: dict[str, str] = {}
        self._schema_cache: dict[str, list[dict[str, Any]]] = {}
        self._validated_policies: set[tuple[str, int]] = set()
        self._version = 0

    def register(
        self,
        tool: BaseTool,
        *,
        replace: bool = False,
        owner: str | None = None,
    ) -> None:
        if tool.name in self._tools:
            if not replace:
                existing_owner = self._tool_owners.get(tool.name, "unknown")
                raise ValueError(
                    f"Tool name conflict for '{tool.name}' "
                    f"(existing owner: {existing_owner})"
                )
            logger.warning("Replacing tool '%s' owned by %s", tool.name, self._tool_owners.get(tool.name, "unknown"))
        self._tools[tool.name] = tool
        self._tool_owners[tool.name] = str(owner or getattr(tool, "owner", None) or "builtin")
        self._touch()

    def unregister(self, name: str) -> bool:
        removed = self._tools.pop(name, None) is not None
        if removed:
            self._tool_owners.pop(name, None)
            self._touch()
        return removed

    def get_tool_owner(self, name: str) -> str | None:
        return self._tool_owners.get(name)

    def register_command(self, name: str, handler: Any) -> None:
        if name in self._commands:
            logger.warning("Command name conflict detected for '%s'; overriding previous registration", name)
        self._commands[name] = handler
        self._touch()

    def unregister_command(self, name: str) -> bool:
        removed = self._commands.pop(name, None) is not None
        if removed:
            self._touch()
        return removed

    def get_command(self, name: str) -> Any | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def get_commands(self) -> dict[str, Any]:
        return dict(self._commands)

    def register_skill(self, name: str, metadata: Any) -> None:
        """Register a skill descriptor for the runtime capability catalog.

        Skills are prompt/runtime capabilities, not model-callable tools. Keep
        their metadata in the same versioned registry as commands so UI and
        websocket capability snapshots describe one coherent session surface.
        """

        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("skill name is required")
        payload = self._build_named_metadata(clean_name, metadata)
        self._skills[clean_name] = payload
        self._touch()

    def unregister_skill(self, name: str) -> bool:
        removed = self._skills.pop(str(name or "").strip(), None) is not None
        if removed:
            self._touch()
        return removed

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())

    def get_skills(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._skills)

    def fork(self) -> "CapabilityRegistry":
        """Build an agent-run-local registry from the current base maps.

        Reusing tool instances is intentional: canonical executors and their
        control plane stay shared, while mutable registration maps and schema
        projections must not leak between parent/child runs or conversations.
        """

        clone = CapabilityRegistry()
        clone._tools = dict(self._tools)
        clone._tool_owners = dict(self._tool_owners)
        clone._commands = deepcopy(self._commands)
        clone._skills = deepcopy(self._skills)
        clone._schema_cache = deepcopy(self._schema_cache)
        clone._validated_policies = set(self._validated_policies)
        clone._version = self._version
        return clone

    def _resolve_toolset_policy(self, toolset_policy: 'ToolsetPolicy | None') -> Any:
        """Return the active policy after checking it against this registry.

        The registry owns the universe of tool and toolset names, so it is the
        only place that can tell a deliberate whitelist from a selection whose
        names no longer exist. An unresolvable allow-list is raised instead of
        silently handing the model a shrunken (or empty) tool surface.

        Materializing every ToolSpec is expensive, so the answer is memoized per
        (policy, registry version) and skipped entirely for a policy that names
        no group or tool to resolve.
        """
        from backend.tools.toolsets import ALL_TOOLSETS, ToolsetPolicy

        policy = toolset_policy or ToolsetPolicy.default()
        if not (policy.enabled_tools or (policy.enabled_toolsets - {ALL_TOOLSETS})):
            return policy
        cache_key = (policy.cache_key(), self._version)
        if cache_key in self._validated_policies:
            return policy
        policy.validate_against(self.get_tool_spec(name) for name in self._tools)
        self._validated_policies.add(cache_key)
        return policy

    @staticmethod
    def _schema_permission_level(
        permission_checker: 'PermissionChecker | None',
        permission_context: 'PermissionContext | None',
        name: str,
        tool: BaseTool,
    ) -> PermissionLevel:
        if permission_context is None:
            return tool.permission
        if permission_checker is None:
            return tool.capability_permission_level(permission_context)
        from backend.permissions.checker import evaluate_permission_decision

        return evaluate_permission_decision(
            permission_checker,
            name,
            context=permission_context,
            tool=tool,
        ).permission_level

    @staticmethod
    def _schema_permission_decision(
        permission_checker: 'PermissionChecker',
        permission_context: 'PermissionContext',
        name: str,
        tool: BaseTool,
    ):
        from backend.permissions.checker import evaluate_permission_decision

        return evaluate_permission_decision(
            permission_checker,
            name,
            args={},
            context=permission_context,
            tool=tool,
        )

    def build_schema_views(
        self,
        *,
        toolset_policy: 'ToolsetPolicy | None' = None,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        materialize_schema: bool = True,
    ) -> list[Any]:
        """Return one ToolSchemaView per registered tool (Phase 1.2).

        Single source of truth for exposure (direct/deferred/hidden) and the
        model-vs-runtime split. ``schema`` is populated only for direct tools
        when materialization is requested; deferred tools expose lightweight
        catalog metadata and load full schema after tool_search activates it.
        """
        active_policy = self._resolve_toolset_policy(toolset_policy)
        views: list[Any] = []
        for name, tool in self._tools.items():
            views.append(
                self._build_schema_view_for_tool(
                    name,
                    tool,
                    active_policy=active_policy,
                    permission_checker=permission_checker,
                    permission_context=permission_context,
                    materialize_schema=materialize_schema,
                )
            )
        return views

    def get_schema_view(
        self,
        name: str,
        *,
        toolset_policy: 'ToolsetPolicy | None' = None,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        materialize_schema: bool = True,
    ) -> Any | None:
        """Return the current ToolSchemaView for one registered tool."""
        tool = self._tools.get(name)
        if tool is None:
            return None
        return self._build_schema_view_for_tool(
            name,
            tool,
            active_policy=self._resolve_toolset_policy(toolset_policy),
            permission_checker=permission_checker,
            permission_context=permission_context,
            materialize_schema=materialize_schema,
        )

    def _build_schema_view_for_tool(
        self,
        name: str,
        tool: BaseTool,
        *,
        active_policy: Any,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        materialize_schema: bool = True,
    ) -> Any:
        from backend.tools.contracts import ToolSchemaView

        spec = self.get_tool_spec(name)
        policy_available = active_policy.is_available(spec)
        direct = policy_available and active_policy.is_directly_visible(spec)
        denied = False
        level = None
        if permission_checker and permission_context:
            capability_check = getattr(permission_checker, "capability_available", None)
            if callable(capability_check):
                allowed, _reason = capability_check(name, context=permission_context, tool=tool)
                decision = self._schema_permission_decision(
                    permission_checker,
                    permission_context,
                    name,
                    tool,
                )
                denied = not allowed or decision.decision == "deny"
                level = decision.permission_level
            else:
                from backend.permissions.checker import evaluate_permission_decision
                decision = evaluate_permission_decision(
                    permission_checker, name, context=permission_context, tool=tool
                )
                level = decision.permission_level
                denied = not decision.capability_allowed
        elif permission_context:
            level = tool.capability_permission_level(permission_context)
            denied = not tool.is_capability_available(permission_context)
        exposure = spec.exposure if spec else "core"
        if not policy_available:
            exposure = "hidden"
        if denied:
            exposure = "hidden"
        direct = direct and not denied
        schema_available = exposure != "hidden"
        description_for_policy = getattr(
            tool,
            "model_description_for_policy",
            None,
        )
        model_description = str(
            description_for_policy(active_policy)
            if callable(description_for_policy)
            else tool.model_description() or ""
        ).strip()
        schema = None
        if materialize_schema and direct and schema_available:
            schema_for_policy = getattr(tool, "model_schema_for_policy", None)
            tool_schema = (
                schema_for_policy(active_policy)
                if callable(schema_for_policy)
                else tool.model_schema() or tool.get_schema()
            )
            if model_description and model_description != str(tool_schema.description or "").strip():
                tool_schema = tool_schema.with_description(model_description)
            schema = tool_schema.to_openai_tool()
        meta = tool.to_runtime_metadata()
        if level is not None:
            meta = {**meta, "permission": level.value}
        return ToolSchemaView(
            name=name,
            exposure=exposure,
            schema=schema,
            direct=direct,
            schema_available=schema_available,
            catalog_text=self._build_catalog_text(name, tool, spec),
            search_hint=getattr(tool, "search_hint", "") or "",
            short_description=model_description,
            runtime_metadata=meta,
        )

    def get_runtime_metadata(
        self,
        *,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
    ) -> dict[str, dict[str, Any]]:
        """Per-tool non-model-facing metadata (permission, capability hints).

        Keeps permission/UI info out of the model schema (Phase 1.3) while still
        giving UI/event consumers the data that used to be appended to the
        function description.
        """
        out: dict[str, dict[str, Any]] = {}
        for name, tool in self._tools.items():
            meta = tool.to_runtime_metadata()
            if permission_context:
                if permission_checker:
                    capability_check = getattr(permission_checker, "capability_available", None)
                    if callable(capability_check):
                        allowed, _reason = capability_check(name, context=permission_context, tool=tool)
                        decision = self._schema_permission_decision(
                            permission_checker,
                            permission_context,
                            name,
                            tool,
                        )
                        level = decision.permission_level
                        if not allowed or decision.decision == "deny":
                            level = PermissionLevel.ALWAYS_DENY
                    else:
                        from backend.permissions.checker import check_permission_level
                        level = check_permission_level(
                            permission_checker, name, context=permission_context, tool=tool
                        )
                else:
                    level = tool.capability_permission_level(permission_context)
                meta = {**meta, "permission": level.value}
            out[name] = meta
        return out

    def build_snapshot(
        self,
        *,
        toolset_policy: 'ToolsetPolicy | None' = None,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        mcp_registry_version: int = 0,
    ) -> dict[str, Any]:
        """Return a stable capability snapshot for UI/runtime consumers."""
        views = self.build_schema_views(
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
        return {
            "version": self._version,
            "tools": deepcopy(
                self.get_schemas(
                    permission_checker=permission_checker,
                    permission_context=permission_context,
                    toolset_policy=toolset_policy,
                    mcp_registry_version=mcp_registry_version,
                )
            ),
            "tool_views": [
                self._build_tool_view_metadata(view)
                for view in sorted(views, key=lambda item: item.name)
            ],
            "tool_runtime_metadata": self.get_runtime_metadata(
                permission_checker=permission_checker,
                permission_context=permission_context,
            ),
            "commands": [
                self._build_named_metadata(name, metadata)
                for name, metadata in sorted(self._commands.items())
            ],
            "skills": [
                deepcopy(metadata)
                for _name, metadata in sorted(self._skills.items())
            ],
            "summary": self._build_capability_summary(views),
        }

    def build_capability_summary(
        self,
        *,
        toolset_policy: 'ToolsetPolicy | None' = None,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
    ) -> dict[str, Any]:
        """Return the compact capability summary without materializing schemas."""
        views = self.build_schema_views(
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
            materialize_schema=False,
        )
        return self._build_capability_summary(views)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_tool_spec(self, name: str):
        from backend.tools.catalog import tool_spec_for

        return tool_spec_for(name, self)

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def _build_catalog_text(self, name: str, tool: BaseTool, spec: Any) -> str:
        """Return lightweight search text without materializing JSON schema."""
        parts = [
            name,
            getattr(tool, "search_hint", "") or "",
            getattr(spec, "capability", "") or "",
            getattr(spec, "toolset", "") or "",
        ]
        try:
            parts.append(tool.model_description() or "")
        except Exception:
            parts.append(getattr(tool, "description", "") or "")
        required_args = getattr(spec, "required_args", ()) or ()
        parts.extend(str(arg) for arg in required_args)
        return " ".join(str(part) for part in parts if str(part).strip())

    def _direct_schema_fingerprint(
        self,
        active_policy: Any,
        *,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
    ) -> str:
        """Fingerprint only tools that can affect this turn's direct schema list."""
        direct_items: list[tuple[Any, ...]] = []
        for name, tool in self._tools.items():
            spec = self.get_tool_spec(name)
            direct = active_policy.is_directly_visible(spec)
            exposure = spec.exposure if spec else "core"
            if permission_checker and permission_context:
                capability_check = getattr(permission_checker, "capability_available", None)
                if callable(capability_check):
                    allowed, _reason = capability_check(name, context=permission_context, tool=tool)
                    decision = self._schema_permission_decision(
                        permission_checker,
                        permission_context,
                        name,
                        tool,
                    )
                    if decision.decision == "deny":
                        allowed = False
                else:
                    from backend.permissions.checker import evaluate_permission_decision
                    allowed = evaluate_permission_decision(
                        permission_checker, name, context=permission_context, tool=tool
                    ).capability_allowed
                if not allowed:
                    direct = False
                    exposure = "hidden"
            if not direct or exposure == "hidden":
                continue
            direct_items.append(
                (
                    0 if exposure == "core" else 1,
                    name,
                    id(tool),
                    getattr(spec, "toolset", ""),
                    getattr(spec, "exposure", ""),
                    bool(getattr(spec, "always_load", False)),
                )
            )
        direct_items.sort()
        return json.dumps(direct_items, ensure_ascii=False, sort_keys=True, default=str)

    def get_tool_schema(
        self,
        name: str,
        *,
        toolset_policy: 'ToolsetPolicy | None' = None,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        require_deferred: bool = False,
    ) -> dict[str, Any] | None:
        """Materialize one tool schema on demand.

        Used when tool_search activates deferred tools. This is intentionally
        separate from build_schema_views so catalog/search paths can stay
        schema-lazy.
        """
        from backend.tools.toolsets import ToolsetPolicy

        tool = self._tools.get(name)
        if tool is None:
            return None
        view = self.get_schema_view(
            name,
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
            materialize_schema=False,
        )
        if view is None or not bool(getattr(view, "schema_available", False)):
            return None
        if require_deferred and (view.exposure != "deferred" or bool(view.direct)):
            return None
        schema_for_policy = getattr(tool, "model_schema_for_policy", None)
        tool_schema = (
            schema_for_policy(toolset_policy or ToolsetPolicy.default())
            if callable(schema_for_policy)
            else tool.model_schema() or tool.get_schema()
        )
        description_for_policy = getattr(
            tool,
            "model_description_for_policy",
            None,
        )
        model_description = str(
            description_for_policy(toolset_policy or ToolsetPolicy.default())
            if callable(description_for_policy)
            else tool.model_description() or ""
        ).strip()
        if model_description and model_description != str(tool_schema.description or "").strip():
            tool_schema = tool_schema.with_description(model_description)
        return tool_schema.to_openai_tool()

    def get_schemas(
        self,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        toolset_policy: 'ToolsetPolicy | None' = None,
        mcp_registry_version: int = 0,
    ) -> list[dict[str, Any]]:
        from backend.tools.toolsets import ToolsetPolicy

        active_policy = self._resolve_toolset_policy(toolset_policy)
        # mcp_registry_version is intentionally ignored for the default direct
        # schema payload: deferred/MCP catalog churn must not perturb the model's
        # tools array. Direct visibility changes still affect the fingerprint.
        direct_fingerprint = self._direct_schema_fingerprint(
            active_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
        cache_key = f"{active_policy.cache_key()}_{direct_fingerprint}"
        if permission_checker and permission_context:
            overrides_hash = hash(frozenset(permission_context.session_overrides.items())) if permission_context.session_overrides else 0
            cache_key = (
                f"{permission_context.mode}"
                f"_{permission_context.approval_policy}"
                f"_{hash(frozenset(permission_context.tool_deny_rules))}"
                f"_{overrides_hash}"
                f"_{active_policy.cache_key()}_{direct_fingerprint}"
            )

        cached = self._schema_cache.get(cache_key)
        if cached is not None:
            return cached

        # Single source of truth: derive direct schemas from ToolSchemaView.
        # A view has a non-None schema iff it is directly visible and not denied.
        from backend.tools.schema import postprocess_tool_schema
        from backend.tools.catalog import canonicalize_tool_schemas

        views = self.build_schema_views(
            toolset_policy=active_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
        direct_views = sorted(
            (v for v in views if v.direct and v.schema is not None),
            key=lambda view: (
                0 if view.exposure == "core" else 1,
                view.name,
            ),
        )
        visible_names = {v.name for v in direct_views}
        schemas: list[dict[str, Any]] = [
            # Model-facing schema stays free of permission/UI noise (Phase 1.3);
            # permission lives in view.runtime_metadata for UI consumers.
            postprocess_tool_schema(dict(v.schema), visible_tool_names=visible_names)
            for v in direct_views
        ]
        schemas = canonicalize_tool_schemas(schemas, tool_registry=self)

        self._schema_cache[cache_key] = schemas
        return schemas

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"Tool '{name}' does not exist. Available: {', '.join(self._tools.keys())}",
                is_error=True,
            )

        validation_error = validate_tool_input(tool, args)
        if validation_error:
            return ToolResult(
                content=f"Invalid input for tool '{name}': {validation_error}",
                is_error=True,
                status="failed",
                error_kind="validation_error",
                user_summary="工具输入未通过结构化校验。",
                developer_detail=validation_error,
                model_observation=(
                    f"The {name} input does not match its schema: {validation_error}. "
                    "Correct the arguments and call the tool again."
                ),
            )

        cancel_event = getattr(context, "cancel_event", None) if context is not None else None
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

        execution_task = asyncio.ensure_future(
            tool.execute(args, context=context)
        )
        cancel_wait_task = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        try:
            if cancel_wait_task is None:
                result = await execution_task
            else:
                done, _ = await asyncio.wait(
                    {execution_task, cancel_wait_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution_task in done:
                    result = execution_task.result()
                else:
                    receipt = await cancel_and_drain_receipt(
                        [execution_task],
                        timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                        label=f"interrupted registry tool {name}",
                        owner=getattr(context, "pending_cleanup_tasks", None),
                    )
                    _publish_registry_cleanup_receipt(context, name, receipt, reason="interrupted")
                    raise asyncio.CancelledError
        except asyncio.CancelledError:
            receipt = await cancel_and_drain_receipt(
                [execution_task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"cancelled registry tool {name}",
                owner=getattr(context, "pending_cleanup_tasks", None),
            )
            _publish_registry_cleanup_receipt(context, name, receipt, reason="cancelled")
            raise
        except Exception as exc:
            return ToolResult(
                content=(
                    f"Tool '{name}' execution failed ({type(exc).__name__}).\n"
                    "Check the arguments or try a different approach."
                ),
                is_error=True,
                developer_detail=str(exc),
            )
        finally:
            if cancel_wait_task is not None and not cancel_wait_task.done():
                cancel_wait_task.cancel()
                await cancel_and_drain(
                    [cancel_wait_task],
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label=f"registry cancellation waiter {name}",
                )
        return result


    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)

    @property
    def version(self) -> int:
        return self._version

    def _build_named_metadata(self, name: str, metadata: Any) -> dict[str, Any]:
        if isinstance(metadata, dict):
            payload = dict(metadata)
        elif metadata is None:
            payload = {}
        else:
            payload = {"handler": getattr(metadata, "__name__", metadata.__class__.__name__)}
        payload["name"] = name
        return payload

    def _build_tool_view_metadata(self, view: Any) -> dict[str, Any]:
        spec = self.get_tool_spec(str(view.name))
        runtime_metadata = dict(getattr(view, "runtime_metadata", {}) or {})
        short_description = str(getattr(view, "short_description", "") or "").strip()
        if short_description.endswith("."):
            short_description = short_description[:-1]
        return {
            "name": str(view.name),
            "exposure": str(view.exposure),
            "direct": bool(view.direct),
            "schema_available": bool(getattr(view, "schema_available", False)),
            "toolset": str(getattr(spec, "toolset", "") or ""),
            "capability": str(getattr(spec, "capability", "") or ""),
            "permission": str(runtime_metadata.get("permission") or "auto"),
            "read_only": bool(runtime_metadata.get("read_only", False)),
            "short_description": short_description,
        }

    def _build_capability_summary(self, views: list[Any]) -> dict[str, Any]:
        tool_names = {str(view.name) for view in views}
        exposure_counts = Counter(str(view.exposure) for view in views)
        mcp_resource_bridge = {"list_mcp_resources", "read_mcp_resource"} <= tool_names
        mcp_resource_template_bridge = {"list_mcp_resource_templates"} <= tool_names
        mcp_resource_subscription_bridge = {
            "subscribe_mcp_resource",
            "unsubscribe_mcp_resource",
            "list_mcp_resource_notifications",
        } <= tool_names
        mcp_prompt_bridge = {"list_mcp_prompts", "get_mcp_prompt"} <= tool_names
        deferred_bridge = "tool_search" in tool_names
        return {
            "tools_total": len(views),
            "direct_tools": sum(
                1
                for view in views
                if bool(view.direct) and bool(getattr(view, "schema_available", False))
            ),
            "core_tools": exposure_counts.get("core", 0),
            "deferred_tools": exposure_counts.get("deferred", 0),
            "hidden_tools": exposure_counts.get("hidden", 0),
            "mcp_proxy_tools": sum(1 for name in tool_names if name.startswith("mcp__")),
            "commands": len(self._commands),
            "skills": len(self._skills),
            "mcp_resource_bridge": mcp_resource_bridge,
            "mcp_resource_template_bridge": mcp_resource_template_bridge,
            "mcp_resource_subscription_bridge": mcp_resource_subscription_bridge,
            "mcp_prompt_bridge": mcp_prompt_bridge,
            "deferred_bridge": deferred_bridge,
            "skill_catalog": bool(self._skills),
        }

    def _touch(self) -> None:
        self._version += 1
        # Schema cache entries are keyed by the direct tool fingerprint. Deferred
        # tool, command, and MCP catalog churn should not evict the stable
        # direct tools payload; direct tool changes naturally use a new key.


ToolRegistry = CapabilityRegistry


def _publish_registry_cleanup_receipt(
    context: ToolExecutionContext | None,
    tool_name: str,
    receipt: Any,
    *,
    reason: str,
) -> None:
    """Expose nested tool-task cleanup through the canonical call context."""

    if context is None:
        return
    evidence = receipt.to_evidence(
        resource_kind="tool",
        resource_id=str(
            getattr(context, "metadata", {}).get("_active_tool_call_id") or tool_name
        ),
        reason=reason,
    )
    metadata = getattr(context, "metadata", None)
    if isinstance(metadata, dict):
        metadata["_registry_cleanup_receipt"] = evidence
    call_id = str(
        getattr(context, "metadata", {}).get("_active_tool_call_id") or ""
    ).strip()
    receipts = getattr(context, "cleanup_receipts", None)
    if call_id and isinstance(receipts, dict):
        receipts[call_id] = evidence
