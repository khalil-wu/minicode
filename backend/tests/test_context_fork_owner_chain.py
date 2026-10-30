import asyncio
import copy
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from backend.agent.context import ContextBuilder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import AgentSettings, TokenBudget
from backend.conversations.repository import ConversationRepository
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.event_outbox import EventOutbox
from backend.ws.fork_registry import ForkRegistry
from backend.ws.handlers.conversation import handle_context_fork


class Socket:
    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["conversation", "workspace"])
async def test_stale_fork_settles_origin_receipt_without_forking_visible_context(tmp_path, stale):
    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    transcript = [{"id": "shared-id", "role": "assistant", "content": "fixture answer"}]
    origin = repo.create_conversation(transcript=transcript)
    visible = repo.create_conversation(transcript=transcript, workspace_root=str(tmp_path / "workspace-b"))
    context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
    context.append_assistant("fixture answer")
    socket = Socket()
    outbox = EventOutbox(session_id="fork-owner", websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=50, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=set())

    async def send(event):
        await outbox.send_payload({"type": event.type, **event.data}, log_context="fork")

    lock = asyncio.Lock()
    session = SimpleNamespace(session_id="fork-owner", conversation_repo=repo, active_conversation=visible,
        active_conversation_id=visible.id, context_builder=context, ws_manager=None,
        connection_generation=1, event_outbox=outbox, send_event=send,
        conversation_lifecycle_lock=lambda: lock)
    session.session_lifecycle = SessionLifecycle(session)
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    dispatcher._user_message_admissions = {}
    dispatcher._regeneration_transitions = {}
    dispatcher._command_semaphore = asyncio.Semaphore(1)
    dispatcher._handle_command_inner = lambda command: handle_context_fork(session, command.data)
    owner = origin.id if stale == "conversation" else visible.id
    command = UserCommand("context.fork", {"conversation_id": owner, "workspace_root": "",
        "client_command_id": "fork-owner-command", "message_id": "shared-id", "create_branch": True})
    assert await dispatcher._dispatch_client_command(command, 1) is True
    assert len(repo.list_conversations()) == 2
    assert session.active_conversation_id == visible.id
    result = socket.messages[-1]
    assert result["type"] == "command.result"
    assert result["level"] == "error"
    assert result["conversation_id"] == owner
    assert result["workspace_root"] == ""
    assert result["client_command_id"] == "fork-owner-command"
    await outbox.drain_delivery()
    await outbox.drain_persistence()


@pytest.mark.asyncio
async def test_explicit_projectless_owner_can_fork_current_context(tmp_path):
    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    parent = repo.create_conversation(transcript=[{"id": "answer", "role": "assistant", "content": "fixture"}])
    context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
    context.append_assistant("fixture")
    events = []

    async def hydrate(_):
        pass

    async def send(event):
        events.append(event)

    session = SimpleNamespace(conversation_repo=repo, active_conversation=parent,
        active_conversation_id=parent.id, context_builder=context, ws_manager=None,
        conversation_runtime=SimpleNamespace(wait_for_hydration=hydrate), send_event=send,
        fork_registry=ForkRegistry(session_id="fork-projectless", root_dir=tmp_path / "forks"),
        attachment_store=AttachmentStore(tmp_path / "attachments"),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), diagnostic_store=DiagnosticPayloadStore())
    await handle_context_fork(session, {"conversation_id": parent.id, "workspace_root": "",
        "message_id": "answer", "create_branch": False})
    assert events[0].type == "context_forked"
    assert events[0].data["parent_conversation_id"] == parent.id
    assert events[-1].type == "command.result" and events[-1].data["level"] == "success"


def _dispatching_fork_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("Original workspace content", encoding="utf-8")
    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
    context.append_user("First request")
    context.append_assistant("First answer")
    context.append_user("Later request")
    parent = repo.create_conversation(
        title="Receipt source",
        workspace_root=str(workspace), git_branch="existing-source-branch",
        permission_mode="auto", permission_deny_rules=["run_command"],
        permission_overrides={"read_file": "auto"},
        transcript=[
            {"id": "first-user", "role": "user", "content": "First request"},
            {"id": "fork-answer", "role": "assistant", "content": "First answer"},
            {"id": "later-user", "role": "user", "content": "Later request"},
        ],
        context_snapshot=context.export_snapshot(),
        model_selection={"provider": "openai", "model": "fixture-model", "reasoning_effort": "high"},
    )
    socket = Socket()
    outbox = EventOutbox(session_id="fork-semantic", websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=100, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=set())
    lock = asyncio.Lock()

    async def send(event):
        return await outbox.send_payload(event.to_ws_message(), log_context="fork-semantic")

    async def hydrate(_conversation_id):
        return None

    async def switch(conversation, *, announce):
        assert announce is False
        session.switched.append(conversation.id)
        return True

    def load_snapshot(conversation_id, snapshot, **_kwargs):
        session.active_conversation = repo.get_conversation(conversation_id)
        restored = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        restored.load_snapshot(snapshot)
        session.context_builder = restored

    async def send_list():
        session.published_lists += 1

    session = SimpleNamespace(conversation_repo=repo, active_conversation=parent,
        active_conversation_id=parent.id, context_builder=context, ws_manager=None,
        session_id="fork-semantic", connection_generation=1, event_outbox=outbox, send_event=send,
        conversation_lifecycle_lock=lambda: lock,
        conversation_runtime=SimpleNamespace(wait_for_hydration=hydrate),
        fork_registry=ForkRegistry(session_id="fork-semantic", root_dir=tmp_path / "forks"),
        attachment_store=AttachmentStore(tmp_path / "attachments"),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), diagnostic_store=DiagnosticPayloadStore(),
        switch_workspace_for_conversation=switch, load_active_conversation_snapshot=load_snapshot,
        sync_permission_mode_with_active_conversation=lambda **_kwargs: None,
        send_conversation_list=send_list, switched=[], published_lists=0)
    session.session_lifecycle = SessionLifecycle(session)
    session.send_payload = outbox.send_payload
    session.runtime_snapshot = lambda: {"active_conversation_id": session.active_conversation_id}
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    dispatcher._user_message_admissions = {}
    dispatcher._regeneration_transitions = {}
    dispatcher._command_semaphore = asyncio.Semaphore(1)
    dispatcher._handle_command_inner = lambda command: handle_context_fork(session, command.data)
    return session, dispatcher, socket, parent


def _fork_command(parent, **overrides):
    return UserCommand("context.fork", {
        "conversation_id": parent.id, "workspace_root": parent.workspace_root,
        "client_command_id": "fork-semantic-command", "message_id": "fork-answer",
        "create_branch": True, "activate": True, **overrides,
    })


async def _drain(session):
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()
    session.artifact_store.shutdown()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("activate", [False, True])
async def test_fork_success_receipt_waits_for_real_fork_and_replays_with_original_owner(tmp_path, activate):
    session, dispatcher, socket, parent = _dispatching_fork_session(tmp_path)
    before = copy.deepcopy(parent.context_snapshot)
    session.attachment_store.save(
        artifact_id="uploaded-source", content="Original attachment text",
        metadata={"conversation_id": parent.id, "workspace_root": parent.workspace_root},
    )
    artifact_id = session.artifact_store.save(
        "Original complete tool result", "read_file", conversation_id=parent.id, workspace_root=parent.workspace_root,
    )
    session.diagnostic_store.put("message", "fork-answer", {"usage": {"output_tokens": 4}}, conversation_id=parent.id)
    started, release = asyncio.Event(), asyncio.Event()

    async def hydrate(conversation_id):
        assert conversation_id == parent.id
        started.set()
        await release.wait()

    session.conversation_runtime.wait_for_hydration = hydrate
    task = asyncio.create_task(dispatcher._dispatch_client_command(_fork_command(parent, activate=activate), 1))
    await started.wait()
    assert socket.messages == []
    assert len(session.conversation_repo.list_conversations()) == 1
    release.set()
    assert await task is True
    forked = next(event for event in socket.messages if event["type"] == "context_forked")
    projections = [event for event in socket.messages if event["type"] == "conversation.switched"]
    if activate:
        assert len(projections) == 1
        projection = projections[0]
        assert projection["conversation_id"] == forked["branch_conversation_id"]
        assert projection["conversation"]["parent_conversation_id"] == parent.id
        assert projection["conversation"]["transcript"] == parent.transcript[:2]
        assert projection["session"]["active_conversation_id"] == forked["branch_conversation_id"]
        assert projection["client_command_id"] == "fork-semantic-command"
        assert projection["seq"] < forked["seq"]
    else:
        assert projections == []
    results = [event for event in socket.messages if event["type"] == "command.result"]
    assert len(results) == 1
    result = results[0]
    assert result["level"] == "success" and result["command"] == "context.fork"
    assert (result["client_command_id"], result["conversation_id"], result["workspace_root"]) == (
        "fork-semantic-command", parent.id, parent.workspace_root,
    )
    assert result["data"]["fork_id"] == forked["fork_id"]
    assert result["data"]["branch_created"] is True
    assert result["data"]["branch_activated"] is activate
    branch_id = forked["branch_conversation_id"]
    assert forked["conversation_id"] == (branch_id if activate else parent.id)
    assert session.active_conversation_id == (branch_id if activate else parent.id)
    await _drain(session)

    restored_repo = ConversationRepository(base_dir=tmp_path / "conversations")
    branch = restored_repo.get_conversation(branch_id)
    assert branch.parent_conversation_id == parent.id and branch.branch_kind == "context_fork"
    assert branch.parent_message_index == 1
    assert branch.transcript == parent.transcript[:2]
    assert len(branch.context_snapshot["history"]) == 2
    assert branch.model_selection == parent.model_selection
    assert branch.permission_deny_rules == parent.permission_deny_rules
    assert branch.permission_overrides == parent.permission_overrides
    assert branch.git_branch == "existing-source-branch" and branch.git_isolated is False
    for owner in (parent.id, branch.id):
        assert session.attachment_store.get("uploaded-source", conversation_id=owner, workspace_root=parent.workspace_root) == "Original attachment text"
        assert session.artifact_store.get(artifact_id, conversation_id=owner, workspace_root=parent.workspace_root) == "Original complete tool result"
    assert set(session.diagnostic_store.get("message", "fork-answer").conversation_ids) == {parent.id, branch.id}
    assert restored_repo.get_conversation(parent.id).context_snapshot == before
    assert (tmp_path / "workspace" / "source.txt").read_text(encoding="utf-8") == "Original workspace content"
    assert ForkRegistry(session_id="fork-semantic", root_dir=tmp_path / "forks").get(forked["fork_id"]).branch_conversation_id == branch.id
    replay = EventOutbox(session_id="fork-semantic", websocket=Socket(), replay_root=tmp_path / "replay",
        replay_limit=100, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=set())
    events, gap = replay.replay_window_after(forked["seq"])
    assert gap is False
    assert next(event for event in events if event["type"] == "command.result") == result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "no_active", "missing_message", "invalid_index", "outside_index", "empty_transcript", "active_turn",
    "mutation_owned", "hydration_failed", "owner_changed", "history_missing", "registry_write_failed", "branch_write_failed",
])
async def test_fork_rejections_settle_error_receipt_and_preserve_source(tmp_path, monkeypatch, failure):
    session, dispatcher, socket, parent = _dispatching_fork_session(tmp_path)
    original = copy.deepcopy(parent.to_dict())
    source_snapshot = session.context_builder.export_snapshot()
    command = _fork_command(parent)
    claim, transition = None, None
    if failure == "no_active":
        session.active_conversation = None
        session.active_conversation_id = ""
        command.data.update(conversation_id="", workspace_root="")
    elif failure == "missing_message":
        command.data["message_id"] = "missing-answer"
    elif failure in {"invalid_index", "outside_index", "empty_transcript"}:
        command.data.pop("message_id")
        command.data["message_index"] = "not-an-index" if failure == "invalid_index" else 99
        if failure == "empty_transcript":
            session.active_conversation = SimpleNamespace(**{**original, "transcript": []})
    elif failure == "active_turn":
        claim = conversation_query_guards().try_start(parent.id, owner_id="real-active-turn")
    elif failure == "mutation_owned":
        transition = conversation_query_guards().try_transition(parent.id, owner_id="real-lifecycle-transition")
    elif failure in {"hydration_failed", "owner_changed"}:
        async def hydrate(_conversation_id):
            if failure == "hydration_failed":
                raise OSError("Actual snapshot hydration failed")
            session.active_conversation_id = "another-visible-conversation"
        session.conversation_runtime.wait_for_hydration = hydrate
    elif failure == "history_missing":
        replacement = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        replacement.append_user("Compacted summary without the target answer")
        session.context_builder = replacement
    elif failure == "registry_write_failed":
        def write_failure():
            raise OSError("Actual fork registry write failed")
        monkeypatch.setattr(session.fork_registry, "_save", write_failure)
    elif failure == "branch_write_failed":
        def write_failure(**_kwargs):
            raise OSError("Actual fork conversation write failed")
        monkeypatch.setattr(session.conversation_repo, "create_conversation", write_failure)

    try:
        assert await dispatcher._dispatch_client_command(command, 1) is True
    finally:
        if claim is not None:
            conversation_query_guards().end(claim)
        if transition is not None:
            conversation_query_guards().release_transition(transition)
    assert [event["type"] for event in socket.messages] == ["error", "command.result"]
    error, result = socket.messages
    assert result["level"] == "error" and result["command"] == "context.fork"
    assert result["client_command_id"] == "fork-semantic-command"
    assert result["message"] == error["message"]
    assert result["data"]["error_type"] == "context" and result["data"]["recoverable"] is True
    assert (result["conversation_id"], result["workspace_root"]) == (
        command.data["conversation_id"], command.data["workspace_root"],
    )
    assert len(session.conversation_repo.list_conversations()) == 1
    assert session.conversation_repo.get_conversation(parent.id).to_dict() == original
    assert ForkRegistry(session_id="fork-semantic", root_dir=tmp_path / "forks").list() == []
    if failure not in {"history_missing"}:
        assert session.context_builder.export_snapshot() == source_snapshot
    assert (tmp_path / "workspace" / "source.txt").read_text(encoding="utf-8") == "Original workspace content"
    assert conversation_query_guards().active_claim(parent.id) is None
    await _drain(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_fails", [False, True])
async def test_failed_fork_activation_reports_partial_branch_and_real_rollback(tmp_path, restore_fails):
    session, dispatcher, socket, parent = _dispatching_fork_session(tmp_path)
    original_snapshot = session.context_builder.export_snapshot()

    async def switch(conversation, *, announce):
        session.switched.append(conversation.id)
        if conversation.id != parent.id:
            # Simulate the actual host loading a child before reporting that
            # its workspace could not be activated.
            session.load_active_conversation_snapshot(conversation.id, conversation.context_snapshot, snapshot_revision=conversation.revision)
            raise RuntimeError("Actual child activation failed")
        if restore_fails:
            raise RuntimeError("Actual parent workspace restoration failed")
        return True

    session.switch_workspace_for_conversation = switch
    assert await dispatcher._dispatch_client_command(_fork_command(parent), 1) is True
    forked = next(event for event in socket.messages if event["type"] == "context_forked")
    results = [event for event in socket.messages if event["type"] == "command.result"]
    assert len(results) == 1
    result = results[0]
    assert result["level"] == "error" and result["data"]["reason"] == "activation_failed"
    assert "Actual child activation failed" in result["message"]
    assert result["conversation_id"] == parent.id and result["workspace_root"] == parent.workspace_root
    assert result["data"]["branch_created"] is True and result["data"]["branch_activated"] is False
    assert result["data"]["branch_conversation_id"] == forked["branch_conversation_id"]
    assert forked["conversation_id"] == parent.id
    assert session.switched == [forked["branch_conversation_id"], parent.id]
    assert session.active_conversation_id == parent.id
    assert len(session.conversation_repo.list_conversations()) == 2
    if restore_fails:
        assert forked["activation_recovery_required"] is True
        assert result["data"]["activation_recovery_required"] is True
    else:
        assert session.active_conversation.id == parent.id
        restored_snapshot = session.context_builder.export_snapshot()
        assert restored_snapshot["guardian_evidence"]["review_context_revision"] != original_snapshot["guardian_evidence"]["review_context_revision"]
        restored_snapshot["guardian_evidence"]["review_context_revision"] = original_snapshot["guardian_evidence"]["review_context_revision"]
        assert restored_snapshot == original_snapshot
    assert session.conversation_repo.get_conversation(parent.id).context_snapshot == original_snapshot
    await _drain(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["registry_binding", "inventory_publish"])
async def test_fork_partial_publication_failure_reports_error_and_keeps_created_branch(tmp_path, monkeypatch, failure):
    session, dispatcher, socket, parent = _dispatching_fork_session(tmp_path)
    if failure == "registry_binding":
        def bind_failure(*_args):
            raise OSError("Actual fork registry binding failed")
        monkeypatch.setattr(session.fork_registry, "bind_branch", bind_failure)
    else:
        async def publish_failure():
            raise OSError("Actual conversation list publication failed")
        session.send_conversation_list = publish_failure
    assert await dispatcher._dispatch_client_command(_fork_command(parent), 1) is True
    forked = next(event for event in socket.messages if event["type"] == "context_forked")
    results = [event for event in socket.messages if event["type"] == "command.result"]
    assert len(results) == 1
    result = results[0]
    assert result["level"] == "error" and result["data"]["reason"] == f"{failure}_failed"
    assert result["data"]["branch_created"] is True and result["data"]["branch_activated"] is True
    assert result["data"]["branch_conversation_id"] == forked["branch_conversation_id"]
    assert result["conversation_id"] == parent.id and result["workspace_root"] == parent.workspace_root
    branch = session.conversation_repo.get_conversation(forked["branch_conversation_id"])
    assert branch.parent_conversation_id == parent.id
    assert session.active_conversation_id == branch.id
    assert len(session.conversation_repo.list_conversations()) == 2
    assert session.conversation_repo.get_conversation(parent.id).transcript == parent.transcript
    await _drain(session)
