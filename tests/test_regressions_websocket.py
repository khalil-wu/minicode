import asyncio
import asyncio
import logging
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget, load_config
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.main import app
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.mcp.client import MCPClient
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.manager import WebSocketManager


class _HungLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        await asyncio.sleep(1.0)
        if False:
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return ""


async def _admit_query_submission(submission) -> None:
    context = submission.session.context_builder
    history_start = context.history_length
    context.append_user(submission.user_message)
    await submission.runtime.metadata["commit_turn_admission"](
        boundary_input=SimpleNamespace(consumed_steer=None),
        history_start=history_start,
        history_end=context.history_length,
    )


@pytest.fixture(autouse=True)
def _use_injected_session_llm(monkeypatch) -> None:
    """Keep websocket unit tests isolated from developer provider credentials."""
    monkeypatch.setattr(
        "backend.ws.agent_runner._get_or_create_session_llm",
        lambda session, **_kwargs: session.llm,
    )


@pytest.fixture(autouse=True)
def _use_injected_tool_registry(monkeypatch) -> None:
    """Let a run reuse the registry the test injected into the session.

    A turn builds its own registry generation through the app bootstrap, which
    only exists inside the FastAPI lifespan; these tests construct the session
    directly, so without this the first turn fails admission with "MiniCode
    bootstrap is unavailable" before reaching the behaviour under test.
    """
    monkeypatch.setattr(
        "backend.ws.handler.WebSocketSession._build_conversation_tool_registry",
        lambda self, conversation_id="", **_kwargs: self.tool_registry,
    )


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


class _QueuedWebSocket(_FakeWebSocket):
    def __init__(self, messages: list[dict[str, object]]) -> None:
        super().__init__()
        self._messages = [json.dumps(message) for message in messages]

    async def receive_text(self) -> str:
        from fastapi import WebSocketDisconnect

        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(code=1000)


def test_websocket_reconnect_closes_resources_created_for_the_discarded_connection(
    monkeypatch,
) -> None:
    class _LLM:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class _Artifacts:
        def __init__(self) -> None:
            self.flushed = False
            self.shutdown_called = False
            self.cleared = False

        async def flush(self) -> None:
            self.flushed = True

        def shutdown(self) -> None:
            self.shutdown_called = True

        def clear(self) -> None:
            self.cleared = True

    class _Socket:
        def __init__(self) -> None:
            self.query_params = {"session_id": "session-reconnect"}
            self.closed = False

        async def accept(self, **_kwargs) -> None:
            return None

        async def close(self, **_kwargs) -> None:
            self.closed = True

    class _ExistingSession:
        def __init__(self, live_llm, live_artifacts) -> None:
            self.llm = live_llm
            self.artifact_store = live_artifacts
            self.connection_generation = 4
            self.previous_socket = _Socket()

        def attach_websocket(self, websocket):
            del websocket
            self.connection_generation += 1
            return self.previous_socket, self.connection_generation

    async def scenario():
        manager = WebSocketManager()
        live_llm = _LLM()
        live_artifacts = _Artifacts()
        existing = _ExistingSession(live_llm, live_artifacts)
        manager._sessions["session-reconnect"] = existing
        incoming_llm = _LLM()
        incoming_artifacts = _Artifacts()
        incoming_socket = _Socket()
        session, generation = await manager.connect(
            websocket=incoming_socket,
            llm=incoming_llm,
            artifact_store=incoming_artifacts,
            tool_registry=object(),
            permission_checker=object(),
            config=object(),
        )
        return (
            session,
            generation,
            existing,
            incoming_llm,
            incoming_artifacts,
        )

    monkeypatch.setattr(
        "backend.ws.manager._websocket_accept_subprotocol",
        lambda _websocket: None,
    )
    session, generation, existing, incoming_llm, incoming_artifacts = asyncio.run(scenario())

    assert session is existing
    assert generation == 5
    assert incoming_llm.closed is True
    assert incoming_artifacts.flushed is True
    assert incoming_artifacts.shutdown_called is True
    assert incoming_artifacts.cleared is True
    assert existing.llm.closed is False
    assert existing.artifact_store.flushed is False


class _DisconnectingWebSocket(_FakeWebSocket):
    async def send_json(self, payload: dict[str, object]) -> None:
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect(code=1001)


class _RecordingRunCommandTool(BaseTool):
    name = "run_command"

    def __init__(self) -> None:
        self.executed: list[dict[str, object]] = []

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Run a command",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        )

    def get_schema(self) -> ToolSchema:
        return self.model_schema()

    async def execute(self, args, context=None) -> ToolResult:
        self.executed.append(dict(args))
        return ToolResult(content="Exit code: 0\napproved output")


def _terminal_exec_approval(sent: list[dict[str, object]]) -> dict[str, object] | None:
    for payload in sent:
        if payload.get("type") == "approval_request":
            return payload
        request = payload.get("request")
        if (
            payload.get("type") == "control_request"
            and isinstance(request, dict)
            and request.get("subtype") == "can_use_tool"
            and request.get("tool_name") == "run_command"
        ):
            return payload
    return None


def _terminal_exec_approval_id(payload: dict[str, object]) -> str:
    request = payload.get("request")
    if isinstance(request, dict):
        return str(payload.get("request_id") or request.get("tool_use_id") or "")
    return str(payload.get("tool_call_id") or "")


def test_unbound_desktop_conversation_snapshot_does_not_expose_current_workspace(monkeypatch, tmp_path) -> None:
    from backend.workspace.state import clear_active_workspace_root, set_active_workspace_root

    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    active_workspace = tmp_path / "global-active"
    active_workspace.mkdir()
    set_active_workspace_root(active_workspace)

    try:
        session = WebSocketSession(
            session_id="session-no-folder",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation()
        session.active_conversation_id = conversation.id

        snapshot = session.runtime_snapshot()
    finally:
        clear_active_workspace_root()

    assert snapshot["active_conversation"]["workspace_root"] == ""
    assert snapshot["workspace_root"] is None
    assert snapshot["permission_profile"] == "auto"
    assert snapshot["workspace_scope"] == "computer"
    assert snapshot["sandbox_status"] in (
        {"os": "app_layer", "network": "approval_required"},
        {"os": "enforced", "network": "approval_required"},
    )
    assert session.session_lifecycle.file_watcher is None


def test_terminal_snapshot_request_returns_bounded_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[list[dict[str, object]], str | None]:
        session = WebSocketSession(
            session_id="session-terminal-snapshot",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv_terminal_snapshot_owner"
        )
        session.active_conversation_id = conversation.id
        from backend.terminal.session import TerminalSession

        terminal = TerminalSession(
            "term_fake",
            cwd=str(tmp_path),
            conversation_id=session.active_conversation_id,
        )
        terminal._output_buffer = ["alpha\n", "beta\n", "gamma\n"]
        session.terminal_manager._sessions[terminal.session_id] = terminal
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="terminal.snapshot.request",
                data={"session_id": terminal.session_id, "max_chars": 11},
            )
        )
        return session.ws.sent, session.active_terminal_session_id

    sent, active_terminal_id = asyncio.run(scenario())

    snapshot = next(payload for payload in sent if payload.get("type") == "terminal.snapshot")
    assert snapshot["session_id"]
    assert active_terminal_id == snapshot["session_id"]
    assert snapshot["output"] == "beta\ngamma\n"
    assert snapshot["truncated"] is True


def test_terminal_clear_updates_reconnectable_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-terminal-clear",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv_terminal_clear_owner"
        )
        session.active_conversation_id = conversation.id
        from backend.terminal.session import TerminalSession

        terminal = TerminalSession(
            "term_clear",
            cwd=str(tmp_path),
            conversation_id=conversation.id,
        )
        terminal._output_buffer = ["secret output\n"]
        session.terminal_manager._sessions[terminal.session_id] = terminal
        await session.command_dispatcher._handle_command(UserCommand(
            type="terminal.clear",
            data={
                "session_id": terminal.session_id,
                "conversation_id": conversation.id,
            },
        ))
        return session.ws.sent

    sent = asyncio.run(scenario())
    snapshot = next(payload for payload in sent if payload.get("type") == "terminal.snapshot")
    assert snapshot["session_id"] == "term_clear"
    assert snapshot["output"] == ""
    assert snapshot["total_output_chars"] == 0
    result = next(
        payload
        for payload in sent
        if payload.get("type") == "command.result" and payload.get("command") == "terminal.clear"
    )
    assert result["level"] == "info"


def test_desktop_terminal_mirror_feeds_backend_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[list[dict[str, object]], dict[str, object] | None]:
        session = WebSocketSession(
            session_id="session-terminal-mirror",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv_terminal_mirror_owner"
        )
        session.active_conversation_id = conversation.id
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="terminal.mirror.created",
                data={
                    "conversation_id": session.active_conversation_id,
                    "session_id": "desktop_term_1",
                    "cwd": str(tmp_path),
                    "shell": "pwsh",
                    "pid": 4242,
                },
            )
        )
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="terminal.mirror.output",
                data={
                    "conversation_id": session.active_conversation_id,
                    "session_id": "desktop_term_1",
                    "data": "server starting\nready on 3000\n",
                },
            )
        )
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="terminal.snapshot.request",
                data={"session_id": "desktop_term_1", "max_chars": 14},
            )
        )
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="terminal.mirror.exit",
                data={
                    "conversation_id": session.active_conversation_id,
                    "session_id": "desktop_term_1",
                },
            )
        )
        return session.ws.sent, session.terminal_manager.snapshot(
            "desktop_term_1",
            conversation_id=session.active_conversation_id,
        )

    sent, mirrored = asyncio.run(scenario())

    snapshot = next(payload for payload in sent if payload.get("type") == "terminal.snapshot")
    assert snapshot["session_id"] == "desktop_term_1"
    assert snapshot["pid"] == 4242
    assert snapshot["shell"] == "pwsh"
    assert snapshot["output"] == "ready on 3000\n"
    assert mirrored is not None
    assert mirrored["is_alive"] is False


def test_websocket_session_switch_hydrates_large_snapshot_in_background(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def fake_to_thread(func, *args, **kwargs):
        await asyncio.sleep(0.05)
        return func(*args, **kwargs)

    monkeypatch.setattr("backend.ws.handler.asyncio.to_thread", fake_to_thread)

    large_snapshot = {
        "history": [
            {"role": "user", "content": f"user-{index}"}
            for index in range(25)
        ],
        "persistent_notes": [],
        "compaction_count": 0,
    }

    async def scenario() -> tuple[list[dict[str, object]], WebSocketSession]:
        session = WebSocketSession(
            session_id="session-switch",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        target = session.conversation_repo.create_conversation(context_snapshot=large_snapshot)
        await session.command_dispatcher._handle_command(
            UserCommand(type="conversation.switch", data={"conversation_id": target.id})
        )
        await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        return session.ws.sent, session

    sent, session = asyncio.run(scenario())

    started = next(
        index for index, payload in enumerate(sent)
        if payload.get("type") == "conversation.switched"
        and payload.get("is_hydrating") is True
    )
    completed = next(
        index for index, payload in enumerate(sent)
        if payload.get("type") == "conversation.hydration.updated"
        and payload.get("is_hydrating") is False
    )
    assert started < completed
    assert session.context_builder.history_length == 25


def test_side_chat_create_does_not_steal_active_conversation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-side-chat-create",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        main = session.conversation_repo.create_conversation(conversation_id="conv_main123", title="Main")
        session.active_conversation_id = main.id
        session.load_active_conversation_snapshot(main.id, main.context_snapshot)

        await session.command_dispatcher._handle_command(UserCommand(
            type="conversation.create",
            data={"conversation_id": "side-chat123", "title": "Side", "side_chat": True},
        ))
        return session

    session = asyncio.run(scenario())

    assert session.active_conversation_id == "conv_main123"
    assert session.conversation_repo.get_conversation("side-chat123") is not None


def test_user_message_routes_to_explicit_conversation_without_switching_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    captured: list[tuple[str, str | None]] = []

    async def fake_run_agent(self, user_message, *, attachments=None, conversation_id=None, **_kwargs):
        captured.append((user_message, conversation_id))
        if conversation_id:
            self.conversation_repo.append_transcript_message(
                conversation_id,
                {"id": "user-fake", "role": "user", "content": user_message},
            )
            self.conversation_repo.append_transcript_message(
                conversation_id,
                {"id": "assistant-fake", "role": "assistant", "content": "ok"},
            )

    monkeypatch.setattr(WebSocketSession, "_run_agent", fake_run_agent)

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-explicit-conversation",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        main = session.conversation_repo.create_conversation(conversation_id="conv_main123", title="Main")
        other = session.conversation_repo.create_conversation(conversation_id="conv_other123", title="Other")
        session.active_conversation_id = main.id
        session.load_active_conversation_snapshot(main.id, main.context_snapshot)

        await session.command_dispatcher._handle_command(UserCommand(
            type="user_message",
            data={"conversation_id": other.id, "content": "hello other"},
        ))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return session

    session = asyncio.run(scenario())

    assert captured == [("hello other", "conv_other123")]
    assert session.active_conversation_id == "conv_main123"
    assert [m["content"] for m in session.conversation_repo.get_conversation("conv_other123").transcript] == [
        "hello other",
        "ok",
    ]
    assert session.conversation_repo.get_conversation("conv_main123").transcript == []


def test_active_run_context_is_isolated_from_conversation_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    class CapturingQueryEngine:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.proceed = asyncio.Event()
            self.snapshots: list[dict[str, object]] = []
            self.captured_builder = None

        async def submit(self, submission):
            await _admit_query_submission(submission)
            self.captured_builder = submission.session.context_builder
            self.snapshots.append(submission.session.context_builder.export_snapshot())
            self.started.set()
            await self.proceed.wait()
            self.snapshots.append(submission.session.context_builder.export_snapshot())
            yield AgentEvent.agent_message_completed("done")
            yield AgentEvent.done()

    async def scenario() -> tuple[WebSocketSession, CapturingQueryEngine, str, str]:
        session = WebSocketSession(
            session_id="session-run-context-isolation",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        first = session.conversation_repo.create_conversation(
            conversation_id="conv_first123",
            title="First",
            context_snapshot={"history": [{"role": "user", "content": "first history"}]},
        )
        second = session.conversation_repo.create_conversation(
            conversation_id="conv_second123",
            title="Second",
            context_snapshot={"history": [{"role": "user", "content": "second history"}]},
        )
        session.active_conversation_id = first.id
        session.load_active_conversation_snapshot(first.id, first.context_snapshot)
        engine = CapturingQueryEngine()
        session.query_engine = engine

        run_task = asyncio.create_task(
            session._run_agent(
                "continue",
                conversation_id=first.id,
                metadata={
                    "user_message_id": "user_context_isolation",
                    "assistant_message_id": "assistant_context_isolation",
                },
            )
        )
        await asyncio.wait_for(engine.started.wait(), timeout=2.0)
        await session.command_dispatcher._handle_command(
            UserCommand(type="conversation.switch", data={"conversation_id": second.id})
        )
        engine.proceed.set()
        # Run settlement performs several atomic generation/inventory writes.
        # A 500 ms wall-clock limit flakes under the full Windows regression
        # suite even though the same scenario completes reliably in isolation;
        # keep the wait bounded without turning filesystem scheduling into the
        # behavior under test.
        await asyncio.wait_for(run_task, timeout=2.0)
        return session, engine, first.id, second.id

    session, engine, first_id, second_id = asyncio.run(scenario())

    assert engine.captured_builder is not session.context_builder
    assert session.active_conversation_id == second_id
    after_switch_history = engine.snapshots[-1]["history"]
    after_switch_contents = [message["content"] for message in after_switch_history]
    assert "first history" in after_switch_contents
    assert "second history" not in after_switch_contents

    first_snapshot = session.conversation_repo.get_conversation(first_id).context_snapshot
    first_contents = [message["content"] for message in first_snapshot["history"]]
    assert "first history" in first_contents
    assert "second history" not in first_contents


def test_user_message_for_missing_conversation_reconciles_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    captured: list[str] = []

    async def fake_run_agent(self, user_message, **_kwargs):
        captured.append(user_message)

    monkeypatch.setattr(WebSocketSession, "_run_agent", fake_run_agent)

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-missing-explicit-conversation",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        main = session.conversation_repo.create_conversation(conversation_id="conv_main123", title="Main")
        session.active_conversation_id = main.id

        await session.command_dispatcher._handle_command(UserCommand(
            type="user_message",
            data={"conversation_id": "conv_missing123", "content": "hello missing"},
        ))
        return session

    session = asyncio.run(scenario())

    assert captured == []
    error = session.ws.sent[0]
    assert error["type"] == "error"
    assert error["error_code"] == "conversation.not_found"
    assert error["error_type"] == "conversation"
    assert error["conversation_id"] == "conv_missing123"
    listing = session.ws.sent[1]
    assert listing["type"] == "conversation.list"
    assert listing["active_conversation_id"] == "conv_main123"


def test_agent_run_for_deleted_active_conversation_reports_and_reconciles(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-missing-active-conversation",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.active_conversation_id = "conv_deleted123"

        await session._run_agent("hello")
        return session

    session = asyncio.run(scenario())

    error = session.ws.sent[0]
    assert error["type"] == "error"
    assert error["error_code"] == "conversation.not_found"
    assert error["conversation_id"] == "conv_deleted123"
    listing = session.ws.sent[1]
    assert listing["type"] == "conversation.list"
    assert listing["active_conversation_id"] is None
    assert listing["active_conversation"] is None
    assert listing["conversations"] == []


def test_delete_active_conversation_does_not_start_workspace_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    workspace_root = tmp_path / "slow-workspace"
    workspace_root.mkdir()
    initialize_started = asyncio.Event()
    mcp_started = asyncio.Event()
    release_mcp = asyncio.Event()

    async def slow_initialize(self):
        initialize_started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr("backend.workspace.context.WorkspaceContext.initialize", slow_initialize)
    class FastScheduler:
        async def destroy_for_conversation(self, _conversation_id: str) -> int:
            return 0

    monkeypatch.setattr(
        "backend.tasks.scheduler.get_global_scheduler",
        lambda: FastScheduler(),
    )

    async def scenario() -> tuple[list[dict[str, object]], bool, bool]:
        session = WebSocketSession(
            session_id="session-delete-active-slow-workspace",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        from backend.api import _state

        class SlowWorkspaceBootstrap:
            def __init__(self) -> None:
                self.manager = SimpleNamespace(registry_version=0)

            async def begin_mcp_workspace_activation(self, _workspace_root):
                mcp_started.set()
                await release_mcp.wait()

                async def ready():
                    return self.manager

                return self.manager, asyncio.create_task(ready())

            def create_tool_registry(self, _artifact_store, *, mcp_manager=None):
                return ToolRegistry()

        monkeypatch.setattr(_state, "bootstrap", SlowWorkspaceBootstrap())
        active = session.conversation_repo.create_conversation(
            conversation_id="conv_active_delete",
            title="Delete me",
        )
        fallback = session.conversation_repo.create_conversation(
            conversation_id="conv_fallback_workspace",
            title="Fallback workspace",
            workspace_root=str(workspace_root),
        )
        session.active_conversation_id = active.id

        await asyncio.wait_for(
            session.command_dispatcher._handle_command(
                UserCommand(
                    type="conversation.delete",
                    data={"conversation_id": active.id},
                )
            ),
            timeout=1,
        )
        try:
            await asyncio.wait_for(initialize_started.wait(), timeout=1)
            started = True
        except asyncio.TimeoutError:
            started = False
        try:
            await asyncio.wait_for(mcp_started.wait(), timeout=1)
            mcp_preparation_started = True
        except asyncio.TimeoutError:
            mcp_preparation_started = False

        release_mcp.set()
        mcp_task = session.session_lifecycle.workspace_mcp_task
        if mcp_task is not None and not mcp_task.done():
            await asyncio.wait_for(mcp_task, timeout=1)
        task = session.session_lifecycle.workspace_context_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return session.ws.sent, started, mcp_preparation_started

    sent, started, mcp_preparation_started = asyncio.run(scenario())

    assert started is False
    assert mcp_preparation_started is False
    listing = next(payload for payload in sent if payload.get("type") == "conversation.list")
    assert listing["active_conversation_id"] == "conv_fallback_workspace"
    assert listing["active_conversation"]["id"] == "conv_fallback_workspace"


def test_workspace_capability_probe_failure_publishes_error_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    workspace_root = tmp_path / "probe-failure-workspace"
    workspace_root.mkdir()

    def fail_probe(*args, **kwargs):
        raise RuntimeError("sandbox probe unavailable")

    monkeypatch.setattr(
        "backend.ws.session_lifecycle.sandbox_capability_for_context",
        fail_probe,
    )

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-capability-probe-failure",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv_probe_failure",
            title="Probe failure",
            workspace_root=str(workspace_root),
        )
        session.active_conversation_id = conversation.id

        await session.session_lifecycle.send_runtime_capabilities(source="workspace.activate")
        task = session.session_lifecycle.sandbox_capability_task
        assert task is not None
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return session.ws.sent

    sent = asyncio.run(scenario())
    snapshots = [payload for payload in sent if payload.get("type") == "runtime.capabilities"]
    assert snapshots[0]["source"] == "workspace.activate"
    assert snapshots[-1]["source"] == "sandbox.probe"
    assert snapshots[-1]["capabilities"]["permission"]["sandbox_status"]["probe_status"] == "error"


def test_user_message_workspace_switch_waits_for_workspace_activation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    initialized = asyncio.Event()
    captured: list[str] = []

    async def slow_initialize(self):
        await asyncio.sleep(0.01)
        initialized.set()
        from backend.workspace.context import ProjectMetadata

        self.metadata = ProjectMetadata(
            root_path=self.root_path,
            project_type="python",
            name="workspace",
            file_count=1,
        )
        return self.metadata

    async def fake_run_agent(self, user_message, **_kwargs):
        captured.append(user_message)

    monkeypatch.setattr("backend.workspace.context.WorkspaceContext.initialize", slow_initialize)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    monkeypatch.setattr(WebSocketSession, "_run_agent", fake_run_agent)

    async def scenario() -> tuple[bool, list[str]]:
        session = WebSocketSession(
            session_id="session-user-message-waits-workspace",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv_workspace_message",
            title="Workspace message",
        )
        session.active_conversation_id = conversation.id

        await session.command_dispatcher._handle_command(
            UserCommand(
                type="user_message",
                data={
                    "conversation_id": conversation.id,
                    "content": "inspect the workspace",
                    "workspace_root": str(workspace_root),
                },
            )
        )
        return initialized.is_set(), captured

    did_initialize, messages = asyncio.run(scenario())

    assert did_initialize is True
    assert messages == ["inspect the workspace"]


def test_websocket_session_interrupt_cancels_active_run(monkeypatch, tmp_path) -> None:
    cancelled = asyncio.Event()

    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-test",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        from backend.ws.stream_state import create_stream_state

        async def blocking_run(
            _content: str,
            *,
            attachments: list[dict[str, object]] | None = None,
            conversation_id: str,
            metadata: dict[str, object] | None = None,
            cancel_event: asyncio.Event | None = None,
            run_context=None,
        ) -> None:
            del attachments, cancel_event, run_context
            admission_future = (metadata or {}).get("_turn_admission_future")
            if isinstance(admission_future, asyncio.Future) and not admission_future.done():
                admission_future.set_result(None)
            message_id = str((metadata or {}).get("assistant_message_id") or "assistant-test")
            session._conversation_streams[conversation_id] = create_stream_state(
                conversation_id,
                message_id,
                turn_id="turn-test",
            )
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                done = AgentEvent.done(status="cancelled", reason="user_interrupted")
                done.data.update({
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                })
                await session.send_event(done)
                raise

        session._run_agent = blocking_run

        async def cancel_empty_run_tree(task_id: str, *, reason: str) -> None:
            # This transport test owns no child agents. Keep it focused on the
            # websocket -> canonical run cancellation path; child terminate and
            # bounded-drain behavior is covered by the runtime cleanup suite.
            session.task_manager.cancel(task_id)

        session.run_manager._cancel_run_tree = cancel_empty_run_tree
        run_task = asyncio.create_task(
            session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "stop me"}))
        )
        # Conversation allocation precedes the cancellable turn identity. Wait
        # for the reconnectable stream record itself instead of sleeping for an
        # assumed amount of setup time.
        async with asyncio.timeout(30):
            while True:
                conversation_id = str(session.active_conversation_id or "")
                if conversation_id and conversation_id in session._conversation_streams:
                    break
                await asyncio.sleep(0)
        agent_task = session.run_manager.active_run_task
        stream_state = session._conversation_streams[conversation_id]
        await session.command_dispatcher._handle_command(UserCommand(type="interrupt", data={
            "conversation_id": conversation_id,
            "turn_id": stream_state["turn_id"],
            "message_id": stream_state["message_id"],
        }))
        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
        if agent_task:
            assert agent_task.done()
            assert agent_task.cancelled()
        await asyncio.wait_for(run_task, timeout=0.2)
        sent = list(session.ws.sent)
        await session.session_lifecycle.shutdown(reason="test_cleanup")
        return sent

    sent = asyncio.run(scenario())

    done = [
        payload
        for payload in sent
        if payload.get("type") == "done" and payload.get("status") == "cancelled"
    ]
    assert len(done) == 1


def test_websocket_session_rejects_new_user_message_while_run_is_active(monkeypatch, tmp_path) -> None:
    started = asyncio.Event()

    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    class BlockingQueryEngine:
        async def submit(self, submission):
            await _admit_query_submission(submission)
            started.set()
            await asyncio.sleep(10)
            if False:
                yield AgentEvent.done()

    async def scenario() -> tuple[list[dict[str, object]], str, str]:
        session = WebSocketSession(
            session_id="session-run-guard",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.query_engine = BlockingQueryEngine()
        await session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "first"}))
        await asyncio.wait_for(started.wait(), timeout=0.5)

        await session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "second"}))

        # The queued second turn is allowed to start after the first run is
        # cancelled. Drain the whole session ownership tree before closing the
        # test loop so the follow-up run cannot leak into GeneratorExit.
        await session.session_lifecycle.shutdown(reason="test_cleanup")
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert any(
        payload.get("type") == "user_message.queue.updated"
        and payload.get("status") == "queued"
        for payload in sent
    )


def test_websocket_session_shutdown_drains_agent_and_cleanup_tasks(monkeypatch, tmp_path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    class BlockingQueryEngine:
        async def submit(self, submission):
            await _admit_query_submission(submission)
            started.set()
            try:
                await asyncio.sleep(10)
                if False:
                    yield AgentEvent.done()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario() -> tuple[WebSocketSession, asyncio.Task | None]:
        session = WebSocketSession(
            session_id="session-shutdown-drain",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.query_engine = BlockingQueryEngine()
        await session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "keep working"}))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        run_task = session.run_manager.active_run_task

        await session.session_lifecycle.shutdown(reason="test_shutdown")
        await asyncio.sleep(0)
        return session, run_task

    session, run_task = asyncio.run(scenario())

    assert cancelled.is_set()
    assert run_task is not None and run_task.done()
    assert session.run_manager.run_tasks == {}
    assert session.command_dispatcher.command_tasks == set()
    assert session.task_manager.summary()["running"] == 0


def test_terminal_exec_confirm_mode_runs_after_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    run_command = _RecordingRunCommandTool()
    registry = ToolRegistry()
    registry.register(run_command)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-terminal-approval",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.session_lifecycle.current_workspace_root = lambda: tmp_path  # type: ignore[method-assign]
        session.resolve_workspace_cwd = lambda _cwd=None: tmp_path  # type: ignore[method-assign]
        conversation = session.conversation_repo.create_conversation(conversation_id="conv_terminal_confirm")
        session.active_conversation_id = conversation.id
        session.permission_context = session.permission_checker.build_context(mode="confirm", source="test")

        exec_task = asyncio.create_task(
            session.command_dispatcher._handle_command(
                UserCommand(type="terminal.exec", data={"command": "npm test", "cwd": str(tmp_path)})
            )
        )
        for _ in range(20):
            if _terminal_exec_approval(session.ws.sent) is not None:
                break
            await asyncio.sleep(0)
        approval = _terminal_exec_approval(session.ws.sent)
        assert approval is not None
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="control_response",
                data={
                    "request_id": _terminal_exec_approval_id(approval),
                    "conversation_id": approval["conversation_id"],
                    "response": {
                        "subtype": "success",
                        "response": {
                            "action": "approve",
                            "request_digest": (
                                approval.get("request_digest")
                                or (approval.get("request") or {}).get("request_digest")
                            ),
                        },
                    },
                },
            )
        )
        await asyncio.wait_for(exec_task, timeout=1)
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert run_command.executed and run_command.executed[0]["command"] == "npm test"
    approval = _terminal_exec_approval(sent)
    assert approval is not None
    request = approval.get("request")
    assert (
        request.get("tool_name") if isinstance(request, dict) else approval.get("tool_name")
    ) == "run_command"
    assert approval["conversation_id"] == "conv_terminal_confirm"
    assert approval.get("request_digest") or (
        request.get("request_digest") if isinstance(request, dict) else ""
    )
    output = next(payload for payload in sent if payload.get("type") == "terminal.output")
    assert output["exit_code"] == 0
    assert "approved output" in str(output["output"])


def test_terminal_exec_confirm_mode_does_not_run_when_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    run_command = _RecordingRunCommandTool()
    registry = ToolRegistry()
    registry.register(run_command)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-terminal-reject",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.session_lifecycle.current_workspace_root = lambda: tmp_path  # type: ignore[method-assign]
        session.resolve_workspace_cwd = lambda _cwd=None: tmp_path  # type: ignore[method-assign]
        conversation = session.conversation_repo.create_conversation(conversation_id="conv_terminal_reject")
        session.active_conversation_id = conversation.id
        session.permission_context = session.permission_checker.build_context(mode="confirm", source="test")

        exec_task = asyncio.create_task(
            session.command_dispatcher._handle_command(
                UserCommand(type="terminal.exec", data={"command": "npm test", "cwd": str(tmp_path)})
            )
        )
        for _ in range(20):
            if _terminal_exec_approval(session.ws.sent) is not None:
                break
            await asyncio.sleep(0)
        approval = _terminal_exec_approval(session.ws.sent)
        assert approval is not None
        await session.command_dispatcher._handle_command(
            UserCommand(
                type="control_response",
                data={
                    "request_id": _terminal_exec_approval_id(approval),
                    "conversation_id": approval["conversation_id"],
                    "response": {
                        "subtype": "success",
                        "response": {
                            "action": "reject",
                            "guidance": "not now",
                        },
                    },
                },
            )
        )
        await asyncio.wait_for(exec_task, timeout=1)
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert run_command.executed == []
    output = next(payload for payload in sent if payload.get("type") == "terminal.output")
    assert output["exit_code"] == -1
    assert "not now" in str(output["output"])


def test_websocket_query_submission_passes_background_manager(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    class CapturingQueryEngine:
        async def submit(self, submission):
            await _admit_query_submission(submission)
            captured["session_context"] = submission.runtime
            yield AgentEvent.done()

    async def scenario() -> None:
        session = WebSocketSession(
            session_id="session-bg-manager",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.query_engine = CapturingQueryEngine()
        await session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "start server"}))
        if session.run_manager.active_run_task:
            await session.run_manager.active_run_task

    asyncio.run(scenario())

    session_context = captured["session_context"]
    assert getattr(session_context, "background_manager") is not None
    assert getattr(session_context, "task_manager") is not None


def test_first_user_message_creates_unbound_conversation(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    async def fake_run_agent_loop(*args, **kwargs):
        captured["session_context"] = kwargs.get("session_context")
        yield AgentEvent.agent_message_completed("hello")
        yield AgentEvent.done()

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-first-global-message",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await session.command_dispatcher._handle_command(UserCommand(type="user_message", data={"content": "hello"}))
        task = session.run_manager.active_run_task
        if task is not None:
            await task
        return session

    session = asyncio.run(scenario())

    conversation_id = session.active_conversation_id
    assert conversation_id is not None
    conversation = session.conversation_repo.get_conversation(conversation_id)
    assert conversation is not None
    assert conversation.workspace_root == ""
    assert getattr(captured["session_context"], "workspace_root") is None
    assert any(payload.get("type") == "item.completed" and payload.get("conversation_id") == conversation_id for payload in session.ws.sent)


def test_websocket_session_sends_adjacent_agent_message_deltas_without_transport_batching(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[list[dict[str, object]], str]:
        session = WebSocketSession(
            session_id="session-batch",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(conversation_id="conv_batch123")
        for delta in ("hello", " ", "world"):
            event = AgentEvent.agent_message_delta(delta)
            event.data["conversation_id"] = conversation.id
            await session.send_event(event)
        done = AgentEvent.done()
        done.data["conversation_id"] = conversation.id
        await session.send_event(done)
        return session.ws.sent, conversation.id

    sent, conversation_id = asyncio.run(scenario())

    assert [item["type"] for item in sent[:4]] == ["agent_message.delta", "agent_message.delta", "agent_message.delta", "done"]
    assert [item.get("delta") for item in sent[:3]] == ["hello", " ", "world"]
    assert [item.get("conversation_id") for item in sent[:4]] == [conversation_id] * 4


def test_websocket_session_drops_scoped_events_without_conversation_id(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-missing-conversation-id",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conv_a = session.conversation_repo.create_conversation()
        conv_b = session.conversation_repo.create_conversation()
        session.active_conversation_id = conv_a.id
        # Legacy field removed: only per-conversation stream state matters now

        await session.send_event(AgentEvent.agent_message_completed("hello"))
        session.active_conversation_id = conv_b.id
        await session.send_event(AgentEvent.done())
        await session.send_event(AgentEvent.stream_event(
            provider="openai",
            event_type="response.output_text.delta",
            data={"delta": "hello"},
        ))
        await session.send_event(AgentEvent.rate_limit())
        await session.send_event(AgentEvent.session_state_changed(state="working"))
        await session.send_event(AgentEvent(
            type="conversation.compaction.updated",
            data={"state": "compacted", "summary": "missing owner"},
        ))
        await session.send_event(AgentEvent(
            type="conversation.summary.updated",
            data={
                "summary": "missing owner",
                "title": "Missing owner",
                "updated_at": "2026-08-15T10:00:00Z",
                "memory_mode": "enabled",
                "memory_polluted": False,
                "memory_pollution_sources": [],
            },
        ))
        await session.send_event(AgentEvent(
            type="background.stalled",
            data={"command_id": "bg-1", "tail": "Continue?", "advice": "Answer the prompt."},
        ))
        await session.send_event(AgentEvent(
            type="context_forked",
            data={
                "fork_id": "fork-1",
                "message_index": 0,
                "context_history_index": 0,
                "history_length": 1,
                "estimated_tokens": 10,
                "parent_conversation_id": conv_a.id,
                "branch_created": False,
                "branch_activated": False,
            },
        ))
        await session.send_event(AgentEvent(
            type="context_ledger",
            data={
                "schema_version": 1,
                "estimated_tokens": 10,
                "actual_tokens": 10,
                "compaction_count": 0,
                "native_attachment_tokens": 0,
                "native_attachment_count": 0,
                "entries": [],
            },
        ))
        await session.send_event(AgentEvent(
            type="context_side_query_result",
            data={"query": "What remains?", "result": "Tests", "focus": "audit"},
        ))
        await session.send_event(AgentEvent(
            type="approval_request",
            data={"tool_call_id": "approval-1", "tool_name": "write_file", "args": {}},
        ))
        await session.send_event(AgentEvent(
            type="ask_user",
            data={"tool_call_id": "ask-1", "question": "Continue?"},
        ))
        await session.send_event(AgentEvent(
            type="control_request",
            data={
                "request_id": "control-1",
                "request": {
                    "subtype": "provider_auth_prompt",
                    "provider": "example",
                    "prompt": "Enter code",
                },
            },
        ))
        return session.ws.sent

    with caplog.at_level(logging.WARNING):
        sent = asyncio.run(scenario())

    assert sent == []
    assert "Dropping conversation-scoped event without conversation_id" in caplog.text


def test_rate_limit_event_preserves_explicit_owner_and_retry_contract() -> None:
    event = AgentEvent.rate_limit(
        provider="openai",
        error_type="rate_limit",
        retry_after_seconds=2.5,
        message="Retry after the provider window resets.",
        recoverable=True,
        conversation_id="conv-rate-limit",
    )

    payload = event.to_ws_message()
    assert payload["type"] == "rate_limit"
    assert payload["conversation_id"] == "conv-rate-limit"
    assert payload["provider"] == "openai"
    assert payload["error_type"] == "rate_limit"
    assert payload["retry_after_seconds"] == 2.5
    assert isinstance(payload["retry_at"], int)
    assert payload["recoverable"] is True


def test_websocket_session_keeps_explicit_event_conversation_when_active_switches(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[list[dict[str, object]], str, str]:
        session = WebSocketSession(
            session_id="session-run-conversation",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conv_a = session.conversation_repo.create_conversation()
        conv_b = session.conversation_repo.create_conversation()
        session.active_conversation_id = conv_a.id

        completed = AgentEvent.agent_message_completed("hello")
        completed.data["conversation_id"] = conv_a.id
        await session.send_event(completed)
        session.active_conversation_id = conv_b.id
        done = AgentEvent.done()
        done.data["conversation_id"] = conv_a.id
        await session.send_event(done)
        return session.ws.sent, conv_a.id, conv_b.id

    sent, conv_a_id, conv_b_id = asyncio.run(scenario())

    assert sent[0]["type"] == "item.completed"
    assert sent[0]["item"]["text"] == "hello"
    assert sent[0]["conversation_id"] == conv_a_id
    assert sent[1]["conversation_id"] == conv_a_id
    assert sent[1]["conversation_id"] != conv_b_id


def test_websocket_session_caches_agent_message_deltas_for_stream_resume(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[WebSocketSession, str]:
        session = WebSocketSession(
            session_id="session-final-answer-cache",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(conversation_id="conv_finalcache")
        session.active_conversation_id = conversation.id
        # Legacy fields removed: only _conversation_streams is authoritative
        session._conversation_streams[conversation.id] = {
            "conversation_id": conversation.id,
            "message_id": "assistant-final",
            "content_blocks": [],
            "tool_calls": {},
        }

        for delta in ("北京今天", "晴。"):
            event = AgentEvent.agent_message_delta(delta)
            event.data["conversation_id"] = conversation.id
            await session.send_event(event)
        accumulated = session._conversation_streams[conversation.id]["content_blocks"][-1]["content"]
        return session, accumulated

    session, accumulated = asyncio.run(scenario())

    assert accumulated == "北京今天晴。"
    # Legacy field removed: only check per-conversation stream state
    assert session._conversation_streams["conv_finalcache"]["content_blocks"][-1]["content"] == "北京今天晴。"


def test_websocket_reemit_stream_resume_includes_pending_tool_calls(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-reemit-tools",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation()
        session.active_conversation_id = conversation.id
        # Legacy fields removed: use _conversation_streams instead
        session._conversation_streams[conversation.id] = {
            "conversation_id": conversation.id,
            "message_id": "assistant_reconnect",
                "content_blocks": [{
                    "type": "text",
                    "itemId": "agent-message",
                    "content": "partial final",
                    "status": "in_progress",
                    "isStreaming": True,
                }],
            "tool_calls": {
                "tc-1": {
                    "id": "tc-1",
                    "name": "read_file",
                    "args": {"path": "backend/ws/handler.py"},
                    "status": "running",
                    "started_at": 123,
                    "display_hint": "Reading file",
                    "input_summary": "backend/ws/handler.py",
                    "iteration_id": "iter-1",
                    "phase": "tool",
                }
            },
        }
        task = asyncio.create_task(asyncio.sleep(10))
        session.run_manager.run_tasks[conversation.id] = task
        try:
            await session.reemit_pending_state()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert sent[-1]["type"] == "stream_resume"
    assert sent[-1]["conversation_id"]
    assert sent[-1]["content_blocks"][-1]["content"] == "partial final"
    assert [block["type"] for block in sent[-1]["content_blocks"]] == ["text"]
    assert sent[-1]["tool_states"] == sent[-1]["tool_calls_pending"]
    assert sent[-1]["tool_calls_pending"] == [
        {
            "id": "tc-1",
            "name": "read_file",
            "args": {"path": "backend/ws/handler.py"},
            "status": "running",
            "started_at": 123,
            "display_hint": "Reading file",
            "input_summary": "backend/ws/handler.py",
            "iteration_id": "iter-1",
            "phase": "tool",
        }
    ]


def test_websocket_reemit_stream_resume_skips_missing_stream_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-reemit-missing-stream",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(conversation_id="conv_missing_stream")
        session.active_conversation_id = conversation.id
        pending_task = asyncio.create_task(asyncio.sleep(10))
        session.run_manager.run_tasks[conversation.id] = pending_task
        try:
            await session.reemit_pending_state(conversation_id=conversation.id)
        finally:
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert all(item.get("type") != "stream_resume" for item in sent)


def test_websocket_stream_resume_is_scoped_per_running_conversation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-reemit-parallel",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conv_a = session.conversation_repo.create_conversation(conversation_id="conv_streama", title="A")
        conv_b = session.conversation_repo.create_conversation(conversation_id="conv_streamb", title="B")
        session.active_conversation_id = conv_a.id
        task_a = asyncio.create_task(asyncio.sleep(10))
        task_b = asyncio.create_task(asyncio.sleep(10))
        session.run_manager.run_tasks.update({conv_a.id: task_a, conv_b.id: task_b})
        session._conversation_streams = {
            conv_a.id: {
                "conversation_id": conv_a.id,
                "message_id": "assistant-a",
                "content_blocks": [{"type": "text", "itemId": "agent-message", "content": "partial A", "status": "in_progress", "isStreaming": True}],
                "tool_calls": {
                    "tc-a": {
                        "id": "tc-a",
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "status": "running",
                    }
                },
            },
            conv_b.id: {
                "conversation_id": conv_b.id,
                "message_id": "assistant-b",
                "content_blocks": [{"type": "text", "itemId": "agent-message", "content": "partial B", "status": "in_progress", "isStreaming": True}],
                "tool_calls": {},
            },
        }
        try:
            await session.reemit_pending_state()
        finally:
            task_a.cancel()
            task_b.cancel()
            for task in (task_a, task_b):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        return session.ws.sent

    sent = asyncio.run(scenario())
    resumes = [item for item in sent if item["type"] == "stream_resume"]

    assert [item["conversation_id"] for item in resumes] == ["conv_streama", "conv_streamb"]
    assert [item["content_blocks"][-1]["content"] for item in resumes] == ["partial A", "partial B"]
    assert resumes[0]["tool_calls_pending"][0]["id"] == "tc-a"
    assert resumes[1]["tool_calls_pending"] == []


def test_websocket_streaming_text_cache_is_scoped_by_conversation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> WebSocketSession:
        session = WebSocketSession(
            session_id="session-stream-cache-parallel",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conv_a = session.conversation_repo.create_conversation(conversation_id="conv_cachea", title="A")
        conv_b = session.conversation_repo.create_conversation(conversation_id="conv_cacheb", title="B")
        session._conversation_streams = {
            conv_a.id: {"conversation_id": conv_a.id, "message_id": "assistant-a", "content_blocks": [], "tool_calls": {}},
            conv_b.id: {"conversation_id": conv_b.id, "message_id": "assistant-b", "content_blocks": [], "tool_calls": {}},
        }

        for conversation_id, delta in ((conv_a.id, "A1"), (conv_b.id, "B1"), (conv_a.id, "A2")):
            event = AgentEvent.agent_message_delta(delta)
            event.data["conversation_id"] = conversation_id
            await session.send_event(event)
        return session

    session = asyncio.run(scenario())

    assert session._conversation_streams["conv_cachea"]["content_blocks"][-1]["content"] == "A1A2"
    assert session._conversation_streams["conv_cacheb"]["content_blocks"][-1]["content"] == "B1"
    assert [item["conversation_id"] for item in session.ws.sent] == ["conv_cachea", "conv_cacheb", "conv_cachea"]


def test_websocket_transport_does_not_batch_or_buffer_terminal_events() -> None:
    source = Path("backend/ws/handler.py").read_text(encoding="utf-8")

    assert "TEXT_CHUNK_BATCH_WINDOW_SECONDS" not in source
    assert "_buffered_terminal_events" not in source
    assert "_flush_pending_agent_message_deltas" not in source


def test_websocket_session_swallows_expected_disconnect_during_event_send(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.start_file_watcher", lambda self: None)

    async def scenario() -> None:
        session = WebSocketSession(
            session_id="session-disconnect",
            websocket=_DisconnectingWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await session.send_event(AgentEvent.done())

    asyncio.run(scenario())


def test_websocket_session_expected_disconnect_is_not_logged_as_unhandled_command_error(
    caplog,
) -> None:
    class _CompletedTask:
        def cancelled(self) -> bool:
            return False

        def exception(self):
            from fastapi import WebSocketDisconnect

            return WebSocketDisconnect(code=1001)

    with caplog.at_level(logging.DEBUG):
        SessionCommandDispatcher._on_command_task_done(_CompletedTask())  # type: ignore[arg-type]

    assert "Unhandled error in _handle_command" not in caplog.text


def test_websocket_session_rejects_commands_when_command_task_backlog_is_full(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")

    async def scenario() -> tuple[list[dict[str, object]], list[str]]:
        websocket = _QueuedWebSocket([
            {"type": "session.sync", "client_command_id": "cmd-1"},
            {"type": "session.sync", "client_command_id": "cmd-2"},
            {"type": "session.sync", "client_command_id": "cmd-3"},
        ])
        session = WebSocketSession(
            session_id="session-command-backlog",
            websocket=websocket,
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        session.command_dispatcher.max_command_tasks = 2
        started: list[str] = []
        release = asyncio.Event()

        async def blocking_handle(command: UserCommand, **_kwargs) -> None:
            started.append(str(command.data.get("client_command_id")))
            await release.wait()

        session.command_dispatcher._handle_command = blocking_handle  # type: ignore[method-assign]
        await session.session_lifecycle.handle()
        await asyncio.sleep(0)
        release.set()
        for task in list(session.command_dispatcher.command_tasks):
            await task
        return websocket.sent, started

    sent, started = asyncio.run(scenario())

    assert started == ["cmd-1", "cmd-2"]
    assert [payload.get("client_command_id") for payload in sent if payload.get("type") == "client.command.ack"] == [
        "cmd-1",
        "cmd-2",
        "cmd-3",
    ]
    overload_errors = [
        payload for payload in sent
        if payload.get("type") == "error" and payload.get("error_type") == "rate_limit"
    ]
    assert len(overload_errors) == 1
    assert overload_errors[0].get("error_code") == "command.backlog"
    assert "Too many pending commands" in str(overload_errors[0].get("message"))


def test_durable_client_command_persistence_failure_is_rejected_before_ack(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> list[dict[str, object]]:
        websocket = _QueuedWebSocket([
            {"type": "session.sync", "client_command_id": "cmd-persist-fail"},
        ])
        session = WebSocketSession(
            session_id="session-command-persist-fail",
            websocket=websocket,
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        durable_queue = session.run_manager.durable_queue
        assert durable_queue is not None

        def fail_persist(_command: UserCommand) -> bool:
            raise OSError("disk unavailable")

        monkeypatch.setattr(durable_queue, "persist_client_command", fail_persist)
        await session.session_lifecycle.handle()
        return websocket.sent

    sent = asyncio.run(scenario())
    acknowledgements = [item for item in sent if item.get("type") == "client.command.ack"]

    assert acknowledgements == [{
        "type": "client.command.ack",
        "client_command_id": "cmd-persist-fail",
        "command_type": "session.sync",
        "accepted": False,
        "reason": "command.persistence",
    }]


def test_durable_client_command_replays_after_ack_before_task_creation_crash(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> tuple[list[dict[str, object]], list[str], list[dict[str, object]]]:
        first_socket = _QueuedWebSocket([
            {"type": "session.sync", "client_command_id": "cmd-crash-window"},
        ])
        first = WebSocketSession(
            session_id="session-command-crash-window",
            websocket=first_socket,
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        first.command_dispatcher._schedule_durable_client_command = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        await first.session_lifecycle.handle()
        first_queue = first.run_manager.durable_queue
        assert first_queue is not None
        assert [
            command.data.get("client_command_id")
            for command in first_queue.pending_client_commands()
        ] == ["cmd-crash-window"]
        assert "cmd-crash-window" not in first.command_dispatcher.recent_client_command_id_set

        second_socket = _FakeWebSocket()
        second = WebSocketSession(
            session_id="session-command-crash-window",
            websocket=second_socket,
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        executed: list[str] = []

        async def record_handle(command: UserCommand, **_kwargs) -> None:
            executed.append(str(command.data.get("client_command_id") or ""))

        second.command_dispatcher._handle_command = record_handle  # type: ignore[method-assign]
        await second.command_dispatcher._replay_pending_client_commands(second.connection_generation)
        tasks = list(second.command_dispatcher.command_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return first_socket.sent, executed, second_socket.sent

    first_sent, executed, replay_sent = asyncio.run(scenario())

    accepted = next(item for item in first_sent if item.get("type") == "client.command.ack")
    assert accepted.get("accepted", True) is True
    assert executed == ["cmd-crash-window"]
    replay_ack = next(item for item in replay_sent if item.get("type") == "client.command.ack")
    assert replay_ack["client_command_id"] == "cmd-crash-window"
    assert replay_ack["duplicate"] is True


def test_durable_client_inflight_replays_only_after_owner_lease_releases(tmp_path) -> None:
    from backend.ws.durable_user_queue import DurableUserMessageQueue

    first = DurableUserMessageQueue(session_id="session-inflight-reload", root_dir=tmp_path)
    command = UserCommand(
        type="session.sync",
        data={"client_command_id": "cmd-inflight-reload"},
    )
    assert first.persist_client_command(command) is True
    assert first.claim_client_command("cmd-inflight-reload") is not None

    second = DurableUserMessageQueue(session_id="session-inflight-reload", root_dir=tmp_path)
    second.load()

    assert second.pending_client_commands() == []
    assert second.claim_client_command("cmd-inflight-reload") is None
    payload = json.loads(second.path.read_text(encoding="utf-8"))
    assert payload["client_inflight"]["cmd-inflight-reload"]["data"][
        "client_command_id"
    ] == "cmd-inflight-reload"
    assert payload["ownership"]["client_inflight"]["cmd-inflight-reload"] == first.owner_id

    first.close()

    assert [
        item.data.get("client_command_id")
        for item in second.pending_client_commands()
    ] == ["cmd-inflight-reload"]
    payload = json.loads(second.path.read_text(encoding="utf-8"))
    assert payload["client_inflight"] == {}


def test_two_live_owners_cannot_claim_same_durable_client_command(tmp_path) -> None:
    from backend.ws.durable_user_queue import DurableUserMessageQueue

    first = DurableUserMessageQueue(session_id="session-client-owner", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="session-client-owner", root_dir=tmp_path)
    command = UserCommand(
        type="session.sync",
        data={"client_command_id": "cmd-owner-once"},
    )
    assert first.persist_client_command(command) is True

    assert first.claim_client_command("cmd-owner-once") is not None
    assert second.claim_client_command("cmd-owner-once") is None
    assert second.complete_client_command("cmd-owner-once") is False
    assert first.complete_client_command("cmd-owner-once") is True
    assert second.claim_client_command("cmd-owner-once") is None


def test_durable_client_command_records_recent_id_before_completion(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> tuple[bool, list[str], list[str]]:
        session = WebSocketSession(
            session_id="session-command-completion",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        durable_queue = session.run_manager.durable_queue
        assert durable_queue is not None
        durable_queue.persist_client_command(UserCommand(
            type="session.sync",
            data={"client_command_id": "cmd-completion"},
        ))
        completion_log: list[str] = []
        original_complete = durable_queue.complete_client_command

        def observe_completion(command_id: str) -> bool:
            completion_log.extend(
                session.command_dispatcher.client_command_store.load_ids(limit=10)
            )
            return original_complete(command_id)

        durable_queue.complete_client_command = observe_completion  # type: ignore[method-assign]
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_handle(_command: UserCommand, **_kwargs) -> None:
            started.set()
            await release.wait()

        session.command_dispatcher._handle_command = blocking_handle  # type: ignore[method-assign]
        task = asyncio.create_task(
            session.command_dispatcher._run_durable_client_command(
                "cmd-completion",
                session.connection_generation,
            )
        )
        await started.wait()
        inflight_before_completion = "cmd-completion" in durable_queue._client_inflight
        release.set()
        await task
        return (
            inflight_before_completion,
            completion_log,
            session.command_dispatcher.client_command_store.load_ids(limit=10),
        )

    inflight_before_completion, completion_log, recent_ids = asyncio.run(scenario())

    assert inflight_before_completion is True
    assert completion_log == ["cmd-completion"]
    assert recent_ids == ["cmd-completion"]


def test_durable_client_command_stays_pending_when_dedup_persistence_fails(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> tuple[list[str], bool]:
        session = WebSocketSession(
            session_id="session-command-dedup-failure",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        durable_queue = session.run_manager.durable_queue
        assert durable_queue is not None
        command_id = "cmd-dedup-failure"
        durable_queue.persist_client_command(UserCommand(
            type="session.sync",
            data={"client_command_id": command_id},
        ))

        async def succeed(_command: UserCommand, **_kwargs) -> None:
            return None

        session.command_dispatcher._handle_command = succeed  # type: ignore[method-assign]

        def fail_append(*_args, **_kwargs) -> None:
            raise OSError("dedup log unavailable")

        monkeypatch.setattr(session.command_dispatcher.client_command_store, "append", fail_append)
        with pytest.raises(OSError, match="dedup log unavailable"):
            await session.command_dispatcher._run_durable_client_command(
                command_id,
                session.connection_generation,
            )
        return (
            [str(item.data.get("client_command_id") or "") for item in durable_queue.pending_client_commands()],
            command_id in session.command_dispatcher.recent_client_command_id_set,
        )

    pending_ids, marked_seen = asyncio.run(scenario())
    assert pending_ids == ["cmd-dedup-failure"]
    assert marked_seen is False


def test_durable_client_handler_failure_keeps_command_pending(monkeypatch, tmp_path) -> None:
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> list[str]:
        session = WebSocketSession(
            session_id="session-command-handler-failure",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        durable_queue = session.run_manager.durable_queue
        assert durable_queue is not None
        command_id = "cmd-handler-failure"
        durable_queue.persist_client_command(UserCommand(
            type="session.sync",
            data={"client_command_id": command_id},
        ))

        async def fail_inner(_command: UserCommand) -> None:
            raise RuntimeError("handler failed")

        session.command_dispatcher._handle_command_inner = fail_inner  # type: ignore[method-assign]
        await session.command_dispatcher._run_durable_client_command(
            command_id,
            session.connection_generation,
        )
        return [
            str(item.data.get("client_command_id") or "")
            for item in durable_queue.pending_client_commands()
        ]

    assert asyncio.run(scenario()) == ["cmd-handler-failure"]


@pytest.mark.parametrize("failure_mode", ["cancelled", "failed"])
def test_interrupted_durable_client_command_returns_to_pending(
    monkeypatch,
    tmp_path,
    failure_mode: str,
) -> None:
    conversation_dir = tmp_path / failure_mode / "conversations"
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir)
    monkeypatch.setattr("backend.ws.run_manager.CONVERSATION_DATA_DIR", conversation_dir)

    async def scenario() -> list[str]:
        session = WebSocketSession(
            session_id=f"session-command-{failure_mode}",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        durable_queue = session.run_manager.durable_queue
        assert durable_queue is not None
        client_command_id = f"cmd-{failure_mode}"
        durable_queue.persist_client_command(UserCommand(
            type="session.sync",
            data={"client_command_id": client_command_id},
        ))

        if failure_mode == "failed":
            async def fail_handle(_command: UserCommand, **_kwargs) -> None:
                raise RuntimeError("handler failed")

            session.command_dispatcher._handle_command = fail_handle  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="handler failed"):
                await session.command_dispatcher._run_durable_client_command(
                    client_command_id,
                    session.connection_generation,
                )
        else:
            started = asyncio.Event()

            async def wait_handle(_command: UserCommand, **_kwargs) -> None:
                started.set()
                await asyncio.Event().wait()

            session.command_dispatcher._handle_command = wait_handle  # type: ignore[method-assign]
            task = asyncio.create_task(session.command_dispatcher._run_durable_client_command(
                client_command_id,
                session.connection_generation,
            ))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        return [
            str(command.data.get("client_command_id") or "")
            for command in durable_queue.pending_client_commands()
        ]

    assert asyncio.run(scenario()) == [f"cmd-{failure_mode}"]


def test_conversation_delete_releases_owned_resources_before_worktree_and_record(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers import conversation as conversation_handlers

    order: list[str] = []
    target = SimpleNamespace(
        id="conv-delete-order",
        worktree_path="C:/repo/.minicode/worktrees/conv-delete-order",
        git_isolated=True,
    )

    class Repo:
        def get_conversation(self, conversation_id: str):
            return target if conversation_id == target.id else None

        def delete_conversation(self, conversation_id: str) -> bool:
            assert conversation_id == target.id
            order.append("delete-record")
            return True

    class TerminalManager:
        async def destroy_sessions_for_conversation(self, conversation_id: str) -> int:
            assert conversation_id == target.id
            order.append("stop-terminals")
            return 1

    class BackgroundManager:
        async def destroy_for_conversation(self, conversation_id: str) -> int:
            assert conversation_id == target.id
            order.append("stop-background")
            return 1

    class Session:
        session_id = "session-delete-order"
        is_connected = True
        ws_manager = None
        active_conversation_id = target.id
        conversation_repo = Repo()
        terminal_manager = TerminalManager()
        background_manager = BackgroundManager()
        # The handler detaches the destructive work and hands the task to the
        # session so shutdown can drain it; the test awaits the same handle.
        command_tasks: list[object] = []
        cleanup_tasks: set[object] = set()

        class EventOutbox:
            replay_path = tmp_path / "session-delete-order.jsonl"
            replay_root = tmp_path

            async def delete_conversation_events(self, _conversation_id: str) -> int:
                return 0

        event_outbox = EventOutbox()
        session_lifecycle = SimpleNamespace(
            clear_workspace_runtime=lambda: order.append("release-workspace")
        )

        def track_command_task(self, task) -> None:
            type(self).command_tasks.append(task)

        async def send_conversation_list(self) -> None:
            order.append("send-list")

        async def emit_command_result(self, command: str, message: str, **kwargs) -> None:
            assert command == "conversation.delete"
            assert message == "Conversation deleted."
            assert kwargs == {
                "level": "success",
                "data": {
                    "conversation_id": target.id,
                    "cleanup": {},
                    "cleanup_errors": [],
                },
            }
            order.append("command-result")

    Session.command_dispatcher = SimpleNamespace(
        track_command_task=lambda task: Session.command_tasks.append(task)
    )

    async def stop_run(_session, conversation_id: str, *, reason: str) -> bool:
        assert conversation_id == target.id
        assert reason == "conversation_deleted"
        order.append("stop-run")
        return True

    async def stop_preview(conversation_id: str):
        assert conversation_id == target.id
        order.append("stop-preview")
        return []

    class Scheduler:
        async def destroy_for_conversation(self, conversation_id: str) -> int:
            assert conversation_id == target.id
            order.append("stop-scheduled")
            return 1

    async def cleanup(_session, conversation, *, force: bool = False):
        assert conversation is target
        assert force is False
        order.append("snapshot-and-remove-worktree")
        return {"removed": True, "conversation_id": target.id}

    async def activate(
        _session,
        _preferred_id: str = "",
        *,
        reconcile_agent_state: bool = True,
    ) -> None:
        assert reconcile_agent_state is False
        order.append("activate-fallback")

    async def purge(_session, conversation_id: str):
        assert conversation_id == target.id
        order.append("purge-runtime")
        return {}, []

    monkeypatch.setattr(conversation_handlers, "_stop_conversation_run", stop_run)
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", cleanup)
    monkeypatch.setattr(conversation_handlers, "_activate_conversation_or_blank", activate)
    monkeypatch.setattr(conversation_handlers, "_purge_conversation_runtime_state", purge)
    monkeypatch.setattr("backend.tasks.scheduler.get_global_scheduler", lambda: Scheduler())
    monkeypatch.setattr("backend.preview.launcher.stop_preview_launches_for_conversation", stop_preview)

    async def scenario() -> None:
        session = Session()
        await conversation_handlers.handle_conversation_delete(
            session,
            {"conversation_id": target.id, "cleanup_worktree": True},
        )
        await asyncio.gather(*type(session).command_tasks)

    asyncio.run(scenario())

    assert order == [
        "stop-run",
        "stop-scheduled",
        "stop-background",
        "stop-terminals",
        "stop-preview",
        "release-workspace",
        "snapshot-and-remove-worktree",
        "delete-record",
        "purge-runtime",
        "activate-fallback",
        "send-list",
        "command-result",
    ]


def test_conversation_delete_keeps_record_and_worktree_when_run_ignores_cancellation() -> None:
    from backend.ws.handlers.conversation import handle_conversation_delete

    async def scenario() -> tuple[list[tuple[tuple[object, ...], dict[str, object]]], bool]:
        started = asyncio.Event()
        cancellation_ignored = asyncio.Event()
        release = asyncio.Event()

        async def resistant_run() -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_ignored.set()
                await release.wait()

        task = asyncio.create_task(resistant_run())
        await started.wait()
        target = SimpleNamespace(id="conv-resistant-delete")

        class RunManager:
            def clear_user_message_queue(self, conversation_id: str) -> None:
                assert conversation_id == target.id

        class Repo:
            deleted = False

            def get_conversation(self, conversation_id: str):
                return target if conversation_id == target.id else None

            def delete_conversation(self, _conversation_id: str) -> bool:
                self.deleted = True
                return True

        class TerminalManager:
            async def destroy_sessions_for_conversation(self, _conversation_id: str) -> int:
                raise AssertionError("terminal cleanup must wait for run convergence")

        class Session:
            session_id = "session-resistant-delete"
            is_connected = True
            ws_manager = None
            active_conversation_id = target.id
            conversation_repo = Repo()
            terminal_manager = TerminalManager()
            run_manager = RunManager()
            command_tasks: list[object] = []

            class EventOutbox:
                replay_path = Path("session-resistant-delete.jsonl")
                replay_root = Path("nonexistent-replay-root")

                async def delete_conversation_events(self, _conversation_id: str) -> int:
                    return 0

            event_outbox = EventOutbox()

            def track_command_task(self, task) -> None:
                type(self).command_tasks.append(task)

            def running_agent_task_for(self, conversation_id: str):
                assert conversation_id == target.id
                return task

            async def cancel_agent_runs(self, *, conversation_id: str, reason: str):
                assert conversation_id == target.id
                assert reason == "conversation_deleted"
                task.cancel()
                await cancellation_ignored.wait()

            async def emit_command_result(self, *args, **kwargs) -> None:
                emitted.append((args, kwargs))

        emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []
        session = Session()
        session.command_dispatcher = SimpleNamespace(
            track_command_task=session.track_command_task,
        )
        await handle_conversation_delete(
            session,
            {"conversation_id": target.id, "cleanup_worktree": True},
        )
        # The delete work is detached but session-owned; await the same handle
        # shutdown would drain.
        await asyncio.gather(*type(session).command_tasks)
        release.set()
        await task
        return emitted, session.conversation_repo.deleted

    emitted, deleted = asyncio.run(scenario())

    assert deleted is False
    assert len(emitted) == 1
    args, kwargs = emitted[0]
    assert args[0] == "conversation.delete"
    assert kwargs["data"] == {
        "conversation_id": "conv-resistant-delete",
        "reason": "run_still_active",
    }
