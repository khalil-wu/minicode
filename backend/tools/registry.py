"""Capability registry for tools plus command/skill metadata."""

from __future__ import annotations

import json
import logging
import inspect
from collections import Counter
from copy import deepcopy
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import PermissionContext
    from backend.tools.toolsets import ToolsetPolicy

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, validate_tool_input

logger = logging.getLogger(__name__)

class CapabilityRegistry:
    """In-memory registry for tools, commands, and skills."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._commands: dict[str, Any] = {}
        self._skills: dict[str, dict[str, Any]] = {}
        self._schema_cache: dict[str, list[dict[str, Any]]] = {}
        self._version = 0

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(
                "Tool name conflict detected for '%s'; overriding previous registration",
                tool.name,
            )
        self._tools[tool.name] = tool
        self._touch()

    def unregister(self, name: str) -> bool:
        removed = self._tools.pop(name, None) is not None
        if removed:
            self._touch()
        return removed

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

    def register_skill(self, name: str, definition: dict[str, Any]) -> None:
        if name in self._skills:
            logger.warning("Skill name conflict detected for '%s'; overriding previous registration", name)
        self._skills[name] = dict(definition)
        self._touch()

    def unregister_skill(self, name: str) -> bool:
        removed = self._skills.pop(name, None) is not None
        if removed:
            self._touch()
        return removed

    def get_skill(self, name: str) -> dict[str, Any] | None:
        skill = self._skills.get(name)
        return dict(skill) if skill is not None else None

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())

    def get_skills(self) -> dict[str, dict[str, Any]]:
        return {name: dict(definition) for name, definition in self._skills.items()}

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
        catalog metadata and load full schema through tool_describe.
        """
        from backend.tools.toolsets import ToolsetPolicy

        active_policy = toolset_policy or ToolsetPolicy.default()
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
        from backend.tools.toolsets import ToolsetPolicy

        return self._build_schema_view_for_tool(
            name,
            tool,
            active_policy=toolset_policy or ToolsetPolicy.default(),
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
            from backend.permissions.checker import check_permission_level

            level = check_permission_level(
                permission_checker, name, context=permission_context, tool=tool
            )
            denied = level == PermissionLevel.ALWAYS_DENY
        exposure = spec.exposure if spec else "core"
        if not policy_available:
            exposure = "hidden"
        if denied:
            exposure = "hidden"
        direct = direct and not denied
        schema_available = exposure != "hidden"
        schema = None
        if materialize_schema and direct and schema_available:
            tool_schema = tool.model_schema() or tool.get_schema()
            model_description = str(tool.model_description() or "").strip()
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
            short_description=(tool.model_description() or "").strip(),
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
            if permission_checker and permission_context:
                from backend.permissions.checker import check_permission_level

                level = check_permission_level(
                    permission_checker, name, context=permission_context, tool=tool
                )
                meta = {**meta, "permission": level.value}
            out[name] = meta
        return out

    def build_snapshot(
        self,
        budget: int = 6000,
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
                    budget,
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
                self._build_named_metadata(name, definition)
                for name, definition in sorted(self._skills.items())
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
                from backend.permissions.checker import check_permission_level

                level = check_permission_level(
                    permission_checker, name, context=permission_context, tool=tool
                )
                if level == PermissionLevel.ALWAYS_DENY:
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

        Used by tool_describe for deferred tools. This is intentionally separate
        from build_schema_views so catalog/search paths can stay schema-lazy.
        """
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
        tool_schema = tool.model_schema() or tool.get_schema()
        model_description = str(tool.model_description() or "").strip()
        if model_description and model_description != str(tool_schema.description or "").strip():
            tool_schema = tool_schema.with_description(model_description)
        return tool_schema.to_openai_tool()

    def get_schemas(
        self,
        budget: int = 6000,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
        toolset_policy: 'ToolsetPolicy | None' = None,
        mcp_registry_version: int = 0,
    ) -> list[dict[str, Any]]:
        from backend.tools.toolsets import ToolsetPolicy

        active_policy = toolset_policy or ToolsetPolicy.default()
        # mcp_registry_version is intentionally ignored for the default direct
        # schema payload: deferred/MCP catalog churn must not perturb the model's
        # tools array. Direct visibility changes still affect the fingerprint.
        direct_fingerprint = self._direct_schema_fingerprint(
            active_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
        cache_key = f"{budget}_{active_policy.cache_key()}_{direct_fingerprint}"
        if permission_checker and permission_context:
            overrides_hash = hash(frozenset(permission_context.session_overrides.items())) if permission_context.session_overrides else 0
            cache_key = (
                f"{budget}_{permission_context.mode}"
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

        try:
            if self._tool_accepts_context(tool):
                result = await tool.execute(args, context=context)
            else:
                result = await tool.execute(args)
        except Exception as exc:
            return ToolResult(
                content=(
                    f"Tool '{name}' execution failed ({type(exc).__name__}).\n"
                    "Check the arguments or try a different approach."
                ),
                is_error=True,
                developer_detail=str(exc),
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

    def _tool_accepts_context(self, tool: BaseTool) -> bool:
        try:
            return "context" in inspect.signature(tool.execute).parameters
        except (TypeError, ValueError):
            return False

    def _annotate_schema_permission(
        self,
        schema: dict[str, Any],
        level: PermissionLevel | None,
    ) -> dict[str, Any]:
        if level is None:
            return schema
        function = schema.get("function")
        if not isinstance(function, dict):
            return schema
        function["description"] = self._description_with_permission(
            str(function.get("description") or ""),
            level,
        )
        return schema

    def _description_with_permission(
        self,
        description: str,
        level: PermissionLevel | None,
    ) -> str:
        if level is None:
            return description
        label = {
            PermissionLevel.AUTO: "auto",
            PermissionLevel.CONFIRM: "requires confirmation",
            PermissionLevel.DIFF_REVIEW: "diff review",
            PermissionLevel.ALWAYS_DENY: "denied",
        }[level]
        marker = f"Permission: {label}."
        if marker in description:
            return description
        return f"{description.rstrip()} {marker}".strip()

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
        deferred_bridge = {"tool_search", "tool_describe", "tool_call"} <= tool_names
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
        # tool, command, skill, and MCP catalog churn should not evict the stable
        # direct tools payload; direct tool changes naturally use a new key.


ToolRegistry = CapabilityRegistry
