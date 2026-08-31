from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from backend.agent import context as context_module
from backend.agent.context import ContextBuilder
from backend.agent import tool_result_persistence as persistence
from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.state import AgentState, ToolCallRecord
from backend.api.models import ToolCallRecord as ApiToolCallRecord
from backend.config import TokenBudget
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.config import PermissionSettings
from backend.artifact.store import ArtifactStore
from backend.tools.read_file import ReadFileTool
from backend.tools.agent_artifact_tools import ReadArtifactTool
import asyncio


def test_persisted_tool_result_preview_contains_readable_path(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)
    monkeypatch.setattr(persistence, "PERSIST_THRESHOLD_CHARS", 100)

    persisted = persistence.persist_tool_result("x" * 500, "call/web", "web_fetch")

    assert persisted is not None
    assert persisted.filepath in persisted.preview
    assert "Full output saved to:" in persisted.preview
    assert Path(persisted.filepath).read_text(encoding="utf-8") == "x" * 500


def test_owned_persisted_tool_result_is_readable_but_not_writable_for_owner(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)

    persisted = persistence.persist_tool_result(
        "cached result",
        "call-owner",
        "web_fetch",
        force=True,
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    assert persisted is not None
    result_file = Path(persisted.filepath)

    owner_permission = PermissionContext(
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    checker = PermissionChecker(PermissionSettings(path_allowlist=["src/**"]), workspace)
    read_decision = checker.evaluate(
        "read_file",
        {"file_path": str(result_file)},
        context=owner_permission,
        tool=ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")),
    )
    write_decision = checker.evaluate(
        "write_file",
        {"file_path": str(result_file), "content": "overwrite"},
        context=owner_permission,
    )

    assert read_decision.capability_allowed is True
    assert write_decision.capability_allowed is True
    assert write_decision.permission_level.value == "diff"
    assert write_decision.decision == "ask"

    context = ToolExecutionContext(
        permission=owner_permission,
        workspace_root=workspace,
        conversation_id="conv-a",
    )
    result = asyncio.run(ReadFileTool(ArtifactStore(storage_dir=str(tmp_path / "artifacts"))).execute(
        {"file_path": str(result_file)}, context=context,
    ))
    assert result.is_error is False
    assert "cached result" in result.content


def test_owned_persisted_tool_result_rejects_cross_owner_and_legacy_reads(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)

    persisted = persistence.persist_tool_result(
        "secret from conv-a",
        "call-owner",
        "web_fetch",
        force=True,
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    assert persisted is not None
    owned_file = Path(persisted.filepath)
    legacy_file = result_dir / "mc_web_fetch_legacy.txt"
    legacy_file.write_text("legacy global result", encoding="utf-8")

    foreign_permission = PermissionContext(
        conversation_id="conv-b",
        workspace_root=workspace,
    )
    checker = PermissionChecker(PermissionSettings(path_allowlist=["src/**"]), workspace)
    foreign_decision = checker.evaluate(
        "read_file",
        {"file_path": str(owned_file)},
        context=foreign_permission,
        tool=ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")),
    )
    legacy_decision = checker.evaluate(
        "read_file",
        {"file_path": str(legacy_file)},
        context=foreign_permission,
        tool=ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")),
    )

    assert foreign_decision.capability_allowed is False
    assert legacy_decision.capability_allowed is False

    foreign_context = ToolExecutionContext(
        permission=foreign_permission,
        workspace_root=workspace,
        conversation_id="conv-b",
    )
    read_tool = ReadFileTool(ArtifactStore(storage_dir=str(tmp_path / "artifacts")))
    foreign_read = asyncio.run(read_tool.execute({"file_path": str(owned_file)}, context=foreign_context))
    legacy_read = asyncio.run(read_tool.execute({"file_path": str(legacy_file)}, context=foreign_context))

    assert foreign_read.is_error is True
    assert legacy_read.is_error is True


def test_persisted_tool_result_is_readable_from_isolated_cwd_without_workspace(
    tmp_path, monkeypatch
) -> None:
    """Subagents/evaluators may have no workspace_root but still need recovery."""
    result_dir = tmp_path / "tool-results"
    result_dir.mkdir(parents=True)
    result_file = result_dir / "cached.txt"
    result_file.write_text("cached from an isolated cwd", encoding="utf-8")
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    isolated_cwd = tmp_path / "isolated-repository"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)

    context = ToolExecutionContext(
        permission=PermissionContext(),
        workspace_root=None,
    )
    result = asyncio.run(
        ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")).execute(
            {"file_path": str(result_file)}, context=context,
        )
    )

    assert result.is_error is False
    assert "cached from an isolated cwd" in result.content


def test_read_artifact_accepts_only_bare_persisted_result_filename(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    result_dir.mkdir(parents=True)
    result_file = result_dir / "mc_web_fetch_example.txt"
    result_file.write_text("cached web result", encoding="utf-8")
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)

    tool = ReadArtifactTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    accepted = asyncio.run(tool.execute({"artifact_id": result_file.name}))
    traversal = asyncio.run(tool.execute({"artifact_id": f"../{result_file.name}"}))
    absolute = asyncio.run(tool.execute({"artifact_id": str(result_file)}))

    assert accepted.is_error is False
    assert accepted.content == "cached web result"
    assert traversal.is_error is True
    assert absolute.is_error is True


def test_read_artifact_persisted_result_is_owner_scoped(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)

    persisted = persistence.persist_tool_result(
        "artifact cache for conv-a",
        "call-artifact",
        "web_fetch",
        force=True,
        conversation_id="conv-a",
        workspace_root=workspace,
    )
    assert persisted is not None
    result_name = Path(persisted.filepath).name
    tool = ReadArtifactTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    owner_context = ToolExecutionContext(
        permission=PermissionContext(conversation_id="conv-a", workspace_root=workspace),
        workspace_root=workspace,
        conversation_id="conv-a",
    )
    foreign_context = ToolExecutionContext(
        permission=PermissionContext(conversation_id="conv-b", workspace_root=workspace),
        workspace_root=workspace,
        conversation_id="conv-b",
    )
    accepted = asyncio.run(tool.execute({"artifact_id": result_name}, context=owner_context))
    foreign = asyncio.run(tool.execute({"artifact_id": result_name}, context=foreign_context))

    assert accepted.is_error is False
    assert accepted.content == "artifact cache for conv-a"
    assert foreign.is_error is True


def test_oversized_tool_results_are_persisted_before_stable_preview(tmp_path, monkeypatch) -> None:
    result_dir = tmp_path / "tool-results"
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)
    monkeypatch.setattr(persistence, "PERSIST_THRESHOLD_CHARS", 100)

    builder = ContextBuilder(token_budget=TokenBudget(total=200_000, response_reserve=1_000))
    builder.append_tool_result("call-large", "web_fetch", ToolResult(content="payload\n" * 20_000))

    stored = str(builder._history[-1].content or "")
    assert stored.startswith("<persisted-output>")
    assert str(result_dir) in stored
    assert list(result_dir.glob("call-large_*.txt"))


def test_aggregate_tool_budget_never_replaces_an_already_seen_result(
    tmp_path, monkeypatch
) -> None:
    """Claude Code freezes prior inline/replaced decisions for cache stability."""
    result_dir = tmp_path / "tool-results"
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)
    monkeypatch.setattr(context_module, "PER_MESSAGE_TOOL_RESULT_BUDGET_CHARS", 200)

    builder = ContextBuilder(
        token_budget=TokenBudget(total=200_000, response_reserve=1_000)
    )
    builder.append_user("inspect")
    builder.append_assistant_tool_calls(
        [ToolCallEvent(id="call-old", name="web_fetch", arguments={})]
    )
    builder.append_tool_result(
        "call-old", "web_fetch", ToolResult(content="o" * 70)
    )
    old_content = str(builder._history[-1].content or "")
    assert not old_content.startswith("<persisted-output>")

    builder.append_assistant_tool_calls(
        [ToolCallEvent(id="call-new", name="web_fetch", arguments={})]
    )
    builder.append_tool_result(
        "call-new", "web_fetch", ToolResult(content="n" * 70)
    )

    tool_contents = {
        str(message.tool_call_id): str(message.content or "")
        for message in builder._history
        if message.role == "tool"
    }
    assert tool_contents["call-old"] == old_content
    assert tool_contents["call-new"].startswith("<persisted-output>")
    assert list(result_dir.glob("call-new_*.txt"))
    assert not list(result_dir.glob("call-old_*.txt"))


def test_snapshot_restore_freezes_existing_inline_tool_results(
    tmp_path, monkeypatch
) -> None:
    result_dir = tmp_path / "tool-results"
    monkeypatch.setattr(persistence, "TOOL_RESULT_DATA_DIR", result_dir)
    monkeypatch.setattr(persistence, "_INITIALIZED", False)
    monkeypatch.setattr(context_module, "PER_MESSAGE_TOOL_RESULT_BUDGET_CHARS", 200)

    original = ContextBuilder(
        token_budget=TokenBudget(total=200_000, response_reserve=1_000)
    )
    original.append_user("inspect")
    original.append_assistant_tool_calls(
        [ToolCallEvent(id="call-old", name="web_fetch", arguments={})]
    )
    original.append_tool_result(
        "call-old", "web_fetch", ToolResult(content="o" * 70)
    )

    restored = ContextBuilder(
        token_budget=TokenBudget(total=200_000, response_reserve=1_000)
    )
    restored.load_snapshot(original.export_snapshot())
    restored.append_assistant_tool_calls(
        [ToolCallEvent(id="call-new", name="web_fetch", arguments={})]
    )
    restored.append_tool_result(
        "call-new", "web_fetch", ToolResult(content="n" * 70)
    )

    tool_contents = {
        str(message.tool_call_id): str(message.content or "")
        for message in restored._history
        if message.role == "tool"
    }
    assert not tool_contents["call-old"].startswith("<persisted-output>")
    assert tool_contents["call-new"].startswith("<persisted-output>")


def test_structured_tool_error_metadata_survives_copy_checkpoint_and_rest_payload(tmp_path) -> None:
    result = ToolResult(
        content="raw provider detail",
        is_error=True,
        error_kind="routing_error",
        user_summary="工具路由失败。",
        developer_detail="route mcp__demo__search was unavailable",
        recoverable=False,
        projection="warning",
        model_observation="Call tool_search before retrying.",
    )
    copied = replace(result, duration_ms=12)
    assert copied.error_kind == "routing_error"
    assert copied.user_summary == "工具路由失败。"
    assert copied.developer_detail == "route mcp__demo__search was unavailable"
    assert copied.recoverable is False
    assert copied.projection == "warning"
    assert copied.model_observation == "Call tool_search before retrying."

    state = AgentState(user_message="inspect")
    state.record_tool_call(
        "mcp__demo__search",
        {"query": "needle"},
        copied.content,
        is_error=True,
        status="failed",
        error_kind=copied.error_kind,
        user_summary=copied.user_summary,
        developer_detail=copied.developer_detail,
        recoverable=copied.recoverable,
        projection=copied.projection,
        model_observation=copied.model_observation,
    )
    save_checkpoint(
        session_id="structured-result",
        user_message=state.user_message,
        iterations=1,
        reply="",
        messages=[],
        tool_calls=state.tool_calls,
        active_skills=[],
        disabled_tools=set(),
        stopped_reason="interrupted",
        last_mutation_index=0,
        base_dir=tmp_path,
    )
    checkpoint = load_latest_checkpoint("structured-result", base_dir=tmp_path)
    assert checkpoint is not None
    restored = ToolCallRecord(**checkpoint.tool_calls[0])
    assert restored.recoverable is False
    assert restored.model_observation == "Call tool_search before retrying."

    api_record = ApiToolCallRecord.model_validate(checkpoint.tool_calls[0])
    payload = api_record.model_dump()
    assert payload["error_kind"] == "routing_error"
    assert payload["user_summary"] == "工具路由失败。"
    assert payload["developer_detail"] == "route mcp__demo__search was unavailable"
    assert payload["recoverable"] is False
    assert payload["projection"] == "warning"
    assert payload["model_observation"] == "Call tool_search before retrying."
