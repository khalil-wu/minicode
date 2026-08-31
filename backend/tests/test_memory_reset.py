from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from backend.conversations.repository import ConversationRepository
from backend.memory.file_memory import FileMemory
from backend.memory.manager import MemoryManager
from backend.ws.handlers import conversation as conversation_handlers


def test_file_memory_reset_replaces_tree_and_restores_default_layout(tmp_path) -> None:
    memory_root = tmp_path / "memory"
    memory = FileMemory(memory_dir=memory_root)
    (memory_root / "custom.md").write_text("# Custom\n\nsecret\n", encoding="utf-8")
    nested = memory_root / "projects" / "project-a"
    nested.mkdir(parents=True)
    (nested / "auto_project.md").write_text("generated", encoding="utf-8")

    result = memory.reset()

    assert result.files_removed == 4
    assert result.directories_removed == 4
    assert result.cleanup_pending is False
    assert not (memory_root / "custom.md").exists()
    assert not (memory_root / "projects").exists()
    assert memory.read_file("MEMORY.md") is not None


def test_repository_memory_reset_preserves_transcript_modes_and_non_memory_notes(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "conversations")
    transcript = [
        {"id": "user-1", "role": "user", "content": "keep this", "timestamp": "2026-08-07T00:00:00Z"},
        {"id": "assistant-1", "role": "assistant", "content": "kept", "timestamp": "2026-08-07T00:00:01Z"},
    ]
    conversation = repository.create_conversation(
        memory_mode="disabled",
        summary="generated summary",
        transcript=transcript,
        context_snapshot={
            "history": [],
            "persistent_notes": [
                {"kind": "profile", "title": "Inherited user profile", "content": "old profile"},
                {"kind": "summary", "title": "Inherited conversation memory", "content": "old summary"},
                {"kind": "compaction_summary", "title": "Compacted", "content": "keep continuity"},
            ],
            "compaction_count": 2,
        },
    )

    result = repository.reset_memory_state()
    restored = repository.get_conversation(conversation.id)

    assert result == {
        "conversations_scanned": 1,
        "conversations_reset": 1,
        "notes_removed": 2,
    }
    assert restored is not None
    assert restored.memory_mode == "disabled"
    assert restored.transcript == transcript
    assert restored.summary == ""
    assert restored.context_snapshot["persistent_notes"] == [
        {"kind": "compaction_summary", "title": "Compacted", "content": "keep continuity"}
    ]
    assert restored.context_snapshot["compaction_count"] == 2


def _memory_reset_session(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(
        memory_mode="enabled",
        summary="old summary",
        transcript=[{"id": "user-1", "role": "user", "content": "keep", "timestamp": "2026-08-07T00:00:00Z"}],
    )
    file_memory = FileMemory(tmp_path / "memory")
    (file_memory.memory_dir / "custom.md").write_text("generated", encoding="utf-8")
    session = SimpleNamespace(
        active_conversation_id=conversation.id,
        ws_manager=None,
        conversation_repo=repository,
        memory_manager=MemoryManager(file_memory),
        emit_command_result=AsyncMock(),
        send_conversation_list=AsyncMock(),
        load_active_conversation_snapshot=Mock(),
    )
    return session, conversation, file_memory


def test_memory_reset_handler_requires_confirmation_and_idle_runtime(tmp_path, monkeypatch) -> None:
    session, conversation, file_memory = _memory_reset_session(tmp_path)

    asyncio.run(conversation_handlers.handle_memory_reset(session, {}))

    session.emit_command_result.assert_awaited_once_with(
        "memory.reset",
        "Explicit confirmation is required before clearing memory.",
        level="error",
        data={"reason": "confirmation_required"},
    )
    assert file_memory.read_file("custom.md") == "generated"

    session.emit_command_result.reset_mock()
    monkeypatch.setattr(
        conversation_handlers,
        "_memory_reset_active_conversation_ids",
        lambda _session: [conversation.id],
    )
    asyncio.run(
        conversation_handlers.handle_memory_reset(
            session,
            {"confirmed": True},
        )
    )

    assert session.emit_command_result.await_args.kwargs["level"] == "warning"
    assert session.emit_command_result.await_args.kwargs["data"]["reason"] == "run_active"
    assert file_memory.read_file("custom.md") == "generated"


def test_memory_reset_handler_clears_memory_and_refreshes_active_context(tmp_path, monkeypatch) -> None:
    session, conversation, file_memory = _memory_reset_session(tmp_path)
    monkeypatch.setattr(
        conversation_handlers,
        "_memory_reset_active_conversation_ids",
        lambda _session: [],
    )

    handled = asyncio.run(
        conversation_handlers.handle_memory_reset(
            session,
            {"confirmed": True},
        )
    )

    assert handled is True
    assert file_memory.read_file("custom.md") is None
    restored = session.conversation_repo.get_conversation(conversation.id)
    assert restored is not None
    assert restored.memory_mode == "enabled"
    assert [message["content"] for message in restored.transcript] == ["keep"]
    assert restored.summary == ""
    session.load_active_conversation_snapshot.assert_called_once_with(
        conversation.id,
        restored.context_snapshot,
    )
    session.send_conversation_list.assert_awaited_once()
    assert session.emit_command_result.await_args.args[0] == "memory.reset"
    assert session.emit_command_result.await_args.kwargs["level"] == "success"
    assert session.emit_command_result.await_args.kwargs["data"]["conversations_reset"] == 1
