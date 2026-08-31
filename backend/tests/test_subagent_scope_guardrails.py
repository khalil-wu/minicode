from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.config import PermissionSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class _FakeTool(BaseTool):
    description = "fake tool"
    permission = PermissionLevel.AUTO

    def __init__(
        self,
        name: str,
        *,
        read_only: bool = False,
        mutates_workspace: bool = False,
    ) -> None:
        self.name = name
        self.read_only = read_only
        self.mutates_workspace = mutates_workspace
        self.calls: list[dict] = []

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object"},
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(content=f"{self.name} ok")


async def _run_tool(
    tool: _FakeTool,
    call: ToolCallEvent,
    *,
    workspace_root: Path,
    metadata: dict,
) -> list:
    runtime = AgentRuntime(
        metrics_file=workspace_root / "agent-metrics.jsonl",
        swarm_store_dir=workspace_root / "swarm",
    )
    runtime.start_run(run_id="parent-run", conversation_id="scope-tests")
    child = runtime.start_subagent(
        subagent_id="subagent-scope-test",
        parent_run_id="parent-run",
        agent_type="implement",
    )
    fenced_metadata = {
        **metadata,
        "agent_mode": "subagent",
        "run_id": child.subagent_id,
        "agent_path": child.agent_path,
        "mailbox_epoch": child.mailbox_epoch,
    }
    registry = ToolRegistry()
    registry.register(tool)
    events = []

    async def _approve(_tool_call_id: str) -> dict[str, str]:
        # These tests exercise the write_scope boundary, not the approval
        # channel. A real subagent bridges approvals to its parent, so supply
        # an approving handler and let the scope guard be the only gate.
        return {"action": "approve"}

    async for event in execute_tool_batch(
        [call],
        ctx=ContextBuilder(TokenBudget()),
        state=AgentState(user_message="subagent test"),
        tool_registry=registry,
        permission_checker=PermissionChecker(
            PermissionSettings(), workspace_root=workspace_root
        ),
        approval_handler=_approve,
        skill_manager=None,
        permission_context=PermissionContext(),
        tool_ctx=ToolExecutionContext(
            permission=PermissionContext(),
            workspace_root=workspace_root,
            metadata=fenced_metadata,
            run_context=RunContext(agent_runtime=runtime),
        ),
    ):
        events.append(event)
    return events


def _result_events(events: list) -> list:
    return [event for event in events if event.type == "tool_result"]


def test_read_only_subagent_blocks_workspace_write(tmp_path) -> None:
    tool = _FakeTool("write_file", mutates_workspace=True)
    events = asyncio.run(
        _run_tool(
            tool,
            ToolCallEvent(
                id="write-1",
                name="write_file",
                arguments={"file_path": "README.md", "content": "hello"},
            ),
            workspace_root=tmp_path,
            metadata={"agent_role": "subagent:explore", "read_only": True},
        )
    )

    results = _result_events(events)
    assert len(results) == 1
    assert results[0].data["status"] == "blocked"
    assert "marked read_only" in results[0].data["summary"]
    assert tool.calls == []


def test_write_scope_blocks_file_write_outside_scope(tmp_path) -> None:
    tool = _FakeTool("write_file", mutates_workspace=True)
    events = asyncio.run(
        _run_tool(
            tool,
            ToolCallEvent(
                id="write-1",
                name="write_file",
                arguments={"file_path": "README.md", "content": "hello"},
            ),
            workspace_root=tmp_path,
            metadata={"agent_role": "subagent:implement", "write_scope": ["src"]},
        )
    )

    results = _result_events(events)
    assert len(results) == 1
    assert results[0].data["status"] == "blocked"
    assert "outside this subagent's write_scope" in results[0].data["summary"]
    assert tool.calls == []


def test_write_scope_allows_file_write_inside_scope(tmp_path) -> None:
    tool = _FakeTool("write_file", mutates_workspace=True)
    events = asyncio.run(
        _run_tool(
            tool,
            ToolCallEvent(
                id="write-1",
                name="write_file",
                arguments={"file_path": "src/app.py", "content": "print('ok')"},
            ),
            workspace_root=tmp_path,
            metadata={"agent_role": "subagent:implement", "write_scope": ["src"]},
        )
    )

    results = _result_events(events)
    assert len(results) == 1
    assert results[0].data["status"] == "success"
    assert results[0].data["summary"] == "write_file ok"
    assert tool.calls[0]["file_path"] == "src/app.py"
    assert tool.calls[0]["content"] == "print('ok')"


def test_write_scope_blocks_apply_patch_outside_scope(tmp_path) -> None:
    tool = _FakeTool("apply_patch", mutates_workspace=True)
    patch = "*** Begin Patch\n*** Add File: README.md\n+hello\n*** End Patch"
    events = asyncio.run(
        _run_tool(
            tool,
            ToolCallEvent(id="patch-1", name="apply_patch", arguments={"patch": patch}),
            workspace_root=tmp_path,
            metadata={"agent_role": "subagent:implement", "write_scope": ["src"]},
        )
    )

    results = _result_events(events)
    assert len(results) == 1
    assert results[0].data["status"] == "blocked"
    assert "outside this subagent's write_scope" in results[0].data["summary"]
    assert tool.calls == []


def test_write_scope_leaves_shell_enforcement_to_the_sandbox(tmp_path) -> None:
    tool = _FakeTool("run_command")
    events = asyncio.run(
        _run_tool(
            tool,
            ToolCallEvent(
                id="command-1",
                name="run_command",
                arguments={
                    "command": "python -c \"open('outside.txt', 'w').write('x')\""
                },
            ),
            workspace_root=tmp_path,
            metadata={"agent_role": "subagent:implement", "write_scope": ["src"]},
        )
    )

    results = _result_events(events)
    assert len(results) == 1
    assert results[0].data["status"] == "success"
    assert len(tool.calls) == 1
