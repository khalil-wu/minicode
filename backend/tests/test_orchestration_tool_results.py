"""Regression coverage for delegated completion and rich MCP error results."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.tool_events import status_for_result
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.tools import agent_tools
from backend.tools.base import ToolResult


@pytest.mark.parametrize(
    ("child_statuses", "expected_status", "expected_error"),
    [
        (("completed", "completed"), "completed", False),
        (("partial", "completed"), "partial", False),
        (("partial", "cancelled"), "partial", False),
        (("cancelled", "completed"), "partial", False),
        (("cancelled", "cancelled"), "cancelled", False),
        (("failed", "completed"), "failed", True),
        (("blocked", "completed"), "failed", True),
        (("exception", "completed"), "failed", True),
    ],
)
def test_parallel_task_preserves_child_completion_and_result_identity(
    monkeypatch, child_statuses, expected_status, expected_error
):
    registered_ids = []

    class Runtime:
        async def acquire_subagent_slot(self, *args, **kwargs):
            return True

        def register_subagent_task(self, subagent_id, *args, **kwargs):
            registered_ids.append(subagent_id)

        def release_subagent_slot(self, *args, **kwargs):
            pass

        def release_subagent_task(self, *args, **kwargs):
            pass

    monkeypatch.setattr(agent_tools, "require_runtime_from_context", lambda context: Runtime())
    tool = agent_tools.TaskTool(artifact_store=object())

    async def child(**kwargs):
        index = kwargs["subtask_index"]
        status = child_statuses[index]
        if status == "exception":
            raise RuntimeError("provider stopped before a result")
        return ToolResult(
            content=f"Child {index} output",
            status=status,
            is_error=status == "failed",
            artifact_id="art_child_result" if index == 1 else None,
        )

    monkeypatch.setattr(tool, "_run_single_subtask", child)
    context = SimpleNamespace(cancel_event=None, metadata={}, task_id="parent", session_id="session")
    result = asyncio.run(tool._run_parallel_subtasks(
        [{"description": "First", "prompt": "First"}, {"description": "Second", "prompt": "Second"}],
        context,
    ))

    assert result.status == expected_status
    assert result.is_error is expected_error
    assert status_for_result(result) == ("success" if expected_status == "completed" else expected_status)
    subtasks = result.runtime_metadata["parallel_subtasks"]
    assert [item["subagent_id"] for item in subtasks] == registered_ids
    assert len(set(registered_ids)) == 2
    assert [item["status"] for item in subtasks] == [
        "failed" if status == "exception" else status for status in child_statuses
    ]
    assert subtasks[1]["artifact_id"] == "art_child_result"
    for item in subtasks:
        assert item["subagent_id"] in result.content
        assert f"({item['status']})" in result.content
        assert item["subagent_id"] not in result.content_preview
        assert f"({item['status']})" in result.content_preview
    assert "Child 1 output" in result.content
    if "exception" in child_statuses:
        assert "provider stopped before a result" in result.content


class ResultClient:
    connected = True

    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, args, *, request_owner=None):
        return self.result


def test_mcp_error_keeps_structured_diagnostics_media_and_resource_artifact(tmp_path):
    response = MCPCallResult(
        content=[
            {"type": "text", "text": "Operation failed after generating diagnostics"},
            {"type": "image", "data": "AQID", "mimeType": "image/png"},
            {"type": "audio", "data": "BAUG", "mimeType": "audio/wav"},
            {"type": "resource_link", "uri": "diagnostics://next", "name": "Detailed report"},
            {"type": "resource", "resource": {"uri": "diagnostics://text", "text": "Captured diagnostic", "mimeType": "text/plain"}},
            {"type": "resource", "resource": {"uri": "diagnostics://binary", "blob": "AQID", "mimeType": "application/octet-stream"}},
        ],
        is_error=True,
        structured_content={"reason": "limit_reached", "retry_after_seconds": 30},
        meta={"private_trace": "trace-visible-only-to-runtime"},
    )
    proxy = MCPToolProxy(
        "diagnostics", MCPToolDef(name="run", description="Run"),
        ResultClient(response), ArtifactStore(storage_dir=tmp_path / "artifacts"),
    )

    result = asyncio.run(proxy.execute({}))

    assert result.status == "failed"
    assert result.is_error is True
    assert "Operation failed after generating diagnostics" in result.content
    assert '"reason": "limit_reached"' in result.content
    assert '"retry_after_seconds": 30' in result.content
    assert "Captured diagnostic" in result.content
    assert "diagnostics://next" in result.content
    assert result.images == [{"media_type": "image/png", "data": "AQID"}]
    assert result.audios == [{"media_type": "audio/wav", "data": "BAUG"}]
    assert result.artifact_id
    assert result.artifact_media_type == "application/octet-stream"
    assert result.artifact_bytes == 3
    assert result.artifact_id in result.content
    assert result.runtime_metadata["mcp"]["structuredContent"] == response.structured_content
    assert result.runtime_metadata["mcp"]["_meta"] == response.meta
    assert "private_trace" not in result.to_context_string()
    assert "trace-visible-only-to-runtime" not in result.to_context_string()


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_content"),
    [
        (MCPCallResult(is_error=True, structured_content={"reason": "failed"}), "failed", '"reason": "failed"'),
        (MCPCallResult(is_error=True), "failed", "MCP tool execution failed"),
        (MCPCallResult(structured_content={"error": "business data", "status": "failed"}), "success", '"error": "business data"'),
    ],
)
def test_mcp_outcome_uses_protocol_error_flag_without_discarding_content(
    response, expected_status, expected_content
):
    proxy = MCPToolProxy("protocol", MCPToolDef(name="result", description="Result"), ResultClient(response))

    result = asyncio.run(proxy.execute({}))

    assert result.status == expected_status
    assert result.is_error is response.is_error
    assert expected_content in result.content
