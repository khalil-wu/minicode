import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

type GitDiffFile = {
  path: string;
  patch: string;
  additions: number;
  deletions: number;
  is_binary?: boolean;
};

const toGitChange = (file: GitDiffFile) => ({
  path: file.path,
  patch: file.patch,
  additions: file.additions,
  deletions: file.deletions,
  isBinary: file.is_binary,
});

export const handleDiffEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "diff.git_working_tree": {
      const ev = e as unknown as {
        files?: GitDiffFile[];
        untracked?: string[];
      };
      s.setGitChanges({
        workingTree: (ev.files ?? []).map(toGitChange),
        untracked: ev.untracked ?? [],
      });
      return true;
    }
    case "diff.git_staged": {
      const ev = e as unknown as {
        files?: GitDiffFile[];
      };
      s.setGitChanges({
        staged: (ev.files ?? []).map(toGitChange),
      });
      return true;
    }
    default:
      return false;
  }
};
