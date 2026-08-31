"""Tests for the MiniCode apply_patch tool and parser."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.apply_patch import ApplyPatchTool, build_apply_patch_diff_payload
from backend.tools.apply_patch_parser import (
    ApplyPatchError,
    ChangeKind,
    apply_update_hunks,
    parse_patch,
)
from backend.tools.file_tools_common import content_hash


# --- parser ---------------------------------------------------------------


def test_parse_add_update_delete_in_one_envelope():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n"
        "+print('hello')\n"
        "+print('world')\n"
        "*** Update File: existing.py\n"
        "@@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** Delete File: gone.py\n"
        "*** End Patch"
    )
    changes = parse_patch(patch)
    assert [(c.kind, c.path) for c in changes] == [
        (ChangeKind.ADD, "new.py"),
        (ChangeKind.UPDATE, "existing.py"),
        (ChangeKind.DELETE, "gone.py"),
    ]
    assert changes[0].new_content == "print('hello')\nprint('world')\n"


def test_parse_rename_via_move_to():
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "*** Move to: b.py\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch"
    )
    assert changes[0].kind == ChangeKind.UPDATE
    assert changes[0].move_to == "b.py"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a patch",
        "*** Begin Patch\ngarbage line\n*** End Patch",
        "*** Begin Patch\n*** Add File: x\n missing plus prefix\n*** End Patch",
        "*** Update File: x\n@@\n-a\n+b\n*** End Patch",  # no Begin
    ],
)
def test_parse_rejects_malformed(bad):
    with pytest.raises(ApplyPatchError):
        parse_patch(bad)


def test_apply_update_hunks_locates_and_replaces():
    original = "line 0\ndef foo():\n    return 1\nline 3\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 99\n"
        "*** End Patch"
    )
    result = apply_update_hunks(original, changes[0].hunks, "f.py")
    assert "return 99" in result
    assert "return 1" not in result
    assert result.startswith("line 0\n") and result.endswith("line 3\n")


def test_apply_update_hunks_missing_context_raises():
    with pytest.raises(ApplyPatchError):
        changes = parse_patch(
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@\n"
            "-nonexistent line\n"
            "+x\n"
            "*** End Patch"
        )
        apply_update_hunks("totally different content\n", changes[0].hunks, "f.py")


# --- tool execute ---------------------------------------------------------

def test_apply_update_hunks_ignores_trailing_whitespace():
    original = "def foo():\n    return 1 \n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch"
    )
    result = apply_update_hunks(original, changes[0].hunks, "f.py")
    assert result == "def foo():\n    return 2\n"


def test_apply_update_hunks_supports_change_context_anchor():
    original = "class First:\n    value = 1\n\nclass Target:\n    value = 2\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@ class Target:\n"
        "-    value = 2\n"
        "+    value = 3\n"
        "*** End Patch"
    )
    result = apply_update_hunks(original, changes[0].hunks, "f.py")
    assert "class Target:\n    value = 3" in result
    assert "class First:\n    value = 1" in result


def test_apply_update_hunks_normalizes_unicode_context_punctuation():
    original = "import asyncio  # local import – avoids top‑level dep\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        "-import asyncio  # local import - avoids top-level dep\n"
        "+import asyncio  # local import - kept intentionally\n"
        "*** End Patch"
    )
    result = apply_update_hunks(original, changes[0].hunks, "f.py")
    assert result == "import asyncio  # local import - kept intentionally\n"


def test_apply_update_hunks_failure_includes_current_excerpt():
    original = "import copy\nimport datetime\nimport re\nimport warnings\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        " import copy\n"
        "-import datetime\n"
        "-import warnings\n"
        "+import warnings\n"
        "*** End Patch"
    )
    with pytest.raises(ApplyPatchError, match="Closest current file excerpt") as exc_info:
        apply_update_hunks(original, changes[0].hunks, "f.py")
    assert "import re" in str(exc_info.value)


def test_apply_update_hunks_rejects_contextless_non_eof_insert():
    # #8: bare insert without context is ambiguous and must not land at file top.
    original = "line 0\nline 1\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        "+inserted\n"
        "*** End Patch"
    )
    with pytest.raises(ApplyPatchError, match="no context lines"):
        apply_update_hunks(original, changes[0].hunks, "f.py")


def test_apply_update_hunks_eof_append_without_context():
    # #8: explicit *** End of File allows context-less append.
    original = "line 0\nline 1\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        "+appended\n"
        "*** End of File\n"
        "*** End Patch"
    )
    result = apply_update_hunks(original, changes[0].hunks, "f.py")
    # original ends with a trailing newline, so split/join preserves the empty
    # final segment and the append lands after it (MiniCode-compatible).
    assert result == "line 0\nline 1\n\nappended"


def test_apply_update_hunks_rejects_eof_mixed_with_context():
    original = "line 0\nline 1\n"
    changes = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: f.py\n"
        "@@\n"
        " line 1\n"
        "+appended\n"
        "*** End of File\n"
        "*** End Patch"
    )
    with pytest.raises(ApplyPatchError, match="mixes context/removal lines"):
        apply_update_hunks(original, changes[0].hunks, "f.py")


# --- tool execute ---------------------------------------------------------


def _bypass_ctx(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=workspace,
    )


def test_apply_patch_tool_full_lifecycle():
    tool = ApplyPatchTool()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "existing.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        (d / "gone.py").write_text("delete me\n", encoding="utf-8")
        (d / "old.py").write_text("line a\nline b\n", encoding="utf-8")

        patch = (
            "*** Begin Patch\n"
            "*** Add File: sub/new.py\n"
            "+print('hi')\n"
            "*** Update File: existing.py\n"
            "@@\n"
            " def foo():\n"
            "-    return 1\n"
            "+    return 42\n"
            "*** Update File: old.py\n"
            "*** Move to: renamed.py\n"
            "@@\n"
            " line a\n"
            "-line b\n"
            "+line B\n"
            "*** Delete File: gone.py\n"
            "*** End Patch"
        )
        context = _bypass_ctx(d)
        context.metadata["_read_file_hashes"] = {
            str((d / "existing.py").resolve()): content_hash("def foo():\n    return 1\n"),
            str((d / "gone.py").resolve()): content_hash("delete me\n"),
            str((d / "old.py").resolve()): content_hash("line a\nline b\n"),
        }
        res = asyncio.run(tool.execute({"patch": patch}, context))

        assert not res.is_error, res.content
        assert (d / "sub" / "new.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert "return 42" in (d / "existing.py").read_text(encoding="utf-8")
        assert not (d / "gone.py").exists()
        assert not (d / "old.py").exists()
        assert (d / "renamed.py").read_text(encoding="utf-8") == "line a\nline B\n"


def test_apply_patch_tool_rejects_bad_context_without_partial_write():
    tool = ApplyPatchTool()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "a.py").write_text("real content\n", encoding="utf-8")
        # Second change has a bad context; the whole patch must fail and the
        # first (valid) Add must NOT be written — validation happens before any
        # write.
        patch = (
            "*** Begin Patch\n"
            "*** Add File: created.py\n"
            "+should not exist\n"
            "*** Update File: a.py\n"
            "@@\n"
            "-line that is not there\n"
            "+x\n"
            "*** End Patch"
        )
        res = asyncio.run(tool.execute({"patch": patch}, _bypass_ctx(d)))
        assert res.is_error
        assert not (d / "created.py").exists(), "partial write leaked despite a later failure"


def test_apply_patch_tool_add_existing_file_errors():
    tool = ApplyPatchTool()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "dup.py").write_text("already here\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Add File: dup.py\n"
            "+new\n"
            "*** End Patch"
        )
        res = asyncio.run(tool.execute({"patch": patch}, _bypass_ctx(d)))
        assert res.is_error
        assert "already exists" in res.content


def test_apply_patch_tool_rejects_overlapping_targets_before_writing(tmp_path):
    patch = (
        "*** Begin Patch\n"
        "*** Add File: same.txt\n"
        "+first\n"
        "*** Add File: same.txt\n"
        "+second\n"
        "*** End Patch"
    )

    result = asyncio.run(ApplyPatchTool().execute({"patch": patch}, _bypass_ctx(tmp_path)))

    assert result.is_error
    assert "more than once" in result.content
    assert not (tmp_path / "same.txt").exists()


def test_apply_patch_tool_rejects_binary_delete_that_cannot_be_rewound(tmp_path):
    target = tmp_path / "binary.dat"
    target.write_bytes(b"\xff\x00abc")
    patch = "*** Begin Patch\n*** Delete File: binary.dat\n*** End Patch"

    result = asyncio.run(ApplyPatchTool().execute({"patch": patch}, _bypass_ctx(tmp_path)))

    assert result.is_error
    assert "binary or non-UTF-8" in result.content
    assert target.read_bytes() == b"\xff\x00abc"


def test_apply_patch_pure_rename_has_structured_review_diff(tmp_path):
    (tmp_path / "before.txt").write_text("same\n", encoding="utf-8")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: before.txt\n"
        "*** Move to: after.txt\n"
        "@@\n"
        " same\n"
        "*** End Patch"
    )

    payload = build_apply_patch_diff_payload(patch, _bypass_ctx(tmp_path))

    assert payload is not None
    assert payload["stats"] == {"files_count": 1, "additions": 0, "deletions": 0}
    assert payload["files"][0]["status"] == "renamed"
    assert payload["files"][0]["old_path"] == "before.txt"


def test_apply_patch_validate_input_requires_patch():
    tool = ApplyPatchTool()
    assert tool.validate_input({}) != ""
    assert tool.validate_input({"patch": ""}) != ""
    assert tool.validate_input({"patch": "*** Begin Patch\n*** End Patch"}) == ""
