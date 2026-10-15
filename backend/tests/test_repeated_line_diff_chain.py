from __future__ import annotations

import asyncio
import difflib
import subprocess

import pytest

from backend.diff.unified import count_unified_diff_changes, iter_unified_diff, split_diff_lines
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.review import generate_edit_diff_payload
from backend.tools.edit_file import EditFileTool
from backend.tools.file_tools_common import _generate_limited_unified_diff


def _diff(old: str, new: str, *, context: int = 3) -> str:
    return "".join(iter_unified_diff(old, new, fromfile="a/folder with spaces/样本.txt",
                                     tofile="b/folder with spaces/样本.txt", context_lines=context))


@pytest.mark.parametrize("changed_index", [0, 150, 300])
@pytest.mark.parametrize("context", [0, 1, 3])
@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_single_change_in_repeated_file_has_one_real_change_and_original_coordinates(changed_index, context, ending):
    rows = ["REPEATED\n"] * 301
    if ending != "\n":
        rows = [row.replace("\n", "\r\n") for row in rows] if ending == "\r\n" else rows[:-1] + ["REPEATED"]
    old = "".join(rows)
    updated = rows.copy()
    updated[changed_index] = rows[changed_index].replace("REPEATED", "changed")
    patch = _diff(old, "".join(updated), context=context)
    start = max(0, changed_index - context)
    count = min(301, changed_index + context + 1) - start
    coordinates = str(start + 1) + (f",{count}" if count != 1 else "")
    assert f"@@ -{coordinates} +{coordinates} @@\n" in patch
    assert count_unified_diff_changes(patch) == (1, 1)
    assert patch.startswith("--- a/folder with spaces/样本.txt\n+++ b/folder with spaces/样本.txt\n")
    assert len(patch.split("\n")) < 17
    if ending == "" and changed_index == 300:
        assert patch.count("\\ No newline at end of file\n") == 2


@pytest.mark.parametrize("operation", ["insert", "delete"])
def test_zero_context_insert_and_delete_keep_original_empty_range_coordinates(operation):
    rows = ["repeated\n"] * 301
    if operation == "insert":
        old = "".join(rows)
        new = "".join(rows[:150] + ["inserted\n"] + rows[150:])
        header, counts = "@@ -150,0 +151 @@\n", (1, 0)
    else:
        old = "".join(rows[:150] + ["removed\n"] + rows[150:])
        new = "".join(rows)
        header, counts = "@@ -151 +150,0 @@\n", (0, 1)
    patch = _diff(old, new, context=0)
    assert header in patch
    assert count_unified_diff_changes(patch) == counts


@pytest.mark.parametrize("old,new", [
    ("", "new\n"), ("old\n", ""), ("same", "same\n"), ("same\n", "same"),
    ("prefix\nold\nsuffix\n", "prefix\nnew\nsuffix\n"),
    ("prefix\none\nmiddle\ntwo\nsuffix\n", "prefix\nONE\nmiddle\nTWO\nsuffix\n"),
])
def test_context_trimming_keeps_standard_unified_format_and_no_final_newline_evidence(old, new):
    expected = []
    for line in difflib.unified_diff(split_diff_lines(old), split_diff_lines(new),
                                     fromfile="a/folder with spaces/样本.txt", tofile="b/folder with spaces/样本.txt"):
        expected.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    assert _diff(old, new) == "".join(expected)


@pytest.mark.parametrize("changed_index", [0, 150, 300])
def test_repeated_line_patch_applies_and_reverses_exact_bytes_with_real_git(tmp_path, changed_index):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    target = tmp_path / "folder with spaces" / "样本.txt"
    target.parent.mkdir()
    old = b"repeated\n" * 300 + b"repeated"
    rows = old.decode().split("\n")
    rows[changed_index] = "changed"
    new = "\n".join(rows).encode()
    target.write_bytes(old)
    patch = _diff(old.decode(), new.decode()).encode()
    applied = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
                             cwd=tmp_path, input=patch, capture_output=True)
    assert applied.returncode == 0, applied.stderr
    assert target.read_bytes() == new
    reversed_patch = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--reverse", "--whitespace=nowarn", "-"],
                                   cwd=tmp_path, input=patch, capture_output=True)
    assert reversed_patch.returncode == 0, reversed_patch.stderr
    assert target.read_bytes() == old


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_approval_actual_edit_tool_and_turn_aggregate_share_minimal_repeated_line_diff(tmp_path, line_ending):
    target = tmp_path / "fact-parent.txt"
    original = "PROJECTION_FILE_PARENT=actual file bytes" + line_ending + ("REAL_FILE_PAYLOAD" + line_ending) * 300
    expected = original.replace("actual file bytes", "edited file bytes")
    target.write_bytes(original.encode())
    approval = generate_edit_diff_payload(str(target), "actual file bytes", "edited file bytes")
    assert approval["stats"] == {"files_count": 1, "additions": 1, "deletions": 1}
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
                                   emit_event=emit, tool_call_id="repeated-edit", conversation_id="repeated-owner")
    context.metadata["run_id"] = "repeated-turn"
    result = asyncio.run(EditFileTool().execute({"file_path": target.name, "old_string": "actual file bytes",
                                                "new_string": "edited file bytes"}, context))
    assert not result.is_error, result.content
    assert "Diff stats: +1 -1." in result.content
    assert target.read_bytes() == expected.encode()
    aggregate = events[-1][1]
    assert events[-1][0] == "turn.diff.updated"
    assert aggregate["thread_id"] == "repeated-owner" and aggregate["turn_id"] == "repeated-turn"
    assert count_unified_diff_changes(aggregate["diff"]) == (1, 1)
    assert count_unified_diff_changes(context.turn_diff_tracker.get_unified_diff()) == (1, 1)
    patch, additions, deletions, truncated = _generate_limited_unified_diff(original, expected, target.name, max_chars=512)
    assert (additions, deletions, truncated) == (1, 1, False)
    assert "-PROJECTION_FILE_PARENT=actual file bytes" in patch
    assert "+PROJECTION_FILE_PARENT=edited file bytes" in patch
