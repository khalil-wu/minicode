from __future__ import annotations

import pytest

from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.services.terminal_service import run_terminal_exec_command
from backend.tools.base import BaseTool, ToolResult, ToolSchema


class _RecordingCommandTool(BaseTool):
    name = "run_command"

    def __init__(self) -> None:
        self.executed: list[dict[str, object]] = []

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Run a command",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        )

    def get_schema(self) -> ToolSchema:
        return self.model_schema()

    async def execute(self, args, context=None) -> ToolResult:
        self.executed.append(dict(args))
        return ToolResult(content="Exit code: 0\ncanonical output")


def _context(tmp_path, checker: PermissionChecker) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=checker.build_context(mode="confirm", source="test"),
        session_id="terminal-test",
        conversation_id="conversation-test",
        workspace_root=tmp_path,
        permission_checker=checker,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
    )


@pytest.mark.asyncio
async def test_terminal_exec_uses_canonical_batch_after_exact_approval(tmp_path) -> None:
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    tool = _RecordingCommandTool()

    async def approve(_tool_call_id: str) -> dict[str, str]:
        return {"action": "approve"}

    payload = await run_terminal_exec_command(
        "npm test",
        str(tmp_path),
        tool=tool,
        context=_context(tmp_path, checker),
        conversation_id="conversation-test",
        approval_handler=approve,
    )

    assert tool.executed == [{"command": "npm test", "cwd": str(tmp_path)}]
    assert payload["exit_code"] == 0
    assert "canonical output" in payload["output"]


@pytest.mark.asyncio
async def test_terminal_exec_fails_closed_without_canonical_approval(tmp_path) -> None:
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    tool = _RecordingCommandTool()

    payload = await run_terminal_exec_command(
        "npm test",
        str(tmp_path),
        tool=tool,
        context=_context(tmp_path, checker),
        conversation_id="conversation-test",
    )

    assert tool.executed == []
    assert payload["exit_code"] == -1
    assert "requires approval" in payload["output"]
