import { useAppStore } from "../stores";
import type {
  GitDiffActionEvent,
  GitDiffFilePayload,
  GitDiffStagedEvent,
  GitDiffWorkingTreeEvent,
  ServerEvent,
  TurnDiffUpdatedEvent,
} from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";

const toGitChange = (file: GitDiffFilePayload) => ({
  path: file.path,
  patch: file.patch,
  additions: file.additions,
  deletions: file.deletions,
  isBinary: file.is_binary,
});

const eventTargetsCurrentWorkspace = (event: {
  conversation_id?: string;
  workspace_root?: string;
}): boolean => {
  const state = useAppStore.getState();
  const conversationId = String(event.conversation_id || "").trim();
  const workspaceRoot = normalizeWorkspaceRoot(event.workspace_root);
  return Boolean(
    conversationId
    && conversationId === String(state.conversationId || "").trim()
    && workspaceRoot
    && workspaceRoot === normalizeWorkspaceRoot(state.workingDirectory),
  );
};

export const handleDiffEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "turn.diff.updated": {
      const ev = e as TurnDiffUpdatedEvent;
      const conversationId = String(ev.conversation_id || "").trim();
      const threadId = String(ev.thread_id || "").trim();
      const turnId = String(ev.turn_id || "").trim();
      const messageId = String(ev.message_id || "").trim();
      const messages = conversationId === s.conversationId
        ? s.messages
        : s.conversationMessages[conversationId] ?? [];
      const matchingTurn = messages.some((message) =>
        message.role === "assistant"
        && message.turnId === turnId
        && (!messageId || message.id === messageId)
      );
      if (!conversationId || threadId !== conversationId || !turnId || !matchingTurn) return true;
      const current = s.turnDiffs[conversationId];
      if (
        current?.turnId === turnId
        && current.revision != null
        && ev.revision != null
        && ev.revision < current.revision
      ) return true;
      s.setTurnDiff(conversationId, {
        threadId,
        turnId,
        messageId: messageId || undefined,
        taskId: ev.task_id,
        diff: ev.diff,
        revision: ev.revision,
        toolCallId: ev.tool_call_id,
        updatedAt: Date.now(),
      });
      return true;
    }
    case "diff.git_working_tree": {
      const ev = e as GitDiffWorkingTreeEvent;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      if (s.gitChanges.workingTreeRequestId && ev.request_id !== s.gitChanges.workingTreeRequestId) return true;
      s.setGitChanges({
        workingTree: (ev.files ?? []).map(toGitChange),
        untracked: ev.untracked ?? [],
        loading: false,
      });
      return true;
    }
    case "diff.git_staged": {
      const ev = e as GitDiffStagedEvent;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      if (s.gitChanges.stagedRequestId && ev.request_id !== s.gitChanges.stagedRequestId) return true;
      s.setGitChanges({
        staged: (ev.files ?? []).map(toGitChange),
        loading: false,
      });
      return true;
    }
    case "diff.git_stage_file":
    case "diff.git_unstage_file":
    case "diff.git_stage_all":
    case "diff.git_unstage_all":
    case "diff.git_revert_file": {
      const ev = e as GitDiffActionEvent;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      if (ev.ok !== false) {
        s.requestGitChanges();
      } else {
        s.setGitChangesLoading(false);
        pushToast(ev.message || "Git 操作失败，工作区未发生更改。", "error", 3500);
      }
      return true;
    }
    default:
      return false;
  }
};
