from backend.ws.fork_registry import ForkRegistry


def test_fork_registry_uses_stable_ids_and_survives_reload(tmp_path) -> None:
    first = ForkRegistry(session_id="session-fork", root_dir=tmp_path)
    record = first.create(
        parent_conversation_id="conversation-1",
        message_index=4,
        history_length=5,
        estimated_tokens=123,
    )
    assert record.fork_id.startswith("fork_")
    assert "0x" not in record.fork_id

    second = ForkRegistry(session_id="session-fork", root_dir=tmp_path)
    restored = second.get(record.fork_id)
    assert restored is not None
    assert restored.parent_conversation_id == "conversation-1"
    assert restored.message_index == 4
    assert [item.fork_id for item in second.list(parent_conversation_id="conversation-1")] == [record.fork_id]
