from __future__ import annotations

from backend.sandbox.runner import _append_bwrap_mount_target_dir_args


def test_mount_target_dirs_recreated_below_masking_anchor(tmp_path):
    anchor = tmp_path / "denied"
    target = anchor / "a" / "b" / "writable"
    target.mkdir(parents=True)  # the host bind source exists
    args: list[str] = []
    _append_bwrap_mount_target_dir_args(args, target, anchor)
    assert args == [
        "--dir", str(anchor / "a"),
        "--dir", str(anchor / "a" / "b"),
        "--dir", str(target),
    ]


def test_file_target_recreates_parent_not_file(tmp_path):
    anchor = tmp_path / "denied"
    target = anchor / "sub" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    args: list[str] = []
    _append_bwrap_mount_target_dir_args(args, target, anchor)
    assert args == [
        "--dir", str(anchor / "sub"),
    ]


def test_no_dirs_when_target_is_direct_child(tmp_path):
    anchor = tmp_path / "denied"
    target = anchor / "writable"
    target.mkdir(parents=True)
    args: list[str] = []
    _append_bwrap_mount_target_dir_args(args, target, anchor)
    assert args == ["--dir", str(target)]
