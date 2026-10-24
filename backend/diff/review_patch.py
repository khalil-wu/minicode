"""The full patch behind the selected Changes source, independent of UI filters."""

from backend.diff.comparison import branch_comparison, commit_comparison
from backend.diff.git_integration import (
    get_comparison_diff,
    get_staged_diff,
    get_uncommitted_diff,
    get_untracked_file_diffs,
    get_untracked_files,
    get_working_tree_diff,
    _read_git,
)


async def review_patch(workspace_root: str, source: str, *, service=None,
                       base_branch: str | None = None, commit_sha: str | None = None) -> str:
    if source == "branch":
        comparison = await branch_comparison(workspace_root, service, base_branch=base_branch)
        diff = await get_comparison_diff(workspace_root, [comparison["merge_base_sha"]],
                                        service=service, worktree=True, binary=True, relative=False)
    elif source == "commit":
        comparison = await commit_comparison(workspace_root, service, commit_sha)
        diff = await get_comparison_diff(workspace_root,
            [comparison["parent_sha"], comparison["commit_sha"]], service=service, binary=True, relative=False)
    else:
        read = {"staged": get_staged_diff, "unstaged": get_working_tree_diff,
                "uncommitted": get_uncommitted_diff}[source]
        diff = await read(workspace_root, service=service, binary=True, relative=False)
    files = diff.files
    if source not in {"staged", "commit"}:
        untracked = await get_untracked_files(workspace_root, service=service)
        prefix = (await _read_git(workspace_root, "rev-parse", "--show-prefix", service=service)).strip()
        if source in {"branch", "uncommitted"}:
            paths = {prefix + path for path in untracked}
            files = [file for file in files if file.path not in paths or file.status == "U"]
        files = [*files, *await get_untracked_file_diffs(workspace_root, untracked, service=service, binary=True, path_prefix=prefix)]
    return "".join(file.patch if file.patch.endswith("\n") else file.patch + "\n" for file in files)
