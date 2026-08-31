from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request

from backend.api import auth as api_auth
from backend.conversations.models import ConversationRecord
from backend.artifact.store import ArtifactStore
from backend.services.artifact_service import read_artifact_content
from backend.services.chat_api_service import (
    ChatApiServiceError,
    generated_artifact_native_payload,
)
from backend.services import llm_provider_service
from backend.workspace import recent_projects
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.handlers import session as session_handlers
from backend.ws.handlers import workspace as workspace_handlers


def _custom_chat_request(
    *,
    base_url: str = "https://tokenrhythm.studio/v1",
    api_key: str = "test-key",
    model: str = "glm-5.2",
) -> SimpleNamespace:
    section = SimpleNamespace(
        api_key=api_key,
        base_url=base_url,
        model=model,
        wire_api="chat",
        available_models=[model] if model else [],
        model_metadata={},
    )
    return SimpleNamespace(
        provider="custom",
        custom=section,
        openai=section,
        anthropic=section,
    )


def _http_status_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_recent_workspace_store_clear_reloads_under_lock_and_preserves_directories(
    tmp_path,
) -> None:
    first_project = tmp_path / "first-project"
    second_project = tmp_path / "second-project"
    first_project.mkdir()
    second_project.mkdir()
    store_path = tmp_path / "recent_projects.json"

    stale_store = recent_projects.RecentProjectStore(store_path)
    writer = recent_projects.RecentProjectStore(store_path)
    writer.add(str(first_project), "first", "python")
    writer.add(str(second_project), "second", "node")

    assert stale_store.clear() == 2
    assert recent_projects.RecentProjectStore(store_path).list(clean=False) == []
    assert first_project.is_dir()
    assert second_project.is_dir()


def test_recent_workspace_handlers_are_idempotent_and_publish_list_before_result(
    monkeypatch,
    tmp_path,
) -> None:
    project = tmp_path / "user-project"
    project.mkdir()
    store_path = tmp_path / "recent_projects.json"
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", store_path)
    recent_projects.RecentProjectStore().add(str(project), "user-project", "python")

    events: list[tuple[str, object]] = []

    class _Session:
        async def send_payload(self, payload, *, log_context):
            events.append(("payload", payload))

        async def emit_command_result(self, command, message, *, level, data):
            events.append(("result", {"command": command, "message": message, "level": level, "data": data}))

    session = _Session()
    asyncio.run(workspace_handlers.handle_workspace_recent_remove(
        session,
        {"path": str(project)},
    ))

    assert [kind for kind, _value in events] == ["payload", "result"]
    assert events[0][1] == {"type": "workspace.recent.list", "projects": []}
    assert events[1][1]["data"] == {"path": str(project), "removed": True}
    assert project.is_dir()

    events.clear()
    asyncio.run(workspace_handlers.handle_workspace_recent_remove(
        session,
        {"path": str(project)},
    ))
    assert events[1][1]["data"] == {"path": str(project), "removed": False}
    assert project.is_dir()


def test_recent_workspace_clear_reports_count_without_deleting_projects(
    monkeypatch,
    tmp_path,
) -> None:
    projects = [tmp_path / "one", tmp_path / "two"]
    for project in projects:
        project.mkdir()
    store_path = tmp_path / "recent_projects.json"
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", store_path)
    store = recent_projects.RecentProjectStore()
    for project in projects:
        store.add(str(project), project.name, "python")

    events: list[tuple[str, object]] = []

    class _Session:
        async def send_payload(self, payload, *, log_context):
            events.append(("payload", payload))

        async def emit_command_result(self, command, message, *, level, data):
            events.append(("result", {"command": command, "message": message, "level": level, "data": data}))

    asyncio.run(workspace_handlers.handle_workspace_recent_clear(_Session(), {}))

    assert [kind for kind, _value in events] == ["payload", "result"]
    assert events[0][1] == {"type": "workspace.recent.list", "projects": []}
    assert events[1][1]["data"] == {"removed_count": 2}
    assert all(project.is_dir() for project in projects)


@pytest.mark.parametrize("operation", ["remove", "clear"])
def test_recent_workspace_explicit_mutation_does_not_claim_success_when_persistence_fails(
    monkeypatch,
    tmp_path,
    operation: str,
) -> None:
    project = tmp_path / "durable-project"
    project.mkdir()
    store_path = tmp_path / "recent_projects.json"
    store = recent_projects.RecentProjectStore(store_path)
    store.add(str(project), project.name, "python")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk is read-only")

    monkeypatch.setattr(recent_projects, "atomic_write_text", fail_write)

    with pytest.raises(recent_projects.RecentProjectPersistenceError):
        if operation == "remove":
            store.remove(str(project))
        else:
            store.clear()

    persisted = recent_projects.RecentProjectStore(store_path).list(clean=False)
    assert [entry.path for entry in persisted] == [str(project.resolve())]
    assert project.is_dir()


@pytest.mark.parametrize(
    ("handler", "command", "payload"),
    [
        (workspace_handlers.handle_workspace_recent_remove, "workspace.recent.remove", {"path": "C:/project"}),
        (workspace_handlers.handle_workspace_recent_clear, "workspace.recent.clear", {}),
    ],
)
def test_recent_workspace_handler_returns_retryable_error_on_persistence_failure(
    monkeypatch,
    handler,
    command: str,
    payload: dict[str, object],
) -> None:
    def fail_mutation(*_args, **_kwargs):
        raise recent_projects.RecentProjectPersistenceError("write failed")

    target = (
        "backend.services.workspace_service.remove_workspace_recent"
        if command.endswith("remove")
        else "backend.services.workspace_service.clear_workspace_recent"
    )
    monkeypatch.setattr(target, fail_mutation)
    session = SimpleNamespace(
        send_payload=AsyncMock(),
        emit_command_result=AsyncMock(),
    )

    asyncio.run(handler(session, payload))

    session.send_payload.assert_not_awaited()
    result = session.emit_command_result.await_args
    assert result.args[0] == command
    assert result.kwargs["level"] == "error"
    assert result.kwargs["data"]["reason"] == "persistence_failed"
    assert result.kwargs["data"]["retryable"] is True


def test_generated_image_artifact_preserves_media_type_after_cold_reload(tmp_path) -> None:
    artifact_dir = tmp_path / "artifacts"
    owner = "conv-image-media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore(storage_dir=artifact_dir)
    artifact_id = store.save(
        "UklGRgAAAABXRUJQ",
        source="generated_image",
        type="image",
        media_type="image/webp",
        conversation_id=owner,
        workspace_root=workspace,
    )

    cold_store = ArtifactStore(storage_dir=artifact_dir)
    result = read_artifact_content(
        cold_store,
        SimpleNamespace(get_payload=lambda *_args, **_kwargs: None),
        artifact_id,
        conversation_id=owner,
        workspace_root=str(workspace),
    )
    event = result.to_event()

    assert result.media_type == "image/webp"
    assert event.data["url"].startswith("data:image/webp;base64,")


def test_generated_image_http_payload_is_owner_scoped_and_binary(tmp_path) -> None:
    owner = "conv-image-owner"
    other_owner = "conv-image-other"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = b"\x89PNG\r\n\x1a\nminimal"
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save(
        base64.b64encode(body).decode("ascii"),
        source="generated_image",
        type="image",
        media_type="image/png",
        conversation_id=owner,
        workspace_root=workspace,
    )
    records = {
        owner: ConversationRecord(id=owner, title="Owner", workspace_root=str(workspace)),
        other_owner: ConversationRecord(id=other_owner, title="Other", workspace_root=str(workspace)),
    }
    session = SimpleNamespace(
        artifact_store=store,
        conversation_repo=SimpleNamespace(get_conversation=lambda conversation_id: records.get(conversation_id)),
        session_lifecycle=SimpleNamespace(
            workspace_root_for_conversation=lambda _conversation: str(workspace),
        ),
    )
    ws_manager = SimpleNamespace(get_session=lambda session_id: session if session_id == "session-image" else None)

    actual_body, media_type, file_name = generated_artifact_native_payload(
        session_id="session-image",
        conversation_id=owner,
        artifact_id=artifact_id,
        ws_manager=ws_manager,
    )

    assert actual_body == body
    assert media_type == "image/png"
    assert file_name == f"generated-{artifact_id}.png"
    with pytest.raises(ChatApiServiceError) as denied:
        generated_artifact_native_payload(
            session_id="session-image",
            conversation_id=other_owner,
            artifact_id=artifact_id,
            ws_manager=ws_manager,
        )
    assert denied.value.status_code == 404


def test_generated_image_http_token_is_bound_to_session_conversation_and_artifact(
    monkeypatch,
) -> None:
    monkeypatch.setenv(api_auth.RUNTIME_TOKEN_ENV, "runtime-secret")
    monkeypatch.setattr(api_auth.time, "time", lambda: 1_700_000_000)
    token = api_auth._build_artifact_asset_token(
        "artifact-image-1",
        "session-image-1",
        "conversation-image-1",
        now=1_700_000_000,
    )

    def request_for(conversation_id: str) -> Request:
        query = urlencode({
            "artifact_id": "artifact-image-1",
            "session_id": "session-image-1",
            "conversation_id": conversation_id,
            "asset_token": token,
        }).encode("ascii")
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/api/artifacts/raw",
            "query_string": query,
            "headers": [],
        })

    assert api_auth._is_artifact_asset_token_authorized(request_for("conversation-image-1")) is True
    assert api_auth._is_artifact_asset_token_authorized(request_for("conversation-other")) is False


def test_conversation_create_emits_authoritative_switch_for_the_new_task(
    monkeypatch,
) -> None:
    created = ConversationRecord(
        id="conv-new-task",
        title="New chat",
        transcript=[],
        context_snapshot={},
    )
    ordering: list[str] = []

    async def broadcast(_session) -> list[str]:
        ordering.append("list")
        return []

    async def send_payload(payload, *, log_context):
        ordering.append("switched")
        assert log_context == "conversation.switched"
        assert payload["type"] == "conversation.switched"
        assert payload["conversation_id"] == created.id
        assert payload["conversation"]["id"] == created.id

    async def emit_result(command, message, *, level, data):
        ordering.append("result")
        assert command == "conversation.create"
        assert message == "Conversation created."
        assert level == "success"
        assert data["conversation_id"] == created.id
        assert data["activated"] is True

    session = SimpleNamespace(
        active_conversation_id="conv-old-task",
        conversation_repo=SimpleNamespace(
            create_conversation=Mock(return_value=created),
        ),
        session_lifecycle=SimpleNamespace(clear_workspace_runtime=Mock()),
        load_active_conversation_snapshot=Mock(return_value=False),
        sync_permission_mode_with_active_conversation=Mock(),
        send_payload=send_payload,
        emit_command_result=emit_result,
        runtime_snapshot=lambda: {
            "session_id": "session-create",
            "active_conversation_id": created.id,
        },
    )
    monkeypatch.setattr(conversation_handlers, "_broadcast_conversation_lists", broadcast)

    assert asyncio.run(conversation_handlers.handle_conversation_create(
        session,
        {
            "type": "conversation.create",
            "conversation_id": created.id,
            "title": created.title,
            "conversation_type": "main",
        },
    )) is True

    assert session.active_conversation_id == created.id
    assert ordering == ["switched", "list", "result"]


def test_provider_connection_preserves_discovery_success_when_generation_is_busy() -> None:
    async def fetch_models(_base_url: str, _api_key: str, **_kwargs: object) -> list[str]:
        return ["glm-5.2", "glm-4.7"]

    async def busy_generation(_base_url: str, _api_key: str, _model: str, _wire_api: str, **_kwargs: object) -> None:
        raise _http_status_error(503, "SERVICE_BUSY")

    result = asyncio.run(llm_provider_service.check_llm_connection(
        _custom_chat_request(),
        fetch_openai_models=fetch_models,
        check_openai_generation=busy_generation,
    ))

    assert result["ok"] is False
    assert result["model_discovery_ok"] is True
    assert result["generation_ok"] is False
    assert result["failure_kind"] == "provider_unavailable"
    assert result["retryable"] is True
    assert result["status_code"] == 503
    assert result["models"] == ["glm-5.2", "glm-4.7"]
    assert "does not mean the API key was rejected" in result["hint"]


@pytest.mark.parametrize("status_code", [401, 403])
def test_provider_connection_calls_only_real_credential_rejections_authentication_failures(
    status_code: int,
) -> None:
    async def fetch_models(_base_url: str, _api_key: str, **_kwargs: object) -> list[str]:
        return []

    async def reject_generation(_base_url: str, _api_key: str, _model: str, _wire_api: str, **_kwargs: object) -> None:
        raise _http_status_error(status_code, "credential rejected")

    result = asyncio.run(llm_provider_service.check_llm_connection(
        _custom_chat_request(),
        fetch_openai_models=fetch_models,
        check_openai_generation=reject_generation,
    ))

    assert result["model_discovery_ok"] is False
    assert result["generation_ok"] is False
    assert result["failure_kind"] == "authentication_failed"
    assert result["retryable"] is False
    assert result["status_code"] == status_code


def test_provider_generation_success_remains_authoritative_when_discovery_is_unavailable() -> None:
    async def fail_discovery(_base_url: str, _api_key: str) -> list[str]:
        raise RuntimeError("models endpoint unavailable")

    async def generation_ok(_base_url: str, _api_key: str, _model: str, _wire_api: str, **_kwargs: object) -> None:
        return None

    result = asyncio.run(llm_provider_service.check_llm_connection(
        _custom_chat_request(),
        fetch_openai_models=fail_discovery,
        check_openai_generation=generation_ok,
    ))

    assert result["ok"] is True
    assert result["model_discovery_ok"] is False
    assert result["generation_ok"] is True
    assert result["models"] == ["glm-5.2"]


def test_provider_connection_missing_fields_are_configuration_errors() -> None:
    result = asyncio.run(llm_provider_service.check_llm_connection(
        _custom_chat_request(base_url="", api_key="", model=""),
    ))

    assert result["ok"] is False
    assert result["model_discovery_ok"] is None
    assert result["generation_ok"] is None
    assert result["failure_kind"] == "configuration_error"
    assert result["retryable"] is False


async def _finish_or_cancel(awaitable, *, succeed: bool) -> bool:
    if succeed:
        await awaitable
        return True
    cancel = getattr(awaitable, "cancel", None)
    if callable(cancel):
        cancel()
        try:
            await awaitable
        except BaseException:
            pass
    else:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
    return False


@pytest.mark.parametrize(
    ("failed_stage", "expected_reason"),
    [
        (1, "scheduled_run_active"),
        (2, "background_command_active"),
        (3, "workspace_resource_active"),
    ],
)
def test_conversation_delete_keeps_repository_and_worktree_when_cleanup_deadline_fails(
    monkeypatch,
    failed_stage: int,
    expected_reason: str,
) -> None:
    conversation_id = "conv-deadline"
    deadline_calls = 0

    async def bounded(awaitable, *, timeout, label, owner=None):
        nonlocal deadline_calls
        deadline_calls += 1
        return await _finish_or_cancel(awaitable, succeed=deadline_calls != failed_stage)

    scheduler = SimpleNamespace(destroy_for_conversation=AsyncMock(return_value=1))
    monkeypatch.setattr("backend.tasks.scheduler.get_global_scheduler", lambda: scheduler)
    monkeypatch.setattr(conversation_handlers, "await_with_deadline", bounded)
    monkeypatch.setattr(
        "backend.preview.launcher.stop_preview_launches_for_conversation",
        AsyncMock(return_value=1),
    )
    cleanup_worktree = AsyncMock(return_value={"removed": True})
    monkeypatch.setattr(conversation_handlers, "_cleanup_conversation_worktree", cleanup_worktree)

    repository = SimpleNamespace(delete_conversation=Mock(return_value=True))
    owner = SimpleNamespace(
        session_id="owner",
        active_conversation_id=conversation_id,
        background_manager=SimpleNamespace(destroy_for_conversation=AsyncMock(return_value=1)),
        terminal_manager=SimpleNamespace(destroy_sessions_for_conversation=AsyncMock(return_value=1)),
    )
    session = SimpleNamespace(
        ws_manager=None,
        cleanup_tasks=set(),
        conversation_repo=repository,
        emit_command_result=AsyncMock(),
    )
    request = SimpleNamespace(
        conversation_id=conversation_id,
        cleanup_worktree=True,
        force_cleanup=False,
    )

    asyncio.run(conversation_handlers._handle_conversation_delete_after_run_fence(
        session,
        request=request,
        target=SimpleNamespace(id=conversation_id),
        owner_sessions=[owner],
    ))

    repository.delete_conversation.assert_not_called()
    cleanup_worktree.assert_not_awaited()
    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.delete"
    assert result.kwargs["data"] == {
        "conversation_id": conversation_id,
        "reason": expected_reason,
        "retryable": True,
    }


def test_conversation_runtime_purge_bounds_cancellation_resistant_ui_state_task(
    monkeypatch,
) -> None:
    owner = "conv-resistant-ui-state"
    monkeypatch.setattr(
        conversation_handlers,
        "CANCELLATION_DRAIN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "backend.agent.checkpoint.clear_checkpoints_for_conversation",
        lambda _owner: 0,
    )
    monkeypatch.setattr(
        "backend.agent.runtime.default_runtime_if_initialized",
        lambda: SimpleNamespace(purge_conversation=lambda _owner: {
            "run_ids": [],
            "subagent_ids": [],
            "task_ids": [],
            "message_ids": [],
            "team_ids": [],
        }),
    )

    async def scenario() -> tuple[dict[str, int], list[str]]:
        release = asyncio.Event()
        started = asyncio.Event()

        async def cancellation_resistant() -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        ui_task = asyncio.create_task(cancellation_resistant())
        await started.wait()
        fork_registry = SimpleNamespace(
            fork_ids_for_conversation=lambda _owner: [],
            delete_for_conversation_across_sessions=lambda _owner: 0,
        )
        cleanup_tasks: set[asyncio.Task[object]] = set()
        session = SimpleNamespace(
            ws_manager=None,
            cleanup_tasks=cleanup_tasks,
            _ui_agent_state_tasks={owner: ui_task},
            _ui_agent_state_pending={owner: {}},
            _ui_agent_state_cache={owner: {}},
            _conversation_streams={owner: object()},
            _interrupted_conversation_ids={owner},
            fork_registry=fork_registry,
            run_manager=SimpleNamespace(forget_conversation=Mock()),
            tool_registry=SimpleNamespace(
                get_tool=lambda _name: SimpleNamespace(clear_session_todos=Mock())
            ),
            checkpoint_manager=SimpleNamespace(delete_for_conversation=lambda _owner: 0),
            attachment_store=SimpleNamespace(delete_for_conversation=lambda _owner: 0),
            artifact_store=SimpleNamespace(delete_for_conversation=lambda _owner: 0),
            diagnostic_store=SimpleNamespace(delete_for_conversation=lambda _owner: 0),
        )

        result = await conversation_handlers._purge_conversation_runtime_state(
            session,
            owner,
        )
        release.set()
        await asyncio.wait_for(ui_task, timeout=1)
        return result

    counts, errors = asyncio.run(scenario())

    assert counts["run_checkpoints"] == 0
    assert "ui_agent_state_task" in errors


def test_conversation_clear_uses_the_same_bounded_background_cleanup(
    monkeypatch,
) -> None:
    conversation_id = "conv-clear-deadline"

    async def deadline_failure(awaitable, *, timeout, label, owner=None):
        return await _finish_or_cancel(awaitable, succeed=False)

    target = SimpleNamespace(id=conversation_id, context_snapshot={})
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: target,
        clear_conversation=Mock(),
    )
    session = SimpleNamespace(
        session_id="owner",
        ws_manager=None,
        cleanup_tasks=set(),
        active_conversation_id=conversation_id,
        conversation_repo=repository,
        background_manager=SimpleNamespace(destroy_for_conversation=AsyncMock(return_value=1)),
        terminal_manager=SimpleNamespace(destroy_sessions_for_conversation=AsyncMock(return_value=1)),
        emit_command_result=AsyncMock(),
    )
    monkeypatch.setattr(conversation_handlers, "_all_live_sessions", lambda _session: [session])
    monkeypatch.setattr(conversation_handlers, "_stop_conversation_run", AsyncMock(return_value=True))
    monkeypatch.setattr(conversation_handlers, "_try_claim_conversation_mutation", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(conversation_handlers, "_release_conversation_mutation", lambda _claim: None)
    monkeypatch.setattr(conversation_handlers, "await_with_deadline", deadline_failure)

    asyncio.run(conversation_handlers.handle_conversation_clear(
        session,
        {"conversation_id": conversation_id},
    ))

    repository.clear_conversation.assert_not_called()
    result = session.emit_command_result.await_args
    assert result.args[0] == "clear"
    assert result.kwargs["data"] == {
        "conversation_id": conversation_id,
        "reason": "background_command_active",
        "retryable": True,
    }


def test_usage_snapshot_emits_budget_and_context_without_visible_silent_result(
    monkeypatch,
) -> None:
    from backend.llm.cost_tracker import CostTracker

    monkeypatch.setattr(
        CostTracker,
        "get_instance",
        staticmethod(lambda: SimpleNamespace(get_summary=lambda _session_id: {
            "scope": "session",
            "input_tokens": 12,
            "output_tokens": 3,
            "total_cost_usd": 0.0,
        })),
    )
    session = SimpleNamespace(
        session_id="session-usage",
        active_conversation_id="conv-usage",
        last_agent_state=None,
        refresh_tool_registry_if_mcp_changed=Mock(),
        tool_registry=SimpleNamespace(get_schemas=Mock(return_value=[])),
        config=SimpleNamespace(token_budget=SimpleNamespace(tool_schemas=6_000)),
        permission_checker=SimpleNamespace(),
        permission_context=SimpleNamespace(),
        context_builder=SimpleNamespace(
            get_budget_snapshot=Mock(return_value={"used": 0, "total": 128_000, "breakdown": {}}),
            context_ledger=Mock(return_value=None),
        ),
        send_event=AsyncMock(),
        emit_command_result=AsyncMock(),
    )

    asyncio.run(session_handlers.handle_session_usage_inspect(session, {"silent": True}))

    assert [call.args[0].type for call in session.send_event.await_args_list] == [
        "budget_update",
        "context_usage",
    ]
    context_event = session.send_event.await_args_list[1].args[0]
    assert context_event.data["used"] == 0
    assert context_event.data["limit"] == 128_000
    session.emit_command_result.assert_not_awaited()


def test_truncate_remains_successful_when_usage_refresh_fails(
    monkeypatch,
) -> None:
    conversation_id = "conv-truncate"
    current = ConversationRecord(
        id=conversation_id,
        title="Recall",
        transcript=[],
        context_snapshot={},
    )
    owner = SimpleNamespace(
        session_id="owner",
        active_conversation_id=conversation_id,
        conversation_repo=SimpleNamespace(get_conversation=lambda _conversation_id: current),
        load_active_conversation_snapshot=Mock(),
        sync_permission_mode_with_active_conversation=Mock(),
        send_payload=AsyncMock(),
        runtime_snapshot=lambda: {},
    )
    session = SimpleNamespace(
        conversation_runtime=SimpleNamespace(rewind_to_user_turn=Mock(return_value=current)),
        emit_command_result=AsyncMock(),
    )
    monkeypatch.setattr(conversation_handlers, "_all_live_sessions", lambda _session: [owner])
    monkeypatch.setattr(conversation_handlers, "_schedule_long_term_memory_forgetting", Mock())
    monkeypatch.setattr(conversation_handlers, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        session_handlers,
        "emit_session_usage_snapshot",
        AsyncMock(side_effect=RuntimeError("usage refresh failed")),
    )
    request = SimpleNamespace(conversation_id=conversation_id, message_id="user-1")

    asyncio.run(conversation_handlers._handle_conversation_truncate_claimed(
        session,
        request,
        current,
    ))

    result = session.emit_command_result.await_args
    assert result.args[0] == "conversation.truncate"
    assert result.kwargs["level"] == "info"
    assert result.kwargs["data"]["conversation_id"] == conversation_id


def test_paged_read_artifact_does_not_resend_the_artifact_head(tmp_path) -> None:
    # Paging exists to avoid pulling in content the model did not ask for.
    # content_preview is rendered into the model-visible text by
    # ToolResult.to_context_string, so emitting a head preview alongside an
    # explicit line window shipped both the requested window and the first
    # ~1600 chars of the artifact on every paged read.
    from backend.tools.agent_artifact_tools import ReadArtifactTool

    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save(
        content="\n".join(f"LINE{i:04d}_{'head' if i < 100 else 'tail'}" for i in range(1, 601)),
        source="test",
        type="test",
    )
    tool = ReadArtifactTool(store)

    paged = asyncio.run(tool.execute({"artifact_id": artifact_id, "offset": 500, "limit": 5}))
    rendered = paged.to_context_string()
    assert "LINE0500_tail" in rendered
    assert "LINE0001_head" not in rendered
    assert paged.content_preview is None

    # An unpaged read still gets the truncated preview.
    unpaged = asyncio.run(tool.execute({"artifact_id": artifact_id}))
    assert unpaged.content_preview
    assert "LINE0001_head" in unpaged.content_preview

    # A rejected/ignored paging argument must behave as an unpaged read rather
    # than silently suppressing the preview.
    for ignored in ({"offset": 0}, {"limit": -1}, {"offset": "abc"}, {"offset": True}):
        result = asyncio.run(tool.execute({"artifact_id": artifact_id, **ignored}))
        assert result.content_preview, ignored


def test_tree_sitter_language_keys_all_have_definition_node_types() -> None:
    """Every loadable language must know which AST nodes are definitions.

    A language present in the package registry but missing from the
    definition-node table resolves to an empty set, so find_definitions
    silently returns nothing for that language instead of failing.
    """
    from backend.tools import tree_sitter_parser as tsp

    missing = sorted(
        set(tsp._LANGUAGE_PACKAGES) - set(tsp._DEFINITION_NODE_TYPES)
    )
    assert not missing, f"languages with no definition node types: {missing}"


def test_tree_sitter_extension_map_only_targets_loadable_languages() -> None:
    from backend.tools import tree_sitter_parser as tsp

    unknown = sorted(
        {
            language
            for language in tsp.EXTENSION_TO_LANGUAGE.values()
            if language not in tsp._LANGUAGE_PACKAGES
        }
    )
    assert not unknown, f"extensions map to unloadable languages: {unknown}"


def test_tree_sitter_tsx_uses_its_own_grammar_entrypoint() -> None:
    """TSX needs language_tsx(); reusing the plain TypeScript grammar
    mis-parses JSX syntax, and tree_sitter_typescript exports no bare
    language() at all."""
    from backend.tools import tree_sitter_parser as tsp

    assert tsp.EXTENSION_TO_LANGUAGE["tsx"] == "tsx"
    assert tsp._LANGUAGE_PACKAGES["typescript"] == (
        "tree_sitter_typescript",
        "language_typescript",
    )
    assert tsp._LANGUAGE_PACKAGES["tsx"] == (
        "tree_sitter_typescript",
        "language_tsx",
    )


def test_tree_sitter_loader_calls_the_declared_entrypoint(monkeypatch) -> None:
    """Pin the loader-name contract without needing real grammars.

    The original defect declared entrypoint "language" for every package.
    tree_sitter_typescript only exports language_typescript/language_tsx, so
    getattr raised AttributeError, the broad except cached None, and TS/TSX
    fell back to regex forever with no error surfaced.
    """
    import sys
    from types import ModuleType

    from backend.tools import tree_sitter_parser as tsp

    for language, (module_name, attr_name) in tsp._LANGUAGE_PACKAGES.items():
        called: list[str] = []
        fake = ModuleType(module_name)

        def _make(name: str, sink: list[str]):
            def _loader():
                sink.append(name)
                return object()

            return _loader

        setattr(fake, attr_name, _make(attr_name, called))
        monkeypatch.setitem(sys.modules, module_name, fake)
        monkeypatch.setattr(tsp, "_HAS_TREE_SITTER", True)
        monkeypatch.setattr(tsp, "_language_cache", {})
        monkeypatch.setattr(tsp, "_parser_cache", {})

        tsp.get_language(language)

        assert called == [attr_name], (
            f"{language}: expected loader {attr_name!r} to be called, got {called}"
        )
