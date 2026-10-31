"""Registered worktree lifecycle on the request's original executor."""
from __future__ import annotations

import asyncio
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
import json
import re
from uuid import uuid4

from fastapi import HTTPException

from backend.workspace.executor_commands import run_workbench_command, target_programs, workbench_policy
from backend.workspace.executor_git import executor_git_environment, executor_git_write_scope, run_executor_git, registered_worktree_metadata, executor_git_action_scope
from backend.workspace.executor_paths import ExecutorPath
from backend.terminal.executor_runner import ExecutorCommandRunner
from backend.environments.errors import ExecutorError, ExecutorProtocolError
from backend.workspace.worktree_snapshot_git import IN_PROGRESS_GIT_STATE, RAW_ATTRIBUTES, index_entries, object_alternate, recovery_mode


_REFS = "refs/minicode/wt-snapshots/"
_MESSAGE = "minicode executor worktree snapshot\n\n"
_ID = re.compile(r"wtsnap_[a-f0-9]{32}\Z")


def _display(service, path):
    return path.uri if service.context.turn_environment.is_remote else path.native


async def _canonical(service, path, *, write=False):
    path = service.resolve(path) if isinstance(path, str) else path
    access = service.scope.resolve_access(path.uri)
    if not access.can_read or write and not access.can_write:
        raise PermissionError(f"Executor {'write' if write else 'read'} permission denied: {path.uri}")
    actual = service.resolve(await service.filesystem.canonicalize(path.uri, sandbox=service.sandbox))
    access = service.scope.resolve_access(actual.uri)
    if not access.can_read or write and not access.can_write:
        raise PermissionError(f"Executor {'write' if write else 'read'} permission denied: {actual.uri}")
    return actual


async def _git(service, programs, *args, cwd=None, write=False, index=None, env=None, allowed=(0,), runners=None, write_paths=(), scope=None):
    policy, overrides = executor_git_environment(service, index_file=index)
    if scope is None:
        scope = await executor_git_write_scope(service, cwd=cwd, write_paths=write_paths) if write else executor_git_action_scope(service.scope)
    command_service = copy(service)
    command_service.cwd = cwd or service.cwd
    stdout = []

    def collect(text, stream):
        if stream == "stdout":
            stdout.append(text)

    result = await run_workbench_command(command_service,
        [programs["git"], *([] if write else ["--no-optional-locks"]), *args],
        env={**overrides, **(env or {})}, shell_policy=policy, permission_scope=scope,
        allow_failure=True, on_runner=runners.append if runners is not None else None, stream_callback=collect)
    if result.exit_code not in allowed:
        raise HTTPException(409, result.stderr.strip() or result.stdout.strip() or f"Git exited with {result.exit_code}")
    return "".join(stdout), result.exit_code, result.stderr


@dataclass
class _Inventory:
    current: ExecutorPath
    main: ExecutorPath
    common: ExecutorPath
    records: list[dict]

    def entry(self, target):
        return next((record for record in self.records if record["path"] == target), None)


async def _inventory(service, programs):
    output, _, _ = await _git(service, programs, "worktree", "list", "--porcelain", "-z")
    records, record = [], {}
    for value in output.split("\0"):
        if not value:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, content = value.partition(" ")
        record[key] = content
    if record:
        records.append(record)
    current = await _canonical(service, service.cwd)
    common_text, _, _ = await _git(service, programs, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = await _canonical(service, common_text.strip())
    for record in records:
        record["path"] = service.resolve(record["worktree"])
    main = await _canonical(service, records[0]["path"])
    return _Inventory(current, main, common, records)


async def _registered(service, programs, inventory, target, *, write=False):
    target = await _canonical(service, target, write=write)
    entry = inventory.entry(target)
    if entry is None:
        raise HTTPException(409, "Worktree not found in this repository")
    paths, _, _ = await _git(service, programs, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--git-dir", cwd=target)
    top, common, gitdir = paths.splitlines()
    if await _canonical(service, top) != target or await _canonical(service, common) != inventory.common:
        raise HTTPException(409, "Worktree repository identity changed")
    primary_git = inventory.main.joinpath(".git")
    primary_info = await service.filesystem.get_metadata(primary_git.uri, sandbox=service.sandbox, follow_symlinks=False)
    if (not primary_info["isDirectory"] or primary_info["isSymlink"]
            or await _canonical(service, primary_git) != inventory.common or inventory.common.parent != inventory.main):
        raise HTTPException(409, "The primary checkout does not own this worktree repository")
    if target != inventory.main:
        gitdir = await _canonical(service, gitdir)
        await registered_worktree_metadata(service, target, gitdir, inventory.common)
        entry = {**entry, "gitdir": gitdir}
    return target, entry


async def _require_finished_git_operation(service, entry):
    if any(item["fileName"] in IN_PROGRESS_GIT_STATE for item in await service.list_directory(entry["gitdir"])):
        raise HTTPException(409, "Worktrees with an in-progress Git operation cannot be removed or snapshotted")


async def _isolated(service, inventory, target):
    if not service.context.turn_environment.is_remote:
        from backend.services.worktree_settings_service import is_managed_worktree_path
        if not is_managed_worktree_path(Path(target.native), Path(inventory.main.native)):
            raise HTTPException(409, "Only MiniCode-managed worktrees can be removed")
        return target.parent
    root = await _canonical(service, inventory.main.joinpath(".minicode/worktrees"))
    if target == root or not target.is_relative_to(root):
        raise HTTPException(409, "Only isolated worktrees under .minicode/worktrees can be removed")
    return root


async def _action_parent(service, inventory, parent):
    """An explicit GUI worktree action may mutate its registered directory pool."""
    parent = await _canonical(service, parent)
    if service.context.turn_environment.is_remote:
        root = await _canonical(service, inventory.main.joinpath(".minicode/worktrees"))
        allowed = parent.is_relative_to(root)
    else:
        from backend.services.worktree_settings_service import is_managed_worktree_parent
        allowed = is_managed_worktree_parent(Path(parent.native), Path(inventory.main.native))
    if not allowed:
        raise HTTPException(409, "Worktree action parent is outside the registered isolated directory")
    scoped = copy(service)
    scoped.scope = executor_git_action_scope(service.scope, write_paths=(parent,), policy_cwd=parent)
    scoped.sandbox = scoped.scope.sandbox_context()
    return scoped


async def _complete_files(service, target, *, write=False):
    walked = await service.walk(target)
    if walked["errors"]:
        raise HTTPException(409, {"message": "Worktree inventory is incomplete", "errors": walked["errors"]})
    if walked["truncated"]:
        raise HTTPException(413, "Worktree exceeds the executor inventory boundary; no removal was performed")
    paths = [service.resolve(item["path"]) for item in walked["entries"]]
    for path in paths:
        access = service.scope.resolve_access(path.uri)
        if not access.can_read or write and not access.can_write:
            raise PermissionError(f"Complete worktree access is required: {path.uri}")
        if write and path.name == ".git" and path.parent != target:
            raise HTTPException(409, "Worktrees containing initialized submodules or embedded Git repositories cannot be removed")
    return walked["entries"]


async def _snapshot_records(service, programs, inventory, *, known=None):
    output, _, _ = await _git(service, programs, "for-each-ref", "--format=%(refname)%00%(objectname)", _REFS)
    records = []
    for line in output.splitlines():
        ref, sha = line.split("\0")
        if known is not None and ref == known["snapshot_ref"] and sha == known["snapshot_sha"]:
            records.append(known)
            continue
        body, _, _ = await _git(service, programs, "show", "-s", "--format=%B", ref)
        if not body.startswith(_MESSAGE):
            raise HTTPException(409, f"Snapshot metadata is unavailable on this executor: {ref}")
        record = json.loads(body[len(_MESSAGE):])
        if not _ID.fullmatch(record["id"]) or ref != _REFS + record["id"]:
            raise HTTPException(409, f"Snapshot identity does not match its repository ref: {ref}")
        if service.resolve(record["main_repo_path"]) != inventory.main or service.resolve(record["common_git_dir"]) != inventory.common:
            raise HTTPException(409, f"Snapshot belongs to a different repository: {ref}")
        original = service.resolve(record["original_path"])
        if service.context.turn_environment.is_remote:
            allowed = original != inventory.main.joinpath(".minicode/worktrees") and original.is_relative_to(inventory.main.joinpath(".minicode/worktrees"))
        else:
            from backend.services.worktree_settings_service import is_managed_worktree_path
            allowed = is_managed_worktree_path(Path(original.native), Path(inventory.main.native))
        if not allowed:
            raise HTTPException(409, f"Snapshot path is outside isolated worktrees: {ref}")
        records.append({**record, "snapshot_sha": sha, "snapshot_ref": ref})
    return sorted(records, key=lambda item: item["created_at"], reverse=True)


async def _snapshot(service, programs, inventory, target, entry, *, label="Before worktree removal"):
    identity = "wtsnap_" + uuid4().hex
    ref = _REFS + identity
    paths, code, _ = await _git(service, programs, "rev-parse", "--path-format=absolute", "--git-path", "index",
        "--shared-index-path", "--verify", "--quiet", "HEAD", cwd=target, allowed=(0, 1))
    index_paths = paths.splitlines()
    head = index_paths.pop() if code == 0 else None
    record = {"id": identity, "conversation_id": service.context.conversation_id,
        "branch": entry.get("branch", "").removeprefix("refs/heads/"), "original_path": target.uri,
        "main_repo_path": inventory.main.uri, "common_git_dir": inventory.common.uri, "head": head,
        "label": label, "created_at": datetime.now(UTC).isoformat()}
    index = inventory.common.joinpath("minicode-wt-index-" + uuid4().hex)
    runners = []
    index_scope = await executor_git_write_scope(service, cwd=target)
    index_sandbox = index_scope.sandbox_context()
    index_owner = ExecutorCommandRunner(await workbench_policy(service, timeout=120, permission_scope=index_scope))

    async def remove_index():
        for runner in runners:
            if not runner.closed:
                await runner.cleanup()
                if not runner.closed:
                    raise ExecutorProtocolError(f"Snapshot index cleanup awaits its original Git process: {index.uri}")
        for suffix in ("", ".lock", ".tree"):
            path = service.resolve(index.native + suffix)
            if await service.metadata(path) is not None:
                await service.filesystem.remove(path.uri, sandbox=index_sandbox)

    await index_owner.add_cleanup_callback(remove_index)
    try:
        original, _, _ = await _git(service, programs, "ls-files", "--stage", "-z", cwd=target, runners=runners)
        previous = {name: mode for mode, _sha, name in index_entries(original)}
        if "160000" in previous.values():
            raise HTTPException(409, "Worktrees containing submodules cannot be snapshotted")
        config, _, _ = await _git(service, programs, "config", "--bool", "--get-regexp", r"^core\.(filemode|symlinks)$", cwd=target, allowed=(0, 1), runners=runners)
        flags = dict(line.split(" ", 1) for line in config.splitlines())
        await _git(service, programs, "read-tree", "--empty", cwd=target, write=True, index=index.uri, runners=runners, scope=index_scope)
        await _git(service, programs, "add", "-N", "-A", "--force", cwd=target, write=True, index=index.uri, runners=runners, scope=index_scope)
        staged, _, _ = await _git(service, programs, "ls-files", "--stage", "-z", cwd=target, index=index.uri, runners=runners)
        entries = index_entries(staged)
        if any(mode == "160000" for mode, _sha, _name in entries):
            raise HTTPException(409, "Worktrees containing embedded Git repositories cannot be snapshotted")
        symbolic_links = [name for mode, _sha, name in entries if mode == "120000"]
        for start in range(0, len(symbolic_links), 64):
            await _git(service, programs, "--literal-pathspecs", "add", "--force", "--", *symbolic_links[start:start + 64],
                cwd=target, write=True, index=index.uri, runners=runners, scope=index_scope)
        regular = [(mode, name) for mode, _sha, name in entries if mode in {"100644", "100755"}]
        sources = [(None, name) for name in index_paths] + regular
        index_blobs = []
        for start in range(0, len(sources), 64):
            group = sources[start:start + 64]
            hashes, _, _ = await _git(service, programs, "hash-object", "-w", "--no-filters", "--",
                *(name for _mode, name in group), cwd=target, write=True, runners=runners, scope=index_scope)
            entries = []
            for (mode, name), sha in zip(group, hashes.splitlines(), strict=True):
                if mode is None:
                    filename = "index" if not index_blobs else name.replace("\\", "/").rsplit("/", 1)[-1]
                    index_blobs.append((filename, sha))
                else:
                    entries.append((recovery_mode(mode, previous.get(name), filemode=flags.get("core.filemode", "true") == "true",
                                                  symlinks=flags.get("core.symlinks", "true") == "true"), name, sha))
            if entries:
                await _git(service, programs, "update-index", "--add",
                    *(value for mode, name, sha in entries for value in ("--cacheinfo", f"{mode},{sha},{name}")),
                    cwd=target, write=True, index=index.uri, runners=runners, scope=index_scope)
        record.update(raw_worktree=True, symbolic_links=symbolic_links)
        tree, _, _ = await _git(service, programs, "write-tree", cwd=target, write=True, index=index.uri, runners=runners, scope=index_scope)
        index_tree = service.resolve(index.native + ".tree")
        tree_bytes = b"".join(b"100644 " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha) for name, sha in index_blobs)
        await service.filesystem.write_file(index_tree.uri, tree_bytes, sandbox=index_sandbox)
        original_tree, _, _ = await _git(service, programs, "hash-object", "-w", "-t", "tree", "--", index_tree.native,
            cwd=target, write=True, runners=runners, scope=index_scope)
        author = {"GIT_AUTHOR_NAME": "MiniCode", "GIT_AUTHOR_EMAIL": "snapshot@minicode.local",
                  "GIT_COMMITTER_NAME": "MiniCode", "GIT_COMMITTER_EMAIL": "snapshot@minicode.local"}
        index_sha, _, _ = await _git(service, programs, "commit-tree", original_tree.strip(), *(["-p", head] if head else []),
            "-m", "minicode worktree index: " + identity, cwd=target, write=True, env=author, runners=runners, scope=index_scope)
        record["index_sha"] = index_sha.strip()
        sha, _, _ = await _git(service, programs, "commit-tree", tree.strip(), *(["-p", head] if head else []),
            "-p", record["index_sha"], "-m", _MESSAGE + json.dumps(record, ensure_ascii=False),
            cwd=target, write=True, env=author, runners=runners, scope=index_scope)
        await _git(service, programs, "update-ref", ref, sha.strip(), "", cwd=target, write=True, runners=runners, scope=index_scope)
        return {**record, "snapshot_sha": sha.strip(), "snapshot_ref": ref}
    finally:
        try:
            await index_owner.cleanup()
        finally:
            if not index_owner.closed:
                await service.context.background_manager.retain_foreground_cleanup(runner=index_owner,
                    command="worktree snapshot index cleanup", cwd=target.uri,
                    conversation_id=service.context.conversation_id, task_id="", parent_run_id="",
                    reason="worktree_snapshot_index_cleanup_pending")


async def _prune(service, programs, inventory, keep_record):
    cutoff = datetime.now(UTC) - timedelta(days=30)
    for index, record in enumerate(await _snapshot_records(service, programs, inventory, known=keep_record)):
        if record["id"] != keep_record["id"] and (index >= 50 or datetime.fromisoformat(record["created_at"]) < cutoff):
            await _git(service, programs, "update-ref", "-d", record["snapshot_ref"], record["snapshot_sha"], write=True)


async def _snapshot_action(service, programs, path, label):
    inventory = await _inventory(service, programs)
    target, entry = await _registered(service, programs, inventory, service.resolve(path or service.cwd.uri))
    await _isolated(service, inventory, target)
    async with service.mutate([inventory.common, target]):
        await _require_finished_git_operation(service, entry)
        entries = await _complete_files(service, target)
        if any((candidate := service.resolve(item["path"])).name == ".git" and candidate.parent != target for item in entries):
            raise HTTPException(409, "Worktrees containing initialized submodules or embedded Git repositories cannot be snapshotted")
        record = await _snapshot(service, programs, inventory, target, entry, label=label)
        await _prune(service, programs, inventory, record)
        return record


async def _remove(service, programs, path, force, *, model_relative=False):
    inventory = await _inventory(service, programs)
    if model_relative:
        raw = str(path)
        pure = PureWindowsPath(raw) if service.cwd.convention == "windows" else PurePosixPath(raw)
        if not raw.startswith("file:") and not pure.is_absolute() and tuple(pure.parts[:2]) != (".minicode", "worktrees"):
            if service.context.turn_environment.is_remote:
                path = inventory.main.joinpath(".minicode/worktrees").joinpath(raw)
            else:
                from backend.services.worktree_settings_service import managed_worktree_root
                path = service.resolve(str(managed_worktree_root() / raw))
        else:
            path = service.resolve(raw)
    target, entry = await _registered(service, programs, inventory, path)
    if target == inventory.current:
        raise HTTPException(409, "Cannot remove current worktree")
    await _isolated(service, inventory, target)
    service = await _action_parent(service, inventory, target.parent)
    async with service.mutate([inventory.common, target]):
        await _require_finished_git_operation(service, entry)
        await _complete_files(service, target, write=True)
        dirty, _, _ = await _git(service, programs, "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all", cwd=target)
        result = {"removed": False, "path": _display(service, target), "branch": entry.get("branch", "").removeprefix("refs/heads/")}
        if dirty and not force:
            return {**result, "error": "Worktree has local changes; force is required"}
        if dirty:
            snapshot = await _snapshot(service, programs, inventory, target, entry)
            result.update(snapshot_id=snapshot["id"], snapshot_ref=snapshot["snapshot_ref"])
            await _prune(service, programs, inventory, snapshot)
        if not service.context.turn_environment.is_remote:
            from backend.workspace.local_environment_lifecycle import run_context_worktree_environment
            await run_context_worktree_environment(Path(inventory.main.native), Path(target.native), "cleanup", service.context, application_config=service.application_config)
        _, code, error = await _git(service, programs, "worktree", "remove", *(["--force"] if force else []),
            "--", target.native, write=True, write_paths=(target.parent,), allowed=(0, 1, 128))
        if code == 0 and not service.context.turn_environment.is_remote:
            from backend.services.worktree_settings_service import unregister_managed_worktree
            unregister_managed_worktree(Path(target.native))
        return {**result, "removed": code == 0, "error": error.strip() if code else None}


async def _restore(service, programs, snapshot_id, dest):
    if not _ID.fullmatch(snapshot_id):
        raise HTTPException(422, "Invalid executor worktree snapshot id")
    inventory = await _inventory(service, programs)
    record = next((item for item in await _snapshot_records(service, programs, inventory) if item["id"] == snapshot_id), None)
    if record is None:
        raise HTTPException(404, "Worktree snapshot was not found")
    original = service.resolve(record["original_path"])
    parent = await _canonical(service, original.parent)
    service = await _action_parent(service, inventory, parent)
    target = service.resolve(dest) if dest else original
    if target == original.parent or not target.is_relative_to(original.parent):
        raise HTTPException(409, "Restore destination must stay within the original worktree parent")
    if not dest and await service.metadata(target) is not None and await service.list_directory(target):
        target = original.parent.joinpath(original.name + "-restored")
        if await service.metadata(target) is not None and await service.list_directory(target):
            target = original.parent.joinpath(original.name + "-restored-" + snapshot_id[-6:])
    nearest = target
    while await service.metadata(nearest) is None:
        nearest = nearest.parent
    actual = await _canonical(service, nearest, write=True)
    target = actual.joinpath(target.relative_to(nearest).as_posix())
    if target == parent or not target.is_relative_to(parent):
        raise HTTPException(409, "Restore destination resolves outside the original worktree parent")
    async with service.mutate([inventory.common, target]):
        if await service.metadata(target) is not None and await service.list_directory(target):
            raise HTTPException(409, "Restore destination is not empty")
        _, code, error = await _git(service, programs, "worktree", "add", "--detach", "--no-checkout", "--", target.native,
            record["snapshot_sha"], write=True, write_paths=(parent,), allowed=(0, 1, 128))
        if code == 0:
            scratch = inventory.common.joinpath("minicode-restore-" + uuid4().hex[:12])
            restore_scope = await executor_git_write_scope(service, cwd=target, write_paths=(parent,))
            restore_sandbox = restore_scope.sandbox_context()
            restore_owner = ExecutorCommandRunner(await workbench_policy(service, timeout=120, permission_scope=restore_scope))
            runners = []

            async def remove_scratch():
                for runner in runners:
                    if not runner.closed:
                        await runner.cleanup()
                        if not runner.closed:
                            raise ExecutorProtocolError(f"Snapshot restore cleanup awaits its original Git process: {scratch.uri}")
                if await service.metadata(scratch) is not None:
                    await service.filesystem.remove(scratch.uri, sandbox=restore_sandbox, recursive=True)

            await restore_owner.add_cleanup_callback(remove_scratch)
            try:
                await _git(service, programs, "-c", "core.longpaths=true", "init", "--bare", "--template=", scratch.native,
                    cwd=target, write=True, scope=restore_scope, runners=runners)
                await service.filesystem.create_directory(scratch.joinpath("info").uri, sandbox=restore_sandbox)
                await service.filesystem.write_file(scratch.joinpath("info/attributes").uri, RAW_ATTRIBUTES, sandbox=restore_sandbox)
                await service.filesystem.write_file(scratch.joinpath("objects/info/alternates").uri,
                    object_alternate(inventory.common.joinpath("objects").native), sandbox=restore_sandbox)
                git_dir, work_tree = "--git-dir=" + scratch.native, "--work-tree=" + target.native
                command = ("-c", "core.symlinks=false", git_dir, work_tree)
                await _git(service, programs, *command, "read-tree", record["snapshot_sha"],
                    cwd=target, write=True, scope=restore_scope, runners=runners)
                await _git(service, programs, *command, "checkout-index", "--all", "--force",
                    cwd=target, write=True, scope=restore_scope, runners=runners)
                links = record.get("symbolic_links")
                if links is None:
                    tree, _, _ = await _git(service, programs, "ls-tree", "-r", "-z", record["snapshot_sha"])
                    links = [item.split("\t", 1)[1] for item in tree.split("\0") if item.startswith("120000 ")]
                for start in range(0, len(links), 64):
                    await _git(service, programs, "-c", "core.symlinks=true", git_dir, work_tree,
                        "checkout-index", "--force", "--", *links[start:start + 64],
                        cwd=target, write=True, scope=restore_scope, runners=runners)
                await _git(service, programs, "read-tree", record["snapshot_sha"], cwd=target, write=True, scope=restore_scope, runners=runners)
            except (HTTPException, ExecutorError, OSError) as exc:
                raise HTTPException(409, f"Could not materialize the recovery snapshot; partial worktree retained at {_display(service, target)}: {exc}") from exc
            finally:
                try:
                    await restore_owner.cleanup()
                finally:
                    if not restore_owner.closed:
                        await service.context.background_manager.retain_foreground_cleanup(runner=restore_owner,
                            command="worktree snapshot restore cleanup", cwd=target.uri,
                            conversation_id=service.context.conversation_id, task_id="", parent_run_id="",
                            reason="worktree_snapshot_restore_cleanup_pending")
            if not service.context.turn_environment.is_remote:
                from backend.services.worktree_settings_service import register_managed_worktree
                register_managed_worktree(Path(target.native), Path(inventory.main.native))
        return {"restored": code == 0, "snapshot_id": snapshot_id, "path": _display(service, target),
            "branch": record["branch"], "error": error.strip() if code else None}


async def _switch(service, programs, path):
    inventory = await _inventory(service, programs)
    target, entry = await _registered(service, programs, inventory, path)
    tracked, _, _ = await _git(service, programs, "ls-files", "-z", cwd=target)
    file_count = sum(bool(name) for name in tracked.split("\0"))
    return {"success": True, "project": {"root_path": _display(service, target), "name": target.name,
        "is_git_repo": True, "is_worktree": target != inventory.main, "main_repo_path": _display(service, inventory.main),
        "current_branch": entry.get("branch", "").removeprefix("refs/heads/"), **service.identity(target)},
        "summary": "", "file_count": file_count}


async def executor_worktree_action(service, operation, **kwargs):
    """Keep the original service lease until an accepted transaction settles."""
    from backend.workspace.executor_editor import ExecutorEditorService

    async def execute():
        async with ExecutorEditorService(service.context, application_config=service.application_config) as captured:
            if operation in {"remove", "restore", "snapshot"} and not captured.scope.resolve_access(captured.cwd.uri).can_write:
                raise PermissionError(f"Executor write permission denied: {captured.cwd.uri}")
            programs = await target_programs(captured)
            if operation == "switch":
                return await _switch(captured, programs, **kwargs)
            if operation == "remove":
                return await _remove(captured, programs, **kwargs)
            if operation == "restore":
                return await _restore(captured, programs, **kwargs)
            if operation == "snapshot":
                return await _snapshot_action(captured, programs, **kwargs)
            inventory = await _inventory(captured, programs)
            records = await _snapshot_records(captured, programs, inventory)
            return {"snapshots": [record for record in records if not kwargs.get("conversation_id")
                or record["conversation_id"] == kwargs["conversation_id"]]}

    task = asyncio.create_task(execute(), name="executor-worktree-" + operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except BaseException:
            pass
        raise


async def executor_worktree_tool(tool, args, context):
    """Model snapshots share the workbench's executor refs and byte recovery."""
    from backend.workspace.executor_editor import ExecutorEditorService

    try:
        async with ExecutorEditorService(context, application_config=context.model_execution.config) as service:
            if tool.name == "remove_worktree":
                result = await executor_worktree_action(service, "remove", path=args["path"], force=bool(args.get("force")), model_relative=True)
                if not result["removed"]:
                    return tool._error_result(f"删除 worktree 失败: {result['error']}")
                repository = context.run_context.conversation_repository
                if repository is not None and context.conversation_id:
                    repository.unregister_worktree(context.conversation_id, path=result["path"])
                saved = f"\n  快照 ID: {result['snapshot_id']}" if result.get("snapshot_id") else ""
                return tool._success_result(f"已删除 worktree: {result['path']}{saved}")
            if tool.name == "worktree_snapshot":
                record = await executor_worktree_action(service, "snapshot", path=args.get("path"), label=str(args.get("label", "")))
                repository = context.run_context.conversation_repository
                if repository is not None and context.conversation_id:
                    repository.thread_resources.add(context.conversation_id, "worktree_snapshot", record["id"], record)
                return tool._success_result(
                    f"已保存 worktree 快照\n  快照 ID: {record['id']}\n  提交: {record['snapshot_sha'][:8]}\n"
                    f"  引用: {record['snapshot_ref']}\n  原路径: {record['original_path']}\n"
                    f"用 worktree_restore(snapshot_id=\"{record['id']}\") 可恢复。")
            if tool.name == "worktree_restore":
                result = await executor_worktree_action(service, "restore", snapshot_id=args["snapshot_id"], dest=args.get("dest"))
                if not result["restored"]:
                    return tool._error_result(f"恢复失败: {result['error']}")
                repository = context.run_context.conversation_repository
                if repository is not None and context.conversation_id:
                    repository.register_worktree(context.conversation_id, path=result["path"], backend="executor",
                        workspace_root=str(context.workspace_root))
                return tool._success_result(f"已从快照 {result['snapshot_id']} 恢复 worktree\n  路径: {result['path']}\n"
                    "该 worktree 处于 detached HEAD,改动以快照提交的形式存在。")
            result = await executor_worktree_action(service, "snapshots", conversation_id=args.get("conversation_id"))
            records = result["snapshots"]
            if not records:
                return tool._success_result("没有找到 worktree 快照")
            lines = [f"找到 {len(records)} 个快照:\n"]
            for index, record in enumerate(records, 1):
                lines.append(f"{index}. {record['id']}  ({record['snapshot_sha'][:8]})")
                lines.append(f"   原路径: {record['original_path']}")
                lines.append(f"   分支: {record['branch'] or '-'}  时间: {record['created_at']}")
                if record["label"]:
                    lines.append(f"   备注: {record['label']}")
            return tool._success_result("\n".join(lines))
    except HTTPException as error:
        return tool._error_result(str(error.detail))
    except (OSError, ValueError, ExecutorError, TimeoutError) as error:
        return tool._error_result(str(error))
