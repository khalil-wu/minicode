"""Source heartbeat provenance at durable scheduler and real input admission."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.agent.query_engine import QueryEngine
from backend.agent.turn_input import user_input_origin_from_trigger
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.config import LLMSettings
from backend.services.scheduled_task_runner import run_scheduled_task
from backend.tasks.scheduler import ScheduledTaskRun, TaskScheduler
from backend.tests.test_harness_turn_boundaries import query_factory
from backend.tests.test_scheduled_runtime_boundaries import scheduled_case
from backend.tests.guardian_http_fixture import http_endpoint, send_sse
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,scheduled", [
    ("heartbeat", True), ("heartbeat", False), ("cron", True), ("cron", False),
])
async def test_real_scheduler_origin_survives_run_store_and_query_admission(scheduled_case, kind, scheduled):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))

    async def execute(task, run):
        return await run_scheduled_task(task, run, bootstrap=case.bootstrap)

    scheduler = TaskScheduler(on_fire=execute)
    task = scheduler.add_task(
        "Input origin", "Keep this exact scheduled instruction", "* * * * *",
        workspace_root=str(case.workspace), conversation_id=conversation.id,
        kind=kind, isolation="workspace",
    )
    if scheduled:
        now = datetime.now(UTC)
        task.next_run_at = (now - timedelta(minutes=1)).isoformat()
        scheduler._save()
        scheduler._tick(now)
        run = scheduler._runs[task.last_run_id]
    else:
        run = scheduler.run_now(task.id)
    workers = list(scheduler._run_tasks.values())
    await asyncio.gather(*workers)
    assert run.status == "completed", run.error

    expected = "heartbeat" if kind == "heartbeat" and scheduled else "user"
    trigger = ("automation_heartbeat_scheduled" if scheduled else "automation_heartbeat_run_now") if kind == "heartbeat" else None
    assert run.turn_trigger == trigger
    cold_scheduler = TaskScheduler()
    assert cold_scheduler._runs[run.id].turn_trigger == trigger
    stored = case.repository.get_conversation(conversation.id)
    assert stored.transcript[0]["metadata"]["source"] == "scheduled_task"
    cold = ContextBuilder()
    cold.load_snapshot(stored.context_snapshot)
    admitted = [message for message in cold._history if message.is_user_input]
    assert len(admitted) == 1 and admitted[0].input_origin == expected
    assert "Keep this exact scheduled instruction" in admitted[0].content
    evidence = cold.guardian_evidence.retained.user_messages
    assert len(evidence) == 1 and evidence[0].value.origin == expected
    assert cold.guardian_evidence.retained.assistant_messages[-1].value.text == "done"


def test_unknown_saved_trigger_and_manual_heartbeat_keep_source_user_origin():
    previous = ScheduledTaskRun.from_dict({"id": "old-pending", "task_id": "heartbeat-task"})
    assert previous.turn_trigger is None
    for trigger in (None, "automation_heartbeat_manual", "automation", "user", "heartbeat"):
        assert user_input_origin_from_trigger(trigger) == "user"


def test_generic_query_label_does_not_create_a_scheduled_input(query_factory):
    make_session, make_submission = query_factory

    class Provider(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Ordinary result", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    session = make_session(Provider())
    request = make_submission(session, "<heartbeat>Ordinary user text</heartbeat>")
    request.runtime.metadata["query_source"] = "heartbeat"

    async def execute():
        try:
            events = [event async for event in QueryEngine().submit(request)]
            assert next(event for event in events if event.type == "done").data["status"] == "completed"
            assert next(message for message in session.context_builder._history if message.is_user_input).input_origin == "user"
            assert session.context_builder.guardian_evidence.retained.user_messages[0].value.origin == "user"
        finally:
            await session.aclose()

    asyncio.run(execute())


@pytest.mark.asyncio
async def test_pending_scheduled_heartbeat_replay_keeps_original_trigger(scheduled_case):
    case = scheduled_case
    seed = TaskScheduler()
    task = seed.add_task("Replay", "Original pending input", "0 0 1 1 *",
        workspace_root=str(case.workspace), conversation_id="original-thread", kind="heartbeat")
    pending = ScheduledTaskRun(task_id=task.id, workspace_root=str(case.workspace),
        conversation_id=task.conversation_id, turn_trigger="automation_heartbeat_scheduled")
    seed._runs[pending.id] = pending
    seed._save()
    observed = []

    async def execute(_task, run):
        observed.append((run.id, run.turn_trigger))
        return {"status": "completed"}

    restored = TaskScheduler(on_fire=execute)
    await restored.start()
    try:
        await asyncio.gather(*list(restored._run_tasks.values()))
    finally:
        await restored.stop()
    assert observed == [(pending.id, "automation_heartbeat_scheduled")]


def test_human_steer_owns_user_origin_inside_a_heartbeat_turn(query_factory):
    make_session, make_submission = query_factory
    request_owner = None

    class ReadBoundary(BaseTool):
        name = "read_boundary"
        description = "Read the controlled test value."
        read_only = True

        def get_schema(self):
            return ToolSchema(name=self.name, description=self.description,
                parameters={"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            queue = request_owner.runtime.run_context.turn_input_queue
            # Public user commands cannot claim the host-only scheduler origin.
            assert queue.enqueue_command(UserCommand(type="user_message", data={
                "content": "Human addition in the same physical turn", "user_message_id": "human-steer",
                "input_origin": "heartbeat", "turn_trigger": "automation_heartbeat_scheduled",
            }))
            return ToolResult(content="Actual boundary result")

    class Provider(LLMAdapter):
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL,
                    tool_calls=[ToolCallEvent(id="read-origin", name="read_boundary", arguments={})])
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Same original final", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    registry = ToolRegistry()
    registry.register(ReadBoundary())
    session = make_session(Provider(), registry)
    request_owner = make_submission(session, "Original scheduled heartbeat input")
    request_owner.runtime.metadata.update({"input_origin": "heartbeat", "user_message_id": "scheduled-input",
        "_source_responses_metadata": {"turn_trigger": "automation_heartbeat_scheduled"}})

    async def execute():
        try:
            events = [event async for event in QueryEngine().submit(request_owner)]
            assert next(event for event in events if event.type == "done").data["status"] == "completed"
            admitted = [message for message in session.context_builder._history if message.is_user_input]
            assert [message.input_origin for message in admitted] == ["heartbeat", "user"]
            assert "Human addition" in admitted[1].content
            assert session.llm.calls == 2
            snapshot = session.context_builder.export_snapshot()
            cold = ContextBuilder()
            cold.load_snapshot(snapshot)
            assert [message.input_origin for message in cold._history if message.is_user_input] == ["heartbeat", "user"]
            assert [(record.value.message_id, record.value.origin)
                for record in cold.guardian_evidence.retained.user_messages] == [
                    ("scheduled-input", "heartbeat"), ("human-steer", "user")]
        finally:
            await session.aclose()

    asyncio.run(execute())


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [True, False])
async def test_actual_responses_request_matches_scheduled_input_provenance(scheduled_case, scheduled):
    case = scheduled_case
    conversation = case.repository.create_conversation(workspace_root=str(case.workspace))

    async def respond(request, reader, writer):
        await send_sse(writer, [{"type": "response.completed", "response": {
            "id": "actual-heartbeat-response", "status": "completed",
            "output": [{"type": "message", "id": "actual-heartbeat-answer", "role": "assistant",
                "phase": "final_answer", "content": [{"type": "output_text", "text": "Actual heartbeat result"}]}],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }}])

    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(api_key="controlled-local-key", model="controlled-heartbeat",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        case.bootstrap.create_llm = lambda **kwargs: adapter

        async def execute(task, run):
            return await run_scheduled_task(task, run, bootstrap=case.bootstrap)

        scheduler = TaskScheduler(on_fire=execute)
        task = scheduler.add_task("Wire source", "Actual scheduled input", "* * * * *",
            workspace_root=str(case.workspace), conversation_id=conversation.id, kind="heartbeat", isolation="workspace")
        try:
            if scheduled:
                now = datetime.now(UTC)
                task.next_run_at = (now - timedelta(minutes=1)).isoformat()
                scheduler._save()
                scheduler._tick(now)
                run = scheduler._runs[task.last_run_id]
            else:
                run = scheduler.run_now(task.id)
            await asyncio.gather(*list(scheduler._run_tasks.values()))
            assert run.status == "completed", run.error
            assert len(server.requests) == 1
            request = server.requests[0]
            assert request.path == "/v1/responses"
            source = json.loads(request.body.get("client_metadata", {}).get("x-codex-turn-metadata", "{}"))
            if scheduled:
                assert source["turn_trigger"] == "automation_heartbeat_scheduled"
                assert source["request_kind"] == "turn"
                assert json.loads(request.headers["x-codex-turn-metadata"]) == source
            else:
                assert source["turn_trigger"] == "automation_heartbeat_run_now"
                assert source["request_kind"] == "turn"
                assert json.loads(request.headers["x-codex-turn-metadata"]) == source
            assert "x-openai-memgen-request" not in request.headers
            assert "input_origin" not in request.body.get("metadata", {})
            cold = ContextBuilder()
            cold.load_snapshot(case.repository.get_conversation(conversation.id).context_snapshot)
            assert [message.input_origin for message in cold._history if message.is_user_input] == [
                "heartbeat" if scheduled else "user"]
        finally:
            await adapter.aclose()
