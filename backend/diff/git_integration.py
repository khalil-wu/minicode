"""Git diff integration — structured diff from working tree and staging area."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from dataclasses import dataclass, field
from uuid import uuid4

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
            in_hunk = False
            for line in chunk.split("\n"):
                # A type change joins a deletion patch and an addition patch.
                # The next file header ends the previous hunk, so its ---/+++
                # metadata must not be counted as changed content.
                if _DIFF_HEADER_RE.fullmatch(line):
                    in_hunk = False
                elif line.startswith("@@"):
                    in_hunk = True
                elif in_hunk and line.startswith("+"):
                    additions += 1
                elif in_hunk and line.startswith("-"):
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
        "git", "--no-optional-locks", "-c", "safe.bareRepository=explicit",
        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "--literal-pathspecs", *args,
        cwd=workspace_root,
        env=sanitized_git_env(workspace_root),
        stdin=asyncio.subprocess.DEVNULL,
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


async def _run_git_ok(workspace_root: str, *args: str, input_data: bytes | None = None) -> bool:
    proc = await spawn_exec(
        "git", "--literal-pathspecs", *args,
        cwd=workspace_root,
        env=sanitized_git_env(workspace_root),
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await communicate(proc, input_data=input_data, timeout=15)
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


async def _read_git(workspace_root: str, *args: str, service=None) -> str:
    if service is None:
        return await _run_git(workspace_root, *args)
    result = await _run_ui_git(service, args)
    if result.returncode != 0:
        raise GitCommandError(args, result.returncode, result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="replace")


async def _run_ui_git(service, args):
    """Source UI Git uses the captured host's standalone process capability.

    Desktop worker $ -> H1 -> main vL -> process/spawn does not inherit the
    model's permission profile. This fixed UI path never retries under another
    scope, host, or environment; model Git tools keep executor_git.py unchanged.
    """
    from backend.sandbox.policy import PermissionProfile
    from backend.workspace.executor_git import executor_git_environment

    policy, overrides = executor_git_environment(service)
    env_policy = {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
        "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}
    host_scope = replace(service.scope, permission_profile=PermissionProfile.disabled())
    hooks = "NUL" if service.cwd.convention == "windows" else "/dev/null"
    return await service.run(["git", "--no-optional-locks", "-c", "safe.bareRepository=explicit",
        "-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false", "--literal-pathspecs", *args],
        timeout=15, env_policy=env_policy, env=overrides, permission_scope=host_scope)


async def _diff_filter_flags(workspace_root: str, *, service=None) -> list[str]:
    # Source tui/get_git_diff.rs disables configured clean/process drivers as
    # well as external diff/textconv: simply reading a diff must not run them.
    try:
        keys = await _read_git(workspace_root, "config", "--null", "--name-only", "--get-regexp",
            r"^filter\..*\.(clean|process)$", service=service)
    except GitCommandError as error:
        if error.exit_code != 1:
            raise
        keys = ""
    drivers = sorted({key.rsplit(".", 1)[0] for key in keys.split("\0") if key})
    return [value for driver in drivers for option in ("clean=", "process=", "required=false")
            for value in ("-c", f"{driver}.{option}")]


async def _write_git(workspace_root: str, *args: str, service=None) -> bool:
    if service is None:
        return await _run_git_ok(workspace_root, *args)
    result = await _run_ui_git(service, args)
    if result.returncode != 0:
        raise GitCommandError(args, result.returncode, result.stderr.decode("utf-8", errors="replace"))
    return True


def is_not_git_repository(exit_code: int | None, stderr: str) -> bool:
    return exit_code == 128 and stderr.startswith("fatal: not a git repository (or any")


async def is_git_worktree(workspace_root: str, *, service=None) -> bool:
    try:
        result = await _read_git(workspace_root, "rev-parse", "--is-inside-work-tree", service=service)
    except GitCommandError as exc:
        if is_not_git_repository(exc.exit_code, exc.stderr):
            return False
        raise
    return result.strip() == "true"


async def get_working_tree_diff(workspace_root: str, *, service=None) -> StructuredDiff:
    # Combined conflict diffs suppress patches when --raw is present. Compare
    # unresolved files with the index's stage 2 to retain their working content.
    flags = await _diff_filter_flags(workspace_root, service=service)
    raw = await _read_git(
        workspace_root, *flags, "diff", "--ours", "--relative", "--raw", "-z", "--patch", "--no-color",
        "--submodule=short", *_GIT_DIFF_SAFETY_FLAGS, "--", ".", service=service,
    )
    return _parse_diff_output(raw)


async def get_staged_diff(workspace_root: str, *, service=None) -> StructuredDiff:
    flags = await _diff_filter_flags(workspace_root, service=service)
    raw = await _read_git(
        workspace_root, *flags, "diff", "--cached", "--relative", "--raw", "-z", "--patch", "--no-color",
        "--submodule=short", *_GIT_DIFF_SAFETY_FLAGS, "--", ".", service=service,
    )
    return _parse_diff_output(raw)


async def get_untracked_files(workspace_root: str, *, service=None) -> list[str]:
    raw = await _read_git(workspace_root, "ls-files", "--others", "--exclude-standard", "-z", service=service)
    return [path for path in raw.split("\0") if path]


async def stage_file(workspace_root: str, path: str, *, service=None) -> bool:
    if service is not None:
        service.resolve_workspace_path(path)
    return await _write_git(workspace_root, "add", "--", path, service=service)


async def unstage_file(workspace_root: str, path: str, *, service=None) -> bool:
    if service is not None:
        service.resolve_workspace_path(path)
    return await _write_git(workspace_root, "reset", "--", path, service=service)


async def stage_all(workspace_root: str, *, service=None) -> bool:
    return await _write_git(workspace_root, "add", "--all", "--", ".", service=service)


async def unstage_all(workspace_root: str, *, service=None) -> bool:
    return await _write_git(workspace_root, "reset", "--", ".", service=service)


async def revert_file(workspace_root: str, path: str, *, service=None) -> bool:
    if service is not None:
        service.resolve_workspace_path(path)
    return await _write_git(workspace_root, "restore", "--worktree", "--", path,
        service=service)


async def revert_patch(workspace_root: str, patch: str, *, service=None) -> bool:
    """Undo the displayed edit while Git checks its context and preserves other edits."""
    try:
        prefix = (await _read_git(workspace_root, "rev-parse", "--show-prefix", service=service)).removesuffix("\n")
    except GitCommandError as error:
        if not is_not_git_repository(error.exit_code, error.stderr):
            raise
        prefix = ""
    directory = (f"--directory={prefix}",) if prefix else ()
    if service is None:
        return await _run_git_ok(
            workspace_root, "apply", "--reverse", "--whitespace=nowarn", *directory, "-",
            input_data=patch.encode("utf-8"),
        )
    # Source process/write has no close-stdin operation. Keep this transport
    # adaptation entirely on the captured host, with one workspace path.
    temporary_directory = service.cwd.joinpath(f".minicode-undo-{uuid4().hex}")
    temporary = temporary_directory.joinpath("displayed.patch")
    async def write_patch():
        await service.filesystem.create_directory(temporary_directory.uri,
            sandbox=None, follow_symlinks=False)
        await service.filesystem.write_file(temporary.uri, patch.encode("utf-8"),
            sandbox=None, follow_symlinks=False)
    write = asyncio.create_task(write_patch())
    error = None
    try:
        await asyncio.shield(write)
        return await _write_git(workspace_root, "apply", "--reverse", "--whitespace=nowarn", *directory, temporary.native,
            service=service)
    except BaseException as caught:
        error = caught
        raise
    finally:
        # A cancelled RPC may still be writing. Finish that original request
        # before deleting, and never look up a replacement environment.
        await asyncio.gather(write, return_exceptions=True)
        try:
            await service.filesystem.remove(temporary.uri, sandbox=None, force=True, follow_symlinks=False)
            await service.filesystem.remove(temporary_directory.uri, sandbox=None, force=True, follow_symlinks=False)
        except Exception as cleanup:
            if error is None:
                cleanup.cleanup_pending = True
                cleanup.cleanup_reason = f"Could not remove {temporary_directory.uri}: {cleanup}"
                raise
            error.cleanup_pending = True
            error.cleanup_reason = f"Could not remove {temporary_directory.uri}: {cleanup}"
