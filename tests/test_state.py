"""Tests for backend.agent.state — AgentState and ToolCallRecord."""

from __future__ import annotations

import pytest

from backend.agent.state import AgentState, ToolCallRecord


# ── ToolCallRecord ─────────────────────────────────────────────────────────


class TestToolCallRecord:
    """ToolCallRecord is a plain dataclass for per-call metadata."""

    def test_minimal_creation(self) -> None:
        record = ToolCallRecord(tool_name="read_file")
        assert record.tool_name == "read_file"
        assert record.tool_input == {}
        assert record.tool_output is None
        assert record.status == "success"

    def test_full_creation(self) -> None:
        record = ToolCallRecord(
            tool_name="web_fetch",
            tool_input={"url": "https://example.com"},
            tool_output="<html>...</html>",
            artifact_id="art_123",
            source_url="https://example.com",
            extraction_status="partial",
            content_preview="first 200 chars",
            evidence_type="fetched",
            provider="openai",
            provider_error_type=None,
            status="error",
        )
        assert record.tool_name == "web_fetch"
        assert record.tool_input == {"url": "https://example.com"}
        assert record.status == "error"
        assert record.evidence_type == "fetched"

    def test_default_status_is_success(self) -> None:
        record = ToolCallRecord(tool_name="x")
        assert record.status == "success"

    def test_artifact_id_default_none(self) -> None:
        record = ToolCallRecord(tool_name="x")
        assert record.artifact_id is None


# ── AgentState.record_tool_call() ─────────────────────────────────────────


class TestRecordToolCall:
    """record_tool_call() appends ToolCallRecord and updates bookkeeping."""

    def test_appends_to_tool_calls(self) -> None:
        state = AgentState(user_message="test")
        state.record_tool_call("read_file", {"file_path": "x.py"}, "content")
        assert len(state.tool_calls) == 1
        assert state.tool_calls[0].tool_name == "read_file"

    def test_is_error_maps_to_error_status(self) -> None:
        state = AgentState(user_message="test")
        state.record_tool_call("read_file", {"file_path": "x.py"}, "err", is_error=True)
        assert state.tool_calls[-1].status == "error"

    def test_explicit_status_overrides_is_error(self) -> None:
        state = AgentState(user_message="test")
        state.record_tool_call("read_file", {"file_path": "x.py"}, "ok", is_error=True, status="success")
        assert state.tool_calls[-1].status == "success"

    def test_artifact_id_tracked(self) -> None:
        state = AgentState(user_message="test")
        state.record_tool_call("read_file", {"file_path": "x.py"}, "big", artifact_id="art_1")
        assert "art_1" in state.artifact_refs
