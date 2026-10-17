"""Generation-bound Changes versions on the original app-owned execution host."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
import stat
from uuid import uuid4

from backend.diff.git_integration import (FileDiff, GitCommandError, StructuredDiff,
    _diff_filter_flags, _read_git, _run_git_result, _run_ui_git)
from backend.media_types import media_type_for_path
from backend.environments.errors import ExecutorRpcError
from backend.workspace.executor_service import executor_registration_id, not_found


MAX_CONTENT_BYTES = 5 * 1024 * 1024  # Source worker uke: default and hard maximum.


class StaleGitSnapshot(RuntimeError):
    def __init__(self):
        super().__init__("The displayed Git snapshot changed; refresh Changes before loading its contents.")


@dataclass
class ReviewSnapshot:
    generation: str
    source: str
    hide_whitespace: bool
    execution: tuple = ()
    repo_state: str | None = None
    files: dict[str, FileDiff] = field(default_factory=dict)
    disk_versions: dict[str, str] = field(default_factory=dict)
    untracked_paths: set[str] = field(default_factory=set)
    comparison: dict = field(default_factory=dict)


def _snapshots(session) -> dict:
    if not hasattr(session, "_git_review_snapshots"):
        session._git_review_snapshots = {}
    return session._git_review_snapshots


def begin_snapshot(session, conversation_id: str, source: str, hide_whitespace: bool) -> ReviewSnapshot:
    snapshot = ReviewSnapshot(uuid4().hex, source, hide_whitespace)
    _snapshots(session)[(conversation_id, source)] = snapshot
    return snapshot


def current_snapshot(session, conversation_id, source, generation) -> ReviewSnapshot | None:
    snapshot = _snapshots(session).get((conversation_id, source))
    return snapshot if snapshot is not None and snapshot.generation == generation else None


def _execution(root, service) -> tuple:
    if service is None:
        return ("local", str(Path(root).resolve()))
    return (executor_registration_id(service.context), service.cwd.uri)


async def repository_state(root, service) -> str:
    head = await _read_git(root, "rev-parse", "--verify", "--quiet", "HEAD", service=service, allowed_exit_codes=(0, 1))
    index = await _read_git(root, "ls-files", "--stage", "-z", service=service)
    return hashlib.sha256((head + "\0" + index).encode("utf-8")).hexdigest()


async def _working_oids(root, paths, service, *, raw=False, flags=()) -> dict[str, str]:
    # Source BDe's argv batches are <=256 paths / 16k estimated bytes.
    # FDe can feed all paths through stdin; exec-server cannot close that stdin,
    # so these equivalent fixed hash-object argv batches stay on the same host.
    batches, batch, size = [], [], 0
    for path in paths:
        cost = 11 + len(path.encode("utf-8"))
        if batch and (len(batch) >= 256 or size + cost > 16_000):
            batches.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += cost
    if batch:
        batches.append(batch)
    values = {}
    for batch in batches:
        output = await _read_git(root, *flags, "hash-object", *(["--no-filters"] if raw else []),
            "--", *batch, service=service)
        values.update(zip(batch, output.splitlines(), strict=True))
    return values


async def prepare_snapshot(snapshot, root, service, result: StructuredDiff, untracked: list[FileDiff], *, repo_state):
    snapshot.execution = _execution(root, service)
    snapshot.repo_state = repo_state
    files = [*result.files, *untracked]
    worktree_source = snapshot.source in {"unstaged", "uncommitted", "branch"}
    flags = await _diff_filter_flags(root, service=service) if worktree_source and files else ()
    untracked_paths = {file.path for file in untracked}
    snapshot.untracked_paths = untracked_paths
    regular = [file.path for file in files if worktree_source and file.new_mode in {"100644", "100755"}]
    oids = await _working_oids(root, regular, service, flags=flags)
    snapshot.disk_versions = await _working_oids(root, regular, service, raw=True)
    for file in files:
        if worktree_source and file.new_mode in {"100644", "100755"}:
            oid = oids[file.path]
            if file.path not in untracked_paths and file.new_oid is not None and oid != file.new_oid:
                raise StaleGitSnapshot()
            file.new_oid = oid
        zeros = "0" * (len(file.old_oid or file.new_oid or "") or 40)
        file.revision = (f"untracked:{file.new_mode}:{file.new_oid or zeros}" if file.path in untracked_paths else
            f"{snapshot.source}:{file.status}:{file.old_mode}:{file.old_oid or zeros}:{file.new_mode}:{file.new_oid or zeros}")
        # The generation needs version identities, not a second retained copy
        # of every patch already delivered to the renderer.
        snapshot.files[file.path] = replace(file, patch="")
    if repo_state is not None and await repository_state(root, service) != repo_state:
        raise StaleGitSnapshot()


async def _result(root, args, service):
    # Immutable object reads do not cause a partial clone to fetch from a
    # provider. Source P3 uses GIT_NO_LAZY_FETCH for exactly this read.
    env = {"GIT_NO_LAZY_FETCH": "1", "LC_MESSAGES": "C", "LANGUAGE": "C"}
    result = await (_run_ui_git(service, args, env=env, output_limit=MAX_CONTENT_BYTES + 1024)
        if service is not None else _run_git_result(root, *args, env_overrides=env))
    result.args = args
    return result


async def _object_bytes(root, oid, service, limit):
    # Source exec-server has no stdin EOF. Independent argv queries avoid
    # inventing a batch/close protocol while retaining the same object identity.
    found = await _result(root, ["rev-parse", "--verify", "--quiet", f"{oid}^{{blob}}"], service)
    if found.returncode == 1:
        return None
    if found.returncode != 0:
        raise GitCommandError(tuple(found.args), found.returncode, found.stderr.decode("utf-8", errors="replace"))
    size = await _result(root, ["cat-file", "-s", oid], service)
    if size.returncode != 0:
        raise GitCommandError(tuple(size.args), size.returncode, size.stderr.decode("utf-8", errors="replace"))
    if int(size.stdout) > limit:
        raise ContentTooLarge(limit)
    blob = await _result(root, ["cat-file", "blob", oid], service)
    if blob.returncode != 0:
        raise GitCommandError(tuple(blob.args), blob.returncode, blob.stderr.decode("utf-8", errors="replace"))
    if len(blob.stdout) > limit:
        raise ContentTooLarge(limit)
    return blob.stdout


class ContentTooLarge(ValueError):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(f"Content exceeds {limit} bytes")


def _local_bytes(root, name, limit):
    path = Path(root) / name
    if path.stat().st_size > limit:
        raise ContentTooLarge(limit)
    with path.open("rb") as file:
        contents = file.read(limit + 1)
    if len(contents) > limit:
        raise ContentTooLarge(limit)
    return contents


async def _disk_bytes(root, name, service, limit):
    if service is None:
        return await asyncio.to_thread(_local_bytes, root, name, limit)
    path = service.resolve_workspace_path(name)
    info = await service.filesystem.get_metadata(path.uri, sandbox=None)
    if info["size"] > limit:
        raise ContentTooLarge(limit)
    contents = bytearray()
    async with await service.filesystem.open(path.uri, sandbox=None) as file:
        while True:
            chunk, eof = await file.read_block(len(contents), min(1024 * 1024, limit + 1 - len(contents)))
            contents.extend(chunk)
            if len(contents) > limit:
                raise ContentTooLarge(limit)
            if eof:
                return bytes(contents)


def _blob_oid(data, expected):
    digest = hashlib.sha256() if len(expected) == 64 else hashlib.sha1()
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _assert_snapshot_owner(session, conversation_id, snapshot, root, service):
    if current_snapshot(session, conversation_id, snapshot.source, snapshot.generation) is not snapshot:
        raise StaleGitSnapshot()
    if snapshot.execution != _execution(root, service):
        raise StaleGitSnapshot()


async def _assert_current(session, conversation_id, snapshot, root, service, paths):
    _assert_snapshot_owner(session, conversation_id, snapshot, root, service)
    if snapshot.repo_state is not None and await repository_state(root, service) != snapshot.repo_state:
        raise StaleGitSnapshot()
    disk_paths = list(dict.fromkeys(path for path in paths if path in snapshot.disk_versions))
    for path in disk_paths:
        expected = snapshot.disk_versions.get(path)
        if expected is not None:
            try:
                if service is None:
                    info = await asyncio.to_thread((Path(root) / path).lstat)
                    regular = stat.S_ISREG(info.st_mode)
                else:
                    info = await service.filesystem.get_metadata(service.resolve_workspace_path(path).uri,
                        sandbox=None, follow_symlinks=False)
                    regular = info["isFile"] and not info["isSymlink"]
                if not regular:
                    raise StaleGitSnapshot()
            except (OSError, ExecutorRpcError) as error:
                if not_found(error):
                    raise StaleGitSnapshot() from error
                raise
    actual = await _working_oids(root, disk_paths, service, raw=True)
    for path in disk_paths:
        if actual[path] != snapshot.disk_versions[path]:
            raise StaleGitSnapshot()


async def file_contents(session, conversation_id, snapshot, root, service, requests, *, max_object_bytes=MAX_CONTENT_BYTES):
    limit = max(0, min(int(max_object_bytes), MAX_CONTENT_BYTES))
    paths = [request["path"] for request in requests if request.get("format", "text") != "markdown-preview"]
    _assert_snapshot_owner(session, conversation_id, snapshot, root, service)
    if paths:
        await _assert_current(session, conversation_id, snapshot, root, service, paths)
    results = []
    for request in requests:
        path, side, format = request["path"], request["side"], request.get("format", "text")
        if side not in {"old", "new"} or format not in {"text", "binary", "markdown-preview"}:
            raise ValueError("Git contents side/format is invalid")
        if format == "markdown-preview" and (side != "new" or snapshot.source == "staged"):
            raise ValueError("Current-path Markdown preview is only available on the new side outside staged review")
        row = {"path": path, "side": side}
        file = snapshot.files.get(path)
        if file is None:
            results.append({**row, "type": "error", "error": {"type": "not-found"}})
            continue
        oid = None if format == "markdown-preview" else file.old_oid if side == "old" else file.new_oid
        absent = file.status == "A" and side == "old" or file.status == "D" and side == "new"
        kind = "absent" if absent else "object"
        try:
            if format == "markdown-preview":
                # Source je reads the live new path independently from the
                # immutable old/new Git versions used by code/media views.
                contents = await _disk_bytes(root, path, service, limit)
                kind = "disk"
            else:
                contents = b"" if absent else await _object_bytes(root, oid, service, limit) if oid else None
            # Old blobs never fall back to disk. A new worktree file is the
            # only disk version admitted, and must still match this snapshot.
            if contents is None and format != "markdown-preview" and side == "new" and path in snapshot.disk_versions:
                contents = await _disk_bytes(root, path, service, limit)
                if _blob_oid(contents, snapshot.disk_versions[path]) != snapshot.disk_versions[path]:
                    raise StaleGitSnapshot()
                kind = "disk"
            if contents is None:
                results.append({**row, "type": "error", "error": {"type": "not-found"}})
                continue
            success = {**row, "type": "success", "source_kind": kind}
            if oid is not None:
                success["oid"] = oid
            if format == "binary":
                success.update(data_base64=base64.b64encode(contents).decode("ascii"),
                    media_type=media_type_for_path(file.old_path or path if side == "old" else path))
            else:
                text = contents.decode("utf-8", errors="replace")
                # Source M3 splits only after LF and preserves CRLF/BOM/the
                # final unterminated line. An empty file is exactly [].
                lines = text.split("\n")
                success["lines"] = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
            results.append(success)
        except ContentTooLarge:
            results.append({**row, "type": "error", "error": {"type": "too-large", "limit_bytes": limit}})
        except (OSError, ExecutorRpcError) as error:
            if not_found(error):
                results.append({**row, "type": "error", "error": {"type": "not-found"}})
            else:
                raise
    _assert_snapshot_owner(session, conversation_id, snapshot, root, service)
    if paths:
        await _assert_current(session, conversation_id, snapshot, root, service, paths)
    return results


async def apply_action(session, conversation_id, snapshot, root, service, *, action, paths=None):
    """Re-read one displayed Source scope, verify its versions, then apply it."""
    from backend.diff.git_integration import (apply_review_patch, get_staged_diff, get_working_tree_diff,
        get_untracked_file_diffs)

    if snapshot.source not in {"unstaged", "staged"}:
        raise ValueError("Git actions are only available in the explicit staged or unstaged review source")
    if action == "stage" and snapshot.source != "unstaged" or action == "unstage" and snapshot.source != "staged":
        raise ValueError("Git action does not match its displayed review source")
    selected = list(snapshot.files) if paths is None else paths
    if not selected or any(path not in snapshot.files for path in selected):
        raise ValueError("The displayed Git review has no selected changes to apply")
    if any(snapshot.files[path].status == "U" for path in selected):
        raise ValueError("Git review actions are unavailable for unresolved merge conflicts")
    await _assert_current(session, conversation_id, snapshot, root, service, selected)
    read_diff = get_staged_diff if snapshot.source == "staged" else get_working_tree_diff
    # bOe/lOe requests only {binary:true}; the captured file set is retained,
    # but an action re-reads the complete patch without a whitespace filter.
    current = await read_diff(root, service=service, binary=True)
    untracked = [path for path in selected if path in snapshot.untracked_paths]
    additions = await get_untracked_file_diffs(root, untracked, service=service, binary=True) if untracked else []
    rows = {file.path: file for file in [*current.files, *additions]}
    regular = [path for path in selected if path in rows and rows[path].new_mode in {"100644", "100755"}]
    if snapshot.source == "unstaged" and regular:
        flags = await _diff_filter_flags(root, service=service)
        for path, oid in (await _working_oids(root, regular, service, flags=flags)).items():
            rows[path].new_oid = oid
    for path in selected:
        before, after = snapshot.files[path], rows.get(path)
        fields = ("status", "old_path", "old_mode", "new_mode", "old_oid", "new_oid")
        if after is None or any(getattr(before, name) != getattr(after, name) for name in fields):
            raise StaleGitSnapshot()
    await _assert_current(session, conversation_id, snapshot, root, service, selected)
    patch = "".join(rows[path].patch for path in selected)
    if not patch.strip():
        raise ValueError("The displayed Git review contains no applicable patch")

    # Source bOe/SOe removes an added embedded repository only after checking
    # its exact captured HEAD and that tracked, untracked and ignored files
    # are all clean. Ordinary worktree files are removed by Git's own patch.
    gitlinks = [snapshot.files[path] for path in selected if action == "revert" and path in snapshot.untracked_paths
        and snapshot.files[path].new_mode == "160000"]
    for file in gitlinks:
        if service is None:
            info = await asyncio.to_thread((Path(root) / file.path).lstat)
            directory = stat.S_ISDIR(info.st_mode)
        else:
            info = await service.filesystem.get_metadata(service.resolve_workspace_path(file.path).uri,
                sandbox=None, follow_symlinks=False)
            directory = info["isDirectory"] and not info["isSymlink"]
        head = await _read_git(root, "-C", file.path, "rev-parse", "HEAD", service=service)
        dirty = await _read_git(root, "-C", file.path, "status", "--porcelain=v1", "--untracked-files=all",
            "--ignored=matching", service=service)
        if not directory or head.strip() != file.new_oid or dirty.strip():
            raise ValueError("Embedded repository changed or contains local files; its displayed change cannot be reverted")
    try:
        result = await apply_review_patch(root, patch, action=action, source=snapshot.source, service=service)
        if result["status"] == "success":
            for file in gitlinks:
                try:
                    if service is None:
                        import shutil

                        target = Path(root).resolve() / file.path
                        target.resolve().relative_to(Path(root).resolve())
                        await asyncio.to_thread(shutil.rmtree, target)
                    else:
                        await service.filesystem.remove(service.resolve_workspace_path(file.path).uri, sandbox=None,
                            recursive=True, force=True, follow_symlinks=False)
                except Exception as error:
                    return {**result, "status": "partial-success", "ok": False, "message": str(error),
                        "skipped_paths": [file.path]}
        return result
    finally:
        # Any applied/partially-applied scope retires the displayed versions;
        # the next read must obtain a new real snapshot from the same owner.
        snapshots = _snapshots(session)
        for key in [key for key in snapshots if key[0] == conversation_id]:
            snapshots.pop(key)
