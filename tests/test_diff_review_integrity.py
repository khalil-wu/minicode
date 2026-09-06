from __future__ import annotations

import asyncio
import re
import subprocess

import pytest

from backend.agent.tool_execution import generate_diff
from backend.agent.turn_diff_tracker import TurnDiffTracker
from backend.atomic_io import canonical_file_path_key
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.review import build_structured_diff_payload, generate_unified_diff
from backend.tools.edit_file import EditFileTool
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.file_tools_common import _generate_limited_unified_diff, content_hash
from backend.tools.write_file import WriteFileTool


@pytest.mark.parametrize("old,new", [
    ("before", "after"),
    ("same", "same\n"),
    ("same\n", "same"),
    ("-- before\n", "++ after\n"),
    ("before \n", "after  \n"),
    ("first\r\nold\r\n", "first\r\nnew\r\n"),
    ("first\rsecond", "first\rchanged"),
    ("first\u2028second\n", "first\u2028changed\n"),
])
@pytest.mark.parametrize("surface", ["approval", "tool_result", "turn"])
def test_diff_round_trips_exact_content_through_git(tmp_path, old, new, surface):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    target = tmp_path / "sample.txt"
    target.write_bytes(old.encode("utf-8"))
    if surface == "approval":
        payload = build_structured_diff_payload("sample.txt", generate_unified_diff("sample.txt", old, new))
        patch = payload["files"][0]["patch"]
        assert payload["stats"]["additions"] > 0
        assert payload["stats"]["deletions"] > 0
    elif surface == "tool_result":
        patch, additions, deletions, truncated = _generate_limited_unified_diff(
            old, new, "sample.txt", max_chars=None,
        )
        assert additions > 0 and deletions > 0 and not truncated
    else:
        tracker = TurnDiffTracker()
        tracker.track_change(old_path="sample.txt", new_path="sample.txt", old_content=old, new_content=new)
        patch = tracker.get_unified_diff()
    applied = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
        input=patch.encode("utf-8"), cwd=tmp_path, capture_output=True,
    )
    assert applied.returncode == 0, applied.stderr.decode("utf-8", errors="replace")
    assert target.read_bytes() == new.encode("utf-8")


@pytest.mark.parametrize("existing", [False, True])
def test_empty_write_has_review_metadata_and_executes(tmp_path, existing):
    target = tmp_path / "empty.txt"
    if existing:
        target.write_bytes(b"before")
    args = {"file_path": "empty.txt", "content": ""}
    payload = generate_diff("write_file", args, workspace_root=tmp_path)
    entry = payload["files"][0]
    assert entry["status"] == ("modified" if existing else "added")
    assert entry["size_bytes"] == 0
    assert entry["deletions"] == int(existing)
    if existing:
        args["expected_hash"] = content_hash("before")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    result = asyncio.run(WriteFileTool().execute(args, context))
    assert not result.is_error, result.content
    assert target.read_bytes() == b""


@pytest.mark.parametrize("content,replace_all,expected", [
    ("\ufeff“old”", False, "\ufeff“new”"),
    ("“old”\n“old”\n", True, "“new”\n“new”\n"),
    ("“old”\n“old”\n", "true", "“new”\n“new”\n"),
])
def test_edit_review_uses_the_actual_quote_and_bom_replacement(tmp_path, content, replace_all, expected):
    target = tmp_path / "quotes.txt"
    target.write_bytes(content.encode("utf-8"))
    args = {"file_path": "quotes.txt", "old_string": '"old"', "new_string": '"new"', "replace_all": replace_all}
    payload = generate_diff("edit_file", args, workspace_root=tmp_path)
    entry = payload["files"][0]
    assert "“new”" in entry["patch"]
    assert entry["size_bytes"] == len(expected.encode("utf-8"))
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    result = asyncio.run(EditFileTool().execute({**args, "expected_hash": content_hash(content)}, context))
    assert not result.is_error, result.content
    assert target.read_bytes() == expected.encode("utf-8")


def test_ambiguous_edit_is_not_previewed_as_a_first_occurrence_replacement(tmp_path):
    target = tmp_path / "repeat.txt"
    target.write_bytes(b"old old\n")
    args = {"file_path": "repeat.txt", "old_string": "old", "new_string": "new", "replace_all": "false"}
    payload = generate_diff("edit_file", args, workspace_root=tmp_path)
    assert payload["format"] == "raw"
    assert "matched 2 places" in payload["raw"]
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    result = asyncio.run(EditFileTool().execute({**args, "expected_hash": content_hash("old old\n")}, context))
    assert result.is_error
    assert target.read_bytes() == b"old old\n"


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"content": "replacement"}),
    ("edit_file", {"old_string": "before", "new_string": "after"}),
])
def test_unreadable_original_is_visible_in_review_instead_of_an_empty_baseline(tmp_path, monkeypatch, tool, args):
    target = tmp_path / "locked.txt"
    target.write_bytes(b"before")
    read_bytes = type(target).read_bytes

    def denied(path, *positional, **kwargs):
        if path == target:
            raise PermissionError("AUDIT_READ_DENIED")
        return read_bytes(path, *positional, **kwargs)

    monkeypatch.setattr(type(target), "read_bytes", denied)
    payload = generate_diff(tool, {"file_path": "locked.txt", **args}, workspace_root=tmp_path)
    assert payload["format"] == "raw"
    assert "AUDIT_READ_DENIED" in payload["raw"]
    assert "files" not in payload


def test_limited_diff_stops_at_the_first_omitted_line_and_counts_all_changes():
    patch, additions, deletions, truncated = _generate_limited_unified_diff(
        "old\n", "x" * 500 + "\ntail\n", "sample.txt", max_chars=100,
    )
    assert truncated
    assert additions == 2 and deletions == 1
    assert "+tail" not in patch


@pytest.mark.parametrize("old,edited", [
    (b"first\r\nold\r\n", b"first\r\nnew\r\n"),
    (b"first\nold\n", b"first\nnew\n"),
    (b"first\r\nold\nlast\r\n", b"first\r\nnew\r\nlast\r\n"),
    (b"first\rold\r", b"first\nnew\n"),
], ids=["crlf", "lf", "mixed", "cr"])
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "apply_patch"])
def test_review_and_turn_patches_reproduce_actual_file_tool_writes(tmp_path, old, edited, tool_name):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    target = tmp_path / "sample.txt"
    target.write_bytes(old)
    expected = b"first\nnew\n" if tool_name == "write_file" else edited
    tracker = TurnDiffTracker()
    events = []

    async def emit(event, data):
        events.append((event, data))

    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    context.emit_event = emit
    context.turn_diff_tracker = tracker
    context.metadata["_read_file_hashes"] = {canonical_file_path_key(target): content_hash(old.decode())}
    if tool_name == "write_file":
        tool = WriteFileTool()
        args = {"file_path": "sample.txt", "content": expected.decode(), "expected_hash": content_hash(old.decode())}
    elif tool_name == "edit_file":
        tool = EditFileTool()
        args = {"file_path": "sample.txt", "old_string": "old", "new_string": "new", "expected_hash": content_hash(old.decode())}
    else:
        tool = ApplyPatchTool()
        args = {"patch": "*** Begin Patch\n*** Update File: sample.txt\n@@\n-old\n+new\n*** End Patch"}
    review = generate_diff(tool_name, args, workspace_root=tmp_path, tool_ctx=context)
    entry = review["files"][0]
    result = asyncio.run(tool.execute(args, context))

    assert not result.is_error, result.content
    assert target.read_bytes() == expected
    assert entry["size_bytes"] == len(expected)
    assert events[-1][0] == "turn.diff.updated"
    if tool_name != "apply_patch":
        reported_hash = re.search(r"content_hash: ([a-f0-9]+)", result.content).group(1)
        assert reported_hash == content_hash(target.read_text(encoding="utf-8"))

    for patch in (entry["patch"], tracker.get_unified_diff()):
        # Approval stores an absolute display path; replay within this fixture.
        patch = patch.replace(f"a/{target}", "a/sample.txt").replace(f"b/{target}", "b/sample.txt")
        target.write_bytes(old)
        applied = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
            input=patch.encode("utf-8"), cwd=tmp_path, capture_output=True,
        )
        assert applied.returncode == 0, applied.stderr.decode(errors="replace")
        assert target.read_bytes() == expected


def test_crlf_write_result_hash_can_be_used_for_the_next_edit(tmp_path):
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    written = asyncio.run(WriteFileTool().execute({
        "file_path": "sample.txt", "content": "first\r\nold\r\n",
    }, context))
    expected_hash = re.search(r"content_hash: ([a-f0-9]+)", written.content).group(1)
    edited = asyncio.run(EditFileTool().execute({
        "file_path": "sample.txt", "old_string": "old", "new_string": "new", "expected_hash": expected_hash,
    }, context))

    assert not edited.is_error, edited.content
    assert (tmp_path / "sample.txt").read_bytes() == b"first\r\nnew\r\n"


@pytest.mark.parametrize("tool,args", [
    (WriteFileTool(), {"content": "after"}),
    (EditFileTool(), {"old_string": "before", "new_string": "after"}),
])
def test_baseline_read_failure_does_not_overwrite_the_file(tmp_path, monkeypatch, tool, args):
    target = tmp_path / "sample.txt"
    target.write_bytes(b"before")
    read_bytes = type(target).read_bytes

    def failed_baseline(path):
        if path == target:
            raise OSError("AUDIT_BASELINE_READ_FAILED")
        return read_bytes(path)

    monkeypatch.setattr(type(target), "read_bytes", failed_baseline)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    result = asyncio.run(tool.execute({
        "file_path": "sample.txt", "expected_hash": content_hash("before"), **args,
    }, context))

    assert result.is_error
    assert "AUDIT_BASELINE_READ_FAILED" in result.content
    assert read_bytes(target) == b"before"
