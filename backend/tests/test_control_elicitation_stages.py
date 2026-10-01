from __future__ import annotations

import asyncio
from dataclasses import replace
import time

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.control_tools import ControlToolRouter, RoutedToolResult
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.config import PermissionSettings
from backend.hooks.manager import HookResult
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.agent_user_tools import AskUserTool


class RecordingHooks:
    def __init__(self, *, blocked=False, pre_blocked=False, updated_input=None):
        self.blocked = blocked
        self.pre_blocked = pre_blocked
        self.updated_input = updated_input
        self.calls = []

    async def run_pre_tool(self, name, args, **scope):
        self.calls.append(("pre", name, dict(args), scope))
        return HookResult(blocked=self.pre_blocked, updated_input=self.updated_input)

    async def run_permission_request(self, *args, **scope):
        self.calls.append(("permission_request", args, scope))
        return HookResult()

    async def run_permission_denied(self, *args, **scope):
        self.calls.append(("permission_denied", args, scope))
        return HookResult()

    async def run_elicitation(self, prompt, **scope):
        self.calls.append(("elicitation", prompt, scope))
        return HookResult(blocked=self.blocked, feedback="configured start denial" if self.blocked else "")

    async def run_elicitation_result(self, **scope):
        self.calls.append(("elicitation_result", scope))
        return HookResult()

    async def run_post_tool_failure(self, *args, **scope):
        self.calls.append(("post_failure", args, scope))
        return HookResult()

    async def run_post_tool(self, *args, **scope):
        self.calls.append(("post", args, scope))
        return HookResult()


def runtime(tmp_path, hooks=None, settings=None):
    from backend.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(AskUserTool())
    permission = PermissionContext(mode="confirm", conversation_id="elicitation-stage", workspace_root=tmp_path)
    run = RunContext(hook_manager=hooks)
    context = ToolExecutionContext(
        permission=permission, run_context=run, workspace_root=tmp_path,
        conversation_id="elicitation-stage", pending_cleanup_tasks=run.lifecycle_cleanup_tasks,
    )
    return registry, PermissionChecker(settings or PermissionSettings(), tmp_path), context


async def collect_batch(tmp_path, hooks, *, response=None, settings=None, call=None, approval=True):
    registry, checker, context = runtime(tmp_path, hooks, settings)
    answers = []

    async def answer(call_id):
        answers.append(call_id)
        return response if response is not None else {"answer": "one"}

    state = AgentState(user_message="a finite clarification")
    runner = ToolBatchRunner(
        ctx=ContextBuilder(conversation_id="elicitation-stage", workspace_root=tmp_path),
        state=state, tool_registry=registry, permission_checker=checker,
        approval_handler=answer if approval else None, skill_manager=None,
        permission_context=context.permission, tool_ctx=context,
    )
    tc = call or ToolCallEvent(id="actual-question", name="ask_user", arguments={"question": "Pick one", "options": ["one", "two"]})
    events = [event async for event in runner.run([tc])]
    return events, answers, state, context


@pytest.mark.asyncio
async def test_control_start_denial_before_question_wait_and_result(tmp_path):
    hooks = RecordingHooks(blocked=True)
    events, answers, state, _ = await collect_batch(tmp_path, hooks)
    assert [entry[0] for entry in hooks.calls] == ["pre", "elicitation"]
    assert answers == []
    assert not any(event.type == "ask_user" for event in events)
    final = [event for event in events if event.type == "tool_result"]
    assert len(final) == 1
    assert final[0].data["status"] == "blocked"
    assert final[0].data["is_error"] is True
    assert "configured start denial" in final[0].data["summary"]
    assert final[0].data["request_digest"]
    assert len(state.tool_calls) == 1


@pytest.mark.asyncio
async def test_control_start_and_result_share_actual_call_identity(tmp_path):
    hooks = RecordingHooks()
    events, answers, _, _ = await collect_batch(tmp_path, hooks)
    assert [entry[0] for entry in hooks.calls] == ["pre", "elicitation", "elicitation_result"]
    assert answers == ["actual-question"]
    question = [event for event in events if event.type == "ask_user"]
    assert len(question) == 1
    assert question[0].data == {"tool_call_id": "actual-question", "question": "Pick one", "options": ["one", "two"]}
    start = hooks.calls[1][2]
    result = hooks.calls[2][1]
    for entry in [start, result]:
        assert entry["elicitation_id"] == "actual-question"
        assert entry["mcp_server_name"] == "ask_user"
        assert entry["mode"] == "control"
    assert result["action"] == "accept"
    assert result["content"] == {"answer": "one"}
    assert events[-1].data["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"action": "reject", "answer": ""}, {"answer": ""}])
async def test_dismissed_question_remains_partial_not_an_invented_answer(tmp_path, response):
    hooks = RecordingHooks()
    events, answers, _, _ = await collect_batch(tmp_path, hooks, response=response)
    assert answers == ["actual-question"]
    assert hooks.calls[-1][1]["action"] == "cancel"
    assert events[-1].data["status"] == "partial"
    assert "Do not assume an answer" in events[-1].data["summary"]


@pytest.mark.asyncio
async def test_canonical_pre_tool_denial_never_enters_control_stage(tmp_path):
    hooks = RecordingHooks(pre_blocked=True)
    events, answers, _, _ = await collect_batch(tmp_path, hooks)
    assert [entry[0] for entry in hooks.calls] == ["pre"]
    assert answers == []
    assert not any(event.type == "ask_user" for event in events)
    assert events[-1].data["status"] == "blocked"


@pytest.mark.asyncio
async def test_canonical_permission_denial_stays_before_elicitation(tmp_path):
    hooks = RecordingHooks()
    settings = replace(PermissionSettings(), always_deny=["ask_user"])
    events, answers, _, _ = await collect_batch(tmp_path, hooks, settings=settings)
    assert [entry[0] for entry in hooks.calls] == ["pre", "permission_denied"]
    assert answers == []
    assert not any(event.type == "ask_user" for event in events)
    assert events[-1].data["status"] == "blocked"


@pytest.mark.asyncio
async def test_missing_question_is_rejected_before_any_hook_or_wait(tmp_path):
    hooks = RecordingHooks()
    tc = ToolCallEvent(id="missing-question", name="ask_user", arguments={})
    events, answers, _, _ = await collect_batch(tmp_path, hooks, call=tc)
    assert hooks.calls == []
    assert answers == []
    assert not any(event.type == "ask_user" for event in events)
    assert events[-1].data["status"] == "blocked"


@pytest.mark.asyncio
async def test_pre_tool_updated_question_is_the_one_admitted_and_gated(tmp_path):
    hooks = RecordingHooks(updated_input={"question": "Updated finite question", "options": ["A", "B"]})
    events, _, _, _ = await collect_batch(tmp_path, hooks)
    assert hooks.calls[1][1] == "Updated finite question"
    question = [event for event in events if event.type == "ask_user"][0]
    assert question.data["question"] == "Updated finite question"
    assert question.data["options"] == ["A", "B"]


@pytest.mark.asyncio
async def test_without_hooks_control_question_is_still_one_call(tmp_path):
    events, answers, _, _ = await collect_batch(tmp_path, None)
    assert answers == ["actual-question"]
    assert sum(event.type == "ask_user" for event in events) == 1
    assert events[-1].data["status"] == "success"


@pytest.mark.asyncio
async def test_router_yields_question_before_it_invokes_the_real_waiter(tmp_path):
    hooks = RecordingHooks()
    _, _, context = runtime(tmp_path, hooks)
    answers = []

    async def answer(call_id):
        answers.append(call_id)
        return {"answer": "one"}

    router = ControlToolRouter(state=AgentState(user_message="clarify"), approval_handler=answer, skill_manager=None, tool_context=context, hook_manager=hooks)
    call = ToolCallEvent(id="router-id", name="ask_user", arguments={"question": "Question"})
    stream = router.run(call)
    first = await anext(stream)
    assert first.type == "ask_user"
    assert answers == []
    assert hooks.calls[0][0] == "elicitation"
    terminal = await anext(stream)
    assert isinstance(terminal, RoutedToolResult)
    assert answers == ["router-id"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_close_at_question_does_not_start_wait_or_result_hook(tmp_path):
    hooks = RecordingHooks()
    _, _, context = runtime(tmp_path, hooks)

    async def no_answer(_call_id):
        raise AssertionError("closed question must not invoke waiter")

    router = ControlToolRouter(state=AgentState(user_message="clarify"), approval_handler=no_answer, skill_manager=None, tool_context=context, hook_manager=hooks)
    stream = router.run(ToolCallEvent(id="closed", name="ask_user", arguments={"question": "Question"}))
    assert (await anext(stream)).type == "ask_user"
    await stream.aclose()
    assert [entry[0] for entry in hooks.calls] == ["elicitation"]


@pytest.mark.asyncio
async def test_deadline_start_hook_retains_actual_pending_owner_until_release(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class StubbornHooks(RecordingHooks):
        async def run_elicitation(self, prompt, **scope):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return HookResult()

    hooks = StubbornHooks()
    _, _, context = runtime(tmp_path, hooks)
    context.deadline_monotonic = time.monotonic() + 0.04

    async def no_answer(_call_id):
        raise AssertionError("timed-out start gate must not ask user")

    router = ControlToolRouter(state=AgentState(user_message="clarify"), approval_handler=no_answer, skill_manager=None, tool_context=context, hook_manager=hooks)
    stream = router.run(ToolCallEvent(id="stubborn", name="ask_user", arguments={"question": "Question"}))
    try:
        terminal = await anext(stream)
        assert isinstance(terminal, RoutedToolResult)
        assert terminal.result.status == "timeout"
        assert entered.is_set() and not finished.is_set()
        pending = {task for task in context.run_context.lifecycle_cleanup_tasks if not task.done()}
        assert pending
        assert context.pending_cleanup_tasks is context.run_context.lifecycle_cleanup_tasks
    finally:
        release.set()
        await asyncio.wait(context.run_context.lifecycle_cleanup_tasks)
        await stream.aclose()
    assert finished.is_set()
