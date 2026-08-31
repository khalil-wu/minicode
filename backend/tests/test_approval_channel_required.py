"""A run with no approval channel must not silently perform approved-only work."""

from __future__ import annotations

import asyncio

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.config import PermissionSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class _WriteTool(BaseTool):
    name = "write_file"
    description = "write a file"
    permission = PermissionLevel.DIFF_REVIEW
    read_only = False
    mutates_workspace = True

    def __init__(self) -> None:
        self.executed = False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path"],
            },
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.executed = True
        return ToolResult(content="wrote it", display_summary="write_file ok")


class _ReadTool(BaseTool):
    name = "read_file"
    description = "read a file"
    read_only = True

    def __init__(self) -> None:
        self.executed = False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.executed = True
        return ToolResult(content="file body", display_summary="read_file ok")


def _run(tool: BaseTool, call: ToolCallEvent, *, mode: str) -> list:
    async def collect() -> list:
        registry = ToolRegistry()
        registry.register(tool)
        checker = PermissionChecker(PermissionSettings())
        permission = checker.build_context(mode=mode)
        events = []
        async for event in execute_tool_batch(
            [call],
            ctx=ContextBuilder(TokenBudget()),
            state=AgentState(user_message="do it"),
            tool_registry=registry,
            permission_checker=checker,
            approval_handler=None,
            skill_manager=None,
            permission_context=permission,
            tool_ctx=ToolExecutionContext(permission=permission),
        ):
            events.append(event)
        return events

    return asyncio.run(collect())


def test_mutating_tool_is_refused_when_no_one_can_approve() -> None:
    tool = _WriteTool()
    events = _run(
        tool,
        ToolCallEvent(
            id="w1", name="write_file", arguments={"file_path": "a.txt", "content": "x"}
        ),
        mode="confirm",
    )

    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].data["status"] == "blocked"
    assert tool.executed is False, "an unapprovable mutation must not run"


def test_bypass_mode_executes_workspace_mutation_without_an_approval_channel() -> None:
    tool = _WriteTool()
    events = _run(
        tool,
        ToolCallEvent(
            id="w2", name="write_file", arguments={"file_path": "a.txt", "content": "x"}
        ),
        mode="bypass",
    )

    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].data["status"] == "success"
    assert tool.executed is True


def test_read_only_tool_keeps_running_without_an_approval_channel() -> None:
    tool = _ReadTool()
    events = _run(
        tool,
        ToolCallEvent(id="r1", name="read_file", arguments={"file_path": "a.txt"}),
        mode="confirm",
    )

    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].data["status"] == "success"
    assert tool.executed is True
