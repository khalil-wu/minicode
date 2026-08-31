import asyncio
import json

from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.llm.base import ToolCallEvent
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import (
    build_agent_execution_profile,
    build_subagent_permission_context,
)
from backend.tools.tool_search import (
    DeferredToolCatalog,
    ToolSearchTool,
    build_deferred_tools_prompt_block,
)
from backend.tools.toolsets import ToolAvailabilityFilter, ToolsetPolicy
from backend.agent.tool_schema_derivation import effective_toolset_policy


def test_default_registry_defers_optional_tools_behind_bridge(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    direct_names = {schema["function"]["name"] for schema in registry.get_schemas()}
    deferred_names = {entry.name for entry in DeferredToolCatalog(registry).entries()}

    assert "tool_search" in direct_names
    assert {"tool_describe", "tool_call"}.isdisjoint(direct_names)
    assert "read_artifact" in direct_names
    assert "web_fetch" in direct_names
    assert "web_search" in direct_names
    assert {
        "todo_write",
        "preview_server",
        "list_mcp_resources",
        "read_mcp_resource",
        "sleep",
        "skill_search",
    }.isdisjoint(direct_names)
    assert {
        "ask_user",
        "list_files",
        "task",
        "task_status",
        "task_stop",
        "send_message",
    } <= direct_names
    assert "update_plan" in direct_names
    assert {
        "preview_server",
        "list_mcp_resources",
        "read_mcp_resource",
        "sleep",
    } <= deferred_names
    assert {"todo_write", "update_plan"}.isdisjoint(deferred_names)
    assert {"web_search", "web_fetch"}.isdisjoint(deferred_names)
    assert "skill_search" not in deferred_names
    assert {
        "ask_user",
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
        "ask_user",
        "task_status",
        "task_stop",
        "send_message",
        "web_fetch",
    } <= names


def test_schema_postprocess_preserves_common_task_words(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    descriptions = {
        schema["function"]["name"]: schema["function"].get("description", "")
        for schema in registry.get_schemas()
    }

    assert "tool_search" in descriptions

    assert "Updates the task plan" in descriptions["update_plan"]
    deferred_names = {entry.name for entry in DeferredToolCatalog(registry).entries()}
    assert {"update_plan", "todo_write"}.isdisjoint(deferred_names)
def test_tool_search_selects_and_activates_optional_tools(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    state = AgentState(user_message="activate tools")
    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_agent_state": state},
    )

    search_result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "select:preview_server"},
            context=context,
        )
    )
    search_payload = json.loads(search_result.content)
    selected_names = set(search_payload["matches"])

    assert selected_names == {"preview_server"}
    assert set(search_payload["activated"]) == selected_names
    assert state.loaded_deferred_tools == selected_names

    active_policy = ToolsetPolicy.from_iterables(enabled_tools=state.loaded_deferred_tools)
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
    assert selected_names.isdisjoint(deferred_names)

    repeated = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "select:preview_server"},
            context=context,
        )
    )
    assert json.loads(repeated.content)["activated"] == []


def test_deferred_keyword_search_returns_cc_style_match_names(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    schema_result = asyncio.run(
        ToolSearchTool(registry).execute({"query": "preview dev server", "max_results": 1})
    )
    schema_payload = json.loads(schema_result.content)

    assert schema_payload["matches"] == ["preview_server"]
    assert schema_payload["total_deferred_tools"] > 0


def test_tool_search_selects_an_already_direct_tool_as_a_noop(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    state = AgentState(user_message="select an already visible tool")
    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_agent_state": state},
    )

    result = asyncio.run(
        ToolSearchTool(registry).execute(
            {"query": "select:read_file"},
            context=context,
        )
    )
    payload = json.loads(result.content)

    assert payload["matches"] == ["read_file"]
    assert payload["activated"] == []
    assert state.loaded_deferred_tools == set()


def test_no_workspace_policy_is_shared_by_schema_catalog_and_tool_search(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    policy = effective_toolset_policy(
        base_policy=ToolsetPolicy.default(),
        tool_registry=registry,
        disabled_tools=set(),
        requires_explicit_workspace=True,
        workspace_root=None,
        permission_mode="confirm",
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
            {"query": "select:read_file,grep_files,list_files,run_command"},
            context=context,
        )
    )

    workspace_tools = {"read_file", "grep_files", "list_files", "run_command"}
    assert workspace_tools.isdisjoint(direct_names)
    assert workspace_tools.isdisjoint(deferred_names)
    assert json.loads(result.content)["matches"] == []
    assert "ask_user" in direct_names


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
                metadata={"_toolset_policy": ToolsetPolicy.default()},
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


def test_deferred_tools_prompt_block_lists_names_without_schemas(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    block = build_deferred_tools_prompt_block(registry)

    assert block.startswith("<available-deferred-tools")
    assert "preview_server" in block
    assert "todo_write" not in block
    assert "update_plan" not in block
    assert "schema" not in block.lower()
    assert "description" not in block.lower()
    assert "routine task tracking" not in block
    assert '"function"' not in block


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


def test_deferred_tools_prompt_block_uses_catalog_scope(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    default_block = build_deferred_tools_prompt_block(registry)
    coordination_block = build_deferred_tools_prompt_block(registry, scope="coordination")

    assert "task_create" not in default_block
    assert "reply" not in default_block
    assert "send_message" not in coordination_block
    assert "task_create" not in coordination_block
    assert "reply" not in coordination_block


def test_tool_search_model_schema_stays_minimal(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    schema = ToolSearchTool(registry).model_schema()

    assert set(schema.parameters["properties"].keys()) == {"query", "max_results"}
    assert schema.parameters["required"] == ["query"]


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

    view = registry.get_schema_view("optional_core_tool")

    assert view is not None
    assert view.exposure == "deferred"
    assert view.direct is False
    assert registry.get_schemas() == []
    assert [entry.name for entry in DeferredToolCatalog(registry).entries()] == [
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
    registry.register(_DeferredFakeTool("ask_user"))
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
            )
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
        ToolSearchTool(registry).execute({"query": "select:ask_user,optional_lookup"}, context=context)
    )
    search_payload = json.loads(search_result.content)

    assert "optional_lookup" in entries
    assert "ask_user" not in entries
    assert "update_plan" not in entries
    assert search_payload["matches"] == ["optional_lookup"]


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
