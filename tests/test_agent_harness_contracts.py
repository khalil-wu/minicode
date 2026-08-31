from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from backend.agent import prompting
from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.agent.context import ContextBuilder
from backend.agent.loop import run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.prompting import (
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    PromptBuilderV2,
    clear_system_prompt_sections,
)
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


def test_context_builder_uses_layered_prompt_contract() -> None:
    state = AgentState(user_message="write README")
    state.tool_runtime_guidance = "Runtime contract: stable tool routing belongs in the system context."

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    system = messages[0].content
    developer = messages[1]
    user = messages[-1].content

    assert "You are an agent for MiniCode" in system
    assert "Complete requested tasks fully" in system
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in system
    assert "Runtime contract" not in system
    assert developer.role == "developer"
    assert "Runtime contract" in developer.content
    assert user.startswith("<system-reminder>")
    assert "Runtime contract" not in user
    assert user.endswith("write README")
    assert "<routing>" not in system
    assert "start with the most specific checks" in system
    assert "Do not attempt to fix" in system
    assert "unrelated bugs or broken tests" in system
    assert "Do not re-run a check that already passed" in system


def test_prompt_dynamic_boundary_keeps_system_prefix_stable() -> None:
    clear_system_prompt_sections()
    state = AgentState(user_message="write README")
    state.tool_runtime_guidance = "Runtime guidance belongs after the cache boundary."
    first = PromptBuilderV2().build(state=state)
    second = PromptBuilderV2().build(state=state)

    first_static = first.render_system().split(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)[0]
    second_static = second.render_system().split(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)[0]

    assert first_static == second_static
    assert "Runtime guidance belongs after the cache boundary." not in first.render_system()

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))
    assert messages[1].role == "developer"
    assert "Runtime guidance belongs after the cache boundary." in messages[1].content
    assert "Runtime guidance belongs after the cache boundary." not in messages[-1].content


def test_prompt_ignores_removed_legacy_harness_guidance_field() -> None:
    state = AgentState(user_message="write README")
    setattr(state, "harness_guidance", "Legacy runtime guidance")

    messages = asyncio.run(ContextBuilder().build(state.user_message, state))

    assert all("Legacy runtime guidance" not in message.content for message in messages)


def test_prompt_section_cache_rebuilds_after_clear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_system_prompt_sections()
    state = AgentState(user_message="hello")

    monkeypatch.setattr(prompting, "build_static_environment_info", lambda workspace_root=None: "ENV A")
    first = PromptBuilderV2().build(state=state, workspace_root=tmp_path)

    monkeypatch.setattr(prompting, "build_static_environment_info", lambda workspace_root=None: "ENV B")
    cached = PromptBuilderV2().build(state=state, workspace_root=tmp_path)
    clear_system_prompt_sections()
    rebuilt = PromptBuilderV2().build(state=state, workspace_root=tmp_path)

    assert "ENV A" in first.stable
    assert "ENV A" in cached.stable
    assert "ENV B" not in cached.stable
    assert "ENV B" in rebuilt.stable


async def _collect_events(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


async def _collect_run_events(stream) -> list[object]:
    return [event async for event in stream]


class _PlaceholderAfterToolLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="我查到来源了，但没有直接提取到具体数值。如果你愿意我可以继续。",
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        prompt = messages[-1].content
        assert "Stop quality gate" in prompt
        assert "阵雨" in prompt
        return "根据工具结果，北京今天阵雨，气温 16℃ 到 23℃，北风 3级。"


class _SearchTool:
    name = "mcp__websearch__search"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Search web",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )

    async def execute(self, args: dict[str, object]) -> ToolResult:
        return ToolResult(content="1. 北京天气 https://example.test/weather 北京天气详情")


class _FetchTool:
    name = "mcp__websearch__fetch_page"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Fetch page",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}},
        )

    async def execute(self, args: dict[str, object]) -> ToolResult:
        return ToolResult(
            content="已抓取 https://example.test/weather（约 5000 tokens）",
            artifact_id="art_weather",
            artifact_preview="北京今天 阵雨 16℃ 到 23℃ 北风 3级",
        )


def _submission_for_runner() -> QuerySubmission:
    return QuerySubmission(
        user_message="hello",
        llm=_GroundingLLM(),
        tool_registry=ToolRegistry(),
        artifact_store=ArtifactStore(),
        permission_checker=PermissionChecker(PermissionSettings()),
        agent_settings=AgentSettings(max_iterations=1),
        token_budget=TokenBudget(),
    )


def test_query_engine_can_emit_filtered_events() -> None:
    async def runner(*args, **kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="tool_call_start", data={"id": "tc_1", "name": "read_file"})
        yield AgentEvent.context_compacted("older turns summarized")
        yield AgentEvent.agent_message_completed("hello")
        yield AgentEvent.done(input_tokens=1, output_tokens=2)

    events = asyncio.run(
        _collect_run_events(QueryEngine(runner=runner).submit_filtered(_submission_for_runner()))
    )

    assert [event.type for event in events] == [
        "context_compacted",
        "item.completed",
        "done",
    ]
    assert events[0].data["summary"] == "older turns summarized"
    assert events[1].data["item"]["text"] == "hello"


class _WorkspaceEchoTool(BaseTool):
    name = "workspace_echo"
    # Non-core tool names are deferred under the default ToolsetPolicy, so the
    # call is blocked ("use tool_search to activate it") before ``execute``
    # runs and the workspace_root plumbing under test is never observed.
    # ``always_load`` is the product's own direct-visibility opt-in.
    always_load = True

    def __init__(self) -> None:
        self.seen_workspace: Path | None = None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Echo workspace context",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        )

    async def execute(self, args: dict[str, object], context=None) -> ToolResult:
        self.seen_workspace = Path(context.workspace_root).resolve() if context and context.workspace_root else None
        return ToolResult(content=str(self.seen_workspace or ""))


class _ReadOnceLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        from backend.llm.base import ToolCallEvent

        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(id="read_workspace", name="workspace_echo", arguments={"value": "README.md"})
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _GroundingLLM(LLMAdapter):
    def __init__(self) -> None:
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        prompt = messages[-1].content
        assert "Stop quality gate" in prompt
        assert "tool result says 0.4.2" in prompt
        return "MiniCode 最新版本是 0.4.2。"


class _FailedExtractionGroundingLLM(LLMAdapter):
    def __init__(self) -> None:
        self.simple_chat_calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_chat_calls += 1
        prompt = messages[-1].content
        assert "Stop quality gate" in prompt
        assert "[extraction: failed]" in prompt
        assert "SVG path" in prompt
        return "I checked the source, but the page extraction failed, so I cannot answer reliably from it."
def test_query_submission_workspace_root_reaches_tool_context(tmp_path: Path) -> None:
    workspace = (tmp_path / "actual-project").resolve()
    workspace.mkdir()
    tool = _WorkspaceEchoTool()
    registry = ToolRegistry()
    registry.register(tool)

    async def run() -> list[AgentEvent]:
        return await _collect_events(
            QueryEngine().submit(
                QuerySubmission(
                    user_message="read README",
                    llm=_ReadOnceLLM(),
                    tool_registry=registry,
                    artifact_store=ArtifactStore(),
                    permission_checker=PermissionChecker(PermissionSettings(auto_allow=["workspace_echo"])),
                    agent_settings=AgentSettings(max_iterations=2),
                    token_budget=TokenBudget(),
                    workspace_root=workspace,
                    state=AgentState(user_message="read README"),
                )
            )
        )

    events = asyncio.run(run())

    assert [(event.type, event.data) for event in events if event.type == "tool_result"]
    assert tool.seen_workspace == workspace


def test_tool_result_event_exposes_web_evidence_metadata() -> None:
    event = AgentEvent.tool_result(
        id="fetch_1",
        summary="已抓取页面",
        artifact_id="art_1",
        source_url="https://example.test/weather",
        extraction_status="ok",
        content_preview="北京 18.3℃ 西南风",
        evidence_type="fetched",
    )

    assert event.data["source_url"] == "https://example.test/weather"
    assert event.data["extraction_status"] == "ok"
    assert event.data["content_preview"] == "北京 18.3℃ 西南风"
    assert event.data["evidence_type"] == "fetched"
def test_tool_state_preserves_artifact_preview_for_stop_gate() -> None:
    state = AgentState(user_message="anything")
    result = ToolResult(
        content="已抓取 https://example.test（约 5000 tokens）",
        artifact_id="art_1",
        artifact_preview="important fact: 42",
    )

    state.record_tool_call("web_fetch", {"url": "https://example.test"}, result.to_context_string())

    assert "important fact: 42" in (state.tool_calls[-1].tool_output or "")
    assert "read_artifact('art_1')" in (state.tool_calls[-1].tool_output or "")


def test_tool_state_preserves_structured_web_evidence_for_stop_gate() -> None:
    state = AgentState(user_message="今天北京天气如何")
    result = ToolResult(
        content="北京 18.3℃ 西南风",
        source_url="https://example.test/weather",
        extraction_status="partial",
        content_preview="北京 18.3℃ 西南风",
        evidence_type="fetched",
    )

    state.record_tool_call(
        "web_fetch",
        {"url": "https://example.test/weather"},
        result.to_context_string(),
        source_url=result.source_url,
        extraction_status=result.extraction_status,
        content_preview=result.content_preview,
        evidence_type=result.evidence_type,
    )

    record = state.tool_calls[-1]
    assert record.source_url == "https://example.test/weather"
    assert record.extraction_status == "partial"
    assert record.content_preview == "北京 18.3℃ 西南风"
    assert record.evidence_type == "fetched"
