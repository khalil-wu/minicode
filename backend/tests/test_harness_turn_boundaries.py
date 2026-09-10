from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.hooks.manager import HookEvent, HookManager, HookResult
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, ToolSchema, ToolResult
from backend.tools.registry import ToolRegistry

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5v8AAAAASUVORK5CYII="


class Provider(LLMAdapter):
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="answer", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


@pytest.fixture
def query_factory(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)

    def session(llm, registry=None, limit=3):
        return AgentSession(
            llm=llm, tool_registry=registry or ToolRegistry(), artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=AgentSettings(max_iterations=limit), token_budget=TokenBudget(),
        )

    def submission(owner, message="test boundary", state=None):
        return QuerySubmission(user_message=message, session=owner, state=state,
            runtime=AgentLoopSessionContext(session_id="boundary-session", workspace_root=tmp_path,
                permission_context=PermissionContext(mode="bypass"), metadata={"conversation_id": "boundary-conversation"},
                run_context=RunContext(agent_runtime=runtime)))

    yield session, submission
    runtime.close()


def test_reusing_state_starts_with_a_fresh_query_budget(query_factory):
    session, submission = query_factory
    provider = Provider()
    owner = session(provider, limit=1)
    state = AgentState(user_message="first", max_iterations=1)

    async def scenario():
        first = [event async for event in QueryEngine().submit(submission(owner, "first", state))]
        state.total_retries = 4
        state.provider_continuation_recovery_count = 8
        second = [event async for event in QueryEngine().submit(submission(owner, "second", state))]
        assert [event.data["status"] for event in first + second if event.type == "done"] == ["completed", "completed"]
        assert provider.calls == 2
        assert state.user_message == "second"
        assert state.total_retries == 0
        assert state.provider_continuation_recovery_count == 0

    asyncio.run(scenario())


def test_image_only_response_completes_without_inventing_answer_text(query_factory):
    session, submission = query_factory
    item = {"type": "reasoning", "id": "image-reasoning", "encrypted_content": "opaque", "summary": []}

    class ImageProvider(Provider):
        async def stream_chat(self, messages, tools=None):
            yield StreamEvent(type=StreamEventType.IMAGE_CHUNK, image_data=PNG, image_media_type="image/png")
            yield StreamEvent(type=StreamEventType.DONE, provider_items=[item])

    owner = session(ImageProvider())

    async def scenario():
        events = [event async for event in QueryEngine().submit(submission(owner))]
        assert [event.data["status"] for event in events if event.type in {"done", "agent.run.completed"}] == ["completed", "completed"]
        assert len([event for event in events if event.type == "image_chunk"]) == 1
        assert not [event for event in events if event.type in {"error", "agent_message.delta", "item.completed"}]
        assert owner.context_builder.export_snapshot()["history"][-1]["provider_items"] == [item]

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["eof", "error"])
def test_an_image_does_not_turn_an_incomplete_stream_into_success_or_a_replayed_request(query_factory, terminal):
    session, submission = query_factory

    class InterruptedImageProvider(Provider):
        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            yield StreamEvent(type=StreamEventType.IMAGE_CHUNK, image_data=PNG, image_media_type="image/png")
            if self.calls == 1 and terminal == "error":
                raise ConnectionError("connection reset by peer")
            if self.calls > 1:
                yield StreamEvent(type=StreamEventType.DONE)

    provider = InterruptedImageProvider()
    owner = session(provider)
    owner.agent_settings = replace(owner.agent_settings, stream_max_attempts=1)

    async def scenario():
        events = [event async for event in QueryEngine().submit(submission(owner))]
        assert provider.calls == 1
        assert next(event for event in events if event.type == "done").data["status"] == "failed"
        assert len([event for event in events if event.type == "image_chunk"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_source", ["consumer", "observer"])
def test_closing_a_tool_turn_records_a_matching_result_before_the_next_query(query_factory, stop_source):
    session, submission = query_factory
    executed = []

    class Probe(BaseTool):
        name = "probe"
        description = "Probe"
        read_only = True

        def get_schema(self):
            return ToolSchema(name=self.name, description=self.description, parameters={"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            executed.append(True)
            return ToolResult(content="probe result")

    class ToolProvider(Provider):
        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="probe-call", name="probe", arguments={})])
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="continued", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE)

    registry = ToolRegistry()
    registry.register(Probe())
    owner = session(ToolProvider(), registry)

    async def scenario():
        if stop_source == "consumer":
            stream = QueryEngine().submit(submission(owner))
            async for event in stream:
                if event.type == "tool_call" and any(m.get("tool_calls") for m in owner.context_builder.export_snapshot()["history"]):
                    break
            else:
                raise AssertionError("tool execution was not reached")
            await stream.aclose()
        else:
            observing = asyncio.Event()
            terminal_events = []

            class Observer:
                async def start(self):
                    pass

                async def observe(self, event):
                    if event.type == "tool_call" and any(m.get("tool_calls") for m in owner.context_builder.export_snapshot()["history"]):
                        observing.set()
                        await asyncio.Event().wait()

                async def finish(self, **kwargs):
                    pass

            owner.lifecycle_observer_factory = lambda **kwargs: Observer()

            async def collect():
                async for event in QueryEngine().submit(submission(owner)):
                    if event.type in {"agent.run.completed", "done"}:
                        # Check durability at delivery time, before the outer
                        # query's final cleanup has a chance to change history.
                        checkpoint = load_latest_checkpoint("boundary-session", conversation_id="boundary-conversation")
                        assert checkpoint is not None
                        assert any(m.get("tool_call_id") == "probe-call" for m in checkpoint.messages)
                        terminal_events.append(event)

            task = asyncio.create_task(collect())
            await observing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert [event.data["status"] for event in terminal_events] == ["cancelled", "cancelled"]
            owner.lifecycle_observer_factory = None
        history = owner.context_builder.export_snapshot()["history"]
        results = [message for message in history if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["probe-call"]
        assert history[-1]["role"] == "user"
        assert "interrupted" in history[-1]["content"].lower()
        assert executed == []
        checkpoint = load_latest_checkpoint("boundary-session", conversation_id="boundary-conversation")
        assert checkpoint is not None
        assert any(message.get("tool_call_id") == "probe-call" for message in checkpoint.messages)
        events = [event async for event in QueryEngine().submit(submission(owner, "continue"))]
        assert next(event for event in events if event.type == "done").data["status"] == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["admission", "checkpoint"])
def test_restored_input_is_not_admitted_or_hooked_again(query_factory, source):
    session, submission = query_factory
    provider = Provider()
    owner = session(provider)
    owner.context_builder = ContextBuilder()
    message = "already admitted unique prompt"
    owner.context_builder.append_user(message)
    hooks_called = []
    admissions = []

    class Hooks(HookManager):
        def has_hooks(self, event):
            return event in {HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT}

        async def run_session_start_once(self, session_id):
            hooks_called.append("session")
            return HookResult()

        async def run_user_prompt_submit(self, message):
            hooks_called.append("prompt")
            return HookResult(blocked=True, message="must not recheck admitted input")

    request = submission(owner, message)
    request.runtime.run_context.hook_manager = Hooks()
    request.runtime.metadata["commit_turn_admission"] = lambda **kw: admissions.append(kw)
    if source == "admission":
        request.runtime.metadata["_turn_admission_restored"] = True
    else:
        snapshot = owner.context_builder.export_snapshot()
        save_checkpoint(
            session_id="boundary-session", conversation_id="boundary-conversation",
            user_message=message, iterations=0, reply="", messages=snapshot["history"],
            context_snapshot=snapshot, tool_calls=[], active_skills=[], disabled_tools=set(),
            stopped_reason="interrupted", last_mutation_index=0,
        )
        request.runtime.metadata["resume_from_checkpoint"] = True

    async def scenario():
        events = [event async for event in QueryEngine().submit(request)]
        assert next(event for event in events if event.type == "done").data["status"] == "completed"
        assert provider.calls == 1
        assert hooks_called == []
        assert admissions == []
        history = owner.context_builder.export_snapshot()["history"]
        users = [item["content"] for item in history if item["role"] == "user"]
        assert len(users) == 1
        assert users[0].endswith(message)

    asyncio.run(scenario())


@pytest.mark.parametrize("resume_requested", [False, True])
def test_stale_recovery_metadata_does_not_suppress_a_new_user_message(query_factory, resume_requested):
    session, submission = query_factory
    owner = session(Provider())
    request = submission(owner, "new prompt after recovery")
    request.runtime.metadata.update({
        "resume_from_checkpoint": resume_requested,
        "_query_engine_recovery_restored": True,
        "checkpoint_origin": {"run_id": "previous-recovered-run"},
    })

    async def scenario():
        events = [event async for event in QueryEngine().submit(request)]
        assert next(event for event in events if event.type == "done").data["status"] == "completed"
        history = owner.context_builder.export_snapshot()["history"]
        assert sum("new prompt after recovery" in item["content"] for item in history) == 1
        assert request.runtime.metadata["_query_engine_recovery_restored"] is False
        assert request.runtime.metadata["checkpoint_origin"] == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["stall", "event_retry", "transport_retry"])
def test_turn_deadline_stops_provider_wait_and_retry_backoff(query_factory, failure):
    session, submission = query_factory

    class ProviderFailure(Provider):
        closed = False

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            try:
                if failure == "event_retry":
                    raise ConnectionError("connection reset by peer")
                await asyncio.Event().wait()
                yield
            finally:
                self.closed = True

    provider = ProviderFailure()
    owner = session(provider)
    owner.agent_settings = replace(owner.agent_settings, max_turn_seconds=1,
        stream_timeout_seconds=0.05 if failure == "transport_retry" else 10,
        stream_retry_policy=SimpleNamespace(decide_retry=lambda _error, attempt:
            SimpleNamespace(should_retry=attempt < 1, delay_seconds=5)))

    async def scenario():
        started = time.perf_counter()
        events = []
        async for event in QueryEngine().submit(submission(owner)):
            if event.type == "done":
                assert provider.closed
            events.append(event)
        assert time.perf_counter() - started < 3
        assert provider.calls == 1
        done = next(e for e in events if e.type == "done")
        assert done.data["reason"] == "max_turn_seconds"
        assert done.data["status"] == "partial"
        retries = [e for e in events if e.type == "agent.progress" and e.data.get("provider_state") == "reconnecting"]
        assert len(retries) == (0 if failure == "stall" else 1)
        checkpoint = load_latest_checkpoint("boundary-session", conversation_id="boundary-conversation")
        assert checkpoint.stopped_reason == "max_turn_seconds"

    asyncio.run(scenario())
