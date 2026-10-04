from __future__ import annotations

import asyncio
from datetime import datetime as RealDateTime, timezone
from pathlib import Path
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.base import ToolResult
from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool


class _CapturingLLM(LLMAdapter):
    def __init__(self) -> None:
        self.message_batches: list[list[LLMMessage]] = []

    async def stream_chat(self, messages: list[LLMMessage], tools=None):
        self.message_batches.append(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="ok")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.message_batches.append(messages)
        return "ok"


class _PlanSwitchLLM(LLMAdapter):
    def __init__(self) -> None:
        self.message_batches: list[list[LLMMessage]] = []
        self.tool_names: list[set[str]] = []
        self.calls = 0

    async def stream_chat(self, messages: list[LLMMessage], tools=None):
        self.calls += 1
        self.message_batches.append(messages)
        self.tool_names.append({
            str((schema.get("function") or {}).get("name") or "")
            for schema in (tools or [])
        })
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="enter-plan",
                        name="enter_plan_mode",
                        arguments={"reason": "Need read-only planning."},
                    )
                ],
            )
            yield StreamEvent(type=StreamEventType.DONE)
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="planning")
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        self.message_batches.append(messages)
        return "ok"


def test_context_builder_adds_runtime_blocks_as_leading_instructions() -> None:
    state = AgentState(user_message="inspect")
    state.prompt_context = {
        "environment": {
            "cwd": r"C:\repo & <unsafe>",
            "workspace_roots": [r"C:\repo & <unsafe>"],
            "shell": "powershell",
            "current_date": "2026-06-28",
            "timezone": "Asia/Shanghai",
            "permission": {
                "mode": "bypass",
                "source": "unit<test>",
                "workspace_scope": "project",
                "file_system_type": "unrestricted",
            },
        },
        "collaboration_mode": "default",
        "agent_mode": "review",
        "previous_turn_aborted": True,
    }

    ctx = ContextBuilder()
    asyncio.run(ctx.start_turn("inspect", state))
    messages = asyncio.run(ctx.build(state))
    system = messages[0].content
    user = messages[1].content

    assert [message.role for message in messages] == ["system", "user"]
    assert "<environment_context>" not in system
    assert "<environment_context>" in user
    assert r"<cwd>C:\repo &amp; &lt;unsafe&gt;</cwd>" in user
    assert r"<root>C:\repo &amp; &lt;unsafe&gt;</root>" in user
    expected_shell = "powershell"
    assert f"<shell>{expected_shell}</shell>" in user
    assert "<current_date>2026-06-28</current_date>" in user
    assert "<timezone>Asia/Shanghai</timezone>" in user
    assert 'permission_profile type="bypass" source="unit&lt;test&gt;"' in user
    assert 'file_system type="unrestricted" workspace_scope="project"' in user
    assert "<collaboration_mode>" in user
    assert "# Collaboration Mode: Default" in user
    assert "<agent_mode>" in user
    assert "mode: review" in user
    assert "# Agent Mode: Review" in user
    assert "<turn_aborted>" in user
    assert "partially executed" in user
    assert user.rstrip().endswith("inspect")


def test_context_builder_does_not_invent_a_workspace_from_process_cwd(monkeypatch) -> None:
    async def unexpected_git_status(_workspace_root=None):
        raise AssertionError("git status must not run without a bound workspace")

    monkeypatch.setattr(
        "backend.agent.context.build_git_status_context_async",
        unexpected_git_status,
    )
    state = AgentState(user_message="hello")
    state.prompt_context = {"environment": {"cwd": "", "workspace_roots": []}}
    ctx = ContextBuilder()

    asyncio.run(ctx.start_turn("hello", state))
    messages = asyncio.run(ctx.build(state))
    user = messages[-1].content

    assert "<cwd></cwd>" in user
    assert "<workspace_roots />" in user
    assert str(Path.cwd()) not in user


def test_context_builder_exposes_host_resolved_user_directories(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_DESKTOP_DIR", r"C:\Desktop")
    monkeypatch.setenv("MINICODE_DOCUMENTS_DIR", r"C:\Users\ago\Documents")
    monkeypatch.setenv("MINICODE_DOWNLOADS_DIR", r"C:\Users\ago\Downloads")
    state = AgentState(user_message="create a document on my desktop")

    ctx = ContextBuilder()
    asyncio.run(ctx.start_turn(state.user_message, state))
    runtime_system = asyncio.run(ctx.build(state))[1].content

    assert "<user_directories>" in runtime_system
    assert r"<desktop>C:\Desktop</desktop>" in runtime_system
    assert r"<documents>C:\Users\ago\Documents</documents>" in runtime_system
    assert r"<downloads>C:\Users\ago\Downloads</downloads>" in runtime_system


def test_context_builder_refreshes_implicit_session_date_across_midnight(monkeypatch) -> None:
    import backend.agent.context as context_module

    class _Clock:
        day = 1

        @classmethod
        def now(cls):
            return RealDateTime(2026, 7, cls.day, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(context_module, "datetime", _Clock)
    ctx = ContextBuilder()
    state = AgentState(user_message="hello")
    asyncio.run(ctx.start_turn("hello", state))
    first = asyncio.run(ctx.build(state))
    _Clock.day = 2
    second = asyncio.run(ctx.build(state))

    assert "<current_date>2026-07-01</current_date>" in first[-1].content
    assert "<current_date>2026-07-02</current_date>" in second[-1].content
    for messages in (first, second):
        users = [message for message in messages if message.is_user_input]
        assert len(users) == 1 and users[0].content.endswith("hello")


def test_context_builder_omits_turn_aborted_block_without_abort_signal() -> None:
    state = AgentState(user_message="hello")

    ctx = ContextBuilder()
    asyncio.run(ctx.start_turn("hello", state))
    messages = asyncio.run(ctx.build(state))
    user = messages[1].content

    assert [message.role for message in messages] == ["system", "user"]
    assert "<environment_context>" in user
    assert "# Collaboration Mode: Default" in user
    assert "<turn_aborted>" not in user


def test_context_builder_keeps_runtime_instructions_transient_and_user_turn_stable() -> None:
    state = AgentState(user_message="今天有什么新闻")
    state.prompt_context = {
        "environment": {
            "cwd": r"C:\repo",
            "workspace_roots": [r"C:\repo"],
            "shell": "powershell",
            "current_date": "2026-07-01",
            "timezone": "Asia/Shanghai",
        },
        "previous_turn_aborted": True,
    }
    ctx = ContextBuilder()
    asyncio.run(ctx.start_turn(state.user_message, state))

    first = asyncio.run(ctx.build(state))
    second = asyncio.run(ctx.build(state))

    assert [message.role for message in first] == ["system", "user"]
    assert first[-1].content.endswith(state.user_message)
    assert "<environment_context>" in first[-1].content
    assert "<turn_aborted>" in first[-1].content
    assert [message.role for message in second] == ["system", "user"]
    assert second[-1].content == first[-1].content
    assert ctx.history_length == 1


def test_context_builder_freezes_old_user_runtime_bytes_and_updates_latest_turn() -> None:
    ctx = ContextBuilder()

    first_state = AgentState(user_message="first")
    first_state.prompt_context = {
        "environment": {
            "cwd": r"C:\repo",
            "workspace_roots": [r"C:\repo"],
            "shell": "powershell",
            "current_date": "2026-07-01",
            "timezone": "Asia/Shanghai",
        }
    }
    asyncio.run(ctx.start_turn("first", first_state))
    first = asyncio.run(ctx.build(first_state))
    assert "<environment_context>" in first[-1].content

    ctx.append_assistant("first reply")
    second_state = AgentState(user_message="second")
    second_state.prompt_context = {
        "environment": {
            "cwd": r"C:\repo",
            "workspace_roots": [r"C:\repo"],
            "shell": "powershell",
            "current_date": "2026-07-02",
            "timezone": "Asia/Shanghai",
        }
    }
    asyncio.run(ctx.start_turn("second", second_state))

    second = asyncio.run(ctx.build(second_state))
    user_messages = [message.content for message in second if message.role == "user"]

    # Once the first request crossed the provider boundary, its rendered bytes
    # are immutable.  Rewriting the reminder would invalidate the exact prompt
    # prefix used by OpenAI/Anthropic/Pi caches.
    assert user_messages[0] == first[-1].content
    assert "<current_date>2026-07-01</current_date>" in user_messages[0]
    assert "<environment_context>" in user_messages[-1]
    assert user_messages[-1].endswith("second")
    assert "Current time:" not in user_messages[-1]
    assert "<current_date>2026-07-02</current_date>" in user_messages[-1]


def test_context_builder_preserves_unprovenanced_runtime_like_text() -> None:
    legacy_runtime = (
        "<environment_context>\n"
        "  <cwd>C:\\legacy</cwd>\n"
        "</environment_context>\n\n"
        "<collaboration_mode>\n"
        "# Collaboration Mode: Default\n"
        "</collaboration_mode>"
    )
    ctx = ContextBuilder()
    ctx.load_snapshot(
        {
            "history": [
                {
                    "role": "user",
                    "content": (
                        f"<system-reminder>\n{legacy_runtime}\n</system-reminder>"
                        "\n\nfirst"
                    ),
                },
                {"role": "assistant", "content": "first reply"},
            ]
        }
    )

    restored = ctx.export_snapshot()["history"]
    assert restored[0]["runtime_context"] == ""

    second_state = AgentState(user_message="second")
    second_state.prompt_context = {"environment": {"cwd": r"C:\repo"}}
    asyncio.run(ctx.start_turn("second", second_state))
    messages = asyncio.run(ctx.build(second_state))
    user_messages = [message.content for message in messages if message.role == "user"]

    assert user_messages[0].startswith("<system-reminder>")
    assert "<cwd>C:\\legacy</cwd>" in user_messages[0]
    assert user_messages[-1].startswith("<system-reminder>")
    assert user_messages[-1].endswith("second")


def test_context_builder_strips_all_old_runtime_block_types() -> None:
    ctx = ContextBuilder()
    first_state = AgentState(user_message="first")
    first_state.prompt_context = {
        "collaboration_mode": "plan",
        "agent_mode": "review",
        "previous_turn_aborted": True,
    }
    asyncio.run(ctx.start_turn("first", first_state))
    first = asyncio.run(ctx.build(first_state))
    assert "<collaboration_mode>" in first[1].content
    assert "<agent_mode>" in first[1].content
    assert "<turn_aborted>" in first[1].content

    ctx.append_assistant("first reply")
    second_state = AgentState(user_message="second")
    second_state.prompt_context = {"environment": {"cwd": r"C:\repo"}}
    asyncio.run(ctx.start_turn("second", second_state))
    second = asyncio.run(ctx.build(second_state))
    user_messages = [message.content for message in second if message.role == "user"]

    assert "<collaboration_mode>" in user_messages[0]
    assert "<agent_mode>" in user_messages[0]
    assert "<turn_aborted>" in user_messages[0]
    assert "<environment_context>" in user_messages[-1]


def test_context_builder_runtime_update_on_internal_loop_when_tool_context_changes() -> None:
    ctx = ContextBuilder()
    state = AgentState(user_message="work")
    state.tool_runtime_guidance = "Runtime contract: first tool set"
    state.prompt_context["environment"] = {"cwd": r"C:\repo"}
    asyncio.run(ctx.start_turn("work", state))

    first = asyncio.run(ctx.build(state))
    assert [message.role for message in first] == ["system", "developer", "user"]
    assert "Runtime contract: first tool set" in first[1].content
    assert "<tool_runtime_context>" not in first[-1].content

    ctx.append_assistant("checking", provider_items=[])
    state.tool_runtime_guidance = "Runtime contract: second tool set"
    state.prompt_context[
        "deferred_tools_prompt_block"
    ] = "<available-deferred-tools>\n- exact_tool\n</available-deferred-tools>"

    second = asyncio.run(ctx.build(state))
    user_messages = [message.content for message in second if message.role == "user"]
    developer_messages = [
        message.content for message in second if message.role == "developer"
    ]

    assert [message.role for message in second] == [
        "system",
        "developer",
        "user",
        "assistant",
    ]
    assert user_messages[0].endswith("work")
    assert "<tool_runtime_context>" not in user_messages[0]
    assert "Runtime contract: second tool set" in developer_messages[0]
    assert "exact_tool" in developer_messages[0]


def test_context_builder_does_not_persist_runtime_only_updates_as_user_messages() -> None:
    ctx = ContextBuilder()
    first_state = AgentState(user_message="first")
    first_state.tool_runtime_guidance = "Runtime contract: first"
    asyncio.run(ctx.start_turn("first", first_state))
    asyncio.run(ctx.build(first_state))
    ctx.append_assistant("first reply")

    first_state.tool_runtime_guidance = "Runtime contract: changed"
    ctx.append_user_context(
        "<tool_runtime_context>\nlegacy standalone runtime update\n</tool_runtime_context>"
    )
    asyncio.run(ctx.build(first_state))
    ctx.append_assistant("second reply")

    second_state = AgentState(user_message="second")
    asyncio.run(ctx.start_turn("second", second_state))
    messages = asyncio.run(ctx.build(second_state))
    user_messages = [message.content for message in messages if message.role == "user"]

    assert "[runtime context omitted]" not in user_messages
    assert "Runtime contract: changed" not in "\n".join(user_messages)
    assert all("<tool_runtime_context>" not in message for message in user_messages)
    assert user_messages[-1].endswith("second")


def test_context_builder_stores_current_user_before_hook_context() -> None:
    state = AgentState(user_message="今天有什么新闻")
    state.prompt_context = {
        "environment": {
            "cwd": r"C:\repo",
            "workspace_roots": [r"C:\repo"],
            "shell": "powershell",
            "current_date": "2026-07-01",
            "timezone": "Asia/Shanghai",
        },
    }
    ctx = ContextBuilder()
    asyncio.run(ctx.start_turn(state.user_message, state))
    ctx.append_user_context("[hook] additional context")

    first = asyncio.run(ctx.build(state))
    second = asyncio.run(ctx.build(state))

    assert [message.role for message in first] == ["system", "user", "user"]
    assert "<environment_context>" in first[1].content
    assert first[1].content.endswith(state.user_message)
    assert "[hook] additional context" in first[2].content
    assert [message.content for message in second[1:]] == [
        first[1].content,
        first[2].content,
    ]


def test_context_snapshot_preserves_sanitized_provider_items() -> None:
    ctx = ContextBuilder()
    encrypted = "encrypted-state"
    ctx.append_assistant(
        "ok",
        phase="final_answer",
        provider_items=[
            {"type": "reasoning", "id": "rs_1", "encrypted_content": encrypted},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
            {
                "type": "anthropic_message",
                "content": [
                    {"type": "thinking", "thinking": "hidden", "signature": "sig"},
                    {"type": "text", "text": "ok"},
                    {
                        "type": "server_tool_use",
                        "id": "srv_1",
                        "name": "web_search",
                        "input": {"query": "q"},
                    },
                ],
            },
            {"type": "chat_reasoning", "field": "reasoning_content", "content": "opaque reasoning"},
            {"type": "message", "content": "must be dropped"},
        ],
    )

    snapshot = ctx.export_snapshot()
    history = snapshot["history"]

    assert history[0]["phase"] == "final_answer"
    assert history[0]["provider_items"] == [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": encrypted},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        {
            "type": "anthropic_message",
            "content": [
                {"type": "thinking", "thinking": "hidden", "signature": "sig"},
                {"type": "text", "text": "ok"},
                {
                    "type": "server_tool_use",
                    "id": "srv_1",
                    "name": "web_search",
                    "input": {"query": "q"},
                },
            ],
        },
        {"type": "chat_reasoning", "field": "reasoning_content", "content": "opaque reasoning"},
    ]

    restored = ContextBuilder()
    restored.load_snapshot(snapshot)
    restored_history = restored.export_snapshot()["history"]

    assert restored_history[0]["phase"] == "final_answer"
    assert restored_history[0]["provider_items"] == history[0]["provider_items"]


def test_context_snapshot_persists_message_timestamp_for_pi_replay() -> None:
    ctx = ContextBuilder()
    ctx.append_user("hello")
    original_timestamp = ctx._history[0].timestamp_ms
    assert original_timestamp is not None

    snapshot = ctx.export_snapshot()
    assert snapshot["history"][0]["timestamp_ms"] == original_timestamp

    restored = ContextBuilder()
    restored.load_snapshot(snapshot)
    assert restored._history[0].timestamp_ms == original_timestamp


def test_context_snapshot_round_trip_keeps_large_provider_visible_messages_exact() -> None:
    """Resume snapshots must not rewrite the bytes used for a cache prefix."""
    user_content = "user-start\n" + ("用户内容🙂\n" * 900) + "user-end"
    assistant_content = "assistant-start\n" + ("assistant 内容\n" * 900) + "assistant-end"
    tool_content = "tool-start\n" + ("tool output\n" * 220) + "tool-end"
    builder = ContextBuilder()
    builder.append_user(user_content)
    builder.append_assistant_tool_calls(
        [ToolCallEvent(id="call-large", name="read_file", arguments={})],
        content=assistant_content,
    )
    builder.append_tool_result(
        "call-large",
        "read_file",
        ToolResult(content=tool_content),
    )
    before = builder.export_snapshot()

    restored = ContextBuilder()
    restored.load_snapshot(before)
    after = restored.export_snapshot()

    assert [item["content"] for item in after["history"]] == [
        item["content"] for item in before["history"]
    ]
    assert after["history"][-1]["content"].endswith(tool_content + "\n</function_call_result>")
    assert "快照截断" not in str(after)
    assert "snapshot" not in str(after["history"])


def test_agent_loop_populates_plan_mode_environment_and_abort_blocks(tmp_path: Path) -> None:
    llm = _CapturingLLM()

    async def run() -> None:
        async for _event in run_agent_loop(
            user_message="say hi",
            llm=llm,
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=tmp_path,
            ),
            agent_settings=AgentSettings(max_iterations=1),
            permission_context=PermissionContext(
                mode="plan",
                source="unit",
                workspace_scope="project",
            ),
            session_context=AgentLoopSessionContext(
                    workspace_root=tmp_path,
                    session_id="session_123",
                    metadata={
                        "conversation_id": "conv_123",
                        "assistant_message_id": "assistant_123",
                        "agent_mode": "explore",
                    },
                    run_context=RunContext(
                        agent_runtime=AgentRuntime(),
                        previous_turn_aborted=True,
                    ),
                ),
        ):
            pass

    asyncio.run(run())

    assert llm.message_batches
    user = llm.message_batches[0][-1].content
    assert f"<cwd>{tmp_path}</cwd>" in user
    assert f"<root>{tmp_path}</root>" in user
    assert 'permission_profile type="plan" source="unit"' in user
    assert 'file_system type="read_only" workspace_scope="project"' in user
    assert "# Collaboration Mode: Plan" in user
    assert "# Agent Mode: Explore" in user
    assert "Map the problem space" in user
    assert "<turn_aborted>" in user
    assert "say hi" in user


def test_enter_plan_mode_updates_next_iteration_permissions(tmp_path: Path) -> None:
    llm = _PlanSwitchLLM()
    registry = ToolRegistry()
    registry.register(EnterPlanModeTool(workspace_root=tmp_path))
    registry.register(ExitPlanModeTool(workspace_root=tmp_path))

    async def run() -> None:
        async for _event in run_agent_loop(
            user_message="make a plan first",
            llm=llm,
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=tmp_path,
            ),
            agent_settings=AgentSettings(max_iterations=2),
            permission_context=PermissionContext(mode="bypass", source="unit"),
            session_context=AgentLoopSessionContext(
                workspace_root=tmp_path,
                session_id="session_plan_switch",
                metadata={
                    "agent_runtime": AgentRuntime(),
                    "conversation_id": "conv_plan_switch",
                },
            ),
        ):
            pass

    asyncio.run(run())

    assert len(llm.message_batches) >= 2
    second_runtime_system = "\n\n".join(
        message.content
        for message in llm.message_batches[1]
        if message.role in {"developer", "user"}
    )
    assert 'permission_profile type="plan"' in second_runtime_system
    assert 'file_system type="read_only"' in second_runtime_system
    assert "# Collaboration Mode: Plan" in second_runtime_system
    assert "exit_plan_mode" in llm.tool_names[1]


def test_session_timezone_is_an_iana_name_or_offset_never_localized_text() -> None:
    import re

    from backend.agent.context import local_timezone_name

    name = local_timezone_name()
    # A localized display name such as the Windows ANSI-code-page rendering
    # of "China Standard Time" is neither an IANA zone nor an offset.
    assert re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*|UTC[+-]\d{2}:\d{2}|UTC", name), name
    assert name.isascii()


@pytest.mark.parametrize("body", [
    "< think>PRIVATE_MARKER</ think>",
    "<think>PRIVATE_MARKER< / think>",
    "<thinking note='" + "x" * 80 + "'>PRIVATE_MARKER</thinking>",
    "<|im_start|><think>PRIVATE_MARKER</think><|im_end|>",
])
def test_stream_control_tags_are_independent_of_chunk_partition(body):
    from backend.agent.stream_sanitizer import ThinkingStreamSanitizer, scrub_thinking_tags

    text = "before " + body + " after"
    assert scrub_thinking_tags(text) == "before  after"
    for split in range(len(text) + 1):
        sanitizer = ThinkingStreamSanitizer()
        result = sanitizer.feed(text[:split]) + sanitizer.feed(text[split:]) + sanitizer.finish()
        assert result == "before  after", split


def test_stream_sanitizer_preserves_non_control_prose_and_memory_citation_chunks():
    from backend.agent.stream_sanitizer import ThinkingStreamSanitizer

    prose = "<internal-api> and <think-tank> and <reasoning-engine>"
    body = "<rollout_ids>source-1</rollout_ids>"
    citation = f"<minicode-memory-citation>{body}</minicode-memory-citation>"
    for split in range(len(citation) + 1):
        sanitizer = ThinkingStreamSanitizer()
        result = sanitizer.feed(prose + citation[:split]) + sanitizer.feed(citation[split:]) + sanitizer.finish()
        assert result == prose
        assert sanitizer.citations == [body]


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_native_media_identity_is_not_sent_as_an_openai_wire_key(api):
    import httpx
    import json
    from backend.config import LLMSettings
    from backend.llm.openai_adapter import OpenAIAdapter

    async def scenario():
        requests = []

        def reply(request):
            requests.append(json.loads(request.content))
            event = {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]} if api == "chat" else {
                "type": "response.completed", "response": {"status": "completed", "output_text": "Done", "output": []},
            }
            return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", wire_api=api), http_client=client)
            message = LLMMessage(role="user", content="Inspect", images=[
                {"media_type": "image/png", "data": "aW1hZ2U=", "artifact_id": "private-image-id"},
            ], documents=[{"media_type": "application/pdf", "data": "JVBERg==", "file_name": "input.pdf", "artifact_id": "private-pdf-id"}])
            try:
                events = [event async for event in adapter.stream_chat([message])]
                assert events[-1].type == StreamEventType.DONE
            finally:
                await adapter.aclose()
        assert "artifact_id" not in json.dumps(requests[0])
        assert "private-image-id" not in json.dumps(requests[0])
        parts = (requests[0]["messages"] if api == "chat" else requests[0]["input"])[0]["content"]
        assert any(part["type"] in {"image_url", "input_image"} for part in parts)
        if api == "responses":
            assert any(part["type"] == "input_file" for part in parts)

    asyncio.run(scenario())


def test_anthropic_native_media_and_tool_result_pick_only_native_form_fields():
    import json
    from backend.llm.anthropic_adapter import AnthropicAdapter

    image = {"media_type": "image/png", "data": "aW1hZ2U=", "artifact_id": "private-image-id"}
    document = {"media_type": "application/pdf", "data": "JVBERg==", "file_name": "input.pdf", "artifact_id": "private-pdf-id"}
    system, messages = AnthropicAdapter._convert_messages([
        LLMMessage(role="user", content="Inspect", images=[image], documents=[document]),
        LLMMessage(role="assistant", tool_calls=[ToolCallEvent(id="call", name="inspect", arguments={})]),
        LLMMessage(role="tool", tool_call_id="call", content="Result", images=[image]),
    ])
    assert "artifact_id" not in json.dumps(messages)
    assert "private-image-id" not in json.dumps(messages)
    native = [part for part in messages[0]["content"] if part["type"] in {"image", "document"}]
    assert len(native) == 2
    assert all(set(part["source"]) == {"type", "media_type", "data"} for part in native)
