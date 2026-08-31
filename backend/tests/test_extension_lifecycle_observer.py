from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.extensions.lifecycle_observer import (
    ExtensionLifecycleObserver,
    _clone,
    lifecycle_observer_factory,
)
from backend.extensions.loader import ExtensionLoader
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


class _Runner:
    active = True

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> list[Any]:
        self.events.append(event)
        return []


def test_extension_payload_clone_fallback_breaks_cycles_without_aliasing() -> None:
    class Uncopyable:
        def __deepcopy__(self, _memo):
            raise RuntimeError("cannot deepcopy")

        def __repr__(self) -> str:
            return "uncopyable"

    source: list[Any] = [Uncopyable()]
    source.append(source)

    cloned = _clone(source)

    assert cloned is not source
    assert cloned == ["uncopyable", None]


def test_observer_preserves_message_and_tool_lifecycle_order() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(runner, "inspect the repository")
        await bridge.start()
        await bridge.observe(AgentEvent.agent_message_started(item_id="assistant-1"))
        await bridge.observe(
            AgentEvent.agent_message_delta("I will inspect it.", item_id="assistant-1")
        )
        await bridge.observe(
            AgentEvent.agent_message_completed(
                "I will inspect it.", item_id="assistant-1", source="commentary"
            )
        )
        # Provider argument streaming is transport detail. Extensions observe
        # the executable call, so a pending preview must not start execution.
        await bridge.observe(
            AgentEvent.tool_call(
                id="call-1", name="read_file", args={"path": "README.md"}, status="pending"
            )
        )
        await bridge.observe(
            AgentEvent.tool_call(
                id="call-1", name="read_file", args={"path": "README.md"}
            )
        )
        await bridge.observe(
            AgentEvent.tool_output_delta(id="call-1", output="partial README")
        )
        await bridge.observe(AgentEvent.tool_result(id="call-1", summary="README"))
        await bridge.observe(AgentEvent.agent_message_started(item_id="assistant-2"))
        await bridge.observe(AgentEvent.agent_message_delta("Finished.", item_id="assistant-2"))
        await bridge.observe(AgentEvent.agent_message_completed("Finished.", item_id="assistant-2"))
        await bridge.finish()
        return runner

    runner = asyncio.run(scenario())
    event_types = [event["type"] for event in runner.events]
    assert event_types[:5] == [
        "before_agent_start",
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
    ]
    assert event_types.count("tool_execution_start") == 1
    assert event_types.count("tool_execution_update") == 1
    assert event_types.count("tool_execution_end") == 1
    assert event_types[-2:] == ["agent_end", "agent_settled"]

    assistant_end = [
        event
        for event in runner.events
        if event["type"] == "message_end"
        and event["message"].get("role") == "assistant"
    ][0]
    assert assistant_end["message"]["content"][-1] == {
        "type": "tool_call",
        "id": "call-1",
        "name": "read_file",
        "arguments": {"path": "README.md"},
    }
    assert assistant_end["message"]["stop_reason"] == "tool_use"

    first_turn_end = [
        event for event in runner.events if event["type"] == "turn_end"
    ][0]
    assert first_turn_end["turn_index"] == 0
    assert first_turn_end["tool_results"][0]["tool_call_id"] == "call-1"


def test_tool_only_parallel_batch_emits_one_complete_message_before_execution() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(runner, "inspect both files")
        await bridge.start()
        for call_id, path in (("call-1", "README.md"), ("call-2", "pyproject.toml")):
            await bridge.observe(
                AgentEvent.tool_call(
                    id=call_id,
                    name="read_file",
                    args={"path": path},
                    status="pending",
                )
            )
        for call_id, path in (("call-1", "README.md"), ("call-2", "pyproject.toml")):
            await bridge.observe(
                AgentEvent.tool_call(
                    id=call_id,
                    name="read_file",
                    args={"path": path},
                )
            )
            await bridge.observe(
                AgentEvent.tool_result(id=call_id, summary=f"read {path}")
            )
        await bridge.finish()
        return runner

    runner = asyncio.run(scenario())
    assistant_ends = [
        event
        for event in runner.events
        if event["type"] == "message_end"
        and event["message"].get("role") == "assistant"
    ]
    assert len(assistant_ends) == 1
    tool_blocks = [
        block
        for block in assistant_ends[0]["message"]["content"]
        if block.get("type") == "tool_call"
    ]
    assert [block["id"] for block in tool_blocks] == ["call-1", "call-2"]
    assert assistant_ends[0]["message"]["stop_reason"] == "tool_use"

    tool_updates = [
        event["assistant_message_event"]
        for event in runner.events
        if event["type"] == "message_update"
        and event.get("assistant_message_event", {}).get("type")
        in {"toolcall_start", "toolcall_end"}
    ]
    assert [
        event["content_index"]
        for event in tool_updates
        if event["type"] == "toolcall_start"
    ] == [0, 1]
    assert [
        event["content_index"]
        for event in tool_updates
        if event["type"] == "toolcall_end"
    ] == [0, 1]

    types = [event["type"] for event in runner.events]
    assistant_end_index = runner.events.index(assistant_ends[0])
    execution_indexes = [
        index
        for index, event in enumerate(runner.events)
        if event["type"] == "tool_execution_start"
    ]
    assert len(execution_indexes) == 2
    assert assistant_end_index < min(execution_indexes)

    turn_end = next(event for event in runner.events if event["type"] == "turn_end")
    assert [result["tool_call_id"] for result in turn_end["tool_results"]] == [
        "call-1",
        "call-2",
    ]


def test_observer_is_inert_when_no_lifecycle_runtime_is_bound() -> None:
    async def scenario() -> None:
        bridge = ExtensionLifecycleObserver(None, "hello")
        await bridge.start()
        await bridge.observe(AgentEvent.agent_message_delta("ignored"))
        await bridge.finish(status="failed", reason="no runner")

    asyncio.run(scenario())


def test_bridge_resets_tool_identity_state_between_turns() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(runner, "run twice")
        await bridge.start()
        for summary in ("first", "second"):
            await bridge.observe(
                AgentEvent.tool_call(
                    id="reused-call-id",
                    name="read_file",
                    args={"path": f"{summary}.txt"},
                    status="pending",
                )
            )
            await bridge.observe(
                AgentEvent.tool_call(
                    id="reused-call-id",
                    name="read_file",
                    args={"path": f"{summary}.txt"},
                )
            )
            await bridge.observe(
                AgentEvent.tool_result(
                    id="reused-call-id",
                    summary=summary,
                )
            )
            if summary == "first":
                await bridge.observe(
                    AgentEvent.agent_message_started(item_id="next-turn")
                )
        await bridge.finish()
        return runner

    runner = asyncio.run(scenario())

    assert [
        event["tool_call_id"]
        for event in runner.events
        if event["type"] == "tool_execution_start"
    ] == ["reused-call-id", "reused-call-id"]


def test_bridge_projects_failed_unfinished_assistant_as_error_stop_reason() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(runner, "fail after partial output")
        await bridge.start()
        await bridge.observe(AgentEvent.agent_message_started(item_id="partial"))
        await bridge.observe(
            AgentEvent.agent_message_delta("partial text", item_id="partial")
        )
        await bridge.finish(status="failed", reason="runtime_error")
        return runner

    runner = asyncio.run(scenario())
    assistant_end = next(
        event
        for event in runner.events
        if event["type"] == "message_end"
        and event["message"].get("role") == "assistant"
    )
    turn_end = next(event for event in runner.events if event["type"] == "turn_end")

    assert assistant_end["message"]["stop_reason"] == "error"
    assert turn_end["message"]["stop_reason"] == "error"


def test_bridge_keeps_thinking_blocks_in_the_final_assistant_message() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(
            runner,
            "summarize",
            metadata={
                "provider": "minicode-audit",
                "model": "thinking-model",
                "api": "minicode-messages",
            },
        )
        await bridge.start()
        await bridge.observe(
            AgentEvent.agent_message_started(item_id="assistant-thinking")
        )
        await bridge.observe(
            AgentEvent.thinking_chunk(
                "",
                content_index=1,
                lifecycle="start",
            )
        )
        await bridge.observe(
            AgentEvent.thinking_chunk(
                "first ",
                content_index=1,
                lifecycle="delta",
            )
        )
        await bridge.observe(
            AgentEvent.thinking_chunk(
                "thought",
                content_index=1,
                lifecycle="end",
            )
        )
        await bridge.observe(
            AgentEvent.agent_message_delta("answer", item_id="assistant-thinking")
        )
        await bridge.observe(
            AgentEvent.agent_message_completed(
                "answer",
                item_id="assistant-thinking",
            )
        )
        await bridge.finish()
        return runner

    runner = asyncio.run(scenario())
    assistant_end = next(
        event
        for event in runner.events
        if event["type"] == "message_end"
        and event["message"].get("role") == "assistant"
    )
    message = assistant_end["message"]
    assert message["api"] == "minicode-messages"
    assert message["provider"] == "minicode-audit"
    assert message["model"] == "thinking-model"
    assert message["usage"]["input"] == 0
    assert message["stop_reason"] == "stop"
    assert {block["type"] for block in message["content"]} == {"text", "thinking"}
    assert next(
        block for block in message["content"] if block["type"] == "thinking"
    )["thinking"] == "first thought"
    thinking_updates = [
        event
        for event in runner.events
        if event["type"] == "message_update"
        and event.get("assistant_message_event", {}).get("type") == "thinking_delta"
    ]
    assert thinking_updates[-1]["message"]["content"][-1] == {
        "type": "thinking",
        "thinking": "first thought",
    }


def test_bridge_normalizes_images_and_read_only_metadata() -> None:
    async def scenario() -> _Runner:
        runner = _Runner()
        bridge = ExtensionLifecycleObserver(
            runner,
            "look at this",
            metadata=MappingProxyType({"provider": "minicode-audit"}),
            images=(
                {
                    "data": "IMAGE_BYTES",
                    "media_type": "image/jpeg",
                },
            ),
        )
        await bridge.start()
        await bridge.finish()
        return runner

    runner = asyncio.run(scenario())
    user_start = next(
        event
        for event in runner.events
        if event["type"] == "message_start" and event["message"].get("role") == "user"
    )
    assert user_start["message"]["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "image", "data": "IMAGE_BYTES", "mime_type": "image/jpeg"},
    ]
    assert isinstance(runner.events, list)


def test_bridge_carries_before_agent_start_results_into_turn_metadata() -> None:
    def factory(api):
        api.on(
            "before_agent_start",
            lambda event, ctx: {
                "system_prompt": "extension-system",
                "message": {"role": "custom", "content": "extension-context"},
            },
        )

    result = asyncio.run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    metadata: dict[str, Any] = {}

    async def scenario() -> None:
        bridge = ExtensionLifecycleObserver(
            runner,
            "hello",
            metadata=metadata,
        )
        await bridge.start()

    asyncio.run(scenario())
    assert metadata["_extension_system_prompt"] == "extension-system"
    assert metadata["_extension_before_agent_messages"][0]["content"] == (
        "extension-context"
    )
    asyncio.run(runner.shutdown("test"))


def test_query_engine_drives_bridge_from_canonical_agent_events(tmp_path) -> None:
    lifecycle_runtime = _Runner()

    async def agent_runner(**kwargs):
        del kwargs
        yield AgentEvent.agent_message_started(item_id="assistant-1")
        yield AgentEvent.agent_message_delta("done", item_id="assistant-1")
        yield AgentEvent.agent_message_completed("done", item_id="assistant-1")
        yield AgentEvent.done(status="completed")

    submission = QuerySubmission(
        user_message="hello",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
            lifecycle_observer_factory=lifecycle_observer_factory,
        ),
        state=AgentState(user_message="hello", max_iterations=2),
        runtime=AgentLoopSessionContext(
            lifecycle_runtime=lifecycle_runtime,
            metadata={"conversation_id": "conv-extension-observer"},
        ),
    )

    async def collect() -> list[AgentEvent]:
        return [
            event
            async for event in QueryEngine(runner=agent_runner).submit(submission)
        ]

    asyncio.run(collect())
    event_types = [event["type"] for event in lifecycle_runtime.events]
    assert event_types.count("agent_start") == 1
    assert event_types.count("agent_end") == 1
    assert event_types.count("agent_settled") == 1
    assert event_types.count("message_update") == 2
    assert event_types.count("turn_end") == 1
