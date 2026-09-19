from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.terminal.manager import BackgroundCommandManager
from backend.terminal.task_output import read_task_output_chunk
from backend.tools.command_tool import RunCommandTool
from backend.tools.monitor_tool import MonitorTool


def python_command(code):
    if sys.platform == "win32":
        return "& '" + sys.executable.replace("'", "''") + "' -u -c '" + code.replace("'", "''") + "'"
    return shlex.quote(sys.executable) + " -u -c " + shlex.quote(code)


def setup_tools(tmp_path, manager, cancel_event=None, stream_callback=None):
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        conversation_id="conv_commands", session_id="command-tests", background_manager=manager,
        cancel_event=cancel_event, stream_callback=stream_callback)
    tool = RunCommandTool(ArtifactStore(storage_dir=str(tmp_path / "artifacts")), manager)
    return tool, context


def test_yield_returns_same_process_then_monitor_waits_and_writes_stdin(tmp_path):
    async def scenario():
        started = []
        async def on_started(command): started.append(command.command_id)
        manager = BackgroundCommandManager(session_id="command-tests", on_started=on_started)
        tool, context = setup_tools(tmp_path, manager)
        try:
            result = await tool.execute({"command": python_command("import sys; print('READY', flush=True); value=sys.stdin.readline(); print('ECHO:'+value.strip(), flush=True)"), "yield_time_ms": 0}, context)
            assert not result.is_error
            command_id = result.runtime_metadata["command_id"]
            command = manager.get_status(command_id, conversation_id=context.conversation_id)
            assert command.timeout_ms == 0
            assert command.status == "running"
            monitor = MonitorTool()
            ready = await monitor.execute({"command_id":command_id,"cursor":0,"yield_time_ms":10000}, context)
            assert "READY" in ready.content
            cursor = ready.runtime_metadata["next_cursor"]
            pid = command.pid
            echoed = await monitor.execute({"action":"write_stdin","command_id":command_id,"chars":"hello\n","cursor":cursor,"yield_time_ms":10000}, context)
            assert not echoed.is_error
            assert "ECHO:hello" in echoed.content
            raw, _, _, _ = manager.get_output_chunk(command_id, conversation_id=context.conversation_id, cursor=cursor, max_chars=1000)
            assert raw.splitlines() == ["ECHO:hello"]
            await manager.wait(command_id, conversation_id=context.conversation_id, wait_ms=10000)
            assert command.pid == pid
            assert command.status == "completed"
            assert started == [command_id]
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_short_managed_command_finishes_without_background_notifications(tmp_path):
    async def scenario():
        notifications = []
        async def notify(command): notifications.append(command.command_id)
        manager = BackgroundCommandManager(session_id="command-tests", on_started=notify, on_completed=notify)
        tool, context = setup_tools(tmp_path, manager)
        try:
            result = await tool.execute({"command":python_command("print('short result')"),"yield_time_ms":10000},context)
            assert not result.is_error
            assert "short result" in result.content
            assert result.runtime_metadata["exit_code"] == 0
            assert manager.get_status(result.runtime_metadata["command_id"], conversation_id=context.conversation_id).stream_callback is None
            assert notifications == []
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_cancel_during_initial_wait_stops_the_owned_command(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="command-tests")
        ready, cancel = asyncio.Event(), asyncio.Event()
        def stream(text):
            if "READY" in text: ready.set()
        tool, context = setup_tools(tmp_path, manager, cancel, stream)
        task = asyncio.create_task(tool.execute({"command":python_command("import time; print('READY', flush=True); time.sleep(30)"),"yield_time_ms":60000},context))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            commands = manager.list_commands(include_completed=True, conversation_id=context.conversation_id)
            assert len(commands) == 1
            assert commands[0]["status"] == "cancelled"
            assert not commands[0]["cleanup_pending"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(scenario())


def test_output_cursors_preserve_utf8_and_never_repeat_consumed_text(tmp_path):
    path = tmp_path / "utf8.output"
    original = "你好🌍\nnext line"
    path.write_bytes(original.encode("utf-8"))
    cursor = 0
    chunks = []
    while True:
        text, next_cursor, more = read_task_output_chunk(path, cursor, 1)
        assert next_cursor > cursor
        chunks.append(text)
        cursor = next_cursor
        if not more: break
    assert "".join(chunks) == original
    assert cursor == len(original.encode("utf-8"))
    assert read_task_output_chunk(path,cursor,100) == ("",cursor,False)
    with pytest.raises(ValueError): read_task_output_chunk(path,cursor+1,100)


def test_command_owner_is_checked_before_waiting(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="command-tests")
        tool, context = setup_tools(tmp_path, manager)
        try:
            result = await tool.execute({"command":python_command("import time; time.sleep(30)"),"yield_time_ms":0},context)
            other = ToolExecutionContext(permission=PermissionContext(mode="bypass"), background_manager=manager, conversation_id="conv_other")
            inspected = await MonitorTool().execute({"command_id":result.runtime_metadata["command_id"],"yield_time_ms":1000,"cursor":0},other)
            assert inspected.is_error
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_explicit_process_timeout_is_independent_of_yield_wait(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="command-tests")
        tool, context = setup_tools(tmp_path, manager)
        assert tool.resolve_timeout({"timeout": 1, "yield_time_ms": 10000}) is None
        try:
            result = await tool.execute({"command":python_command("import time; time.sleep(30)"),"yield_time_ms":0,"timeout":1},context)
            command_id = result.runtime_metadata["command_id"]
            command = await manager.wait(command_id,conversation_id=context.conversation_id,wait_ms=10000)
            assert command.status == "failed"
            assert command.timeout_ms == 1000
            assert command.lifecycle.error["kind"] == "timeout"
            assert not command.cleanup_pending
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_cancelled_monitor_wait_does_not_cancel_the_background_process(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="command-tests")
        tool, context = setup_tools(tmp_path, manager)
        try:
            launched = await tool.execute({"command":python_command("import time; time.sleep(30)"),"yield_time_ms":0},context)
            command_id = launched.runtime_metadata["command_id"]
            waiting = asyncio.create_task(MonitorTool().execute({"command_id":command_id,"yield_time_ms":60000},context))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError): await waiting
            assert manager.get_status(command_id,conversation_id=context.conversation_id).status == "running"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_session_scoped_command_failure_keeps_full_output_for_replay(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="headless-tests")
        tool, context = setup_tools(tmp_path, manager)
        context.conversation_id = ""
        try:
            launched = await tool.execute({"command": python_command(
                "import sys; print('FAILURE_AT_START'); print('x' * 1500); print('FAILURE_AT_END'); sys.exit(7)"
            ), "yield_time_ms": 0}, context)
            command_id = launched.runtime_metadata["command_id"]
            await manager.wait(command_id, conversation_id=context.session_id, wait_ms=10000)
            monitor = MonitorTool()
            first = await monitor.execute({"command_id": command_id, "cursor": 0, "max_chars": 128}, context)
            assert first.is_error and first.status == "failed"
            assert first.runtime_metadata["exit_code"] == 7
            assert "FAILURE_AT_START" in first.content
            assert first.runtime_metadata["more_output"]
            replay = await monitor.execute({"command_id": command_id, "cursor": 0, "max_chars": 5000}, context)
            assert "FAILURE_AT_START" in replay.content and "FAILURE_AT_END" in replay.content
            assert not replay.runtime_metadata["more_output"]
            assert len(manager.list_commands(include_completed=True, conversation_id=context.session_id)) == 1
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("max_chars", [1, 2, 127, 128])
def test_head_tail_preview_preserves_utf8_and_replay_cursor(tmp_path, max_chars):
    from backend.terminal.task_output import read_task_output_preview
    path = tmp_path / "output.log"
    text = "BEGIN\n" + "中文🙂中间\n" * 1000 + "FAILURE_AT_END\n"
    path.write_bytes(text.encode("utf-8"))
    preview, cursor, more, end = read_task_output_preview(path, max_chars)
    assert more and end == len(text.encode("utf-8"))
    head_chars = max_chars // 2
    assert preview.startswith(text[:head_chars])
    assert preview.endswith(text[-(max_chars - head_chars):])
    assert cursor == len(text[:head_chars].encode("utf-8"))
    remaining, final_cursor, more = read_task_output_chunk(path, cursor, len(text))
    assert text[:head_chars] + remaining == text
    assert final_cursor == end and not more
    short = tmp_path / "short.log"
    short.write_text("好", encoding="utf-8")
    assert read_task_output_preview(short, max_chars) == ("好", 3, False, 3)


@pytest.mark.parametrize("managed", [True, False])
def test_command_output_budget_keeps_failure_summary_and_original_output(tmp_path, managed):
    async def scenario():
        manager = BackgroundCommandManager(session_id="output-budget") if managed else None
        tool, context = setup_tools(tmp_path, manager)
        code = "import sys; print('BEGIN_DIAGNOSTIC'); print('x' * 8000); print('FINAL_FAILURE'); sys.exit(7)"
        try:
            result = await tool.execute({"command": python_command(code), "max_chars": 128}, context)
            view = result.content if managed else result.artifact_preview
            assert result.is_error
            assert "BEGIN_DIAGNOSTIC" in view and "FINAL_FAILURE" in view
            assert "bytes omitted" in view
            if managed:
                assert result.runtime_metadata["exit_code"] == 7
                command_id = result.runtime_metadata["command_id"]
                full, cursor, more, _ = manager.get_output_chunk(command_id, conversation_id=context.conversation_id, cursor=0, max_chars=10000)
                assert "x" * 8000 in full and not more
                assert cursor == result.runtime_metadata["end_cursor"]
                assert result.runtime_metadata["next_cursor"] < cursor
            else:
                assert result.artifact_id
        finally:
            if manager is not None:
                await manager.shutdown()
    asyncio.run(scenario())


def test_preview_end_cursor_follows_new_output_without_replaying_old_output(tmp_path):
    async def scenario():
        manager = BackgroundCommandManager(session_id="preview-live")
        tool, context = setup_tools(tmp_path, manager)
        try:
            launched = await tool.execute({"command": python_command(
                "print('BEGIN'); print('x' * 8000); print('OLD_END', flush=True); input(); print('NEW_OUTPUT', flush=True)"
            ), "yield_time_ms": 0, "max_chars": 100}, context)
            command_id = launched.runtime_metadata["command_id"]
            await manager.wait(command_id, conversation_id=context.conversation_id, cursor=0, wait_ms=10000)
            monitor = MonitorTool()
            preview = await monitor.execute({"command_id": command_id, "output_mode": "head_tail", "max_chars": 100}, context)
            assert "BEGIN" in preview.content and "OLD_END" in preview.content
            delta = await monitor.execute({"action": "write_stdin", "command_id": command_id,
                "chars": "continue\n", "cursor": preview.runtime_metadata["end_cursor"], "yield_time_ms": 10000}, context)
            assert "\nNEW_OUTPUT" in delta.content and "\nOLD_END" not in delta.content
        finally:
            await manager.shutdown()
    asyncio.run(scenario())
