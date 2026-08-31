"""Drift guards for the REST projection of the internal tool-call record."""

from __future__ import annotations

import dataclasses

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
    )

    payload = ApiToolCallRecord.from_internal(internal).model_dump()

    assert payload == {
        "tool_name": "bash",
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
    }
