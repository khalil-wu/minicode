from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import backend.agent.turn_iteration_admission as turn_admission_module
from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy


class _InspectTool(BaseTool):
    name = "inspect_context"
    description = "Inspect deterministic context for a complex loop test."
    read_only = True
    # Only names in ``CORE_TOOL_NAMES`` are directly visible under the default
    # ToolsetPolicy; everything else is deferred and is blocked at execution
    # until tool_search activates it. ``always_load`` is the product's own
    # opt-in for "full schema on turn 1", so the loop actually runs this tool
    # instead of returning the deferred-gate block this test is not about.
    always_load = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        )

    async def execute(self, args, context=None):
        return ToolResult(content=f"inspected:{args['target']}", status="success")


class _PermissionSensitiveWriteTool(BaseTool):
    name = "write_sensitive"
    description = "A mutation used to verify live permission changes."
    mutates_workspace = True
    permission = PermissionLevel.CONFIRM
    # Without this the deferred-exposure gate rejects the call before the
    # permission checker ever runs, so the live permission change under test
    # would never be evaluated. See _InspectTool for the full rationale.
    always_load = True

    def __init__(self) -> None:
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )

    async def execute(self, args, context=None):
        self.calls += 1
        return ToolResult(content=f"wrote:{args['value']}", status="success")


class _NamedTool(BaseTool):
    read_only = True

    def __init__(
        self,
        name: str,
        *,
        exposure: str = "core",
        toolset: str = "core",
    ) -> None:
        self.name = name
        self._exposure = exposure
        self._toolset = toolset

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            exposure=self._exposure,  # type: ignore[arg-type]
            toolset=self._toolset,
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=f"Test tool {self.name}",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None):
        return ToolResult(content=self.name, status="success")


class _ComplexEventLLM(LLMAdapter):
    def __init__(self) -> None:
        self.call = 0

    async def stream_chat(self, messages, tools=None):
        self.call += 1
        if self.call == 1:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK, content="I will inspect the workspace."
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="inspect-1",
                        name="inspect_context",
                        arguments={"target": "README.md"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            return

        if self.call == 2:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="Checking the tool result.",
                phase="commentary",
                raw={"message_phase": "commentary"},
            )
            for content in ("<thi", "nking>SECRET", "</thinking>", "Answer part 1 "):
                yield StreamEvent(
                    type=StreamEventType.TEXT_CHUNK,
                    content=content,
                    phase="final_answer",
                    raw={"message_phase": "final_answer"},
                )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
            return

        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="part 2.",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        return ""


def test_complex_agent_loop_event_sequence_preserves_visibility_and_terminal_invariants() -> (
    None
):
    async def run():
        td = tempfile.mkdtemp()
        registry = ToolRegistry()
        registry.register(_InspectTool())
        context = ContextBuilder()
        state = AgentState(user_message="Inspect and answer")
        events = []

        async for event in run_agent_loop(
            user_message="Inspect and answer",
            llm=_ComplexEventLLM(),
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=td),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=Path(td),
            ),
            agent_settings=AgentSettings(
                max_iterations=6,
                live_text_streaming=True,
            ),
            permission_context=PermissionContext(mode="bypass"),
            context_builder=context,
            state=state,
        ):
            events.append(event)
        return events, context, state

    events, context, state = asyncio.run(run())
    serialized_events = json.dumps(
        [{"type": event.type, "data": event.data} for event in events],
        ensure_ascii=False,
        default=str,
    )

    assert "SECRET" not in serialized_events
    assert "SECRET" not in str(context._history)
    assert state.reply == "Answer part 1 part 2."
    assert len(state.tool_calls) == 1
    assert state.tool_calls[0].status == "success"

    event_types = [event.type for event in events]
    # The unphased pre-tool sentence is streamed as a provisional item and is
    # then reclassified as commentary at the tool boundary.  It remains a
    # real lifecycle item; the two later items are the partial/continued
    # answer segments.
    # Pi's tool-only assistant lifecycle is synthesized by its event bridge
    # from pending tool calls and must not leak as a visible empty answer item.
    assert event_types.count("item.started") == 3
    process_items = [
        event.data
        for event in events
        if event.type == "agent.item" and event.data.get("kind") == "process_text"
    ]
    assert not any(
        item.get("source") == "model_preamble_retracted" for item in process_items
    )
    assert any(item.get("source") == "commentary" for item in process_items)
    completed = [
        event.data.get("item", {}) for event in events if event.type == "item.completed"
    ]
    text_completed = [item for item in completed if item.get("text")]
    assert [item.get("text") for item in text_completed[-2:]] == [
        "Answer part 1 ",
        "part 2.",
    ]

    done_events = [event.data for event in events if event.type == "done"]
    intent_events = [
        event.data for event in events if event.type == "agent.terminal.intent"
    ]
    assert len(done_events) == 1
    assert done_events[0].get("status") == "completed"
    assert done_events[0].get("checkpoint") is not None
    # The loop kernel publishes terminal *intent* plus evidence; the durable
    # run-record CAS and its ``agent.run.completed`` projection belong to
    # QueryEngine._finalize_query (covered by
    # backend/tests/test_canonical_terminal_transaction.py). Asserting
    # agent.run.completed here measured the wrong layer's ownership.
    assert len(intent_events) == 1
    assert intent_events[0].get("status") == "completed"
    assert not any(event.type == "agent.run.completed" for event in events)
    assert event_types.index("agent.terminal.intent") < event_types.index("done")


def test_unexpected_iteration_failure_commits_failed_terminal_run(
    monkeypatch, tmp_path
) -> None:
    async def broken_admit(self, **_kwargs):
        if False:
            yield None
        raise RuntimeError("synthetic iteration failure")

    monkeypatch.setattr(
        turn_admission_module.TurnIterationAdmission, "admit", broken_admit
    )

    # Committing the durable run record is QueryEngine's terminal transaction,
    # not the loop kernel's: run_agent_loop only publishes terminal intent and
    # evidence. Driving the loop directly could never produce
    # ``agent.run.completed``, so this goes through the real owner.
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    submission = QuerySubmission(
        user_message="trigger failure",
        session=AgentSession(
            llm=_ComplexEventLLM(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2),
            token_budget=TokenBudget(),
        ),
        runtime=AgentLoopSessionContext(
            task_id="task-iteration-failure",
            metadata={},
            run_context=RunContext(agent_runtime=runtime),
        ),
    )

    async def run():
        return [event async for event in QueryEngine().submit(submission)]

    try:
        events = asyncio.run(run())
        errors = [event for event in events if event.type == "error"]
        completed = [
            event.data for event in events if event.type == "agent.run.completed"
        ]
        done = [event.data for event in events if event.type == "done"]
        assert errors and errors[-1].data["error_code"] == "agent_loop.runtime_error"
        assert completed and completed[-1]["status"] == "failed"
        assert completed[-1]["error"] == "runtime_error"
        assert len(done) == 1
        assert done[0]["status"] == "failed"
        assert done[0]["reason"] == "runtime_error"
        assert [event.type for event in events].index("agent.run.completed") < [
            event.type for event in events
        ].index("done")
        runs = runtime.list_runs(conversation_id="")["runs"]
        assert [run["status"] for run in runs] == ["failed"]
        assert runs[0]["terminal_reason"] == "runtime_error"
    finally:
        runtime.close(release_lease=True)


def test_agent_session_active_tools_intersect_the_durable_capability_ceiling(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    for tool in (
        _NamedTool("read_file"),
        _NamedTool("write_file"),
        _NamedTool("preview_server", exposure="deferred", toolset="preview"),
    ):
        registry.register(tool)
    session_ceiling = ToolsetPolicy.from_iterables(
        enabled_toolsets=(),
        enabled_tools=["read_file"],
        disabled_tools=["write_file"],
    )

    class _CaptureToolsLLM(LLMAdapter):
        def __init__(self) -> None:
            self.tool_names: list[list[str]] = []

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.tool_names.append(
                [
                    str((schema.get("function") or {}).get("name") or "")
                    for schema in (tools or [])
                ]
            )
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages, *, max_tokens=None):
            return ""

    llm = _CaptureToolsLLM()
    session = AgentSession(
        llm=llm,
        tool_registry=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(max_iterations=1),
        token_budget=TokenBudget(),
        active_tool_names=("read_file", "write_file", "preview_server"),
    )
    submission = QuerySubmission(
        user_message="inspect",
        session=session,
        runtime=AgentLoopSessionContext(
            permission_context=PermissionContext(mode="bypass"),
            workspace_root=tmp_path,
            metadata={"_session_toolset_policy": session_ceiling},
        ),
    )

    async def run() -> list:
        return [event async for event in QueryEngine().submit(submission)]

    events = asyncio.run(run())

    assert llm.tool_names == [["read_file"]]
    assert session.active_tool_names == (
        "read_file",
        "write_file",
        "preview_server",
    )
    assert any(
        event.type == "done" and event.data.get("status") == "completed"
        for event in events
    )


def test_agent_session_can_add_an_active_tool_within_its_original_ceiling(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    owner: dict[str, AgentSession] = {}

    class _LoadOptionalTool(BaseTool):
        name = "load_optional"
        read_only = True

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Activate the optional tool",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None):
            owner["session"].active_tool_names = (
                "load_optional",
                "preview_server",
            )
            return ToolResult(content="activated", status="success")

    registry.register(_LoadOptionalTool())
    registry.register(
        _NamedTool("preview_server", exposure="deferred", toolset="preview")
    )

    class _LoadThenFinishLLM(LLMAdapter):
        def __init__(self) -> None:
            self.tool_names: list[list[str]] = []

        async def stream_chat(self, messages, tools=None, metadata=None):
            names = [
                str((schema.get("function") or {}).get("name") or "")
                for schema in (tools or [])
            ]
            self.tool_names.append(names)
            if len(self.tool_names) == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="load-optional-1",
                            name="load_optional",
                            arguments={},
                        )
                    ],
                )
                yield StreamEvent(
                    type=StreamEventType.DONE,
                    finish_reason="tool_calls",
                )
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages, *, max_tokens=None):
            return ""

    llm = _LoadThenFinishLLM()
    session = AgentSession(
        llm=llm,
        tool_registry=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(max_iterations=3),
        token_budget=TokenBudget(),
        active_tool_names=("load_optional",),
    )
    owner["session"] = session
    submission = QuerySubmission(
        user_message="load the optional tool",
        session=session,
        runtime=AgentLoopSessionContext(
            permission_context=PermissionContext(mode="bypass"),
            workspace_root=tmp_path,
            metadata={},
        ),
    )

    async def run() -> list:
        return [event async for event in QueryEngine().submit(submission)]

    events = asyncio.run(run())

    assert llm.tool_names == [
        ["load_optional"],
        ["load_optional", "preview_server"],
    ]
    assert any(
        event.type == "done" and event.data.get("status") == "completed"
        for event in events
    )


def test_live_permission_change_cancels_tracked_call_and_denies_new_mutation(
    tmp_path,
) -> None:
    async def run():
        registry = ToolRegistry()
        mutation = _PermissionSensitiveWriteTool()
        registry.register(mutation)
        current = {"value": PermissionContext(mode="bypass")}

        class _PermissionChangeLLM(LLMAdapter):
            def __init__(self) -> None:
                self.calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    # Simulate the user switching to plan mode while the
                    # provider is still streaming the tool call.
                    current["value"] = PermissionContext(mode="plan", source="user")
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL,
                        tool_calls=[
                            ToolCallEvent(
                                id="mut-1",
                                name="write_sensitive",
                                arguments={"value": "x"},
                            )
                        ],
                    )
                    yield StreamEvent(
                        type=StreamEventType.DONE, finish_reason="tool_calls"
                    )
                else:
                    yield StreamEvent(
                        type=StreamEventType.TEXT_CHUNK,
                        content="plan mode blocked the mutation",
                    )
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

            async def simple_chat(self, messages):
                return "plan mode blocked the mutation"

        events = []
        async for event in run_agent_loop(
            user_message="change the file",
            llm=_PermissionChangeLLM(),
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            permission_context=PermissionContext(mode="bypass"),
            run_context=RunContext(
                permission_context_provider=lambda: current["value"]
            ),
            agent_settings=AgentSettings(max_iterations=3),
            token_budget=TokenBudget(),
            state=AgentState(user_message="change the file"),
        ):
            events.append(event)
        return events, mutation

    events, mutation = asyncio.run(run())
    assert mutation.calls == 0
    assert any(
        event.type == "permission.decision"
        and event.data.get("decision") == "deny"
        and event.data.get("tool_name") == "write_sensitive"
        for event in events
    ), [
        (
            event.type,
            event.data.get("tool_name") or event.data.get("name"),
            event.data.get("decision"),
            event.data.get("message") or event.data.get("summary"),
        )
        for event in events
    ]
