"""Resolve a saved PR reference on the conversation's original host."""
from __future__ import annotations

import asyncio
import json
import shutil
from typing import Literal
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.async_cleanup import to_thread_cancel_safe
from backend.conversations.thread_resources import pull_request_identity
from backend.services.github_service import github_cli_command, github_command_env
from backend.services.workspace_api_service import capture_workspace_execution
from backend.subprocesses import communicate, spawn_exec


_FIELDS = "number,title,state,isDraft,url,headRefName,statusCheckRollup"


class PullRequestStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    number: int
    title: str
    state: Literal["OPEN", "MERGED", "CLOSED"]
    isDraft: bool
    url: str
    headRefName: str
    statusCheckRollup: list[dict] | None = None


class GitLabMergeRequestStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    iid: int
    title: str
    state: Literal["opened", "locked", "merged", "closed"]
    draft: bool
    web_url: str
    source_branch: str


async def _local_status(program, arguments, cwd):
    command = github_cli_command() if program == "gh" else shutil.which("glab")
    if command is None:
        raise HTTPException(503, f"{program} is not available on the owning host")
    process = await spawn_exec(command, *arguments, cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=github_command_env())
    output, error = await communicate(process, timeout=15)
    if process.returncode:
        raise HTTPException(502, error.decode("utf-8", errors="replace").strip())
    return output


async def linked_pull_request_status(session, conversation_id, resource_id):
    repository = session.conversation_repo
    conversation = await to_thread_cancel_safe(repository.get_conversation_summary, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    reference = next((item for item in conversation.resource_references
        if item["id"] == resource_id and item["attachment_type"] == "pull_request"), None)
    if reference is None:
        raise HTTPException(404, "Pull request reference not found")
    payload = reference["payload"]
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise HTTPException(409, "The saved pull request has no usable URL")
    url = payload["url"]
    try:
        identity = pull_request_identity(url)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if identity != reference["identity_key"]:
        raise HTTPException(409, "The saved pull request URL does not match its identity")
    gitlab = "/-/merge_requests/" in url
    if gitlab:
        host, owner, repository_name, number = json.loads(identity)
        endpoint = f"projects/{quote(owner + '/' + repository_name, safe='')}/merge_requests/{number}"
        program, arguments = "glab", ["api", "--hostname", host, endpoint]
    else:
        program, arguments = "gh", ["pr", "view", url, "--json", _FIELDS]

    try:
        async with capture_workspace_execution(session, conversation_id) as context:
            if context is not None and context.turn_environment.is_remote:
                from backend.workspace.executor_commands import run_workbench_command, target_programs
                from backend.workspace.executor_editor import ExecutorEditorService

                async with ExecutorEditorService(context, application_config=session.config) as service:
                    programs = await target_programs(service)
                    if program not in programs:
                        raise HTTPException(503, f"{program} is not available on the owning executor")
                    result = await run_workbench_command(service, [programs[program], *arguments],
                        timeout=15, env={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "CLICOLOR": "0"})
                    output = result.stdout
            else:
                output = await _local_status(program, arguments, repository.storage_root)
        if gitlab:
            merge_request = GitLabMergeRequestStatus.model_validate_json(output)
            status = PullRequestStatus(number=merge_request.iid, title=merge_request.title,
                state={"opened": "OPEN", "locked": "OPEN", "closed": "CLOSED", "merged": "MERGED"}[merge_request.state],
                isDraft=merge_request.draft, url=merge_request.web_url, headRefName=merge_request.source_branch, statusCheckRollup=[])
        else:
            status = PullRequestStatus.model_validate_json(output)
        if pull_request_identity(status.url) != identity:
            raise HTTPException(502, "The upstream response belongs to a different pull request")
        return {"reference_id": resource_id, "conversation_id": conversation_id, "pr": status.model_dump()}
    except ValidationError as error:
        raise HTTPException(502, "The owning provider returned an invalid pull request response") from error
    except asyncio.TimeoutError as error:
        raise HTTPException(504, "Pull request status request timed out") from error
    except OSError as error:
        raise HTTPException(503, str(error)) from error
