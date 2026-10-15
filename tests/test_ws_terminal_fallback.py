from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from backend.agent.message import AgentEvent
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.tasks.manager import TaskManager
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class _NoopLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        if False:
            yield None

    async def simple_chat(self, messages, *, max_tokens=None):
        return ""


class _FakeWebSocket:
    def __init__(self) -> None:
        self.headers = {}
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(dict(payload))


def _session(tmp_path: Path, monkeypatch) -> WebSocketSession:
    monkeypatch.setattr(
        "backend.ws.handler.CONVERSATION_DATA_DIR",
        tmp_path / "conversations",
    )
    monkeypatch.setattr("backend.ws.handler.get_llm_provider", lambda: "openai")
    monkeypatch.setattr(
        "backend.ws.handler.get_available_models",
        lambda _provider="openai": [],
    )
    monkeypatch.setattr(
        "backend.ws.handler.get_models_source",
        lambda _provider="openai": "test",
    )
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda session, **_kwargs: session.llm)
    monkeypatch.setattr(WebSocketSession, "_build_conversation_tool_registry", lambda self, conversation_id="", **_kwargs: self.tool_registry)
    session = WebSocketSession(
        session_id="session-terminal-fallback",
        websocket=_FakeWebSocket(),
        llm=_NoopLLM(),
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        config=AppConfig(llm=LLMSettings(api_key="", provider="openai", model="fixture")),
    )
    # Runtime capability notifications are unrelated to these lifecycle tests.
    session.task_manager = TaskManager()
    return session


async def _wait_for_run_cleanup(session: WebSocketSession, task_id: str) -> None:
    try:
        await session.task_manager.wait(task_id)
    except (asyncio.CancelledError, RuntimeError):
        pass
    for _ in range(100):
        if not session.run_manager.run_tasks:
            break
        await asyncio.sleep(0)
    assert not session.run_manager.run_tasks
    persist_tail = session.event_outbox.persistence_tail
    if persist_tail is not None:
        await asyncio.shield(persist_tail)


def test_start_agent_run_setup_exception_emits_one_failed_done_and_idle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> tuple[WebSocketSession, list[dict[str, object]]]:
        session = _session(tmp_path, monkeypatch)

        async def fail_setup(*_args, **_kwargs) -> None:
            raise RuntimeError("setup exploded")

        # Stub the locked body, not ``_run_agent`` itself: the pre-admission
        # terminal closure lives in ``_run_agent`` and is the contract here.
        session._run_agent_locked = fail_setup  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="exited before the user turn was durably admitted"):
            await session.start_agent_run(
                "hello",
                conversation_id="conv-setup-failure",
                metadata={"assistant_message_id": "assistant-setup-failure"},
            )
        task_id = session.task_manager.list()[0].id
        await _wait_for_run_cleanup(session, task_id)
        return session, session.ws.sent

    session, events = asyncio.run(scenario())
    done = [event for event in events if event.get("type") == "done"]
    idle = [event for event in events if event.get("type") == "session.state_changed"]
    errors = [event for event in events if event.get("type") == "error"]

    assert len(errors) == 1
    assert errors[0]["error_code"] == "startup_failed"
    assert len(done) == 1
    assert done[0]["status"] == "failed"
    assert done[0]["reason"] == "startup_failed"
    assert done[0]["conversation_id"] == "conv-setup-failure"
    assert done[0]["message_id"] == "assistant-setup-failure"
    assert len(idle) == 1
    assert idle[0]["state"] == "idle"
    assert idle[0]["reason"] == "startup_failed"
    assert idle[0]["conversation_id"] == "conv-setup-failure"
    assert session.run_manager.is_delivery_complete("conv-setup-failure") is True


def test_start_agent_run_does_not_duplicate_canonical_terminal_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> list[dict[str, object]]:
        session = _session(tmp_path, monkeypatch)

        class FinalReplyModel(LLMAdapter):
            def __init__(self, settings):
                self._settings = settings
                self._provider_id = "openai"

            async def stream_chat(self, messages, tools=None, metadata=None):
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Owned terminal reply", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

            async def simple_chat(self, messages, *, max_tokens=None):
                return "Owned terminal reply"

        # Keep the actual QueryEngine/TurnIterationRuntime/AnswerCommitter;
        # only the model transport is controlled. The session factory seam
        # returns this captured model for the real run-scoped binding.
        session.llm = FinalReplyModel(session.config.llm)
        conversation = session.conversation_repo.create_conversation(title="Canonical terminal")
        task_id = await session.start_agent_run(
            "hello",
            conversation_id=conversation.id,
        )
        await _wait_for_run_cleanup(session, task_id)
        restored = session.conversation_repo.get_conversation(conversation.id)
        assert restored.transcript[0]["content"] == "hello"
        assert restored.context_snapshot["turn_admissions"]
        assert any(message["role"] == "assistant" and message["content"] == "Owned terminal reply"
                   for message in restored.transcript)
        return session.ws.sent

    events = asyncio.run(scenario())
    terminal = [event for event in events if event.get("type") == "done"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "completed"
    assert not [event for event in events if event.get("type") == "error"]
    transitions = [event for event in events if event.get("type") == "session.state_changed"]
    assert [(event["state"], event["reason"]) for event in transitions] == [
        ("working", "agent_run_started"), ("idle", "completed"),
    ]
    assert all(event["conversation_id"] == terminal[0]["conversation_id"] for event in transitions)


def test_start_agent_run_cancellation_emits_one_cancelled_done_and_idle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A stop during the pre-admission setup zone must still close the turn.

    The setup zone (MCP refresh, conversation lookup, hydration, registry and
    lifecycle construction) runs before QueryEngine owns a terminal
    transaction, and the scheduler net declines to fabricate a terminal for a
    run that was never durably admitted. Cancellation therefore has to be
    closed by ``_run_agent`` itself or the conversation stays busy forever.
    """

    async def scenario() -> tuple[WebSocketSession, list[dict[str, object]]]:
        session = _session(tmp_path, monkeypatch)
        started = asyncio.Event()

        async def wait_forever(*_args, **_kwargs) -> None:
            started.set()
            await asyncio.Event().wait()

        session._run_agent_locked = wait_forever  # type: ignore[method-assign]
        start_task = asyncio.create_task(
            session.start_agent_run(
                "hello",
                conversation_id="conv-cancelled",
                metadata={"assistant_message_id": "assistant-cancelled"},
            )
        )
        await started.wait()
        task_id = session.run_manager.run_task_ids["conv-cancelled"]
        assert session.task_manager.cancel(task_id) is True
        with pytest.raises(RuntimeError, match="cancelled before the user turn was durably admitted"):
            await start_task
        await _wait_for_run_cleanup(session, task_id)
        return session, session.ws.sent

    session, events = asyncio.run(scenario())
    done = [event for event in events if event.get("type") == "done"]
    idle = [event for event in events if event.get("type") == "session.state_changed"]
    assert len(done) == 1
    assert done[0]["status"] == "cancelled"
    assert done[0]["reason"] == "startup_cancelled"
    assert done[0]["conversation_id"] == "conv-cancelled"
    assert done[0]["message_id"] == "assistant-cancelled"
    assert len(idle) == 1
    assert idle[0]["state"] == "idle"
    assert idle[0]["reason"] == "startup_cancelled"
    assert session.run_manager.is_delivery_complete("conv-cancelled") is True


def test_normal_return_without_terminal_fence_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> list[dict[str, object]]:
        session = _session(tmp_path, monkeypatch)

        async def return_without_terminal(*_args, **_kwargs) -> None:
            return None

        session._run_agent_locked = return_without_terminal  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="exited before the user turn was durably admitted"):
            await session.start_agent_run("hello", conversation_id="conv-missing-terminal")
        task_id = session.task_manager.list()[0].id
        await _wait_for_run_cleanup(session, task_id)
        return session.ws.sent

    events = asyncio.run(scenario())
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["status"] == "failed"
    assert done[0]["reason"] == "startup_rejected"


def test_pre_admission_busy_run_emits_terminal_done_and_idle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from backend.agent.conversation_query_guard import conversation_query_guards

    async def scenario() -> list[dict[str, object]]:
        session = _session(tmp_path, monkeypatch)
        guards = conversation_query_guards()
        claim = guards.try_start("conv-busy", owner_id="other-run")
        assert claim is not None
        try:
            with pytest.raises(RuntimeError, match="exited before the user turn was durably admitted"):
                await session.start_agent_run(
                    "hello",
                    conversation_id="conv-busy",
                    metadata={"assistant_message_id": "assistant-busy"},
                )
            task_id = session.task_manager.list()[0].id
            await _wait_for_run_cleanup(session, task_id)
            return session.ws.sent
        finally:
            guards.end(claim)

    events = asyncio.run(scenario())
    done = [event for event in events if event.get("type") == "done"]
    idle = [event for event in events if event.get("type") == "session.state_changed"]

    assert len(done) == 1
    assert done[0]["status"] == "failed"
    assert done[0]["reason"] == "conversation_busy"
    assert done[0]["message_id"] == "assistant-busy"
    assert idle == []


def test_second_run_is_refused_instead_of_orphaning_the_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One conversation owns at most one live run.

    ``RunManager.register`` used to overwrite the run task, task id and cancel
    event unconditionally. Because ``cancel(conversation_id=...)`` and
    ``_cancel_run_tree`` resolve a conversation through exactly those three
    dicts, the first run became unreachable by Stop and by session interrupt
    while both loops streamed into the same conversation. Slash dispatch runs
    before the upstream busy/queue check, so ``/resume`` during an active run
    reached this boundary directly.
    """

    async def scenario() -> tuple[WebSocketSession, list[dict[str, object]], str]:
        session = _session(tmp_path, monkeypatch)
        first_started = asyncio.Event()

        async def wait_forever(*_args, **_kwargs) -> None:
            first_started.set()
            await asyncio.Event().wait()

        session._run_agent_locked = wait_forever  # type: ignore[method-assign]
        first_start_task = asyncio.create_task(
            session.start_agent_run(
                "hello",
                conversation_id="conv-single-owner",
            )
        )
        await first_started.wait()
        first_task_id = session.run_manager.run_task_ids["conv-single-owner"]

        raised = ""
        try:
            await session.start_agent_run("again", conversation_id="conv-single-owner")
        except RuntimeError as exc:
            raised = str(exc)

        # The first run must still be the one Stop reaches.
        assert session.run_manager.run_task_ids["conv-single-owner"] == first_task_id
        assert await session.cancel_agent_runs(conversation_id="conv-single-owner") is True
        with pytest.raises(RuntimeError, match="cancelled before the user turn was durably admitted"):
            await first_start_task
        await _wait_for_run_cleanup(session, first_task_id)
        return session, session.ws.sent, raised

    session, events, raised = asyncio.run(scenario())
    assert "already has a live agent run" in raised
    busy = [
        event
        for event in events
        if event.get("type") == "error" and event.get("error_code") == "agent.busy"
    ]
    assert len(busy) == 1
    assert busy[0]["conversation_id"] == "conv-single-owner"
    # The first run still closes with exactly one terminal.
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["status"] == "cancelled"
