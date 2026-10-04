import { useEffect } from "react";
import { fetchWorkspaceGitWorktree } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { workspaceRootsEqual } from "../lib/workspace-path";

export const useWorkspaceGit = () => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setWorkspaceGit = useAppStore((s) => s.setWorkspaceGit);

  useEffect(() => {
    let cancelled = false;
    setWorkspaceGit(null);
    if (!workingDirectory) {
      return;
    }
    const isCurrentWorkspace = () => !cancelled && workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory);
    fetchWorkspaceGitWorktree(workingDirectory).then((result) => {
      if (!isCurrentWorkspace()) return;
      setWorkspaceGit({
        branch: result.current_branch ?? "",
        isWorktree: Boolean(result.is_worktree),
        currentPath: result.current_path,
        mainRepoPath: result.main_repo_path,
        worktreeCount: result.worktree_count,
        isolatedCount: result.worktrees?.filter((item) => item.is_isolated).length ?? 0,
        error: result.error,
      });
    }).catch((error: unknown) => {
      if (!isCurrentWorkspace()) return;
      setWorkspaceGit({
        branch: "",
        isWorktree: false,
        currentPath: workingDirectory,
        error: error instanceof Error ? error.message : String(error),
      });
    });
    return () => {
      cancelled = true;
    };
  }, [setWorkspaceGit, workingDirectory]);
};
