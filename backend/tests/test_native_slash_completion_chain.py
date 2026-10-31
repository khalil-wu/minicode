from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

import pytest
import pytest_asyncio

from backend.agent.message import AgentEvent, UserCommand
from backend.artifact.store import ArtifactStore
from backend.commands.catalog import get_composer_command_catalog
from backend.commands.slash_commands import (
    _handle_compact, _handle_goal, _handle_permissions, _handle_plan, _handle_skills,
    build_slash_command_handler,
)
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest_asyncio.fixture
async def native_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_ENABLE_NETWORK_MARKETPLACE", "0")
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    session = WebSocketSession(
        "native-completion", Socket(), SimpleNamespace(), ArtifactStore(storage_dir=tmp_path / "artifacts"),
        ToolRegistry(), PermissionChecker(PermissionSettings()), AppConfig(llm=LLMSettings(api_key="")),
        mcp_manager=None,
    )
    session.session_lifecycle.schedule_task_runtime_update = lambda: None
    session._ensure_extension_commands_for_conversation = AsyncMock()
    session.start_agent_run = AsyncMock()
    monkeypatch.setattr("backend.hooks.runtime.run_session_end_hook", AsyncMock())
    session.command_registry.clear_slash_handlers()
    for name, handler in {"goal": _handle_goal, "permissions": _handle_permissions,
                          "plan": _handle_plan, "skills": _handle_skills, "compact": _handle_compact}.items():
        session.command_registry.register_slash(f"/{name}", handler)
    conversation = session.conversation_repo.create_conversation(permission_mode="confirm")
    session.active_conversation_id = conversation.id
    session.permission_context = session.permission_checker.build_context(mode="confirm")
    yield session
    await session.session_lifecycle.shutdown(reason="native_completion_fixture")


def command(session, content, suffix="one"):
    return UserCommand("user_message", {
        "content": content, "display_content": content, "conversation_id": session.active_conversation_id,
        "client_command_id": f"native-{suffix}", "user_message_id": f"user-{suffix}",
        "assistant_message_id": f"assistant-{suffix}",
    })


async def dispatch(session, request):
    assert await session.command_dispatcher._dispatch_client_command(request, session.connection_generation)
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()
    return [event for event in session.ws.sent if event["type"] == "command.result"
            and event.get("client_command_id") == request.data["client_command_id"]]


def completion(events):
    receipts = [event for event in events if event["command"] == "user_message"]
    assert len(receipts) == 1
    return receipts[0]


@pytest.mark.asyncio
async def test_goal_completion_follows_real_durable_mutation_without_a_model_admission(native_session):
    # Native completion acknowledges the mutation independently of the
    # automatic goal continuation dispatched by the existing turn scheduler.
    native_session.schedule_next_queued_user_message = Mock()
    request = command(native_session, "/goal 原生持久目标 中文🙂")
    owner_workspace = native_session.session_lifecycle.workspace_root_for_conversation()
    owner_workspace_root = str(owner_workspace) if owner_workspace is not None else ""
    events = await dispatch(native_session, request)
    receipt = completion(events)
    assert receipt["data"] == {
        "admitted": False, "native_completion": "completed", "native_completed": True,
        "slash_command": "/goal", "conversation_id": request.data["conversation_id"],
        "client_command_id": request.data["client_command_id"], "workspace_root": owner_workspace_root,
        "user_message_id": request.data["user_message_id"],
        "assistant_message_id": request.data["assistant_message_id"], "command_results": [
            {key: events[0].get(key, "info" if key == "level" else "") for key in ("command", "level", "message")},
        ],
    }
    assert receipt["client_command_id"] == request.data["client_command_id"]
    assert receipt["conversation_id"] == request.data["conversation_id"]
    assert receipt["workspace_root"] == owner_workspace_root
    assert events[0]["command"] == "goal"
    assert events.index(receipt) > events.index(events[0])
    recovered = ConversationRepository(native_session.conversation_repo.storage_root).get_conversation(request.data["conversation_id"])
    assert recovered.goal["text"] == "原生持久目标 中文🙂"
    assert recovered.transcript == []
    goal_input = recovered.context_snapshot["history"][-1]
    assert goal_input["accepted_input"]["kind"] == "user.goal"
    assert not goal_input.get("is_user_input", False)
    retained = recovered.context_snapshot["guardian_evidence"]["retained_context"]["user_messages"][-1]
    assert retained["text"] == goal_input["content"] and retained["complete"]
    assert "原生持久目标 中文🙂" in retained["text"]
    native_session.start_agent_run.assert_not_awaited()
    native_session.schedule_next_queued_user_message.assert_called_with(request.data["conversation_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/goal pause", "/permissions invalid-mode"])
async def test_real_usage_warning_is_refused_without_a_fabricated_model_turn(native_session, content):
    events = await dispatch(native_session, command(native_session, content))
    receipt = completion(events)
    assert receipt["level"] == "warning"
    assert receipt["data"]["native_completion"] == "refused"
    assert receipt["data"]["native_completed"] is False
    assert receipt["message"] == events[0]["message"]
    assert native_session.conversation_repo.get_conversation(native_session.active_conversation_id).transcript == []
    native_session.start_agent_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_level", ["success", "error"])
async def test_skills_waits_for_all_real_handler_steps_and_preserves_an_inner_refusal(native_session, second_level):
    entered, release = asyncio.Event(), asyncio.Event()

    async def marketplace_leaf(_payload):
        entered.set()
        await release.wait()
        await native_session.emit_command_result("skills.marketplace.list", "Fixture marketplace outcome", level=second_level)
        return True

    # Only the marketplace I/O leaf is substituted. The real /skills handler,
    # first skills.list, command registry, event outbox and dispatcher run.
    native_session.command_registry.register("skills.marketplace.list", marketplace_leaf)
    task = asyncio.create_task(dispatch(native_session, command(native_session, "/skills")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert any(event.get("command") == "skills.list" for event in native_session.ws.sent)
    assert not any(event.get("command") == "user_message" for event in native_session.ws.sent)
    release.set()
    events = await task
    receipt = completion(events)
    assert [result["command"] for result in receipt["data"]["command_results"]] == [
        "skills.list", "skills.marketplace.list", "skills",
    ]
    assert receipt["data"]["native_completion"] == ("completed" if second_level == "success" else "refused")
    assert receipt["level"] == (events[-2]["level"] if second_level == "success" else "error")
    native_session.start_agent_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_native_extension_reports_unknown_instead_of_inventing_success(native_session):
    async def silent(_session, _arg, _attachments):
        return True, ""

    native_session.command_registry.replace_extension_slash_handlers(
        native_session.active_conversation_id,
        {"/silent-extension": (silent, {
            "name": "silent-extension", "command": "silent-extension", "label": "/silent-extension",
            "description": "Silent fixture extension", "type": "protocol", "source": "extension", "enabled": True,
        })},
    )
    receipt = completion(await dispatch(native_session, command(native_session, "/silent-extension 原参数")))
    assert receipt["data"]["native_completion"] == "unknown"
    assert receipt["data"]["native_completed"] is False
    assert receipt["data"]["command_results"] == []
    assert receipt["level"] == "warning"
    native_session.start_agent_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/plan inspect the source", "/permissions auto inspect the source", "/review inspect the source"])
async def test_intermediate_native_or_template_result_cannot_complete_a_real_model_input(native_session, content, tmp_path):
    if content.startswith("/review"):
        root = tmp_path / "review-repository"
        root.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        (root / "actual.py").write_text("value = 1\n", encoding="utf-8")
        native_session.conversation_repo.update_workspace_binding(native_session.active_conversation_id, workspace_root=str(root))
        native_session.session_lifecycle.workspace_root = root
        options = await dispatch(native_session, UserCommand.from_ws_message({"type": "review.options", "execution_cwd": str(root),
            "conversation_id": native_session.active_conversation_id, "client_command_id": "review-options"}))
        assert options[-1]["command"] == "review.options"
        assert options[-1]["data"]["gitRoot"] == str(root)
        prepared = await dispatch(native_session, UserCommand.from_ws_message({"type": "review.prepare", "execution_cwd": str(root),
            "conversation_id": native_session.active_conversation_id, "client_command_id": "review-prepare", "mode": "uncommitted"}))
        assert prepared[-1]["command"] == "review.prepare"
        assert prepared[-1]["data"]["scope"]["mode"] == "uncommitted"
        assert "actual.py" in prepared[-1]["data"]["diff"]
    review = next(entry for entry in get_composer_command_catalog(None, resolve_active_workspace=False) if entry["command"] == "review")
    native_session.command_registry.register_slash("/review", build_slash_command_handler(review))
    events = await dispatch(native_session, command(native_session, content))
    assert events
    assert not any(event["command"] == "user_message" for event in events)
    native_session.start_agent_run.assert_awaited_once()
    call = native_session.start_agent_run.await_args
    assert call.kwargs["metadata"]["user_message_id"] == "user-one"
    assert call.kwargs["metadata"]["assistant_message_id"] == "assistant-one"
    assert call.kwargs["metadata"]["client_command_id"] == "native-one"
    assert "inspect the source" in call.args[0]
    if content.startswith("/review"):
        assert call.kwargs["metadata"]["code_review"]["mode"] == "uncommitted"
        assert call.kwargs["metadata"]["code_review"]["cwd"] == str(root)
        assert "Review Guidelines" in call.args[0]
        assert "Review Guidelines" not in call.kwargs["metadata"]["display_content"]
        assert "agent_mode" not in call.kwargs["metadata"]


@pytest.mark.asyncio
async def test_result_capture_excludes_nested_other_commands_and_closed_inherited_scopes(native_session):
    events = native_session.event_outbox
    release = asyncio.Event()
    with events.bind_client_command("captured", "user_message"):
        with events.capture_command_results() as scope:
            await native_session.emit_command_result("first", "actual first result", level="success")
            with events.bind_client_command("other", "settings"):
                await native_session.emit_command_result("other", "other owner", level="error")

            async def detached():
                await release.wait()
                await native_session.emit_command_result("late", "late inherited context", level="error")

            late = asyncio.create_task(detached())
        release.set()
        await late
    assert scope.results == [{"command": "first", "level": "success", "message": "actual first result"}]
    await events.drain_delivery()
    await events.drain_persistence()


async def prepare_native_compaction(session, monkeypatch, http_handler):
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.agent.state import AgentState
    from backend.llm.openai_adapter import OpenAIAdapter

    settings = LLMSettings(api_key="native-fixture", provider="openai", model="compact-model",
        wire_api="responses", base_url="https://compact-native.invalid/v1", native_compaction=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(http_handler))
    adapter = OpenAIAdapter(settings, http_client=client)
    execution = ModelExecutionSnapshot(config=AppConfig(llm=settings), llm=adapter, provider="openai", model="compact-model")
    builder = session.context_builder
    builder.bind_llm(adapter)
    state = AgentState(user_message="Preserve original compact task")
    await builder.start_turn(state.user_message, state)
    builder.append_assistant("Actual prior work")
    session.conversation_repo.save_context_snapshot(session.active_conversation_id, builder.export_snapshot())
    monkeypatch.setattr("backend.ws.handlers.conversation._capture_manual_compaction_model", lambda *_args: (execution, None, []))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
async def test_real_compact_terminal_result_drives_the_native_slash_completion(native_session, monkeypatch, status):
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        assert request.url.path == "/v1/responses/compact"
        entered.set()
        await release.wait()
        if status == 400:
            return httpx.Response(400, json={"error": {"message": "actual compact refusal"}})
        return httpx.Response(200, json={"id": "compact-response", "output": [
            {"type": "compaction", "encrypted_content": "actual-retained-window"}],
            "usage": {"input_tokens": 100, "output_tokens": 10}})

    client = await prepare_native_compaction(native_session, monkeypatch, provider)
    owner = native_session.active_conversation_id
    before = native_session.conversation_repo.get_conversation(owner)
    task = asyncio.create_task(dispatch(native_session, command(native_session, "/compact")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await native_session.event_outbox.drain_delivery()
        assert any(event.get("state") == "compacting" for event in native_session.ws.sent)
        assert not any(event.get("command") in {"context.compact", "user_message"} for event in native_session.ws.sent)
        release.set()
        events = await task
        receipt = completion(events)
        assert receipt["data"]["native_completion"] == ("completed" if status == 200 else "refused")
        assert receipt["level"] == ("success" if status == 200 else "error")
        assert receipt["data"]["command_results"] == [{
            "command": "context.compact", "level": events[0]["level"], "message": events[0]["message"],
        }]
        assert events[0]["command"] == "context.compact"
        assert events.index(receipt) > events.index(events[0])
        after = native_session.conversation_repo.get_conversation(owner)
        if status == 200:
            assert after.transcript[:-1] == before.transcript
            compaction = after.transcript[-1]
            assert compaction["role"] == "system" and compaction["content"] == ""
            assert len(compaction["blocks"]) == 1
            block = compaction["blocks"][0]
            assert block["type"] == "process" and block["itemKind"] == "context_compaction"
            assert block["source"] == "manual" and block["status"] == "completed"
            assert block["id"].startswith("context-compaction-")
            completed = next(event for event in native_session.ws.sent if event["type"] == "item.completed" and event["item"]["id"] == block["id"])
            assert completed["conversation_id"] == owner
            assert compaction["id"] == block["id"]
            assert compaction["turn_id"] == completed["turn_id"]
            assert completed["item"]["type"] == "context_compaction" and completed["item"]["status"] == "completed"
            assert block["startedAt"] == completed["item"]["started_at"]
            assert block["finishedAt"] == completed["item"]["finished_at"]
            assert after.compaction_state == "compacted"
            assert events[0]["message"] == ""
        else:
            assert after.transcript == before.transcript
            assert after.context_snapshot == before.context_snapshot
            assert "400 Bad Request" in receipt["message"]
        native_session.start_agent_run.assert_not_awaited()
    finally:
        release.set()
        await task
        await client.aclose()


@pytest.mark.asyncio
async def test_compact_busy_refusal_is_confirmed_without_a_provider_call_or_fake_start(native_session, monkeypatch):
    monkeypatch.setattr("backend.ws.handlers.conversation._conversation_has_active_run", lambda *_args: True)
    before = native_session.conversation_repo.get_conversation(native_session.active_conversation_id)
    capture = AsyncMock()
    monkeypatch.setattr("backend.ws.handlers.conversation._capture_manual_compaction_model", capture)
    receipt = completion(await dispatch(native_session, command(native_session, "/compact")))
    assert receipt["data"]["native_completion"] == "refused"
    assert receipt["level"] == "error"
    assert receipt["data"]["command_results"][0]["command"] == "context.compact"
    assert not any(event["type"] == "conversation.compaction.updated" for event in native_session.ws.sent)
    assert native_session.conversation_repo.get_conversation(before.id).context_snapshot == before.context_snapshot
    capture.assert_not_called()
    native_session.start_agent_run.assert_not_awaited()
