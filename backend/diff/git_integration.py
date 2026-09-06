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


_DIFF_HEADER_RE = re.compile(r"^diff --(?:git|cc|combined) .+$", re.MULTILINE)
_BINARY_RE = re.compile(r"^Binary files", re.MULTILINE)


def _parse_diff_output(output: str) -> StructuredDiff:
    if not output:
        return StructuredDiff()

    # --raw -z and --patch describe the same Git snapshot. Take filenames
    # from the NUL-delimited records, not quoted, human-readable diff headers.
    metadata, _, raw = output.partition("\0\0")
    records = iter(metadata.rstrip("\0").split("\0"))
    changes: dict[str, str] = {}
    unmerged: set[str] = set()
    for record in records:
        status = record.split()[-1][0]
        path = next(records)
        if status in {"R", "C"}:
            path = next(records)
        if status == "U":
            unmerged.add(path)
        changes[path] = status

    # An unmerged record has no patch of its own. --ours may follow it with
    # a normal modification record; type changes have two patches (old/new).
    patch_text = raw
    patches = {path: f"* Unmerged path {path}\n" for path in unmerged}
    for marker in patches.values():
        patch_text = patch_text.replace(marker, "")
    patch_paths = [
        path
        for path, status in changes.items()
        for _ in range(2 if status == "T" else int(status != "U"))
    ]
    headers = list(_DIFF_HEADER_RE.finditer(patch_text))
    for i, (path, match) in enumerate(zip(patch_paths, headers, strict=True)):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(patch_text)
        patches[path] = patches.get(path, "") + patch_text[match.start():end]

    files: list[FileDiff] = []
    for path in changes:
        chunk = patches[path]
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
        "git", "--literal-pathspecs", *args,
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
    # Combined conflict diffs suppress patches when --raw is present. Compare
    # unresolved files with the index's stage 2 to retain their working content.
    raw = await _run_git(
        workspace_root, "diff", "--ours", "--raw", "-z", "--patch", "--no-color",
        "--submodule=short", *_GIT_DIFF_SAFETY_FLAGS
    )
    return _parse_diff_output(raw)


async def get_staged_diff(workspace_root: str) -> StructuredDiff:
    raw = await _run_git(
        workspace_root, "diff", "--cached", "--raw", "-z", "--patch", "--no-color",
        "--submodule=short", *_GIT_DIFF_SAFETY_FLAGS
    )
    return _parse_diff_output(raw)


async def get_untracked_files(workspace_root: str) -> list[str]:
    raw = await _run_git(workspace_root, "ls-files", "--others", "--exclude-standard", "-z")
    return [path for path in raw.split("\0") if path]


async def stage_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "add", "--", path)


async def unstage_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "reset", "--", path)


async def stage_all(workspace_root: str) -> bool:
    return await _run_git_ok(workspace_root, "add", "--all", "--", ".")


async def unstage_all(workspace_root: str) -> bool:
    return await _run_git_ok(workspace_root, "reset", "--", ".")


async def revert_file(workspace_root: str, path: str) -> bool:
    return await _run_git_ok(workspace_root, "restore", "--worktree", "--", path)
