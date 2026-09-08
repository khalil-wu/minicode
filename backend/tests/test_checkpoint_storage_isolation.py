from __future__ import annotations

from pathlib import Path

import pytest

from backend.agent.checkpoint import (
    clear_checkpoints,
    get_checkpoint_dir,
    load_latest_checkpoint,
    save_checkpoint,
)
from backend.agent.execution_journal import ExecutionJournal, delete_agent_journal


def _save(base_dir: Path, *, conversation_id: str, session_id: str = "session-1") -> Path:
    return save_checkpoint(
        session_id=session_id,
        user_message=f"message for {conversation_id}",
        iterations=1,
        reply="partial",
        messages=[],
        tool_calls=[],
        active_skills=[],
        disabled_tools=set(),
        loaded_deferred_tools={"preview_server"},
        stopped_reason="interrupted",
        last_mutation_index=0,
        base_dir=base_dir,
        run_id=f"run-{conversation_id}",
        conversation_id=conversation_id,
    )


def test_checkpoint_load_and_clear_are_conversation_scoped(tmp_path: Path) -> None:
    _save(tmp_path, conversation_id="conversation-a")
    _save(tmp_path, conversation_id="conversation-b")

    checkpoint_a = load_latest_checkpoint(
        "session-1",
        base_dir=tmp_path,
        conversation_id="conversation-a",
    )
    checkpoint_b = load_latest_checkpoint(
        "session-1",
        base_dir=tmp_path,
        conversation_id="conversation-b",
    )
    assert checkpoint_a is not None and checkpoint_a.conversation_id == "conversation-a"
    assert checkpoint_b is not None and checkpoint_b.conversation_id == "conversation-b"
    assert checkpoint_a.loaded_deferred_tools == ["preview_server"]
    assert checkpoint_b.loaded_deferred_tools == ["preview_server"]

    clear_checkpoints(
        "session-1",
        base_dir=tmp_path,
        conversation_id="conversation-a",
    )

    assert load_latest_checkpoint(
        "session-1",
        base_dir=tmp_path,
        conversation_id="conversation-a",
    ) is None
    assert load_latest_checkpoint(
        "session-1",
        base_dir=tmp_path,
        conversation_id="conversation-b",
    ) is not None


@pytest.mark.parametrize("unsafe_id", [
    "../escape", "..\\escape", "/absolute", "C:\\absolute", "..",
    "alice@../escape", "alice@..\\escape", "CON", "nul.txt",
])
def test_checkpoint_and_journal_reject_path_like_ids(tmp_path: Path, unsafe_id: str) -> None:
    with pytest.raises(ValueError):
        load_latest_checkpoint(unsafe_id, base_dir=tmp_path)
    with pytest.raises(ValueError):
        ExecutionJournal(unsafe_id, base_dir=tmp_path / "journals")

    assert not (tmp_path.parent / "escape").exists()


def test_named_teammate_checkpoint_and_journal_round_trip(tmp_path: Path) -> None:
    agent_id = "alice@audit"
    checkpoint_path = _save(tmp_path, conversation_id="conversation", session_id=agent_id)
    journal = ExecutionJournal(agent_id, base_dir=tmp_path / "journals")
    journal.append("user_prompt", {"content": "Audit the named teammate."})

    checkpoint = load_latest_checkpoint(agent_id, base_dir=tmp_path, conversation_id="conversation")
    assert checkpoint is not None
    assert checkpoint.session_id == agent_id
    assert checkpoint.reply == "partial"
    assert checkpoint_path.parent.name == agent_id
    restored = ExecutionJournal(agent_id, base_dir=tmp_path / "journals")
    assert restored.read_events()[0].agent_id == agent_id
    assert restored.path.parent.name == agent_id

    clear_checkpoints(agent_id, base_dir=tmp_path, conversation_id="conversation")
    assert not checkpoint_path.exists()
    assert delete_agent_journal(agent_id, base_dir=tmp_path / "journals")
    assert not journal.path.parent.exists()


def test_default_checkpoint_directory_honors_runtime_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path))

    checkpoint_dir = get_checkpoint_dir("desktop-session")

    assert checkpoint_dir == tmp_path / "data" / "agent-runtime" / "checkpoints" / "desktop-session"
