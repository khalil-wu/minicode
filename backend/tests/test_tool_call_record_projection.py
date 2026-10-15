"""Drift guards for the REST projection of the internal tool-call record."""

from __future__ import annotations

import dataclasses
from copy import deepcopy

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
