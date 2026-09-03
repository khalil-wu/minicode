import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from backend.agent.loop import run_agent_loop
from backend.agent.provider_response_recovery import textual_tool_call_imitation
from backend.agent.progress import agent_progress
from backend.agent.query_terminal import QueryTerminalTransaction
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import (
    LLMAdapter,
    StreamEvent,
    StreamEventType,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolCallStartEvent,
    UsageInfo,
)
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class _PreambleThenToolLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="我先查看相关文件。")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_1",
                    name="inspect_context",
                    arguments={"target": "README.md"},
                )
            ],
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ProviderReasoningThenFinalLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.THINKING_CHUNK,
            content="厂家 raw reasoning",
            raw={"provider_reasoning_type": "reasoning_content"},
        )
        yield StreamEvent(
            type=StreamEventType.THINKING_CHUNK,
            content="厂家 reasoning summary",
            raw={"provider_reasoning_type": "reasoning_summary_text"},
        )
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="结论。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _LengthAfterToolBlockLLM(LLMAdapter):
    """Complete tool block followed by a provider length stop, then recovery."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls_final=False,
                tool_calls=[
                    ToolCallEvent(
                        id="truncated_tool",
                        name="inspect_context",
                        arguments={"target": "README.md"},
                    )
                ],
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls_final=True,
                tool_calls=[
                    ToolCallEvent(
                        id="truncated_tool",
                        name="inspect_context",
                        arguments={"target": "README.md"},
                    )
                ],
            )
            yield StreamEvent(
                type=StreamEventType.DONE,
                finish_reason="length",
            )
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="恢复后的回答。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _InternalAnalysisPreambleThenToolLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content=(
                "The user is asking about today's weather in Beijing. "
                "This is current information that I need to search for. "
                "Let me use web_search to find current weather information for Beijing."
            ),
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_1",
                    name="inspect_context",
                    arguments={"target": "README.md"},
                )
            ],
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ProviderFinalPhaseLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="这是 provider 标记的最终回答。",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _FinalPhaseThenToolLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="我先从官方气象源确认一下。",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_after_final_phase",
                    name="inspect_context",
                    arguments={"target": "weather"},
                )
            ],
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ProviderCommentaryThenFinalLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="我先核对日期。",
            phase="commentary",
            raw={"message_phase": "commentary"},
        )
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="昨天是 2026 年 6 月 28 日。",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ProviderCommentaryThenToolLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="我先核对日期。",
            phase="commentary",
            raw={"message_phase": "commentary"},
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_after_commentary",
                    name="inspect_context",
                    arguments={"target": "README.md"},
                )
            ],
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ToolCallThenFinalLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self.done_after_tool_was_consumed = False

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="inspect_early",
                        name="inspect_context",
                        arguments={"target": "README.md"},
                    )
                ],
            )
            self.done_after_tool_was_consumed = True
            yield StreamEvent(
                type=StreamEventType.DONE,
                usage=UsageInfo(
                    input_tokens=17,
                    output_tokens=5,
                    cache_read_input_tokens=8,
                    reasoning_output_tokens=3,
                ),
            )
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="完成。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _NonFinalToolBlocksThenFinalBatchLLM(LLMAdapter):
    def __init__(self) -> None:
        self.trailing_done_was_consumed = False

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_a",
                    name="inspect_context",
                    arguments={"target": "a.py"},
                )
            ],
            tool_calls_final=False,
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_b",
                    name="inspect_context",
                    arguments={"target": "b.py"},
                )
            ],
            tool_calls_final=False,
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_a",
                    name="inspect_context",
                    arguments={"target": "a.py"},
                ),
                ToolCallEvent(
                    id="inspect_b",
                    name="inspect_context",
                    arguments={"target": "b.py"},
                ),
            ],
            tool_calls_final=True,
        )
        self.trailing_done_was_consumed = True
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _NonFinalToolBlockThenDoneLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_half",
                    name="inspect_context",
                    arguments={"target": "half.py"},
                )
            ],
            tool_calls_final=False,
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _NonFinalToolBlockThenLengthLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="inspect_truncated",
                        name="inspect_context",
                        arguments={"target": "truncated.py"},
                    )
                ],
                tool_calls_final=False,
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="续写完成。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _NonFinalToolBlockDelayedFinalBatchLLM(LLMAdapter):
    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_streamed",
                    name="inspect_context",
                    arguments={"target": "streamed.py"},
                )
            ],
            tool_calls_final=False,
        )
        await asyncio.sleep(0.02)
        self.markers.append("final_batch")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_streamed",
                    name="inspect_context",
                    arguments={"target": "streamed.py"},
                )
            ],
            tool_calls_final=True,
        )
        self.markers.append("trailing_done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _FinalToolBatchDelayedDoneLLM(LLMAdapter):
    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_final",
                    name="inspect_context",
                    arguments={"target": "final.py"},
                )
            ],
            tool_calls_final=True,
        )
        await asyncio.sleep(0.02)
        self.markers.append("trailing_done")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")

    async def simple_chat(self, messages):
        return ""


class _PreambleSplitByNonFinalToolBlockLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="上海今天的实时天气我去核一下最新预报，再给你一个可直",
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_weather",
                    name="inspect_context",
                    arguments={"target": "weather.md"},
                )
            ],
            tool_calls_final=False,
        )
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="观使用的结论。")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_weather",
                    name="inspect_context",
                    arguments={"target": "weather.md"},
                )
            ],
            tool_calls_final=True,
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _NonFinalWebSearchDelayedFinalBatchLLM(LLMAdapter):
    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="web_streamed",
                    name="web_search",
                    arguments={"query": "MiniCode streaming executor"},
                )
            ],
            tool_calls_final=False,
        )
        await asyncio.sleep(0.02)
        self.markers.append("final_batch")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="web_streamed",
                    name="web_search",
                    arguments={"query": "MiniCode streaming executor"},
                )
            ],
            tool_calls_final=True,
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")

    async def simple_chat(self, messages):
        return ""


class _UnsafeThenSafeNonFinalBlocksLLM(LLMAdapter):
    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="unsafe_first",
                    name="unsafe_context",
                    arguments={"target": "state"},
                )
            ],
            tool_calls_final=False,
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="inspect_after_unsafe",
                    name="inspect_context",
                    arguments={"target": "after.py"},
                )
            ],
            tool_calls_final=False,
        )
        await asyncio.sleep(0.02)
        self.markers.append("final_batch")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="unsafe_first",
                    name="unsafe_context",
                    arguments={"target": "state"},
                ),
                ToolCallEvent(
                    id="inspect_after_unsafe",
                    name="inspect_context",
                    arguments={"target": "after.py"},
                ),
            ],
            tool_calls_final=True,
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")

    async def simple_chat(self, messages):
        return ""


class _PartialWriteFileThenFinalLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="我会先创建一个单文件 HTML 游戏。",
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                tool_call_start=ToolCallStartEvent(
                    id="write-1", name="write_file", index=0
                ),
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_DELTA,
                tool_call_delta=ToolCallDeltaEvent(
                    id="write-1",
                    partial_arguments='{"file_path":"angry-birds.html","content":"',
                ),
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_DELTA,
                tool_call_delta=ToolCallDeltaEvent(
                    id="write-1",
                    partial_arguments='{"file_path":"angry-birds.html","content":"<html></html>"}',
                ),
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="write-1",
                        name="write_file",
                        arguments={
                            "file_path": "angry-birds.html",
                            "content": "<html></html>",
                        },
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="已完成。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _MalformedWriteFileLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="write-malformed",
                    name="write_file",
                    arguments={"content": "truncated"},
                    arguments_repaired=True,
                )
            ],
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _TextualToolImitationLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content=(
                "我来查询。\n"
                "using web_fetch\n"
                '<invoke name="web_fetch">\n'
                '<parameter name="url">https://example.test</parameter>\n'
                "nvoke>\n"
                "查询完成。"
            ),
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        return ""


class _DocumentedTextualToolSyntaxLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content=(
                "不要这样写：\n"
                "```xml\n"
                '<invoke name="web_fetch">\n'
                '<parameter name="url">https://example.test</parameter>\n'
                "</invoke>\n"
                "```"
            ),
        )
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages):
        return ""


class _InspectContextTool(BaseTool):
    name = "inspect_context"
    description = "Read deterministic context."
    read_only = True
    # Non-core tool names are deferred under the default ToolsetPolicy and are
    # blocked at execution until tool_search activates them, so without this
    # opt-in the tool never runs and the settled-boundary markers below are
    # never recorded. ``always_load`` is the product's own "direct on turn 1"
    # flag (backend/tools/base.py), not a test-only escape hatch.
    always_load = True

    def __init__(self, markers: list[str] | None = None) -> None:
        self.markers = markers

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        )

    async def execute(self, args, context=None):
        if self.markers is not None:
            self.markers.append("tool_started")
            await asyncio.sleep(0.05)
            self.markers.append("tool_finished")
        return ToolResult(content="README.md exists", status="success")


class _WebFetchTestTool(_InspectContextTool):
    name = "web_fetch"
    description = "Fetch deterministic web content."


class _UnsafeContextTool(BaseTool):
    name = "unsafe_context"
    description = "A deterministic non-concurrency-safe tool."
    read_only = False
    mutates_workspace = True
    # See _InspectContextTool: deferred exposure would block execution before
    # the concurrency-safety ordering under test could be observed.
    always_load = True

    def __init__(self, markers: list[str] | None = None) -> None:
        self.markers = markers

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        )

    async def execute(self, args, context=None):
        if self.markers is not None:
            self.markers.append("unsafe_started")
        return ToolResult(content="unsafe ran", status="success")


class _WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write deterministic file content."
    read_only = False
    mutates_workspace = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        )

    def streamed_input_preview(self, args, context=None, prior=None):
        file_path = args.get("file_path")
        return {"file_path": file_path} if isinstance(file_path, str) else {}

    async def execute(self, args, context=None):
        return ToolResult(
            content=f"Created {args.get('file_path', 'file')}\n+1 -0",
            status="success",
        )


class _WebSearchContextTool(BaseTool):
    name = "web_search"
    description = "Search deterministic web context."
    read_only = True
    open_world = True

    def __init__(self, markers: list[str]) -> None:
        self.markers = markers

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )

    async def execute(self, args, context=None):
        self.markers.append("web_started")
        await asyncio.sleep(0.05)
        self.markers.append("web_finished")
        return ToolResult(
            content="[1] MiniCode docs — https://example.test", status="success"
        )


def _run(
    llm: LLMAdapter,
    *,
    max_iterations: int = 1,
    tool: BaseTool | None = None,
    tools: list[BaseTool] | None = None,
):
    async def _go():
        td = tempfile.mkdtemp()
        registry = ToolRegistry()
        for item in tools or [tool or _InspectContextTool()]:
            registry.register(item)
        events = []
        async for ev in run_agent_loop(
            user_message="检查项目",
            llm=llm,
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=td),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=Path(td),
            ),
            agent_settings=AgentSettings(max_iterations=max_iterations),
            permission_context=PermissionContext(mode="bypass"),
        ):
            events.append(ev)
        return events

    return asyncio.run(_go())


def test_unphased_preamble_before_tool_is_streamed_then_reclassified_as_commentary():
    events = _run(_PreambleThenToolLLM(), max_iterations=1)

    deltas = [
        e.data.get("delta")
        for e in events
        if getattr(e, "type", None) == "agent_message.delta"
    ]
    completed = [
        e.data.get("item", {})
        for e in events
        if getattr(e, "type", None) == "item.completed"
        and e.data.get("item", {}).get("type") == "agent_message"
    ]

    assert deltas == ["我先查看相关文件。"]
    assert completed[-1]["text"] == "我先查看相关文件。"
    assert completed[-1]["source"] == "commentary"
    assert completed[-1]["status"] == "completed"


def test_textual_tool_imitation_fails_turn_without_executing_tool():
    markers: list[str] = []
    events = _run(
        _TextualToolImitationLLM(),
        tool=_WebFetchTestTool(markers),
    )

    assert markers == []
    errors = [event for event in events if getattr(event, "type", None) == "error"]
    assert len(errors) == 1
    assert errors[0].data["error_type"] == "invalid_model_action"
    assert errors[0].data["error_code"] == "textual_tool_call_imitation"
    assert "该工具没有执行" in errors[0].data["message"]
    completed = [
        event.data.get("item", {})
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    assert completed[-1]["status"] == "failed"
    assert completed[-1]["source"] == "provider_protocol_error"
    done = [event for event in events if getattr(event, "type", None) == "done"]
    assert done[-1].data["status"] == "failed"
    assert done[-1].data["reason"] == "invalid_model_action"


def test_fenced_tool_syntax_example_remains_normal_answer_text():
    events = _run(_DocumentedTextualToolSyntaxLLM())

    assert not any(
        getattr(event, "type", None) == "error"
        and event.data.get("error_code") == "textual_tool_call_imitation"
        for event in events
    )
    completed = [
        event.data.get("item", {})
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    assert completed[-1]["status"] == "completed"
    assert '<invoke name="web_fetch">' in completed[-1]["text"]


def test_textual_tool_imitation_ignores_inline_examples_and_unexposed_names():
    assert (
        textual_tool_call_imitation(
            '例如 `<invoke name="web_fetch"><parameter name="url">...</parameter></invoke>`。',
            exposed_tool_names={"web_fetch"},
        )
        == ""
    )
    assert (
        textual_tool_call_imitation(
            '<invoke name="unknown_tool"><parameter name="value">1</parameter>',
            exposed_tool_names={"web_fetch"},
        )
        == ""
    )


def test_textual_tool_imitation_detects_case_insensitive_xml_tags():
    assert (
        textual_tool_call_imitation(
            '<INVOKE NAME="web_fetch">\n<PARAMETER NAME="url">https://example.test</PARAMETER>',
            exposed_tool_names={"web_fetch"},
        )
        == "web_fetch"
    )


def test_agent_progress_is_owned_by_its_event_type():
    event = agent_progress(
        "Cache settled",
        stage="cache",
        status="completed",
        id="cache:settled",
        phase="model",
        label="cache",
    )

    assert event.type == "agent.progress"


def test_unphased_provider_text_before_tool_is_not_silently_discarded():
    events = _run(_InternalAnalysisPreambleThenToolLLM(), max_iterations=1)

    deltas = [
        str(e.data.get("delta") or "")
        for e in events
        if getattr(e, "type", None) == "agent_message.delta"
    ]
    completed = [
        e.data.get("item", {})
        for e in events
        if getattr(e, "type", None) == "item.completed"
        and e.data.get("item", {}).get("type") == "agent_message"
    ]

    assert any("Let me use web_search" in delta for delta in deltas)
    assert "Let me use web_search" in completed[-1].get("text", "")
    assert completed[-1].get("source") == "commentary"


def test_streamed_tool_boundary_preserves_split_preamble_order_without_duplication():
    events = _run(_PreambleSplitByNonFinalToolBlockLLM())

    deltas = [
        str(e.data.get("delta") or "")
        for e in events
        if getattr(e, "type", None) == "agent_message.delta"
    ]
    completed_messages = [
        e.data.get("item", {})
        for e in events
        if getattr(e, "type", None) == "item.completed"
        and e.data.get("item", {}).get("type") == "agent_message"
    ]
    process_items = [
        e.data
        for e in events
        if getattr(e, "type", None) == "agent.item"
        and e.data.get("kind") == "process_text"
    ]
    assert deltas == ["上海今天的实时天气我去核一下最新预报，再给你一个可直"]
    assert completed_messages[-1].get("source") == "commentary"
    assert [item.get("content") for item in process_items] == [
        "观使用的结论。",
        "观使用的结论。",
    ]
    assert [item.get("status") for item in process_items] == ["running", "completed"]
    assert (
        completed_messages[-1].get("text", "") + process_items[-1].get("content", "")
        == "上海今天的实时天气我去核一下最新预报，再给你一个可直观使用的结论。"
    )


def test_provider_raw_and_summary_reasoning_are_public_thinking_events():
    events = _run(_ProviderReasoningThenFinalLLM())

    thinking_events = [
        e for e in events if getattr(e, "type", None) == "thinking_delta"
    ]

    assert [event.data for event in thinking_events] == [
        {
            "content": "厂家 raw reasoning",
            "source": "provider",
            "visibility": "timeline",
            "phase": "model",
            "provider_reasoning_type": "reasoning_content",
            "lifecycle": "delta",
        },
        {
            "content": "厂家 reasoning summary",
            "source": "provider",
            "visibility": "timeline",
            "phase": "model",
            "provider_reasoning_type": "reasoning_summary_text",
            "lifecycle": "delta",
        }
    ]
    completed = [
        event
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    assert [event.data["item"].get("text") for event in completed] == ["结论。"]


def test_length_stop_rejects_complete_tool_batch_and_recovers_without_executing_it():
    markers: list[str] = []
    tool = _InspectContextTool(markers)
    events = _run(_LengthAfterToolBlockLLM(), max_iterations=3, tool=tool)

    # Pi's assistant-message boundary wins here: any call in a length-stopped
    # message may have silently truncated arguments, so it must never execute.
    assert markers.count("tool_finished") == 0
    assert not any(
        getattr(event, "type", None) == "tool_result"
        and event.data.get("id") == "truncated_tool"
        for event in events
    )
    completed = [
        event
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    assert [event.data.get("item", {}).get("text") for event in completed] == [
        "恢复后的回答。"
    ]
    assert any(
        getattr(event, "type", None) == "done"
        and event.data.get("status") == "completed"
        for event in events
    )


def test_provider_text_phase_is_only_committed_on_completed_item():
    events = _run(_ProviderFinalPhaseLLM())

    started = [
        event for event in events if getattr(event, "type", None) == "item.started"
    ]
    deltas = [
        event
        for event in events
        if getattr(event, "type", None) == "agent_message.delta"
    ]
    completed = [
        event
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]

    assert [event.data.get("delta") for event in deltas] == [
        "这是 provider 标记的最终回答。"
    ]
    # The declared phase travels with the item start (live placement); the
    # completed item remains the only authoritative commit of answer text.
    assert all(
        event.data.get("item", {}).get("source") == "model_final" for event in started
    )
    assert all("source" not in event.data for event in deltas)
    assert len(completed) == 1
    assert completed[0].data["item"].get("text") == "这是 provider 标记的最终回答。"
    assert completed[0].data["item"].get("source") == "model_final"
    assert (
        completed[0].data.get("provider_raw", {}).get("message_phase") == "final_answer"
    )


def test_final_phase_text_before_tool_preserves_final_source_and_terminal_visibility():
    events = _run(_FinalPhaseThenToolLLM(), max_iterations=1)

    lifecycle = [
        event
        for event in events
        if getattr(event, "type", None)
        in {
            "item.started",
            "agent_message.delta",
            "item.completed",
        }
    ]

    assert [event.type for event in lifecycle] == [
        "item.started",
        "agent_message.delta",
        "item.completed",
    ]
    # The provider-declared final phase remains authoritative when the same
    # response also contains a tool call. The tool boundary must not downgrade
    # the completed item into commentary.
    assert lifecycle[0].data["item"]["source"] == "model_final"
    assert "source" not in lifecycle[1].data
    assert lifecycle[2].data["item"] == {
        "id": lifecycle[0].data["item"]["id"],
        "type": "agent_message",
        "text": "我先从官方气象源确认一下。",
        "source": "model_final",
        "status": "completed",
    }
    terminal = QueryTerminalTransaction(
        turn_ctx=SimpleNamespace(state=SimpleNamespace(reply="")),
        journal=SimpleNamespace(),
    )
    terminal.observe_runner_event(lifecycle[2])
    assert terminal.observed_visible_result is True


def test_provider_commentary_is_not_replayed_as_final_answer():
    events = _run(_ProviderCommentaryThenFinalLLM())

    deltas = [
        event
        for event in events
        if getattr(event, "type", None) == "agent_message.delta"
    ]
    completed = [
        event
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    process_items = [
        event
        for event in events
        if getattr(event, "type", None) == "agent.item"
        and event.data.get("kind") == "process_text"
    ]

    assert [event.data.get("delta") for event in deltas] == [
        "昨天是 2026 年 6 月 28 日。"
    ]
    assert [event.data.get("content") for event in process_items] == [
        "我先核对日期。",
        "我先核对日期。",
    ]
    assert [event.data.get("status") for event in process_items] == [
        "running",
        "completed",
    ]
    assert [event.data.get("source") for event in process_items] == [
        "commentary",
        "commentary",
    ]
    assert len(completed) == 1
    assert completed[0].data["item"].get("text") == "昨天是 2026 年 6 月 28 日。"


def test_provider_commentary_before_tool_keeps_commentary_process_source():
    events = _run(_ProviderCommentaryThenToolLLM(), max_iterations=1)

    process_items = [
        event
        for event in events
        if getattr(event, "type", None) == "agent.item"
        and event.data.get("kind") == "process_text"
    ]

    assert [event.data.get("content") for event in process_items] == [
        "我先核对日期。",
        "我先核对日期。",
    ]
    assert [event.data.get("status") for event in process_items] == [
        "running",
        "completed",
    ]
    assert [event.data.get("source") for event in process_items] == [
        "commentary",
        "commentary",
    ]
    assert [event.data.get("id") for event in process_items] == [
        "iter:1:model-output:commentary",
        "iter:1:model-output:commentary",
    ]


def test_partial_write_file_stream_projects_one_pending_tool_card_before_execution():
    events = _run(
        _PartialWriteFileThenFinalLLM(), max_iterations=2, tool=_WriteFileTool()
    )

    prepare_items = [
        event
        for event in events
        if getattr(event, "type", None) == "agent.item"
        and event.data.get("id") == "iter:1:tool-prepare:write-1"
    ]
    assert prepare_items == []
    pending_tool_events = [
        event
        for event in events
        if getattr(event, "type", None) == "tool_call"
        and event.data.get("id") == "write-1"
        and event.data.get("status") == "pending"
    ]
    assert pending_tool_events[0].data.get("args") == {}
    assert pending_tool_events[-1].data.get("args") == {
        "file_path": "angry-birds.html",
    }
    assert all(
        "content" not in event.data.get("args", {}) for event in pending_tool_events
    )

    pending_tool_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "tool_call"
        and event.data.get("id") == "write-1"
        and event.data.get("status") == "pending"
    )
    tool_call_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "tool_call"
        and event.data.get("id") == "write-1"
        and event.data.get("status") == "running"
    )
    tool_result_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "tool_result"
        and event.data.get("id") == "write-1"
    )

    assert pending_tool_index < tool_call_index < tool_result_index


def test_repaired_tool_json_is_rejected_as_malformed_before_required_arg_validation():
    events = _run(_MalformedWriteFileLLM(), tool=_WriteFileTool())

    result = next(
        event
        for event in events
        if getattr(event, "type", None) == "tool_result"
        and event.data.get("id") == "write-malformed"
    )
    assert result.data.get("status") == "blocked"
    assert "malformed provider JSON" in result.data.get("summary", "")
    assert "missing required argument" not in result.data.get("summary", "")


def test_complete_tool_call_consumes_trailing_done_usage_before_handoff():
    llm = _ToolCallThenFinalLLM()
    events = _run(llm, max_iterations=2)

    assert llm.done_after_tool_was_consumed is True
    tool_event_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "tool_call"
        and event.data.get("id") == "inspect_early"
    )
    result_event_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "tool_result"
        and event.data.get("id") == "inspect_early"
    )
    final_text_index = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("text") == "完成。"
    )

    assert tool_event_index < result_event_index < final_text_index
    done_events = [event for event in events if getattr(event, "type", None) == "done"]
    assert done_events[-1].data["usage"] == {
        "input_tokens": 17,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 8,
        "prompt_cache_total_tokens": 17,
        "prompt_cache_hit_rate": 47.1,
        "reasoning_output_tokens": 3,
        "input_includes_cache_read": True,
        "input_includes_cache_write": True,
        "ordinary_input_tokens": 9,
    }


def test_non_final_tool_blocks_are_merged_until_final_batch():
    llm = _NonFinalToolBlocksThenFinalBatchLLM()
    events = _run(llm)

    assert llm.trailing_done_was_consumed is True
    tool_call_ids = [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_call"
    ]
    tool_result_ids = [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_result"
    ]

    assert tool_call_ids.count("inspect_a") == 1
    assert tool_call_ids.count("inspect_b") == 1
    assert tool_result_ids.count("inspect_a") == 1
    assert tool_result_ids.count("inspect_b") == 1


def test_non_final_tool_block_without_final_batch_is_not_executed():
    events = _run(_NonFinalToolBlockThenDoneLLM())

    assert not [
        event
        for event in events
        if getattr(event, "type", None) in {"tool_call", "tool_result"}
    ]
    assert any(
        getattr(event, "type", None) == "error"
        and event.data.get("error_type") == "incomplete_tool_stream"
        for event in events
    )


def test_length_stop_with_non_final_tool_block_continues_without_executing_it():
    events = _run(_NonFinalToolBlockThenLengthLLM(), max_iterations=3)

    assert not any(
        getattr(event, "type", None) == "error"
        and event.data.get("error_type") == "incomplete_tool_stream"
        for event in events
    )
    assert not any(
        getattr(event, "type", None) == "tool_result"
        and event.data.get("id") == "inspect_truncated"
        for event in events
    )
    completed = [
        event
        for event in events
        if getattr(event, "type", None) == "item.completed"
        and event.data.get("item", {}).get("type") == "agent_message"
    ]
    assert [event.data["item"].get("text") for event in completed] == ["续写完成。"]


def test_safe_non_final_tool_block_waits_for_settled_message_and_executes_once():
    markers: list[str] = []
    events = _run(
        _NonFinalToolBlockDelayedFinalBatchLLM(markers),
        tool=_InspectContextTool(markers),
    )

    assert markers.index("final_batch") < markers.index("tool_started")
    assert markers.index("trailing_done") < markers.index("tool_started")
    assert markers.count("tool_started") == 1
    assert markers.count("tool_finished") == 1
    assert markers.index("trailing_done") > markers.index("final_batch")
    assert [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_result"
    ].count("inspect_streamed") == 1


def test_safe_final_tool_batch_waits_for_trailing_done_and_executes_once():
    markers: list[str] = []
    events = _run(
        _FinalToolBatchDelayedDoneLLM(markers),
        tool=_InspectContextTool(markers),
    )

    assert markers.index("trailing_done") < markers.index("tool_started")
    assert markers.count("tool_started") == 1
    assert markers.count("tool_finished") == 1
    assert [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_result"
    ].count("inspect_final") == 1


def test_builtin_web_search_non_final_block_waits_for_settled_message():
    markers: list[str] = []
    events = _run(
        _NonFinalWebSearchDelayedFinalBatchLLM(markers),
        tool=_WebSearchContextTool(markers),
    )

    assert markers.index("final_batch") < markers.index("web_started")
    assert markers.count("web_started") == 1
    assert markers.count("web_finished") == 1
    assert [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_result"
    ].count("web_streamed") == 1


def test_non_safe_tool_block_preserves_later_safe_tool_order():
    markers: list[str] = []
    events = _run(
        _UnsafeThenSafeNonFinalBlocksLLM(markers),
        tools=[
            _UnsafeContextTool(markers),
            _InspectContextTool(markers),
        ],
    )

    assert markers.index("final_batch") < markers.index("unsafe_started")
    assert markers.index("final_batch") < markers.index("tool_started")
    assert markers.count("tool_started") == 1
    assert [
        event.data.get("id")
        for event in events
        if getattr(event, "type", None) == "tool_result"
    ] == ["unsafe_first", "inspect_after_unsafe"]
