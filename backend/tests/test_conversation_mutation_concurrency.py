from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.conversations.models import ConversationRecord
from backend.ws.handler import WebSocketSession
from backend.ws.command_dispatcher import (
    SessionCommandDispatcher,
    _command_targets_conversation_delete_fence,
    _is_conversation_lifecycle_command,
)
from backend.ws.manager import WebSocketManager
from backend.agent.message import UserCommand
from backend.ws.handlers import conversation as conversation_handlers


def _manager_with(*sessions):
    return SimpleNamespace(iter_sessions=lambda: list(sessions))


def _command_result_session(*, conversation_id: str, repository) -> SimpleNamespace:
    session = SimpleNamespace(
        session_id="session-a",
        active_conversation_id=conversation_id,
        conversation_repo=repository,
        emit_command_result=AsyncMock(),
        running_agent_task_for=lambda _conversation_id: None,
    )
    session.ws_manager = _manager_with(session)
    return session


def test_terminal_preview_scheduler_and_conversation_commands_share_lifecycle_domain() -> None:
    for command in (
        "conversation.clear",
        "terminal.create",
        "preview.launch",
        "scheduler.update",
        "user_message",
    ):
        assert _is_conversation_lifecycle_command(command) is True
    assert _is_conversation_lifecycle_command("commands.list") is False

    async def inspect() -> None:
        manager = WebSocketManager()
        first = manager.conversation_lifecycle_lock()
        second = manager.conversation_lifecycle_lock()
        assert first is second

    asyncio.run(inspect())


def test_delete_fence_resolves_implicit_and_global_lifecycle_targets() -> None:
    manager = WebSocketManager()
    session = SimpleNamespace(
        active_conversation_id="conv-deleting",
        ws_manager=manager,
    )
    token, reason, count = manager.begin_conversation_delete("conv-deleting")

    assert token
    assert reason == ""
    assert count == 0
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(type="conversation.rename", data={"conversation_id": "conv-deleting"}),
    ) == ("conv-deleting",)
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(type="user_message", data={"content": "late write"}),
    ) == ("conv-deleting",)
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(type="conversation.create", data={"activate": True}),
    ) == ("conv-deleting",)
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(
            type="conversation.list",
            data={"preferred_conversation_id": "conv-deleting"},
        ),
    ) == ("conv-deleting",)
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(type="memory.reset", data={"confirmed": True}),
    ) == ("conv-deleting",)
    assert _command_targets_conversation_delete_fence(
        session,
        UserCommand(type="conversation.export", data={"conversation_id": "conv-deleting"}),
    ) == ()

    manager.end_conversation_delete("conv-deleting", token)


def test_command_ingress_rejects_mutation_while_delete_fence_is_held() -> None:
    async def scenario() -> None:
        manager = WebSocketManager()
        token, _, _ = manager.begin_conversation_delete("conv-deleting")
        assert token
        session = SimpleNamespace(
            active_conversation_id="conv-deleting",
            ws_manager=manager,
            connection_generation=1,
            event_outbox=SimpleNamespace(
                bind_connection_generation=lambda _generation: contextlib.nullcontext(),
            ),
            _handle_command_inner=AsyncMock(),
            send_event=AsyncMock(),
        )
        session.command_dispatcher = object.__new__(SessionCommandDispatcher)
        session.command_dispatcher._session = session

        await session.command_dispatcher._handle_command(
            UserCommand(type="conversation.rename", data={"conversation_id": "conv-deleting"}),
        )

        session._handle_command_inner.assert_not_awaited()
        event = session.send_event.await_args.args[0]
        assert event.type == "command.result"
        assert event.data["data"]["reason"] == "delete_in_progress"
        manager.end_conversation_delete("conv-deleting", token)

    asyncio.run(scenario())


def test_conversation_delete_task_survives_initiator_disconnect(monkeypatch) -> None:
    async def scenario() -> None:
        manager = WebSocketManager()
        started = asyncio.Event()
        release = asyncio.Event()
        target = SimpleNamespace(id="conv-detached-delete")
        session = SimpleNamespace(
            ws_manager=manager,
            is_connected=True,
            conversation_repo=SimpleNamespace(
                get_conversation=lambda _conversation_id: target,
            ),
        )

        async def detached_delete(_session, _data):
            started.set()
            await release.wait()
            return True

        monkeypatch.setattr(
            conversation_handlers,
            "_handle_conversation_delete_fenced",
            detached_delete,
        )

        await conversation_handlers.handle_conversation_delete(
            session,
            {"conversation_id": target.id},
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        session.is_connected = False
        assert manager.conversation_delete_fence(target.id)

        release.set()
        await asyncio.wait_for(
            asyncio.gather(*tuple(manager._conversation_delete_tasks)),
            timeout=1,
        )
        await asyncio.sleep(0)

        assert manager._conversation_delete_tasks == set()
        assert manager.conversation_delete_fence(target.id) is None

    asyncio.run(scenario())


def test_manager_shutdown_drains_detached_conversation_delete(monkeypatch) -> None:
    async def scenario() -> None:
        manager = WebSocketManager()
        started = asyncio.Event()
        target = SimpleNamespace(id="conv-manager-shutdown")
        session = SimpleNamespace(
            ws_manager=manager,
            is_connected=False,
            conversation_repo=SimpleNamespace(
                get_conversation=lambda _conversation_id: target,
            ),
        )

        async def detached_delete(_session, _data):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            conversation_handlers,
            "_handle_conversation_delete_fenced",
            detached_delete,
        )

        await conversation_handlers.handle_conversation_delete(
            session,
            {"conversation_id": target.id},
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await manager.shutdown()

        assert manager._conversation_delete_tasks == set()
        assert manager.conversation_delete_fence(target.id) is None

    asyncio.run(scenario())


def test_delete_release_waiter_still_releases_fence_after_its_own_cancellation() -> None:
    async def scenario() -> None:
        manager = WebSocketManager()
        token, _, _ = manager.begin_conversation_delete("conv-release-waiter")
        assert token
        cleanup_owner = manager.conversation_delete_cleanup_owner("conv-release-waiter")
        assert cleanup_owner is not None
        release = asyncio.Event()

        async def cleanup() -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        cleanup_task = asyncio.create_task(cleanup())
        cleanup_owner.add(cleanup_task)
        manager.finish_conversation_delete("conv-release-waiter", token)
        release_task = manager._conversation_delete_release_tasks["conv-release-waiter"]

        await asyncio.sleep(0)
        release_task.cancel()
        await asyncio.sleep(0)
        assert manager.conversation_delete_fence("conv-release-waiter")
        assert release_task.done() is False

        release.set()
        await asyncio.wait_for(
            asyncio.gather(release_task, cleanup_task, return_exceptions=True),
            timeout=1,
        )
        await asyncio.sleep(0)
        assert manager.conversation_delete_fence("conv-release-waiter") is None

    asyncio.run(scenario())


def test_workspace_projection_switches_every_active_renderer() -> None:
    conversation = ConversationRecord(
        id="conv_workspace",
        title="Workspace task",
        revision=4,
    )
    sessions = []
    for session_id in ("session-a", "session-b"):
        owner = SimpleNamespace(
            session_id=session_id,
            active_conversation_id=conversation.id,
            conversation_repo=SimpleNamespace(get_conversation=lambda _conversation_id: conversation),
            switch_workspace_for_conversation=AsyncMock(return_value=True),
            send_payload=AsyncMock(return_value=True),
            runtime_snapshot=lambda session_id=session_id: {"session_id": session_id},
        )
        sessions.append(owner)
    sessions[0].ws_manager = _manager_with(*sessions)

    errors = asyncio.run(
        conversation_handlers._switch_active_sessions_to_conversation_workspace(
            sessions[0],
            conversation,
            announce_initiator=True,
            source="test",
        )
    )

    assert errors == []
    sessions[0].switch_workspace_for_conversation.assert_awaited_once_with(
        conversation,
        announce=True,
        wait_for_initialize=True,
    )
    sessions[1].switch_workspace_for_conversation.assert_awaited_once_with(
        conversation,
        announce=False,
        wait_for_initialize=True,
    )
    for owner in sessions:
        payload = owner.send_payload.await_args.args[0]
        assert payload["type"] == "conversation.switched"
        assert payload["conversation_id"] == conversation.id


def test_permission_rule_projection_updates_all_active_runtimes_and_all_windows() -> None:
    sessions = []
    for session_id, active_id in (
        ("session-a", "conv_rules"),
        ("session-b", "conv_rules"),
        ("session-c", "conv_other"),
    ):
        owner = SimpleNamespace(
            session_id=session_id,
            active_conversation_id=active_id,
            set_permission_context_rules=Mock(return_value=True),
            session_lifecycle=SimpleNamespace(
                send_task_runtime_update=AsyncMock(),
            ),
            emit_permission_rules_updated=AsyncMock(),
        )
        sessions.append(owner)
    sessions[0].ws_manager = _manager_with(*sessions)

    errors = asyncio.run(conversation_handlers._project_permission_rules_update(
        sessions[0],
        conversation_id="conv_rules",
        source="test",
        overrides={"read_file": "allow"},
        deny_rules=["run_command(rm:*)"],
    ))

    assert errors == []
    for owner in sessions[:2]:
        owner.set_permission_context_rules.assert_called_once_with(
            session_overrides={"read_file": "allow"},
        tool_deny_rules=["run_command(rm:*)"],
            source="test",
        )
        owner.session_lifecycle.send_task_runtime_update.assert_awaited_once()
    sessions[2].set_permission_context_rules.assert_not_called()
    sessions[2].session_lifecycle.send_task_runtime_update.assert_not_awaited()
    for owner in sessions:
        owner.emit_permission_rules_updated.assert_awaited_once_with(
            conversation_id="conv_rules",
            source="test",
        )


def test_archive_reports_resource_counts_before_persisting(monkeypatch) -> None:
    target = SimpleNamespace(id="conv_archive")
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        set_archived=Mock(),
    )
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_activity_blockers",
        lambda _session, _conversation_id: {
            "background_commands": 1,
            "terminal_sessions": 2,
            "preview_processes": 3,
            "scheduled_tasks": 4,
        },
    )

    asyncio.run(conversation_handlers.handle_conversation_archive(
        session,
        {"conversation_id": target.id},
    ))

    repository.set_archived.assert_not_called()
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.archive"
    assert result.kwargs["data"] == {
        "conversation_id": target.id,
        "reason": "runtime_resource_active",
        "resources": {
            "background_commands": 1,
            "terminal_sessions": 2,
            "preview_processes": 3,
            "scheduled_tasks": 4,
        },
        "retryable": True,
    }


def test_memory_mode_race_cannot_mutate_after_run_check(monkeypatch) -> None:
    target = SimpleNamespace(
        id="conv_memory",
        memory_mode="polluted",
        memory_polluted=True,
        memory_pollution_sources=["web_search"],
    )
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        update_memory_mode=Mock(),
    )
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_has_active_run",
        lambda _session, _conversation_id: False,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_try_claim_conversation_mutation",
        lambda *_args, **_kwargs: None,
    )

    asyncio.run(conversation_handlers.handle_conversation_memory_mode_set(
        session,
        {"conversation_id": target.id, "memory_mode": "enabled"},
    ))

    repository.update_memory_mode.assert_not_called()
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.memory_mode.set"
    assert result.kwargs["data"]["reason"] == "run_active"
    assert result.kwargs["data"]["retryable"] is True


def test_clear_cannot_cross_a_detached_query_claim(monkeypatch) -> None:
    target = SimpleNamespace(id="conv_clear", context_snapshot={})
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        clear_conversation=Mock(),
    )
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_stop_conversation_run",
        AsyncMock(return_value=True),
    )
    claim = conversation_query_guards().try_start(target.id, owner_id="scheduler:test")
    assert claim is not None
    try:
        asyncio.run(conversation_handlers.handle_conversation_clear(
            session,
            {"conversation_id": target.id},
        ))
    finally:
        assert conversation_query_guards().end(claim) is True

    repository.clear_conversation.assert_not_called()
    result = session.emit_command_result.await_args
    assert result.args[0] == "clear"
    assert result.kwargs["data"]["reason"] == "run_active"


def test_worktree_cleanup_rejects_active_query_before_git_mutation(monkeypatch) -> None:
    target = SimpleNamespace(id="conv_cleanup", git_isolated=True)
    repository = SimpleNamespace(get_conversation=lambda _conversation_id: target)
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_activity_blockers",
        lambda _session, _conversation_id: {
            "background_commands": 0,
            "terminal_sessions": 0,
            "preview_processes": 0,
            "scheduled_tasks": 0,
        },
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", cleanup)
    claim = conversation_query_guards().try_start(target.id, owner_id="rest:test")
    assert claim is not None
    try:
        asyncio.run(conversation_handlers.handle_conversation_worktree_cleanup(
            session,
            {"conversation_id": target.id},
        ))
    finally:
        assert conversation_query_guards().end(claim) is True

    cleanup.assert_not_awaited()
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.worktree.cleanup"
    assert result.kwargs["data"]["reason"] == "run_active"


def test_worktree_cleanup_rebinds_and_projects_all_active_windows(monkeypatch) -> None:
    target = SimpleNamespace(
        id="conv_cleanup_success",
        git_isolated=True,
        workspace_root="C:\\repo\\.minicode\\worktrees\\task",
        worktree_path="C:\\repo\\.minicode\\worktrees\\task",
        git_branch="minicode/conv_cleanup_success",
    )
    updated = SimpleNamespace(
        id=target.id,
        revision=8,
        git_isolated=False,
        workspace_root="C:\\repo",
        worktree_path="",
        git_branch="",
    )
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        update_workspace_binding=Mock(return_value=updated),
    )
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_activity_blockers",
        lambda _session, _conversation_id: {
            "background_commands": 0,
            "terminal_sessions": 0,
            "preview_processes": 0,
            "scheduled_tasks": 1,
        },
    )
    release = AsyncMock(return_value=[session])
    cleanup = AsyncMock(return_value={
        "removed": True,
        "conversation_id": target.id,
        "workspace_root": "C:\\repo",
        "message": "Removed isolated worktree",
    })
    switch = AsyncMock(return_value=["session-b"])
    broadcast = AsyncMock(return_value=[])
    monkeypatch.setattr(
        conversation_handlers,
        "_release_active_sessions_from_conversation_workspace",
        release,
    )
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", cleanup)
    monkeypatch.setattr(
        conversation_handlers,
        "_switch_active_sessions_to_conversation_workspace",
        switch,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_broadcast_conversation_lists",
        broadcast,
    )

    asyncio.run(conversation_handlers.handle_conversation_worktree_cleanup(
        session,
        {"conversation_id": target.id},
    ))

    release.assert_awaited_once_with(session, target.id)
    cleanup.assert_awaited_once_with(session, target, force=False)
    repository.update_workspace_binding.assert_called_once_with(
        target.id,
        workspace_root="C:\\repo",
        git_branch="",
        worktree_path="",
        git_isolated=False,
    )
    switch.assert_awaited_once_with(
        session,
        updated,
        announce_initiator=True,
        source="conversation.worktree.cleanup",
    )
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.worktree.cleanup"
    assert result.kwargs["level"] == "warning"
    assert result.kwargs["data"]["revision"] == 8
    assert result.kwargs["data"]["projection_errors"] == ["session-b"]


def test_worktree_cleanup_binding_failure_recreates_original_worktree(monkeypatch, tmp_path) -> None:
    base_root = tmp_path / "repo"
    source = base_root / ".minicode" / "worktrees" / "conv_cleanup_rollback"
    target = SimpleNamespace(
        id="conv_cleanup_rollback",
        git_isolated=True,
        workspace_root=str(source),
        worktree_path=str(source),
        git_branch="minicode/conv_cleanup_rollback",
    )
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        update_workspace_binding=Mock(return_value=None),
    )
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    session.main_worktree_root = lambda _path: base_root
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_activity_blockers",
        lambda _session, _conversation_id: {
            "background_commands": 0,
            "terminal_sessions": 0,
            "preview_processes": 0,
            "scheduled_tasks": 0,
        },
    )
    release = AsyncMock(return_value=[session])
    cleanup = AsyncMock(return_value={
        "removed": True,
        "conversation_id": target.id,
        "path": str(source),
        "branch": target.git_branch,
        "head": "original-head",
        "snapshot_id": "wtsnap_rollback",
        "workspace_root": str(base_root),
        "message": "Removed isolated worktree",
    })
    switch = AsyncMock(return_value=[])
    monkeypatch.setattr(
        conversation_handlers,
        "_release_active_sessions_from_conversation_workspace",
        release,
    )
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", cleanup)
    monkeypatch.setattr(
        conversation_handlers,
        "_switch_active_sessions_to_conversation_workspace",
        switch,
    )

    restore_calls: list[tuple] = []

    class Manager:
        def __init__(self, root):
            assert root == base_root.resolve()

        def restore_removed_worktree(
            self,
            path,
            *,
            branch: str,
            expected_head: str,
            snapshot_id: str,
        ):
            restore_calls.append((path, branch, expected_head, snapshot_id))
            return SimpleNamespace(restored=True, path=path, error="")

    monkeypatch.setattr("backend.workspace.worktree.WorktreeManager", Manager)

    asyncio.run(conversation_handlers.handle_conversation_worktree_cleanup(
        session,
        {"conversation_id": target.id, "force": True},
    ))

    assert restore_calls == [(
        source.resolve(),
        target.git_branch,
        "original-head",
        "wtsnap_rollback",
    )]
    switch.assert_awaited_once_with(
        session,
        target,
        announce_initiator=False,
        source="conversation.worktree.cleanup.rollback_binding",
    )
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.worktree.cleanup"
    assert result.kwargs["level"] == "error"
    assert result.kwargs["data"]["reason"] == "workspace_binding_failed"
    assert result.kwargs["data"]["rollback_completed"] is True
    assert result.kwargs["data"]["rollback_errors"] == []
    assert result.kwargs["data"]["recovery_required"] is False


def test_worktree_cleanup_late_binding_error_does_not_recreate_worktree(monkeypatch, tmp_path) -> None:
    base_root = tmp_path / "repo"
    source = base_root / ".minicode" / "worktrees" / "conv_cleanup_late_commit"
    target = SimpleNamespace(
        id="conv_cleanup_late_commit",
        revision=3,
        git_isolated=True,
        workspace_root=str(source),
        worktree_path=str(source),
        git_branch="minicode/conv_cleanup_late_commit",
    )
    committed = SimpleNamespace(
        id=target.id,
        revision=4,
        git_isolated=False,
        workspace_root=str(base_root),
        worktree_path="",
        git_branch="",
    )

    class Repository:
        current = target

        def get_conversation(self, _conversation_id: str):
            return self.current

        def update_workspace_binding(self, _conversation_id: str, **_kwargs):
            self.current = committed
            raise OSError("directory fsync failed after manifest replace")

    repository = Repository()
    session = _command_result_session(
        conversation_id=target.id,
        repository=repository,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_conversation_activity_blockers",
        lambda _session, _conversation_id: {
            "background_commands": 0,
            "terminal_sessions": 0,
            "preview_processes": 0,
            "scheduled_tasks": 0,
        },
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_release_active_sessions_from_conversation_workspace",
        AsyncMock(return_value=[session]),
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_cleanup_conversation_worktree",
        AsyncMock(return_value={
            "removed": True,
            "conversation_id": target.id,
            "path": str(source),
            "branch": target.git_branch,
            "head": "original-head",
            "workspace_root": str(base_root),
            "message": "Removed isolated worktree",
        }),
    )
    restore = AsyncMock()
    switch = AsyncMock(return_value=[])
    broadcast = AsyncMock(return_value=[])
    monkeypatch.setattr(
        conversation_handlers,
        "_restore_removed_conversation_worktree",
        restore,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_switch_active_sessions_to_conversation_workspace",
        switch,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_broadcast_conversation_lists",
        broadcast,
    )

    asyncio.run(conversation_handlers.handle_conversation_worktree_cleanup(
        session,
        {"conversation_id": target.id},
    ))

    restore.assert_not_awaited()
    switch.assert_awaited_once_with(
        session,
        committed,
        announce_initiator=True,
        source="conversation.worktree.cleanup",
    )
    result = session.emit_command_result.await_args
    assert result.kwargs["level"] == "warning"
    assert "binding_warning" in result.kwargs["data"]
    assert result.kwargs["data"]["revision"] == 4


def test_local_to_worktree_binding_failure_restores_source_and_removes_created_git_state(
    monkeypatch,
    tmp_path,
) -> None:
    source = tmp_path / "repo"
    worktree = source / ".minicode" / "worktrees" / "conv_handoff_local"
    source.mkdir(parents=True)
    order: list[str] = []
    conversation = SimpleNamespace(
        id="conv_handoff_local",
        workspace_root=str(source),
        worktree_path="",
        git_branch="main",
        git_isolated=False,
    )

    class Repository:
        def update_workspace_binding(self, conversation_id: str, **_kwargs):
            assert conversation_id == conversation.id
            order.append("persist-binding")
            return None

        def get_conversation(self, _conversation_id: str):
            return conversation

    session = SimpleNamespace(
        session_id="session-handoff-local",
        conversation_repo=Repository(),
        current_workspace_root=lambda: source,
        main_worktree_root=lambda _path: source,
        emit_command_result=AsyncMock(),
    )
    creation = SimpleNamespace(
        created=True,
        workspace_root=str(worktree),
        worktree_path=str(worktree),
        git_branch="minicode/conv_handoff_local",
    )

    def create_binding(*_args, **_kwargs):
        order.append("create-worktree")
        return creation

    def stash_changes(path, *, label: str):
        assert path == source
        assert label == "minicode-handoff-conv_handoff_local"
        order.append("stash-source")
        return True, "stash@{0}"

    def restore_stash(path, stash_ref: str):
        assert path == source
        assert stash_ref == "stash@{0}"
        order.append("restore-source-stash")
        return True, ""

    class Manager:
        def __init__(self, root):
            assert root == source

        def remove_worktree(self, path, *, force: bool):
            assert path == worktree
            assert force is True
            order.append("remove-created-worktree")
            return True

    def delete_branch(root, branch: str):
        assert root == source
        assert branch == creation.git_branch
        order.append("delete-created-branch")
        return True, ""

    monkeypatch.setattr(
        "backend.services.conversation_payload_service.create_isolated_worktree_binding",
        create_binding,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.stash_workspace_changes",
        stash_changes,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.restore_workspace_stash",
        restore_stash,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.delete_local_branch",
        delete_branch,
    )
    monkeypatch.setattr("backend.workspace.worktree.WorktreeManager", Manager)

    asyncio.run(conversation_handlers._handle_conversation_worktree_handoff_claimed(
        session,
        conversation=conversation,
        conversation_id=conversation.id,
        target_kind="worktree",
        dirty_action="stash",
        preflight={"conversation_id": conversation.id},
    ))

    assert order == [
        "stash-source",
        "create-worktree",
        "persist-binding",
        "remove-created-worktree",
        "delete-created-branch",
        "restore-source-stash",
    ]
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.worktree.handoff.execute"
    assert result.kwargs["level"] == "error"
    assert result.kwargs["data"]["reason"] == "workspace_binding_failed"
    assert result.kwargs["data"]["rollback_completed"] is True
    assert result.kwargs["data"]["recovery_required"] is False


def test_worktree_to_local_binding_failure_recreates_worktree_before_restoring_stash(
    monkeypatch,
    tmp_path,
) -> None:
    base_root = tmp_path / "repo"
    source = base_root / ".minicode" / "worktrees" / "conv_handoff_protected"
    source.mkdir(parents=True)
    order: list[str] = []
    conversation = SimpleNamespace(
        id="conv_handoff_protected",
        workspace_root=str(source),
        worktree_path=str(source),
        git_branch="minicode/conv_handoff_protected",
        git_isolated=True,
    )

    class Repository:
        def update_workspace_binding(self, conversation_id: str, **_kwargs):
            assert conversation_id == conversation.id
            order.append("persist-binding")
            return None

        def get_conversation(self, _conversation_id: str):
            return conversation

    session = SimpleNamespace(
        session_id="session-handoff-protected",
        conversation_repo=Repository(),
        main_worktree_root=lambda _path: base_root,
        emit_command_result=AsyncMock(),
    )

    def stash_changes(path, *, label: str):
        assert path == source
        assert label == "minicode-handoff-conv_handoff_protected"
        order.append("stash-worktree")
        return True, "stash@{1}"

    def restore_stash(path, stash_ref: str):
        assert path == source
        assert stash_ref == "stash@{1}"
        order.append("restore-worktree-stash")
        return True, ""

    def switch_main(root, branch: str):
        assert root == base_root
        assert branch == conversation.git_branch
        order.append("switch-main-to-task")
        return True, ""

    def restore_main(root, *, branch: str, head: str):
        assert root == base_root
        assert branch == "main"
        assert head == "main-head"
        order.append("restore-main-checkout")
        return True, ""

    class Manager:
        def __init__(self, root):
            assert root == base_root

        def remove_worktree(self, path, *, force: bool):
            assert path == source
            assert force is False
            order.append("remove-worktree")
            return True

        def create_worktree(self, path, *, branch: str, new_branch: bool):
            assert path == source
            assert branch == conversation.git_branch
            assert new_branch is False
            order.append("recreate-worktree")
            return True

    async def release(_session, conversation_id: str):
        assert conversation_id == conversation.id
        order.append("release-active-runtimes")
        return [session]

    async def project(*_args, **_kwargs):
        order.append("project-restored-worktree")
        return []

    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.stash_workspace_changes",
        stash_changes,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.restore_workspace_stash",
        restore_stash,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.switch_main_checkout",
        switch_main,
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service.restore_main_checkout",
        restore_main,
    )
    monkeypatch.setattr("backend.workspace.worktree.WorktreeManager", Manager)
    monkeypatch.setattr(
        conversation_handlers,
        "_release_active_sessions_from_conversation_workspace",
        release,
    )
    monkeypatch.setattr(
        conversation_handlers,
        "_switch_active_sessions_to_conversation_workspace",
        project,
    )

    asyncio.run(conversation_handlers._handle_conversation_worktree_handoff_claimed(
        session,
        conversation=conversation,
        conversation_id=conversation.id,
        target_kind="local",
        dirty_action="stash",
        preflight={
            "conversation_id": conversation.id,
            "main_checkout": {
                "path": str(base_root),
                "branch": "main",
                "head": "main-head",
            },
        },
    ))

    assert order == [
        "stash-worktree",
        "release-active-runtimes",
        "remove-worktree",
        "switch-main-to-task",
        "persist-binding",
        "restore-main-checkout",
        "recreate-worktree",
        "restore-worktree-stash",
        "project-restored-worktree",
    ]
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.worktree.handoff.execute"
    assert result.kwargs["level"] == "error"
    assert result.kwargs["data"]["rollback_completed"] is True
    assert result.kwargs["data"]["rollback_errors"] == []


def test_post_commit_binding_error_is_detected_without_rolling_back_authority() -> None:
    expected = SimpleNamespace(
        id="conv_binding_warning",
        workspace_root="C:\\repo",
        git_branch="main",
        worktree_path="",
        git_isolated=False,
    )

    class Repository:
        def update_workspace_binding(self, *_args, **_kwargs):
            raise OSError("manifest replacement reported a late error")

        def get_conversation(self, conversation_id: str):
            assert conversation_id == expected.id
            return expected

    session = SimpleNamespace(conversation_repo=Repository())
    updated, warning = asyncio.run(
        conversation_handlers._persist_workspace_binding(
            session,
            expected.id,
            workspace_root=expected.workspace_root,
            git_branch=expected.git_branch,
            worktree_path=expected.worktree_path,
            git_isolated=expected.git_isolated,
        )
    )

    assert updated is expected
    assert "late error" in warning
