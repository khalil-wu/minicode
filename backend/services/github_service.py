"""GitHub connection and repository capabilities used by the desktop workbench."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import anyio
from pathlib import Path
from urllib.parse import urlsplit

from backend.async_cleanup import to_thread_cancel_safe
from backend.diff.git_integration import is_not_git_repository
from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import communicate, spawn_exec, terminate_process_tree


class GitHubCommandError(Exception):
    """A GitHub CLI request returned a failure rather than a connection result."""


def github_cli_command() -> str | None:
    configured = os.environ.get("MINICODE_GH_COMMAND", "")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("gh")


def github_host() -> str:
    return os.environ.get("GH_HOST") or "github.com"


def github_command_env() -> dict[str, str]:
    return {**sanitized_subprocess_env(), "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "CLICOLOR": "0"}


async def github_connection_status() -> dict:
    command = github_cli_command()
    host = github_host()
    base = {"available": bool(command), "authenticated": False, "login": None, "host": host}
    if command is None:
        return {**base, "message": "GitHub 连接组件尚未就绪。"}
    process = await spawn_exec(command, "auth", "status", "--hostname", host, "--json", "hosts", "--active",
        env=github_command_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await communicate(process, timeout=15)
    if not output.strip():
        if process.returncode:
            raise GitHubCommandError(error.decode("utf-8", errors="replace").strip())
        raise GitHubCommandError("GitHub did not return connection status")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise GitHubCommandError("GitHub returned an unreadable connection status") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), dict):
        raise GitHubCommandError("GitHub returned an invalid connection status")
    account = next((entry for entry in payload.get("hosts", {}).get(host, [])
        if entry.get("active") and entry.get("state") == "success"), None)
    if account is None:
        return {**base, "message": "连接 GitHub 后可查看 Pull Request 和检查结果。"}
    return {**base, "authenticated": True, "login": account.get("login"), "message": "GitHub 已连接。"}


async def github_repository_context(root: Path) -> dict:
    from backend.services.workspace_api_service import run_ui_git_metadata

    # These fixed, read-only queries belong to the authenticated workbench,
    # not model-issued command execution. They need neither tool secrets nor
    # a code-execution sandbox to discover a local repository.
    async def metadata(*args: str):
        try:
            result = await to_thread_cancel_safe(run_ui_git_metadata, root, *args, timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise GitHubCommandError("读取 Git 仓库信息超时。") from exc
        return result.returncode, result.stdout, result.stderr

    code, _output, error = await metadata("rev-parse", "--is-inside-work-tree")
    if code:
        if is_not_git_repository(code, error):
            return {"is_git_repo": False, "eligible": False, "host": "", "branch": ""}
        raise GitHubCommandError(error)
    code, remotes, error = await metadata("config", "--get-regexp", r"^remote\..*\.url$")
    if code not in {0, 1}:
        raise GitHubCommandError(error)
    host = ""
    for line in remotes.splitlines():
        remote = line.split(None, 1)[1]
        candidate = urlsplit(remote).hostname if "://" in remote else remote.split(":", 1)[0].rsplit("@", 1)[-1]
        if candidate in {"github.com", github_host()}:
            host = candidate
            break
    code, branch, error = await metadata("symbolic-ref", "--quiet", "--short", "HEAD")
    if code not in {0, 1}:
        raise GitHubCommandError(error)
    return {"is_git_repo": True, "eligible": bool(host), "host": host,
        "branch": branch.strip() if code == 0 else ""}


async def github_login_events(command: str):
    """Own the CLI's browser/device flow for the lifetime of the HTTP stream."""
    host = github_host()
    process = await spawn_exec(command, "auth", "login", "--hostname", host, "--git-protocol", "https", "--web", "--clipboard=false",
        env=github_command_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        yield {"phase": "starting", "message": "正在准备 GitHub 授权…"}
        last_line = ""
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                last_line = line
            code = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", line)
            if code:
                yield {"phase": "authorizing", "user_code": code.group(1),
                    "message": "请复制验证码，然后打开 GitHub 授权页面。"}
            link = re.search(r"Open this URL to continue in your web browser:\s*(\S+)", line)
            if link:
                target = link.group(1)
                try:
                    parsed = urlsplit(target)
                except ValueError as exc:
                    raise GitHubCommandError("GitHub returned an invalid authorization URL") from exc
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.casefold() != host.casefold():
                    raise GitHubCommandError("GitHub returned an invalid authorization URL")
                yield {"phase": "authorizing", "verification_uri": target,
                    "message": "请打开 GitHub 授权页面确认连接。"}
        await process.wait()
        if process.returncode:
            yield {"phase": "error", "message": "GitHub 授权未完成，请重新连接。", "detail": last_line}
            return
        connection = await github_connection_status()
        if connection["authenticated"]:
            yield {"phase": "connected", "connection": connection}
        else:
            yield {"phase": "error", "message": "尚未确认 GitHub 账号连接，请刷新连接状态。"}
    finally:
        if process.returncode is None:
            with anyio.CancelScope(shield=True):
                await terminate_process_tree(process)
