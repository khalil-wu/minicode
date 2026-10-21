from __future__ import annotations

import asyncio

import pytest

from backend.agent.message import AgentEvent, UserCommand
from backend.conversations.repository import ConversationRepository
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.skills.runtime_roots import RuntimeSkillRoots
from backend.tests.test_conversation_window_follower import windows
from backend.tests.test_queue_owner_chain_audit import _manager, _message
from backend.tests.test_unconfigured_session_protocol import ProtocolSocket, protocol_environment


def test_shared_queue_edits_replace_stale_hot_text_and_survive_cold_restart(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    with _manager(repo, "first") as first, _manager(repo, "second") as second:
        original = _message("one")
        original.data["_model_runtime_handle"] = object()
        first.enqueue_user_message("conv_queueaudit", original)
        first.enqueue_user_message("conv_queueaudit", _message("two"))
        assert first.queued_user_messages("conv_queueaudit")[0] is original
        updated = second.update_queued_user_message("conv_queueaudit", "one", {"content": "新的用户正文", "attachments": [],
            "display_content": "修改后的正文", "context_refs": [{"kind": "skill", "name": "source", "path": "/actual/SKILL.md"}]})
        assert updated.data["assistant_message_id"] == "one"
        second.reorder_queued_user_messages("conv_queueaudit", ["two", "one"])
        first_queue = first.queued_user_messages("conv_queueaudit")
        assert [command.data["assistant_message_id"] for command in first_queue] == ["two", "one"]
        assert first_queue[1].data["content"] == "新的用户正文"
        assert first_queue[1] is not original
        assert first_queue[1].data["_submitted_content"] == "新的用户正文"
        assert original.data["content"] == "one"
    with _manager(repo, "cold") as cold:
        rows = cold.queued_user_message_snapshot("conv_queueaudit")
        assert [row["message_id"] for row in rows] == ["two", "one"]
        assert rows[1]["content"] == "新的用户正文"
        assert rows[1]["context_refs"][0]["path"] == "/actual/SKILL.md"


@pytest.mark.parametrize("ids", [[], ["one"], ["one", "one"], ["one", "foreign"]])
def test_reorder_requires_source_exact_set_without_mutating_queue(tmp_path, ids):
    repo = ConversationRepository(tmp_path / "conversations")
    with _manager(repo, "exact") as manager:
        for name in ("one", "two"):
            manager.enqueue_user_message("conv_queueaudit", _message(name))
        with pytest.raises(ValueError, match="every queued submission exactly once"):
            manager.reorder_queued_user_messages("conv_queueaudit", ids)
        assert [row["message_id"] for row in manager.queued_user_message_snapshot()] == ["one", "two"]


def test_claimed_input_cannot_be_edited_or_revived_by_stale_reorder(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    with _manager(repo, "first") as first, _manager(repo, "second") as second:
        first.enqueue_user_message("conv_queueaudit", _message("one"))
        first.enqueue_user_message("conv_queueaudit", _message("two"))
        second.queued_user_messages("conv_queueaudit")
        claimed = first.dequeue_user_message("conv_queueaudit")
        assert claimed.data["content"] == "one"
        assert second.update_queued_user_message("conv_queueaudit", "one", {"content": "too late"}) is None
        with pytest.raises(ValueError):
            second.reorder_queued_user_messages("conv_queueaudit", ["two", "one"])
        assert [row["message_id"] for row in second.queued_user_message_snapshot()] == ["two"]
        first.finish_user_message_dispatch("conv_queueaudit", claimed, succeeded=True)


def test_failed_edit_publish_keeps_last_durable_input_and_reports_the_io_error(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    with _manager(repo, "writer") as manager:
        manager.enqueue_user_message("conv_queueaudit", _message("one"))
        write = manager.durable_queue._write_current_unlocked
        monkeypatch.setattr(manager.durable_queue, "_write_current_unlocked", lambda: (_ for _ in ()).throw(OSError("actual disk error")))
        with pytest.raises(OSError, match="actual disk error"):
            manager.update_queued_user_message("conv_queueaudit", "one", {"content": "not committed"})
        monkeypatch.setattr(manager.durable_queue, "_write_current_unlocked", write)
        assert manager.queued_user_message_snapshot()[0]["content"] == "one"


@pytest.mark.asyncio
async def test_real_window_dispatch_preserves_edit_owner_inputs_and_never_starts_a_new_run(windows):
    manager, owner, follower, conversation, other, start, drain = windows
    task = start()
    await follower.reemit_pending_state(conversation.id)
    owner.active_conversation_id = other.id
    for name in ("one", "two"):
        await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.add", {
            "client_command_id": "add-" + name, "conversation_id": conversation.id,
            "message_id": "assistant-" + name, "user_message_id": "user-" + name, "content": "原始" + name,
            "display_content": "显示" + name, "attachments": [],
        }), follower.connection_generation)
    first = owner.run_manager.queued_user_messages(conversation.id)[0]
    assert first.data["_client_queue_key"] == follower.run_manager.durable_client_commands.path.stem
    original_acceptance_order = first.data["_guardian_acceptance_order"]
    owner.attachment_store.save(artifact_id="kept-file", content="真正的附件正文", metadata={
        "conversation_id": conversation.id, "workspace_root": conversation.workspace_root})
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.update", {
        "client_command_id": "edit-one", "conversation_id": conversation.id, "message_id": "assistant-one",
        "content": "实际修改正文", "display_content": "实际可见正文",
        "attachments": [{"artifact_id": "kept-file", "file_name": "正文.txt", "size_bytes": 10}],
        "context_refs": [{"kind": "skill", "name": "selected-skill", "path": str(conversation.workspace_root) + "/SKILL.md"}],
        "quoted_message": {"id": "quote", "role": "assistant", "content": "保留引用"},
    }), follower.connection_generation)
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.reorder", {
        "client_command_id": "reorder", "conversation_id": conversation.id, "message_ids": ["assistant-two", "assistant-one"],
    }), follower.connection_generation)
    await drain()
    rows = owner.run_manager.queued_user_message_snapshot(conversation.id)
    assert [row["message_id"] for row in rows] == ["assistant-two", "assistant-one"]
    assert rows[1]["content"] == "实际修改正文" and rows[1]["display_content"] == "实际可见正文"
    assert rows[1]["attachments"][0]["artifact_id"] == "kept-file"
    assert rows[1]["quoted_message"]["content"] == "保留引用"
    assert owner.run_manager.queued_user_messages(conversation.id)[1].data["_guardian_acceptance_order"] > original_acceptance_order
    assert owner.run_manager.running_task_for(conversation.id) is task and not follower.run_manager.run_tasks
    assert owner.active_conversation_id == other.id
    assert any(event.get("input", {}).get("content") == "实际修改正文" for event in follower.ws.sent if event["type"] == "user_message.queue.updated")
    assert any(event.get("client_command_id") == "edit-one" and event.get("level") == "success" for event in follower.ws.sent)
    assert not any(event.get("client_command_id") == "edit-one" for event in owner.ws.sent)


@pytest.mark.asyncio
async def test_queue_mutation_rejects_foreign_attachment_without_replacing_the_original(windows):
    _, owner, follower, conversation, other, start, drain = windows
    start()
    owner.run_manager.enqueue_user_message(conversation.id, UserCommand("user_message", {
        "assistant_message_id": "retained", "user_message_id": "retained-user", "content": "原始未修改正文"}))
    owner.attachment_store.save(artifact_id="foreign", content="other workspace bytes", metadata={
        "conversation_id": other.id, "workspace_root": other.workspace_root})
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.update", {
        "client_command_id": "foreign-edit", "conversation_id": conversation.id, "message_id": "retained",
        "content": "forged", "attachments": [{"artifact_id": "foreign", "file_name": "private.txt"}],
    }), follower.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_message_snapshot(conversation.id)[0]["content"] == "原始未修改正文"
    assert any(event.get("client_command_id") == "foreign-edit" and event.get("level") == "error" for event in follower.ws.sent)


@pytest.mark.asyncio
async def test_extra_roots_are_global_user_scope_replaceable_watched_and_frozen_for_active_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("user", root) for root in self._extra_roots])
    notifications = asyncio.Queue()

    async def changed():
        await notifications.put(True)

    roots = RuntimeSkillRoots(changed, throttle_seconds=.05)
    first = SkillManager(SkillLoader(), runtime_roots=roots)
    second = SkillManager(SkillLoader(), runtime_roots=roots)
    root = tmp_path / "extra"
    file = root / "workflow" / "SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: extra-workflow\ndescription: original description\n---\nOriginal instructions", encoding="utf-8")
    try:
        await roots.set_extra_roots([str(root)])
        await asyncio.wait_for(notifications.get(), 3)
        assert first.list_all()[0]["source_level"] == "user"
        frozen = first.snapshot(None)
        assert second.get_meta("extra-workflow").description == "original description"
        file.write_text("---\nname: extra-workflow\ndescription: actual live edit\n---\nUpdated instructions", encoding="utf-8")
        await asyncio.wait_for(notifications.get(), 5)
        assert second.get_meta("extra-workflow").description == "actual live edit"
        assert frozen.get_meta("extra-workflow").description == "original description"
        missing = tmp_path / "missing"
        await roots.set_extra_roots([str(missing)])
        await asyncio.wait_for(notifications.get(), 3)
        assert first.list_all() == second.list_all() == []
        late = missing / "late" / "SKILL.md"
        late.parent.mkdir(parents=True)
        late.write_text("---\nname: late-created-skill\ndescription: Watched missing root\n---\nLate instructions", encoding="utf-8")
        await asyncio.wait_for(notifications.get(), 5)
        assert second.get_meta("late-created-skill") is not None
        await roots.set_extra_roots([])
        assert roots.roots == ()
        assert first.list_all() == []
        with pytest.raises(ValueError, match="absolute"):
            await roots.set_extra_roots(["relative/path"])
    finally:
        await roots.aclose()
    assert not roots._observer.is_alive()


@pytest.mark.asyncio
async def test_extra_roots_command_updates_both_real_session_catalogs_and_broadcasts_global_change(windows, tmp_path, monkeypatch):
    manager, owner, follower, _, _, _, drain = windows
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("user", root) for root in self._extra_roots])

    async def changed():
        await manager.broadcast_event(AgentEvent("skills/changed", {}))

    roots = RuntimeSkillRoots(changed, throttle_seconds=.05)
    for session in (owner, follower):
        session.skill_manager = SkillManager(SkillLoader(), runtime_roots=roots)
    path = tmp_path / "runtime-skills" / "new" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: runtime-command-skill\ndescription: Actual user runtime root\n---\nInstructions", encoding="utf-8")
    try:
        await follower.command_dispatcher._dispatch_client_command(UserCommand("skills/extraRoots/set", {
            "client_command_id": "extra-roots", "extraRoots": [str(path.parent.parent)]}), follower.connection_generation)
        await drain()
        assert owner.skill_manager.get_meta("runtime-command-skill") is not None
        assert follower.skill_manager.get_meta("runtime-command-skill") is not None
        assert all(any(event["type"] == "skills/changed" for event in session.ws.sent) for session in (owner, follower))
        assert any(event.get("client_command_id") == "extra-roots" and event.get("level") == "success" for event in follower.ws.sent)
    finally:
        await roots.aclose()


@pytest.mark.asyncio
async def test_application_websocket_entrypoint_carries_bootstrap_roots_to_new_and_reconnected_sessions(tmp_path, monkeypatch, protocol_environment):
    from backend import main
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("user", root) for root in self._extra_roots])
    skill = tmp_path / "app-shared-skills" / "workflow" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: actual-app-shared-root\ndescription: Original bootstrap owner\n---\nShared instructions", encoding="utf-8")
    sockets = [ProtocolSocket("app-shared-roots-first"), ProtocolSocket("app-shared-roots-second")]
    async with main.lifespan(main.app):
        tasks = [asyncio.create_task(main.websocket_endpoint(socket)) for socket in sockets]
        active = list(zip(sockets, tasks))
        try:
            for socket in sockets:
                await socket.until("llm.model.updated")
            manager = main._state.ws_manager
            first, second = [manager.get_session(socket.query_params["session_id"]) for socket in sockets]
            roots = main._state.bootstrap.runtime_skill_roots
            await sockets[0].command("skills/extraRoots/set", client_command_id="app-extra-roots", extraRoots=[str(skill.parent.parent)])
            result = await sockets[0].until("command.result", client_command_id="app-extra-roots")
            assert result["level"] == "success" and roots.roots == (skill.parent.parent.resolve(),)
            for session in (first, second):
                assert session.skill_manager.get_meta("actual-app-shared-root").source_level == "user"
            await sockets[0].finish(tasks[0])
            reconnect = ProtocolSocket(sockets[0].query_params["session_id"])
            reconnect_task = asyncio.create_task(main.websocket_endpoint(reconnect))
            active = [(sockets[1], tasks[1]), (reconnect, reconnect_task)]
            await reconnect.until("llm.model.updated")
            assert manager.get_session(reconnect.query_params["session_id"]) is first
            assert first.skill_manager.get_meta("actual-app-shared-root") is not None
            await reconnect.command("skills/extraRoots/set", client_command_id="replace-app-extra-roots", extraRoots=[])
            result = await reconnect.until("command.result", client_command_id="replace-app-extra-roots")
            assert result["level"] == "success" and roots.roots == ()
            assert first.skill_manager.list_all() == second.skill_manager.list_all() == []
        finally:
            for socket, task in active:
                await socket.finish(task)
