import { useAppStore } from "../stores";
import type {
  GitDiffActionEvent,
  GitDiffFilePayload,
  GitDiffStagedEvent,
  GitDiffWorkingTreeEvent,
  ServerEvent,
} from "../protocol/events";

const toGitChange = (file: GitDiffFilePayload) => ({
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
      const ev = e as GitDiffWorkingTreeEvent;
      s.setGitChanges({
        workingTree: (ev.files ?? []).map(toGitChange),
        untracked: ev.untracked ?? [],
      });
      return true;
    }
    case "diff.git_staged": {
      const ev = e as GitDiffStagedEvent;
      s.setGitChanges({
        staged: (ev.files ?? []).map(toGitChange),
      });
      return true;
    }
    case "diff.git_stage_file":
    case "diff.git_unstage_file":
    case "diff.git_stage_all":
    case "diff.git_unstage_all":
    case "diff.git_revert_file": {
      const ev = e as GitDiffActionEvent;
      if (ev.ok !== false) {
        s.requestGitChanges();
      }
      return true;
    }
    default:
      return false;
  }
};
