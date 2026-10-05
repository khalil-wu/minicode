from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import communicate, spawn_exec
from backend.services.github_service import (
    GitHubCommandError, github_cli_command, github_connection_status, github_repository_context,
)


class GitDeliveryRequest(BaseModel):
    action: Literal["commit", "push", "draft_pr", "branch", "init"]
    message: str = Field(default="", max_length=20000)
    remote: str = ""
    branch: str = ""
    base: str = ""
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=50000)
    expected_branch: str = ""


async def run_delivery_command(
    root: Path,
    command: list[str],
    *,
    input_data: bytes | None = None,
    allow_failure=False,
):
    try:
        process = await spawn_exec(
            *command,
            cwd=root,
            env={
                **sanitized_subprocess_env(),
                "GIT_TERMINAL_PROMPT": "0",
                "GH_PROMPT_DISABLED": "1",
            },
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"未找到 {command[0]}，请安装后重试。") from exc
    stdout, stderr = await communicate(process, input_data=input_data, timeout=120)
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode and not allow_failure:
        raise HTTPException(status_code=409, detail=error or output or "Git 操作失败")
    return output, process.returncode, error


async def git_delivery_status(root: Path, *, include_github: bool = True):
    repo, status, error = await run_delivery_command(
        root, ["git", "rev-parse", "--show-toplevel"], allow_failure=True
    )
    if status:
        return {"is_git_repo": False, "error": error}
    branch, remotes, upstream, log = await asyncio.gather(
        run_delivery_command(root, ["git", "branch", "--show-current"]),
        run_delivery_command(root, ["git", "remote"]),
        run_delivery_command(
            root,
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
            allow_failure=True,
        ),
        run_delivery_command(
            root, ["git", "log", "-8", "--format=%h%x09%s"], allow_failure=True
        ),
    )
    result = {
        "is_git_repo": True,
        "repo_root": repo,
        "branch": branch[0],
        "remotes": remotes[0].splitlines(),
        "upstream": upstream[0] if upstream[1] == 0 else "",
        "commits": [
            {"hash": line.split("\t", 1)[0], "subject": line.split("\t", 1)[1]}
            for line in log[0].splitlines()
        ],
        "detached": not branch[0],
    }
    if include_github:
        repository = await github_repository_context(root)
        connection = {"available": bool(github_cli_command()), "authenticated": False,
            "message": "当前仓库未关联 GitHub。"}
        if repository["eligible"]:
            try:
                connection = await github_connection_status()
            except (GitHubCommandError, OSError, asyncio.TimeoutError):
                connection = {"available": bool(github_cli_command()), "authenticated": None,
                    "message": "暂时无法确认 GitHub 连接，请刷新连接设置。"}
        result["github"] = {**repository, **connection}
    return result


async def git_delivery_action(root: Path, request: GitDeliveryRequest):
    if request.action == "init":
        output, _, _ = await run_delivery_command(root, ["git", "init", "-b", "main"])
        return {"message": output}
    status = await git_delivery_status(root, include_github=False)
    if not status["is_git_repo"]:
        raise HTTPException(status_code=409, detail="当前文件夹没有 Git 仓库。")
    if request.expected_branch != status["branch"]:
        raise HTTPException(
            status_code=409, detail="当前分支已经变化，请刷新后再操作。"
        )
    if request.action == "branch":
        await run_delivery_command(
            root, ["git", "check-ref-format", "--branch", request.branch]
        )
        output, _, _ = await run_delivery_command(
            root, ["git", "switch", "-c", request.branch]
        )
    elif request.action == "commit":
        if not request.message.strip():
            raise HTTPException(status_code=422, detail="请填写提交说明。")
        output, _, _ = await run_delivery_command(
            root,
            ["git", "commit", "--file=-"],
            input_data=request.message.encode("utf-8"),
        )
    elif request.action == "push":
        if not status["branch"] or request.remote not in status["remotes"]:
            raise HTTPException(
                status_code=422, detail="请选择当前仓库的远端，并切换到一个分支。"
            )
        output, _, _ = await run_delivery_command(
            root, ["git", "push", "--set-upstream", "--", request.remote, "HEAD"]
        )
    else:
        repository = await github_repository_context(root)
        if not repository["eligible"]:
            raise HTTPException(status_code=409, detail="当前仓库未关联 GitHub，暂不能创建 PR。")
        command = github_cli_command()
        if command is None:
            raise HTTPException(status_code=503, detail="GitHub 连接组件尚未就绪，请查看连接设置。")
        try:
            connection = await github_connection_status()
        except (GitHubCommandError, OSError, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=502, detail="暂时无法确认 GitHub 连接，请重试。") from exc
        if not connection["authenticated"]:
            raise HTTPException(status_code=401, detail="GitHub 尚未连接，请先在设置中连接账号。")
        if (
            not request.title.strip()
            or not request.base.strip()
            or not status["branch"]
        ):
            raise HTTPException(
                status_code=422, detail="请填写 PR 标题、目标分支，并切换到一个分支。"
            )
        output, _, _ = await run_delivery_command(
            root,
            [
                command,
                "pr",
                "create",
                "--draft",
                "--head",
                status["branch"],
                "--base",
                request.base,
                "--title",
                request.title,
                "--body-file",
                "-",
            ],
            input_data=request.body.encode("utf-8"),
        )
    return {"message": output, "url": output if request.action == "draft_pr" else ""}
