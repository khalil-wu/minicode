"""Read-only Files comparisons on the workspace request's captured owner."""
from __future__ import annotations

from backend.diff.comparison import ComparisonUnavailable, branch_comparison, commit_comparison, review_options
from backend.diff.git_integration import (
    StructuredDiff, get_comparison_diff, get_staged_diff, get_uncommitted_diff,
    get_untracked_file_diffs, get_untracked_files, get_working_tree_diff, is_git_worktree,
)
from backend.services.diff_service import diff_file_payload


async def file_review_options(root, service=None, *, base_branch=None):
    if not await is_git_worktree(str(root), service=service):
        return {"is_git_repo": False, "current_branch": None, "default_base_branch": None,
            "branches": [], "merge_base_sha": None, "commits": [], "comparison_status": "success", "reason": None}
    return {"is_git_repo": True, **await review_options(str(root), service, base_branch=base_branch)}


async def file_review_diff(root, service=None, *, source, base_branch=None, commit_sha=None, hide_whitespace=False):
    is_repo = await is_git_worktree(str(root), service=service)
    result, comparison, status, reason = StructuredDiff(), {}, "success", None
    if is_repo:
        try:
            if source == "branch":
                comparison = await branch_comparison(str(root), service, base_branch=base_branch)
                result = await get_comparison_diff(str(root), [comparison["merge_base_sha"]],
                    service=service, hide_whitespace=hide_whitespace, worktree=True, binary=True)
            elif source == "commit":
                comparison = await commit_comparison(str(root), service, commit_sha)
                result = await get_comparison_diff(str(root), [comparison["parent_sha"], comparison["commit_sha"]],
                    service=service, hide_whitespace=hide_whitespace, binary=True)
            else:
                read_diff = {"uncommitted": get_uncommitted_diff, "unstaged": get_working_tree_diff, "staged": get_staged_diff}[source]
                result = await read_diff(str(root), service=service, hide_whitespace=hide_whitespace, binary=True)
        except ComparisonUnavailable as unavailable:
            status, reason = "expected", unavailable.reason
        if source not in {"staged", "commit"} and status == "success":
            untracked = await get_untracked_files(str(root), service=service)
            if source in {"uncommitted", "branch"}:
                result.files = [file for file in result.files if file.path not in untracked or file.status == "U"]
            result.files.extend(await get_untracked_file_diffs(str(root), untracked, service=service, binary=True))
        if service is not None:
            result.files = [file for file in result.files if service.scope.resolve_access(service.cwd.joinpath(file.path).uri).can_read]
    return {"is_git_repo": is_repo, "source": source, "status": status, "reason": reason, "comparison": comparison,
        "files": [diff_file_payload(file) for file in result.files], "diff": "".join(file.patch for file in result.files)}
