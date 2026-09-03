from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.agent.diagnostic_store import DiagnosticPayloadStore
from backend.agent.message import AgentEvent
from backend.commands.catalog import get_file_command_catalog
from backend.services.terminal_service import terminal_output_payload
from backend.services.workspace_service import workspace_imported_payload
from backend.ws import agent_runner as agent_runner_module
from backend.ws.agent_runner import (
    _finalize_generated_image_text_offsets,
    _generated_image_projection,
    _generated_image_rejection_notice,
    _utf16_code_unit_length,
    _validated_generated_image,
)
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import (
    WORKSPACE_SCOPED_EVENT_TYPES,
    WebSocketSession,
    _requires_conversation_owner,
)
from backend.ws.turn_wait_state import TurnWaitState
from backend.ws.handlers.conversation import handle_context_compact
from backend.ws.handlers.misc import handle_commands_list
from backend.ws.handlers.preview import handle_preview_refresh
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.stream_state import create_stream_state, get_stream_content_blocks


PNG_BYTES = b"\x89PNG\r\n\x1a\nminimal"
JPEG_BYTES = b"\xff\xd8\xff\xe0minimal"
GIF87_BYTES = b"GIF87aminimal"
GIF89_BYTES = b"GIF89aminimal"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBPminimal"


def _encoded(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def test_ask_user_control_payload_preserves_turn_and_message_owner() -> None:
    session = object.__new__(WebSocketSession)
    payload = session._build_ws_payload(
        AgentEvent(
            type="ask_user",
            data={
                "tool_call_id": "ask-1",
                "question": "Continue?",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "message_id": "assistant-1",
            },
        )
    )

    assert payload["conversation_id"] == "conversation-1"
    assert payload["turn_id"] == "turn-1"
    assert payload["message_id"] == "assistant-1"
    assert payload["request"] == {
        "subtype": "elicitation",
        "tool_use_id": "ask-1",
        "prompt": "Continue?",
        "question": "Continue?",
    }


def test_agent_event_factories_enforce_stream_approval_and_queue_contracts() -> None:
    delta = AgentEvent.agent_message_delta(" ", item_id="  answer-1  ")
    assert delta.data == {"item_id": "answer-1", "delta": " "}

    approval_args = {"command": "echo ok"}
    approval = AgentEvent.approval_request(
        "  call-1  ",
        "  run_command  ",
        approval_args,
        source_agent="  reviewer  ",
    )
    assert approval.data == {
        "tool_call_id": "call-1",
        "tool_name": "run_command",
        "args": approval_args,
        "source_agent": "reviewer",
    }
    assert approval.data["args"] is not approval_args

    queued = AgentEvent.user_message_queue_updated(
        status="QUEUED",
        conversation_id="  conversation-1  ",
        message_id="  assistant-1  ",
        user_message_id="  user-1  ",
        position=1,
    )
    assert queued.data == {
        "status": "queued",
        "conversation_id": "conversation-1",
        "message_id": "assistant-1",
        "user_message_id": "user-1",
        "position": 1,
    }

    with pytest.raises(ValueError, match="delta must be a non-empty string"):
        AgentEvent.agent_message_delta("")
    with pytest.raises(ValueError, match="tool_call_id is required"):
        AgentEvent.approval_request("", "run_command", {})
    with pytest.raises(ValueError, match="positive position"):
        AgentEvent.user_message_queue_updated(
            status="queued",
            conversation_id="conversation-1",
            message_id="assistant-1",
        )
    with pytest.raises(ValueError, match="steered_current_turn reason"):
        AgentEvent.user_message_queue_updated(
            status="dequeued",
            conversation_id="conversation-1",
            message_id="assistant-1",
            turn_mode="steer",
        )


def test_agent_event_factories_enforce_typed_item_reasoning_and_budget_contracts() -> None:
    started = AgentEvent.agent_message_started(item_id=" answer-1 ")
    assert started.data == {
        "item": {
            "id": "answer-1",
            "type": "agent_message",
            "text": "",
            "status": "in_progress",
        }
    }

    completed = AgentEvent.agent_message_completed(
        "final answer",
        item_id="answer-1",
        status="partial",
        finish_reason="max_output",
    )
    assert completed.data["item"]["status"] == "partial"
    assert completed.data["finish_reason"] == "max_output"

    start_reasoning = AgentEvent.thinking_chunk(
        "",
        item_id="reasoning-1",
        content_index=0,
        lifecycle="start",
    )
    assert start_reasoning.data == {
        "content": "",
        "lifecycle": "start",
        "item_id": "reasoning-1",
        "content_index": 0,
    }

    item = AgentEvent.agent_item(
        id="process-1",
        kind="process_text",
        content="Inspecting source",
        seq=7,
    )
    assert item.data["order"] == 7
    assert "seq" not in item.data

    progress = AgentEvent.progress(
        "Running read_file",
        stage="tool",
        status="running",
        tool_call_id="call-1",
        tool_name="read_file",
    )
    assert progress.data["phase"] == "tool"
    assert progress.data["visibility"] == "timeline"

    done = AgentEvent.done(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=4,
        input_includes_cache_read=True,
        status="failed",
        duration_ms=25,
    )
    assert done.data["status"] == "failed"
    assert done.data["usage"]["input_includes_cache_read"] is True

    ledger = {
        "schema_version": 1,
        "estimated_tokens": 120,
        "actual_tokens": 100,
        "compaction_count": 1,
        "native_attachment_tokens": 0,
        "native_attachment_count": 0,
        "entries": [],
    }
    context_usage = AgentEvent.context_usage(
        used=100,
        limit=1000,
        conversation_id="conversation-1",
        ledger=ledger,
    )
    compacted = AgentEvent.context_compacted(
        "Retained current task state",
        before_tokens=900,
        after_tokens=100,
        retained_categories=["history"],
        ledger=ledger,
    )
    budget = AgentEvent.budget_update(
        used=100,
        total=1000,
        breakdown={"history": 80, "system_runtime": 20},
        conversation_id="conversation-1",
    )
    assert context_usage.data["ledger"] == ledger
    assert compacted.data["before_tokens"] == 900
    assert compacted.data["after_tokens"] == 100
    assert budget.data["breakdown"] == {"history": 80, "system_runtime": 20}

    with pytest.raises(ValueError, match="completion status"):
        AgentEvent.agent_message_completed("answer", status="running")
    with pytest.raises(ValueError, match="content is required"):
        AgentEvent.thinking_chunk("", lifecycle="delta")
    with pytest.raises(ValueError, match="Unsupported thinking lifecycle"):
        AgentEvent.thinking_chunk("text", lifecycle="pause")
    with pytest.raises(ValueError, match="requires content or summary"):
        AgentEvent.agent_item(id="empty", kind="status")
    with pytest.raises(ValueError, match="requires tool_name"):
        AgentEvent.progress("Running", stage="tool", tool_call_id="call-1")
    with pytest.raises(ValueError, match="Unsupported done status"):
        AgentEvent.done(status="running")
    with pytest.raises(ValueError, match="non-negative safe integer"):
        AgentEvent.done(input_tokens=-1)
    with pytest.raises(ValueError, match="finite non-negative number"):
        AgentEvent.done(cost_usd=float("nan"))
    with pytest.raises(ValueError, match="percent must be between 0 and 1"):
        AgentEvent.budget_warning("context", 1.1)


def test_error_event_truncates_oversized_exception_text_without_secondary_failure() -> None:
    prefix = "provider-prefix:"
    suffix = ":provider-suffix"
    event = AgentEvent.error(f"{prefix}{'x' * 80_000}{suffix}")

    message = event.data["message"]
    assert len(message) == 65_536
    assert message.startswith(prefix)
    assert message.endswith(suffix)
    assert "[error message truncated]" in message


def test_terminal_output_payload_omits_unknown_exit_status_instead_of_sending_null() -> None:
    unknown = terminal_output_payload(
        "echo pending",
        "pending",
        None,
        conversation_id="conversation-1",
    )
    success = terminal_output_payload(
        "echo ok",
        "ok",
        0,
        conversation_id="conversation-1",
    )

    assert unknown == {
        "type": "terminal.output",
        "command": "echo pending",
        "output": "pending",
        "conversation_id": "conversation-1",
    }
    assert success["exit_code"] == 0


def test_preview_refresh_validates_url_and_preserves_command_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = SimpleNamespace(
        workspace_root=str(tmp_path),
        worktree_path="",
    )

    class _ConversationRepo:
        def get_conversation(self, conversation_id: str) -> Any:
            return conversation if conversation_id == "conversation-1" else None

    session = SimpleNamespace(
        session_id="session-1",
        active_conversation_id="conversation-1",
        conversation_repo=_ConversationRepo(),
        send_event=AsyncMock(),
        resolve_requested_workspace=lambda requested=None: Path(requested or tmp_path).resolve(),
    )
    session.session_lifecycle = SessionLifecycle(session)
    monkeypatch.setattr(
        "backend.preview.launcher.preview_url_is_owned",
        lambda *_args, **_kwargs: True,
    )

    assert asyncio.run(handle_preview_refresh(session, {
        "conversation_id": "conversation-1",
        "workspace_root": str(tmp_path),
        "request_id": "refresh-1",
        "url": "http://localhost:5173/app",
    })) is True
    refreshed = session.send_event.await_args.args[0]
    assert refreshed.type == "preview.refreshed"
    assert refreshed.data == {
        "url": "http://localhost:5173/app",
        "conversation_id": "conversation-1",
        "workspace_root": str(tmp_path.resolve()),
        "request_id": "refresh-1",
    }

    session.send_event.reset_mock()
    assert asyncio.run(handle_preview_refresh(session, {
        "conversation_id": "conversation-1",
        "workspace_root": str(tmp_path),
        "request_id": "refresh-2",
        "url": "http://user:password@localhost:5173/app",
    })) is True
    rejected = session.send_event.await_args.args[0]
    assert rejected.type == "command.result"
    assert rejected.data["command"] == "preview.refresh"
    assert rejected.data["level"] == "error"
    assert "embedded URL credentials" in rejected.data["message"]


@pytest.mark.parametrize(
    ("media_type", "payload"),
    [
        ("image/png", PNG_BYTES),
        ("image/jpeg", JPEG_BYTES),
        ("image/gif", GIF87_BYTES),
        ("image/gif", GIF89_BYTES),
        ("image/webp", WEBP_BYTES),
    ],
)
def test_validated_generated_image_accepts_only_matching_raster_payloads(
    media_type: str,
    payload: bytes,
) -> None:
    encoded = _encoded(payload)

    actual_encoded, actual_media_type, decoded = _validated_generated_image(
        encoded,
        media_type,
    )

    assert actual_encoded == encoded
    assert actual_media_type == media_type
    assert decoded == payload


def test_validated_generated_image_normalizes_jpg_and_matching_data_url() -> None:
    encoded = _encoded(JPEG_BYTES)

    actual_encoded, media_type, decoded = _validated_generated_image(
        f"data:image/jpeg;base64,{encoded}",
        "image/jpg; charset=binary",
    )

    assert actual_encoded == encoded
    assert media_type == "image/jpeg"
    assert decoded == JPEG_BYTES


@pytest.mark.parametrize(
    ("image_data", "media_type", "message"),
    [
        ("not base64!", "image/png", "not valid base64"),
        (_encoded(PNG_BYTES), "image/jpeg", "do not match"),
        (_encoded(PNG_BYTES), "image/svg+xml", "unsupported"),
        (
            f"data:image/jpeg;base64,{_encoded(PNG_BYTES)}",
            "image/png",
            "does not match its media type",
        ),
    ],
)
def test_validated_generated_image_rejects_spoofed_or_malformed_payloads(
    image_data: str,
    media_type: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validated_generated_image(image_data, media_type)


def test_validated_generated_image_enforces_artifact_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_runner_module, "MAX_ARTIFACT_CONTENT_CHARS", 8)

    with pytest.raises(ValueError, match="artifact size limit"):
        _validated_generated_image(_encoded(PNG_BYTES), "image/png")


def test_generated_image_projection_keeps_wire_and_transcript_values_aligned() -> None:
    transcript, wire = _generated_image_projection(
        artifact_id="artifact-image-1",
        media_type="image/png",
        decoded_image=PNG_BYTES,
        conversation_id="conversation-1",
        message_id="assistant-1",
        text_offset=14,
    )

    assert transcript == {
        "artifactId": "artifact-image-1",
        "kind": "image",
        "summary": "Generated PNG image",
        "bytes": len(PNG_BYTES),
        "mediaType": "image/png",
        "textOffset": 14,
    }
    assert wire == {
        "type": "artifact.preview",
        "conversation_id": "conversation-1",
        "message_id": "assistant-1",
        "artifact_id": "artifact-image-1",
        "kind": "image",
        "summary": "Generated PNG image",
        "bytes": len(PNG_BYTES),
        "media_type": "image/png",
        "text_offset": 14,
    }


def test_generated_image_projection_repairs_zero_offset_after_final_text_arrives() -> None:
    artifacts = [{"kind": "image", "textOffset": 0, "artifactId": "image-1"}]

    _finalize_generated_image_text_offsets(
        artifacts,
        "好的，我来生成这张图片。\n\n图像已经为你生成好了。",
    )

    assert artifacts[0]["textOffset"] == _utf16_code_unit_length(
        "好的，我来生成这张图片。\n\n"
    )


def test_generated_image_text_anchor_uses_javascript_utf16_offsets() -> None:
    assert _utf16_code_unit_length("开场🙂\n") == len("开场🙂\n".encode("utf-16-le")) // 2 == 5


def test_generated_image_rejection_notice_is_owned_and_never_contains_raw_image() -> None:
    raw_marker = _encoded(PNG_BYTES)
    event = _generated_image_rejection_notice(
        conversation_id="conversation-1",
        message_id="assistant-1",
        media_type="image/png",
        encoded_characters=len(raw_marker),
        reason="generated-image bytes do not match their declared media type",
    )

    assert event.type == "system_notice"
    assert event.data["conversation_id"] == "conversation-1"
    assert event.data["message_id"] == "assistant-1"
    assert event.data["data"] == {
        "kind": "generated_image_rejected",
        "media_type": "image/png",
        "encoded_characters": len(raw_marker),
    }
    serialized = json.dumps(event.data, sort_keys=True)
    assert raw_marker not in serialized
    assert "image_data" not in serialized


class _WorkspaceContext:
    def to_dict(self) -> dict[str, Any]:
        return {
            "root_path": "C:\\stale-root",
            "project_type": "python",
            "name": "Audit workspace",
            "description": "Semantic projection fixtures",
            "file_count": 999,
            "total_size": 123_456,
            "has_project_instructions": False,
            "index_truncated": True,
        }

    def get_project_summary(self) -> str:
        return "Python project with a truncated index"


def test_workspace_imported_payload_canonicalizes_owner_root_and_file_count(tmp_path: Path) -> None:
    payload = workspace_imported_payload(
        _WorkspaceContext(),
        SimpleNamespace(file_count="42"),
        conversation_id="  conversation-1  ",
        workspace_root=tmp_path / ".",
        request_id="  request-1  ",
    )

    canonical_root = str(tmp_path.resolve())
    assert payload["conversation_id"] == "conversation-1"
    assert payload["workspace_root"] == canonical_root
    assert payload["project"]["root_path"] == canonical_root
    assert payload["file_count"] == 42
    assert payload["project"]["file_count"] == 42
    assert payload["request_id"] == "request-1"


def test_workspace_imported_payload_rejects_an_empty_conversation_owner(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a conversation owner"):
        workspace_imported_payload(
            _WorkspaceContext(),
            SimpleNamespace(file_count=42),
            conversation_id=" ",
            workspace_root=tmp_path,
        )


def test_file_command_catalog_never_uses_another_active_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    command_a = workspace_a / ".minicode" / "commands" / "alpha.md"
    command_b = workspace_b / ".minicode" / "commands" / "beta.md"
    command_a.parent.mkdir(parents=True)
    command_b.parent.mkdir(parents=True)
    command_a.write_text("Alpha command body", encoding="utf-8")
    command_b.write_text("Beta command body", encoding="utf-8")

    import backend.agent.markdown_scopes as markdown_scopes_module
    import backend.commands.catalog as catalog_module

    monkeypatch.setattr(catalog_module, "get_explicit_active_workspace_root", lambda: workspace_b)
    monkeypatch.setattr(catalog_module, "_get_managed_minicode_dir", lambda: tmp_path / "managed")
    monkeypatch.setattr(markdown_scopes_module, "get_minicode_config_home_dir", lambda: tmp_path / "user")

    commands = get_file_command_catalog(
        workspace_a,
        resolve_active_workspace=False,
    )
    project_commands = {
        str(command["command"]): command
        for command in commands
        if command.get("source") == "project"
    }

    assert "alpha" in project_commands
    assert "beta" not in project_commands
    assert project_commands["alpha"]["source_path"] == str(command_a)


def test_commands_list_keeps_captured_owner_and_workspace_across_await(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_workspace = tmp_path / "old-workspace"
    new_workspace = tmp_path / "new-workspace"
    old_conversation = SimpleNamespace(id="conversation-old", workspace_root=str(old_workspace))
    new_conversation = SimpleNamespace(id="conversation-new", workspace_root=str(new_workspace))

    class _ConversationRepo:
        def get_conversation(self, conversation_id: str) -> Any:
            return {
                "conversation-old": old_conversation,
                "conversation-new": new_conversation,
            }.get(conversation_id)

    class _CommandRegistry:
        def __init__(self) -> None:
            self.scopes: list[str | None] = []

        def list_extension_slash_commands(self, *, scope_id: str | None) -> list[dict[str, Any]]:
            self.scopes.append(scope_id)
            return [{"name": "extension-old", "command": "extension-old", "source": "extension"}]

    session = SimpleNamespace()
    session.active_conversation_id = "conversation-old"
    session.conversation_repo = _ConversationRepo()
    session.command_registry = _CommandRegistry()
    session.session_lifecycle = SimpleNamespace(
        workspace_root_for_conversation=lambda conversation: Path(conversation.workspace_root)
    )
    session.event_outbox = SimpleNamespace(client_command_id="request-old")
    sent: list[tuple[dict[str, Any], str]] = []

    async def ensure_extension_commands(conversation_id: str) -> None:
        assert conversation_id == "conversation-old"
        session.active_conversation_id = "conversation-new"
        await asyncio.sleep(0)

    async def send_ws_payload(payload: dict[str, Any], *, log_context: str) -> bool:
        sent.append((payload, log_context))
        return True

    session._ensure_extension_commands_for_conversation = ensure_extension_commands
    session.send_payload = send_ws_payload

    list_calls: list[tuple[Path | None, bool]] = []

    def fake_list_commands(
        workspace_root: str | Path | None = None,
        *,
        resolve_active_workspace: bool = True,
    ) -> list[dict[str, Any]]:
        root = Path(workspace_root) if workspace_root is not None else None
        list_calls.append((root, resolve_active_workspace))
        return [{"name": "project-old", "command": "project-old", "source": "project"}]

    import backend.services.skills_service as skills_service_module

    monkeypatch.setattr(skills_service_module, "list_commands", fake_list_commands)

    assert asyncio.run(handle_commands_list(session, {})) is True
    assert session.active_conversation_id == "conversation-new"
    assert session.command_registry.scopes == ["conversation-old"]
    assert list_calls == [(old_workspace, False)]
    assert sent == [
        (
            {
                "type": "commands.list",
                "conversation_id": "conversation-old",
                "commands": [
                    {"name": "extension-old", "command": "extension-old", "source": "extension"},
                    {"name": "project-old", "command": "project-old", "source": "project"},
                ],
                "request_id": "request-old",
            },
            "commands.list",
        )
    ]


def test_manual_context_compact_emits_owned_lifecycle_then_authoritative_usage() -> None:
    class _ContextBuilder:
        compacted = False

        def context_ledger(self) -> dict[str, Any]:
            tokens = 900 if not self.compacted else 240
            return {
                "schema_version": 1,
                "estimated_tokens": tokens,
                "actual_tokens": tokens,
                "compaction_count": 1 if self.compacted else 0,
                "native_attachment_tokens": 0,
                "native_attachment_count": 0,
                "entries": [{
                    "category": "history",
                    "label": "History",
                    "estimated_tokens": tokens,
                    "item_count": 4,
                    "source_count": 2,
                    "sources": ["turn-1", "turn-2"],
                }],
            }

        def get_budget_snapshot(self, *, state: Any, tool_schemas: Any) -> dict[str, Any]:
            assert state is agent_state
            assert tool_schemas == []
            used = 900 if not self.compacted else 240
            return {
                "used": used,
                "total": 1_000,
                "breakdown": {"history": used},
            }

        async def compact(self, *, focus: str, restore_state: Any) -> str:
            assert focus == "retain release evidence"
            assert restore_state is agent_state
            self.compacted = True
            return "Retained release goal and latest verification evidence."

        def export_snapshot(self) -> dict[str, Any]:
            return {"compaction_count": 1}

    class _ConversationRepo:
        def __init__(self) -> None:
            self.committed: tuple[str, dict[str, Any], str, str, int] | None = None

        def get_conversation(self, conversation_id: str) -> Any:
            assert conversation_id == "conversation-1"
            return SimpleNamespace(
                revision=4,
                context_snapshot={"preserved": True},
            )

        def commit_compaction(
            self,
            conversation_id: str,
            *,
            context_snapshot: dict[str, Any],
            state: str,
            summary: str,
            expected_revision: int,
        ) -> Any:
            assert projection_lock.locked()
            self.committed = (
                conversation_id,
                context_snapshot,
                state,
                summary,
                expected_revision,
            )
            return SimpleNamespace(id=conversation_id)

    agent_state = SimpleNamespace(user_message="current")
    builder = _ContextBuilder()
    repo = _ConversationRepo()
    projection_lock = asyncio.Lock()
    session = SimpleNamespace(
        active_conversation_id="conversation-1",
        context_builder=builder,
        last_agent_state=agent_state,
        tool_registry=SimpleNamespace(get_schemas=lambda **_kwargs: []),
        config=SimpleNamespace(token_budget=SimpleNamespace(tool_schemas=6000)),
        permission_checker=object(),
        permission_context=object(),
        conversation_repo=repo,
        send_event=AsyncMock(),
        _conversation_projection_lock=lambda _conversation_id: projection_lock,
        ws_manager=None,
    )

    assert asyncio.run(handle_context_compact(
        session,
        {"focus": "retain release evidence"},
    )) is True

    events = [call.args[0] for call in session.send_event.await_args_list]
    assert [event.type for event in events] == [
        "context_compacted",
        "budget_update",
        "context_usage",
    ]
    assert events[0].data["conversation_id"] == "conversation-1"
    assert events[0].data["before_tokens"] == 900
    assert events[0].data["after_tokens"] == 240
    assert events[0].data["retained_categories"] == ["history"]
    assert events[0].data["ledger"]["actual_tokens"] == 240
    assert events[1].data == {
        "used": 240,
        "total": 1_000,
        "breakdown": {"history": 240},
        "conversation_id": "conversation-1",
    }
    assert events[2].data["used"] == 240
    assert events[2].data["limit"] == 1_000
    assert events[2].data["ledger"]["actual_tokens"] == 240
    assert repo.committed == (
        "conversation-1",
        {"compaction_count": 1, "preserved": True},
        "compacted",
        "Retained release goal and latest verification evidence.",
        4,
    )


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _minimal_transport_session(tmp_path: Path) -> tuple[WebSocketSession, _FakeWebSocket]:
    session = object.__new__(WebSocketSession)
    websocket = _FakeWebSocket()
    session.session_id = "session-test"
    session.event_outbox = EventOutbox(
        session_id=session.session_id,
        websocket=websocket,
        replay_root=tmp_path,
        replay_limit=1000,
        cleanup_tasks=set(),
        has_active_run=lambda: False,
        requires_conversation_owner=_requires_conversation_owner,
        workspace_scoped_event_types=WORKSPACE_SCOPED_EVENT_TYPES,
    )
    return session, websocket


def _minimal_event_session() -> WebSocketSession:
    session = object.__new__(WebSocketSession)
    session.session_id = "session-test"
    session._conversation_streams = {}
    session.turn_wait_state = TurnWaitState()
    session.diagnostic_store = DiagnosticPayloadStore()
    session.event_outbox = SimpleNamespace(client_command_id="")
    session._notification_hook_tasks = set()
    session._run_notification_hook_for_event = AsyncMock()
    session.send_payload = AsyncMock(return_value=True)
    return session


def test_handle_rejects_non_object_json_and_continues_to_ping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.messages = iter(("null", "[]", '"text"', '{"type":"ping"}'))

        async def receive_text(self) -> str:
            try:
                return next(self.messages)
            except StopIteration as exc:
                raise WebSocketDisconnect() from exc

    class _ArtifactStore:
        def __init__(self) -> None:
            self.flushed = False
            self.cleared = False

        async def flush(self) -> None:
            self.flushed = True

        def clear(self) -> None:
            self.cleared = True

    session = object.__new__(WebSocketSession)
    session.session_id = "session-non-object-json"
    session.event_outbox = SimpleNamespace(
        websocket=_WebSocket(),
        connection_generation=1,
    )
    session.artifact_store = _ArtifactStore()
    session.session_lifecycle = SessionLifecycle(session)
    session.session_lifecycle.ensure_workspace_context_task = lambda: None
    session.session_lifecycle.recover_orphaned_background_commands = AsyncMock()
    session.command_dispatcher = SessionCommandDispatcher(
        session,
        root_dir=tmp_path / "client-commands",
    )
    session.send_llm_state = AsyncMock()
    session.command_dispatcher._replay_pending_client_commands = AsyncMock()
    events: list[dict] = []
    payloads: list[dict] = []

    async def capture_event(event: AgentEvent) -> None:
        events.append(event.to_ws_message())

    async def capture_payload(payload: dict, **_kwargs: Any) -> bool:
        payloads.append(dict(payload))
        return True

    session.send_event = capture_event
    session.send_payload = capture_payload
    monkeypatch.setattr("backend.api.routes_health.get_mcp_status", lambda: [])

    asyncio.run(session.session_lifecycle.handle(connection_generation=1))

    errors = [event for event in events if event.get("type") == "error"]
    assert [event["message"] for event in errors] == [
        "WebSocket message must be a JSON object",
        "WebSocket message must be a JSON object",
        "WebSocket message must be a JSON object",
    ]
    assert all(event["recoverable"] is True for event in errors)
    assert payloads == [{"type": "pong"}]
    assert session.artifact_store.flushed is True
    assert session.artifact_store.cleared is True


def test_send_payload_requires_both_workspace_import_owners(tmp_path: Path) -> None:
    session, websocket = _minimal_transport_session(tmp_path)
    base = {
        "type": "workspace.imported",
        "conversation_id": "conversation-1",
        "workspace_root": "C:\\workspace",
    }

    without_conversation = {**base, "conversation_id": ""}
    without_workspace = {**base, "workspace_root": ""}
    assert asyncio.run(session.send_payload(
        without_conversation,
        connection_generation=1,
        log_context="test",
        envelope=False,
    )) is False
    assert asyncio.run(session.send_payload(
        without_workspace,
        connection_generation=1,
        log_context="test",
        envelope=False,
    )) is False
    assert websocket.sent == []

    assert asyncio.run(session.send_payload(
        base,
        connection_generation=1,
        log_context="test",
        envelope=True,
    )) is True
    assert websocket.sent[0]["type"] == "workspace.imported"
    assert websocket.sent[0]["conversation_id"] == "conversation-1"
    assert websocket.sent[0]["workspace_root"] == "C:\\workspace"


def test_send_event_rejects_missing_conversation_owner_before_state_hooks_or_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )

    asyncio.run(session.send_event(AgentEvent(
        type="workspace.imported",
        data={
            "conversation_id": "",
            "workspace_root": "C:\\workspace",
            "project": {},
            "summary": "",
            "file_count": 0,
        },
    )))

    assert applied == []
    assert session._conversation_streams == {}
    session._run_notification_hook_for_event.assert_not_awaited()
    session.send_payload.assert_not_awaited()


def test_send_event_rejects_missing_workspace_owner_before_state_hooks_or_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )

    asyncio.run(session.send_event(AgentEvent(
        type="workspace.imported",
        data={
            "conversation_id": "conversation-1",
            "workspace_root": "",
            "project": {},
            "summary": "",
            "file_count": 0,
        },
    )))

    assert applied == []
    assert session._conversation_streams == {}
    session._run_notification_hook_for_event.assert_not_awaited()
    session.send_payload.assert_not_awaited()


def test_send_event_rejects_unowned_user_queue_updates_before_projection_or_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )

    asyncio.run(session.send_event(AgentEvent(
        type="user_message.queue.updated",
        data={
            "status": "queued",
            "conversation_id": "",
            "message_id": "assistant-1",
            "position": 1,
        },
    )))

    assert applied == []
    session._run_notification_hook_for_event.assert_not_awaited()
    session.send_payload.assert_not_awaited()


def test_send_event_with_complete_owners_reaches_state_hooks_and_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )
    event = AgentEvent(
        type="workspace.imported",
        data={
            "conversation_id": "conversation-1",
            "workspace_root": "C:\\workspace",
            "project": {
                "root_path": "C:\\workspace",
                "project_type": "python",
                "name": "Workspace",
                "description": "",
                "file_count": 0,
                "total_size": 0,
                "has_project_instructions": False,
                "index_truncated": False,
            },
            "summary": "Python workspace",
            "file_count": 0,
        },
    )

    asyncio.run(session.send_event(event))

    assert len(applied) == 1
    assert applied[0][1:] == (
        "conversation-1",
        "workspace.imported",
        event.data,
    )
    session._run_notification_hook_for_event.assert_not_awaited()
    sent_payload = session.send_payload.await_args.args[0]
    assert sent_payload["conversation_id"] == "conversation-1"
    assert sent_payload["workspace_root"] == "C:\\workspace"
    session.send_payload.assert_awaited_once_with(
        sent_payload,
        log_context="event:workspace.imported",
    )


def test_send_event_reuses_wire_owner_defaults_for_stream_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    session._conversation_streams = {
        "conversation-1": create_stream_state(
            "conversation-1",
            "assistant-current",
            "turn-current",
        )
    }
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )
    event = AgentEvent(
        type="ask_user",
        data={
            "conversation_id": "conversation-1",
            "tool_call_id": "ask-1",
            "question": "Continue?",
        },
    )

    asyncio.run(session.send_event(event))

    assert len(applied) == 1
    assert applied[0][1:] == (
        "conversation-1",
        "ask_user",
        {
            "conversation_id": "conversation-1",
            "tool_call_id": "ask-1",
            "question": "Continue?",
            "message_id": "assistant-current",
        },
    )
    assert "message_id" not in event.data
    wire = session.send_payload.await_args.args[0]
    assert wire["type"] == "control_request"
    assert wire["message_id"] == "assistant-current"


def test_send_event_replaces_blank_wire_owner_defaults_before_stream_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    session._conversation_streams = {
        "conversation-1": create_stream_state(
            "conversation-1",
            "assistant-current",
            "turn-current",
        )
    }
    applied: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "backend.ws.handler.apply_stream_event",
        lambda *args: applied.append(args),
    )
    event = AgentEvent(
        type="ask_user",
        data={
            "conversation_id": "conversation-1",
            "message_id": "",
            "tool_call_id": "ask-1",
            "question": "Continue?",
        },
    )

    asyncio.run(session.send_event(event))

    assert applied[0][3]["message_id"] == "assistant-current"
    assert session.send_payload.await_args.args[0]["message_id"] == "assistant-current"
    assert event.data["message_id"] == ""


def test_send_event_rejects_invalid_projection_before_stream_state_mutation() -> None:
    session = _minimal_event_session()
    session._conversation_streams = {
        "conversation-1": create_stream_state(
            "conversation-1",
            "assistant-1",
            "turn-1",
        )
    }

    asyncio.run(session.send_event(AgentEvent(
        type="stream_resume",
        data={
            "conversation_id": "conversation-1",
            "message_id": "assistant-1",
            "tool_calls_pending": "not-a-list",
        },
    )))

    assert session._conversation_streams["conversation-1"]["event_seq"] == 0
    session.send_payload.assert_not_awaited()


def test_send_event_does_not_snapshot_reasoning_withheld_by_transport() -> None:
    session = _minimal_event_session()
    session._conversation_streams = {
        "conversation-1": create_stream_state(
            "conversation-1",
            "assistant-1",
            "turn-1",
        )
    }

    asyncio.run(session.send_event(AgentEvent(
        type="thinking_delta",
        data={
            "conversation_id": "conversation-1",
            "message_id": "assistant-1",
            "content": "private provider reasoning",
            "source": "provider",
            "provider_reasoning_type": "reasoning_content",
            "visibility": "debug",
        },
    )))

    assert session._conversation_streams["conversation-1"]["event_seq"] == 0
    session.send_payload.assert_not_awaited()


def test_send_event_keeps_timeline_reasoning_live_while_excluding_it_from_resume_snapshot() -> None:
    session = _minimal_event_session()
    session._conversation_streams = {
        "conversation-1": create_stream_state(
            "conversation-1",
            "assistant-1",
            "turn-1",
        )
    }

    asyncio.run(session.send_event(AgentEvent(
        type="thinking_delta",
        data={
            "conversation_id": "conversation-1",
            "message_id": "assistant-1",
            "content": "visible provider reasoning",
            "source": "provider",
            "provider_reasoning_type": "reasoning_content",
            "visibility": "timeline",
        },
    )))

    assert session._conversation_streams["conversation-1"]["event_seq"] == 1
    assert session.send_payload.await_args.args[0]["content"] == "visible provider reasoning"
    assert session._conversation_streams["conversation-1"]["content_blocks"]
    assert get_stream_content_blocks(session._conversation_streams["conversation-1"]) == []


def test_send_event_does_not_mutate_shared_broadcast_event_owner() -> None:
    first = _minimal_event_session()
    second = _minimal_event_session()
    first.conversation_runtime = SimpleNamespace(active_conversation_id=None)
    second.conversation_runtime = SimpleNamespace(active_conversation_id=None)
    first.run_manager = SimpleNamespace(watch_conversation_notifications=lambda _id: None)
    second.run_manager = SimpleNamespace(watch_conversation_notifications=lambda _id: None)
    first.active_conversation_id = "conversation-first"
    second.active_conversation_id = "conversation-second"
    event = AgentEvent(
        type="system_notice",
        data={"content": "notice"},
    )

    asyncio.run(first.send_event(event))
    asyncio.run(second.send_event(event))

    assert first.send_payload.await_args.args[0]["conversation_id"] == "conversation-first"
    assert second.send_payload.await_args.args[0]["conversation_id"] == "conversation-second"
    assert "conversation_id" not in event.data


def test_send_event_projects_before_slow_notification_hook_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session = _minimal_event_session()
        monkeypatch.setattr("backend.ws.handler.apply_stream_event", lambda *_args: None)
        hook_started = asyncio.Event()
        release_hook = asyncio.Event()

        async def slow_hook(_event: AgentEvent, _payload: dict[str, Any]) -> None:
            hook_started.set()
            await release_hook.wait()

        session._run_notification_hook_for_event = slow_hook
        event = AgentEvent(
            type="system_notice",
            data={
                "conversation_id": "conversation-1",
                "content": "Child task started",
                "level": "info",
            },
        )

        await session.send_event(event)
        session.send_payload.assert_awaited_once()
        await asyncio.wait_for(hook_started.wait(), timeout=0.5)
        assert len(session._notification_hook_tasks) == 1
        assert not next(iter(session._notification_hook_tasks)).done()

        release_hook.set()
        await asyncio.gather(*session._notification_hook_tasks)

    asyncio.run(scenario())


def test_item_completed_transport_removes_pi_tool_metadata_and_defers_provider_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    monkeypatch.setattr("backend.ws.handler.apply_stream_event", lambda *_args: None)
    event = AgentEvent(
        type="item.completed",
        data={
            "conversation_id": "conversation-1",
            "message_id": "assistant-1",
            "item": {
                "id": "answer-1",
                "type": "agent_message",
                "text": "Final answer",
                "source": "model_final",
                "status": "completed",
            },
            "tool_calls": [{
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "secret-sized-argument.txt"},
            }],
            "provider_raw": {
                "trace_id": "trace-item-1",
                "provider": "openai",
                "citations": [{"url": "https://example.test/source", "title": "Source"}],
                "provider_timeline": [{"event": "delta", "raw": "x" * 10_000}],
            },
        },
    )

    asyncio.run(session.send_event(event))

    wire = session.send_payload.await_args.args[0]
    assert "tool_calls" not in wire
    assert wire["provider_raw"]["diagnostics_deferred"] is True
    assert wire["provider_raw"]["trace_id"] == "trace-item-1"
    assert wire["provider_raw"]["citations"] == [
        {"url": "https://example.test/source", "title": "Source"}
    ]
    assert "provider_timeline" not in wire["provider_raw"]

    stored = session.diagnostic_store.get("provider", "trace-item-1")
    assert stored is not None
    assert stored.payload["provider_timeline"][0]["raw"] == "x" * 10_000


def test_done_transport_defers_canonical_snake_case_provider_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _minimal_event_session()
    monkeypatch.setattr("backend.ws.handler.apply_stream_event", lambda *_args: None)
    event = AgentEvent(
        type="done",
        data={
            "conversation_id": "conversation-1",
            "message_id": "assistant-1",
            "status": "completed",
            "usage": {},
            "provider_raw": {
                "trace_id": "trace-done-1",
                "provider": "openai",
                "provider_timeline": [{"event": "delta", "raw": "x" * 10_000}],
            },
        },
    )

    asyncio.run(session.send_event(event))

    wire = session.send_payload.await_args.args[0]
    assert wire["provider_raw"]["diagnostics_deferred"] is True
    assert wire["provider_raw"]["trace_id"] == "trace-done-1"
    assert "provider_timeline" not in wire["provider_raw"]
    stored = session.diagnostic_store.get("provider", "trace-done-1")
    assert stored is not None
    assert stored.payload["provider_timeline"][0]["raw"] == "x" * 10_000


def test_replay_persistence_repairs_a_failed_predecessor_from_the_staged_prefix() -> None:
    class Store:
        def __init__(self) -> None:
            self.rewrites: list[list[dict[str, Any]]] = []
            self.appends: list[dict[str, Any]] = []

        def rewrite(self, events: list[dict[str, Any]]) -> None:
            self.rewrites.append([dict(event) for event in events])

        def append(self, event: dict[str, Any]) -> None:
            self.appends.append(dict(event))

    async def failed_predecessor() -> None:
        raise OSError("replay disk full")

    async def scenario() -> Store:
        store = Store()
        owner = object.__new__(EventOutbox)
        owner._store = store
        owner.session_id = "session-replay-repair"
        owner._persistence_failed_seqs = set()
        owner._persistence_errors = []
        await EventOutbox._persist_after(
            owner,
            asyncio.create_task(failed_predecessor()),
            {"type": "done", "seq": 2},
            None,
            [
                {"type": "agent_message.delta", "seq": 1},
                {"type": "done", "seq": 2},
            ],
        )
        return store

    store = asyncio.run(scenario())
    assert store.rewrites == [[
        {"type": "agent_message.delta", "seq": 1},
        {"type": "done", "seq": 2},
    ]]
    assert store.appends == []
