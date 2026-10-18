"""Source desktop's dedicated, tool-free commit and PR message generation."""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.services.git_settings_service import get_git_preferences
from backend.workspace.delivery import run_delivery_command
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.executor_git import run_executor_git


class GitMessageRequest(BaseModel):
    kind: Literal["commit", "pull_request"]
    message: str = Field(default="", max_length=20000)
    base: str = ""
    expected_branch: str
    expected_registration_id: str = ""


class CommitMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    message: str = Field(min_length=8, max_length=4000, pattern=r"^[^\r\n]{1,72}(?:\r?\n[\s\S]*)?$")


class PullRequestMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    title: str = Field(min_length=8, max_length=120)
    body: str = Field(min_length=12, max_length=30000)


# Current desktop src-DsLVGakC.js DJ/OJ; the user guidance is scoped to
# generated text. This is a side query, not a main-agent turn with Git tools.
COMMIT_GENERATION_INSTRUCTIONS = """Using the supplied git context below, generate a git commit message.
Write the result into the structured response field message.
message must contain plain commit-message text only, not JSON, field labels, or code fences.
Custom commit instructions for message content and formatting override the fallback rules below.
Make 0 tool calls.
Bounds:
- Keep the complete message under 4000 characters.
- Keep the subject under 72 characters.
Fallback rules:
- Generate a concise single-line subject.
- Use an imperative verb first.
- Do not add a scope prefix unless the context already clearly uses one.
- Do not include markdown, quotes, or trailing punctuation."""

PR_GENERATION_INSTRUCTIONS = """You are a helpful assistant. Generate a pull request title and body.
Write the result into the structured response fields title and body.
Make 0 tool calls.
If context includes pull request instructions, follow them even when they conflict with the default rules below.
Language rules:
- Match the primary language of the supplied context; default to English.
- Translate standard section headings such as Summary and Testing when writing in another language.
Fallback PR title rules:
- title must contain only the PR title, not JSON, field labels, or body content.
- Use an imperative or action-oriented phrasing first.
- Keep the title under 120 characters.
- No trailing punctuation.
Body rules:
- body must contain only the PR body, not JSON, field labels, the title, or a full PR draft.
- Do not repeat, restate, or label the title inside body.
- Keep the body concise and scannable.
- Keep the body under 30000 characters.
- Use Markdown with short bullets.
- Include a Summary section and a Testing section.
- In Testing, describe meaningful validation at a high level, such as new unit or integration tests, or local UI testing with Playwright.
- Do not paste command transcripts. For routine checks, summarize the result, for example: lint and formatting passed.
- If tests were not run, say "Not run (not requested)".
- If context includes pull request instructions, apply them to the title/body content only."""


async def generate_git_message(service, request: GitMessageRequest):
    from backend.config import load_config
    from backend.llm.base import LLMMessage, LLMTurnContext, SideQueryOptions
    from backend.llm.model_registry import create_session_llm
    from backend.workspace.executor_commands import require_workspace_registration

    executor = isinstance(service, ExecutorEditorService)
    if executor and service.context.turn_environment.is_remote:
        require_workspace_registration(service, request.expected_registration_id)
    root = None if executor else service.workspace_root_path()

    async def git(*args):
        if executor:
            result = await run_executor_git(service, ["git", *args])
            if result.returncode:
                raise HTTPException(409, result.stderr.decode("utf-8", errors="replace").strip())
            return result.stdout.decode("utf-8", errors="replace").strip()
        output, _, _ = await run_delivery_command(root, ["git", *args])
        return output

    branch = await git("branch", "--show-current")
    if branch != request.expected_branch:
        raise HTTPException(409, "当前分支已经变化，请刷新后再生成。")
    preferences = get_git_preferences()
    context = []
    if request.kind == "commit":
        diff = await git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color")
        if not diff:
            raise HTTPException(409, "没有已暂存的变更可用于生成提交说明。")
        if preferences.commit_instructions.strip():
            context.append("Custom commit instructions (follow these for commit message content and formatting):\n" + preferences.commit_instructions.strip())
        if request.message.strip():
            context.append("Draft message:\n" + request.message.strip())
        lines = diff.splitlines()
        added = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
        removed = sum(line.startswith("-") and not line.startswith("---") for line in lines)
        if added + removed > 1000:
            files = sum(line.startswith("diff --git ") for line in lines)
            context.append(f"Changes:\nDiff too large to include inline.\nSummary: {files} changed files, +{added}/-{removed} lines.\n\nUntracked changes are not included.")
        else:
            context.append("Changes:\n" + diff + "\nUntracked changes are not included.")
        context.append("Testing note: If you mention tests, include unit tests or UI testing frameworks only. Skip lint/tsc since CI runs those.")
        instructions, result_type, limit, timeout = COMMIT_GENERATION_INSTRUCTIONS, CommitMessage, 20000, 30
    else:
        base = await git("rev-parse", "--verify", "--end-of-options", request.base + "^{commit}")
        diff = await git("diff", "--no-ext-diff", "--no-textconv", "--no-color", base + "...HEAD")
        files = (await git("diff", "--name-only", "-z", base + "...HEAD")).split("\0")
        context.append(f"Branches:\n- Head: {branch}\n- Base: {request.base}")
        if preferences.pull_request_instructions.strip():
            context.append("Pull request instructions (apply these to the title/body content only):\n" + preferences.pull_request_instructions.strip())
        if len(diff.splitlines()) <= 1000:
            context.append("Changes:\n" + diff)
        else:
            context.append("Changes:\n" + "\n".join("- " + file for file in files[:100]))
        instructions, result_type, limit, timeout = PR_GENERATION_INSTRUCTIONS, PullRequestMessage, 30000, 45
    config = service.application_config if executor else load_config(cwd=root)
    adapter = create_session_llm(config)
    try:
        text = await adapter.side_query(
            [LLMMessage(role="system", content=instructions), LLMMessage(role="user", content="\n\n".join(context)[:limit])],
            options=SideQueryOptions(operation="git_" + request.kind + "_message", use_small_fast_model=True,
                disable_reasoning=True, enable_prompt_cache=False, output_schema=result_type.model_json_schema(),
                attempt_timeout_seconds=timeout, max_retries=0),
            turn_context=LLMTurnContext(),
        )
        try:
            result = result_type.model_validate_json(text)
        except ValidationError as exc:
            raise HTTPException(502, "模型返回的 Git 文案不符合生成格式，请重新生成。") from exc
        return {**result.model_dump(), **(service.identity() if executor else {})}
    finally:
        await adapter.aclose()
