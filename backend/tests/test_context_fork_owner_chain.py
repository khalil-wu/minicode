import asyncio
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from backend.agent.context import ContextBuilder
from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import AgentSettings, TokenBudget
from backend.conversations.repository import ConversationRepository
from backend.ws.command_dispatcher import SessionCommandDispatcher
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
    session = SimpleNamespace(conversation_repo=repo, active_conversation=visible,
        active_conversation_id=visible.id, context_builder=context, ws_manager=None,
        connection_generation=1, event_outbox=outbox, send_event=send,
        conversation_lifecycle_lock=lambda: lock)
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    dispatcher._user_message_admissions = {}
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
