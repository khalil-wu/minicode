"""Live +/- line counts while a file tool's arguments stream in."""

from __future__ import annotations

from pathlib import Path

from backend.agent.message import AgentEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.edit_file import EditFileTool
from backend.tools.write_file import WriteFileTool


def _context(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=workspace,
    )


def test_write_file_streams_real_line_counts_against_the_existing_file(tmp_path) -> None:
    target = tmp_path / "page.html"
    target.write_text("a\nb\nc\n", encoding="utf-8")  # 4 lines
    tool = WriteFileTool()
    context = _context(tmp_path)

    first = tool.streamed_input_preview(
        {"file_path": str(target), "content": "one\n"},
        context=context,
    )
    assert first["diff"] == {"plus": 2, "minus": 4}
    # The baseline is read once and cached in the private channel; the next
    # delta must not re-read the workspace.
    assert first["_baseline_lines"] == 4

    second = tool.streamed_input_preview(
        {"file_path": str(target), "content": "one\ntwo\nthree\n"},
        context=None,  # prior alone is enough
        prior=first,
    )
    assert second["diff"] == {"plus": 4, "minus": 4}


def test_write_file_new_file_counts_from_zero(tmp_path) -> None:
    tool = WriteFileTool()
    preview = tool.streamed_input_preview(
        {"file_path": str(tmp_path / "fresh.html"), "content": "x\ny\n"},
        context=_context(tmp_path),
    )
    assert preview["diff"] == {"plus": 3, "minus": 0}


def test_write_file_without_path_or_content_stays_a_plain_preview(tmp_path) -> None:
    tool = WriteFileTool()
    assert tool.streamed_input_preview({}, context=_context(tmp_path)) == {}
    assert tool.streamed_input_preview(
        {"file_path": "x.html"},
        context=_context(tmp_path),
    ) == {"file_path": "x.html"}


def test_edit_file_live_counts_match_the_committed_region_algorithm(tmp_path) -> None:
    tool = EditFileTool()
    preview = tool.streamed_input_preview(
        {
            "file_path": "page.html",
            "old_string": "line one\nline two\n",
            "new_string": "line one\nline two\nline three\n",
        },
        context=_context(tmp_path),
    )
    assert preview["diff"] == {"plus": 1, "minus": 0}


def test_tool_call_event_carries_sanitized_diff() -> None:
    event = AgentEvent.tool_call(
        id="call_1",
        name="write_file",
        args={"file_path": "x.html"},
        status="pending",
        diff={"plus": 7, "minus": 2},
    )
    assert event.data["diff"] == {"plus": 7, "minus": 2}

    # Empty or negative counts never reach the wire.
    empty = AgentEvent.tool_call(id="call_2", name="write_file", args={}, diff={"plus": 0, "minus": 0})
    assert "diff" not in empty.data
    negative = AgentEvent.tool_call(id="call_3", name="write_file", args={}, diff={"plus": -1, "minus": -4})
    assert "diff" not in negative.data
