from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.monitor_tool import MonitorTool


class _FakeBackgroundManager:
    def __init__(self) -> None:
        self.command = SimpleNamespace(
            command_id="bg_123",
            command="npm run dev",
            cwd="C:/repo",
            status="running",
            output="server ready\n",
            exit_code=None,
            started_at=123.0,
            completed_at=None,
            conversation_id="conv-monitor",
        )

    def list_commands(self, include_completed: bool = False, *, conversation_id: str):
        assert conversation_id == self.command.conversation_id
        return [{
            "command_id": self.command.command_id,
            "command": self.command.command,
            "cwd": self.command.cwd,
            "status": self.command.status,
            "exit_code": self.command.exit_code,
            "started_at": self.command.started_at,
            "completed_at": self.command.completed_at,
            "output_length": len(self.command.output),
        }]

    def get_status(self, command_id: str, *, conversation_id: str):
        return (
            self.command
            if command_id == self.command.command_id and conversation_id == self.command.conversation_id
            else None
        )

    def get_output_snapshot(self, command_id: str, *, conversation_id: str, max_chars: int):
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return None
        truncated = len(command.output) > max_chars
        output = command.output[-max_chars:] if truncated else command.output
        return output, truncated, ""

    async def cancel(self, command_id: str, *, conversation_id: str) -> bool:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None or command.status != "running":
            return False
        command.status = "cancelled"
        command.exit_code = -1
        command.completed_at = 456.0
        return True

    async def write_stdin(
        self,
        command_id: str,
        chars: str,
        *,
        conversation_id: str,
        close_stdin: bool = False,
    ) -> int:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        if command.status != "running":
            raise RuntimeError(f"Background command {command_id} is {command.status}; stdin is closed.")
        command.stdin_chars = getattr(command, "stdin_chars", "") + chars
        command.stdin_closed = close_stdin
        return len(chars.encode("utf-8"))


def test_monitor_lists_background_commands() -> None:
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=_FakeBackgroundManager(),
        conversation_id="conv-monitor",
    )

    result = asyncio.run(tool.execute({}, context=context))

    assert not result.is_error
    assert "bg_123" in result.content
    assert "npm run dev" in result.content


def test_monitor_reads_background_command_output() -> None:
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=_FakeBackgroundManager(),
        conversation_id="conv-monitor",
    )

    result = asyncio.run(tool.execute({"command_id": "bg_123"}, context=context))

    assert not result.is_error
    assert "Background command bg_123 (running)" in result.content
    assert "server ready" in result.content


def test_monitor_does_not_read_another_conversation_background_command() -> None:
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=_FakeBackgroundManager(),
        conversation_id="conv-other",
    )

    result = asyncio.run(tool.execute({"command_id": "bg_123"}, context=context))

    assert result.is_error
    assert "not found" in result.content.lower()


def test_monitor_cancels_exact_owned_background_command() -> None:
    manager = _FakeBackgroundManager()
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=manager,
        conversation_id="conv-monitor",
    )

    result = asyncio.run(tool.execute(
        {"action": "cancel", "command_id": "bg_123"},
        context=context,
    ))

    assert not result.is_error
    assert manager.command.status == "cancelled"
    assert "Cancelled owned background command bg_123" in result.content
    assert tool.is_read_only({"action": "status"}) is True
    assert tool.is_read_only({"action": "cancel"}) is False


def test_monitor_writes_exact_input_to_owned_background_command() -> None:
    manager = _FakeBackgroundManager()
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=manager,
        conversation_id="conv-monitor",
    )

    result = asyncio.run(tool.execute(
        {
            "action": "write_stdin",
            "command_id": "bg_123",
            "chars": "yes\n",
            "close_stdin": True,
        },
        context=context,
    ))

    assert not result.is_error
    assert manager.command.stdin_chars == "yes\n"
    assert manager.command.stdin_closed is True
    assert "Wrote 4 UTF-8 bytes" in result.content
    assert "closed stdin" in result.content
    assert tool.is_read_only({"action": "write_stdin"}) is False
    assert tool.is_idempotent({"action": "write_stdin"}) is False


def test_monitor_refuses_cross_conversation_stdin_write() -> None:
    manager = _FakeBackgroundManager()
    result = asyncio.run(
        MonitorTool().execute(
            {
                "action": "write_stdin",
                "command_id": "bg_123",
                "chars": "secret\n",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                background_manager=manager,
                conversation_id="conv-other",
            ),
        )
    )

    assert result.is_error
    assert "not found" in result.content.lower()
    assert not hasattr(manager.command, "stdin_chars")


def test_monitor_refuses_cross_conversation_cancel() -> None:
    manager = _FakeBackgroundManager()
    tool = MonitorTool()
    context = ToolExecutionContext(
        permission=PermissionContext(),
        background_manager=manager,
        conversation_id="conv-other",
    )

    result = asyncio.run(tool.execute(
        {"action": "cancel", "command_id": "bg_123"},
        context=context,
    ))

    assert result.is_error
    assert "not found" in result.content.lower()
    assert manager.command.status == "running"


def test_monitor_reports_pending_cleanup_without_claiming_cancellation() -> None:
    manager = _FakeBackgroundManager()

    async def request_only(command_id: str, *, conversation_id: str) -> bool:
        command = manager.get_status(command_id, conversation_id=conversation_id)
        assert command is not None
        command.cleanup_pending = True
        return True

    manager.cancel = request_only  # type: ignore[method-assign]
    manager.command.cleanup_pending = False
    result = asyncio.run(
        MonitorTool().execute(
            {"action": "cancel", "command_id": "bg_123"},
            context=ToolExecutionContext(
                permission=PermissionContext(),
                background_manager=manager,
                conversation_id="conv-monitor",
            ),
        )
    )

    assert result.status == "pending"
    assert manager.command.status == "running"
    assert "process cleanup is still pending" in result.content
    assert "Cancelled owned background command" not in result.content
