"""Git delivery with target tools, auth and temporary messages on one executor."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from backend.workspace.executor_commands import require_workspace_registration, run_workbench_command, target_programs
from backend.workspace.executor_git import executor_git_environment, executor_git_write_scope, run_executor_git
from backend.workspace.executor_service import executor_registration_id


async def _git(service, *args, allow_failure=False):
    result = await run_executor_git(service, ["git", *args])
    if result.returncode and not allow_failure:
        raise HTTPException(409, result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace").strip(), result.returncode, result.stderr.decode("utf-8", errors="replace").strip()


async def executor_delivery_status(service, *, include_github=True, programs=None):
    root, code, error = await _git(service, "rev-parse", "--show-toplevel", allow_failure=True)
    if code:
        return {"is_git_repo": False, "error": error, "registration_id": executor_registration_id(service.context), **service.identity()}
    branch, remotes, upstream, log = await asyncio.gather(_git(service, "branch", "--show-current"),
        _git(service, "remote"), _git(service, "rev-parse", "--abbrev-ref", "@{upstream}", allow_failure=True),
        _git(service, "log", "-8", "--format=%h%x09%s", allow_failure=True))
    result = {"is_git_repo": True, "repo_root": root, "branch": branch[0], "remotes": remotes[0].splitlines(),
        "upstream": upstream[0] if upstream[1] == 0 else "", "detached": not branch[0],
        "commits": [{"hash": row.split("\t", 1)[0], "subject": row.split("\t", 1)[1]} for row in log[0].splitlines()],
        "registration_id": executor_registration_id(service.context), **service.identity()}
    if include_github:
        programs = await target_programs(service) if programs is None else programs
        host = programs["__github_host__"] or "github.com"
        remote_urls, _, _ = await _git(service, "remote", "-v")
        eligible = False
        for row in remote_urls.splitlines():
            remote = row.split()[1]
            candidate = urlsplit(remote).hostname if "://" in remote else remote.split(":", 1)[0].rsplit("@", 1)[-1]
            if candidate in {"github.com", host}:
                host, eligible = candidate, True
                break
        github = {"eligible": eligible, "host": host, "available": "gh" in programs,
            "authenticated": False, "remote": True, "message": "当前仓库未关联 GitHub。"}
        if eligible and "gh" in programs:
            argv = [programs["gh"], "auth", "status", "--hostname", host, "--json", "hosts", "--active"]
            auth = await run_workbench_command(service, argv, timeout=15,
                env={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "CLICOLOR": "0"}, allow_failure=True)
            if auth.stdout.strip():
                accounts = json.loads(auth.stdout)["hosts"].get(host, [])
                github["authenticated"] = any(item.get("state") == "success" and item.get("active") for item in accounts)
                github["message"] = "" if github["authenticated"] else "请在所选设备连接 GitHub 后刷新。"
            elif auth.exit_code:
                raise HTTPException(502, auth.stderr.strip())
            else:
                raise HTTPException(502, "The selected device did not return GitHub connection status.")
        elif eligible:
            github["message"] = "所选设备尚未安装 GitHub CLI。"
        result["github"] = github
    return result


async def executor_delivery_action(service, request):
    require_workspace_registration(service, request.expected_registration_id)
    programs = await target_programs(service)
    policy, overrides = executor_git_environment(service)
    environment = {**overrides, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    if request.action == "init":
        scope = await executor_git_write_scope(service, initialize=True)
        result = await run_workbench_command(service, [programs["git"], "init", "-b", "main"],
            env=environment, shell_policy=policy, permission_scope=scope)
        return {"message": result.stdout.strip(), **service.identity()}
    status = await executor_delivery_status(service, include_github=request.action == "draft_pr", programs=programs)
    if not status["is_git_repo"]:
        raise HTTPException(409, "当前文件夹没有 Git 仓库。")
    if request.expected_branch != status["branch"]:
        raise HTTPException(409, "当前分支已经变化，请刷新后再操作。")
    scope = await executor_git_write_scope(service)
    input_text = None
    if request.action == "branch":
        await _git(service, "check-ref-format", "--branch", request.branch)
        argv = [programs["git"], "switch", "-c", request.branch]
    elif request.action == "commit":
        if not request.message.strip():
            raise HTTPException(422, "请填写提交说明。")
        argv, input_text = [programs["git"], "commit", "--file"], request.message
    elif request.action == "push":
        if not status["branch"] or request.remote not in status["remotes"]:
            raise HTTPException(422, "请选择当前仓库的远端，并切换到一个分支。")
        argv = [programs["git"], "push", "--set-upstream", "--", request.remote, "HEAD"]
    else:
        github = status["github"]
        if not github["eligible"]:
            raise HTTPException(409, "当前仓库未关联 GitHub，暂不能创建 PR。")
        if not github["available"]:
            raise HTTPException(503, "所选设备尚未安装 GitHub CLI。")
        if not github["authenticated"]:
            raise HTTPException(401, "请在所选设备连接 GitHub 后刷新。")
        if not request.title.strip() or not request.base.strip() or not status["branch"]:
            raise HTTPException(422, "请填写 PR 标题、目标分支，并切换到一个分支。")
        argv = [programs["gh"], "pr", "create", "--draft", "--head", status["branch"], "--base", request.base,
            "--title", request.title, "--body-file"]
        input_text = request.body
    result = await run_workbench_command(service, argv, env=environment, shell_policy=policy,
        permission_scope=scope, input_text=input_text, programs=programs)
    return {"message": result.stdout.strip(), "url": result.stdout.strip() if request.action == "draft_pr" else "", **service.identity()}
