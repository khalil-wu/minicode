"""MiniCode Git capabilities executed in the accepted filesystem namespace."""
from __future__ import annotations

from backend.environments.errors import ExecutorError
from backend.sandbox.policy import (AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
    FileSystemPermissions, FileSystemSandboxEntry)
from backend.workspace.executor_service import ExecutorWorkspaceService


async def run_executor_git(service, argv, *, cwd=None, write_metadata=False, timeout=30, index_file=None):
    from backend.tools.git_support import _GIT_REPOSITORY_ENV

    policy = service.config.shell_environment_policy
    environment = {key: value for key, value in policy.set_values.items()
                   if key.upper() not in _GIT_REPOSITORY_ENV and not key.upper().startswith("GIT_CONFIG")}
    overrides = {}
    if service.scope.user_home_uri:
        overrides["GIT_CEILING_DIRECTORIES"] = service.resolve(service.scope.user_home_uri).native
    if index_file is not None:
        overrides["GIT_INDEX_FILE"] = service.resolve(str(index_file)).native
    env_policy = {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
        "exclude": [*policy.exclude, *_GIT_REPOSITORY_ENV, "GIT_CONFIG*"],
        "set": environment, "includeOnly": list(policy.include_only)}
    process_scope = service.scope
    if write_metadata:
        if not service.scope.resolve_access(service.cwd.uri).can_write:
            raise PermissionError("Git metadata writes require workspace-write authority")
        discovered = await run_executor_git(service,
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd=cwd)
        if discovered.returncode != 0:
            raise PermissionError(discovered.stderr.decode("utf-8", errors="replace").strip())
        grants = []
        for name in discovered.stdout.decode("utf-8").splitlines():
            path = service.resolve(name)
            access = service.scope.resolve_access(path.uri)
            if not access.can_read or not path.is_relative_to(service.cwd) and not access.can_write:
                raise PermissionError(f"Git metadata needs explicit write authority: {path.uri}")
            grants.append(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.WRITE))
        process_scope = service.scope.with_additional_permissions(AdditionalPermissionProfile(file_system=FileSystemPermissions(tuple(grants))))
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
