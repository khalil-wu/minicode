import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from fastapi.testclient import TestClient

from backend.agent.loop import run_agent_loop
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import PROJECT_ROOT, AgentSettings, LLMSettings, PermissionSettings, TokenBudget, load_llm_settings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.tools.agent_tools import ReadArtifactTool
from backend.llm.openai_adapter import OpenAIAdapter, _clean_error_message
from backend.agent.message import AgentEvent
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.permissions.checker import PermissionChecker
from backend.agent.tool_execution import generate_diff as _generate_diff
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry
from backend.ws.handler import _build_effective_user_message


def _receive_next_non_task_update(ws, *, max_attempts: int = 20) -> dict[str, object]:
    bookkeeping_events = {
        "task.update",
        "file.changed",
        "session.state_changed",
        "agent.run.started",
        "agent.run.completed",
    }
    for _ in range(max_attempts):
        payload = ws.receive_json()
        if payload.get("type") in bookkeeping_events:
            continue
        return payload
    raise AssertionError("did not receive a non task.update/file.changed websocket event in time")


def _receive_next_type(ws, event_type: str, *, max_attempts: int = 20) -> dict[str, object]:
    """Wait for a semantic event while allowing independent runtime snapshots."""
    for _ in range(max_attempts):
        payload = _receive_next_non_task_update(ws)
        if payload.get("type") == event_type:
            return payload
    raise AssertionError(f"did not receive websocket event type {event_type!r} in time")


def _assert_startup_events(ws) -> list[dict[str, object]]:
    events = [
        _receive_next_non_task_update(ws),
        _receive_next_non_task_update(ws),
    ]
    assert {event["type"] for event in events} == {"mcp_status", "llm.model.updated"}
    return events


async def _fake_agent_loop(*args, **kwargs):
    yield AgentEvent.agent_message_completed("stub reply", source="model_final")
    yield AgentEvent.done(input_tokens=3, output_tokens=2)


async def _fake_recovered_agent_loop(*args, **kwargs):
    yield AgentEvent.error("temporary provider failure", recoverable=True, error_type="api")
    yield AgentEvent.agent_message_completed("recovered reply", source="model_final")
    yield AgentEvent.done(status="completed")


async def _fake_websocket_agent_loop(*args, **kwargs):
    await _admit_fake_turn(kwargs)
    yield AgentEvent.agent_message_completed("stub reply", source="model_final")
    yield AgentEvent.done(input_tokens=3, output_tokens=2)


async def _admit_fake_turn(kwargs) -> None:
    context = kwargs["context_builder"]
    history_start = context.history_length
    context.append_user(kwargs["user_message"])
    await kwargs["metadata"]["commit_turn_admission"](
        boundary_input=SimpleNamespace(consumed_steer=None),
        history_start=history_start,
        history_end=context.history_length,
    )


def test_health_endpoint_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert payload["ready"] is True
    assert payload["components"]["core"]["status"] == "ok"
    assert "version" in payload


def test_status_endpoint_returns_design_sidebars_data() -> None:
    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert {"mcp", "skills", "memory", "llm"} <= payload.keys()
    assert "rag" not in payload


def test_status_endpoint_exposes_capability_snapshot() -> None:
    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert "capabilities" in payload
    assert isinstance(payload["capabilities"]["version"], int)
    assert isinstance(payload["capabilities"]["tools"], list)
    assert isinstance(payload["capabilities"]["tool_views"], list)
    assert isinstance(payload["capabilities"]["commands"], list)
    assert isinstance(payload["capabilities"]["skills"], list)
    assert isinstance(payload["capabilities"]["composer_commands"], list)
    summary = payload["capabilities"]["summary"]
    assert summary["tools_total"] >= summary["direct_tools"] >= 1
    assert len(payload["capabilities"]["tool_views"]) == summary["tools_total"]
    assert sum(1 for view in payload["capabilities"]["tool_views"] if view["direct"]) == summary["direct_tools"]
    assert {
        "name",
        "exposure",
        "direct",
        "schema_available",
        "toolset",
        "capability",
        "permission",
        "read_only",
        "short_description",
    } <= set(payload["capabilities"]["tool_views"][0])
    assert summary["commands"] == len(payload["capabilities"]["commands"])
    assert summary["skills"] == len(payload["capabilities"]["skills"])
    assert summary["mcp_resource_bridge"] is True
    assert summary["deferred_bridge"] is True
    assert summary["skill_catalog"] is bool(payload["capabilities"]["skills"])
    assert any(
        command.get("name") == "conversation.list"
        for command in payload["capabilities"]["commands"]
    )
    assert any(
        command.get("command") == "review"
        for command in payload["capabilities"]["composer_commands"]
    )
    composer_command_names = {
        str(command.get("command", "")).strip()
        for command in payload["capabilities"]["composer_commands"]
    }
    assert {
        "new",
        "clear",
        "permissions",
        "memory",
        "archive",
        "unarchive",
        "tasks",
        "status",
        "help",
    } <= composer_command_names


def test_doctor_endpoint_exposes_agent_capability_summary() -> None:
    with TestClient(app) as client:
        response = client.get("/api/doctor")

    assert response.status_code == 200
    payload = response.json()
    summary = payload["capabilities"]["summary"]
    assert summary["tools_total"] >= summary["direct_tools"] >= 1
    assert summary["mcp_resource_bridge"] is True
    assert summary["deferred_bridge"] is True
    assert summary["skill_catalog"] is bool(payload["capabilities"]["skills"])


def test_status_endpoint_exposes_authoritative_composer_command_schema() -> None:
    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    composer_commands = payload["capabilities"]["composer_commands"]
    review = next(command for command in composer_commands if command.get("command") == "review")
    permissions = next(
        command for command in composer_commands if command.get("command") == "permissions"
    )

    assert {
        "id",
        "command",
        "name",
        "label",
        "description",
        "template",
        "type",
        "source",
        "availability",
        "enabled",
    } <= review.keys()
    assert review["type"] == "template"
    assert review["source"] == "builtin"
    assert review["enabled"] is True
    assert review["availability"]["kind"] == "always"

    assert permissions["type"] == "local"
    assert permissions["availability"]["scope"] == "session"


def test_status_endpoint_exposes_runtime_snapshot() -> None:
    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    runtime = payload.get("runtime", {})
    assert {
        "active_sessions",
        "running_tasks",
        "pending_tasks",
        "completed_tasks",
        "failed_tasks",
        "cancelled_tasks",
    } <= runtime.keys()
    assert "sessions" not in runtime


def test_status_endpoint_does_not_expose_cross_session_runtime_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr("backend.main._create_session_llm", lambda config, model_override=None, **_kwargs: object())
    monkeypatch.setattr(
        "backend.ws.handler.get_available_models",
        lambda provider=None: ["gpt-5.4", "gpt-5.4-mini"],
    )
    monkeypatch.setattr("backend.ws.handler.get_llm_provider", lambda: "openai")

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_runtime_status") as ws:
            _assert_startup_events(ws)

            response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["active_sessions"] == 1
    assert "sessions" not in payload["runtime"]
    rendered = str(payload["runtime"])
    assert "session_test_runtime_status" not in rendered
    assert "gpt-5.4" not in rendered


def test_status_endpoint_exposes_llm_model_options(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "gpt-5.4,gpt-5.4-mini")
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", tmp_path / "settings.json")

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["llm"]["current_model"] == "gpt-5.4"
    assert payload["llm"]["available_models"] == ["gpt-5.4", "gpt-5.4-mini"]


def test_env_example_documents_openmcp_external_provider_variables() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "OPENMCP_SEARCH_URL=" in env_example
    assert "OPENMCP_DOCPARSE_URL=" in env_example


class _StubLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        if False:
            yield None

    async def simple_chat(self, messages):
        return ""


def test_upload_document_stays_draft_until_user_message_is_sent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main._create_session_llm", lambda config, model_override=None, **_kwargs: object())
    # The websocket turn builds its own session adapter; without a stub it would
    # construct a real provider adapter for a test-only API key.
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _StubLLM(),
    )
    monkeypatch.setattr(
        "backend.agent.query_engine.run_agent_loop",
        _fake_websocket_agent_loop,
    )
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr(
        "backend.documents.service._parse_pdf",
        lambda path: {"title": "Paper", "full_text": "Extracted text", "format": "pdf", "pages": 1},
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=session_test_upload") as ws:
            _assert_startup_events(ws)

            response = client.post(
                "/api/uploads",
                params={"session_id": "session_test_upload"},
                files={"file": ("note.txt", b"hello minicode", "text/plain")},
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["file_name"] == "note.txt"
            assert "indexed_chunks" not in payload
            assert payload["artifact_id"]
            assert payload["doc_id"]
            assert payload["attachment"]["file_name"] == "note.txt"
            assert payload["attachment"]["artifact_id"] == payload["artifact_id"]
            assert payload["attachment"]["doc_id"] == payload["doc_id"]
            assert payload["attachment"]["kind"] == "document"

            preview_response = client.get(
                "/api/attachments/preview",
                params={
                    "session_id": "session_test_upload",
                    "conversation_id": payload["conversation_id"],
                    "artifact_id": payload["artifact_id"],
                },
            )
            assert preview_response.status_code == 200
            assert preview_response.json()["content"] == "hello minicode"
            assert preview_response.json()["file_name"] == "note.txt"
            assert preview_response.json()["has_native"] is False

            pdf_response = client.post(
                "/api/uploads",
                params={
                    "session_id": "session_test_upload",
                    "conversation_id": payload["conversation_id"],
                },
                files={"file": ("paper.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
            )
            assert pdf_response.status_code == 200
            assert "data" not in pdf_response.json()["attachment"]
            pdf_payload = pdf_response.json()

            pdf_preview = client.get(
                "/api/attachments/preview",
                params={
                    "session_id": "session_test_upload",
                    "conversation_id": pdf_payload["conversation_id"],
                    "artifact_id": pdf_payload["artifact_id"],
                },
            )
            assert pdf_preview.status_code == 200
            assert pdf_preview.json()["media_type"] == "application/pdf"
            assert pdf_preview.json()["has_native"] is True

            raw_pdf = client.get(
                "/api/attachments/raw",
                params={
                    "session_id": "session_test_upload",
                    "conversation_id": pdf_payload["conversation_id"],
                    "artifact_id": pdf_payload["artifact_id"],
                },
                headers={"Range": "bytes=0-3"},
            )
            assert raw_pdf.status_code == 206
            assert raw_pdf.content == b"%PDF"
            assert raw_pdf.headers["content-range"] == "bytes 0-3/14"

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)
            assert listing["type"] == "conversation.list"
            transcript = listing["active_conversation"]["transcript"]
            assert not any(message.get("attachments") for message in transcript)

            ws.send_json(
                {
                    "type": "user_message",
                    "content": "Summarize the attachment",
                    "attachments": [payload["attachment"]],
                }
            )
            _receive_next_type(ws, "item.completed")
            _receive_next_type(ws, "done")
            _receive_next_type(ws, "conversation.summary.updated")

            ws.send_json({"type": "conversation.list"})
            listing = _receive_next_non_task_update(ws)
            transcript = listing["active_conversation"]["transcript"]
            assert any(
                message["role"] == "user"
                and message.get("attachments")
                and message["attachments"][0]["artifact_id"] == payload["artifact_id"]
                and message["attachments"][0]["file_name"] == "note.txt"
                and message["content"] == "Summarize the attachment"
                for message in transcript
            )


def test_rest_chat_streams_agent_loop_events_into_response(monkeypatch) -> None:
    monkeypatch.setattr("backend.main._create_session_llm", lambda config, model_override=None, **_kwargs: object())
    # The REST route delegates to QueryEngine, which resolves the loop from its
    # own module global; that is the single seam a run passes through.
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_agent_loop)

    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "hello", "max_iterations": 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "stub reply"
    assert payload["stopped_reason"] == "completed"
    assert payload["status"] == "completed"
    assert payload["errors"] == []
    assert payload["iterations"] == 0


def test_rest_chat_keeps_recoverable_errors_separate_from_final_reply(monkeypatch) -> None:
    monkeypatch.setattr("backend.main._create_session_llm", lambda config, model_override=None, **_kwargs: object())
    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", _fake_recovered_agent_loop)

    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "hello", "max_iterations": 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "recovered reply"
    assert "temporary provider failure" not in payload["reply"]
    assert payload["errors"] == ["temporary provider failure"]
    assert payload["status"] == "completed"


def test_windows_entrypoint_does_not_force_reload() -> None:
    source = (PROJECT_ROOT / "backend" / "__main__.py").read_text(encoding="utf-8")

    assert 'reload=True' not in source
