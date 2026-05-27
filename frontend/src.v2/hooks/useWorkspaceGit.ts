import { useEffect } from "react";
import { fetchWorkspaceGitWorktree } from "../protocol/workspace";
import { useAppStore } from "../stores";

export const useWorkspaceGit = () => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setWorkspaceGit = useAppStore((s) => s.setWorkspaceGit);

  useEffect(() => {
    let cancelled = false;
    if (!workingDirectory) {
      setWorkspaceGit(null);
      return;
    }
    fetchWorkspaceGitWorktree().then((result) => {
      if (cancelled) return;
      if (!result) {
        setWorkspaceGit(null);
        return;
      }
      setWorkspaceGit({
        branch: result.current_branch ?? "",
        isWorktree: Boolean(result.is_worktree),
        currentPath: result.current_path,
        mainRepoPath: result.main_repo_path,
        worktreeCount: result.worktree_count,
        isolatedCount: result.worktrees?.filter((item) => item.is_isolated).length ?? 0,
        error: result.error,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [setWorkspaceGit, workingDirectory]);
};
