"""MiniCode Git capabilities executed in the accepted filesystem namespace."""
from __future__ import annotations

from dataclasses import replace
from backend.environments.errors import ExecutorError
from backend.sandbox.policy import (AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
    FileSystemPermissions, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSandboxKind,
    FileSystemSpecialPath, PermissionProfile, SandboxEnforcement)
from backend.workspace.executor_service import ExecutorWorkspaceService
from backend.workspace.executor_paths import ExecutorPath


def executor_git_environment(service, *, index_file=None):
    from backend.tools.git_support import _GIT_REPOSITORY_ENV

    policy = service.config.shell_environment_policy
    environment = {key: value for key, value in policy.set_values.items()
                   if key.upper() not in _GIT_REPOSITORY_ENV and not key.upper().startswith("GIT_CONFIG")}
    overrides = {}
    if service.scope.user_home_uri:
        overrides["GIT_CEILING_DIRECTORIES"] = service.resolve(service.scope.user_home_uri).native
    if index_file is not None:
        overrides["GIT_INDEX_FILE"] = service.resolve(str(index_file)).native
    return replace(policy, exclude=tuple([*policy.exclude, *_GIT_REPOSITORY_ENV, "GIT_CONFIG*"]),
        set_values=environment), overrides


async def run_executor_git_metadata(service, args, *, cwd=None, timeout=5):
    """Fixed app-owned metadata queries on the original accepted executor.

    Source git-utils runs metadata as host reads when reads are unrestricted.
    A real read restriction keeps the original enforcing scope. Model Git
    tools and Git mutations use run_executor_git instead of this capability.
    """
    policy, overrides = executor_git_environment(service)
    env_policy = {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
        "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}
    metadata_scope = (replace(service.scope, permission_profile=PermissionProfile.disabled())
        if service.scope.has_full_disk_read_access() else service.scope)
    hooks_path = "NUL" if service.cwd.convention == "windows" else "/dev/null"
    argv = ["git", "--no-optional-locks", "-c", "safe.bareRepository=explicit",
        "-c", f"core.hooksPath={hooks_path}", "-c", "core.fsmonitor=false", "--literal-pathspecs", *args]
    return await service.run(argv, cwd=cwd, timeout=timeout, env_policy=env_policy,
        env=overrides, permission_scope=metadata_scope)


async def registered_worktree_metadata(service, cwd, gitdir, common):
    """Source git-utils worktree/trust ownership, through the original executor FS."""
    async def canonical(path):
        if not service.scope.resolve_access(path.uri).can_read:
            raise PermissionError(f"Git metadata is not readable: {path.uri}")
        return service.resolve(await service.filesystem.canonicalize(path.uri, sandbox=service.sandbox))

    async def metadata_text(path):
        if not service.scope.resolve_access(path.uri).can_read:
            raise PermissionError(f"Git metadata is not readable: {path.uri}")
        metadata = await service.filesystem.get_metadata(path.uri, sandbox=service.sandbox, follow_symlinks=False)
        if not metadata["isFile"] or metadata["isSymlink"] or metadata["size"] > 64 * 1024:
            raise PermissionError(f"Git ownership metadata is not a regular file: {path.uri}")
        contents = await service.filesystem.read_file(path.uri, sandbox=service.sandbox, follow_symlinks=False)
        if len(contents) > 64 * 1024:
            raise PermissionError(f"Git ownership metadata exceeds the Source boundary: {path.uri}")
        text = contents.decode("utf-8").strip()
        if not text or "\n" in text or "\r" in text:
            raise PermissionError(f"Git ownership metadata does not contain one path: {path.uri}")
        return text

    def relative(base, value):
        return ExecutorPath.resolve(value, cwd_uri=base.uri, platform_os=service.scope.platform_os)

    dot_git = cwd.joinpath(".git")
    pointer = await metadata_text(dot_git)
    if not pointer.startswith("gitdir:") or not pointer[len("gitdir:"):].strip():
        raise PermissionError("The checkout does not identify a registered worktree")
    linked = relative(cwd, pointer[len("gitdir:"):].strip())
    link_metadata = await service.filesystem.get_metadata(linked.uri, sandbox=service.sandbox, follow_symlinks=False)
    if not link_metadata["isDirectory"] or link_metadata["isSymlink"]:
        raise PermissionError("The worktree Git directory is not a regular directory")
    actual_gitdir, actual_common = await canonical(linked), await canonical(common)
    if actual_gitdir.uri != (await canonical(gitdir)).uri or actual_gitdir.parent.name != "worktrees" or actual_gitdir.parent.parent.uri != actual_common.uri:
        raise PermissionError("The worktree administrative directory does not belong to its common repository")
    backlink = relative(actual_gitdir, await metadata_text(actual_gitdir.joinpath("gitdir")))
    linked_common = relative(actual_gitdir, await metadata_text(actual_gitdir.joinpath("commondir")))
    if (backlink.name != ".git" or (await canonical(backlink.parent)).uri != (await canonical(cwd)).uri
            or (await canonical(linked_common)).uri != actual_common.uri):
        raise PermissionError("The worktree administrative backlink does not identify this checkout")
    main_git = actual_common.parent.joinpath(".git")
    main_metadata = await service.filesystem.get_metadata(main_git.uri, sandbox=service.sandbox, follow_symlinks=False)
    if not main_metadata["isDirectory"] or main_metadata["isSymlink"]:
        raise PermissionError("The primary checkout does not have its own regular Git directory")
    if (await canonical(main_git)).uri != actual_common.uri:
        raise PermissionError("The main checkout no longer owns the worktree repository")
    return (actual_gitdir, actual_common)


def executor_git_action_scope(scope, *, write_paths=(), policy_cwd=None):
    """A fixed Git action writes only its declared data and metadata paths."""
    profile = scope.permission_profile
    if profile.enforcement is not SandboxEnforcement.MANAGED:
        return scope
    filesystem = profile.file_system
    entries = []
    if filesystem.kind is FileSystemSandboxKind.UNRESTRICTED:
        entries.append(FileSystemSandboxEntry(FileSystemPath("special", FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ))
    else:
        for entry in filesystem.entries:
            selector = entry.path
            if selector.kind == "glob_pattern":
                selector = FileSystemPath.glob(scope.resolve_path(str(selector.value)).native)
            entries.append(replace(entry, path=selector,
                access=FileSystemAccessMode.READ if entry.access is FileSystemAccessMode.WRITE else entry.access))
    entries.extend(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.WRITE) for path in write_paths)
    return replace(scope, cwd_uri=policy_cwd.uri if policy_cwd is not None else scope.cwd_uri,
        permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries,
            glob_scan_max_depth=filesystem.glob_scan_max_depth), network=profile.network))


async def executor_git_write_scope(service, *, cwd=None, initialize=False, write_paths=()):
    accepted = service.context.permission.environment_scope
    if not accepted.resolve_access(accepted.cwd_uri).can_write:
        raise PermissionError("Git metadata writes require workspace-write authority")
    if initialize:
        paths = [service.cwd.joinpath(".git")]
    else:
        discovered = await run_executor_git(service,
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd=cwd)
        if discovered.returncode != 0:
            raise PermissionError(discovered.stderr.decode("utf-8", errors="replace").strip())
        paths = [service.resolve(name) for name in discovered.stdout.decode("utf-8").splitlines()]
    external_readonly = [path for path in paths if not path.is_relative_to(service.cwd)
        and not service.scope.resolve_access(path.uri).can_write]
    verified_metadata = ()
    if external_readonly:
        # A user-initiated Git action may write the linked repository metadata,
        # just as it may write cwd/.git. It never grants the main checkout.
        verified_metadata = await registered_worktree_metadata(service, cwd or service.cwd, *paths)
    grants = []
    for path in paths:
        access = service.scope.resolve_access(path.uri)
        if not access.can_read or not path.is_relative_to(service.cwd) and not access.can_write and path not in verified_metadata:
            raise PermissionError(f"Git metadata needs explicit write authority: {path.uri}")
        grants.append(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.WRITE))
    if initialize:
        return service.scope.with_additional_permissions(AdditionalPermissionProfile(file_system=FileSystemPermissions(tuple(grants))))
    for path in write_paths:
        if not service.scope.resolve_access(path.uri).can_write:
            raise PermissionError(f"Git action data path needs write authority: {path.uri}")
    # The linked checkout's normal writable-root SID can retain its .git DENY
    # ACE from earlier commands. Metadata actions do not need that write root.
    # The real process cwd remains independent from this allowed policy cwd.
    common = paths[-1]
    return executor_git_action_scope(service.scope, write_paths=(common, *write_paths),
        policy_cwd=write_paths[0] if write_paths else common)


async def run_executor_git(service, argv, *, cwd=None, write_metadata=False, timeout=30, index_file=None, write_paths=()):
    policy, overrides = executor_git_environment(service, index_file=index_file)
    env_policy = {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
        "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}
    process_scope = executor_git_action_scope(service.scope)
    if write_metadata:
        process_scope = await executor_git_write_scope(service, cwd=cwd, write_paths=write_paths)
    else:
        argv = [argv[0], "--no-optional-locks", *argv[1:]]
    return await service.run(argv, cwd=cwd, timeout=timeout, env_policy=env_policy, env=overrides, permission_scope=process_scope)


def _message(result):
    return result.stderr.decode("utf-8", errors="replace").strip()


async def executor_git_tool(tool, args, context):
    from backend.tools.git_support import _raise_if_cancelled

    try:
        async with ExecutorWorkspaceService(context) as service:
            if tool.name == "git_status":
                directory = service.resolve(args.get("path") or ".")
                result = await run_executor_git(service, ["git", "status", "--porcelain=v1", "--branch", "-z"], cwd=directory)
                if result.returncode != 0:
                    if "not a git repository" in _message(result).lower():
                        return tool._success_result("当前工作区不是 Git 仓库；跳过 Git 状态检查。")
                    return tool._error_result(_message(result))
                location = await run_executor_git(service, ["git", "rev-parse", "--show-toplevel"], cwd=directory)
                if location.returncode != 0:
                    return tool._error_result(_message(location))
                root = service.resolve(location.stdout.decode("utf-8").strip())
                entries, visible = iter(result.stdout.decode("utf-8").split("\0")), []
                for record in entries:
                    if not record:
                        continue
                    if record.startswith("## "):
                        visible.append(record)
                        continue
                    status, target = record[:2], record[3:]
                    source = next(entries) if "R" in status or "C" in status else None
                    paths = [root.joinpath(target)] + ([root.joinpath(source)] if source is not None else [])
                    if any(not path.is_relative_to(service.cwd) or not service.scope.resolve_access(path.uri).can_read for path in paths):
                        continue
                    visible.append(f"{status} {source + ' -> ' if source else ''}{target}")
                return tool._success_result("\n".join(visible) or "工作区干净，没有未提交的更改")
            if tool.name == "git_diff":
                staged = ["--staged"] if args.get("staged") else []
                if args.get("file_path") and not service.scope.resolve_access(args["file_path"]).can_read:
                    return tool._error_result(f"Read permission denied: {args['file_path']}")
                query = ["--", ":(literal)" + service.resolve(args["file_path"]).relative_to(service.cwd).as_posix()] if args.get("file_path") else ["--"]
                common = ["git", "diff", "--relative", "--no-ext-diff", "--no-textconv", *staged]
                changed = await run_executor_git(service, [*common, "--no-renames", "--name-only", "-z", *query])
                if changed.returncode != 0:
                    if "not a git repository" in _message(changed).lower():
                        return tool._success_result("当前工作区不是 Git 仓库；没有 Git diff。")
                    return tool._error_result(_message(changed))
                denied = [name for name in changed.stdout.decode("utf-8").split("\0")
                          if name and not service.scope.resolve_access(service.cwd.joinpath(name).uri).can_read]
                result = await run_executor_git(service, [*common, f"--unified={args.get('context_lines', 3)}", *query,
                    *(f":(exclude,literal){name}" for name in denied)])
                empty = "暂存区没有变更" if args.get("staged") else "工作区没有变更"
            elif tool.name == "git_log":
                argv = ["git", "log", f"-{args.get('limit', 10)}", "--oneline" if args.get("oneline") else "--pretty=format:%h - %an, %ar : %s"]
                if args.get("file_path"):
                    path = service.resolve(args["file_path"])
                    if not service.scope.resolve_access(path.uri).can_read:
                        return tool._error_result(f"Read permission denied: {path.uri}")
                    argv.extend(["--", ":(literal)" + path.relative_to(service.cwd).as_posix()])
                result = await run_executor_git(service, argv)
                empty = "没有提交历史"
            else:
                if not args.get("message"):
                    return tool._error_result("缺少提交信息")
                if args.get("add_all"):
                    added = await run_executor_git(service, ["git", "add", "-A"], write_metadata=True)
                    if added.returncode != 0:
                        return tool._error_result(f"暂存失败，未创建提交: {_message(added)}")
                    _raise_if_cancelled(context)
                result = await run_executor_git(service, ["git", "commit", "-m", args["message"]], write_metadata=True)
                empty = "没有需要提交的更改"
            if result.returncode != 0:
                if "not a git repository" in _message(result).lower() and tool.read_only:
                    return tool._success_result("当前工作区不是 Git 仓库；没有 Git 提交历史。")
                return tool._error_result(_message(result))
            return tool._success_result(result.stdout.decode("utf-8", errors="replace").strip() or empty)
    except (OSError, ValueError, ExecutorError, TimeoutError) as error:
        return tool._error_result(str(error))
