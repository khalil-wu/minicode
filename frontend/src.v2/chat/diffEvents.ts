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
import { parseUnifiedDiffLines, unifiedDiffFilePaths } from "../lib/unified-diff";
import type { GitDiffSnapshot } from "../stores/types";

const toGitChange = (file: GitDiffFilePayload) => {
  const { oldPath, newPath } = unifiedDiffFilePaths(parseUnifiedDiffLines(file.patch));
  return {
    path: file.path,
    ...(file.old_path ? { oldPath: file.old_path }
      : oldPath && newPath && oldPath !== newPath && oldPath !== "/dev/null" && newPath !== "/dev/null" ? { oldPath } : {}),
    patch: file.patch,
    additions: file.additions,
    deletions: file.deletions,
    isBinary: file.is_binary,
    status: file.status,
    oldMode: file.old_mode,
    newMode: file.new_mode,
    oldOid: file.old_oid,
    newOid: file.new_oid,
    revision: file.revision,
  };
};

const snapshotOf = (event: GitDiffWorkingTreeEvent | GitDiffStagedEvent): GitDiffSnapshot | undefined =>
  event.snapshot_generation ? {
    conversationId: event.conversation_id,
    workspaceRoot: event.workspace_root,
    environmentId: event.environment_id,
    cwdUri: event.cwd_uri,
    source: event.source ?? (event.type === "diff.git_staged" ? "staged" : "unstaged"),
    generation: event.snapshot_generation,
    hideWhitespace: event.hide_whitespace === true,
  } : undefined;

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

const comparisonFromEvent = (event: GitDiffWorkingTreeEvent | GitDiffStagedEvent) => {
  const current = useAppStore.getState().gitChanges.comparison;
  if (!current?.requestId || event.request_id !== current.requestId) return undefined;
  const source = event.source ?? (event.type === "diff.git_staged" ? "staged" : "unstaged");
  if (source !== current.source) return undefined;
  const untracked = event.type === "diff.git_working_tree" ? event.untracked_files ?? [] : [];
  const files = new Map((event.files ?? []).map(file => [file.path, toGitChange(file)]));
  untracked.forEach(file => files.set(file.path, toGitChange(file)));
  return { ...current, files: [...files.values()], loading: false, error: undefined, snapshot: snapshotOf(event),
    status: event.type === "diff.git_working_tree" ? event.status : "success" as const,
    reason: event.type === "diff.git_working_tree" ? event.reason : null,
    comparison: event.type === "diff.git_working_tree" ? event.comparison : undefined };
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
        : s.sideChats[conversationId]?.messages ?? s.conversationMessages[conversationId] ?? [];
      const matchingTurn = messages.some((message) =>
        message.role === "assistant"
        && message.turnId === turnId
        && (!messageId || message.id === messageId)
      );
      if (!conversationId || threadId !== conversationId || !turnId || !matchingTurn) return true;
      const current = s.turnDiffs[conversationId];
      if (current && current.turnId !== turnId) {
        const incomingOwnerIndex = messages.findIndex((message) => message.role === "assistant" && message.turnId === turnId);
        const currentOwnerIndex = messages.findIndex((message) => message.role === "assistant" && message.turnId === current.turnId);
        if (currentOwnerIndex > incomingOwnerIndex) return true;
      }
      if (
        current?.turnId === turnId
        && current.revision != null
        && ev.revision != null
        && ev.revision <= current.revision
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
      if (s.pendingConversationSwitchId || s.pendingConversationCreateId) return true;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      const comparison = comparisonFromEvent(ev);
      const statusRead = (ev.source === undefined || ev.source === "unstaged")
        && Boolean(s.gitChanges.workingTreeRequestId && ev.request_id === s.gitChanges.workingTreeRequestId);
      if (!statusRead && !comparison) return true;
      s.setGitChanges({
        isGitRepo: ev.is_git_repo,
        ...comparison ? { comparison } : {},
        ...statusRead ? {
        ...(ev.is_git_repo === false ? { staged: [] } : {}),
        workingTree: (ev.files ?? []).map(toGitChange),
        untracked: ev.untracked ?? [],
        untrackedFiles: (ev.untracked_files ?? []).map(toGitChange),
        workingTreeSnapshot: snapshotOf(ev),
        loading: false,
        } : {},
      });
      return true;
    }
    case "diff.git_staged": {
      const ev = e as GitDiffStagedEvent;
      if (s.pendingConversationSwitchId || s.pendingConversationCreateId) return true;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      const comparison = comparisonFromEvent(ev);
      const statusRead = Boolean(s.gitChanges.stagedRequestId && ev.request_id === s.gitChanges.stagedRequestId);
      if (!statusRead && !comparison) return true;
      s.setGitChanges({
        isGitRepo: ev.is_git_repo,
        ...comparison ? { comparison } : {},
        ...statusRead ? {
        ...(ev.is_git_repo === false ? { workingTree: [], untracked: [] } : {}),
        staged: (ev.files ?? []).map(toGitChange),
        stagedSnapshot: snapshotOf(ev),
        loading: false,
        } : {},
      });
      return true;
    }
    case "diff.git_stage_file":
    case "diff.git_unstage_file":
    case "diff.git_stage_all":
    case "diff.git_unstage_all":
    case "diff.git_revert_file":
    case "diff.git_revert_all": {
      const ev = e as GitDiffActionEvent;
      if (!eventTargetsCurrentWorkspace(ev)) return true;
      if (ev.ok !== false || ev.status === "partial-success") {
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
