import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool
from backend.artifact.store import ArtifactStore
from backend.config import LLMSettings, PermissionSettings, TokenBudget
from backend.agent.context import ContextBuilder
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.llm.base import ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter, _responses_function_call_output_item
from backend.llm.provider_contracts import ModelDefinition
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import (
    build_agent_execution_profile,
    build_subagent_permission_context,
)
from backend.tools.tool_search import DeferredToolCatalog, ToolSearchTool
from backend.tools.toolsets import ToolAvailabilityFilter, ToolsetPolicy
from backend.agent.tool_schema_derivation import derive_turn_tool_schema_state, effective_toolset_policy, requested_tool_mode
from backend.tests.test_code_execution import run_model
from backend.tests.test_harness_native_tool_protocol import ResponsesEndpoint, completed


SEARCH_POLICY = ToolsetPolicy(supports_search_tool=True)


def search_names(result):
    assert json.loads(result.content) == result.tool_search_output
    return {tool["name"] for namespace in result.tool_search_output for tool in namespace["tools"]}


@pytest.mark.parametrize("terminal_only", [False, True])
def test_native_search_namespace_call_and_journal_recovery_use_the_actual_responses_chain(tmp_path, terminal_only):
    async def scenario():
        calls = []

        async def invoke(name, arguments, *, request_owner):
            calls.append((name, arguments, request_owner))
            return MCPCallResult(content=[{"type": "text", "text": "Inspected counter: 7"}])

        tool = MCPToolProxy("catalog", MCPToolDef("inspect_counter", "Inspect a counter", annotations={"readOnlyHint": True}),
            SimpleNamespace(connected=True, call_tool=invoke))
        search = {"type": "tool_search_call", "id": "native-discovery", "call_id": "search-owned",
            "execution": "client", "status": "completed", "arguments": {"query": "inspect counter", "limit": 1}}
        search_events = [completed([search])] if terminal_only else [
            {"type": "response.output_item.added", "output_index": 0, "item": {**search, "status": "in_progress", "arguments": {}}},
            {"type": "response.output_item.done", "output_index": 0, "item": search},
            completed([search]),
        ]
        invoke_item = {"type": "function_call", "id": "mcp-invoke", "call_id": "invoke-owned", "status": "completed",
            "namespace": tool.model_namespace, "name": tool.model_tool_name, "arguments": "{}"}
        endpoint = ResponsesEndpoint([search_events, [completed([invoke_item])], [completed([
            {"type": "message", "id": "final", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "The counter was inspected."}]},
        ])]])
        settings = LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="responses",
            tool_mode="direct", supports_search_tool=True, max_tokens=1024)
        selected = ModelDefinition(provider=settings.provider, id=settings.model, name="Search-capable Responses fixture",
            api="openai-responses", base_url=settings.base_url, tool_mode="direct", supports_search_tool=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings, http_client=client)
            state, builder, journal, events, owner = await run_model(tmp_path, adapter, [tool], model_info=selected)
            assert state.terminal_status == "completed", state.stopped_reason
            assert owner.model_execution.model_info is selected
            assert [(name, args) for name, args, _ in calls] == [("inspect_counter", {})]
            assert calls[0][2]["conversation_id"] == "code-conv"
            first_tools = endpoint.requests[0]["tools"]
            assert any(item["type"] == "tool_search" and item["execution"] == "client" for item in first_tools)
            assert not any(item.get("name") == tool.name for item in first_tools)
            searched = next(item for item in endpoint.requests[1]["input"] if item.get("type") == "tool_search_output")
            assert searched["call_id"] == "search-owned"
            assert searched["execution"] == "client"
            assert searched["status"] == "completed"
            [namespace] = searched["tools"]
            assert namespace["type"] == "namespace" and namespace["name"] == tool.model_namespace
            assert namespace["tools"][0]["name"] == tool.model_tool_name
            assert namespace["tools"][0]["defer_loading"] is True
            assert sum(event.type == "tool_call" and event.data.get("name") == "tool_search" and event.data.get("status") != "pending"
                for event in events) == 1
            assert sum(record.tool_name == "tool_search" for record in state.tool_calls) == 1
            assert sum(event.type == "tool_result" and event.data.get("id") == "search-owned" for event in events) == 1
            recorded = next(item for item in endpoint.requests[2]["input"] if item.get("call_id") == "invoke-owned" and item.get("type") == "function_call")
            assert recorded["namespace"] == tool.model_namespace and recorded["name"] == tool.model_tool_name
            assert any(item.get("call_id") == "invoke-owned" and "Inspected counter: 7" in str(item.get("output"))
                for item in endpoint.requests[2]["input"] if item.get("type") == "function_call_output")
            snapshot = builder.export_snapshot()
            snapshot["history"] = journal.reconstruct_history()
            cold = ContextBuilder(llm=adapter, token_budget=TokenBudget(total=96000), workspace_root=tmp_path)
            cold.load_snapshot(snapshot)
            restored = next(message for message in cold._history if message.name == "tool_search")
            assert _responses_function_call_output_item(restored) == searched
            call = next(call for message in cold._history if message.tool_calls for call in message.tool_calls if call.id == "invoke-owned")
            assert call.native_namespace == tool.model_namespace and call.native_name == tool.model_tool_name
            assert "tool_search_output" not in json.dumps([event.data for event in events if event.type == "tool_result"])
    asyncio.run(scenario())


def test_model_without_search_capability_receives_mcp_schema_on_first_call():
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    tool = MCPToolProxy("catalog", MCPToolDef("inspect_counter", "Inspect a counter", annotations={"readOnlyHint": True}), None)
    registry.register(tool)
    assert {schema["function"]["name"] for schema in registry.get_schemas()} == {tool.name}
    assert {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=SEARCH_POLICY)} == {"tool_search"}


def test_search_default_and_explicit_limits_use_source_native_definitions():
    registry = ToolRegistry()
    for index in range(24):
        registry.register(MCPToolProxy("catalog", MCPToolDef(f"inspect_counter_{index}", "Inspect a counter", annotations={"readOnlyHint": True}), None))
    search = ToolSearchTool(registry)
    default = asyncio.run(search.execute({"query": "inspect counter"}))
    [namespace] = default.tool_search_output
    assert len(namespace["tools"]) == 8
    expanded = asyncio.run(search.execute({"query": "inspect counter", "limit": 24}))
    assert len(expanded.tool_search_output[0]["tools"]) == 24
    assert all(tool["defer_loading"] is True for tool in expanded.tool_search_output[0]["tools"])


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_search_rejects_invalid_source_limits_without_catalog_side_effects(limit):
    registry = ToolRegistry()
    state = AgentState(user_message="find tools")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), tool_registry=registry,
        metadata={"_agent_state": state, "_toolset_policy": SEARCH_POLICY})
    result = asyncio.run(ToolSearchTool(registry).execute({"query": "inspect", "limit": limit}, context=context))
    assert result.is_error
    assert result.tool_search_output is None
    assert "limit" in result.content
    assert state.loaded_deferred_tools == set()


def test_search_capable_registry_defers_optional_tools_behind_bridge(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    direct_names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=SEARCH_POLICY)}
    deferred_names = {entry.name for entry in DeferredToolCatalog(registry, toolset_policy=SEARCH_POLICY).entries()}

    assert "tool_search" in direct_names
    assert {"tool_describe", "tool_call"}.isdisjoint(direct_names)
    assert "read_artifact" in direct_names
    assert "web_fetch" in direct_names
    assert "web_search" in direct_names
    resource_names = {"list_mcp_resources", "read_mcp_resource", "list_mcp_resource_templates"}
    assert resource_names <= direct_names
    assert {
        "todo_write",
        "preview_server",
        "sleep",
        "skill_search",
    }.isdisjoint(direct_names)
    assert {
        "request_user_input",
        "list_files",
        "task",
        "task_status",
        "task_stop",
        "send_message",
    } <= direct_names
    assert "update_plan" in direct_names
    assert {
        "preview_server",
        "sleep",
    } <= deferred_names
    assert resource_names.isdisjoint(deferred_names)
    assert {"todo_write", "update_plan"}.isdisjoint(deferred_names)
    assert {"web_search", "web_fetch"}.isdisjoint(deferred_names)
    assert "skill_search" not in deferred_names
    assert {
        "request_user_input",
        "list_files",
        "task",
        "task_status",
        "task_stop",
        "send_message",
    }.isdisjoint(deferred_names)


def test_default_schema_budget_never_hides_core_execution_entrypoints(tmp_path) -> None:
    """The provider's real default schema budget must retain core capabilities."""
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    names = {
        schema["function"]["name"]
        for schema in registry.get_schemas()
    }

    assert {
        "task",
        "list_files",
        "request_user_input",
        "task_status",
        "task_stop",
        "send_message",
        "web_fetch",
    } <= names


def test_code_mode_catalog_lists_nested_tools_without_disabled_capabilities(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    policy = ToolsetPolicy(code_mode_only=True, disabled_tools=frozenset({"run_command"}))
    schemas = registry.get_schemas(toolset_policy=policy)
    names = {schema["function"]["name"] for schema in schemas}
    tool_exec = next(schema for schema in schemas if schema["function"]["name"] == "tool_exec")

    assert "read_file" not in names
    assert "- tools.read_file(" in tool_exec["function"]["description"]
    assert "- tools.run_command(" not in tool_exec["function"]["description"]
    assert "- tools.read_file(" in tool_exec["_minicode_freeform"]["description"]


def test_model_tool_modes_choose_distinct_direct_surfaces(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    observed = {}
    for mode in ("direct", "code_mode", "code_mode_only"):
        policy = effective_toolset_policy(
            base_policy=ToolsetPolicy.default(), tool_registry=registry,
            disabled_tools=set(), requires_explicit_workspace=False,
            workspace_root=tmp_path, permission_mode="bypass", tool_mode=mode,
        )
        observed[mode] = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}

    assert "read_file" in observed["direct"] and "tool_exec" not in observed["direct"]
    assert {"read_file", "tool_exec"} <= observed["code_mode"]
    assert "read_file" not in observed["code_mode_only"] and "tool_exec" in observed["code_mode_only"]


def test_empty_model_tool_mode_uses_explicit_adapter_setting() -> None:
    from types import SimpleNamespace

    llm = SimpleNamespace(configured_tool_mode=lambda: "direct")
    snapshot = SimpleNamespace(
        model_info=SimpleNamespace(tool_mode=""),
        config=SimpleNamespace(llm=SimpleNamespace(tool_mode="direct")),
    )
    assert requested_tool_mode(default_code_mode_only=True, model_execution=snapshot, llm=llm) == "direct"
    assert "Searches over deferred tool metadata with BM25" in ToolSearchTool().model_description()
    assert "select:" not in ToolSearchTool().model_description()


def test_per_tool_exposure_separates_direct_nested_and_deferred_surfaces() -> None:
    class SurfaceTool(BaseTool):
        read_only = True

        def __init__(self, name, exposure):
            self.name = name
            self.exposure = exposure

        def get_spec(self):
            return ToolSpec(name=self.name, exposure=self.exposure)

        def get_schema(self):
            return ToolSchema(self.name, self.name, {"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            return ToolResult(self.name)

    registry = ToolRegistry()
    for tool in (
        ToolExecTool(), ToolWaitTool(), ToolSearchTool(registry),
        SurfaceTool("model_only", "direct_model_only"),
        SurfaceTool("nested_only", "code_mode_only"),
        SurfaceTool("deferred_model", "deferred_model_only"),
    ):
        registry.register(tool)

    model_policy = ToolsetPolicy(code_mode_only=True, supports_search_tool=True)
    direct = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=model_policy)}
    assert "model_only" in direct
    assert "nested_only" not in direct
    assert "deferred_model" not in direct
    nested_policy = replace(model_policy, nested_surface=True, code_mode_only=False)
    nested = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=nested_policy)}
    assert "nested_only" in nested
    assert "model_only" not in nested
    assert "deferred_model" not in nested
    assert "deferred_model" in {entry.name for entry in DeferredToolCatalog(registry, toolset_policy=model_policy).entries()}
    state = AgentState(user_message="Find the deferred tool")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        metadata={"_agent_state": state, "_toolset_policy": model_policy},
        tool_registry=registry,
    )
    discovered = asyncio.run(ToolSearchTool(registry).execute({"query": "deferred_model"}, context=context))
    assert search_names(discovered) == {"deferred_model"}
    assert state.loaded_deferred_tools == {"deferred_model"}
    activated = replace(model_policy, enabled_tools=frozenset({"deferred_model"}))
    assert "deferred_model" in {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=activated)}


def test_schema_postprocess_preserves_common_task_words(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    descriptions = {
        schema["function"]["name"]: schema["function"].get("description", "")
        for schema in registry.get_schemas(toolset_policy=SEARCH_POLICY)
    }

    assert "tool_search" in descriptions

    assert "Updates the task plan" in descriptions["update_plan"]
    deferred_names = {entry.name for entry in DeferredToolCatalog(registry, toolset_policy=SEARCH_POLICY).entries()}
    assert {"update_plan", "todo_write"}.isdisjoint(deferred_names)
def test_tool_search_returns_namespaces_and_activates_optional_tools(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    state = AgentState(user_message="activate tools")
    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_agent_state": state, "_toolset_policy": SEARCH_POLICY},
    )

    search_result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "preview_server", "limit": 1},
            context=context,
        )
    )
    selected_names = search_names(search_result)

    assert selected_names == {"preview_server"}
    assert state.loaded_deferred_tools == selected_names

    active_policy = ToolsetPolicy.from_iterables(enabled_tools=state.loaded_deferred_tools, supports_search_tool=True)
    direct_names = {
        schema["function"]["name"]
        for schema in registry.get_schemas(toolset_policy=active_policy)
    }
    deferred_names = {
        entry.name
        for entry in DeferredToolCatalog(
            registry,
            toolset_policy=active_policy,
        ).entries()
    }
    assert selected_names <= direct_names
    # Source retains loaded definitions in the searchable catalog.
    assert selected_names <= deferred_names

    repeated = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "preview_server", "limit": 1},
            context=context,
        )
    )
    assert search_names(repeated) == selected_names
    assert state.loaded_deferred_tools == selected_names


def test_deferred_keyword_search_returns_source_namespace_definitions(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    schema_result = asyncio.run(
        ToolSearchTool(registry).execute({"query": "preview dev server", "limit": 1})
    )
    assert search_names(schema_result) == {"preview_server"}
    namespace = schema_result.tool_search_output[0]
    assert namespace["type"] == "namespace" and namespace["name"] == "functions"
    assert namespace["tools"][0]["type"] == "function"
    assert namespace["tools"][0]["defer_loading"] is True


def test_tool_search_does_not_return_core_tools(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    state = AgentState(user_message="select an already visible tool")
    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_agent_state": state, "_toolset_policy": SEARCH_POLICY},
    )

    result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "read_file"},
            context=context,
        )
    )
    assert "read_file" not in search_names(result)
    assert "read_file" not in state.loaded_deferred_tools
    assert state.loaded_deferred_tools == search_names(result)


def test_no_workspace_policy_is_shared_by_schema_catalog_and_tool_search(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    policy = effective_toolset_policy(
        base_policy=ToolsetPolicy.default(),
        tool_registry=registry,
        disabled_tools=set(),
        requires_explicit_workspace=True,
        workspace_root=None,
        permission_mode="confirm",
        supports_search_tool=True,
    )
    direct_names = {
        schema["function"]["name"]
        for schema in registry.get_schemas(toolset_policy=policy)
    }
    deferred_names = {
        entry.name
        for entry in DeferredToolCatalog(registry, toolset_policy=policy).entries()
    }
    state = AgentState(user_message="inspect code")
    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_agent_state": state, "_toolset_policy": policy},
    )
    result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "read_file grep_files list_files run_command"},
            context=context,
        )
    )

    workspace_tools = {"read_file", "grep_files", "list_files", "run_command"}
    assert workspace_tools.isdisjoint(direct_names)
    assert workspace_tools.isdisjoint(deferred_names)
    assert workspace_tools.isdisjoint(search_names(result))
    assert "request_user_input" in direct_names


def test_execution_boundary_rejects_unactivated_deferred_tool() -> None:
    class DeferredCounterTool(BaseTool):
        name = "deferred_counter"
        permission = PermissionLevel.AUTO
        calls = 0

        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name=self.name,
                capability="test.deferred",
                toolset="optional",
                exposure="deferred",
            )

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Count executions",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None) -> ToolResult:
            self.calls += 1
            return ToolResult(content="executed")

    tool = DeferredCounterTool()
    registry = ToolRegistry()
    registry.register(tool)
    tc = ToolCallEvent(id="call-deferred", name=tool.name, arguments={})

    blocked = asyncio.run(
        run_tool(
            tc,
            registry,
            ToolExecutionContext(
                permission=PermissionContext(mode="bypass"),
                metadata={"_toolset_policy": SEARCH_POLICY},
            ),
        )
    )
    assert blocked.is_error is True
    assert blocked.error_kind == "tool_unavailable"
    assert "has not been activated" in blocked.content
    assert tool.calls == 0

    active = asyncio.run(
        run_tool(
            tc,
            registry,
            ToolExecutionContext(
                permission=PermissionContext(mode="bypass"),
                metadata={
                    "_toolset_policy": ToolsetPolicy.from_iterables(
                        enabled_tools={tool.name},
                        supports_search_tool=True,
                    )
                },
            ),
        )
    )
    assert active.is_error is False
    assert active.content == "executed"
    assert tool.calls == 1


def test_availability_filter_mapping_treats_strings_as_single_values() -> None:
    clause = ToolAvailabilityFilter.from_mapping(
        {
            "tools": "read_file",
            "toolsets": ["mcp"],
            "capabilities": "workspace.read",
        }
    )

    assert clause.tools == {"read_file"}
    assert clause.toolsets == {"mcp"}
    assert clause.capabilities == {"workspace.read"}

    try:
        ToolAvailabilityFilter.from_mapping({"tools": 42})
    except ValueError as exc:
        assert "must be a string or list" in str(exc)
    else:  # pragma: no cover - explicit fail-closed contract
        raise AssertionError("invalid availability filter must fail closed")


def test_source_guidance_has_no_synthetic_available_tools_directory(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    state = derive_turn_tool_schema_state(
        base_tool_schemas=registry.get_schemas(toolset_policy=SEARCH_POLICY),
        mcp_instructions={}, tool_registry=registry, toolset_policy=SEARCH_POLICY,
    )
    assert state.deferred_tools_prompt_block == ""
    assert "available-deferred-tools" not in state.runtime_guidance


def test_coordination_tools_are_direct_not_deferred(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    direct_names = {schema["function"]["name"] for schema in registry.get_schemas()}
    default_names = {entry.name for entry in DeferredToolCatalog(registry).entries()}
    coordination_names = {
        entry.name for entry in DeferredToolCatalog(registry, scope="coordination").entries()
    }

    coordination_tools = {
        "message_list",
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "task_output",
        "team_create",
        "team_list",
        "team_delete",
    }
    assert coordination_tools <= direct_names
    assert coordination_tools.isdisjoint(default_names)
    assert coordination_names == set()


def test_deferred_catalog_respects_its_scope_without_prompt_directory(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    default_names = {entry.name for entry in DeferredToolCatalog(registry, toolset_policy=SEARCH_POLICY).entries()}
    coordination_names = {entry.name for entry in DeferredToolCatalog(registry, toolset_policy=SEARCH_POLICY, scope="coordination").entries()}
    assert {"task_create", "reply"}.isdisjoint(default_names)
    assert {"send_message", "task_create", "reply"}.isdisjoint(coordination_names)


def test_tool_search_model_schema_stays_minimal(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    schema = ToolSearchTool(registry).model_schema()

    assert set(schema.parameters["properties"].keys()) == {"query", "limit"}
    assert schema.parameters["required"] == ["query"]
    assert schema.parameters["additionalProperties"] is False
    assert "Defaults to 8" in schema.parameters["properties"]["limit"]["description"]


def test_should_defer_hint_moves_core_spec_out_of_direct_schema() -> None:
    class OptionalCoreTool(BaseTool):
        name = "optional_core_tool"
        description = "Optional core-shaped tool"
        should_defer = True

        def get_spec(self) -> ToolSpec:
            return ToolSpec(name=self.name, exposure="core", toolset="core")

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None):
            return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.register(OptionalCoreTool())

    view = registry.get_schema_view("optional_core_tool", toolset_policy=SEARCH_POLICY)

    assert view is not None
    assert view.exposure == "deferred"
    assert view.direct is False
    assert registry.get_schemas(toolset_policy=SEARCH_POLICY) == []
    assert [entry.name for entry in DeferredToolCatalog(registry, toolset_policy=SEARCH_POLICY).entries()] == [
        "optional_core_tool"
    ]


class _DeferredFakeTool(BaseTool):
    description = "Deferred fake tool"
    read_only = True
    should_defer = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.calls += 1
        return ToolResult(content=f"{self.name} ok")


def test_subagent_deferred_catalog_filters_denied_parent_tools() -> None:
    registry = ToolRegistry()
    registry.register(_DeferredFakeTool("request_user_input"))
    registry.register(_DeferredFakeTool("update_plan"))
    registry.register(_DeferredFakeTool("optional_lookup"))
    sub_permission = build_subagent_permission_context(
        "implement",
        ToolExecutionContext(permission=PermissionContext(mode="bypass")),
    )
    checker = PermissionChecker(PermissionSettings())
    context = ToolExecutionContext(
        permission=sub_permission,
        permission_checker=checker,
        metadata={
            "_agent_execution_profile": build_agent_execution_profile(
                background=False,
            ),
            "_session_toolset_policy": SEARCH_POLICY,
        },
    )

    entries = {
        entry.name
        for entry in DeferredToolCatalog(
            registry,
            permission_checker=checker,
            permission_context=sub_permission,
        ).entries()
    }
    search_result = asyncio.run(
        ToolSearchTool(registry).execute({"query": "request_user_input optional_lookup"}, context=context)
    )
    assert "optional_lookup" in entries
    assert "request_user_input" not in entries
    assert "update_plan" not in entries
    assert search_names(search_result) == {"optional_lookup"}


def test_enabled_toolsets_is_a_whitelist_that_can_exclude_by_omission(tmp_path) -> None:
    """Naming concrete groups must mean "only these groups".

    ``exposure == "core"`` used to be visible whenever the ``core`` *toolset* was
    enabled, regardless of the tool's own group, so selecting ``{"core"}`` still
    exposed run_command/monitor/update_plan (``default``), the agent group and
    read_artifact (``artifact``) — a caller could never exclude a group by
    leaving it out. The default selection is the ``*`` sentinel so plugins that
    register their own group stay visible without enumerating groups here.
    """
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    specs = [registry.get_tool_spec(name) for name in registry._tools]

    default_visible = {s.name for s in specs if ToolsetPolicy.default().is_directly_visible(s)}
    assert {"run_command", "read_artifact", "send_message", "update_plan"} <= default_visible

    core_only = ToolsetPolicy.from_iterables(enabled_toolsets=["core"])
    core_visible = {s.name for s in specs if core_only.is_directly_visible(s)}
    assert {"read_file", "write_file", "grep_files"} <= core_visible
    assert core_visible.isdisjoint({"run_command", "read_artifact", "send_message", "update_plan"})

    with_agent = ToolsetPolicy.from_iterables(enabled_toolsets=["core", "agent"])
    assert "send_message" in {s.name for s in specs if with_agent.is_directly_visible(s)}


def test_active_tool_selection_cannot_widen_session_ceiling() -> None:
    """Pi-style active selection is an intersection, never a capability grant."""

    read = ToolSpec(name="read_file", toolset="core", exposure="core")
    write = ToolSpec(name="write_file", toolset="core", exposure="core")
    optional = ToolSpec(
        name="preview_server",
        toolset="preview",
        exposure="deferred",
    )

    ceiling = ToolsetPolicy.from_iterables(
        enabled_toolsets=(),
        enabled_tools=["read_file"],
        disabled_tools=["write_file"],
    )
    selected = ceiling.with_active_tool_selection(
        ["read_file", "write_file", "preview_server"]
    )

    assert selected.is_available(read)
    assert selected.is_directly_visible(read)
    assert not selected.is_available(write)
    # The active list must not re-open a name outside the parent's exact
    # whitelist, even though it is present in the UI selection.
    assert not selected.is_available(optional)
    assert not selected.is_directly_visible(optional)


def test_active_tool_selection_makes_selected_deferred_tool_direct() -> None:
    optional = ToolSpec(
        name="preview_server",
        toolset="preview",
        exposure="deferred",
    )

    selected = ToolsetPolicy.default().with_active_tool_selection(
        ["preview_server"]
    )

    assert selected.is_available(optional)
    assert selected.is_directly_visible(optional)


def test_registry_rejects_a_selection_naming_unknown_tools_or_toolsets(tmp_path) -> None:
    """An unresolvable whitelist silently shrank the surface; it must raise."""
    import pytest

    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    with pytest.raises(ValueError, match="totally-bogus"):
        registry.get_schemas(
            toolset_policy=ToolsetPolicy.from_iterables(enabled_toolsets=["totally-bogus"])
        )
    with pytest.raises(ValueError, match="nope_not_a_tool"):
        registry.get_schemas(
            toolset_policy=ToolsetPolicy.from_iterables(enabled_tools=["nope_not_a_tool"])
        )
    # Attenuating directions stay tolerant: removing a capability that is not
    # installed is already satisfied, and safety profiles name optional groups.
    registry.get_schemas(
        toolset_policy=ToolsetPolicy.from_iterables(
            disabled_toolsets=["not-installed"], disabled_tools=["not_installed"]
        )
    )


def test_runtime_review_same_object_replacement_refreshes_only_visible_schemas():
    from backend.tools.base import BaseTool, ToolSchema
    from backend.tools.registry import ToolRegistry
    from backend.tools.toolsets import ToolsetPolicy

    class RevisedDirectTool(BaseTool):
        name = "runtime_review_direct"
        description = "original description"
        field = "old_arg"
        def get_schema(self):
            return ToolSchema(name=self.name, description=self.description, parameters={
                "type": "object", "properties": {self.field: {"type": "string"}},
                "required": [self.field],
            })
        async def execute(self, args, context=None):
            raise AssertionError("schema-only verification must not execute")

    class DeferredTool(RevisedDirectTool):
        name = "runtime_review_deferred"
        should_defer = True

    registry = ToolRegistry()
    direct = RevisedDirectTool()
    deferred = DeferredTool()
    registry.register(direct)
    registry.register(deferred)
    default_schemas = registry.get_schemas(toolset_policy=SEARCH_POLICY)
    assert default_schemas[0]["function"]["parameters"]["required"] == ["old_arg"]
    deferred.field = "changed_deferred_arg"
    registry.register(deferred, replace=True)
    assert registry.get_schemas(toolset_policy=SEARCH_POLICY) is default_schemas
    fork = registry.fork()
    assert fork.get_schemas(toolset_policy=SEARCH_POLICY) == default_schemas
    direct.field = "new_arg"
    direct.description = "updated description"
    registry.register(direct, replace=True)
    refreshed = registry.get_schemas(toolset_policy=SEARCH_POLICY)
    assert refreshed[0]["function"]["parameters"]["required"] == ["new_arg"]
    assert refreshed[0]["function"]["description"] == "updated description"
    assert default_schemas[0]["function"]["parameters"]["required"] == ["old_arg"]
    fork.register(direct, replace=True)
    assert fork.get_schemas(toolset_policy=SEARCH_POLICY)[0]["function"]["parameters"]["required"] == ["new_arg"]
    activated = ToolsetPolicy.from_iterables(enabled_tools=[deferred.name], supports_search_tool=True)
    visible_deferred = registry.get_schemas(toolset_policy=activated)
    deferred.field = "another_deferred_arg"
    registry.register(deferred, replace=True)
    changed = registry.get_schemas(toolset_policy=activated)
    before = next(s for s in visible_deferred if s["function"]["name"] == deferred.name)
    after = next(s for s in changed if s["function"]["name"] == deferred.name)
    assert before["function"]["parameters"]["required"] == ["changed_deferred_arg"]
    assert after["function"]["parameters"]["required"] == ["another_deferred_arg"]
