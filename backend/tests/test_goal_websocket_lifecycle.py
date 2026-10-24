import asyncio
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent
from backend.agent.query_engine import QueryEngine
from backend.agent.run_context import RunContext
from backend.agent.runtime import default_runtime
from backend.config import AppConfig, LLMSettings
from backend.feature_flags import FeatureFlags
from backend.services.goal_runtime import GoalTurnAccounting, new_goal
from backend.tests.test_agent_runner_done_fallback import _Session, _NoopLLM, _admit_runner_turn, _run_session_scenario
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import WebSocketSession


class _Socket:
    def __init__(self, events):
        self.events = events

    async def send_json(self, payload):
        self.events.append(payload)


class _PayloadSession(_Session):
    # Keep the production method and its required keyword-only log_context;
    # actual outbox validation and persistence run behind the recording socket.
    send_payload = WebSocketSession.send_payload

    def refresh_llm_selection(self):
        return False

    def __init__(self, tmp_path, events, runner_events=None):
        super().__init__(tmp_path, events, runner_events=runner_events)
        self.event_outbox = EventOutbox(session_id=self.session_id, websocket=_Socket(events),
            replay_root=Path(os.environ["MINICODE_STATE_ROOT"]) / "goal-ws-replay", replay_limit=200,
            cleanup_tasks=self.cleanup_tasks, has_active_run=lambda: False,
            requires_conversation_owner=lambda kind, payload: kind in {"goal.updated", "conversation.summary.updated"},
            workspace_scoped_event_types=())


def configure_runner(monkeypatch):
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda cwd=None, **kwargs: AppConfig(llm=LLMSettings(api_key="offline")))
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", lambda config, model_override=None, **kwargs: _NoopLLM())


async def drain(session):
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()


@pytest.mark.parametrize("status,goal_status", [("completed", "active"), ("failed", "blocked")])
def test_terminal_goal_delivery_uses_latest_revision_and_preserves_projection_receipt(tmp_path, monkeypatch, status, goal_status):
    configure_runner(monkeypatch)
    events = []
    session = _PayloadSession(tmp_path, events)
    repository = session.conversation_repo
    repository.update_goal(session.active_conversation_id, new_goal("Explicit user goal"))
    accounting = GoalTurnAccounting(repository, session.active_conversation_id)
    owner = RunContext(conversation_repository=repository, goal_accounting=accounting)

    async def runner(**kwargs):
        await _admit_runner_turn(kwargs)
        yield AgentEvent.agent_message_completed("Actual retained progress", source="model_final")
        yield AgentEvent.done(status=status, reason="completed" if status == "completed" else "api_error")
    session.query_engine = QueryEngine(runner=runner)

    async def scenario():
        await session._run_agent_locked("Continue the explicit goal", conversation_id=session.active_conversation_id,
            metadata={"agent_runtime": default_runtime(), "assistant_message_id": "assistant-goal", "user_message_id": "user-goal"},
            run_context=owner)
        await drain(session)
    _run_session_scenario(session, scenario())
    current = repository.get_conversation(session.active_conversation_id)
    goal_event = next(event for event in events if event["type"] == "goal.updated" and event["source"] == "turn_completed")
    summary_event = next(event for event in events if event["type"] == "conversation.summary.updated")
    assert goal_event["revision"] == summary_event["revision"] == current.revision == accounting.revision
    assert summary_event["updated_at"] == current.updated_at
    assert goal_event["goal"]["status"] == current.goal["status"] == goal_status
    assert current.transcript[-1]["content"] == "Actual retained progress"
    assert not any(event.get("error_code") == "conversation.persistence_failed" for event in events)
    commits = [event for event in owner.execution_journal.read_events()
        if event.payload.get("lifecycle") == "conversation_projection_committed"]
    assert commits and commits[-1].payload["conversation_revision"] < current.revision


@pytest.mark.parametrize("cancelled", [False, True])
def test_goal_setup_failure_and_cancel_publish_with_the_actual_session_sender(tmp_path, monkeypatch, cancelled):
    events = []
    session = _PayloadSession(tmp_path, events)
    session.conversation_repo.update_goal(session.active_conversation_id, new_goal("Explicit setup goal"))
    async def stopped(*args, **kwargs):
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("Actual pre-admission setup failure")
    monkeypatch.setattr(session, "_run_agent_locked", stopped)

    async def scenario():
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await session._run_agent("Continue", conversation_id=session.active_conversation_id)
        else:
            await session._run_agent("Continue", conversation_id=session.active_conversation_id)
        await drain(session)
    _run_session_scenario(session, scenario())
    current = session.conversation_repo.get_conversation(session.active_conversation_id)
    update = next(event for event in events if event["type"] == "goal.updated")
    assert update["source"] == ("turn_cancelled" if cancelled else "execution_unavailable")
    assert update["revision"] == current.revision
    assert update["goal"] == current.goal
    assert current.goal["_continuation_pending"] is (not cancelled)


def test_goal_delivery_failure_cannot_reclassify_a_committed_terminal_as_a_projection_failure(tmp_path, monkeypatch):
    configure_runner(monkeypatch)
    events = []
    session = _PayloadSession(tmp_path, events)
    session.conversation_repo.update_goal(session.active_conversation_id, new_goal("Explicit goal with transport failure"))
    accounting = GoalTurnAccounting(session.conversation_repo, session.active_conversation_id)
    owner = RunContext(conversation_repository=session.conversation_repo, goal_accounting=accounting)
    original = session.event_outbox.send_payload
    async def send(payload, *, connection_generation=None, log_context, envelope=True, wait_for_delivery=True):
        if payload["type"] == "goal.updated":
            raise OSError("Actual goal transport failure")
        return await original(payload, connection_generation=connection_generation, log_context=log_context,
            envelope=envelope, wait_for_delivery=wait_for_delivery)
    monkeypatch.setattr(session.event_outbox, "send_payload", send)

    async def scenario():
        with pytest.raises(OSError, match="Actual goal transport failure"):
            await session._run_agent_locked("Continue", conversation_id=session.active_conversation_id,
                metadata={"agent_runtime": default_runtime(), "assistant_message_id": "assistant-goal", "user_message_id": "user-goal"},
                run_context=owner)
        await drain(session)
    _run_session_scenario(session, scenario())
    current = session.conversation_repo.get_conversation(session.active_conversation_id)
    assert current.transcript[-1]["terminal_status"] == "completed"
    assert accounting.finished
    assert any(event.payload.get("lifecycle") == "conversation_projection_committed" for event in owner.execution_journal.read_events())
    assert not any(event.get("error_code") == "conversation.persistence_failed" for event in events)


def test_disabled_goal_queue_preserves_pending_saved_state_and_does_not_start_a_turn(tmp_path, monkeypatch):
    events = []
    session = _PayloadSession(tmp_path, events)
    conversation_id = session.active_conversation_id
    session.conversation_repo.update_goal(conversation_id, new_goal("Retain the pending goal"))
    before = session.conversation_repo.get_conversation(conversation_id)
    off = replace(session.config, feature_flags=FeatureFlags({"goals": False}))
    monkeypatch.setattr("backend.config.load_config", lambda **kwargs: off)
    owner = SimpleNamespace(ws_manager=None, run_manager=session.run_manager, conversation_repo=session.conversation_repo,
        running_agent_task_for=lambda _: None, start_agent_run=AsyncMock(),
        session_lifecycle=SimpleNamespace(is_shutting_down=False, workspace_root_for_conversation=lambda record: Path(record.workspace_root)))
    WebSocketSession.schedule_next_queued_user_message(owner, conversation_id)
    assert not session.run_manager.is_queue_dispatching(conversation_id)
    owner.start_agent_run.assert_not_called()
    after = session.conversation_repo.get_conversation(conversation_id)
    assert after.goal == before.goal and after.revision == before.revision


def test_disabled_goal_rejects_automatic_admission_without_rewriting_accounting(tmp_path, monkeypatch):
    events = []
    session = _PayloadSession(tmp_path, events)
    conversation_id = session.active_conversation_id
    session.conversation_repo.update_goal(conversation_id, new_goal("Retain this automatic goal"))
    before = session.conversation_repo.get_conversation(conversation_id)
    off = replace(session.config, feature_flags=FeatureFlags({"goals": False}))
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: off)
    enter = AsyncMock()
    monkeypatch.setattr(session, "_run_agent_locked", enter)
    owner = RunContext()
    _run_session_scenario(session, session._run_agent("Must not run", conversation_id=conversation_id,
        metadata={"_goal_continuation": before.goal["id"]}, run_context=owner))
    enter.assert_not_awaited()
    after = session.conversation_repo.get_conversation(conversation_id)
    assert owner.goal_accounting is None and after.goal == before.goal and after.revision == before.revision


def test_disabled_goals_are_not_injected_or_charged_during_an_ordinary_user_turn(tmp_path, monkeypatch):
    configure_runner(monkeypatch)
    events, observed = [], []
    session = _PayloadSession(tmp_path, events)
    conversation_id = session.active_conversation_id
    session.conversation_repo.update_goal(conversation_id, new_goal("Old goal must remain stored but inactive"))
    before = session.conversation_repo.get_conversation(conversation_id).goal
    off = replace(session.config, feature_flags=FeatureFlags({"goals": False}))
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: off)
    async def runner(**kwargs):
        await _admit_runner_turn(kwargs)
        observed.append((kwargs["state"].task_summary, kwargs["run_context"].goal_accounting))
        yield AgentEvent.agent_message_completed("Actual ordinary reply", source="model_final")
        yield AgentEvent.done()
    session.query_engine = QueryEngine(runner=runner)
    owner = RunContext()
    async def scenario():
        await session._run_agent("A new ordinary user request", conversation_id=conversation_id,
            metadata={"assistant_message_id": "ordinary-answer", "user_message_id": "ordinary-user"}, run_context=owner)
        await drain(session)
    _run_session_scenario(session, scenario())
    assert observed and all(not summary and accounting is None for summary, accounting in observed)
    assert session.conversation_repo.get_conversation(conversation_id).goal == before
    assert not any(event.get("type") == "goal.updated" for event in events)
