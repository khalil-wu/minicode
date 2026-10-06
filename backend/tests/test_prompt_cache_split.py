import asyncio
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.agent.prompting import (
    PromptBuilderV2,
    PromptParts,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    clear_system_prompt_sections,
    split_sys_prompt_prefix,
)
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.tools.catalog import canonicalize_tool_schemas


class _MessagesResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, payload: str) -> None:
        self._lines = payload.splitlines()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None


class _MessagesClient:
    def __init__(self, captured: dict[str, object], payload: str | None = None) -> None:
        self.captured = captured
        self.payload = payload or (
            'data: {"type":"message_start","message":{"id":"m","usage":{"input_tokens":0,"output_tokens":0}}}\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}\n'
            'data: {"type":"message_stop"}\n'
        )

    def stream(self, method, url, **kwargs):
        self.captured.update(kwargs)
        body = kwargs.get("json")
        if isinstance(body, dict):
            self.captured.update(body)
        return _MessagesResponse(self.payload)


def _install_messages_client(adapter: AnthropicAdapter, captured: dict[str, object], payload: str | None = None) -> None:
    adapter._http_client = _MessagesClient(captured, payload)


class _EmptyStream:
    def __init__(self) -> None:
        self._events = iter(
            [
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(
                        id="msg-empty",
                        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
                        stop_reason="",
                    ),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                    usage=SimpleNamespace(output_tokens=0),
                ),
                SimpleNamespace(type="message_stop"),
            ]
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


class _BadRequest(Exception):
    status_code = 400


def test_split_system_prompt_prefix_keeps_stable_prefix_byte_exact() -> None:
    system = f"stable bytes\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nworkspace context"

    split = split_sys_prompt_prefix(system)

    assert split.stable_prefix == "stable bytes"
    assert split.dynamic_suffix == "workspace context"


def test_anthropic_system_blocks_cache_stable_and_dynamic_segments() -> None:
    system = f"stable prefix\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\ndynamic context"

    blocks = AnthropicAdapter._build_system_blocks(system)

    # Claude Code marks only the stable system prefix. The request-scoped
    # suffix remains unmarked so workspace/skill churn does not create a new
    # cache write for the stable prefix.
    assert blocks == [
        {"type": "text", "text": "stable prefix", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "dynamic context"},
    ]
    assert "cache_control" in str(blocks[0])
    assert "cache_control" not in str(blocks[1])


def test_openai_responses_cache_key_is_the_canonical_session_identity() -> None:
    """Codex and Pi route the Responses prompt cache by session identity."""
    from backend.llm.openai_adapter import _responses_prompt_cache_key

    assert _responses_prompt_cache_key({"session_id": "session-123"}) == "session-123"
    assert _responses_prompt_cache_key(
        {
            "session_id": "session-123",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
        }
    ) == "session-123"
    assert _responses_prompt_cache_key(
        {
            "session_id": "session-456",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
        }
    ) == "session-456"
    assert _responses_prompt_cache_key({"thread_id": "thread-only"}) == ""
    assert _responses_prompt_cache_key(None) == ""


def test_openai_responses_cache_key_clamps_unicode_like_pi() -> None:
    from backend.llm.openai_adapter import _responses_prompt_cache_key

    session_id = "会" * 63 + "🙂" + "尾"
    assert len(session_id) == 65
    assert _responses_prompt_cache_key({"session_id": session_id}) == session_id[:64]


def test_tool_schema_wire_order_is_core_prefix_then_mcp_and_key_canonical() -> None:
    schemas = [
        {
            "type": "function",
            "function": {
                "parameters": {
                    "properties": {"z": {"type": "string"}, "a": {"type": "string"}},
                    "required": ["z", "a"],
                    "type": "object",
                },
                "description": "mcp",
                "name": "mcp__server__lookup",
            },
        },
        {
            "function": {
                "name": "write_file",
                "parameters": {"type": "object", "properties": {}},
                "description": "write",
            },
            "type": "function",
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "read",
                "parameters": {"properties": {}, "type": "object"},
            },
        },
    ]
    reordered = [schemas[2], schemas[0], schemas[1]]

    first = canonicalize_tool_schemas(schemas)
    second = canonicalize_tool_schemas(reordered)

    assert first == second
    assert [item["function"]["name"] for item in first] == [
        "read_file",
        "write_file",
        "mcp__server__lookup",
    ]
    # Required/enum-like arrays retain semantic order; only object keys are
    # canonicalized.
    assert first[-1]["function"]["parameters"]["required"] == ["z", "a"]


def test_prompt_builder_keeps_dynamic_turn_context_out_of_stable_prefix(tmp_path: Path) -> None:
    class _WorkspaceContext:
        def get_project_summary(self) -> str:
            return "DYNAMIC WORKSPACE SUMMARY: package graph changed"

    state = SimpleNamespace(
        workspace_context=_WorkspaceContext(),
        tool_runtime_guidance="DYNAMIC TOOL GUIDANCE: only expose current tools",
        prompt_context={
            "deferred_tools_prompt_block": "<available-deferred-tools>\nDYNAMIC_DEFERRED_TOOL\n</available-deferred-tools>",
        },
    )

    parts = PromptBuilderV2().build(
        state=state,
        workspace_root=tmp_path,
        project_guidelines="DYNAMIC PROJECT GUIDELINES: local AGENTS.md",
        skill_context="DYNAMIC SKILL CONTEXT: selected skill for this turn",
        memory_context="DYNAMIC MEMORY CONTEXT: recent conversation fact",
        persistent_context="DYNAMIC PERSISTENT CONTEXT: saved user preference",
    )

    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY in parts.render_system()
    assert "You are a coding agent running in the Codex CLI" in parts.stable

    for dynamic_fact in (
        "DYNAMIC WORKSPACE SUMMARY",
        "DYNAMIC TOOL GUIDANCE",
        "DYNAMIC PROJECT GUIDELINES",
        "DYNAMIC SKILL CONTEXT",
        "DYNAMIC MEMORY CONTEXT",
        "DYNAMIC PERSISTENT CONTEXT",
        "DYNAMIC_DEFERRED_TOOL",
    ):
        assert dynamic_fact not in parts.stable

    assert "DYNAMIC WORKSPACE SUMMARY" in parts.context
    assert "DYNAMIC SKILL CONTEXT" in parts.context
    assert "DYNAMIC PROJECT GUIDELINES" in parts.project_guidelines
    assert "DYNAMIC PROJECT GUIDELINES" not in parts.render_system()
    assert "DYNAMIC TOOL GUIDANCE" not in parts.context
    assert "DYNAMIC_DEFERRED_TOOL" not in parts.context


def test_conditional_rules_follow_project_guidelines_in_user_context(tmp_path: Path) -> None:
    state = SimpleNamespace(workspace_context=None, prompt_context={})
    builder = PromptBuilderV2()
    common = dict(
        state=state,
        workspace_root=tmp_path,
        project_guidelines="PROJECT GUIDE",
        skill_context="SKILL CATALOG",
        memory_context="MEMORY",
        persistent_context="PERSISTENT FACT",
        git_status_context="GIT SNAPSHOT",
    )
    first = builder.build_sections(**common, conditional_rules="RULE A")
    second = builder.build_sections(**common, conditional_rules="RULE B")
    assert first[-1].name == "conditional_rules"
    assert second[-1].name == "conditional_rules"
    assert [section.content for section in first[:-1]] == [section.content for section in second[:-1]]
    assert "GIT SNAPSHOT" in first[-2].content
    assert first[-1].content == "RULE A"
    assert second[-1].content == "RULE B"
    first_system = PromptParts.from_sections(first).render_system()
    second_system = PromptParts.from_sections(second).render_system()
    first_blocks = AnthropicAdapter._build_system_blocks(first_system)
    second_blocks = AnthropicAdapter._build_system_blocks(second_system)
    assert first_blocks == second_blocks
    assert "RULE A" not in first_system
    assert "RULE B" not in second_system
    assert "cache_control" in first_blocks[0]
    first_user = PromptParts.from_sections(first).render_user_instructions()
    second_user = PromptParts.from_sections(second).render_user_instructions()
    assert first_user.index("PROJECT GUIDE") < first_user.index("RULE A")
    assert second_user.endswith("RULE B\n</INSTRUCTIONS>")
    rules_only = PromptParts(stable="STABLE", conditional_rules="RULE A")
    assert "RULE A" not in rules_only.render_system()
    assert "RULE A" in rules_only.render_user_instructions()


def test_context_builder_sends_project_rules_as_user_instructions(tmp_path: Path, monkeypatch) -> None:
    from backend.agent import instruction_discovery

    monkeypatch.setattr(instruction_discovery, "load_matching_project_rules", lambda *args, **kwargs: "MATCHED RULE")
    context = ContextBuilder()
    monkeypatch.setattr(context, "_get_project_guidelines", lambda root: "PROJECT GUIDE")
    state = AgentState(user_message="continue", workspace_root=tmp_path)
    parts = context._build_prompt_parts(state, tmp_path)
    assert parts.conditional_rules == "MATCHED RULE"
    assert parts.project_guidelines == "PROJECT GUIDE"
    asyncio.run(context.start_turn(state.user_message, state))
    messages = asyncio.run(context.build(state))
    assert "PROJECT GUIDE" not in messages[0].content
    assert "MATCHED RULE" not in messages[0].content
    instructions = next(message for message in messages if "PROJECT GUIDE" in message.content)
    assert instructions.role == "user"
    assert instructions.content.index("PROJECT GUIDE") < instructions.content.index("MATCHED RULE")
    assert messages.index(instructions) < len(messages) - 1
    assert messages[-1].content.endswith("continue")
    assert [section["name"] for section in state.prompt_context["prompt_section_summary"]["sections"]][-1] == "conditional_rules"


def test_read_only_subagent_keeps_project_instructions_without_parent_memory(tmp_path: Path) -> None:
    class _WorkspaceContext:
        def get_project_summary(self) -> str:
            return "SCOPED WORKSPACE SUMMARY"

    state = SimpleNamespace(
        workspace_context=_WorkspaceContext(),
        prompt_context={"subagent": "explore"},
        user_message="Inspect the parser.",
        task_summary="",
        retrieved_chunks=[],
        loop_guidance=[],
    )

    parts = PromptBuilderV2().build(
        state=state,
        workspace_root=tmp_path,
        project_guidelines="PARENT PROJECT GUIDELINES",
        skill_context="TASK SPECIFIC SKILL",
        memory_context="PARENT CONVERSATION MEMORY",
        persistent_context="PARENT PERSISTENT FACTS",
    )

    assert "SCOPED WORKSPACE SUMMARY" in parts.context
    assert "TASK SPECIFIC SKILL" in parts.context
    assert "PARENT PROJECT GUIDELINES" in parts.project_guidelines
    assert "PARENT CONVERSATION MEMORY" not in parts.context
    assert "PARENT PERSISTENT FACTS" not in parts.context


def test_context_builder_puts_tool_runtime_context_in_dynamic_developer_instructions() -> None:
    state = AgentState(user_message="continue")
    state.tool_runtime_guidance = "DYNAMIC TOOL GUIDANCE: only expose current tools"
    state.prompt_context[
        "deferred_tools_prompt_block"
    ] = "<available-deferred-tools>\nDYNAMIC_DEFERRED_TOOL\n</available-deferred-tools>"

    context = ContextBuilder()
    asyncio.run(context.start_turn(state.user_message, state))
    messages = asyncio.run(context.build(state))
    system = messages[0].content
    developer = messages[1].content
    user = messages[-1].content

    assert "DYNAMIC TOOL GUIDANCE" not in system
    assert "DYNAMIC_DEFERRED_TOOL" not in system
    assert "<tool_runtime_context>" in developer
    assert "DYNAMIC TOOL GUIDANCE" in developer
    assert "DYNAMIC_DEFERRED_TOOL" in developer
    assert user.startswith("<system-reminder>")
    assert user.endswith("continue")
    assert "<tool_runtime_context>" not in user


def test_prompt_builder_stable_section_cache_ignores_workspace(tmp_path: Path) -> None:
    clear_system_prompt_sections()
    first = PromptBuilderV2().build(
        state=SimpleNamespace(prompt_context={}),
        workspace_root=tmp_path / "repo-a",
    )
    second = PromptBuilderV2().build(
        state=SimpleNamespace(prompt_context={}),
        workspace_root=tmp_path / "repo-b",
    )

    assert first.stable == second.stable
    assert str(tmp_path / "repo-a") not in first.stable
    assert str(tmp_path / "repo-b") not in second.stable


def test_anthropic_stream_payload_uses_block_level_cache_control() -> None:
    captured: dict[str, object] = {}
    adapter = AnthropicAdapter(api_key="test")
    _install_messages_client(adapter, captured)

    messages = [
        LLMMessage(
            role="system",
            content=f"stable prefix\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\ndynamic context",
        ),
        LLMMessage(role="user", content="hello"),
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    events = asyncio.run(_collect_events(adapter.stream_chat(messages, tools)))

    assert events[-1].type == StreamEventType.DONE
    # No top-level cache_control; it lives on system blocks, last message, last tool.
    assert "cache_control" not in captured
    system_blocks = captured["system"]
    assert isinstance(system_blocks, list)
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system_blocks[1]
    # Last message should have cache_control on its last content block.
    api_messages = captured["messages"]
    last_msg = api_messages[-1]
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Last tool should have cache_control.
    api_tools = captured["tools"]
    assert api_tools[-1]["cache_control"] == {"type": "ephemeral"}
    summary = events[-1].raw["request_summary"]
    assert summary["wire_api"] == "anthropic_messages"
    assert summary["instructions_len"] == sum(
        len(str(block.get("text") or "")) for block in system_blocks
    )
    assert len(summary["instructions_hash"]) == 12
    assert len(summary["instructions_full_hash"]) == 12
    assert summary["instructions_full_hash"] != summary["instructions_hash"]
    assert summary["tools_len"] == 1
    assert summary["tool_names"] == ["read_file"]
    assert len(summary["tools_hash"]) == 12
    assert summary["request_params"]["cache_control_present"] is True
    # stable system + last tool + last message
    assert summary["request_params"]["cache_breakpoints"] == 3
    assert summary["request_params"]["system_blocks"] == 2
    assert "stable prefix" not in str(summary)
    assert "dynamic context" not in str(summary)


def test_anthropic_stream_payload_maps_request_metadata_to_hashed_user_id() -> None:
    captured: dict[str, object] = {}
    adapter = AnthropicAdapter(api_key="test")
    _install_messages_client(adapter, captured)

    events = asyncio.run(
        _collect_events(
            adapter.stream_chat(
                [LLMMessage(role="user", content="hello")],
                metadata={
                    "conversation_id": "conv_123",
                    "cwd": "C:/repo",
                    "turn_id": "assistant_123",
                },
            )
        )
    )

    expected_user_id = f"minicode-{hashlib.sha256(b'conv_123').hexdigest()[:32]}"
    assert events[-1].type == StreamEventType.DONE
    assert captured["metadata"] == {"user_id": expected_user_id}
    assert "C:/repo" not in str(captured["metadata"])
    assert "assistant_123" not in str(captured["metadata"])


def test_anthropic_stream_surfaces_cache_control_rejection_without_retry() -> None:
    import httpx

    adapter = AnthropicAdapter(api_key="test")

    requests: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            request=request,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "cache_control unsupported"}},
        )
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    events = asyncio.run(
        _collect_events(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert events[-1].type == StreamEventType.ERROR
    assert len(requests) == 1
    assert "cache_control" in requests[0].content.decode()


def test_anthropic_tool_result_blocks_use_only_supported_cache_markers() -> None:
    captured: dict[str, object] = {}
    adapter = AnthropicAdapter(api_key="test")
    _install_messages_client(adapter, captured)

    messages = [
        LLMMessage(role="system", content="system prompt"),
        LLMMessage(role="user", content="do something"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCallEvent(id="tc_001", name="read_file", arguments={"path": "/tmp"})],
        ),
        LLMMessage(role="tool", content="file contents", name="read_file", tool_call_id="tc_001"),
        LLMMessage(role="user", content="now what?"),
    ]

    asyncio.run(_collect_events(adapter.stream_chat(messages)))

    api_messages = captured["messages"]
    # The last message (user "now what?") should have cache_control.
    last_msg = api_messages[-1]
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][-1].get("cache_control") == {"type": "ephemeral"}

    found_tool_result = False
    for msg in api_messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                assert "cache_reference" not in block
                found_tool_result = True

    assert found_tool_result
    assert "cache_edits" not in captured


def test_anthropic_tool_schema_cache_keys_by_name_and_input_schema() -> None:
    """Match Claude Code's name + input-schema cache identity.

    Description-only drift keeps the first render stable, while a changed
    input contract for the same tool name must receive a fresh cache entry.
    """
    adapter = AnthropicAdapter(api_key="test")
    tools_v1 = [
        {"type": "function", "function": {
            "name": "read_file", "description": "Read file v1",
            "parameters": {"type": "object", "properties": {}},
        }}
    ]
    tools_v2 = [
        {"type": "function", "function": {
            "name": "read_file", "description": "Read file v2 CHANGED",
            "parameters": {"type": "object", "properties": {}},
        }}
    ]
    tools_v3 = [
        {"type": "function", "function": {
            "name": "read_file", "description": "Read file with path",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }}
    ]

    result1 = adapter._convert_tools_cached(tools_v1)
    result2 = adapter._convert_tools_cached(tools_v2)
    result3 = adapter._convert_tools_cached(tools_v3)

    assert len(result1) == 1
    assert len(result2) == 1
    assert len(result3) == 1
    # Capability/routing instructions must follow the current description.
    assert result2[0]["description"] == "Read file v2 CHANGED"
    assert result2[0] is not result1[0]
    assert adapter._convert_tools_cached(tools_v2)[0] is result2[0]
    # Same name with a new input contract must not reuse the stale schema.
    assert result3[0]["description"] == "Read file with path"
    assert result3[0]["input_schema"] == tools_v3[0]["function"]["parameters"]
    assert result3[0] is not result1[0]

    # Clearing the cache lets the new description through.
    adapter.clear_tool_schema_cache()
    result4 = adapter._convert_tools_cached(tools_v2)
    assert result4[0]["description"] == "Read file v2 CHANGED"


async def _collect_events(stream) -> list[StreamEvent]:
    return [event async for event in stream]


def _gateway_sse() -> str:
    frames = [
        ("message_start", '{"type":"message_start","message":{"id":"m1","usage":{"input_tokens":5,"output_tokens":0}}}'),
        ("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'),
        ("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}'),
        ("content_block_stop", '{"type":"content_block_stop","index":0}'),
        ("message_delta", '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}'),
        ("message_stop", '{"type":"message_stop"}'),
    ]
    return "".join(f"event: {name}\ndata: {payload}\n\n" for name, payload in frames)


def _anthropic_status_error(status: int, body: str):
    import httpx

    request = httpx.Request("POST", "https://gw.example.test/v1/messages")
    response = httpx.Response(status, request=request, text=body)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected an HTTPStatusError")


_CACHE_CONTROL_REJECTION = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"Extra inputs are not permitted: messages.0.content.0.cache_control"}}'
)
