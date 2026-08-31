from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.agent.execution_journal import ExecutionJournal, execution_journal_owner
from backend.permissions.context import ToolExecutionContext
from backend.sdk import _mcp_callable_for_tool, tool
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


def _journal(server_name: str, tool_name: str, root: Path) -> ExecutionJournal:
    return ExecutionJournal(
        execution_journal_owner("sdk_mcp", server_name, tool_name),
        base_dir=root,
    )


def test_sdk_mcp_tool_runs_through_canonical_execution_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal_root = tmp_path / "journals"
    monkeypatch.setattr("backend.agent.execution_journal.JOURNAL_ROOT", journal_root)
    seen_contexts: list[ToolExecutionContext] = []

    @tool(name="sdk_read", read_only=True)
    async def sdk_read(value: str, context: ToolExecutionContext) -> str:
        seen_contexts.append(context)
        return f"value={value}"

    callable_tool = _mcp_callable_for_tool(
        sdk_read,
        server_name="sdk-boundary",
        workspace_root=tmp_path,
    )
    output = asyncio.run(callable_tool(value="ok"))

    assert output.startswith("value=ok")
    assert len(seen_contexts) == 1
    assert seen_contexts[0].workspace_root == tmp_path.resolve()
    assert seen_contexts[0].permission_checker is not None
    assert seen_contexts[0].sandbox_policy is not None

    events = _journal("sdk-boundary", "sdk_read", journal_root).read_events()
    assert [event.event_type for event in events] == ["tool_use", "tool_result"]
    result = events[-1].payload
    assert result["status"] == "success"
    assert len(result["request_digest"]) == 64


def test_sdk_mcp_nonautomatic_tool_cannot_bypass_approval(tmp_path: Path) -> None:
    executed = False

    @tool(name="sdk_confirm", permission=PermissionLevel.CONFIRM, read_only=False)
    async def sdk_confirm() -> str:
        nonlocal executed
        executed = True
        return "executed"

    output = asyncio.run(
        _mcp_callable_for_tool(
            sdk_confirm,
            server_name="sdk-boundary",
            workspace_root=tmp_path,
        )()
    )

    assert "requires MiniCode approval" in output
    assert executed is False




def test_sdk_mcp_auto_external_callable_cannot_bypass_approval(tmp_path: Path) -> None:
    executed = False

    @tool(name="sdk_external_auto", read_only=False)
    async def sdk_external_auto() -> str:
        nonlocal executed
        executed = True
        return "executed"

    output = asyncio.run(
        _mcp_callable_for_tool(
            sdk_external_auto,
            server_name="sdk-boundary",
            workspace_root=tmp_path,
        )()
    )

    assert "requires approval" in output
    assert executed is False


def test_sdk_mcp_timeout_persists_cleanup_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal_root = tmp_path / "journals"
    monkeypatch.setattr("backend.agent.execution_journal.JOURNAL_ROOT", journal_root)
    monkeypatch.setattr(
        "backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS",
        0.001,
    )

    class ResistantTool(BaseTool):
        name = "sdk_resistant"
        permission = PermissionLevel.AUTO
        read_only = True
        timeout_seconds = 0.001

        def __init__(self) -> None:
            self.release: asyncio.Event | None = None
            self.finished: asyncio.Event | None = None
            self.context: ToolExecutionContext | None = None

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Resist cancellation until released",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(
            self,
            _args: dict[str, Any],
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            self.release = asyncio.Event()
            self.finished = asyncio.Event()
            self.context = context
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content="released")
            finally:
                self.finished.set()

    resistant = ResistantTool()

    async def scenario() -> tuple[str, int, int]:
        output = await _mcp_callable_for_tool(
            resistant,
            server_name="sdk-timeout",
            workspace_root=tmp_path,
        )()
        assert resistant.context is not None
        owned_at_return = len(resistant.context.pending_cleanup_tasks)
        assert resistant.release is not None
        assert resistant.finished is not None
        resistant.release.set()
        await asyncio.wait_for(resistant.finished.wait(), timeout=1.0)
        for _ in range(100):
            if not resistant.context.pending_cleanup_tasks:
                break
            await asyncio.sleep(0)
        return output, owned_at_return, len(resistant.context.pending_cleanup_tasks)

    output, owned_at_return, owned_after = asyncio.run(scenario())

    assert "timed out" in output
    assert "cleanup: pending/manual recovery required" in output
    assert owned_at_return == 1
    assert owned_after == 0
    events = _journal("sdk-timeout", "sdk_resistant", journal_root).read_events()
    receipt = events[-1].payload["cleanup_receipt"]
    assert receipt["pending"] == 1
    assert receipt["manual_recovery_required"] is True
