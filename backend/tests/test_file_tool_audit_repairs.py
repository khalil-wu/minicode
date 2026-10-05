from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_execution import _execution_arguments_for_tool, store_result, subagent_scope_guard_reason
from backend.conversations.models import ConversationRecord
from backend.conversations.repository import ConversationRepository
from backend.hooks.manager import HookEvent, HookResult
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import ast_tools, tree_sitter_parser, worktree_tools
from backend.tools.apply_patch import ApplyPatchTool, build_apply_patch_diff_payload
from backend.tools.apply_patch_parser import apply_update_hunks, parse_patch
from backend.tools.edit_file import EditFileTool
from backend.tools.file_tools_common import content_hash
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.registry import ToolRegistry
from backend.tools.worktree_tools import CreateWorktreeTool, RemoveWorktreeTool
from backend.workspace.worktree import WorktreeManager


def _context(root, events):
    async def emit(kind, payload):
        events.append((kind, payload))

    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), workspace_root=root,
        emit_event=emit, tool_call_id="audit-repair", conversation_id="audit",
    )


@pytest.mark.parametrize(("path", "read_only", "allowed"), [
    ("allowed/notebook.ipynb", False, True),
    ("other/notebook.ipynb", False, False),
    ("allowed/notebook.ipynb", True, False),
])
def test_notebook_edit_respects_subagent_write_scope(tmp_path, path, read_only, allowed):
    context = _context(tmp_path, [])
    context.metadata.update({"write_scope": ["allowed"], "read_only": read_only})
    registry = ToolRegistry()
    registry.register(NotebookEditTool())
    call = ToolCallEvent(id="scoped-notebook", name="notebook_edit", arguments={
        "notebook_path": path, "cell_id": "known", "new_source": "updated",
    })
    reason = subagent_scope_guard_reason(call, registry, context)
    assert (not reason) is allowed


def test_notebook_scope_and_hash_use_its_declared_path_not_an_unrelated_alias(tmp_path):
    actual = tmp_path / "other" / "notebook.ipynb"
    alias = tmp_path / "allowed" / "notebook.ipynb"
    actual.parent.mkdir()
    alias.parent.mkdir()
    actual.write_text("actual notebook", encoding="utf-8")
    alias.write_text("unrelated file", encoding="utf-8")
    context = _context(tmp_path, [])
    context.metadata.update({"write_scope": ["allowed"], "_read_file_hashes": {
        str(actual): content_hash("actual notebook"), str(alias): content_hash("unrelated file"),
    }})
    registry = ToolRegistry()
    registry.register(NotebookEditTool())
    call = ToolCallEvent(id="notebook-alias", name="notebook_edit", arguments={
        "notebook_path": "other/notebook.ipynb", "file_path": "allowed/notebook.ipynb",
        "cell_id": "known", "new_source": "updated",
    })
    assert subagent_scope_guard_reason(call, registry, context)
    execution_args = _execution_arguments_for_tool(call, tool_registry=registry, tool_ctx=context)
    assert execution_args["expected_hash"] == content_hash("actual notebook")
    assert "expected_hash" not in call.arguments


def test_patch_partial_failure_publishes_only_committed_changes(tmp_path):
    base = tmp_path / "base.txt"
    base.write_text("before\n", encoding="utf-8")
    (tmp_path / "blocker").write_text("occupied", encoding="utf-8")
    events = []
    context = _context(tmp_path, events)

    async def run():
        await EditFileTool().execute({"file_path": "base.txt", "old_string": "before", "new_string": "after"}, context)
        return await ApplyPatchTool().execute({"patch": (
            "*** Begin Patch\n*** Add File: first.txt\n+committed\n"
            "*** Add File: blocker/second.txt\n+not committed\n*** End Patch"
        )}, context)

    result = asyncio.run(run())
    assert result.is_error and result.status == "partial"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "committed\n"
    files = result.runtime_metadata["committed_diff"]["files"]
    assert [entry["path"] for entry in files] == ["first.txt"]
    final_diff = events[-1][1]["diff"]
    assert "base.txt" in final_diff and "first.txt" in final_diff
    assert "second.txt" not in final_diff and "not committed" not in final_diff


def test_patch_failed_move_reports_destination_write_without_source_deletion(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("original\n", encoding="utf-8")
    context = _context(tmp_path, [])
    context.metadata["_read_file_hashes"] = {str(source): content_hash("original\n")}
    unlink = Path.unlink

    def fail_source(self, *args, **kwargs):
        if self == source:
            raise PermissionError("source remains open")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    result = asyncio.run(ApplyPatchTool().execute({"patch": (
        "*** Begin Patch\n*** Update File: source.txt\n*** Move to: destination.txt\n*** End Patch"
    )}, context))
    assert result.status == "partial" and source.exists()
    assert (tmp_path / "destination.txt").read_text(encoding="utf-8") == "original\n"
    file = result.runtime_metadata["committed_diff"]["files"][0]
    assert file["path"] == "destination.txt" and file["status"] == "added"
    assert "destination.txt" in result.content
    assert "source.txt" not in context.turn_diff_tracker.snapshot().unified_diff


@pytest.mark.parametrize("context_line,patch_line", [
    ("keep()  ", "keep()"), ("    keep()", "keep()"),
    ("# keep – punctuation", "# keep - punctuation"),
    ("# keep\u2028text", "# keep\u2028text"),
])
def test_patch_lenient_context_preserves_original_bytes(context_line, patch_line):
    original = "head\r\nold\n" + context_line + "\r\nlast\n"
    patch = "*** Begin Patch\n*** Update File: f\n@@\n head\n-old\n+new\n " + patch_line + "\n*** End Patch"
    result = apply_update_hunks(original, parse_patch(patch)[0].hunks, "f")
    assert result == "head\r\nnew\r\n" + context_line + "\r\nlast\n"


def test_patch_contextless_append_preserves_real_blank_line():
    patch = "*** Begin Patch\n*** Update File: f\n@@\n+appended\n*** End Patch"
    assert apply_update_hunks("before\n\n", parse_patch(patch)[0].hunks, "f") == "before\n\nappended\n"


def _notebook(path):
    old = json.dumps({
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [{"id": "known", "cell_type": "code", "metadata": {}, "source": ["before\n"], "outputs": [], "execution_count": None}],
    })
    path.write_text(old, encoding="utf-8")
    return old


@pytest.mark.parametrize("cell_id", ["cell-1", "missing"])
def test_notebook_replace_missing_cell_never_appends(tmp_path, cell_id):
    path = tmp_path / "notebook.ipynb"
    old = _notebook(path)
    context = _context(tmp_path, [])
    context.metadata["_read_file_hashes"] = {str(path): content_hash(old)}
    result = asyncio.run(NotebookEditTool().execute({"notebook_path": path.name, "cell_id": cell_id, "new_source": "unexpected"}, context))
    assert result.is_error
    assert path.read_text(encoding="utf-8") == old


@pytest.mark.parametrize("mode,args", [
    ("replace", {"cell_id": "known", "new_source": "after\n"}),
    ("insert", {"cell_id": "known", "new_source": "inserted\n", "cell_type": "markdown"}),
    ("delete", {"cell_id": "known"}),
])
def test_notebook_committed_diff_joins_existing_file_edits(tmp_path, mode, args):
    path = tmp_path / "notebook.ipynb"
    old = _notebook(path)
    (tmp_path / "base.txt").write_text("before\n", encoding="utf-8")
    events = []
    context = _context(tmp_path, events)
    context.metadata["_read_file_hashes"] = {str(path): content_hash(old)}

    async def run():
        await EditFileTool().execute({"file_path": "base.txt", "old_string": "before", "new_string": "after"}, context)
        return await NotebookEditTool().execute({"notebook_path": path.name, "edit_mode": mode, **args}, context)

    result = asyncio.run(run())
    assert not result.is_error, result.content
    assert result.runtime_metadata["committed_diff"]["files"][0]["path"] == path.name
    assert "base.txt" in events[-1][1]["diff"] and path.name in events[-1][1]["diff"]
    cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
    if mode == "replace":
        assert cells[0]["id"] == "known" and cells[0]["source"] == ["after\n"]
    elif mode == "insert":
        assert cells[1]["cell_type"] == "markdown"
    else:
        assert cells == []


@pytest.mark.parametrize("source,line", [
    ("first, target = (1, 2)\n", 1),
    ("[first, [target]] = [1, [2]]\n", 1),
    ("first, *target = range(3)\n", 1),
    ("obj.target = 2\n", None),
])
def test_python_definition_finds_binding_patterns_only(source, line):
    assert ast_tools._python_ast_definitions(source, "target") == ([] if line is None else [line])


@pytest.mark.parametrize("extension,source,line", [
    ("go", "package demo\nvar first, target int\n", 2),
    ("go", "package demo\nconst target = 1\n", 2),
    ("go", "package demo\nvar (\n first, target int\n)\n", 3),
    ("go", "package demo\nconst (\n first = 1\n target = 2\n)\n", 4),
    ("rs", "const target: usize = 1;\n", 1),
    ("rs", "static mut target: usize = 1;\n", 1),
])
def test_definition_regex_covers_module_variable_forms(extension, source, line):
    assert ast_tools._regex_definitions(source, "target", extension) == [line]


@pytest.mark.parametrize("language,source", [
    ("go", "package demo\nvar first, target int\nfunc use(){ _ = target }\n"),
    ("go", "package demo\nconst target = 1\nfunc use(){ _ = target }\n"),
    ("rust", "const target: usize = 1;\nfn use_it(){ let _x = target; }\n"),
    ("rust", "static target: usize = 1;\nfn use_it(){ let _x = target; }\n"),
])
def test_tree_sitter_variable_definitions_and_reference_exclusion(language, source):
    if tree_sitter_parser.get_parser(language) is None:
        pytest.skip(f"{language} grammar not installed")
    definitions = tree_sitter_parser.find_definitions(source, "target", language)
    references = tree_sitter_parser.find_references(source, "target", language, include_definitions=False)
    assert definitions and references
    assert {line for line, _ in definitions}.isdisjoint({line for line, _ in references})


def test_hook_worktree_registration_survives_repository_reload(tmp_path, monkeypatch):
    owner = tmp_path / "repo"
    owner.mkdir()
    (owner / ".git").mkdir()
    path = owner / "custom-hook-checkout"
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(workspace_root=str(owner), worktree_path="existing-chat-binding")
    calls = []

    class Hooks:
        def has_hooks(self, event):
            return event in {HookEvent.WORKTREE_CREATE, HookEvent.WORKTREE_REMOVE}
        async def run_worktree_create(self, **kwargs):
            return HookResult(worktree_path=str(path))
        async def run_worktree_remove(self, **kwargs):
            calls.append(kwargs["path"])
            return HookResult()

    async def no_git(_context):
        pytest.fail("registered hook checkout was routed to Git")

    monkeypatch.setattr(worktree_tools, "_resolve_worktree_manager", no_git)
    context = _context(owner, [])
    context.conversation_id = record.id
    context.run_context = RunContext(hook_manager=Hooks(), conversation_repository=repository)
    created = asyncio.run(CreateWorktreeTool().execute({"path": "requested"}, context))
    assert not created.is_error
    worktree_tools._HOOK_CREATED_WORKTREES.clear()
    reloaded = ConversationRepository(repository.storage_root)
    context.run_context.conversation_repository = reloaded
    saved = reloaded.get_conversation(record.id)
    assert saved.worktree_path == "existing-chat-binding"
    assert saved.worktree_registrations[0]["backend"] == "hook"
    clone = reloaded.clone_conversation(record.id)
    assert clone.worktree_registrations == []
    removed = asyncio.run(RemoveWorktreeTool().execute({"path": str(path)}, context))
    assert not removed.is_error
    assert calls == [str(path)]
    assert reloaded.get_conversation(record.id).worktree_registrations == []


def test_old_conversation_defaults_worktree_registration_to_empty():
    record = ConversationRecord.from_dict({"id": "old", "worktree_path": "existing"})
    assert record.worktree_registrations == [] and record.worktree_path == "existing"


def test_worktree_restore_occupied_file_uses_existing_suffix_rule(tmp_path):
    occupied = tmp_path / "checkout"
    occupied.write_text("retain", encoding="utf-8")
    record = SimpleNamespace(id="snapshot123456", snapshot_sha="abc", main_repo_path=str(tmp_path), original_path=str(occupied))
    manager = object.__new__(WorktreeManager)
    manager.repo_root = tmp_path
    manager._snapshot_store = SimpleNamespace(get=lambda _: record)
    calls = []
    manager._git_ok = lambda root, *args: calls.append((root, args)) or True
    result = manager.restore_snapshot(record.id)
    assert result.restored and result.path == tmp_path / "checkout-restored"
    assert occupied.read_text(encoding="utf-8") == "retain"
    assert calls[0][1] == ("worktree", "add", "--detach", str(result.path), "abc")


def test_worktree_dirty_check_uses_bound_runner_without_service_policy(tmp_path, monkeypatch):
    manager = object.__new__(WorktreeManager)
    manager.repo_root = tmp_path
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, " M changed.txt\n", "")
    manager._git_runner = run
    monkeypatch.setattr("backend.services.workspace_service.run_readonly_git", lambda *a, **k: pytest.fail("dirty check rebuilt snapshot policy"))
    assert manager.has_local_changes(tmp_path / "linked")
    assert calls[0][1]["cwd"] == tmp_path / "linked"
    assert "--ignored" in calls[0][0] and "--no-optional-locks" in calls[0][0]


def test_store_result_preserves_real_partial_patch_diff_over_proposed_diff(tmp_path):
    (tmp_path / "blocker").write_text("occupied", encoding="utf-8")
    context = _context(tmp_path, [])
    patch = ("*** Begin Patch\n*** Add File: first.txt\n+committed\n"
        "*** Add File: blocker/second.txt\n+never committed\n*** End Patch")
    proposed = build_apply_patch_diff_payload(patch, context)
    assert len(proposed["files"]) == 2
    tool = ApplyPatchTool()
    call = ToolCallEvent(id="partial-patch", name=tool.name, arguments={"patch": patch})
    result = asyncio.run(tool.execute(call.arguments, context))
    registry = ToolRegistry()
    registry.register(tool)
    builder = ContextBuilder()
    builder.append_user("apply patch")
    builder.append_assistant_tool_calls([call])
    state = AgentState(user_message="apply patch")
    event = store_result(call, result, builder, state, diff=proposed, tool_ctx=context, tool_registry=registry)
    assert event.data["status"] == "partial" and event.data["is_error"]
    assert event.data["diff"] == result.runtime_metadata["committed_diff"]
    assert [file["path"] for file in event.data["diff"]["files"]] == ["first.txt"]
    assert "second.txt" not in json.dumps(event.data["diff"])


def test_store_result_uses_notebook_committed_diff_over_proposed_diff(tmp_path):
    path = tmp_path / "notebook.ipynb"
    old = _notebook(path)
    context = _context(tmp_path, [])
    context.metadata["_read_file_hashes"] = {str(path): content_hash(old)}
    tool = NotebookEditTool()
    call = ToolCallEvent(id="notebook", name=tool.name,
        arguments={"notebook_path": path.name, "cell_id": "known", "new_source": "after\n"})
    result = asyncio.run(tool.execute(call.arguments, context))
    registry = ToolRegistry()
    registry.register(tool)
    builder = ContextBuilder()
    builder.append_user("edit notebook")
    builder.append_assistant_tool_calls([call])
    proposed = {"format": "structured", "stats": {"additions": 1, "deletions": 0, "files_count": 1},
        "files": [{"path": path.name, "patch": "+WRONG_PENDING_OUTPUT", "additions": 1, "deletions": 0}]}
    event = store_result(call, result, builder, AgentState(user_message="edit notebook"),
        diff=proposed, tool_ctx=context, tool_registry=registry)
    assert event.data["status"] == "success"
    assert event.data["diff"] == result.runtime_metadata["committed_diff"]
    assert "after" in json.dumps(event.data["diff"]) and "WRONG_PENDING_OUTPUT" not in json.dumps(event.data["diff"])
