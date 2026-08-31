import asyncio
import threading

from backend.agent.context import ContextBuilder
from backend.conversations.repository import ConversationRepository
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.utils import build_summary_from_transcript


def _runtime(repo: ConversationRepository) -> ConversationRuntime:
    return ConversationRuntime(
        conversation_repo=repo,
        context_builder=ContextBuilder(),
        build_summary_from_transcript=build_summary_from_transcript,
    )


def _runtime_with_builder(
    repo: ConversationRepository,
) -> tuple[ConversationRuntime, ContextBuilder]:
    builder = ContextBuilder()
    return ConversationRuntime(
        conversation_repo=repo,
        context_builder=builder,
        build_summary_from_transcript=build_summary_from_transcript,
    ), builder


def test_recall_truncation_updates_snapshot_so_deleted_tail_does_not_rehydrate(tmp_path):
    repo = ConversationRepository(base_dir=tmp_path)
    transcript = [
        {"id": "user-1", "role": "user", "content": "write an html game"},
        {"id": "assistant-1", "role": "assistant", "content": "I'll create it."},
        {"id": "user-2", "role": "user", "content": "why is it stuck?"},
    ]
    snapshot = {
        "history": [
            {"role": "user", "content": "write an html game"},
            {"role": "assistant", "content": "I'll create it."},
            {"role": "user", "content": "why is it stuck?"},
        ],
        "persistent_notes": [],
        "compaction_count": 0,
        "turn_admissions": {
            "user-1": {"history_start": 0, "history_end": 1},
            "user-2": {"history_start": 2, "history_end": 3},
        },
    }
    conversation = repo.create_conversation(
        conversation_id="conv_recalltruncate",
        transcript=transcript,
        context_snapshot=snapshot,
    )

    updated = _runtime(repo).rewind_to_user_turn(
        conversation=conversation,
        retry_from_message_id="user-1",
    )

    assert updated is not None
    assert updated.transcript == []
    assert updated.context_snapshot["history"] == []

    reloaded_repo = ConversationRepository(base_dir=tmp_path)
    reloaded = reloaded_repo.get_conversation("conv_recalltruncate")

    assert reloaded is not None
    assert reloaded.transcript == []
    assert reloaded.context_snapshot["history"] == []


def test_recall_keeps_canonical_history_and_discards_persisted_context_ledger(tmp_path):
    repo = ConversationRepository(base_dir=tmp_path)
    stale_ledger = {
        "estimated_tokens": 999_999,
        "actual_tokens": 999_999,
        "entries": [{"category": "history", "tokens": 999_999}],
    }
    conversation = repo.create_conversation(
        conversation_id="conv-recall-ordinary",
        transcript=[
            {"id": "user-1", "role": "user", "content": "first question"},
            {"id": "assistant-1", "role": "assistant", "content": "first answer"},
            {"id": "user-2", "role": "user", "content": "second question"},
            {"id": "assistant-2", "role": "assistant", "content": "second answer"},
        ],
        context_snapshot={
            "history": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
            ],
            "turn_admissions": {
                "user-1": {"history_start": 0, "history_end": 1},
                "user-2": {"history_start": 2, "history_end": 3},
            },
            "persistent_notes": [{"content": "keep me"}],
            "compaction_count": 2,
            "context_ledger": stale_ledger,
        },
    )
    runtime, builder = _runtime_with_builder(repo)

    updated = runtime.rewind_to_user_turn(
        conversation=conversation,
        retry_from_message_id="user-2",
    )

    assert updated is not None
    assert [message["role"] for message in updated.context_snapshot["history"]] == [
        "user",
        "assistant",
    ]
    assert [message["content"] for message in updated.context_snapshot["history"]] == [
        "first question",
        "first answer",
    ]
    assert updated.context_snapshot["persistent_notes"] == [{"content": "keep me"}]
    assert updated.context_snapshot["compaction_count"] == 2
    assert "context_ledger" not in updated.context_snapshot
    assert builder.context_ledger() != stale_ledger

    reloaded = ConversationRepository(base_dir=tmp_path).get_conversation(
        "conv-recall-ordinary"
    )
    assert reloaded is not None
    assert reloaded.context_snapshot["history"] == updated.context_snapshot["history"]
    assert "context_ledger" not in reloaded.context_snapshot


def test_recall_rebuild_keeps_tool_call_and_result_pairs_among_normal_history(tmp_path):
    repo = ConversationRepository(base_dir=tmp_path)
    conversation = repo.create_conversation(
        conversation_id="conv-recall-tools",
        transcript=[
            {"id": "user-1", "role": "user", "content": "inspect the file"},
            {
                "id": "assistant-tool",
                "role": "assistant",
                "content": "I will inspect it.",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "args": {"path": "app.py"},
                    "outputPreview": "print('ok')",
                }],
            },
            {"id": "assistant-normal", "role": "assistant", "content": "The file is valid."},
            {"id": "user-2", "role": "user", "content": "continue"},
        ],
        context_snapshot={
            "history": [
                {"role": "user", "content": "inspect the file"},
                {
                    "role": "assistant",
                    "content": "I will inspect it.",
                    "tool_calls": [{
                        "id": "call-1",
                        "name": "read_file",
                        "arguments": {"path": "app.py"},
                    }],
                },
                {
                    "role": "tool",
                    "content": "print('ok')",
                    "tool_call_id": "call-1",
                    "name": "read_file",
                },
                {"role": "assistant", "content": "The file is valid."},
                {"role": "user", "content": "continue"},
            ],
            "turn_admissions": {
                "user-1": {"history_start": 0, "history_end": 1},
                "user-2": {"history_start": 4, "history_end": 5},
            },
        },
    )

    updated = _runtime(repo).rewind_to_user_turn(
        conversation=conversation,
        retry_from_message_id="user-2",
    )

    assert updated is not None
    history = updated.context_snapshot["history"]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[1]["tool_calls"] == [{
        "id": "call-1",
        "name": "read_file",
        "arguments": {"path": "app.py"},
    }]
    assert history[2]["tool_call_id"] == "call-1"
    assert history[2]["content"] == "print('ok')"
    assert history[3]["content"] == "The file is valid."


def test_hydration_barrier_waits_for_complete_history_before_provider_turn(tmp_path, monkeypatch):
    repo = ConversationRepository(base_dir=tmp_path)
    history = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(25)
    ]
    conversation = repo.create_conversation(
        conversation_id="conv-hydration-barrier",
        context_snapshot={"history": history},
    )
    runtime, builder = _runtime_with_builder(repo)
    runtime.active_conversation_id = conversation.id
    entered = threading.Event()
    release = threading.Event()
    original = ContextBuilder.deserialize_snapshot_history

    def blocked(raw_history, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original(raw_history, **kwargs)

    monkeypatch.setattr(ContextBuilder, "deserialize_snapshot_history", staticmethod(blocked))

    async def exercise():
        assert runtime.load_active_conversation_snapshot(conversation.id, conversation.context_snapshot)
        wait_task = asyncio.create_task(runtime.wait_for_hydration(conversation.id))
        await asyncio.to_thread(entered.wait, 1)
        assert not wait_task.done()
        assert [message.content for message in builder._history] == [
            f"message-{index}" for index in range(5, 25)
        ]
        release.set()
        await asyncio.wait_for(wait_task, timeout=2)
        assert [message.content for message in builder._history] == [
            f"message-{index}" for index in range(25)
        ]

    asyncio.run(exercise())
