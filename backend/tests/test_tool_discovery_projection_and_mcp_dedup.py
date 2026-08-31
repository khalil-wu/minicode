from __future__ import annotations

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_events import tool_call_start_event
from backend.agent.tool_execution import store_result
from backend.llm.base import ToolCallEvent
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolRegistry
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool


class _NativeTool(BaseTool):
    description = "Native equivalent"

    def __init__(self, name: str) -> None:
        self.name = name

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict) -> ToolResult:
        return ToolResult(content="ok")


class _ExternalWebsearchClient:
    connected = True
    _command = "node"
    _args = ["custom-search-server.js"]


def _websearch_definitions() -> list[MCPToolDef]:
    return [
        MCPToolDef(name="search", description="Search the web"),
        MCPToolDef(name="fetch_page", description="Fetch a page"),
    ]


def test_tool_search_owns_a_reviewable_projection_label() -> None:
    tool = ToolSearchTool()

    assert tool.to_projection_metadata() == {
        "result_kind": "generic",
        "activity_kind": "genericTool",
        "display_label": "Find tools",
        "visibility": "debug",
    }


def test_tool_search_success_is_debug_but_failure_is_timeline() -> None:
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    call = ToolCallEvent(
        id="search-1",
        name="tool_search",
        arguments={"query": "select:read_file"},
    )

    start = tool_call_start_event(
        call,
        started_epoch=1.0,
        iteration_id="iter:1",
        tool_registry=registry,
    )
    success = store_result(
        call,
        ToolResult(content='{"matches": []}'),
        ContextBuilder(),
        AgentState(user_message="find tools"),
        tool_registry=registry,
    )
    failure = store_result(
        call,
        ToolResult(content="registry unavailable", is_error=True),
        ContextBuilder(),
        AgentState(user_message="find tools"),
        tool_registry=registry,
    )

    assert start.data["visibility"] == "debug"
    assert success.data["visibility"] == "debug"
    assert failure.data["visibility"] == "timeline"


def test_external_search_connector_is_not_hidden_by_native_web_tools() -> None:
    registry = ToolRegistry()
    registry.register(_NativeTool("web_search"))
    registry.register(_NativeTool("web_fetch"))
    mcp_registry = MCPToolRegistry(registry)

    registered = mcp_registry.register_server_tools(
        "websearch",
        _websearch_definitions(),
        _ExternalWebsearchClient(),  # type: ignore[arg-type]
    )

    assert registered == 2
    assert registry.has_tool("mcp__websearch__search")
    assert registry.has_tool("mcp__websearch__fetch_page")
