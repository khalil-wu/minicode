from __future__ import annotations

import inspect
import asyncio
from contextlib import suppress
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, get_args, get_origin
from uuid import uuid4

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.execution_journal import ExecutionJournal, execution_journal_owner
from backend.agent.state import AgentState
from backend.agent.tool_execution import execute_tool_batch
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, PermissionSettings, TokenBudget, load_config
from backend.feature_flags import feature_enabled
from backend.llm.base import LLMAdapter, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import SandboxPolicy
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
        # An arbitrary embedding callable that is not declared read-only must
        # be treated as an external side effect.  MiniCode cannot infer that it
        # only mutates the workspace, so fail toward the broader capability.
        self.mutates_external_state = not self.read_only
        self.open_world = not self.read_only
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
        self._active_query = False

    async def query(self, message: str, **overrides: Any) -> AsyncIterator[AgentEvent]:
        if self._active_query:
            raise RuntimeError(
                "SDK session is already processing a prompt. Wait for the active "
                "query to finish before submitting another one."
            )
        self._active_query = True
        run_kwargs = dict(self._query_kwargs)
        run_kwargs.update(overrides)
        metadata = dict(self.metadata)
        metadata.update(run_kwargs.pop("metadata", {}) or {})
        session_id = str(run_kwargs.pop("session_id", self.session_id))
        context_builder = run_kwargs.pop("context_builder", self.context_builder)
        stream = query(
            message,
            session_id=session_id,
            context_builder=context_builder,
            metadata=metadata,
            **run_kwargs,
        )
        try:
            async for event in stream:
                yield event
        finally:
            with suppress(Exception):
                await stream.aclose()
            self._active_query = False

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
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    workspace_root: str | Path | None = None,
    artifact_store: ArtifactStore | None = None,
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
            _mcp_callable_for_tool(
                sdk_tool,
                server_name=name,
                permission_checker=permission_checker,
                permission_context=permission_context,
                workspace_root=workspace_root,
                artifact_store=artifact_store,
            ),
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
    """Run one SDK turn under MiniCode's process-wide conversation fence."""
    effective_metadata = dict(metadata or {"source": "sdk"})
    conversation_id = str(effective_metadata.get("conversation_id") or "").strip()
    owner_session_id = str(effective_metadata.get("session_id") or session_id or "sdk").strip()
    claim = conversation_query_guards().try_start(
        conversation_id,
        owner_id=f"sdk:{owner_session_id}:{uuid4().hex}",
    )
    if claim is None:
        active = conversation_query_guards().active_claim(conversation_id)
        error = AgentEvent.error(
            "This conversation already has an active turn.",
            recoverable=True,
            error_type="conversation_busy",
        )
        error.data["conversation_id"] = conversation_id
        yield error
        done = AgentEvent.done(status="failed", reason="conversation_busy")
        done.data["conversation_id"] = conversation_id
        if active is not None:
            done.data["active_generation"] = active.generation
        yield done
        return
    try:
        async for event in _query_unclaimed(
            message,
            llm=llm,
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            permission_checker=permission_checker,
            permission_context=permission_context,
            tools=tools,
            config=config,
            agent_settings=agent_settings,
            token_budget=token_budget,
            state=state,
            context_builder=context_builder,
            session_id=session_id,
            max_iterations=max_iterations,
            metadata=effective_metadata,
        ):
            if not conversation_query_guards().owns(claim):
                done = AgentEvent.done(status="cancelled", reason="conversation_ownership_lost")
                done.data["conversation_id"] = conversation_id
                yield done
                return
            yield event
    finally:
        conversation_query_guards().end(claim)


async def _query_unclaimed(
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
        from backend.llm.model_registry import create_session_llm

        llm = create_session_llm(config)
    if tool_registry is None:
        from backend.services.tool_registry_factory import build_tool_registry

        tool_registry = build_tool_registry(artifact_store)
    for sdk_tool in tools or ():
        tool_registry.register(sdk_tool)
    permission_checker = permission_checker or PermissionChecker(config.permissions)
    agent_settings = agent_settings or config.agent
    token_budget = token_budget or config.token_budget
    if max_iterations is not None:
        agent_settings = replace(agent_settings, max_iterations=max_iterations)
    state = state or AgentState(user_message=message, max_iterations=agent_settings.max_iterations)
    state.conversation_id = str(
        (metadata or {}).get("conversation_id") or state.conversation_id or ""
    ).strip()
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


def _mcp_callable_for_tool(
    sdk_tool: BaseTool,
    *,
    server_name: str = "minicode-sdk",
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    workspace_root: str | Path | None = None,
    artifact_store: ArtifactStore | None = None,
) -> ToolCallable:
    schema = sdk_tool.get_schema()

    async def _run(**kwargs: Any) -> str:
        from backend.tools.base import validate_tool_input

        arguments = dict(kwargs)
        validation_error = validate_tool_input(sdk_tool, arguments)
        if validation_error:
            return f"Tool input rejected: {validation_error}"
        declared = getattr(sdk_tool, "permission", PermissionLevel.AUTO)
        try:
            level = declared if isinstance(declared, PermissionLevel) else PermissionLevel(str(declared))
        except ValueError:
            level = PermissionLevel.CONFIRM
        # An MCP server call has no MiniCode approval channel.  Only tools
        # explicitly classified as automatic may cross this embedding API.
        if level != PermissionLevel.AUTO:
            return "Tool execution rejected: this capability requires MiniCode approval."
        resolved_workspace = Path(workspace_root or Path.cwd()).expanduser().resolve()
        effective_permission = permission_context or PermissionContext(
            mode="confirm",
            workspace_root=resolved_workspace,
            conversation_id=f"sdk-mcp:{server_name}",
        )
        checker = permission_checker or PermissionChecker(
            PermissionSettings(auto_allow=[schema.name]),
            resolved_workspace,
        )
        registry = create_tool_registry(sdk_tool)
        store = artifact_store or ArtifactStore()
        sandbox_policy = SandboxPolicy.workspace_default(resolved_workspace)
        context = ToolExecutionContext(
            permission=effective_permission,
            session_id="sdk-mcp",
            metadata={
                "source": "sdk-mcp",
                "server_name": server_name,
                "workspace_root": str(resolved_workspace),
            },
            workspace_root=resolved_workspace,
            allow_network=sandbox_policy.allow_network,
            sandbox_policy=sandbox_policy,
            permission_checker=checker,
            conversation_id=f"sdk-mcp:{server_name}",
            artifact_store=store,
        )
        call_id = f"sdk-mcp-{uuid4().hex}"
        state = AgentState(user_message=f"MCP tool call: {schema.name}", max_iterations=1)
        model_context = ContextBuilder()
        journal = ExecutionJournal(
            execution_journal_owner("sdk_mcp", server_name, schema.name)
        )
        journal.append(
            "tool_use",
            {
                "tool_call": {
                    "id": call_id,
                    "name": schema.name,
                    "arguments": arguments,
                },
                "lifecycle": "tool_claimed",
                "source": "sdk-mcp",
            },
        )
        result_event: AgentEvent | None = None
        try:
            async for event in execute_tool_batch(
                [ToolCallEvent(id=call_id, name=schema.name, arguments=arguments)],
                ctx=model_context,
                state=state,
                tool_registry=registry,
                permission_checker=checker,
                approval_handler=None,
                skill_manager=None,
                permission_context=effective_permission,
                tool_ctx=context,
            ):
                if event.type == "tool_result":
                    result_event = event
        except asyncio.CancelledError:
            receipt = dict(context.cleanup_receipts.get(call_id) or {})
            if receipt:
                journal.append_cleanup(
                    {
                        "resource_kind": "tool",
                        "resource_id": call_id,
                        "status": "pending" if receipt.get("pending") else "completed",
                        "reason": "sdk_mcp_cancelled",
                        "details": receipt,
                    }
                )
            raise

        if result_event is None or not state.tool_calls:
            journal.append(
                "tool_result",
                {
                    "tool_call_id": call_id,
                    "tool_name": schema.name,
                    "content": "MiniCode tool execution ended without a canonical result.",
                    "status": "failed",
                    "lifecycle": "tool_completed",
                },
            )
            return "Tool execution failed: MiniCode did not produce a canonical result."

        record = state.tool_calls[-1]
        journal.append(
            "tool_result",
            {
                "tool_call_id": call_id,
                "tool_name": schema.name,
                "content": str(record.tool_output or ""),
                "status": record.status,
                "request_digest": record.request_digest,
                "cleanup_receipt": dict(record.cleanup_receipt),
                "lifecycle": "tool_completed",
            },
        )
        return str(record.tool_output or result_event.data.get("summary") or "")

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
