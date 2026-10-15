"""Drift guards for the REST projection of the internal tool-call record."""

from __future__ import annotations

import dataclasses
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from backend.agent.state import ToolCallRecord as InternalToolCallRecord
from backend.api.models import ToolCallRecord as ApiToolCallRecord


def test_api_tool_call_record_covers_every_internal_field_or_omits_it_loudly() -> None:
    """A new internal field must be consciously projected or explicitly omitted.

    The REST surface is a curated subset of backend.agent.state.ToolCallRecord.
    Without this guard a new internal field would silently disappear from the
    API payload (the inline dict and the Pydantic model had already drifted
    apart before the single projection was introduced).
    """
    internal_fields = {f.name for f in dataclasses.fields(InternalToolCallRecord)}
    api_fields = set(ApiToolCallRecord.model_fields)

    assert internal_fields == api_fields | set(ApiToolCallRecord.NOT_PROJECTED_FIELDS)
    assert not (api_fields & ApiToolCallRecord.NOT_PROJECTED_FIELDS)


def test_api_tool_call_record_from_internal_maps_projected_fields() -> None:
    internal = InternalToolCallRecord(
        tool_name="bash",
        tool_call_id="call-123",
        tool_input={"command": "ls"},
        tool_output="file.txt",
        artifact_kind="file",
        artifact_media_type="text/plain",
        artifact_bytes=8,
        status="error",
        error_kind="timeout",
        user_summary="listed files",
        developer_detail="exit 0",
        recoverable=False,
        projection="terminal",
        model_observation="ok",
        cleanup_receipt={"reaped": True},
        request_digest="abc",
        result_payload={"id": "call-123", "summary": "internal completed event"},
    )

    payload = ApiToolCallRecord.from_internal(internal).model_dump()

    assert payload == {
        "tool_name": "bash",
        "tool_call_id": "call-123",
        "tool_input": {"command": "ls"},
        "tool_output": "file.txt",
        "artifact_id": None,
        "artifact_kind": "file",
        "artifact_media_type": "text/plain",
        "artifact_bytes": 8,
        "status": "error",
        "error_kind": "timeout",
        "user_summary": "listed files",
        "developer_detail": "exit 0",
        "recoverable": False,
        "projection": "terminal",
        "model_observation": "ok",
        "delegations": [],
        "command_actions": [],
        "stdout_preview": None,
        "stderr_preview": None,
        "exit_code": None,
        "recipient_id": None,
        "recipient_ids": [],
    }


def test_delegations_survive_rest_json_roundtrip_without_mutating_the_internal_record() -> None:
    internal = InternalToolCallRecord(tool_name="task", tool_call_id="spawn-call",
        tool_input={"worker": {"paths": ["src/app.py"]}}, status="success", delegations=[
            {"agent_id": "queued-worker", "name": "实际待执行任务", "status": "pending",
             "started_at": 0, "accepted_at": 100, "input_prompt": "private original instructions"},
            {"agent_id": "running-worker", "name": "实际执行任务", "status": "running",
             "started_at": 120, "accepted_at": 90},
        ])
    original = deepcopy(dataclasses.asdict(internal))
    projected = ApiToolCallRecord.from_internal(internal)
    wire = projected.model_dump_json()
    restored = ApiToolCallRecord.model_validate_json(wire)
    assert restored.model_dump() == projected.model_dump()
    assert restored.delegations == [
        {"agent_id": "queued-worker", "name": "实际待执行任务", "status": "pending", "started_at": 0, "accepted_at": 100},
        {"agent_id": "running-worker", "name": "实际执行任务", "status": "running", "started_at": 120, "accepted_at": 90},
    ]
    assert "input_prompt" not in wire
    assert dataclasses.asdict(internal) == original
    projected.delegations[0]["name"] = "API caller mutation"
    projected.tool_input["worker"]["paths"].append("src/another.py")
    assert dataclasses.asdict(internal) == original
    assert restored.delegations[0]["name"] == "实际待执行任务"


def test_real_rest_consumer_preserves_public_evidence_and_omits_private_model_only_records(tmp_path, monkeypatch) -> None:
    from backend.agent.message import AgentEvent
    from backend.api import routes_chat
    from backend.config import AppConfig, LLMSettings
    from backend.llm.base import LLMAdapter
    from backend.permissions.checker import PermissionChecker
    from backend.tools.registry import ToolRegistry

    public = InternalToolCallRecord(tool_name="read_file", tool_call_id="public-read", tool_input={"file_path": "public.txt"},
        tool_output="actual public evidence", status="success")
    private = InternalToolCallRecord(tool_name="tool_exec", tool_call_id="private-wrapper", tool_input={"code": "private orchestration"},
        tool_output="PRIVATE_MODEL_ONLY_ENVELOPE", status="success", private_model_only=True)
    original = [deepcopy(dataclasses.asdict(record)) for record in (public, private)]

    class Provider(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            raise AssertionError("The controlled producer supplies typed tool receipts")
            yield

        async def simple_chat(self, messages):
            raise AssertionError("This consumer regression does not sample a model")

    class Producer:
        async def submit(self, submission):
            submission.state.tool_calls.extend((public, private))
            submission.state.reply = "Done."
            yield AgentEvent.done(status="completed")

    config = AppConfig(llm=LLMSettings(api_key="", provider="custom", model="rest-projection-fixture"))
    bootstrap = SimpleNamespace(mcp_manager=None,
        create_tool_registry=lambda *_args, **_kwargs: ToolRegistry(),
        create_permission_checker=lambda **_kwargs: PermissionChecker(config.permissions, tmp_path),
        create_llm=lambda **_kwargs: Provider())
    monkeypatch.setattr("backend.services.chat_api_service.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(routes_chat._state, "bootstrap", bootstrap)
    monkeypatch.setattr(routes_chat, "QueryEngine", Producer)
    app = FastAPI()
    app.include_router(routes_chat.router)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://projection.test") as client:
            response = await client.post("/api/chat", json={"message": "Project actual evidence"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed" and body["reply"] == "Done."
        assert body["tool_calls"] == [ApiToolCallRecord.from_internal(public).model_dump()]
        assert "actual public evidence" in response.text
        assert "PRIVATE_MODEL_ONLY_ENVELOPE" not in response.text
        assert "private-wrapper" not in response.text and "private orchestration" not in response.text
        assert "private_model_only" not in response.text

    asyncio.run(request())
    assert [dataclasses.asdict(record) for record in (public, private)] == original
