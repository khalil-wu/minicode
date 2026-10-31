from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from functools import partial

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import AgentEvent
from backend.agent.plans import bind_plan_owner
from backend.api import _state
from backend.main import app
from backend.tests.test_user_input_presentation_chain import session_for
from tests.test_conversations import _install_noop_llm, _receive_until_event


def _rules(payload):
    return [item["rule_content"] for item in payload["rules"]["session_prompt_rules"]]


@pytest.mark.timeout(45)
def test_permission_prompt_rules_follow_their_conversation_across_windows(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=permission-owner-one") as first:
            _receive_until_event(first, "llm.model.updated")
            owner = _state.ws_manager.get_session("permission-owner-one")
            conversation_a = owner.conversation_repo.create_conversation(title="A", memory_mode="none")
            conversation_b = owner.conversation_repo.create_conversation(title="B", memory_mode="none")
            first.send_json({"type": "conversation.switch", "conversation_id": conversation_a.id})
            assert _receive_until_event(first, "conversation.switched")["conversation_id"] == conversation_a.id
            with client.websocket_connect("/ws?session_id=permission-owner-two") as second:
                _receive_until_event(second, "llm.model.updated")
                follower = _state.ws_manager.get_session("permission-owner-two")
                second.send_json({"type": "conversation.switch", "conversation_id": conversation_a.id})
                assert _receive_until_event(second, "conversation.switched")["conversation_id"] == conversation_a.id

                client.portal.call(partial(owner.add_command_prompt_rules_for_conversation,
                    conversation_a.id, ["run A tests"], source="test"))
                assert _rules(_receive_until_event(first, "permission.rules.updated")) == ["prompt: run A tests"]
                assert _rules(_receive_until_event(second, "permission.rules.updated")) == ["prompt: run A tests"]
                assert follower.permission_context.command_prompt_allow_rules == ("run A tests",)

                first.send_json({"type": "conversation.switch", "conversation_id": conversation_b.id})
                assert _receive_until_event(first, "conversation.switched")["conversation_id"] == conversation_b.id
                assert owner.permission_context.command_prompt_allow_rules == ()
                client.portal.call(partial(owner.add_command_prompt_rules_for_conversation,
                    conversation_b.id, ["run B tests"], source="test"))
                assert _rules(_receive_until_event(first, "permission.rules.updated")) == ["prompt: run B tests"]

                # A's still-running owner may receive a plan decision after the
                # same window has switched to B. It updates A and its observer.
                client.portal.call(partial(owner.add_command_prompt_rules_for_conversation,
                    conversation_a.id, ["run A checks"], source="test"))
                assert _rules(_receive_until_event(second, "permission.rules.updated")) == [
                    "prompt: run A tests", "prompt: run A checks",
                ]
                assert owner.permission_context.command_prompt_allow_rules == ("run B tests",)
                assert follower.permission_context.command_prompt_allow_rules == ("run A tests", "run A checks")

                first.send_json({"type": "conversation.switch", "conversation_id": conversation_a.id})
                assert _receive_until_event(first, "conversation.switched")["conversation_id"] == conversation_a.id
                assert owner.permission_context.command_prompt_allow_rules == ("run A tests", "run A checks")
                assert owner.permission_context_for_conversation(conversation_b, source="test").command_prompt_allow_rules == ("run B tests",)

                first.close()
                deadline = time.monotonic() + 5
                while owner.is_connected and time.monotonic() < deadline:
                    time.sleep(.02)
                assert not owner.is_connected
                client.portal.call(partial(follower.add_command_prompt_rules_for_conversation,
                    conversation_a.id, ["run A after reconnect"], source="test"))
                assert _rules(_receive_until_event(second, "permission.rules.updated")) == [
                    "prompt: run A tests", "prompt: run A checks", "prompt: run A after reconnect",
                ]
                assert follower.permission_context.command_prompt_allow_rules == (
                    "run A tests", "run A checks", "run A after reconnect",
                )


def test_plan_paths_and_global_constraints_are_derived_for_the_target(tmp_path, monkeypatch):
    session, conversation_a, _ = session_for(tmp_path, monkeypatch)
    try:
        repository = session.conversation_repo
        conversation_b = repository.create_conversation(title="B", execution_cwd=conversation_a.execution_cwd)
        repository.update_permission_mode(conversation_a.id, "plan")
        repository.update_permission_mode(conversation_b.id, "plan")
        _, plan_a = bind_plan_owner(repository, conversation_a.id, conversation_a.execution_cwd)
        _, plan_b = bind_plan_owner(repository, conversation_b.id, conversation_b.execution_cwd)
        shared_root, private_root = tmp_path / "shared", tmp_path / "only-a"
        private_root.mkdir()
        private_file = private_root / "owned.txt"
        private_file.write_text("A's file", encoding="utf-8")
        session._base_permission_constraints = {"readable_roots": [str(shared_root)]}
        context_a = session.permission_context_for_conversation(repository.get_conversation(conversation_a.id), source="test")
        session.permission_context = replace(context_a, filesystem_constraints={
            **context_a.filesystem_constraints, "readable_roots": [str(private_root)],
        })
        current_a = session.permission_context_for_conversation(repository.get_conversation(conversation_a.id), source="test")
        context_b = session.permission_context_for_conversation(repository.get_conversation(conversation_b.id), source="test")
        assert context_a.filesystem_constraints["plan_files"] == [str(plan_a.resolve())]
        assert current_a.filesystem_constraints["readable_roots"] == [str(private_root)]
        assert context_b.filesystem_constraints["plan_files"] == [str(plan_b.resolve())]
        assert context_b.filesystem_constraints["readable_roots"] == [str(shared_root)]
        assert session.permission_checker.validate_file_operation(str(private_file), "read", context=current_a)[0]
        assert not session.permission_checker.validate_file_operation(str(private_file), "read", context=context_b)[0]
    finally:
        session.run_manager.stop_notification_wake_intake()
        session.run_manager.close_durable_queue()


@pytest.mark.asyncio
async def test_real_run_keeps_background_plan_rules_on_its_original_owner(tmp_path, monkeypatch):
    session, conversation_a, _ = session_for(tmp_path, monkeypatch)
    conversation_b = session.conversation_repo.create_conversation(title="B", execution_cwd=conversation_a.execution_cwd)
    entered, release, applied = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def query(submission):
        entered.set()
        await release.wait()
        await submission.runtime.run_context.command_prompt_allow_rules_setter(["run A tests"])
        applied.set()
        yield AgentEvent.done(status="completed", reason="test")

    monkeypatch.setattr(session.query_engine, "submit", query)
    task = asyncio.create_task(session._run_agent("fixture", conversation_id=conversation_a.id,
        metadata={"user_message_id": "user-owned", "assistant_message_id": "assistant-owned"}))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        session.active_conversation_id = conversation_b.id
        session.permission_context = session.permission_context_for_conversation(conversation_b, source="test-switch")
        release.set()
        await asyncio.wait_for(applied.wait(), 10)
        assert session.permission_context.command_prompt_allow_rules == ()
        assert session.permission_context_for_conversation(conversation_a, source="test").command_prompt_allow_rules == ("run A tests",)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="permission-owner-test")
