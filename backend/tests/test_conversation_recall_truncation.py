import asyncio
import threading

import pytest

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
        assert runtime.load_active_conversation_snapshot(conversation.id, conversation.context_snapshot, snapshot_revision=conversation.revision)
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


@pytest.mark.parametrize("change", ["metadata", "snapshot"])
def test_hydration_reloads_only_changed_context_and_keeps_full_work_off_loop(tmp_path, monkeypatch, change):
    repo = ConversationRepository(tmp_path)
    history = [{"role": "user", "content": f"message-{index}"} for index in range(60)]
    conversation = repo.create_conversation(context_snapshot={"history": history})
    runtime, builder = _runtime_with_builder(repo)
    runtime.active_conversation_id = conversation.id
    loop_thread = threading.get_ident()
    full_reads, decoded = [], []
    entered, release = threading.Event(), threading.Event()
    read = repo.get_conversation
    deserialize = ContextBuilder.deserialize_snapshot_history

    def read_full(owner):
        assert threading.get_ident() != loop_thread, "full record copied on the event loop"
        full_reads.append(owner)
        return read(owner)

    def decode(raw_history, **options):
        decoded.append((len(raw_history), threading.get_ident()))
        if threading.get_ident() != loop_thread and not entered.is_set():
            entered.set()
            assert release.wait(5), "prefix decode was never released"
        return deserialize(raw_history, **options)

    def full_load_is_not_a_reload(*_args, **_kwargs):
        raise AssertionError("changed context must use the asynchronous partial hydration path")

    monkeypatch.setattr(repo, "get_conversation", read_full)
    monkeypatch.setattr(ContextBuilder, "deserialize_snapshot_history", staticmethod(decode))
    monkeypatch.setattr(builder, "load_snapshot", full_load_is_not_a_reload)

    async def exercise():
        completed = []

        async def complete(owner):
            completed.append(owner)

        runtime.defer_repository_hydration(conversation.id, on_hydration_complete=complete)
        runtime.start_hydration(conversation.id)
        waiter = asyncio.create_task(runtime.wait_for_hydration(conversation.id))
        expected = list(history)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not waiter.done()
            if change == "metadata":
                changed = await asyncio.to_thread(repo.rename_conversation, conversation.id, "Renamed during resume")
            else:
                expected.append({"role": "user", "content": "new persisted requirement"})
                changed = await asyncio.to_thread(repo.save_context_snapshot, conversation.id, {"history": expected})
                # This field belongs to public transcript/memory, not provider context.
                assert changed.content_revision == conversation.content_revision
            assert changed.revision > conversation.revision
        finally:
            release.set()
            await asyncio.wait_for(waiter, 5)
        assert [message.content for message in builder._history] == [item["content"] for item in expected]
        assert completed == [conversation.id]
        assert full_reads == [conversation.id] * (1 if change == "metadata" else 2)
        assert all(thread != loop_thread for count, thread in decoded if count > 20)
        assert [count for count, thread in decoded if thread == loop_thread] == ([20] if change == "metadata" else [20, 20])
        await runtime.shutdown()

    asyncio.run(exercise())


@pytest.mark.parametrize("history_count", [3, 25])
@pytest.mark.parametrize("entrypoint", ["repository", "detached"])
def test_snapshot_and_its_source_revision_stay_paired_until_publication(tmp_path, monkeypatch, history_count, entrypoint):
    repo = ConversationRepository(tmp_path)
    history = [{"role": "user", "content": f"before-{index}"} for index in range(history_count)]
    conversation = repo.create_conversation(context_snapshot={"history": history})
    runtime, builder = _runtime_with_builder(repo)
    runtime.active_conversation_id = conversation.id
    read_ready, release = threading.Event(), threading.Event()
    read = repo.get_conversation_for_resume
    reads = []

    def hold_detached_record(owner):
        record = read(owner)
        reads.append(record.revision)
        if len(reads) == 1:
            read_ready.set()
            assert release.wait(5), "detached snapshot read was never released"
        return record

    if entrypoint == "repository":
        monkeypatch.setattr(repo, "get_conversation_for_resume", hold_detached_record)

    async def exercise():
        completed = []

        async def complete(owner):
            completed.append(owner)

        waiter = None
        expected = [*history, {"role": "user", "content": "after source read"}]
        try:
            if entrypoint == "repository":
                runtime.defer_repository_hydration(conversation.id, on_hydration_complete=complete)
                runtime.start_hydration(conversation.id)
                waiter = asyncio.create_task(runtime.wait_for_hydration(conversation.id))
                assert await asyncio.to_thread(read_ready.wait, 5)
            changed = await asyncio.to_thread(repo.save_context_snapshot, conversation.id, {"history": expected})
            assert changed.revision > conversation.revision
            assert changed.content_revision == conversation.content_revision
            if entrypoint == "detached":
                assert runtime.load_active_conversation_snapshot(
                    conversation.id, conversation.context_snapshot, snapshot_revision=conversation.revision,
                    notify=True, on_hydration_complete=complete,
                )
                waiter = asyncio.create_task(runtime.wait_for_hydration(conversation.id))
        finally:
            release.set()
            if waiter is not None:
                await asyncio.wait_for(waiter, 5)
        assert [message.content for message in builder._history] == [item["content"] for item in expected]
        assert completed == [conversation.id]
        if entrypoint == "repository":
            assert reads == [conversation.revision, changed.revision]
        await runtime.shutdown()

    asyncio.run(exercise())


def test_resume_plan_recovery_and_concurrent_history_write_share_the_storage_mutation(tmp_path, monkeypatch):
    from backend.agent import plans

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    monkeypatch.setattr(plans, "get_plans_directory", lambda *_args, **_kwargs: plans_dir)
    repo = ConversationRepository(tmp_path / "conversations")
    before = {"plan_slug": "focused-resume-test", "history": [{"role": "user", "content": "original"}],
              "plan_file_reference": {"path": str(plans_dir / "focused-resume-test.md"), "plan_content": "Actual recovered plan"}}
    conversation = repo.create_conversation(context_snapshot=before)
    entered, release, writing, written = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    recover = plans.ensure_plan_file_for_resume

    def hold_recovery(*args):
        entered.set()
        assert release.wait(5), "plan recovery was never released"
        return recover(*args)

    monkeypatch.setattr(plans, "ensure_plan_file_for_resume", hold_recovery)
    after = {**before, "history": [{"role": "user", "content": "new concurrent requirement"}]}

    def write_history():
        writing.set()
        updated = repo.save_context_snapshot(conversation.id, after)
        written.set()
        return updated

    async def exercise():
        restoring = asyncio.create_task(asyncio.to_thread(repo.get_conversation_for_resume, conversation.id))
        writer = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            writer = asyncio.create_task(asyncio.to_thread(write_history))
            assert await asyncio.to_thread(writing.wait, 5)
            assert not await asyncio.to_thread(written.wait, 0.1)
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(restoring, *([writer] if writer is not None else [])), 5)
        restored, updated = results
        assert restored.context_snapshot["plan_recovery_status"] == "recovered"
        assert updated.revision > restored.revision
        assert repo.get_conversation(conversation.id).context_snapshot["history"] == after["history"]
        assert (plans_dir / "focused-resume-test.md").read_text(encoding="utf-8") == "Actual recovered plan"

    asyncio.run(exercise())


@pytest.mark.parametrize("warm_summary_index", [False, True])
def test_repository_hydration_uses_the_readable_generation_revision_after_corruption(tmp_path, warm_summary_index):
    repo = ConversationRepository(tmp_path)
    conversation = repo.create_conversation(context_snapshot={"history": [{"role": "user", "content": "readable history"}]})
    changed = repo.save_context_snapshot(conversation.id, {"history": [{"role": "user", "content": "damaged generation"}]})
    (tmp_path / f"{conversation.id}.g{changed.revision}.snapshot.json").write_text("{broken", encoding="utf-8")
    cold = ConversationRepository(tmp_path)
    if warm_summary_index:
        cold.list_conversations()
    runtime, builder = _runtime_with_builder(cold)
    runtime.active_conversation_id = conversation.id

    async def exercise():
        completed = []

        async def complete(owner):
            completed.append(owner)

        runtime.defer_repository_hydration(conversation.id, on_hydration_complete=complete)
        runtime.start_hydration(conversation.id)
        await asyncio.wait_for(runtime.wait_for_hydration(conversation.id), 5)
        assert [message.content for message in builder._history] == ["readable history"]
        assert cold.get_context_snapshot_revision(conversation.id) == conversation.revision
        assert completed == [conversation.id]
        await runtime.shutdown()

    asyncio.run(exercise())
