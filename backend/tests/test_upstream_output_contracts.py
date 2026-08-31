from __future__ import annotations

from backend.agent.context import ContextBuilder
from backend.agent.tool_execution import _force_artifact_for_oversized_tool_result
from backend.artifact.store import ArtifactStore
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOOL_RESULT_LINES,
    ToolResult,
    truncate_text_head,
    truncate_tool_result,
)
from backend.tools.subagent_result import PER_TASK_OUTPUT_CAP, full_subagent_result


def test_pi_head_truncation_keeps_complete_utf8_lines() -> None:
    line = "汉字" * 40
    result = truncate_text_head(
        "\n".join([line] * 20),
        max_lines=20,
        max_bytes=1_000,
    )

    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert result.output_bytes <= 1_000
    assert result.content.splitlines()
    assert all(rendered == line for rendered in result.content.splitlines())


def test_pi_head_truncation_uses_the_2000_line_contract() -> None:
    result = truncate_text_head("\n".join(f"line {index}" for index in range(2_001)))

    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert result.output_lines == MAX_TOOL_RESULT_LINES
    assert result.content.splitlines()[-1] == "line 1999"


def test_minicode_generic_result_limit_counts_characters_not_utf8_bytes() -> None:
    content = "\n".join(["汉" * 1_000] * 40)

    assert len(content) < MAX_TOOL_RESULT_CHARS
    assert len(content.encode("utf-8")) > MAX_TOOL_RESULT_BYTES
    assert truncate_tool_result(content) == content

    oversized = "\n".join(["汉" * 1_000] * 51)
    rendered = truncate_tool_result(oversized)
    assert "50000 character limit" in rendered
    assert len(rendered.split("\n\n", 1)[0]) <= MAX_TOOL_RESULT_CHARS


def test_parallel_subagent_output_reuses_the_shared_50_kib_cap() -> None:
    content = "x" * (MAX_TOOL_RESULT_BYTES + 137)

    rendered, truncated = full_subagent_result(content)

    assert PER_TASK_OUTPUT_CAP == MAX_TOOL_RESULT_BYTES
    assert truncated is True
    assert rendered.startswith("x" * MAX_TOOL_RESULT_BYTES)
    assert "137 bytes omitted" in rendered


def test_artifact_backed_tool_result_keeps_pi_preview_without_double_persistence(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    raw = "\n".join(f"{index:04d}:" + ("x" * 100) for index in range(700))
    call = ToolCallEvent(id="call-large", name="custom_tool", arguments={})
    tool_context = ToolExecutionContext(
        permission=PermissionContext(),
        artifact_store=store,
    )

    artifact_result = _force_artifact_for_oversized_tool_result(
        call,
        ToolResult(content=raw),
        tool_context,
        inline_limit=MAX_TOOL_RESULT_CHARS,
    )

    assert artifact_result.artifact_id
    assert store.get(artifact_result.artifact_id) == raw
    assert artifact_result.artifact_preview == truncate_tool_result(raw)
    assert "0100:" in (artifact_result.artifact_preview or "")

    context = ContextBuilder()
    context.append_tool_result(call.id, call.name, artifact_result)
    model_content = str(context._history[-1].content or "")  # type: ignore[attr-defined]

    assert not model_content.startswith("<persisted-output>")
    assert artifact_result.artifact_id in model_content
    assert "0100:" in model_content
