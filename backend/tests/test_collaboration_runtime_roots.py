from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

import backend.agent.runtime as runtime_module
from backend.agent.context import ContextBuilder
from backend.agent.mailbox_delivery import inject_subagent_mailbox_updates, _leader_plan_review_required
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.swarm_store import FileSwarmStore
from backend.config import AgentSettings, AppConfig, LLMSettings
from backend.config_requirements import RequirementViolation
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import SendMessageTool


def _runtime(tmp_path):
    return AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
                        enable_lease_heartbeat=False)


def test_explicit_broadcast_does_not_deliver_to_later_first_incarnation(tmp_path):
    async def scenario():
        runtime = _runtime(tmp_path)
        try:
            parent = runtime.start_run(run_id="parent", conversation_id="conversation")
            first = runtime.start_subagent(subagent_id="subagent-first", parent_run_id=parent.run_id,
                                          agent_type="general-purpose")
            owner = RunContext(agent_runtime=runtime)
            sender = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
                conversation_id="conversation", metadata={"run_id": parent.run_id}, run_context=owner)
            result = await SendMessageTool().execute({"recipient": "*", "message": "Only existing recipient"}, sender)
            assert not result.is_error
            late = runtime.start_subagent(subagent_id="subagent-late", parent_run_id=parent.run_id,
                                         agent_type="general-purpose")
            ctx = ContextBuilder()
            count = await inject_subagent_mailbox_updates(
                ctx=ctx, state=AgentState(user_message="continue"),
                metadata={"agent_mode": "subagent", "agent_id": late.subagent_id, "run_id": "late-query"},
                conversation_id="conversation", run_context=owner,
            )
            assert count == 0
            assert not any("Only existing recipient" in str(message.content) for message in ctx._history)
            assert runtime.list_swarm_messages(participant_id=late.subagent_id, conversation_id="conversation",
                                               mailbox_epoch=late.mailbox_epoch) == []
            assert runtime.claim_swarm_messages(participant_id=late.subagent_id, conversation_id="conversation",
                                                mailbox_epoch=late.mailbox_epoch) == []
            assert len(runtime.claim_swarm_messages(participant_id=first.subagent_id,
                conversation_id="conversation", mailbox_epoch=first.mailbox_epoch)) == 1
        finally:
            runtime.close(release_lease=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("epochs", [None, {}])
def test_legacy_broadcast_empty_snapshot_retains_initial_incarnation_contract(epochs):
    message = {"recipient_id": "*", "recipient_mailbox_epochs": epochs}
    assert FileSwarmStore._message_targets_incarnation(message, participant_id="legacy", mailbox_epoch=1)
    assert not FileSwarmStore._message_targets_incarnation(message, participant_id="legacy", mailbox_epoch=2)


@pytest.mark.parametrize("scope", ["task", "session", "conversation"])
def test_pending_child_stop_is_not_convergence_and_purge_preserves_real_owner(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(runtime_module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)
    async def scenario():
        runtime = _runtime(tmp_path)
        parent = runtime.start_run(run_id="parent", conversation_id="conversation", task_id="parent-task",
                                   session_id="parent-session")
        child = runtime.start_subagent(subagent_id="subagent-child", parent_run_id=parent.run_id,
            agent_type="general-purpose", session_id="parent-session")
        entered, release = asyncio.Event(), asyncio.Event()
        effects = []
        async def callback():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            effects.append("late write")
        task = asyncio.create_task(callback())
        runtime.register_subagent_task(child.subagent_id, task, cancel_event=asyncio.Event(),
            parent_run_id=parent.run_id, owner_task_id="parent-task", session_id="parent-session")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if scope == "task":
                stopped = await runtime.stop_subagent_tasks_for_task("parent-task")
            elif scope == "session":
                stopped = await runtime.stop_subagent_tasks_for_session("parent-session")
            else:
                stopped = await runtime.stop_subagent_tasks_for_conversation("conversation")
            assert stopped is False
            assert not task.done()
            assert runtime.get_subagent(child.subagent_id).cleanup_pending
            with pytest.raises(RuntimeError, match="pending subagent cleanup"):
                runtime.purge_conversation("conversation")
            assert runtime._subagent_tasks[child.subagent_id] is task
            assert runtime.get_subagent(child.subagent_id) is not None
            assert runtime._swarm_store.get_subagent(child.subagent_id) is not None
            assert effects == []
        finally:
            release.set()
            await asyncio.wait({task})
            await asyncio.sleep(0)
        assert task.done() and effects == ["late write"]
        assert not runtime.get_subagent(child.subagent_id).cleanup_pending
        assert await runtime.stop_subagent_tasks_for_conversation("conversation") is True
        removed = runtime.purge_conversation("conversation")
        assert child.subagent_id in removed["subagent_ids"]
        runtime.close(release_lease=True)
    asyncio.run(scenario())


def test_completed_child_without_live_task_does_not_leave_permanent_cleanup_intent(tmp_path):
    async def scenario():
        runtime = _runtime(tmp_path)
        try:
            parent = runtime.start_run(run_id="parent", conversation_id="conversation")
            child = runtime.start_subagent(subagent_id="subagent-complete", parent_run_id=parent.run_id,
                agent_type="general-purpose")
            runtime.complete_subagent(child.subagent_id, agent_path=child.agent_path,
                                      mailbox_epoch=child.mailbox_epoch)
            assert await runtime.stop_subagent_tasks_for_conversation("conversation")
            assert not runtime.get_subagent(child.subagent_id).cleanup_pending
            assert child.subagent_id in runtime.purge_conversation("conversation")["subagent_ids"]
        finally:
            runtime.close(release_lease=True)
    asyncio.run(scenario())


def test_authoritative_live_policy_failure_never_falls_back_to_old_bypass():
    def rejected_policy():
        raise RequirementViolation("approval_policy", "never", ["on-request"], None)
    owner = RunContext(permission_context_provider=rejected_policy)
    with pytest.raises(RequirementViolation):
        _leader_plan_review_required({"prompt_mode": "bypass"}, owner)
    owner.permission_context_provider = lambda: PermissionContext(mode="confirm")
    assert _leader_plan_review_required({"prompt_mode": "bypass"}, owner)


def test_sdk_borrows_one_process_runtime_and_session_close_cannot_stop_peer(tmp_path, monkeypatch):
    async def no_repository_probe(_root, *, context=None):
        return ""

    monkeypatch.setattr("backend.agent.context.build_git_status_context_async", no_repository_probe)

    from backend import sdk
    from backend.agent.query_engine import QueryEngine
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)
    monkeypatch.setattr(runtime_module, "SWARM_DIR", tmp_path / "swarm")
    monkeypatch.setattr(runtime_module, "METRICS_FILE", tmp_path / "metrics.jsonl")
    captured = []
    class Engine(QueryEngine):
        def _setup_query(self, submission):
            ctx = super()._setup_query(submission)
            captured.append(ctx.run_context.agent_runtime)
            return ctx
    class Model(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="finished", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)
        async def simple_chat(self, messages, **kwargs):
            return "unused"
    monkeypatch.setattr(sdk, "QueryEngine", Engine)
    async def scenario():
        config = AppConfig(llm=LLMSettings(api_key="synthetic"), agent=AgentSettings(max_iterations=1))
        first = sdk.SDKSession(session_id="first", llm=Model(), config=config,
                               tool_registry=ToolRegistry(), workspace_root=tmp_path)
        peer = sdk.SDKSession(session_id="peer", llm=Model(), config=config,
                              tool_registry=ToolRegistry(), workspace_root=tmp_path)
        try:
            for session in (first, peer):
                async with aclosing(session.query("finish")) as stream:
                    events = [event async for event in stream]
                assert events[-1].data["status"] == "completed"
            assert captured[0] is captured[1]
            runtime = captured[0]
            assert runtime._lease_thread.is_alive()
            await first.aclose()
            assert runtime._lease_thread.is_alive() and not runtime._lease_lost
            async with aclosing(peer.query("peer survives")) as stream:
                events = [event async for event in stream]
            assert events[-1].data["status"] == "completed"
            assert captured[-1] is runtime
            await peer.aclose()
            assert runtime._lease_thread.is_alive()
        finally:
            await first.aclose()
            await peer.aclose()
            if captured:
                captured[0].close(release_lease=True)
    asyncio.run(scenario())
