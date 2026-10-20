import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from backend.agent.message import UserCommand
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, permission_settings_from_config
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.utils import normalize_user_input_metadata


class Socket:
    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class Answer(LLMAdapter):
    model = "audit-input"
    provider_name = "custom"

    def __init__(self):
        self.requests = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append(deepcopy(messages))
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="fixture answer", phase="final_answer")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="completed", usage=UsageInfo())

    async def simple_chat(self, messages, **kwargs):
        return "fixture summary"


def session_for(tmp_path, monkeypatch):
    adapter = Answer()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(llm=LLMSettings(api_key="fixture", model=adapter.model, provider="custom", tool_mode="direct"),
        agent=AgentSettings(code_mode_only=False), permissions=permission_settings_from_config({}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")), {}),)))
    session = WebSocketSession(session_id="input-display", websocket=Socket(), llm=adapter,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(config.permissions, workspace_root=workspace), config=config,
        mcp_manager=None)
    session.conversation_repo = ConversationRepository(tmp_path / "conversations")
    conversation = session.conversation_repo.create_conversation(execution_cwd=str(workspace))
    session.active_conversation_id = conversation.id
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: config)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(session, "_ensure_lifecycle_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(session, "_build_conversation_tool_registry", lambda *args, **kwargs: session.tool_registry)
    monkeypatch.setattr(session, "_model_runtime_for_conversation", lambda _: None)
    session._resolve_llm_provider = lambda *args: "custom"
    session._resolve_available_models = lambda *args: [adapter.model]
    session._resolve_models_source = lambda *args: "fixture"
    return session, conversation, adapter


def input_packet(display="fix the input"):
    return {
        "content": "Quoted message (assistant):\nfixture quote\n\nfix the input\n\n<context>fixture model-only context</context>",
        "display_content": display,
        "context_refs": [
            {"kind": "file", "name": "app.py", "path": "src/app.py"},
            {"kind": "url", "name": "reference", "path": "https://example.invalid/reference"},
            {"kind": "plugin", "name": "fixture", "path": "plugin://fixture", "configName": "fixture"},
            {"kind": "browser_annotation", "name": "button", "path": "https://example.invalid/page",
             "url": "https://example.invalid/page", "note": "move this", "selector": "#button", "targetId": "target",
             "xPercent": 12, "yPercent": 24, "widthPercent": 8, "heightPercent": 10, "viewportWidth": 1000, "viewportHeight": 800},
        ],
        "quoted_message": {"id": "quote-id", "role": "assistant", "content": "fixture quote"},
        "attachments": [],
        "user_message_id": "user-input-display", "assistant_message_id": "assistant-input-display",
    }


def with_diff_comment(packet):
    comment = {
        "type": "comment", "content": [{"content_type": "text", "text": "修改这段真实行范围\n保留原注释"}],
        "position": {"path": "src/app.py", "side": "right", "line": 103, "start_line": 100},
        "localWorkspaceRoot": "C:/owned-comment-workspace", "localDiffHunk": "@@ -50,2 +100,4 @@\n-old\n+new",
    }
    return {**packet, "content": (
        "\n# Diff comments:\n\n## User Comment 1\nFile: src/app.py\n"
        "Workspace: C:/owned-comment-workspace\nSide: R\nLines: 100-103\n"
        "Diff hunk:\n```diff\n@@ -50,2 +100,4 @@\n-old\n+new\n```\n"
        "Comment:\n修改这段真实行范围\n保留原注释\n\n## My request:\n" + packet["content"] + "\n"
    ), "context_refs": [*packet["context_refs"], {
        "kind": "diff_comment", "name": "src/app.py", "path": "src/app.py", "comment": comment,
    }]}


@pytest.mark.asyncio
@pytest.mark.parametrize("display", ["fix the input", ""])
@pytest.mark.parametrize("diff_comments", [False, True])
async def test_real_ws_turn_persists_original_input_without_changing_provider_content(tmp_path, monkeypatch, display, diff_comments):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    packet = input_packet(display)
    if diff_comments:
        packet = with_diff_comment(packet)
    try:
        await asyncio.wait_for(session._run_agent(packet["content"], conversation_id=conversation.id,
            metadata={**normalize_user_input_metadata(packet), "user_message_id": packet["user_message_id"],
                      "assistant_message_id": packet["assistant_message_id"]}), 25)
        fresh = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
        user = next(row for row in fresh.transcript if row["role"] == "user")
        assert user["content"] == packet["content"]
        assert user["display_content"] == display
        assert user["context_refs"] == packet["context_refs"]
        assert user["quoted_message"] == packet["quoted_message"]
        assert adapter.requests and any(message.role == "user" and isinstance(message.content, str)
            and packet["content"] in message.content for message in adapter.requests[0])
        if display:
            assert fresh.title == display
        command = UserCommand("user_message", {**packet, "conversation_id": conversation.id})
        session.run_manager.enqueue_user_message(conversation.id, command)
        queued = session.run_manager.queued_user_message_snapshot(conversation.id)
        await asyncio.gather(*list(session.execution_cleanup_tasks))
        task = asyncio.create_task(asyncio.Event().wait())
        session._register_agent_run(conversation_id=conversation.id, task=task,
            task_id="steer-restoration-task", cancel_event=asyncio.Event())
        session._conversation_streams[conversation.id] = {"turn_id": "steer-restoration-probe", "message_id": "active-answer"}
        session.run_manager.turn_input_queue(conversation.id).begin_turn("steer-restoration-probe")
        assert session.run_manager.enqueue_user_message_as_steer(conversation.id,
            UserCommand("user_message", {**command.data, "user_message_id": "user-steer-display",
                "assistant_message_id": "assistant-steer-display"}), expected_turn_id="steer-restoration-probe", target_message_id="active-answer") is not None
        steered = session.run_manager.pending_turn_input_snapshot()
        for row in [queued[0], steered[0]]:
            assert row["display_content"] == display
            assert row["context_refs"] == packet["context_refs"]
            assert row["quoted_message"] == packet["quoted_message"]
        evidence = tmp_path / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / ("backend-empty-public.json" if not display else "backend-public.json")).write_text(
            json.dumps({"user": user, "queued": queued, "steered": steered}, indent=2) + "\n", encoding="utf-8")
    finally:
        await session.session_lifecycle.shutdown(reason="input_audit")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"display_content": None}, {"context_refs": {}}, {"context_refs": [{"kind": [], "name": "bad"}]},
    {"quoted_message": {"id": "quote", "role": "assistant", "content": 4}},
])
async def test_malformed_input_presentation_is_rejected_before_start(tmp_path, monkeypatch, invalid):
    session, conversation, _ = session_for(tmp_path, monkeypatch)
    start = AsyncMock()
    monkeypatch.setattr(session, "start_agent_run", start)
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message",
            {**input_packet(), **invalid, "conversation_id": conversation.id}))
        start.assert_not_awaited()
        assert any(row.get("type") == "command.result" and row.get("level") == "error" for row in session.ws.messages)
        assert session.conversation_repo.get_conversation(conversation.id).transcript == []
    finally:
        await session.session_lifecycle.shutdown(reason="input_audit")


@pytest.mark.asyncio
async def test_consumed_steer_admission_uses_its_original_presentation(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitRead(BaseTool):
        name = "audit_wait_read"
        read_only = True

        def get_schema(self):
            return ToolSchema(self.name, "Controlled read", {"type": "object", "properties": {}})

        async def execute(self, args, context=None):
            entered.set()
            await release.wait()
            return ToolResult("fixture read complete")

    session.tool_registry.register(WaitRead())

    async def stream(messages, tools=None, metadata=None):
        adapter.requests.append(deepcopy(messages))
        if len(adapter.requests) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent("audit-read-call", "audit_wait_read", {})])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls", usage=UsageInfo())
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="fixture answer", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="completed", usage=UsageInfo())

    monkeypatch.setattr(adapter, "stream_chat", stream)
    packet = {**with_diff_comment(input_packet("steer source display")), "conversation_id": conversation.id,
        "streaming_behavior": "steer", "user_message_id": "steer-user", "assistant_message_id": "steer-assistant"}
    run = asyncio.create_task(session._run_agent("initial provider prompt", conversation_id=conversation.id,
        metadata={"display_content": "initial display", "context_refs": [],
            "user_message_id": "initial-user", "assistant_message_id": "initial-assistant"}))
    session.run_manager.run_tasks[conversation.id] = run
    try:
        await asyncio.wait_for(entered.wait(), 10)
        packet["expected_turn_id"] = session._conversation_streams[conversation.id]["turn_id"]
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", packet))
        release.set()
        await asyncio.wait_for(run, 25)
        fresh = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
        initial = next(row for row in fresh.transcript if row["id"] == "initial-user")
        steered = next(row for row in fresh.transcript if row["id"] == "steer-user")
        assert initial["display_content"] == "initial display"
        assert steered["display_content"] == "steer source display"
        assert steered["content"] == packet["content"]
        assert steered["context_refs"] == packet["context_refs"]
        assert steered["quoted_message"] == packet["quoted_message"]
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "backend-consumed-steer.json").write_text(json.dumps(steered, indent=2) + "\n", encoding="utf-8")
    finally:
        release.set()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        session.run_manager.run_tasks.clear()
        await session.session_lifecycle.shutdown(reason="input_audit")
