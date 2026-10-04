"""Persist and replay the WS admission callback with real history and stores."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import UserCommand
from backend.agent.turn_input import TurnInput
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage
from backend.ws import agent_runner
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.run_manager import SessionRunManager


@pytest.mark.asyncio
@pytest.mark.parametrize("steered", [False, True])
async def test_admission_and_journal_use_the_admitted_inputs_identity(tmp_path, steered):
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation()
    context = ContextBuilder(conversation_id=conversation.id)
    context._history_store.append(LLMMessage(role="user", content="accepted request", is_user_input=True))
    journal = ExecutionJournal("admission-audit", base_dir=tmp_path / "runtime")
    command_id = "command-steer" if steered else "command-main"
    message_id = "user-steer" if steered else "user-main"
    original = UserCommand(type="user_message", data={
        "content": "accepted request", "conversation_id": conversation.id,
        "assistant_message_id": "assistant-input", "user_message_id": message_id,
        "client_command_id": command_id, "_queued_user_message_dispatch": True,
    })
    events = []
    started = []

    async def send_event(event):
        events.append(event)

    async def start(content, **kwargs):
        started.append(kwargs)

    session = SimpleNamespace(
        _conversation_projection_lock=lambda cid: asyncio.Lock(), conversation_repo=repo,
        _extension_shutdown_requested=False, active_conversation_id=conversation.id,
        session_id="admission-audit", send_event=send_event, start_agent_run=start,
        running_agent_task_for=lambda cid: None,
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None),
    )
    manager = session.run_manager = SessionRunManager(session)
    manager._notification_wakes_closed = True
    try:
        # Exercise the actual nested callback without booting unrelated provider
        # and workspace services. Its implementation is never copied into this test.
        module = ast.parse(Path(agent_runner.__file__).read_text(encoding="utf-8"))
        callback = next(node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_commit_turn_admission")
        namespace = dict(vars(agent_runner))
        namespace.update(
            self=session, parent_notification_only=False, conversation=conversation,
            run_metadata={"user_message_id": "user-main", "client_command_id": "command-main", "run_id": "run-main"},
            user_message="accepted request", normalized_attachments=[], persisted_context_refs=[],
            run_context_builder=context, execution_journal=journal,
            conversation_store_id=repo.store_instance_id(),
        )
        module = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
        exec(compile(module, agent_runner.__file__, "exec"), namespace)
        await namespace["_commit_turn_admission"](
            boundary_input=SimpleNamespace(consumed_steer=TurnInput.from_command(original) if steered else None),
            history_start=0, history_end=1,
        )
        saved = repo.get_conversation(conversation.id)
        assert saved.context_snapshot["turn_admissions"][message_id]["client_command_id"] == command_id
        prompts = [event for event in journal.read_events() if event.event_type == "user_prompt"]
        assert len(prompts) == 1
        assert prompts[0].payload["client_command_id"] == command_id
        dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "commands")
        await dispatcher._handle_command_inner(original)
        assert events == []
        assert len(started) == 1
        assert started[0]["metadata"]["_turn_admission_restored"] is True
        assert len(repo.get_conversation(conversation.id).transcript) == 1
    finally:
        manager._unsubscribe_parent_notifications()
        manager.close_durable_queue()
