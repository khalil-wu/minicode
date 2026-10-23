import { useAppStore } from "../stores";
import type { PermissionMode } from "../stores/types";
import type { ConversationMarkdownOwner } from "../chat/conversationMarkdown";
import type { ClientCommand } from "../protocol/events";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { setConversationArchived } from "../chat/archiveConversation";
import { openNewSideChat } from "../chat/openSideChat";
import { getWebSocket } from "../hooks/useWebSocket";
import { showConfirm, showPrompt } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { translateApp as tr } from "../i18n";
import { copyText } from "./clipboard";
import { workspaceRootsEqual } from "./workspace-path";

export const THREAD_ACTION_EVENTS = {
  renameThread: "rename-thread",
  toggleThreadPin: "toggle-thread-pin",
  archiveThread: "archive-thread",
  copyDeeplink: "copy-deeplink",
  copyWorkingDirectory: "copy-working-directory",
  openSideChat: "open-side-chat",
} as const;
export type ThreadActionId = keyof typeof THREAD_ACTION_EVENTS;
export const THREAD_ACTION_IDS = Object.keys(THREAD_ACTION_EVENTS) as ThreadActionId[];

export type ThreadActionOwner = ConversationMarkdownOwner & {
  archived: boolean;
  pinned: boolean;
  environment?: "local" | "remote" | "ssh";
  conversationType?: "main" | "side_chat";
  isRunning: boolean;
  isWorktree: boolean;
  isGitRepo: boolean;
  permissionMode: PermissionMode;
  forkWorkspaceRoot?: string;
};

/** Capture the canonical selection once; prompts and acknowledgements retain this owner. */
export function captureThreadActionOwner(): ThreadActionOwner | null {
  const state = useAppStore.getState();
  if (state.pendingConversationSwitchId) return null;
  const conversation = state.conversations.find(item => item.id === state.conversationId);
  if (!conversation) return null;
  const root = conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot || state.workingDirectory;
  return {
    id: conversation.id, title: conversation.title, archived: Boolean(conversation.archived), pinned: Boolean(conversation.pinned),
    workspaceRoot: root, messages: state.getVisibleMessages(conversation.id), historyPage: state.conversationHistoryPages[conversation.id],
    sessionId: getWebSocket()?.sessionId ?? "", messageCount: conversation.messageCount,
    forkWorkspaceRoot: conversation.worktreePath || conversation.workspaceRoot || undefined,
    isRunning: Boolean(conversation.sessionStatus === "running" || conversation.sessionStatus === "waiting"
      || state.conversationStreaming[conversation.id] || state.isStreaming),
    isWorktree: Boolean(conversation.gitIsolated || conversation.worktreePath),
    environment: conversation.environment, conversationType: conversation.conversationType,
    isGitRepo: Boolean(conversation.gitBranch || conversation.gitIsolated || conversation.worktreePath
      || state.gitChanges.isGitRepo && workspaceRootsEqual(state.gitChanges.workspaceRoot, root)),
    permissionMode: state.permissionMode,
  };
}

export function canExecuteThreadAction(action: ThreadActionId, owner: ThreadActionOwner): boolean {
  if (action === "copyDeeplink") return true;
  if (action === "copyWorkingDirectory") return Boolean(owner.workspaceRoot);
  if (action === "toggleThreadPin") return !owner.archived && owner.conversationType !== "side_chat";
  return !owner.archived;
}

async function runCommand(command: ClientCommand) {
  const result = command.type === "conversation.archive" || command.type === "conversation.unarchive"
    ? await setConversationArchived(command.conversation_id, command.type === "conversation.archive", command.archive_navigation)
    : await sendClientCommandAwaitResult(command, command.type);
  if (!commandResultSucceeded(result)) pushToast(result.message || tr("聊天操作未完成，请重试。"), "error");
}

/** Shared user-action boundary for header, configured keys and desktop commands. */
export async function executeThreadAction(action: ThreadActionId, owner: ThreadActionOwner): Promise<void> {
  if (!canExecuteThreadAction(action, owner)) return;
  try {
    switch (action) {
      case "renameThread": {
        const title = await showPrompt({ title: tr("重命名聊天"), message: tr("聊天名称"), defaultValue: owner.title,
          confirmLabel: tr("保存"), allowEmpty: true });
        if (title !== null && title !== owner.title) await runCommand({ type: "conversation.rename", conversation_id: owner.id, title });
        return;
      }
      case "toggleThreadPin":
        await runCommand({ type: "conversation.pin.set", conversation_id: owner.id, pinned: !owner.pinned });
        return;
      case "archiveThread":
        if (owner.isRunning && !await showConfirm({ title: tr("停止并归档此聊天？"),
          message: tr("归档会停止所有正在进行的工作。你可以稍后在设置中恢复该聊天。"),
          confirmLabel: tr("停止并归档"), danger: true })) return;
        await runCommand({ type: "conversation.archive", conversation_id: owner.id, archived: true, archive_navigation: "home" });
        return;
      case "copyDeeplink":
        await copyText(`minicode://conversation?conversation_id=${encodeURIComponent(owner.id)}`, tr("复制深度链接"));
        return;
      case "copyWorkingDirectory":
        await copyText(owner.workspaceRoot, tr("工作区路径"));
        return;
      case "openSideChat":
        openNewSideChat({ conversationId: owner.id, workspaceRoot: owner.workspaceRoot, permissionMode: owner.permissionMode });
    }
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error");
  }
}
