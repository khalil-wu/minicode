"""Registered worktree lifecycle on the request's original executor."""
from __future__ import annotations

import asyncio
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import re
from uuid import uuid4

from fastapi import HTTPException

from backend.workspace.executor_commands import run_workbench_command, target_programs, workbench_policy
from backend.workspace.executor_git import executor_git_environment, executor_git_write_scope, run_executor_git, registered_worktree_metadata, executor_git_action_scope
from backend.workspace.executor_paths import ExecutorPath
from backend.terminal.executor_runner import ExecutorCommandRunner
from backend.environments.errors import ExecutorError, ExecutorProtocolError


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


async def _git(service, programs, *args, cwd=None, write=False, index=None, env=None, allowed=(0,), runners=None, write_paths=()):
    policy, overrides = executor_git_environment(service, index_file=index)
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
    top, _, _ = await _git(service, programs, "rev-parse", "--show-toplevel", cwd=target)
    common, _, _ = await _git(service, programs, "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=target)
    if await _canonical(service, top.strip()) != target or await _canonical(service, common.strip()) != inventory.common:
        raise HTTPException(409, "Worktree repository identity changed")
    primary_git = inventory.main.joinpath(".git")
    primary_info = await service.filesystem.get_metadata(primary_git.uri, sandbox=service.sandbox, follow_symlinks=False)
    if (not primary_info["isDirectory"] or primary_info["isSymlink"]
            or await _canonical(service, primary_git) != inventory.common or inventory.common.parent != inventory.main):
        raise HTTPException(409, "The primary checkout does not own this worktree repository")
    if target != inventory.main:
        gitdir, _, _ = await _git(service, programs, "rev-parse", "--path-format=absolute", "--git-dir", cwd=target)
        gitdir = await _canonical(service, gitdir.strip())
        await registered_worktree_metadata(service, target, gitdir, inventory.common)
    return target, entry


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


async def _snapshot_records(service, programs, inventory):
    output, _, _ = await _git(service, programs, "for-each-ref", "--format=%(refname)%00%(objectname)", _REFS)
    records = []
    for line in output.splitlines():
        ref, sha = line.split("\0")
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
    head, code, _ = await _git(service, programs, "rev-parse", "--verify", "--quiet", "HEAD", cwd=target, allowed=(0, 1))
    head = head.strip() if code == 0 else None
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
        for suffix in ("", ".lock"):
            path = service.resolve(index.native + suffix)
            if await service.metadata(path) is not None:
                await service.filesystem.remove(path.uri, sandbox=index_sandbox)

    await index_owner.add_cleanup_callback(remove_index)
    try:
        await _git(service, programs, "read-tree", *([head] if head else ["--empty"]),
            cwd=target, write=True, index=index.uri, runners=runners)
        await _git(service, programs, "add", "-A", "--force", cwd=target, write=True, index=index.uri, runners=runners)
        staged, _, _ = await _git(service, programs, "ls-files", "--stage", "-z", cwd=target, index=index.uri, runners=runners)
        regular = []
        for item in staged.split("\0"):
            if item:
                header, name = item.split("\t", 1)
                mode, _sha, _stage = header.split(" ")
                if mode in {"100644", "100755"}:
                    regular.append((mode, name))
        # Git add and checkout apply repository clean/smudge and newline
        # attributes. Recovery commits must retain the actual working bytes.
        for start in range(0, len(regular), 64):
            group = regular[start:start + 64]
            hashes, _, _ = await _git(service, programs, "hash-object", "-w", "--no-filters", "--",
                *(target.joinpath(name).native for _mode, name in group), cwd=target, write=True, runners=runners)
            entries = [(mode, name, sha) for (mode, name), sha in zip(group, hashes.splitlines(), strict=True)]
            await _git(service, programs, "update-index", "--add",
                *(value for mode, name, sha in entries for value in ("--cacheinfo", f"{mode},{sha},{name}")),
                cwd=target, write=True, index=index.uri, runners=runners)
        tree, _, _ = await _git(service, programs, "write-tree", cwd=target, write=True, index=index.uri, runners=runners)
        sha, _, _ = await _git(service, programs, "commit-tree", tree.strip(), *(["-p", head] if head else []),
            "-m", _MESSAGE + json.dumps(record, ensure_ascii=False), cwd=target, write=True,
            env={"GIT_AUTHOR_NAME": "MiniCode", "GIT_AUTHOR_EMAIL": "snapshot@minicode.local",
                 "GIT_COMMITTER_NAME": "MiniCode", "GIT_COMMITTER_EMAIL": "snapshot@minicode.local"}, runners=runners)
        await _git(service, programs, "update-ref", ref, sha.strip(), "", cwd=target, write=True, runners=runners)
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


async def _prune(service, programs, inventory, keep_id):
    cutoff = datetime.now(UTC) - timedelta(days=30)
    for index, record in enumerate(await _snapshot_records(service, programs, inventory)):
        if record["id"] != keep_id and (index >= 50 or datetime.fromisoformat(record["created_at"]) < cutoff):
            await _git(service, programs, "update-ref", "-d", record["snapshot_ref"], record["snapshot_sha"], write=True)


async def _snapshot_action(service, programs, path, label):
    inventory = await _inventory(service, programs)
    target, entry = await _registered(service, programs, inventory, service.resolve(path or service.cwd.uri))
    await _isolated(service, inventory, target)
    async with service.mutate([inventory.common, target]):
        entries = await _complete_files(service, target)
        if any((candidate := service.resolve(item["path"])).name == ".git" and candidate.parent != target for item in entries):
            raise HTTPException(409, "Worktrees containing initialized submodules or embedded Git repositories cannot be snapshotted")
        record = await _snapshot(service, programs, inventory, target, entry, label=label)
        await _prune(service, programs, inventory, record["id"])
        return record


async def _remove(service, programs, path, force):
    inventory = await _inventory(service, programs)
    target, entry = await _registered(service, programs, inventory, path)
    if target == inventory.current:
        raise HTTPException(409, "Cannot remove current worktree")
    await _isolated(service, inventory, target)
    service = await _action_parent(service, inventory, target.parent)
    async with service.mutate([inventory.common, target]):
        await _complete_files(service, target, write=True)
        dirty, _, _ = await _git(service, programs, "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all", cwd=target)
        result = {"removed": False, "path": _display(service, target), "branch": entry.get("branch", "").removeprefix("refs/heads/")}
        if dirty and not force:
            return {**result, "error": "Worktree has local changes; force is required"}
        if not service.context.turn_environment.is_remote:
            from backend.workspace.local_environment_lifecycle import run_context_worktree_environment
            await run_context_worktree_environment(Path(inventory.main.native), Path(target.native), "cleanup", service.context, application_config=service.application_config)
        if dirty:
            snapshot = await _snapshot(service, programs, inventory, target, entry)
            result.update(snapshot_id=snapshot["id"], snapshot_ref=snapshot["snapshot_ref"])
            await _prune(service, programs, inventory, snapshot["id"])
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
        _, code, error = await _git(service, programs, "-c", "core.symlinks=true", "worktree", "add", "--detach", "--", target.native,
            record["snapshot_sha"], write=True, write_paths=(parent,), allowed=(0, 1, 128))
        if code == 0:
            tree, _, _ = await _git(service, programs, "ls-tree", "-r", "-z", record["snapshot_sha"])
            for item in tree.split("\0"):
                if not item:
                    continue
                header, name = item.split("\t", 1)
                mode, _kind, sha = header.split(" ")
                if mode in {"100644", "100755"}:
                    blob = await run_executor_git(service, [programs["git"], "cat-file", "blob", sha])
                    if blob.returncode:
                        raise HTTPException(409, blob.stderr.decode("utf-8", errors="replace").strip())
                    file = await _canonical(service, target.joinpath(name), write=True)
                    if not file.is_relative_to(target):
                        raise HTTPException(409, "Restored file resolves outside the target worktree")
                    await service.filesystem.write_file(file.uri, blob.stdout, sandbox=service.sandbox, follow_symlinks=False)
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
            if tool.name == "worktree_snapshot":
                record = await executor_worktree_action(service, "snapshot", path=args.get("path"), label=str(args.get("label", "")))
                return tool._success_result(
                    f"已保存 worktree 快照\n  快照 ID: {record['id']}\n  提交: {record['snapshot_sha'][:8]}\n"
                    f"  引用: {record['snapshot_ref']}\n  原路径: {record['original_path']}\n"
                    f"用 worktree_restore(snapshot_id=\"{record['id']}\") 可恢复。")
            if tool.name == "worktree_restore":
                result = await executor_worktree_action(service, "restore", snapshot_id=args["snapshot_id"], dest=args.get("dest"))
                if not result["restored"]:
                    return tool._error_result(f"恢复失败: {result['error']}")
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
