from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.artifact.store import ArtifactStore
from backend.api import _state
from backend.services.tool_registry_factory import build_tool_registry
from backend.config import PermissionSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.catalog import tool_spec_for
from backend.tools.registry import ToolRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOJIBAKE_TOKENS = ("\ufffd", "\u951f", "\u9225", "\u9239", "\u922b", "\u00c3", "\u00c2")
MOJIBAKE_SCAN_GLOBS = (
    "backend/agent/**/*.py",
    "backend/tools/**/*.py",
    "backend/ws/**/*.py",
    "frontend/src.v2/chat/**/*",
    "frontend/src.v2/lib/**/*",
    "frontend/src.v2/protocol/**/*",
    "frontend/src.v2/stores/**/*",
)


def test_runtime_source_paths_do_not_contain_common_mojibake_tokens() -> None:
    offenders: list[str] = []
    for pattern in MOJIBAKE_SCAN_GLOBS:
        for path in PROJECT_ROOT.glob(pattern):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            matches = [token for token in MOJIBAKE_TOKENS if token in text]
            if matches:
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}: {matches}"
                )
    assert offenders == []


def test_default_agent_registry_exposes_mcp_resource_bridge_but_not_proxy_tools(
    monkeypatch,
) -> None:
    from backend.mcp.client import MCPToolDef

    class FakeMCPManager:
        def get_all_tools(self):
            return {
                "docparse": [
                    MCPToolDef(
                        name="parse",
                        description="Parse document",
                        input_schema={
                            "type": "object",
                            "required": ["source"],
                            "properties": {"source": {"type": "string"}},
                        },
                    )
                ]
            }

        def get_client(self, _server_name):
            raise AssertionError("MCP proxy tools should not be registered eagerly")

    monkeypatch.setattr(
        _state,
        "bootstrap",
        SimpleNamespace(
            file_memory=None, mcp_manager=FakeMCPManager(), skill_manager=None
        ),
    )
    registry = build_tool_registry(ArtifactStore())
    names = set(registry.list_tools())
    assert not any(name.startswith("mcp__") for name in names)
    assert {"list_mcp_resources", "read_mcp_resource", "read_terminal"} <= names


def test_agent_message_uses_started_delta_completed_lifecycle() -> None:
    started = AgentEvent.agent_message_started()
    delta = AgentEvent.agent_message_delta("answer")
    completed = AgentEvent.agent_message_completed("answer")

    assert started.to_ws_message() == {
        "type": "item.started",
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "text": "",
            "status": "in_progress",
        },
    }
    assert delta.to_ws_message() == {
        "type": "agent_message.delta",
        "item_id": "agent-message",
        "delta": "answer",
    }
    assert completed.data["item"] == {
        "id": "agent-message",
        "type": "agent_message",
        "text": "answer",
        "source": "model_final",
        "status": "completed",
    }


def test_tool_lifecycle_is_structured_and_does_not_encode_frontend_routing() -> None:
    started = AgentEvent.tool_call(
        id="tool_1",
        name="web_fetch",
        args={"url": "https://example.test/weather"},
        started_at=1234,
        display_hint="Fetching page",
        activity_kind="webSearch",
    )
    completed = AgentEvent.tool_result(
        id="tool_1",
        summary="Fetched page",
        status="success",
        result_kind="web",
        activity_kind="webSearch",
    )

    assert started.type == "tool_call"
    assert started.data["status"] == "running"
    assert started.data["args"] == {"url": "https://example.test/weather"}
    assert completed.type == "tool_result"
    assert completed.data["status"] == "success"
    assert completed.data["result_kind"] == "web"
    assert "display_scope" not in started.data
    assert "display_scope" not in completed.data


class _SearchTool(BaseTool):
    name = "web_search"
    description = "Search web"
    read_only = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )

    async def execute(self, args, context=None):
        self.calls.append(dict(args))
        return ToolResult(content=f"searched: {args['query']}", result_kind="search")


def _execute_search(tmp_path: Path, tool: _SearchTool, arguments: dict[str, object]):
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState(user_message="today Beijing weather")

    async def collect():
        return [
            event
            async for event in execute_tool_batch(
                [ToolCallEvent(id="search-1", name="web_search", arguments=arguments)],
                ctx=ContextBuilder(TokenBudget()),
                state=state,
                tool_registry=registry,
                permission_checker=PermissionChecker(
                    PermissionSettings(auto_allow=["web_search"]),
                    tmp_path,
                ),
                approval_handler=None,
                skill_manager=None,
                permission_context=PermissionContext(),
                tool_ctx=ToolExecutionContext(
                    permission=PermissionContext(),
                    workspace_root=tmp_path,
                ),
            )
        ]

    return registry, asyncio.run(collect())


def test_missing_tool_argument_is_blocked_without_runtime_guessing(tmp_path) -> None:
    tool = _SearchTool()
    registry, events = _execute_search(tmp_path, tool, {})
    call = next(event for event in events if event.type == "tool_call")
    result = next(event for event in events if event.type == "tool_result")

    assert call.data["args"] == {}
    assert result.data["status"] == "blocked"
    assert "missing required argument" in result.data["summary"]
    assert tool.calls == []
    assert tool_spec_for("web_search", registry).required_args == ("query",)


def test_model_can_retry_blocked_tool_with_explicit_arguments(tmp_path) -> None:
    tool = _SearchTool()
    _registry, events = _execute_search(tmp_path, tool, {"query": "Beijing weather"})
    result = next(event for event in events if event.type == "tool_result")

    assert result.data["status"] == "success"
    assert tool.calls == [{"query": "Beijing weather"}]
