"""Regression tests for read->write expected_hash guards."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.tool_batch_execution import (
    _refresh_read_file_hashes_after_write,
    execute_tool_batch,
)
from backend.agent.tool_execution import inject_expected_hash
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.atomic_io import canonical_file_path_key
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.apply_patch import ApplyPatchTool, build_apply_patch_diff_payload
from backend.tools.edit_file import EditFileTool
from backend.tools.file_tools_common import content_hash
from backend.tools.read_file import ReadFileTool
from backend.tools.write_file import WriteFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.base import PermissionLevel


def _ctx(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        session_id="s",
        workspace_root=tmp_path,
        metadata={"_read_file_hashes": {}},
    )


def test_execution_guard_prefers_read_time_hash(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    key = canonical_file_path_key(target)
    ctx.metadata["_read_file_hashes"][key] = "read-time-hash"
    target.write_text("new-on-disk\n", encoding="utf-8")

    args = {"file_path": str(target), "content": "fresh\n"}
    from backend.agent.tool_execution import _execution_arguments_for_tool
    from backend.tools.registry import ToolRegistry as _Registry

    class _NoopTool:
        pass

    execution_args = _execution_arguments_for_tool(
        ToolCallEvent(
            id="write-1",
            name="write_file",
            arguments=args,
        ),
        tool_registry=_Registry(),
        tool_ctx=ctx,
    )
    args = execution_args
    assert args["expected_hash"] == "read-time-hash"


def test_write_rejects_stale_read_time_hash(tmp_path: Path) -> None:
    target = tmp_path / "b.py"
    target.write_text("old\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    read_hash = content_hash("old\n")
    key = canonical_file_path_key(target)
    ctx.metadata["_read_file_hashes"][key] = read_hash
    target.write_text("changed\n", encoding="utf-8")

    args = {"file_path": str(target), "content": "next\n"}
    inject_expected_hash(args, key, read_time_hashes=ctx.metadata["_read_file_hashes"])
    result = asyncio.run(WriteFileTool().execute(args, ctx))
    assert result.is_error
    assert (
        "changed on disk" in result.content.lower() or "hash" in result.content.lower()
    )


def test_successful_write_advances_read_time_hash_for_next_edit(tmp_path: Path) -> None:
    target = tmp_path / "same-turn.py"
    target.write_text("first\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    key = canonical_file_path_key(target)
    ctx.metadata["_read_file_hashes"][key] = content_hash("first\n")
    target.write_text("second\n", encoding="utf-8")

    _refresh_read_file_hashes_after_write(
        ToolCallEvent(
            id="write-1",
            name="write_file",
            arguments={"file_path": str(target), "content": "second\n"},
        ),
        {"files": [{"path": str(target), "status": "modified"}]},
        ctx,
    )

    args = {"file_path": str(target), "content": "third\n"}
    inject_expected_hash(args, key, read_time_hashes=ctx.metadata["_read_file_hashes"])
    assert args["expected_hash"] == content_hash("second\n")


def test_same_turn_edits_use_hash_from_previous_successful_edit(tmp_path: Path) -> None:
    target = tmp_path / "serial-edits.py"
    original = "left = 1\nright = 2\n"
    target.write_text(original, encoding="utf-8")
    tool_ctx = _ctx(tmp_path)
    tool_ctx.metadata["_read_file_hashes"][canonical_file_path_key(target)] = (
        content_hash(original)
    )

    class _UnattendedEditFileTool(EditFileTool):
        # This test exercises optimistic hash advancement, not the interactive
        # diff-review gate which is covered by the permission suite.
        def check_permission(self, args=None, context=None):
            return PermissionLevel.AUTO

    registry = ToolRegistry()
    registry.register(_UnattendedEditFileTool())
    probe = {
        "file_path": str(target),
        "old_string": "left = 1",
        "new_string": "left = 10",
    }
    inject_expected_hash(
        probe,
        str(target),
        read_time_hashes=dict(tool_ctx.metadata["_read_file_hashes"]),
    )
    assert probe["expected_hash"] == content_hash(original)
    permission = PermissionContext(
        mode="auto", approval_policy="on-request", source="test"
    )
    tool_ctx.permission = permission
    calls = [
        ToolCallEvent(
            id="edit-left",
            name="edit_file",
            arguments={
                "file_path": str(target),
                "old_string": "left = 1",
                "new_string": "left = 10",
            },
        ),
        ToolCallEvent(
            id="edit-right",
            name="edit_file",
            arguments={
                "file_path": str(target),
                "old_string": "right = 2",
                "new_string": "right = 20",
            },
        ),
    ]

    async def collect() -> list:
        state = AgentState(user_message="apply two edits", iterations=1)
        context = ContextBuilder(TokenBudget())
        context.append_assistant_tool_calls(calls)
        return [
            event
            async for event in execute_tool_batch(
                calls,
                ctx=context,
                state=state,
                tool_registry=registry,
                permission_checker=PermissionChecker(
                    PermissionSettings(
                        auto_allow=["edit_file"],
                        require_diff_review=[],
                    ),
                    tmp_path,
                ),
                approval_handler=None,
                skill_manager=None,
                permission_context=permission,
                tool_ctx=tool_ctx,
            )
        ]

    events = asyncio.run(collect())
    results = [event.data for event in events if event.type == "tool_result"]
    assert [result["status"] for result in results] == ["success", "success"], results
    assert target.read_text(encoding="utf-8") == "left = 10\nright = 20\n"


def test_apply_patch_uses_read_time_hash_and_rejects_stale(tmp_path: Path) -> None:
    target = tmp_path / "c.py"
    original = "line1\nline2\n"
    target.write_text(original, encoding="utf-8")
    ctx = _ctx(tmp_path)
    read_hash = content_hash(original)
    key = canonical_file_path_key(target)
    ctx.metadata["_read_file_hashes"][key] = read_hash
    # Keep hunk context matchable, but change file after read.
    target.write_text("line1\nline2\nextra\n", encoding="utf-8")

    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {target.name}\n"
        "@@\n"
        " line1\n"
        "-line2\n"
        "+line2-new\n"
        "*** End Patch\n"
    )
    expected: dict[str, str] = {}
    payload = build_apply_patch_diff_payload(
        patch,
        ctx,
        expected_hashes=expected,
        read_time_hashes=ctx.metadata["_read_file_hashes"],
    )
    assert payload is not None
    assert expected[key] == read_hash

    result = asyncio.run(
        ApplyPatchTool().execute({"patch": patch, "_expected_hashes": expected}, ctx)
    )
    assert result.is_error
    assert (
        "changed on disk" in result.content.lower()
        or "re-read" in result.content.lower()
    )


def test_apply_patch_direct_execute_without_review_still_guards(tmp_path: Path) -> None:
    target = tmp_path / "d.py"
    original = "alpha\n"
    target.write_text(original, encoding="utf-8")
    ctx = _ctx(tmp_path)
    read_hash = content_hash(original)
    key = canonical_file_path_key(target)
    ctx.metadata["_read_file_hashes"][key] = read_hash
    target.write_text("alpha\ntrail\n", encoding="utf-8")

    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {target.name}\n"
        "@@\n"
        "-alpha\n"
        "+gamma\n"
        "*** End Patch\n"
    )
    result = asyncio.run(ApplyPatchTool().execute({"patch": patch}, ctx))
    assert result.is_error
    assert (
        "changed on disk" in result.content.lower()
        or "re-read" in result.content.lower()
    )


def test_edit_file_rejects_without_expected_hash_on_existing(tmp_path: Path) -> None:
    target = tmp_path / "e.py"
    target.write_text("x\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    result = asyncio.run(
        EditFileTool().execute(
            {"file_path": str(target), "old_string": "x\n", "new_string": "y\n"},
            ctx,
        )
    )
    assert result.is_error
    assert "expected_hash" in result.content.lower()


def test_read_file_hashes_round_trip_and_clear() -> None:
    context = ContextBuilder(TokenBudget())
    hashes = context.read_file_hashes()
    for index in range(105):
        hashes[f"C:/workspace/file-{index}.py"] = f"hash-{index}"
    recently_used = hashes.pop("C:/workspace/file-0.py")
    hashes["C:/workspace/file-0.py"] = recently_used

    snapshot = context.export_snapshot()
    assert len(snapshot["read_file_hashes"]) == 100
    assert "C:/workspace/file-0.py" in snapshot["read_file_hashes"]
    assert "C:/workspace/file-1.py" not in snapshot["read_file_hashes"]
    assert snapshot["read_file_hashes"]["C:/workspace/file-104.py"] == "hash-104"

    restored = ContextBuilder(TokenBudget())
    restored.load_snapshot(snapshot)
    assert restored.read_file_hashes() == {
        canonical_file_path_key(path): file_hash
        for path, file_hash in snapshot["read_file_hashes"].items()
    }

    restored.clear()
    assert restored.read_file_hashes() == {}


class _ReadExistingFileLLM(LLMAdapter):
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="read-existing",
                        name="read_file",
                        arguments={"file_path": self.file_path},
                    )
                ],
            )
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Read complete.")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Read complete."


class _WriteExistingFileLLM(LLMAdapter):
    def __init__(self, file_path: str, content: str) -> None:
        self.file_path = file_path
        self.content = content
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="write-existing",
                        name="write_file",
                        arguments={
                            "file_path": self.file_path,
                            "content": self.content,
                        },
                    )
                ],
            )
        else:
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK, content="Write complete."
            )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Write complete."


def _run_file_turn(
    *,
    root: Path,
    context: ContextBuilder,
    llm: LLMAdapter,
) -> list:
    artifact_store = ArtifactStore(storage_dir=str(root / ".artifacts"))
    registry = ToolRegistry()
    registry.register(ReadFileTool(artifact_store))

    class _UnattendedWriteFileTool(WriteFileTool):
        def check_permission(self, args=None, context=None):
            return PermissionLevel.AUTO

    registry.register(_UnattendedWriteFileTool())

    async def collect() -> list:
        return [
            event
            async for event in run_agent_loop(
                user_message="Continue the interrupted file task.",
                llm=llm,
                tool_registry=registry,
                artifact_store=artifact_store,
                permission_checker=PermissionChecker(
                    settings=PermissionSettings(require_diff_review=[]),
                    workspace_root=root,
                ),
                agent_settings=AgentSettings(max_iterations=4),
                permission_context=PermissionContext(
                    mode="auto", approval_policy="on-request"
                ),
                session_context=AgentLoopSessionContext(workspace_root=root),
                context_builder=context,
            )
        ]

    return asyncio.run(collect())


def test_interrupted_turn_can_write_unchanged_file_without_rereading(
    tmp_path: Path,
) -> None:
    target = tmp_path / "resume.py"
    target.write_text("before\n", encoding="utf-8")
    first_context = ContextBuilder(TokenBudget())
    _run_file_turn(
        root=tmp_path,
        context=first_context,
        llm=_ReadExistingFileLLM(str(target)),
    )

    restored_context = ContextBuilder(TokenBudget())
    restored_context.load_snapshot(first_context.export_snapshot())
    events = _run_file_turn(
        root=tmp_path,
        context=restored_context,
        llm=_WriteExistingFileLLM(str(target), "after\n"),
    )

    result = next(
        event
        for event in events
        if event.type == "tool_result" and event.data.get("id") == "write-existing"
    )
    assert result.data["status"] == "success", result.data
    assert target.read_text(encoding="utf-8") == "after\n"


def test_interrupted_turn_still_rejects_externally_changed_file(tmp_path: Path) -> None:
    target = tmp_path / "externally-changed.py"
    target.write_text("before\n", encoding="utf-8")
    first_context = ContextBuilder(TokenBudget())
    _run_file_turn(
        root=tmp_path,
        context=first_context,
        llm=_ReadExistingFileLLM(str(target)),
    )
    snapshot = first_context.export_snapshot()
    target.write_text("external\n", encoding="utf-8")

    restored_context = ContextBuilder(TokenBudget())
    restored_context.load_snapshot(snapshot)
    events = _run_file_turn(
        root=tmp_path,
        context=restored_context,
        llm=_WriteExistingFileLLM(str(target), "agent-write\n"),
    )

    result = next(
        event
        for event in events
        if event.type == "tool_result" and event.data.get("id") == "write-existing"
    )
    assert result.data["status"] in {"failed", "blocked"}
    assert target.read_text(encoding="utf-8") == "external\n"
