from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.manager import WebSocketManager


@pytest.mark.parametrize("blocked_stage", ["summary", "delete"])
def test_delete_disk_io_keeps_event_loop_and_new_conversation_owner_available(monkeypatch, tmp_path, blocked_stage):
    async def run() -> None:
        repository = ConversationRepository(tmp_path / "conversations")
        target = repository.create_conversation(title="Delete this chat")
        other = repository.create_conversation(title="Keep the workspace draft",
            context_snapshot={"scratch": "Unchanged other conversation"})
        manager = WebSocketManager()
        blocked = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        disk_method = repository.get_conversation_summary if blocked_stage == "summary" else repository.delete_conversation

        def blocked_disk(conversation_id):
            assert threading.get_ident() != loop_thread
            loop.call_soon_threadsafe(blocked.set)
            if not release.wait(timeout=2):
                raise AssertionError("The event loop failed to release its blocked disk worker")
            return disk_method(conversation_id)

        monkeypatch.setattr(repository, "get_conversation_summary" if blocked_stage == "summary" else "delete_conversation", blocked_disk)
        session = SimpleNamespace(
            session_id="delete-io-owner", active_conversation_id=target.id, is_connected=True,
            conversation_repo=repository, ws_manager=manager, cleanup_tasks=set(),
            background_manager=SimpleNamespace(destroy_for_conversation=AsyncMock()),
            terminal_manager=SimpleNamespace(destroy_sessions_for_conversation=AsyncMock()),
            emit_command_result=AsyncMock(), send_conversation_list=AsyncMock(),
        )
        activate = AsyncMock()
        monkeypatch.setattr(conversation_handlers, "_stop_conversation_run", AsyncMock(return_value=True))
        monkeypatch.setattr(conversation_handlers, "_purge_conversation_replay_state", AsyncMock(return_value=({}, [])))
        monkeypatch.setattr(conversation_handlers, "_purge_conversation_runtime_state", AsyncMock(return_value=({}, [])))
        monkeypatch.setattr(conversation_handlers, "_schedule_long_term_memory_forgetting", lambda *args: None)
        monkeypatch.setattr(conversation_handlers, "_activate_conversation_or_blank", activate)
        monkeypatch.setattr("backend.tasks.scheduler.get_global_scheduler", lambda: SimpleNamespace(destroy_for_conversation=AsyncMock()))
        monkeypatch.setattr("backend.preview.launcher.stop_preview_launches_for_conversation", AsyncMock())

        await conversation_handlers.handle_conversation_delete(session, {"conversation_id": target.id})
        tasks = tuple(manager._conversation_delete_tasks)
        try:
            await asyncio.wait_for(blocked.wait(), timeout=1)
            assert manager.conversation_delete_fence(target.id)
            # The native disk operation stays blocked while the renderer's
            # owner changes. Delete completion must preserve that choice.
            session.active_conversation_id = other.id
            await asyncio.sleep(0)
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert session.active_conversation_id == other.id
        activate.assert_not_awaited()
        assert session.emit_command_result.await_args.kwargs["level"] == "success"
        assert manager.conversation_delete_fence(target.id) is None
        assert repository.get_conversation(target.id) is None
        assert repository.get_conversation(other.id).context_snapshot["scratch"] == "Unchanged other conversation"

    asyncio.run(run())
