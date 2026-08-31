import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.conversations.repository import ConversationRepository
from backend.ws.fork_registry import ForkRegistry
from backend.ws.handlers.conversation import (
    handle_context_fork,
    handle_context_ledger,
    handle_context_side_query,
)


def _resource_stores(tmp_path: Path) -> dict[str, object]:
    return {
        "attachment_store": AttachmentStore(tmp_path / "attachments"),
        "artifact_store": ArtifactStore(storage_dir=tmp_path / "artifacts"),
        "diagnostic_store": DiagnosticPayloadStore(),
    }


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_context_fork_materializes_independent_branch(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, SimpleNamespace, ConversationRepository]:
        repo = ConversationRepository(base_dir=tmp_path / "conversations")
        parent = repo.create_conversation(
            conversation_id="conv_parent",
            title="Parent",
            transcript=[
                {"id": "m1", "role": "user", "content": "start"},
                {"id": "m2", "role": "assistant", "content": "answer"},
            ],
            workspace_root="C:/repo",
            worktree_path="C:/repo/.minicode/worktrees/conv_parent",
            git_isolated=True,
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        ctx.append_user("start")
        ctx.append_assistant("answer")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)
        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id="session-1", root_dir=tmp_path / "forks"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path),
        )
        await handle_context_fork(session, {"message_index": 1, "create_branch": True})
        assert events and events[0].type == "context_forked"
        return events[0].data, session, repo

    data, session, repo = asyncio.run(scenario())
    branch_id = str(data["branch_conversation_id"])
    branch = repo.get_conversation(branch_id)
    assert branch is not None
    assert branch.parent_conversation_id == "conv_parent"
    assert branch.branch_kind == "context_fork"
    assert branch.transcript[-1]["content"] == "answer"
    assert branch.context_snapshot["history"]
    assert branch.workspace_root == "C:/repo/.minicode/worktrees/conv_parent"
    assert branch.worktree_path == ""
    assert branch.git_isolated is False


def test_context_fork_resolves_message_id_before_ui_index(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, ConversationRepository]:
        repo = ConversationRepository(base_dir=tmp_path / "conversations")
        parent = repo.create_conversation(
            conversation_id="conv_parent",
            title="Parent",
            transcript=[
                {"id": "user-1", "role": "user", "content": "start"},
                {"id": "assistant-target", "role": "assistant", "content": "answer"},
            ],
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        ctx.append_user("start")
        ctx.append_assistant("answer")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id="session-1", root_dir=tmp_path / "forks"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path),
        )
        # The UI index is intentionally malformed. Stable message_id must win
        # without consulting the compatibility fallback at all.
        await handle_context_fork(
            session,
            {
                "message_id": "assistant-target",
                "message_index": True,
                "create_branch": True,
            },
        )
        assert events and events[0].type == "context_forked"
        return events[0].data, repo

    data, repo = asyncio.run(scenario())
    branch = repo.get_conversation(str(data["branch_conversation_id"]))
    assert branch is not None
    assert data["message_id"] == "assistant-target"
    assert data["message_index"] == 1
    assert data["context_history_index"] == 1
    assert branch.parent_message_index == 1
    assert branch.transcript[-1]["id"] == "assistant-target"


def test_context_fork_rejects_unknown_message_id_without_creating_branch(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[AgentEvent], ConversationRepository]:
        repo = ConversationRepository(base_dir=tmp_path / "conversations")
        parent = repo.create_conversation(
            conversation_id="conv_parent",
            title="Parent",
            transcript=[{"id": "m1", "role": "user", "content": "start"}],
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        ctx.append_user("start")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id="session-1", root_dir=tmp_path / "forks"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path),
        )
        await handle_context_fork(session, {"message_id": "missing", "create_branch": True})
        return events, repo

    events, repo = asyncio.run(scenario())
    assert events[0].type == "error"
    assert events[0].data["recoverable"] is True
    assert [item.id for item in repo.list_conversations()] == ["conv_parent"]


def test_context_fork_maps_recent_message_after_context_prefix_compaction(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, ConversationRepository]:
        repo = ConversationRepository(base_dir=tmp_path / "conversations")
        parent = repo.create_conversation(
            conversation_id="conv_parent",
            title="Parent",
            transcript=[
                {"id": "user-old", "role": "user", "content": "old request"},
                {"id": "assistant-old", "role": "assistant", "content": "old answer"},
                {"id": "user-new", "role": "user", "content": "new request"},
                {"id": "assistant-new", "role": "assistant", "content": "new answer"},
            ],
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        # Simulate compaction: the transcript keeps stable ids while the model
        # history replaces the old prefix with a summary.
        ctx.append_user("Compacted summary of the old turn")
        ctx.append_user("new request")
        ctx.append_assistant("new answer")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id="session-1", root_dir=tmp_path / "forks"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path),
        )
        await handle_context_fork(
            session,
            {"message_id": "assistant-new", "create_branch": True},
        )
        assert events and events[0].type == "context_forked"
        return events[0].data, repo

    data, repo = asyncio.run(scenario())
    branch = repo.get_conversation(str(data["branch_conversation_id"]))
    assert branch is not None
    assert data["message_index"] == 3
    assert data["context_history_index"] == 2
    assert branch.transcript[-1]["id"] == "assistant-new"
    assert len(branch.context_snapshot["history"]) == 3


def test_context_fork_command_reaches_registered_websocket_route(tmp_path: Path) -> None:
    from backend.artifact.store import ArtifactStore
    from backend.permissions.checker import PermissionChecker
    from backend.tools.registry import ToolRegistry
    from backend.ws.handler import WebSocketSession

    async def scenario() -> tuple[list[dict], ConversationRepository, str]:
        websocket = _RecordingWebSocket()
        session = WebSocketSession(
            session_id="session-context-fork-route",
            websocket=websocket,
            llm=object(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        context = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        context.append_user("start")
        context.append_assistant("answer")
        parent = session.conversation_repo.create_conversation(
            conversation_id="conv_route_parent",
            title="Route parent",
            transcript=[
                {"id": "user-start", "role": "user", "content": "start"},
                {"id": "assistant-answer", "role": "assistant", "content": "answer"},
            ],
            context_snapshot=context.export_snapshot(),
        )
        session.active_conversation_id = parent.id
        session.load_active_conversation_snapshot(parent.id, parent.context_snapshot)
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="context.fork",
                data={
                    "message_id": "assistant-answer",
                    "message_index": 0,
                    "create_branch": True,
                },
            )
        )
        return websocket.sent, session.conversation_repo, parent.id

    sent, repo, parent_id = asyncio.run(scenario())
    forked = next(payload for payload in sent if payload.get("type") == "context_forked")
    assert forked["message_id"] == "assistant-answer"
    assert forked["message_index"] == 1
    assert forked["conversation_id"] == parent_id
    assert forked["branch_created"] is True
    assert forked["branch_activated"] is False
    assert "data" not in forked
    branch = repo.get_conversation(str(forked["branch_conversation_id"]))
    assert branch is not None
    assert branch.parent_conversation_id == parent_id


def test_context_fork_owner_tracks_whether_the_new_branch_was_activated(tmp_path: Path) -> None:
    async def run_one(*, activate: bool, suffix: str) -> tuple[dict, SimpleNamespace]:
        repo = ConversationRepository(base_dir=tmp_path / f"conversations-{suffix}")
        parent = repo.create_conversation(
            conversation_id=f"conv_parent_{suffix}",
            title="Parent",
            transcript=[
                {"id": "m1", "role": "user", "content": "start"},
                {"id": "m2", "role": "assistant", "content": "answer"},
            ],
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        ctx.append_user("start")
        ctx.append_assistant("answer")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        async def switch_workspace(_conversation: object, *, announce: bool) -> None:
            assert announce is False

        async def send_conversation_list() -> None:
            return None

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id=f"session-{suffix}", root_dir=tmp_path / f"forks-{suffix}"),
            ws_manager=None,
            send_event=send_event,
            switch_workspace_for_conversation=switch_workspace,
            load_active_conversation_snapshot=lambda *_args: None,
            sync_permission_mode_with_active_conversation=lambda **_kwargs: None,
            send_conversation_list=send_conversation_list,
            **_resource_stores(tmp_path / suffix),
        )
        await handle_context_fork(
            session,
            {"message_id": "m2", "create_branch": True, "activate": activate},
        )
        assert events and events[-1].type == "context_forked"
        return events[-1].data, session

    inactive_data, inactive_session = asyncio.run(run_one(activate=False, suffix="inactive"))
    active_data, active_session = asyncio.run(run_one(activate=True, suffix="active"))

    assert inactive_data["conversation_id"] == "conv_parent_inactive"
    assert inactive_data["branch_created"] is True
    assert inactive_data["branch_activated"] is False
    assert inactive_session.active_conversation_id == "conv_parent_inactive"

    assert active_data["conversation_id"] == active_data["branch_conversation_id"]
    assert active_data["branch_created"] is True
    assert active_data["branch_activated"] is True
    assert active_session.active_conversation_id == active_data["branch_conversation_id"]


def test_context_fork_legacy_index_maps_to_compacted_model_history(tmp_path: Path) -> None:
    async def scenario() -> AgentEvent:
        repo = ConversationRepository(base_dir=tmp_path / "conversations")
        parent = repo.create_conversation(
            conversation_id="conv_parent",
            transcript=[
                {"id": "user-old", "role": "user", "content": "old request"},
                {"id": "assistant-old", "role": "assistant", "content": "old answer"},
                {"id": "user-new", "role": "user", "content": "new request"},
                {"id": "assistant-new", "role": "assistant", "content": "new answer"},
            ],
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        ctx.append_user("Compacted summary of the old turn")
        ctx.append_user("new request")
        ctx.append_assistant("new answer")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id="session-index", root_dir=tmp_path / "forks"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path),
        )
        await handle_context_fork(
            session,
            {"message_index": 3, "create_branch": False},
        )
        return events[-1]

    event = asyncio.run(scenario())
    assert event.type == "context_forked"
    assert event.data["message_index"] == 3
    assert event.data["context_history_index"] == 2
    assert event.data["branch_created"] is False
    assert "branch_conversation_id" not in event.data


def test_context_fork_rejects_invalid_unstable_indices_and_empty_transcript(tmp_path: Path) -> None:
    async def run_one(raw_index: object, *, empty: bool = False) -> list[AgentEvent]:
        suffix = str(len(list(tmp_path.iterdir())))
        repo = ConversationRepository(base_dir=tmp_path / f"conversations-{suffix}")
        transcript = [] if empty else [{"id": "m1", "role": "user", "content": "start"}]
        parent = repo.create_conversation(
            conversation_id=f"conv-{suffix}",
            transcript=transcript,
        )
        ctx = ContextBuilder(token_budget=TokenBudget(), agent_settings=AgentSettings())
        if not empty:
            ctx.append_user("start")
        events: list[AgentEvent] = []

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session = SimpleNamespace(
            context_builder=ctx,
            conversation_repo=repo,
            active_conversation_id=parent.id,
            active_conversation=parent,
            fork_registry=ForkRegistry(session_id=f"session-{suffix}", root_dir=tmp_path / f"forks-{suffix}"),
            ws_manager=None,
            send_event=send_event,
            **_resource_stores(tmp_path / f"stores-{suffix}"),
        )
        await handle_context_fork(
            session,
            {"message_index": raw_index, "create_branch": True},
        )
        return events

    for raw_index in (True, 0.5, "not-an-index", 99):
        events = asyncio.run(run_one(raw_index))
        assert events[-1].type == "error"
        assert events[-1].data["conversation_id"]
        # The renderer's inbound contract requires `recoverable`; without it the
        # rejection was dropped by the validator and the user saw nothing.
        assert events[-1].data["recoverable"] is True

    empty_events = asyncio.run(run_one(-1, empty=True))
    assert empty_events[-1].type == "error"
    assert "visible message" in empty_events[-1].data["message"]
    assert empty_events[-1].data["recoverable"] is True


def test_context_side_query_keeps_owner_captured_before_async_wait() -> None:
    async def scenario() -> tuple[list[AgentEvent], str]:
        started = asyncio.Event()
        release = asyncio.Event()
        events: list[AgentEvent] = []
        session = SimpleNamespace(active_conversation_id="conv-source", agent_state=None)

        class Context:
            async def side_query(self, query: str, *, focus: str, state: object) -> str:
                assert query == "What remains?"
                assert focus == "verification"
                assert state is None
                started.set()
                await release.wait()
                return "Browser verification remains."

        async def send_event(event: AgentEvent) -> None:
            events.append(event)

        session.context_builder = Context()
        session.send_event = send_event
        task = asyncio.create_task(handle_context_side_query(
            session,
            {"query": "  What remains?  ", "focus": " verification "},
        ))
        await started.wait()
        session.active_conversation_id = "conv-opened-later"
        release.set()
        await task
        return events, session.active_conversation_id

    events, current_owner = asyncio.run(scenario())
    assert current_owner == "conv-opened-later"
    assert [event.type for event in events] == ["context_side_query_result"]
    assert events[0].data == {
        "query": "What remains?",
        "result": "Browser verification remains.",
        "focus": "verification",
        "conversation_id": "conv-source",
    }


def test_context_ledger_keeps_original_owner_when_builder_mutates_session() -> None:
    events: list[AgentEvent] = []
    session = SimpleNamespace(active_conversation_id="conv-source")
    ledger = {
        "schema_version": 1,
        "estimated_tokens": 100,
        "actual_tokens": 110,
        "compaction_count": 0,
        "native_attachment_tokens": 0,
        "native_attachment_count": 0,
        "entries": [],
    }

    class Context:
        def context_ledger(self) -> dict:
            session.active_conversation_id = "conv-opened-later"
            return ledger

    async def send_event(event: AgentEvent) -> None:
        events.append(event)

    session.context_builder = Context()
    session.send_event = send_event
    asyncio.run(handle_context_ledger(session, {}))

    assert [event.type for event in events] == ["context_ledger"]
    assert events[0].data == {**ledger, "conversation_id": "conv-source"}
