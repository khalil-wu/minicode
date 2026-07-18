from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Callable, Iterable, get_args, get_origin

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, PermissionSettings, TokenBudget, load_config
from backend.feature_flags import feature_enabled
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


JsonSchema = dict[str, Any]
ToolCallable = Callable[..., Any]


class FunctionTool(BaseTool):
    """SDK adapter that exposes a Python callable as a MiniCode tool."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        func: ToolCallable,
        parameters: JsonSchema | None = None,
        permission: PermissionLevel | str = PermissionLevel.AUTO,
        read_only: bool = True,
        strict: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self._func = func
        self._parameters = parameters or _schema_from_callable(func)
        self.permission = _coerce_permission(permission)
        self.read_only = bool(read_only)
        self.strict = bool(strict)

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self._parameters,
            strict=self.strict,
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        kwargs = dict(args or {})
        if _callable_accepts_context(self._func):
            kwargs.setdefault("context", context)
        result = self._func(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _coerce_tool_result(result)


def tool(
    func: ToolCallable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: JsonSchema | None = None,
    permission: PermissionLevel | str = PermissionLevel.AUTO,
    read_only: bool = True,
    strict: bool = True,
) -> FunctionTool | Callable[[ToolCallable], FunctionTool]:
    """Expose a Python callable as a MiniCode tool.

    Can be used as ``tool(fn, ...)`` or ``@tool(...)``.
    """

    def _wrap(target: ToolCallable) -> FunctionTool:
        tool_name = name or target.__name__
        return FunctionTool(
            name=tool_name,
            description=description or inspect.getdoc(target) or tool_name,
            func=target,
            parameters=parameters,
            permission=permission,
            read_only=read_only,
            strict=strict,
        )

    if func is not None:
        return _wrap(func)
    return _wrap


def create_tool_registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for sdk_tool in tools:
        registry.register(sdk_tool)
    return registry


class SDKSession:
    """Stateful SDK runner with reusable context history and fork support."""

    def __init__(
        self,
        *,
        session_id: str = "sdk",
        context_builder: ContextBuilder | None = None,
        metadata: dict[str, Any] | None = None,
        **query_kwargs: Any,
    ) -> None:
        self.session_id = session_id
        self.context_builder = context_builder or ContextBuilder()
        self.metadata = dict(metadata or {"source": "sdk"})
        self._query_kwargs = dict(query_kwargs)

    async def query(self, message: str, **overrides: Any) -> AsyncIterator[AgentEvent]:
        run_kwargs = dict(self._query_kwargs)
        run_kwargs.update(overrides)
        metadata = dict(self.metadata)
        metadata.update(run_kwargs.pop("metadata", {}) or {})
        session_id = str(run_kwargs.pop("session_id", self.session_id))
        context_builder = run_kwargs.pop("context_builder", self.context_builder)
        async for event in query(
            message,
            session_id=session_id,
            context_builder=context_builder,
            metadata=metadata,
            **run_kwargs,
        ):
            yield event

    async def resume_with_context(self, message: str = "继续", **overrides: Any) -> AsyncIterator[AgentEvent]:
        async for event in self.query(message, **overrides):
            yield event

    def fork(
        self,
        *,
        session_id: str | None = None,
        system_note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "SDKSession":
        child_metadata = dict(self.metadata)
        child_metadata.update(metadata or {})
        child = SDKSession(
            session_id=session_id or f"{self.session_id}-fork",
            context_builder=_clone_context_builder(self.context_builder),
            metadata=child_metadata,
            **self._query_kwargs,
        )
        if system_note:
            child.context_builder.append_system_note(system_note)
        return child


def create_session(**kwargs: Any) -> SDKSession:
    return SDKSession(**kwargs)


def create_sdk_mcp_server(
    *tools: BaseTool,
    name: str = "minicode-sdk",
    instructions: str | None = None,
):
    """Expose SDK tools through an in-process FastMCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("create_sdk_mcp_server() requires the MCP SDK package.") from exc

    server = FastMCP(
        name,
        instructions=instructions
        or "MiniCode SDK MCP server exposing tools registered by the embedding application.",
    )
    for sdk_tool in tools:
        schema = sdk_tool.get_schema()
        server.add_tool(
            _mcp_callable_for_tool(sdk_tool),
            name=schema.name,
            description=schema.description,
        )
    return server


async def query(
    message: str,
    *,
    llm: LLMAdapter | None = None,
    tool_registry: ToolRegistry | None = None,
    artifact_store: ArtifactStore | None = None,
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    tools: Iterable[BaseTool] | None = None,
    config: AppConfig | None = None,
    agent_settings: AgentSettings | None = None,
    token_budget: TokenBudget | None = None,
    state: AgentState | None = None,
    context_builder: ContextBuilder | None = None,
    session_id: str = "sdk",
    max_iterations: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run MiniCode programmatically and yield the same AgentEvent stream as WS.

    Dependencies are injectable for tests and embedded runtimes. When omitted,
    the SDK builds the normal local config, LLM adapter, tool registry,
    artifact store, and permission checker.
    """
    if not feature_enabled("sdk_query", True):
        raise RuntimeError("MiniCode SDK query is disabled by the sdk_query feature flag.")

    config = config or load_config()
    artifact_store = artifact_store or ArtifactStore()
    if llm is None:
        from backend.llm.model_registry import create_llm_adapter

        llm = create_llm_adapter(config)
    if tool_registry is None:
        from backend.api.tool_registry import _build_tool_registry

        tool_registry = _build_tool_registry(artifact_store)
    for sdk_tool in tools or ():
        tool_registry.register(sdk_tool)
    permission_checker = permission_checker or PermissionChecker(config.permissions)
    agent_settings = agent_settings or config.agent
    token_budget = token_budget or config.token_budget
    if max_iterations is not None:
        agent_settings = replace(agent_settings, max_iterations=max_iterations)
    state = state or AgentState(user_message=message, max_iterations=agent_settings.max_iterations)
    context_builder = context_builder or ContextBuilder(
        token_budget=token_budget,
        agent_settings=agent_settings,
        llm=llm,
    )

    # Route through the same QueryEngine the WS path uses so both entry points
    # share one lifecycle: workspace-root rebinding on the permission checker
    # and adapter-only event filtering (tool_call_start/delta stay internal).
    submission = QuerySubmission(
        user_message=message,
        session=AgentSession(
            llm=llm,
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            permission_checker=permission_checker,
            agent_settings=agent_settings,
            token_budget=token_budget,
            context_builder=context_builder,
        ),
        state=state,
        runtime=AgentLoopSessionContext(
            permission_context=permission_context,
            session_id=session_id,
            metadata=metadata or {"source": "sdk"},
        ),
    )
    async for event in QueryEngine().submit(submission):
        yield event


def _schema_from_callable(func: ToolCallable) -> JsonSchema:
    signature = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if name == "context":
            continue
        properties[name] = _json_schema_for_annotation(parameter.annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _clone_context_builder(builder: ContextBuilder) -> ContextBuilder:
    return clone_context_builder(builder)


def _mcp_callable_for_tool(sdk_tool: BaseTool) -> ToolCallable:
    schema = sdk_tool.get_schema()

    async def _run(**kwargs: Any) -> str:
        result = await sdk_tool.execute(dict(kwargs), context=None)
        return result.to_context_string()

    _run.__name__ = schema.name
    _run.__doc__ = schema.description
    _run.__signature__ = _signature_from_json_schema(schema.parameters)  # type: ignore[attr-defined]
    return _run


def _signature_from_json_schema(schema: JsonSchema) -> inspect.Signature:
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    parameters: list[inspect.Parameter] = []
    for name, property_schema in properties.items():
        if not isinstance(name, str):
            continue
        default = inspect.Parameter.empty if name in required else None
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=_annotation_for_json_schema(property_schema),
            )
        )
    return inspect.Signature(parameters=parameters)


def _json_schema_for_annotation(annotation: Any) -> JsonSchema:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is not None:
        if origin is list:
            return {"type": "array"}
        if origin is dict:
            return {"type": "object"}
        if type(None) in args:
            non_none = [arg for arg in args if arg is not type(None)]
            if non_none:
                return _json_schema_for_annotation(non_none[0])
    if annotation in {str, inspect.Parameter.empty}:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in {dict, dict[str, Any]}:
        return {"type": "object"}
    if annotation in {list, list[str], list[Any]}:
        return {"type": "array"}
    return {"type": "string"}


def _annotation_for_json_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return str
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), schema_type[0] if schema_type else None)
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "object":
        return dict[str, Any]
    if schema_type == "array":
        return list[Any]
    return str


def _callable_accepts_context(func: ToolCallable) -> bool:
    try:
        parameter = inspect.signature(func).parameters.get("context")
    except (TypeError, ValueError):
        return False
    return parameter is not None


def _coerce_permission(permission: PermissionLevel | str) -> PermissionLevel:
    if isinstance(permission, PermissionLevel):
        return permission
    normalized = str(permission or "").strip().lower()
    for level in PermissionLevel:
        if normalized in {level.value, level.name.lower()}:
            return level
    return PermissionLevel.AUTO


def _coerce_tool_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(content=value)
    try:
        return ToolResult(content=json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return ToolResult(content=str(value))
