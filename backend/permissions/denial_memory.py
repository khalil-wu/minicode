"""Conversation-scoped memory of write targets the user declined.

When the user rejects a write_file, edit_file, notebook_edit, apply_patch or
shell write, the target paths are recorded on the live PermissionContext. A
later shell command that the execution policy would run without asking, but
that writes one of those paths, is asked again: the refusal applies to the
file, not to the tool that happened to ask first. An explicit approval of a
write to the same path lifts the refusal.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_WRITE_PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "write_file": ("file_path",),
    "edit_file": ("file_path",),
    "notebook_edit": ("notebook_path",),
}
_NON_FILE_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "NUL"})


def normalized_denied_path(raw: Any, base: Path | None) -> str:
    """Absolute, case-normalized form used as the memory key; empty when unresolvable."""
    text = str(raw or "").strip().strip("'\"")
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        if base is None:
            return ""
        path = Path(base) / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    return os.path.normcase(str(resolved))


def shell_write_targets(command: str) -> list[str]:
    """Paths a shell command writes through redirects, dd output or copy/move destinations."""
    from backend.permissions.checker import (
        _DD_OUTPUT_RE,
        _SHELL_WRITE_COMMAND_RE,
        _SHELL_WRITE_REDIRECT_RE,
        _split_shell_compound,
    )

    targets: list[str] = []
    for segment in _split_shell_compound(command):
        dd = _DD_OUTPUT_RE.search(segment)
        if dd:
            targets.append(dd.group(1))
        targets.extend(match.group(1) for match in _SHELL_WRITE_REDIRECT_RE.finditer(segment))
        command_match = _SHELL_WRITE_COMMAND_RE.search(segment)
        if command_match:
            operands: list[str] = []
            for token in command_match.group(1).split():
                # Operands end at the first redirection or pipe token.
                if token.startswith(("<", ">", "|")) or ">" in token:
                    break
                if not token.startswith("-"):
                    operands.append(token)
            if operands:
                # tee writes every operand; mv/cp/install/ln write their last one.
                writes_all = command_match.group(0).split()[0].lower() == "tee"
                targets.extend(operands if writes_all else operands[-1:])
    cleaned = [target.strip("'\"") for target in targets]
    return [target for target in cleaned if target and target not in _NON_FILE_TARGETS and not target.startswith("&")]


def denied_write_targets(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None,
    command_cwd: Path | None = None,
) -> list[str]:
    base = Path(command_cwd) if command_cwd is not None else (Path(workspace_root) if workspace_root else None)
    if tool_name in _WRITE_PATH_ARGUMENTS:
        return [
            normalized
            for key in _WRITE_PATH_ARGUMENTS[tool_name]
            if (normalized := normalized_denied_path(args.get(key), base))
        ]
    if tool_name == "apply_patch":
        from backend.tools.apply_patch_parser import patch_target_paths

        try:
            raw_paths = patch_target_paths(str(args.get("patch") or ""))
        except ValueError:
            return []
        return [normalized for item in raw_paths if (normalized := normalized_denied_path(item, base))]
    if tool_name == "run_command":
        return [
            normalized
            for item in shell_write_targets(str(args.get("command") or ""))
            if (normalized := normalized_denied_path(item, base))
        ]
    return []


def record_denied_write_targets(
    permission: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None,
    command_cwd: Path | None = None,
) -> list[str]:
    targets = denied_write_targets(tool_name, args, workspace_root=workspace_root, command_cwd=command_cwd)
    permission.denied_write_paths.update(targets)
    return targets


def discard_denied_write_targets(
    permission: Any,
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None,
    command_cwd: Path | None = None,
) -> list[str]:
    targets = denied_write_targets(tool_name, args, workspace_root=workspace_root, command_cwd=command_cwd)
    permission.denied_write_paths.difference_update(targets)
    return targets


def denied_shell_write_target(permission: Any, command: str, cwd: Path) -> str:
    """The first shell write target that the user declined earlier, as written in the command."""
    if not permission.denied_write_paths:
        return ""
    for raw in shell_write_targets(command):
        if normalized_denied_path(raw, cwd) in permission.denied_write_paths:
            return raw
    return ""
