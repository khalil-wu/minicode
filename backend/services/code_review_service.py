"""Installed Codex desktop Markdown review request preparation."""
from __future__ import annotations

from pathlib import Path
import asyncio
import subprocess
from typing import Any

from backend.services.workspace_api_service import run_ui_git_metadata

_GUIDELINES = Path(__file__).parents[1] / "agent" / "codex_prompt_resources" / "desktop-review-guidelines.md"
_UNCOMMITTED = "Review the current code changes (staged, unstaged, and untracked files) and provide concise, actionable feedback in a normal Markdown response."
_BASE_BRANCH = "Review the code changes against the base branch '{baseBranch}'. The merge base commit for this comparison is {mergeBaseSha}. Run `git diff {mergeBaseSha}` to inspect the changes relative to {baseBranch}. Provide concise, actionable feedback in a normal Markdown response."


def review_template_prompt() -> str:
    return "\n".join(["## Code review guidelines:", _GUIDELINES.read_text(encoding="utf-8").strip(), _UNCOMMITTED,
                      "## My request:", "Please review my uncommitted changes"])


def _git(cwd: Path, *args: str) -> str:
    result = run_ui_git_metadata(cwd, "-c", "core.quotePath=false", *args)
    result.check_returncode()
    return result.stdout.strip()


def _uncommitted_diff(git_root: Path) -> str:
    try:
        baseline = _git(git_root, "rev-parse", "--verify", "--quiet", "HEAD")
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        baseline = _git(git_root, "hash-object", "-t", "tree", "--stdin")
    args = ("-c", "core.quotePath=false", "diff", "--no-color", "--no-textconv", "--no-ext-diff")
    result = run_ui_git_metadata(git_root, *args, baseline, "--")
    result.check_returncode()
    patches = [result.stdout]
    inventory = run_ui_git_metadata(git_root, "ls-files", "--others", "--exclude-standard", "-z")
    inventory.check_returncode()
    paths = inventory.stdout.split("\0")
    for path in paths:
        if not path:
            continue
        result = run_ui_git_metadata(git_root, *args, "--no-index", "--", "/dev/null", path)
        if result.returncode not in {0, 1}:  # git diff --no-index reports differences with 1.
            result.check_returncode()
        patches.append(result.stdout)
    return "".join(patches)


def code_review_options(cwd: Path) -> dict[str, Any]:
    return asyncio.run(code_review_options_on_executor(str(cwd), None))


async def _review_location(cwd, service):
    from backend.diff.git_integration import _read_git

    git_root = (await _read_git(cwd, "rev-parse", "--show-toplevel", service=service)).strip()
    if service is None:
        return str(Path(cwd)), str(Path(git_root))
    root = service.resolve(git_root)
    remote = service.context.turn_environment.is_remote
    return (service.cwd.uri, root.uri) if remote else (service.cwd.native, root.native)


async def code_review_options_on_executor(cwd, service) -> dict[str, Any]:
    from backend.diff.comparison import review_options

    directory, git_root = await _review_location(cwd, service)
    options = await review_options(cwd, service)
    default = options["default_base_branch"]
    targets = list(dict.fromkeys([*([default] if default else []), *options["branches"]]))
    return {"cwd": directory, "gitRoot": git_root, "currentBranch": options["current_branch"] or "HEAD",
        "branches": [branch for branch in targets if branch != options["current_branch"]],
        "defaultBaseBranch": default, "comparisonStatus": options["comparison_status"], "reason": options["reason"]}


async def prepare_code_review_on_executor(cwd, service, mode, base_branch="", merge_base_sha="", request_message=""):
    directory, git_root = await _review_location(cwd, service)
    if service is None:
        return await _prepare_repository_review(directory, git_root, None, mode, base_branch, merge_base_sha, request_message)
    from backend.workspace.executor_editor import ExecutorEditorService

    # Source prepares repository-relative review patches. Keep the original
    # captured execution attachment/lease while selecting its real Git root;
    # the caller's cwd remains the public review scope's cwd.
    async with ExecutorEditorService(service.context, editor_root=service.resolve(git_root),
            application_config=service.application_config) as repository:
        return await _prepare_repository_review(directory, git_root, repository, mode, base_branch, merge_base_sha, request_message)


async def _prepare_repository_review(directory, git_root, service, mode, base_branch, merge_base_sha, request_message):
    from backend.diff.comparison import branch_comparison
    from backend.diff.git_integration import (get_comparison_diff, get_uncommitted_diff, get_untracked_files,
        get_untracked_file_diffs, _read_git)

    scope = {"mode": mode, "cwd": directory, "gitRoot": git_root}
    if mode == "uncommitted":
        result = await get_uncommitted_diff(git_root, service=service)
        untracked = await get_untracked_files(git_root, service=service)
        additions = await get_untracked_file_diffs(git_root, untracked, service=service)
        paths = set(untracked)
        patch = "".join(file.patch for file in result.files if file.path not in paths or file.status == "U")
        patch += "".join(file.patch for file in additions)
        instructions, request = _UNCOMMITTED, "Please review my uncommitted changes"
    elif mode == "base-branch":
        if not base_branch.strip():
            raise ValueError("Select a base branch for code review.")
        comparison = await branch_comparison(git_root, service, base_branch=base_branch, merge_base_sha=merge_base_sha or None)
        sha = comparison["merge_base_sha"]
        scope.update(baseBranch=base_branch, mergeBaseSha=sha)
        result = await get_comparison_diff(git_root, [sha], service=service, worktree=True)
        untracked = await get_untracked_files(git_root, service=service)
        additions = await get_untracked_file_diffs(git_root, untracked, service=service)
        paths = set(untracked)
        patch = "".join(file.patch for file in result.files if file.path not in paths or file.status == "U")
        patch += "".join(file.patch for file in additions)
        instructions = _BASE_BRANCH.format(baseBranch=base_branch, mergeBaseSha=sha)
        source_branch = (await _read_git(git_root, "symbolic-ref", "--quiet", "--short", "HEAD",
            service=service, allowed_exit_codes=(0, 1))).strip() or "HEAD"
        request = f"Please review changes on {source_branch} against {base_branch}"
    else:
        raise ValueError("Code review target must be uncommitted or base-branch.")
    request = request_message or request
    prompt = "\n".join(["## Code review guidelines:", _GUIDELINES.read_text(encoding="utf-8").strip(), instructions, "## My request:", request])
    return {"scope": scope, "prompt": prompt, "requestMessage": request, "diff": patch}


def normalize_code_review_scope(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("mode") not in {"uncommitted", "base-branch"}:
        raise ValueError("Code review target must be uncommitted or base-branch.")
    for field in ("cwd", "gitRoot"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"Code review {field} is required.")
    if raw["mode"] == "base-branch" and any(not isinstance(raw.get(field), str) or not raw[field].strip() for field in ("baseBranch", "mergeBaseSha")):
        raise ValueError("Base branch review requires a branch and its resolved merge base.")
    return {key: raw[key] for key in ("mode", "cwd", "gitRoot", "baseBranch", "mergeBaseSha") if key in raw}


def prepare_code_review(cwd: Path, mode: str, base_branch: str = "", merge_base_sha: str = "", request_message: str = "") -> dict[str, Any]:
    git_root = Path(_git(cwd, "rev-parse", "--show-toplevel"))
    scope: dict[str, Any] = {"mode": mode, "cwd": str(cwd), "gitRoot": str(git_root)}
    if mode == "uncommitted":
        instructions = _UNCOMMITTED
        request = "Please review my uncommitted changes"
        patch = _uncommitted_diff(git_root)
    elif mode == "base-branch":
        if not base_branch.strip():
            raise ValueError("Select a base branch for code review.")
        # A cold review keeps the merge-base pinned by its accepted input.
        if merge_base_sha:
            sha = _git(git_root, "rev-parse", "--verify", "--end-of-options", f"{merge_base_sha}^{{commit}}")
        else:
            base_sha = _git(git_root, "rev-parse", "--verify", "--end-of-options", f"{base_branch}^{{commit}}")
            sha = _git(git_root, "merge-base", "HEAD", base_sha)
        scope.update(baseBranch=base_branch, mergeBaseSha=sha)
        instructions = _BASE_BRANCH.format(baseBranch=base_branch, mergeBaseSha=sha)
        source_branch = _git(git_root, "branch", "--show-current") or "HEAD"
        request = f"Please review changes on {source_branch} against {base_branch}"
        result = run_ui_git_metadata(git_root, "-c", "core.quotePath=false", "diff", "--no-color", "--no-textconv", "--no-ext-diff", sha, "--")
        result.check_returncode()
        patch = result.stdout
    else:
        raise ValueError("Code review target must be uncommitted or base-branch.")
    request = request_message or request
    prompt = "\n".join(["## Code review guidelines:", _GUIDELINES.read_text(encoding="utf-8").strip(), instructions, "## My request:", request])
    return {"scope": scope, "prompt": prompt, "requestMessage": request, "diff": patch}
