import asyncio
import logging
import json
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.run_context import RunContext
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget, load_config
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent, ToolCallStartEvent
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.main import app
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.mcp.client import MCPClient
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class _HungLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        await asyncio.sleep(1.0)
        if False:
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return ""


class _DoneLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _ReflectableDoneLLM(LLMAdapter):
    def __init__(self, simple_replies: list[str] | None = None) -> None:
        self.simple_replies = list(simple_replies or [])
        self.simple_calls: list[list[LLMMessage]] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="draft reply")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_calls.append(messages)
        if self.simple_replies:
            return self.simple_replies.pop(0)
        return "LGTM"


class _ToolCallingLLM(LLMAdapter):
    def __init__(self, tool_calls) -> None:
        self._tool_calls = tool_calls
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=self._tool_calls)
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _PersistentRepeatedWebSearchLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self.tool_names_by_call: list[list[str]] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        self.tool_names_by_call.append([
            str((tool.get("function") or {}).get("name") or "")
            for tool in (tools or [])
        ])
        available = set(self.tool_names_by_call[-1])
        if "web_search" in available:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id=f"web_{self.calls}",
                        name="web_search",
                        arguments={"query": "same retry query"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="answered from available evidence")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "answered from available evidence"


class _PreambleToolCallingLLM(LLMAdapter):
    def __init__(self, preamble: str, tool_calls) -> None:
        self._preamble = preamble
        self._tool_calls = tool_calls
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self._preamble)
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=self._tool_calls)
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _PreambleThenToolLLM(LLMAdapter):
    def __init__(self, tool_call: ToolCallEvent, preamble: str = "我先查一下最新信息。") -> None:
        self.tool_call = tool_call
        self.preamble = preamble
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.preamble)
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[self.tool_call])
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="最终答案。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "最终答案。"


class _LongDraftThenAnswerLLM(LLMAdapter):
    def __init__(self, draft: str, final_text: str) -> None:
        self.draft = draft
        self.final_text = final_text

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.draft)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.final_text)
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return self.draft + self.final_text


class _LongDraftThenToolLLM(LLMAdapter):
    def __init__(self, draft: str, tool_call: ToolCallEvent, final_text: str) -> None:
        self.draft = draft
        self.tool_call = tool_call
        self.final_text = final_text
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.draft)
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[self.tool_call])
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.final_text)
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return self.final_text


class _ToolThenPostToolPreambleThenToolLLM(LLMAdapter):
    def __init__(self, post_tool_note: str) -> None:
        self.post_tool_note = post_tool_note
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(id="read_first", name="read_file", arguments={"file_path": "README.md"})
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        if self.calls == 2:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.post_tool_note)
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(id="read_second", name="read_file", arguments={"file_path": "pyproject.toml"})
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _PreambleIncompleteToolStartLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="checking before tool")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            tool_call_start=ToolCallStartEvent(id="partial_tool", name="read_file"),
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _PreambleIncompleteToolStartErrorLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="checking before tool")
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START,
            tool_call_start=ToolCallStartEvent(id="partial_tool", name="read_file"),
        )
        yield StreamEvent(type=StreamEventType.ERROR, content="provider disconnected")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _ProviderThinkingLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(
            type=StreamEventType.THINKING_CHUNK,
            content="raw private provider chain of thought",
        )
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="final answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "final answer"


class _MaxOutputThenContinueLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self.user_prompts: list[str] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        self.user_prompts.append(messages[-1].content)
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="part one, ")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="max_tokens")
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="part two")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _RepeatingToolLLM(LLMAdapter):
    def __init__(self, repeats: int, final_text: str = "done") -> None:
        self.repeats = repeats
        self.final_text = final_text
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls <= self.repeats:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id=f"repeat_{self.calls}",
                        name="read_file",
                        arguments={"file_path": "src/main.py"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return

        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=self.final_text)
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return self.final_text


class _WeakFinalThenConcreteLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self.simple_prompts: list[str] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="read_once",
                        name="read_file",
                        arguments={"file_path": "README.md"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        if self.calls == 2:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="如果你要，我现在就继续。")
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="已根据现有文件继续处理。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.simple_prompts.append(messages[-1].content)
        return "ok"


class _FutureActionThenConcreteLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="read_once",
                        name="read_file",
                        arguments={"file_path": "README.md"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        if self.calls == 2:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="我接下来会重新读取文件并继续优化。")
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="已完成下一步优化。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "ok"


class _ContinueTailThenConcreteLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="read_once",
                        name="read_file",
                        arguments={"file_path": "README.md"},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        if self.calls == 2:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="已继续优化，并且验证通过。\n\n如果你后面还想继续，我可以接着帮你做移动端触控优化。",
            )
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="已完成优化并验证通过。")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "ok"


class _ToolThenTimeoutLLM(LLMAdapter):
    def __init__(self, tool_calls) -> None:
        self._tool_calls = tool_calls
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=self._tool_calls)
            yield StreamEvent(type=StreamEventType.DONE)
            return
        await asyncio.sleep(0.05)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="late")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.calls += 1
        assert any(msg.role == "tool" for msg in messages), "tool results must reach simple_chat fallback"
        return "根据工具结果，北京今天晴，气温 20°C 到 30°C。"


class _ToolThenStreamErrorLLM(LLMAdapter):
    def __init__(self, tool_calls) -> None:
        self._tool_calls = tool_calls
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=self._tool_calls)
            yield StreamEvent(type=StreamEventType.DONE)
            return
        yield StreamEvent(type=StreamEventType.ERROR, content="503 service unavailable")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        assert any(msg.role == "tool" for msg in messages), "tool results must reach simple_chat fallback"
        return "已经获取到的信息：web_fetch:1: https://docs.example/reference"


class _ImmediateStreamErrorLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        yield StreamEvent(type=StreamEventType.ERROR, content="503 service unavailable")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _ReasoningOnlyThenAnswerLLM(LLMAdapter):
    """A reasoning-heavy gateway truncates before emitting any visible action."""

    def __init__(self) -> None:
        self.calls = 0
        self.user_prompts: list[str] = []
        self._max_tokens = 16_384
        self.seen_caps: list[int] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        self.seen_caps.append(self._max_tokens)
        self.user_prompts.append(messages[-1].content if messages else "")
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.THINKING_CHUNK,
                content="provider reasoning that fills the output budget",
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="finished after recovery")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _RaisedStreamErrorLLM(LLMAdapter):
    """Raises at the adapter boundary instead of yielding an ERROR event."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        raise RuntimeError("RemoteProtocolError: Server disconnected without sending a response")
        yield  # pragma: no cover - keep this an async generator for the adapter contract

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


class _PlannerLLM(LLMAdapter):
    def __init__(self) -> None:
        self.simple_prompts: list[str] = []
        self.stream_prompts: list[str] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        prompt = messages[-1].content
        self.stream_prompts.append(prompt)
        if "Current step 2/2: Update the frontend state" in prompt:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Updated frontend state.")
        elif "Current step 1/2: Inspect the backend flow" in prompt:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Checked backend flow.")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Completed step.")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        prompt = messages[-1].content
        self.simple_prompts.append(prompt)
        if "Return valid JSON" in prompt:
            return json.dumps(
                {
                    "summary": "Break the work into backend and frontend passes.",
                    "steps": [
                        {
                            "title": "Inspect the backend flow",
                            "instruction": "Inspect the backend flow and identify the required code changes.",
                        },
                        {
                            "title": "Update the frontend state",
                            "instruction": "Update the frontend state handling to match the backend changes.",
                        },
                    ],
                },
                ensure_ascii=False,
            )
        return "Final synthesis: backend and frontend changes are aligned."


class _HighHistoryContextBuilder:
    def __init__(self) -> None:
        self.history_length = 5

    def append_user(self, _message: str) -> None:
        return None

    def needs_compaction(self) -> bool:
        return False

    async def build(self, user_message: str, state) -> list[LLMMessage]:
        return [LLMMessage(role="user", content=user_message)]

    def get_budget_snapshot(self, state, tool_schemas) -> dict[str, object]:
        return {}

    def append_assistant(self, _message: str) -> None:
        return None

    def append_assistant_tool_calls(self, _tool_calls) -> None:
        return None

    def append_tool_result(self, _tool_call_id, _tool_name, _result) -> None:
        return None


class _CountingTool(BaseTool):
    permission = None

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=f"{self.name} tool",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict[str, object], context=None) -> ToolResult:
        self.calls += 1
        return ToolResult(content=f"{self.name}:{self.calls}:{args}")


class _ContextGuardedTool(BaseTool):
    name = "context_guarded"
    description = "Requires the runtime permission context to be forwarded."
    permission = None

    def __init__(self) -> None:
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    def check_permission(self, args=None, context=None) -> PermissionLevel | None:
        if context is not None:
            return PermissionLevel.ALWAYS_DENY
        return None

    async def execute(self, args: dict[str, object], context=None) -> ToolResult:
        self.calls += 1
        return ToolResult(content="should not execute")


async def _collect_events(stream) -> list:
    return [event async for event in stream]


def _final_events(events) -> list:
    """Return authoritative terminal-answer items, excluding commentary."""
    return [
        event
        for event in events
        if event.type == "item.completed"
        and isinstance(event.data.get("item"), dict)
        and event.data["item"].get("type") == "agent_message"
        and event.data["item"].get("source") in {"model_final", "reply", "recovery", "partial", None}
    ]


def _final_chunks(events) -> list[str]:
    return [str(event.data["item"].get("text") or "") for event in _final_events(events)]


def _final_text(events) -> str:
    return "".join(_final_chunks(events))


def _thinking_events(
    events,
    *,
    source: str | None = None,
    visibility: str | None = None,
) -> list[dict]:
    chunks = [event.data for event in events if event.type == "thinking_delta"]
    if source is not None:
        chunks = [chunk for chunk in chunks if chunk.get("source") == source]
    if visibility is not None:
        chunks = [chunk for chunk in chunks if chunk.get("visibility") == visibility]
    return chunks


def _agent_items(events, *, kind: str | None = None, source: str | None = None) -> list[dict]:
    items = [event.data for event in events if event.type == "agent.item"]
    if kind is not None:
        items = [item for item in items if item.get("kind") == kind]
    if source is not None:
        items = [item for item in items if item.get("source") == source]
    return items


def test_run_agent_loop_applies_turn_error_budget_to_existing_state(tmp_path) -> None:
    state = AgentState(user_message="hello")
    state.max_total_retries = 99

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=_DoneLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
                agent_settings=AgentSettings(max_iterations=1, turn_error_budget=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    assert state.max_total_retries == 2
    assert _final_text(events) == "done"


def test_run_agent_loop_uses_effective_permission_context_for_execution(tmp_path) -> None:
    tool = _ContextGuardedTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState(user_message="call guarded tool")
    llm = _ToolCallingLLM([
        ToolCallEvent(id="guarded_1", name="context_guarded", arguments={})
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="call guarded tool",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(
                    PermissionSettings(auto_allow=["context_guarded"]),
                    tmp_path,
                ),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert tool.calls == 0
    assert tool_results[0]["status"] == "blocked"
    assert state.tool_calls[-1].tool_name == "context_guarded"
    assert state.tool_calls[-1].status == "blocked"


def test_run_agent_loop_retries_after_distinct_policy_blocks(tmp_path) -> None:
    class _ManyBlockedThenFinalLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call: list[list[LLMMessage]] = []

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(id="blocked_list", name="list_files", arguments={"directory": str(tmp_path)}),
                        ToolCallEvent(id="blocked_py", name="glob_files", arguments={"directory": str(tmp_path), "pattern": "*.py"}),
                        ToolCallEvent(id="blocked_md", name="glob_files", arguments={"directory": str(tmp_path), "pattern": "*.md"}),
                        ToolCallEvent(id="blocked_json", name="glob_files", arguments={"directory": str(tmp_path), "pattern": "*.json"}),
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

            assert any("outside allowlist" in message.content for message in messages)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="I can adjust strategy.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    class _PolicyBlocksChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return "Path is outside allowlist"

    llm = _ManyBlockedThenFinalLLM()
    registry = ToolRegistry()
    registry.register(_CountingTool("list_files"))
    registry.register(_CountingTool("glob_files"))

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="describe this project",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_PolicyBlocksChecker(),
                agent_settings=AgentSettings(
                    max_iterations=3,
                ),
                token_budget=TokenBudget(),
                state=AgentState(user_message="describe this project"),
            )
        )
    )

    assert [event.data["status"] for event in events if event.type == "tool_result"] == [
        "blocked",
        "blocked",
        "blocked",
        "blocked",
    ]
    assert llm.calls > 1
    assert not any(
        event.type == "error" and event.data.get("error_type") == "stagnant"
        for event in events
    )


def test_run_agent_loop_times_out_hung_stream(monkeypatch) -> None:
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=_HungLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    stream_timeout_seconds=0.01,
                    stream_max_attempts=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    assert any(
        event.type == "error" and event.data.get("error_type") == "timeout"
        for event in events
    )
    # The loop now closes every turn with exactly one terminal boundary
    # (``agent.terminal.intent`` + ``done``); a stopped turn is reported as
    # ``done{status="failed"}`` rather than by omitting ``done``. Asserting the
    # absence of ``done`` encoded the pre-terminal-projection wire shape.
    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["status"] == "failed"
    assert done[0].data.get("reason") == "timeout"




def test_run_agent_loop_streams_final_text_as_retractable_draft_then_commits() -> None:
    llm = _ReflectableDoneLLM(simple_replies=["should not be used"])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    committed = _final_events(events)

    assert len(committed) == 1
    assert committed[0].data["item"]["text"] == "draft reply"
    assert committed[0].data["item"]["source"] == "model_final"


def test_run_agent_loop_emits_accepted_final_reply_before_done() -> None:
    class _ChunkedFinalLLM(LLMAdapter):
        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="chunk one ")
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="chunk two")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=_ChunkedFinalLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    text_events = _final_chunks(events)
    done_index = next(index for index, event in enumerate(events) if event.type == "done")
    first_text_index = next(index for index, event in enumerate(events) if event.type == "item.completed")

    assert text_events == ["chunk one chunk two"]
    assert first_text_index < done_index


def test_run_agent_loop_keeps_initial_preamble_out_of_timeline_thinking() -> None:
    class _AllowPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect",
                llm=_PreambleToolCallingLLM(
                    "checking first ",
                    [ToolCallEvent(id="read_1", name="read_file", arguments={"file_path": "README.md"})],
                ),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AllowPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    # The preamble never enters the committed final-answer projection.
    assert _final_text(events) == "done"


def test_run_agent_loop_streams_post_tool_process_note_before_next_tool() -> None:
    class _AllowPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    note = "我看完 README 了，接下来查配置来确认启动方式。"
    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect",
                llm=_ToolThenPostToolPreambleThenToolLLM(note),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AllowPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_indices = [
        index for index, event in enumerate(events)
        if event.type == "tool_call" and event.data.get("name") == "read_file"
    ]
    assert len(tool_indices) == 2
    # Post-tool narration is excluded from the committed final answer.
    assert _final_text(events) == "done"


def test_run_agent_loop_keeps_short_preamble_out_of_final_answer_stream(tmp_path) -> None:
    tool_call = ToolCallEvent(id="search_1", name="web_search", arguments={"query": "北京天气"})
    llm = _PreambleThenToolLLM(tool_call)
    registry = ToolRegistry()
    state = AgentState(user_message="今天北京天气如何")

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="今天北京天气如何",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    # Preamble is process narration, not final-answer content.
    assert _final_text(events) == "最终答案。"


def test_run_agent_loop_keeps_long_preamble_out_of_final_answer_stream(tmp_path) -> None:
    preamble = (
        "我会先读取相关文件和最近的活动记录，确认聊天消息、Activity 面板和输入框的连接方式，"
        "再根据参考界面把过程输出整理成可折叠的执行轨迹，最后只把真正结论放到最终回复里。"
    )
    tool_call = ToolCallEvent(id="read_1", name="read_file", arguments={"file_path": "README.md"})
    llm = _PreambleThenToolLLM(tool_call, preamble=preamble)
    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="继续优化 UI",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=AgentState(user_message="继续优化 UI"),
            )
        )
    )

    final_deltas = _final_chunks(events)

    # Long preamble remains inspectable as process narration.
    assert final_deltas == ["最终答案。"]


def test_run_agent_loop_keeps_long_draft_out_of_final_answer_until_tool_turn_completes(tmp_path) -> None:
    draft = (
        "我先检查一下实时来源，再把页面中相关的过程动作整理成可见的步骤，"
        "最后再给你一个简短结论。"
    )
    tool_call = ToolCallEvent(id="search_1", name="web_search", arguments={"query": "北京天气"})
    llm = _LongDraftThenToolLLM(draft, tool_call, "北京今天有小雨。")
    state = AgentState(user_message="今天北京天气如何")

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="今天北京天气如何",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    # Draft before a tool call is kept in the process area only.
    assert _final_text(events) == "北京今天有小雨。"


def test_run_agent_loop_streams_long_answer_after_draft_threshold(tmp_path) -> None:
    draft = "这是一个较长的前置说明，用来确认阈值触发后不会丢掉前半段文本。"
    final_tail = "接下来才是可直接展示给用户的答案主体。"
    llm = _LongDraftThenAnswerLLM(draft, final_tail)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="继续优化",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
                state=AgentState(user_message="继续优化"),
            )
        )
    )

    final_chunks = _final_chunks(events)

    assert "".join(final_chunks) == draft + final_tail
    assert final_chunks == [draft + final_tail]




def test_run_agent_loop_planner_mode_no_longer_uses_orchestrator_chain() -> None:
    llm = _PlannerLLM()
    state = AgentState(user_message="Refactor backend and frontend flow")

    async def approval_handler(tool_call_id: str) -> dict[str, str]:
        raise AssertionError(f"unexpected approval request: {tool_call_id}")
        return {"answer": "yes"}

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="Refactor backend and frontend flow",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=2, agent_mode="planner"),
                token_budget=TokenBudget(),
                state=state,
                approval_handler=approval_handler,
            )
        )
    )

    combined = _final_text(events)
    assert "Execution plan" not in combined
    assert "Execution cancelled" not in combined
    assert combined == "Completed step."
    assert llm.simple_prompts == []
    assert len(llm.stream_prompts) == 1
    assert any(event.type == "done" for event in events)


def test_run_agent_loop_auto_does_not_orchestrate_complex_request() -> None:
    llm = _PlannerLLM()
    state = AgentState(user_message="1. Inspect backend flow\n2. Update frontend state")

    async def approval_handler(tool_call_id: str) -> dict[str, str]:
        return {"answer": "approve"}

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="1. Inspect backend flow\n2. Update frontend state",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=2,
                    agent_mode="auto",
                ),
                token_budget=TokenBudget(),
                state=state,
                approval_handler=approval_handler,
            )
        )
    )

    assert not any(event.type == "ask_user" for event in events)
    assert llm.simple_prompts == []
    assert len(llm.stream_prompts) == 1
    combined = _final_text(events)
    assert "Execution plan" not in combined
    assert any(event.type == "done" for event in events)


def test_run_agent_loop_executes_adjacent_auto_tools_in_parallel() -> None:
    tracker = {"active": 0, "max_active": 0}

    class _ParallelTool(BaseTool):
        permission = None
        read_only = True

        def __init__(self, name: str, *, delay: float) -> None:
            self.name = name
            self.delay = delay

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=f"{self.name} tool",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            tracker["active"] += 1
            tracker["max_active"] = max(tracker["max_active"], tracker["active"])
            await asyncio.sleep(self.delay)
            tracker["active"] -= 1
            return ToolResult(content=f"{self.name} done")

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    first_tool = _ParallelTool("read_file", delay=0.02)
    # ``glob_files`` (not ``list_files``) is the core-exposure listing tool.
    # ``list_files`` is deliberately deferred, so calling it without a
    # tool_search activation is blocked before execution and the batch would
    # never fan out -- that gate is not what this regression measures.
    second_tool = _ParallelTool("glob_files", delay=0.02)
    llm = _ToolCallingLLM(
        [
            ToolCallEvent(id="tool_1", name="read_file", arguments={"file_path": "README.md"}),
            ToolCallEvent(id="tool_2", name="glob_files", arguments={"pattern": "*"}),
        ]
    )
    registry = ToolRegistry()
    registry.register(first_tool)
    registry.register(second_tool)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="parallel tools",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    assert any(event.type == "done" for event in events)
    assert tracker["max_active"] == 2


def test_run_agent_loop_times_out_parallel_tools(monkeypatch) -> None:
    class _NeverFinishesTool(BaseTool):
        permission = None
        read_only = True

        def __init__(self, name: str) -> None:
            self.name = name
            self.timeout_seconds = 0.01

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=f"{self.name} tool",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            await asyncio.sleep(10)
            return ToolResult(content=f"{self.name} done")

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    llm = _ToolCallingLLM(
        [
            ToolCallEvent(id="slow_read", name="read_file", arguments={"file_path": "README.md"}),
            # Core-exposure listing tool; ``list_files`` is deferred and would
            # be blocked by the toolset gate before ever reaching execution,
            # so it could never reach the parallel timeout path under test.
            ToolCallEvent(id="slow_list", name="glob_files", arguments={"pattern": "*"}),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NeverFinishesTool("read_file"))
    registry.register(_NeverFinishesTool("glob_files"))

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="parallel timeout",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert [result["id"] for result in tool_results[:2]] == ["slow_read", "slow_list"]
    assert all(result["is_error"] is True for result in tool_results[:2])
    assert all(result["status"] == "timeout" for result in tool_results[:2])
    assert all(result["limitation"] == "timeout" for result in tool_results[:2])
    assert all("timed out" in result["summary"] for result in tool_results[:2])


def test_llm_error_sanitizer_hides_raw_provider_concurrency_text() -> None:
    raw = "LLM API 调用失败: Concurrency limit exceeded for account, please retry later"
    classification = classify_llm_error(raw)

    message = sanitize_llm_error_message(raw, classification)

    assert classification.provider_error_type == "busy"
    assert "Concurrency limit exceeded" not in message
    assert "LLM API" not in message
    assert message == "模型暂时繁忙或达到并发限制，请稍后重试或切换模型。（provider=busy）"


def test_run_agent_loop_preserves_tool_result_order_when_parallel_tools_finish_out_of_order() -> None:
    class _OrderedParallelTool(BaseTool):
        permission = None

        def __init__(self, name: str, *, delay: float) -> None:
            self.name = name
            self.delay = delay

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=f"{self.name} tool",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            await asyncio.sleep(self.delay)
            return ToolResult(content=f"{self.name} done")

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    llm = _ToolCallingLLM(
        [
            ToolCallEvent(id="slow", name="read_file", arguments={"file_path": "slow.md"}),
            ToolCallEvent(id="fast", name="list_files", arguments={"directory": "."}),
        ]
    )
    registry = ToolRegistry()
    registry.register(_OrderedParallelTool("read_file", delay=0.03))
    registry.register(_OrderedParallelTool("list_files", delay=0.0))

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="ordered parallel tools",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    result_ids = [event.data["id"] for event in events if event.type == "tool_result"]
    assert result_ids[:2] == ["slow", "fast"]


def test_run_agent_loop_recovers_from_empty_run_command_with_model_guidance(tmp_path) -> None:
    from backend.tools.command_tool import RunCommandTool

    class _RecordingRunCommandTool(RunCommandTool):
        def __init__(self) -> None:
            super().__init__(ArtifactStore())
            self.calls: list[dict[str, object]] = []

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(content=f"command={args['command']}\ncwd={tmp_path}")

    class _EmptyThenValidCommandLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call: list[list[LLMMessage]] = []

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[ToolCallEvent(id="cmd_empty", name="run_command", arguments={})],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            if self.calls == 2:
                assert any(
                    "run_command" in message.content and "command" in message.content
                    for message in messages
                )
                assert any(
                    tc.id == "cmd_empty"
                    for message in messages
                    for tc in (message.tool_calls or [])
                )
                assert any(message.role == "tool" and message.tool_call_id == "cmd_empty" for message in messages)
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="cmd_pwd",
                            name="run_command",
                            arguments={"command": "pwd"},
                        )
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

            assert any(message.role == "tool" and message.tool_call_id == "cmd_pwd" for message in messages)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="You are in the workspace.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    llm = _EmptyThenValidCommandLLM()
    run_tool = _RecordingRunCommandTool()
    registry = ToolRegistry()
    registry.register(run_tool)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="what folder am I in",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
                state=AgentState(user_message="what folder am I in"),
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    final_text = _final_text(events)

    assert [result["id"] for result in tool_results[:2]] == ["cmd_empty", "cmd_pwd"]
    assert tool_results[0]["status"] == "blocked"
    assert tool_results[0]["is_error"] is True
    assert tool_results[1]["status"] == "success"
    assert run_tool.calls == [{"command": "pwd"}]
    assert final_text == "You are in the workspace."
    assert llm.calls == 3


def test_desktop_no_workspace_turn_disables_local_workspace_tools() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    class _RecordingShellTool(BaseTool):
        name = "run_command"

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Run a shell command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(
                content="天气候选，20度。",
                display_summary=f"Searched web: {args.get('query')}",
                result_kind="search",
                evidence_type="candidate",
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(content="should not execute")

    shell_tool = _RecordingShellTool()
    registry = ToolRegistry()
    registry.register(shell_tool)
    llm = _ToolCallingLLM([
        ToolCallEvent(id="cmd_pwd", name="run_command", arguments={"command": "pwd"}),
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="what folder am I in",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
                state=AgentState(user_message="what folder am I in"),
                session_context=AgentLoopSessionContext(
                    workspace_root=None,
                    run_context=RunContext(requires_explicit_workspace=True),
                ),
            )
        )
    )

    tool_result = next(event.data for event in events if event.type == "tool_result")

    assert shell_tool.calls == []
    assert tool_result["id"] == "cmd_pwd"
    assert tool_result["status"] == "blocked"
    assert tool_result["is_error"] is True
    assert tool_result["display_summary"] == "Tool unavailable"
    assert "unavailable under the active execution capability policy" in tool_result["summary"]


def test_desktop_no_workspace_bypass_keeps_local_workspace_tools_available() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    class _RecordingShellTool(BaseTool):
        name = "run_command"

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Run a shell command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(content="executed")

    shell_tool = _RecordingShellTool()
    registry = ToolRegistry()
    registry.register(shell_tool)
    llm = _ToolCallingLLM([
        ToolCallEvent(id="cmd_pwd", name="run_command", arguments={"command": "pwd"}),
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="what folder am I in",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
                state=AgentState(user_message="what folder am I in"),
                session_context=AgentLoopSessionContext(
                    permission_context=PermissionContext(mode="bypass", source="test"),
                    workspace_root=None,
                    metadata={"requires_explicit_workspace": True},
                ),
            )
        )
    )

    tool_result = next(event.data for event in events if event.type == "tool_result")

    assert shell_tool.calls == [{"command": "pwd"}]
    assert tool_result["id"] == "cmd_pwd"
    assert tool_result["status"] == "success"
    assert "executed" in tool_result["summary"]


def test_run_agent_loop_keeps_tool_lifecycle_on_tool_events() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    read_tool = _CountingTool("read_file")
    registry = ToolRegistry()
    registry.register(read_tool)
    llm = _ToolCallingLLM(
        [ToolCallEvent(id="read_progress", name="read_file", arguments={"file_path": "README.md"})]
    )

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect progress",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    progress = [event.data for event in events if event.type == "agent.progress"]
    tool_calls = [event.data for event in events if event.type == "tool_call"]
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "read_progress"
    assert tool_calls[0]["name"] == "read_file"
    assert tool_calls[0]["args"] == {"file_path": "README.md"}
    assert tool_calls[0]["phase"] == "tool"
    assert tool_calls[0]["display_hint"] == "read_file"
    assert tool_calls[0]["group_id"] == "iter:1"
    assert tool_calls[0]["step_id"] == "read_progress"
    assert tool_results and tool_results[0]["id"] == "read_progress"
    assert tool_results[0]["is_error"] is False
    assert tool_results[0]["phase"] == "tool"
    assert tool_results[0]["group_id"] == "iter:1"
    assert tool_results[0]["step_id"] == "read_progress"
    assert tool_results[0]["result_kind"] == "generic"


def test_run_agent_loop_hides_preamble_when_model_calls_tools() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    llm = _PreambleToolCallingLLM(
        "我继续落地优化：先看看文件。\n",
        [ToolCallEvent(id="read_preamble", name="read_file", arguments={"file_path": "README.md"})],
    )

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect progress",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    assert _final_text(events) == "done"


def test_run_agent_loop_requires_explicit_model_query_for_empty_web_search() -> None:
    expected_query = "today Beijing weather"

    class _RepairableWebSearchTool(BaseTool):
        name = "web_search"
        description = "Search web"
        read_only = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(
                content="北京天气：晴，20℃。",
                display_summary=f"Searched web: {args.get('query')}",
                result_kind="search",
                evidence_type="candidate",
            )

    class _EmptyWebSearchThenAnswerLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[ToolCallEvent(id="web_empty", name="web_search", arguments={})],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

            if self.calls == 2:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="web_explicit",
                            name="web_search",
                            arguments={"query": expected_query},
                        )
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

            assert any(
                msg.role == "assistant"
                and msg.tool_calls
                and msg.tool_calls[0].arguments == {"query": expected_query}
                for msg in messages
            )
            assert any(msg.role == "tool" and msg.tool_call_id == "web_explicit" for msg in messages)
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content="Based on unverified search snippets, on 2026-06-01 Beijing may be sunny, 20C.",
            )
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    web_tool = _RepairableWebSearchTool()
    registry = ToolRegistry()
    registry.register(web_tool)
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="today Beijing weather",
                llm=_EmptyWebSearchThenAnswerLLM(),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                    permission_checker=_AutoPermissionChecker(),
                    agent_settings=AgentSettings(
                        max_iterations=3,
                        stream_retry_delay_seconds=0,
                    ),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_calls = [event.data for event in events if event.type == "tool_call"]
    tool_results = [event.data for event in events if event.type == "tool_result"]
    text = _final_text(events)

    assert web_tool.calls == [{"query": expected_query}]
    assert tool_calls[0]["args"] == {}
    assert tool_results[0]["status"] == "blocked"
    assert "missing required argument" in tool_results[0]["summary"]
    assert tool_calls[1]["args"] == {"query": expected_query}
    assert tool_results[1]["status"] == "success"
    assert "2026-06-01" in text
    assert "unverified search snippets" in text


def test_run_agent_loop_requires_explicit_model_url_for_empty_web_fetch() -> None:
    class _SearchTool(BaseTool):
        name = "web_search"
        description = "Search web"
        read_only = True

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            return ToolResult(
                content=(
                    "Search returned 1 candidate source.\n"
                    "[1] Beijing Weather\n"
                    "    URL: https://weather.example/beijing\n"
                    "    Snippet: Beijing weather candidate."
                ),
                result_kind="search",
                evidence_type="candidate",
            )

    class _FetchTool(BaseTool):
        name = "web_fetch"
        description = "Fetch page"
        read_only = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            )

        async def execute(self, args: dict[str, object], context=None) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(
                content="Beijing weather fetched page",
                source_url=str(args.get("url")),
                result_kind="web",
                evidence_type="fetched",
            )

    class _SearchThenEmptyFetchLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, object]] | None = None,
        ):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="search_weather",
                            name="web_search",
                            arguments={"query": "weather in Beijing city today"},
                        )
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            if self.calls == 2:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[ToolCallEvent(id="fetch_empty", name="web_fetch", arguments={})],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            if self.calls == 3:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="fetch_explicit",
                            name="web_fetch",
                            arguments={"url": "https://weather.example/beijing"},
                        )
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return

            assert any(msg.role == "tool" and msg.tool_call_id == "fetch_explicit" for msg in messages)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="On 2026-06-04, Beijing weather answer")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "unused"

    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    fetch_tool = _FetchTool()
    registry = ToolRegistry()
    registry.register(_SearchTool())
    registry.register(fetch_tool)

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="weather in Beijing city today",
                llm=_SearchThenEmptyFetchLLM(),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(
                    max_iterations=4,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    text = _final_text(events)

    assert fetch_tool.calls == [{"url": "https://weather.example/beijing"}]
    assert [result["status"] for result in tool_results] == ["success", "blocked", "success"]
    assert "url" in tool_results[1]["summary"]
    assert text == "On 2026-06-04, Beijing weather answer"


def test_run_agent_loop_discards_tool_preamble_draft() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    llm = _PreambleToolCallingLLM(
        "tool preamble",
        [ToolCallEvent(id="read_preamble_draft", name="read_file", arguments={"file_path": "README.md"})],
    )

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect progress",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    text = _final_text(events)

    # Tool preamble is not committed as answer text.
    assert text == "done"


def test_run_agent_loop_does_not_commit_incomplete_tool_start_preamble() -> None:
    state = AgentState(user_message="inspect progress")
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect progress",
                llm=_PreambleIncompleteToolStartLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    process_items = _agent_items(events, kind="process_text", source="model")
    error = next(event.data for event in events if event.type == "error")

    assert process_items == []
    assert _final_chunks(events) == []
    assert error["error_type"] == "incomplete_tool_stream"
    assert error["recoverable"] is True
    assert state.reply == ""


def test_run_agent_loop_does_not_degrade_partial_tool_stream_to_answer() -> None:
    state = AgentState(user_message="inspect progress")
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect progress",
                llm=_PreambleIncompleteToolStartErrorLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    errors = [event.data for event in events if event.type == "error"]

    assert _final_chunks(events) == []
    assert errors[-1]["error_type"] == "incomplete_tool_stream"
    assert state.reply == ""


def test_run_agent_loop_hides_raw_provider_reasoning() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="answer",
                llm=_ProviderThinkingLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=1),
                token_budget=TokenBudget(),
            )
        )
    )

    text = _final_text(events)
    thinking = [event.data for event in events if event.type == "thinking_delta"]

    assert text == "final answer"
    # MiniCode's default surface shows provider summaries, not raw chain of
    # thought. Raw reasoning must not enter either the final answer or the
    # public timeline.
    assert "raw private provider chain of thought" not in text
    assert thinking == []


def test_run_agent_loop_continues_partial_text_after_output_limit() -> None:
    llm = _MaxOutputThenContinueLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="write the full answer",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
            )
        )
    )

    chunks = _final_chunks(events)
    assert llm.calls == 2
    assert chunks == ["part one, ", "part two"]
    assert "Output token limit hit. Resume directly" in llm.user_prompts[-1]
    assert not any(event.type == "error" and event.data.get("error_type") == "max_output" for event in events)
    assert [event.data.get("status") for event in events if event.type == "done"] == ["completed"]


def test_reasoning_only_truncation_continues_without_fabricating_partial_answer() -> None:
    llm = _ReasoningOnlyThenAnswerLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="inspect and fix the issue",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
            )
        )
    )

    assert llm.calls == 2
    assert llm.seen_caps == [16_384, 16_384]
    assert _final_text(events) == "finished after recovery"
    assert not any(event.type == "error" and event.data.get("error_type") == "max_output" for event in events)


class _ExactDefaultCapThenAnswerLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self._max_tokens = 16_384
        self.seen_caps: list[int] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.calls += 1
        self.seen_caps.append(self._max_tokens)
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="long visible draft")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="completed")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "unused"


def test_exact_16384_default_cap_continues_partial_answer() -> None:
    llm = _ExactDefaultCapThenAnswerLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="write a long answer",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
            )
        )
    )

    assert llm.calls == 2
    assert llm.seen_caps == [16_384, 16_384]
    assert _final_text(events) == "long visible draftcompleted"


def test_run_agent_loop_stop_hook_feedback_uses_explicit_turn_boundary(monkeypatch) -> None:
    class _AlwaysFeedbackHook:
        def __init__(self) -> None:
            self.calls = 0
            self.active_values: list[bool] = []

        def has_hooks(self, event) -> bool:
            return getattr(event, "value", "") == "stop"

        def bind_runtime(self, **_kwargs) -> None:
            return None

        async def run_stop(
            self,
            user_message: str,
            draft_reply: str,
            tool_results=None,
            stop_hook_active: bool = False,
        ):
            from backend.hooks.manager import HookResult

            self.calls += 1
            self.active_values.append(stop_hook_active)
            return HookResult(feedback="revise again")

    class _DraftLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages: list[LLMMessage], tools=None):
            self.calls += 1
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=f"draft {self.calls}")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "draft"

    hook = _AlwaysFeedbackHook()
    llm = _DraftLLM()
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="answer",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                run_context=RunContext(hook_manager=hook),
            )
        )
    )

    assert hook.calls == 4
    assert llm.calls == 4
    assert hook.active_values == [False, True, True, True]
    assert _final_text(events) == ""
    assert sum(event.type == "done" for event in events) == 1
    done = next(event for event in events if event.type == "done")
    assert done.data["status"] == "failed"
    assert done.data["reason"] == "max_iterations"
    assert any(event.type == "error" for event in events)


def test_run_agent_loop_commits_model_final_reply_without_regex_retry() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    state = AgentState(user_message="继续优化")
    llm = _WeakFinalThenConcreteLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="继续优化",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    text = _final_text(events)

    assert text == "如果你要，我现在就继续。"
    assert llm.calls == 2
    assert state.stopped_reason == "completed"


def test_run_agent_loop_does_not_discard_final_draft_for_regex_retry() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    state = AgentState(user_message="continue")
    llm = _WeakFinalThenConcreteLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="continue",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    text = _final_text(events)
    committed = [event.data for event in _final_events(events)]

    assert len(committed) == 1
    assert committed[0]["item"]["text"] == text
    assert committed[0]["item"]["source"] == "model_final"
    assert text == "如果你要，我现在就继续。"
    assert llm.calls == 2
    assert state.stopped_reason == "completed"


def test_run_agent_loop_uses_tool_results_when_final_model_stream_times_out() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("web_fetch"))
    state = AgentState(user_message="今天北京天气如何")
    llm = _ToolThenTimeoutLLM([
        ToolCallEvent(
            id="fetch_weather",
            name="web_fetch",
            arguments={"url": "https://weather.example/beijing"},
        )
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="今天北京天气如何",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(
                    max_iterations=3,
                    stream_timeout_seconds=0.01,
                    stream_max_attempts=0,
                ),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    all_text = _final_text(events)
    assert "LLM response timed out" not in all_text
    assert all_text == ""
    assert any(event.type == "error" for event in events)
    # One terminal boundary per turn: a stopped turn still emits ``done``, now
    # carrying status/reason instead of being omitted entirely.
    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["status"] == "failed"
    assert done[0].data.get("reason") == "timeout"


def test_run_agent_loop_retries_clean_idle_timeout_before_any_output() -> None:
    class _TimeoutThenAnswerLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.05)
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="reconnected")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "unused"

    llm = _TimeoutThenAnswerLLM()
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    stream_timeout_seconds=0.01,
                    stream_max_attempts=3,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    assert llm.calls == 2
    assert _final_text(events) == "reconnected"
    assert not any(event.type == "error" for event in events)
    assert any(event.type == "done" for event in events)


def test_run_agent_loop_does_not_replay_timeout_after_partial_text() -> None:
    class _PartialThenTimeoutLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="partial")
            await asyncio.sleep(0.05)
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "unused"

    llm = _PartialThenTimeoutLLM()
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    stream_timeout_seconds=0.01,
                    stream_max_attempts=3,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    assert llm.calls == 1
    assert sum(
        event.data.get("delta", "").count("partial")
        for event in events
        if event.type == "agent_message.delta"
    ) <= 1
    assert any(event.type in {"error", "done"} for event in events)


def test_run_agent_loop_uses_tool_results_when_final_model_stream_errors() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("web_fetch"))
    state = AgentState(user_message="summarize this reference page")
    context_builder = ContextBuilder()
    llm = _ToolThenStreamErrorLLM([
        ToolCallEvent(
            id="fetch_weather",
            name="web_fetch",
            arguments={"url": "https://docs.example/reference"},
        )
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="summarize this reference page",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(
                    max_iterations=3,
                    # Keep this regression deterministic and fast. The
                    # production default follows MiniCode's ten-attempt
                    # exponential retry policy; this test only exercises the
                    # terminal boundary after a bounded stream-error ladder.
                    stream_max_attempts=3,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
                context_builder=context_builder,
                state=state,
            )
        )
    )

    assert _final_text(events) == ""
    assert llm.calls == 5
    # One terminal boundary per turn: the stopped turn reports itself through
    # ``done{status="failed"}`` rather than by suppressing ``done``.
    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["status"] == "failed"
    assert done[0].data.get("reason") == "api_error"
    snapshot = context_builder.export_snapshot()
    assert not any(
        "Use the tool results above to answer the user's original question" in message.get("content", "")
        for message in snapshot["history"]
        if message.get("role") == "user"
    )


def test_run_agent_loop_keeps_stream_errors_failed_without_tool_results() -> None:
    runtime_spans: list[dict] = []

    async def scenario():
        async def emit_event(event_type: str, payload: dict) -> None:
            if event_type == "runtime.span":
                runtime_spans.append(payload)

        return await _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=_ImmediateStreamErrorLLM(),
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
                emit_event=emit_event,
            )
        )

    events = asyncio.run(scenario())

    assert any(event.type == "error" and event.data.get("error_type") == "api" for event in events)
    # One terminal boundary per turn; the failure surfaces as done status.
    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["status"] == "failed"
    assert done[0].data.get("reason") == "api_error"
    started_ids = [
        payload["span_id"]
        for payload in runtime_spans
        if payload.get("event") == "provider.request.started"
    ]
    first_event_ids = [
        payload["span_id"]
        for payload in runtime_spans
        if payload.get("event") == "provider.first_event"
    ]
    terminal_ids = [
        payload["span_id"]
        for payload in runtime_spans
        if payload.get("event") in {
            "provider.request.completed",
            "provider.request.failed",
            "provider.request.cancelled",
        }
    ]
    assert len(started_ids) > 1
    assert first_event_ids == started_ids
    assert terminal_ids == started_ids


def test_run_agent_loop_provider_retries_are_independent_of_turn_error_budget() -> None:
    llm = _ImmediateStreamErrorLLM()
    state = AgentState(user_message="hello")

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=8,
                    turn_error_budget=1,
                    stream_max_attempts=4,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    done = [event for event in events if event.type == "done"]
    # Provider transport retries are owned by ``stream_max_attempts``.  They
    # must not consume the loop-level recovery fuse, which is reserved for
    # context rebuilds, stop-hook feedback, and other loop-owned recoveries.
    assert llm.calls == 5  # initial request + four provider retries
    assert state.total_retries == 0
    assert state.stopped_reason == "api_error"
    assert len(done) == 1
    assert done[0].data.get("status") == "failed"
    assert done[0].data.get("reason") == "api_error"
    assert not any(
        event.type == "item.completed"
        for event in events
    )


def test_run_agent_loop_normalizes_raised_provider_errors_into_bounded_retries() -> None:
    llm = _RaisedStreamErrorLLM()
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="hello",
                llm=llm,
                tool_registry=ToolRegistry(),
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings()),
                agent_settings=AgentSettings(
                    max_iterations=1,
                    # The production default follows MiniCode's ten
                    # retries; this regression pins the older shaped
                    # three-retry boundary explicitly.
                    stream_max_attempts=3,
                    stream_retry_delay_seconds=0,
                ),
                token_budget=TokenBudget(),
            )
        )
    )

    # Three retries after the initial request are explicit for this test.
    assert llm.calls == 4
    assert any(event.type == "error" for event in events)
    # One terminal boundary per turn; the exhausted retry ladder surfaces as
    # done status rather than as a missing ``done`` event.
    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["status"] == "failed"
    assert done[0].data.get("reason") == "api_error"


def test_run_agent_loop_commits_future_action_reply_after_tools_without_regex_retry() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    state = AgentState(user_message="继续优化")
    llm = _FutureActionThenConcreteLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="继续优化",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    text = _final_text(events)

    # The kernel is model-driven: a user-visible answer is never rejected
    # merely because it starts with a future-action phrase.
    assert "接下来会重新读取文件并继续优化。" in text
    assert text == "我接下来会重新读取文件并继续优化。"
    assert llm.calls == 2
    assert state.stopped_reason == "completed"


def test_run_agent_loop_commits_continue_offer_tail_after_tools_without_regex_retry() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    registry = ToolRegistry()
    registry.register(_CountingTool("read_file"))
    state = AgentState(user_message="继续优化")
    llm = _ContinueTailThenConcreteLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="继续优化",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    text = _final_text(events)

    assert "如果你后面还想继续" in text
    assert text == "已继续优化，并且验证通过。\n\n如果你后面还想继续，我可以接着帮你做移动端触控优化。"
    assert llm.calls == 2
    assert state.stopped_reason == "completed"


def test_run_agent_loop_allows_repeated_successful_tool_calls() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    read_tool = _CountingTool("read_file")
    registry = ToolRegistry()
    registry.register(read_tool)
    state = AgentState(user_message="repeat read")

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="repeat read",
                llm=_RepeatingToolLLM(repeats=2),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=3),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[0]["is_error"] is False
    assert tool_results[1]["is_error"] is False
    assert [record.status for record in state.tool_calls] == ["success", "success"]
    assert any(event.type == "done" for event in events)


def test_run_agent_loop_executes_duplicate_successful_tool_calls_in_same_model_step() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    read_tool = _CountingTool("read_file")
    registry = ToolRegistry()
    registry.register(read_tool)
    llm = _ToolCallingLLM(
        [
            ToolCallEvent(id="read_1", name="read_file", arguments={"file_path": "src/main.py"}),
            ToolCallEvent(id="read_2", name="read_file", arguments={"file_path": "src/main.py"}),
        ]
    )

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="same batch duplicate",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert len(tool_results) >= 2
    assert [result["id"] for result in tool_results[:2]] == ["read_1", "read_2"]
    assert tool_results[0]["is_error"] is False
    assert tool_results[1]["is_error"] is False


def test_run_agent_loop_executes_same_provider_tool_id_twice_in_one_model_step() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    read_tool = _CountingTool("read_file")
    registry = ToolRegistry()
    registry.register(read_tool)
    llm = _ToolCallingLLM(
        [
            ToolCallEvent(id="read_duplicate", name="read_file", arguments={"file_path": "src/a.py"}),
            ToolCallEvent(id="read_duplicate", name="read_file", arguments={"file_path": "src/b.py"}),
        ]
    )

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="same provider id batch",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert [result["id"] for result in tool_results[:2]] == [
        "read_duplicate",
        "read_duplicate:dup2",
    ]
    assert all(result["is_error"] is False for result in tool_results[:2])


def test_run_agent_loop_allows_similar_web_searches_for_independent_evidence() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    web_tool = _CountingTool("web_search")
    registry = ToolRegistry()
    registry.register(web_tool)
    state = AgentState(user_message="find today's LLM paper")
    state.record_tool_call(
        "web_search",
        {"query": "2026 May arxiv LLM paper"},
        "搜索 '2026 May arxiv LLM paper' 未返回结果。",
    )
    state.record_tool_call(
        "web_search",
        {"query": "site:arxiv.org 2026 May LLM paper"},
        "No results returned.",
    )
    llm = _ToolCallingLLM([
        ToolCallEvent(
            id="web_1",
            name="web_search",
            arguments={"query": "arxiv 2026 May LLM paper"},
        )
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="find today's LLM paper",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert web_tool.calls == 1
    assert tool_results[0]["is_error"] is False
    assert tool_results[0]["status"] == "success"
    assert state.tool_calls[-1].status == "success"
    assert "web_search" not in state.disabled_tools


def test_run_agent_loop_allows_distinct_web_search_after_soft_budget() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    web_tool = _CountingTool("web_search")
    registry = ToolRegistry()
    registry.register(web_tool)
    state = AgentState(user_message="search a lot")
    for index in range(6):
        state.record_tool_call(
            "web_search",
            {"query": f"confirmed source {index}"},
            f"Result {index}",
        )
    llm = _ToolCallingLLM([
        ToolCallEvent(
            id="web_budget",
            name="web_search",
            arguments={"query": "one more source"},
        )
    ])

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="search a lot",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert web_tool.calls == 1
    assert tool_results[0]["is_error"] is False
    assert "\u641c\u7d22\u9884\u7b97\u5df2\u8fbe" not in tool_results[0]["summary"]
    assert state.tool_calls[-1].status == "success"
    assert "web_search" not in state.disabled_tools


def test_run_agent_loop_keeps_web_tools_available_after_repeated_similar_searches() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    web_tool = _CountingTool("web_search")
    registry = ToolRegistry()
    registry.register(web_tool)
    state = AgentState(user_message="search a lot")
    state.record_tool_call(
        "web_search",
        {"query": "same retry query"},
        "Result 1",
    )
    state.record_tool_call(
        "web_search",
        {"query": "retry same query"},
        "Result 2",
    )
    llm = _PersistentRepeatedWebSearchLLM()

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="search a lot",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=4),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    assert web_tool.calls == 4
    assert llm.calls == 4
    assert all("web_search" in names for names in llm.tool_names_by_call)
    assert state.stopped_reason == "max_iterations"
    assert state.terminal_status == "failed"
    assert any(event.type == "error" for event in events)
    assert "web_search" not in state.disabled_tools
    assert "web_fetch" not in state.disabled_tools
    assert state.reply == ""
    assert [event.data.get("status") for event in events if event.type == "done"] == ["failed"]
    assert _final_text(events) == ""


def test_low_stakes_meme_turn_limits_prefetched_web_searches() -> None:
    class _AutoPermissionChecker:
        def check(self, tool_name: str, args=None, context=None):
            from backend.tools.base import PermissionLevel

            return PermissionLevel.AUTO

        def get_denial_reason(self, tool_name: str, args=None, context=None):
            return None

    web_tool = _CountingTool("web_search")
    fetch_tool = _CountingTool("web_fetch")
    registry = ToolRegistry()
    registry.register(web_tool)
    registry.register(fetch_tool)
    llm = _ToolCallingLLM([
        ToolCallEvent(id="web_1", name="web_search", arguments={"query": "雪碧巧乐兹 梗"}),
        ToolCallEvent(id="web_2", name="web_search", arguments={"query": "张雪峰 巧乐兹"}),
        ToolCallEvent(id="web_3", name="web_search", arguments={"query": "雪碧巧乐兹 来源"}),
        ToolCallEvent(id="fetch_1", name="web_fetch", arguments={"url": "https://www.zhihu.com/question/123"}),
    ])

    state = AgentState(user_message="雪碧巧乐兹是什么梗")
    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="雪碧巧乐兹是什么梗",
                llm=llm,
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=_AutoPermissionChecker(),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                state=state,
            )
        )
    )

    tool_results = [event.data for event in events if event.type == "tool_result"]
    assert web_tool.calls == 3
    assert fetch_tool.calls == 1
    assert not any("\u641c\u7d22\u9884\u7b97\u5df2\u8fbe" in result["summary"] for result in tool_results)
    assert "web_fetch" not in state.disabled_tools


def test_cost_tracker_uses_only_explicit_provider_cost() -> None:
    from backend.llm.cost_tracker import CostTracker

    tracker = CostTracker.get_instance()
    tracker.reset()
    unknown_cost = tracker.record_usage(1_000_000, 0, model_id="unpriced-model")
    explicit_cost = tracker.record_usage(
        1_000_000,
        0,
        model_id="provider-priced-model",
        cost_usd=2.75,
    )

    assert unknown_cost is None
    assert explicit_cost == 2.75
    summary = tracker.get_summary()
    assert summary["total_cost_usd"] == 2.75
    # An unpriced model must report "unknown", not a fabricated $0: reporting
    # 0.0 made every OpenAI/DeepSeek/gateway request look free and left a
    # configured cost ceiling silently unarmed.
    assert summary["unpriced_requests"] == 1
    assert summary["priced_requests"] == 1
    assert summary["cost_complete"] is False


def test_llm_blocked_gateway_error_is_fatal() -> None:
    classification = classify_llm_error("Error: LLM API 调用失败: Your request was blocked.")

    assert classification.fatal is True
    assert classification.retryable is False
    assert classification.error_type == "blocked"
