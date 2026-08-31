from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.conversations.repository import ConversationRepository
from backend.memory.pollution import (
    pollution_sources_from_tool_calls,
    pollution_sources_from_transcript,
)
from backend.ws.handlers.conversation import handle_conversation_memory_mode_set


def test_external_context_detection_uses_completed_tool_records() -> None:
    records = [
        {"name": "web_search", "status": "success"},
        {"name": "web_fetch", "status": "failed"},
        {"name": "mcp__github__search_issues", "status": "partial"},
        {"name": "read_file", "status": "success"},
        {"name": "browser_control", "status": "cancelled"},
        {"name": "tool_search", "status": "completed"},
    ]

    assert pollution_sources_from_tool_calls(records) == [
        "web_search",
        "mcp__github__search_issues",
        "tool_search",
    ]
    assert pollution_sources_from_transcript([
        {"role": "assistant", "tool_calls": records},
    ]) == [
        "web_search",
        "mcp__github__search_issues",
        "tool_search",
    ]


def test_repository_pollution_is_durable_and_cleared_by_explicit_mode(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(
        memory_mode="enabled",
        summary="Keep the visible task summary",
        transcript=[{"id": "user-1", "role": "user", "content": "keep"}],
    )

    polluted = repository.mark_memory_polluted(
        conversation.id,
        ["web_search", "mcp__github__search_issues", "web_search"],
    )

    assert polluted is not None
    assert polluted.memory_polluted is True
    assert polluted.memory_pollution_sources == [
        "web_search",
        "mcp__github__search_issues",
    ]
    assert polluted.summary == "Keep the visible task summary"
    assert polluted.memory_mode == "polluted"
    assert polluted.transcript[0]["id"] == "user-1"

    reloaded = ConversationRepository(tmp_path / "conversations").get_conversation(
        conversation.id
    )
    assert reloaded is not None
    assert reloaded.memory_polluted is True
    assert reloaded.memory_mode == "polluted"

    cleared = repository.update_memory_mode(
        conversation.id,
        "enabled",
    )
    assert cleared is not None
    assert cleared.memory_polluted is False
    assert cleared.memory_pollution_sources == []


def test_memory_reenable_checks_active_run_before_mutation(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(
        memory_mode="enabled",
        memory_polluted=True,
        memory_pollution_sources=["web_search"],
    )
    results: list[tuple[str, str, str, dict]] = []

    async def emit_result(command, message, *, level="info", data=None):
        results.append((command, message, level, dict(data or {})))

    session = SimpleNamespace(
        active_conversation_id=conversation.id,
        conversation_repo=repository,
        ws_manager=None,
        running_agent_task_for=lambda conversation_id: SimpleNamespace(done=lambda: False),
        emit_command_result=emit_result,
    )

    asyncio.run(handle_conversation_memory_mode_set(session, {
        "conversation_id": conversation.id,
        "memory_mode": "enabled",
    }))

    restored = repository.get_conversation(conversation.id)
    assert restored is not None
    assert restored.memory_mode == "polluted"
    assert restored.memory_polluted is True
    assert results[-1][0] == "conversation.memory_mode.set"
    assert results[-1][3]["reason"] == "run_active"
