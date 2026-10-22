import { useI18n } from "../i18n";
import { useCallback, useEffect, useState } from "react";
import { MoreHorizontal } from "../lib/icons";
import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import type { ClientCommand } from "../protocol/events";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { showConfirm, showPrompt } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { copyText } from "../lib/clipboard";
import { setConversationArchived } from "../chat/archiveConversation";
import { canOpenThreadWindow, openThreadWindow } from "./threadWindowActions";
import { useWorkspaceOpenMenu } from "./useWorkspaceOpenMenu";
import { getWebSocket } from "../hooks/useWebSocket";
import { loadConversationMarkdown, type ConversationMarkdownOwner } from "../chat/conversationMarkdown";
import { openNewSideChat } from "../chat/openSideChat";
import { workspaceRootsEqual } from "../lib/workspace-path";
import type { PermissionMode } from "../stores/types";
import "./ShellMenus.css";

type MenuOwner = ConversationMarkdownOwner & { archived: boolean; pinned: boolean; environment?: "local" | "remote" | "ssh"; conversationType?: "main" | "side_chat"; isRunning: boolean; isWorktree: boolean; isGitRepo: boolean; permissionMode: PermissionMode; forkWorkspaceRoot?: string };
const sourceIcon = (name: "copy" | "fork" | "local" | "worktree" | "new-chat" | "rename" | "archive" | "new-window" | "pin" | "unpin") => <span aria-hidden="true" style={{
  width: 16, height: 16, background: "currentColor", mask: `url(${import.meta.env.BASE_URL}apps/thread-${name}.svg) center / contain no-repeat`,
}} />;

/** Header actions capture the selected conversation, just like sidebar actions. */
export function ChatActionsButton() {
  const { t: tr } = useI18n();
  const conversationId = useAppStore((state) => state.conversationId);
  const conversation = useAppStore((state) => state.conversations.find((item) => item.id === state.conversationId));
  const pendingOwner = useAppStore((state) => state.pendingConversationSwitchId);
  const workspaceRoot = useAppStore((state) => state.workingDirectory);
  const appMode = useAppStore((state) => state.appMode);
  const [menu, setMenu] = useState<{ owner: MenuOwner; position: { x: number; y: number } } | null>(null);
  const [busy, setBusy] = useState(false);
  const closeMenu = useCallback(() => setMenu(null), []);
  useEffect(closeMenu, [conversationId, pendingOwner, workspaceRoot, appMode, closeMenu]);

  const runCommand = async (command: ClientCommand) => {
    setBusy(true);
    try {
      const result = command.type === "conversation.archive" || command.type === "conversation.unarchive"
        ? await setConversationArchived(command.conversation_id, command.type === "conversation.archive", command.archive_navigation)
        : await sendClientCommandAwaitResult(command, command.type);
      if (!commandResultSucceeded(result)) pushToast(result.message || tr("聊天操作未完成，请重试。"), "error");
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally { setBusy(false); }
  };
  const rename = async (owner: MenuOwner) => {
    const title = await showPrompt({ title: tr("重命名聊天"), message: tr("聊天名称"), defaultValue: owner.title, confirmLabel: tr("保存"), allowEmpty: true });
    if (title !== null && title !== owner.title) await runCommand({ type: "conversation.rename", conversation_id: owner.id, title });
  };
  const copyMarkdown = async (owner: MenuOwner) => {
    try { const markdown = await loadConversationMarkdown(owner); if (markdown !== null) await copyText(markdown, tr("复制为 Markdown")); }
    catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
  };
  const fork = (owner: MenuOwner, createBranch: boolean) => runCommand({
    type: "context.fork", conversation_id: owner.id, ...(owner.forkWorkspaceRoot ? { workspace_root: owner.forkWorkspaceRoot } : {}),
    message_id: owner.messages.at(-1)!.id, create_branch: createBranch, activate: true,
  });
  const archive = async (owner: MenuOwner) => {
    if (owner.isRunning && !await showConfirm({ title: tr("停止并归档此聊天？"),
      message: tr("归档会停止所有正在进行的工作。你可以稍后在设置中恢复该聊天。"), confirmLabel: tr("停止并归档"), danger: true })) return;
    await runCommand({ type: "conversation.archive", conversation_id: owner.id, archived: true, archive_navigation: "home" });
  };
  const owner = menu?.owner;
  const workspaceItems = useWorkspaceOpenMenu(owner && (!owner.environment || owner.environment === "local") ? owner.workspaceRoot : undefined, Boolean(menu));
  const windowItems = [
    ...(owner && canOpenThreadWindow(owner) ? [{ label: tr("在新窗口中打开"), icon: sourceIcon("new-window"), onClick: () => void openThreadWindow(owner.id) }] : []),
    ...workspaceItems,
  ];

  return <>
    <button type="button" className="btn-ghost mc-icon-button" aria-label={tr("聊天操作")} title={tr("聊天操作")}
      aria-haspopup="menu" aria-expanded={Boolean(menu)} disabled={!conversation || Boolean(pendingOwner) || busy}
      onClick={(event) => {
        if (menu) { closeMenu(); return; }
        const rect = event.currentTarget.getBoundingClientRect();
        const state = useAppStore.getState();
        const root = conversation!.executionCwd || conversation!.worktreePath || conversation!.workspaceRoot || workspaceRoot;
        const requestedMenu = { position: { x: rect.right - 228, y: rect.bottom + 6 }, owner: {
          id: conversation!.id, title: conversation!.title, archived: Boolean(conversation!.archived), pinned: Boolean(conversation!.pinned),
          workspaceRoot: root, messages: state.getVisibleMessages(conversation!.id), historyPage: state.conversationHistoryPages[conversation!.id], sessionId: getWebSocket()?.sessionId ?? "",
          messageCount: conversation!.messageCount, forkWorkspaceRoot: conversation!.worktreePath || conversation!.workspaceRoot || undefined,
          isRunning: Boolean(conversation!.sessionStatus === "running" || conversation!.sessionStatus === "waiting" || state.conversationStreaming[conversation!.id] || state.isStreaming),
          isWorktree: Boolean(conversation!.gitIsolated || conversation!.worktreePath),
          environment: conversation!.environment, conversationType: conversation!.conversationType,
          isGitRepo: Boolean(conversation!.gitBranch || conversation!.gitIsolated || conversation!.worktreePath || state.gitChanges.isGitRepo && workspaceRootsEqual(state.gitChanges.workspaceRoot, root)),
          permissionMode: state.permissionMode,
        } };
        setMenu(requestedMenu);
      }}><MoreHorizontal size={17} /></button>
    {menu && owner && owner.id === conversationId && !pendingOwner && <ContextMenu className="mc-chat-actions-menu" position={menu.position} onClose={closeMenu} items={[
      ...(!owner.archived ? [{ label: tr("重命名"), icon: sourceIcon("rename"), onClick: () => void rename(owner) },
        ...(owner.conversationType !== "side_chat"
          ? [{ label: tr(owner.pinned ? "取消固定" : "固定"), icon: sourceIcon(owner.pinned ? "unpin" : "pin"), onClick: () => void runCommand({ type: "conversation.pin.set", conversation_id: owner.id, pinned: !owner.pinned }) }] : []),
        { label: "", separator: true },
        { label: tr("菜单侧边聊天"), icon: sourceIcon("new-chat"), onClick: () => { openNewSideChat({ conversationId: owner.id, workspaceRoot: owner.workspaceRoot, permissionMode: owner.permissionMode }); } },
        ...(owner.messages.length ? [{ label: appMode === "cowork" ? tr("继续") : tr("分叉"), icon: sourceIcon("fork"), submenu: [
          { label: appMode === "cowork" ? (owner.isWorktree ? tr("在同一工作树中继续") : tr("在新聊天中继续")) : (owner.isWorktree ? tr("在同一工作树中创建聊天分支") : tr("创建聊天分支")),
            icon: sourceIcon(owner.isWorktree ? "worktree" : "local"), onClick: () => void fork(owner, false) },
          ...(owner.isGitRepo ? [{ label: appMode === "cowork" ? tr("在新工作树中继续") : tr("在新工作树中创建聊天分支"), icon: sourceIcon("worktree"), onClick: () => void fork(owner, true) }] : []),
        ] }] : []),
      ] : []),
      ...(!owner.archived ? [{ label: "", separator: true }] : []),
      { label: tr("复制"), icon: sourceIcon("copy"), submenu: [
        { label: tr("复制深度链接"), icon: sourceIcon("copy"), onClick: () => void copyText(`minicode://conversation?conversation_id=${encodeURIComponent(owner.id)}`, tr("复制深度链接")) },
        { label: tr("复制为 Markdown"), icon: sourceIcon("copy"), onClick: () => void copyMarkdown(owner) },
        { label: tr("复制工作目录"), icon: sourceIcon("copy"), disabled: !owner.workspaceRoot, onClick: () => void copyText(owner.workspaceRoot, tr("工作区路径")) },
      ] },
      ...(windowItems.length ? [{ label: "", separator: true }, ...windowItems] : []),
      ...(!owner.archived ? [{ label: "", separator: true }, { label: tr("归档"), icon: sourceIcon("archive"), onClick: () => void archive(owner) }] : []),
    ]} />}
  </>;
}
