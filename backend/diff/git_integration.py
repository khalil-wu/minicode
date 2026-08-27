"""Git diff integration — structured diff from working tree and staging area."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from backend.runtime_env import sanitized_git_env
from backend.subprocesses import communicate, spawn_exec


class GitCommandError(RuntimeError):
    """A git command did not produce a trustworthy result."""

    def __init__(self, args: tuple[str, ...], exit_code: int | None, stderr: str):
        self.args_list = args
        self.exit_code = exit_code
        self.stderr = stderr.strip()
        command = "git " + " ".join(args)
        detail = self.stderr or "git exited without diagnostic output"
        super().__init__(f"{command} failed (exit={exit_code}): {detail}")


@dataclass
class FileDiff:
    path: str
    patch: str
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False


@dataclass
class StructuredDiff:
    files: list[FileDiff] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    raw: str = ""


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_BINARY_RE = re.compile(r"^Binary files", re.MULTILINE)


def _parse_diff_output(raw: str) -> StructuredDiff:
    if not raw.strip():
        return StructuredDiff(raw=raw)

    files: list[FileDiff] = []
    headers = list(_DIFF_HEADER_RE.finditer(raw))

    for i, match in enumerate(headers):
        start = match.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        chunk = raw[start:end]
        path = match.group(2)

        is_binary = bool(_BINARY_RE.search(chunk))
        additions = 0
        deletions = 0

        if not is_binary:
            for line in chunk.split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deletions += 1

        files.append(FileDiff(
            path=path,
            patch=chunk,
            additions=additions,
            deletions=deletions,
            is_binary=is_binary,
        ))

    total_add = sum(f.additions for f in files)
    total_del = sum(f.deletions for f in files)
    return StructuredDiff(files=files, total_additions=total_add, total_deletions=total_del, raw=raw)


async def _run_git(workspace_root: str, *args: str) -> str:
    proc = await spawn_exec(
        "git", *args,
        cwd=workspace_root,
        env=sanitized_git_env(workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await communicate(proc, timeout=15)
    if proc.returncode != 0:
        raise GitCommandError(
            args,
            proc.returncode,
            stderr.decode("utf-8", errors="replace"),
        )
    return stdout.decode("utf-8", errors="replace")


async def _run_git_ok(workspace_root: str, *args: str) -> bool:
    proc = await spawn_exec(
        "git", *args,
        cwd=workspace_root,
        env=sanitized_git_env(workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await communicate(proc, timeout=15)
    if proc.returncode != 0:
        raise GitCommandError(
            args,
            proc.returncode,
            stderr.decode("utf-8", errors="replace"),
        )
    return True


# MiniCode gitDiff.ts / get_git_diff.rs disable external diff drivers and
# textconv filters: a repo-configured diff driver is arbitrary code execution
# triggered by reading a diff.
_GIT_DIFF_SAFETY_FLAGS = ("--no-textconv", "--no-ext-diff")


async def get_working_tree_diff(workspace_root: str) -> StructuredDiff:
    raw = await _run_git(
        workspace_root, "diff", "--no-color", *_GIT_DIFF_SAFETY_FLAGS
    )
    return _parse_diff_output(raw)


async def get_staged_diff(workspace_root: str) -> StructuredDiff:
    raw = await _run_git(
        workspace_root, "diff", "--cached", "--no-color", *_GIT_DIFF_SAFETY_FLAGS
    )
    return _parse_diff_output(raw)


async def get_untracked_files(workspace_root: str) -> list[str]:
    raw = await _run_git(workspace_root, "ls-files", "--others", "--exclude-standard")
    return [line for line in raw.strip().split("\n") if line]


async def stage_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "add", "--", path)


async def unstage_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "reset", "HEAD", "--", path)


async def stage_all(workspace_root: str) -> bool:
    return await _run_git_ok(workspace_root, "add", "--all", "--", ".")


async def unstage_all(workspace_root: str) -> bool:
    return await _run_git_ok(workspace_root, "reset", "HEAD", "--", ".")


async def revert_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "restore", "--worktree", "--", path)
