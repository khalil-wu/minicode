"""Git diff integration — structured diff from working tree and staging area."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path


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
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=workspace_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


async def get_working_tree_diff(workspace_root: str) -> StructuredDiff:
    raw = await _run_git(workspace_root, "diff", "--no-color")
    return _parse_diff_output(raw)


async def get_staged_diff(workspace_root: str) -> StructuredDiff:
    raw = await _run_git(workspace_root, "diff", "--cached", "--no-color")
    return _parse_diff_output(raw)


async def get_untracked_files(workspace_root: str) -> list[str]:
    raw = await _run_git(workspace_root, "ls-files", "--others", "--exclude-standard")
    return [line for line in raw.strip().split("\n") if line]


async def stage_file(workspace_root: str, path: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git", "add", "--", path,
        cwd=workspace_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def unstage_file(workspace_root: str, path: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git", "reset", "HEAD", "--", path,
        cwd=workspace_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0
