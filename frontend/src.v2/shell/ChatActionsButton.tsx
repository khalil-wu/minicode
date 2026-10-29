import { useI18n } from "../i18n";
import { useCallback, useEffect, useMemo, useState } from "react";
import { MoreHorizontal } from "../lib/icons";
import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import type { ClientCommand } from "../protocol/events";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { copyText } from "../lib/clipboard";
import { canOpenThreadWindow, openThreadWindow } from "./threadWindowActions";
import { useWorkspaceOpenMenu } from "./useWorkspaceOpenMenu";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { captureThreadActionOwner, executeThreadAction, type ThreadActionId, type ThreadActionOwner } from "../lib/thread-actions";
import { loadThreadAutomations, openThreadAutomationEditor } from "../lib/automations-navigation";
import { heartbeatRendererFactsForConversation, projectHeartbeatRendererEligibility } from "../chat/heartbeatRendererState";
import type { ScheduledTaskEntry } from "../stores/types";
import "./ShellMenus.css";

type MenuOwner = ThreadActionOwner;
const sourceIcon = (name: "copy" | "fork" | "local" | "worktree" | "new-chat" | "rename" | "archive" | "new-window" | "pin" | "unpin" | "scheduled-task") => <span aria-hidden="true" style={{
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
  const shortcutBindings = useAppStore((state) => state.shortcutBindings);
  const conversationRunning = useAppStore(state => Boolean(conversation?.sessionStatus === "running" || conversation?.sessionStatus === "waiting"
    || state.conversationStreaming[conversationId || ""] || state.isStreaming));
  const [menu, setMenu] = useState<{ owner: MenuOwner; position: { x: number; y: number } } | null>(null);
  const [busy, setBusy] = useState(false);
  const [automationInventory, setAutomationInventory] = useState<{ ownerId: string; state: "loading" | "ready" | "error"; tasks: ScheduledTaskEntry[]; error?: string } | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  useEffect(closeMenu, [conversationId, pendingOwner, workspaceRoot, appMode, closeMenu]);

  const runCommand = async (command: ClientCommand) => {
    setBusy(true);
    try {
      const result = await sendClientCommandAwaitResult(command, command.type);
      if (!commandResultSucceeded(result)) pushToast(result.message || tr("聊天操作未完成，请重试。"), "error");
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally { setBusy(false); }
  };
  const runThreadAction = async (action: ThreadActionId, owner: MenuOwner) => {
    if (action === "copyDeeplink" || action === "copyWorkingDirectory" || action === "openSideChat") {
      await executeThreadAction(action, owner);
      return;
    }
    setBusy(true);
    try { await executeThreadAction(action, owner); }
    finally { setBusy(false); }
  };
  const copyMarkdown = async (owner: MenuOwner) => {
    const captured = { ...owner };
    try {
      const { loadConversationMarkdown } = await import("../chat/conversationMarkdown");
      const markdown = await loadConversationMarkdown(captured);
      if (markdown !== null) await copyText(markdown, tr("复制为 Markdown"));
    }
    catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
  };
  const fork = (owner: MenuOwner, createBranch: boolean) => runCommand({
    type: "context.fork", conversation_id: owner.id, ...(owner.forkWorkspaceRoot ? { workspace_root: owner.forkWorkspaceRoot } : {}),
    message_id: owner.messages.at(-1)!.id, create_branch: createBranch, activate: true,
  });
  const owner = useMemo(() => menu && conversation?.id === menu.owner.id ? {
    ...menu.owner, title: conversation.title, pinned: Boolean(conversation.pinned), archived: Boolean(conversation.archived), isRunning: conversationRunning,
  } : undefined, [menu, conversation, conversationRunning]);
  const automationEligibility = useAppStore(state => {
    if (!owner) return null;
    const facts = heartbeatRendererFactsForConversation(state, owner.id, !state.isConnected, true);
    if (!facts) return "missing_conversation";
    facts.supportedHost = !owner.environment || owner.environment === "local";
    return projectHeartbeatRendererEligibility(facts).reason ?? "eligible";
  });
  const canLoadAutomations = Boolean(owner && !owner.archived && owner.workspaceRoot && (!owner.environment || owner.environment === "local"));
  useEffect(() => {
    const lookupOwner = menu?.owner;
    if (!lookupOwner || !canLoadAutomations) { setAutomationInventory(null); return; }
    let current = true;
    const request = loadThreadAutomations(lookupOwner);
    setAutomationInventory({ ownerId: lookupOwner.id, state: "loading", tasks: [] });
    void request.then(tasks => { if (current) setAutomationInventory({ ownerId: lookupOwner.id, state: "ready", tasks }); })
      .catch(error => { if (current) setAutomationInventory({ ownerId: lookupOwner.id, state: "error", tasks: [], error: error instanceof Error ? error.message : String(error) }); });
    return () => { current = false; };
  }, [menu, canLoadAutomations]);
  const inventory = automationInventory?.ownerId === owner?.id ? automationInventory : null;
  const existingAutomation = inventory?.tasks.find(task => task.conversation_id === owner?.id);
  const showAutomation = canLoadAutomations && (existingAutomation || automationEligibility === "eligible" || automationEligibility === "turn_in_progress");
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
        const requestedOwner = captureThreadActionOwner();
        if (!requestedOwner) return;
        const rect = event.currentTarget.getBoundingClientRect();
        setMenu({ position: { x: rect.right - 228, y: rect.bottom + 6 }, owner: requestedOwner });
      }}><MoreHorizontal size={17} /></button>
    {menu && owner && owner.id === conversationId && !pendingOwner && <ContextMenu className="mc-chat-actions-menu" position={menu.position} onClose={closeMenu} items={[
      ...(!owner.archived ? [{ label: tr("重命名"), icon: sourceIcon("rename"), shortcut: formatShortcut(shortcutBindings.renameThread), onClick: () => void runThreadAction("renameThread", owner) },
        ...(owner.conversationType !== "side_chat"
          ? [{ label: tr(owner.pinned ? "取消固定" : "固定"), icon: sourceIcon(owner.pinned ? "unpin" : "pin"), shortcut: formatShortcut(shortcutBindings.toggleThreadPin), onClick: () => void runThreadAction("toggleThreadPin", owner) }] : []),
        { label: "", separator: true },
        { label: tr("菜单侧边聊天"), icon: sourceIcon("new-chat"), shortcut: formatShortcut(shortcutBindings.openSideChat), onClick: () => void runThreadAction("openSideChat", owner) },
        ...(owner.messages.length ? [{ label: appMode === "cowork" ? tr("继续") : tr("分叉"), icon: sourceIcon("fork"), submenu: [
          { label: appMode === "cowork" ? (owner.isWorktree ? tr("在同一工作树中继续") : tr("在新聊天中继续")) : (owner.isWorktree ? tr("在同一工作树中创建聊天分支") : tr("创建聊天分支")),
            icon: sourceIcon(owner.isWorktree ? "worktree" : "local"), onClick: () => void fork(owner, false) },
          ...(owner.isGitRepo ? [{ label: appMode === "cowork" ? tr("在新工作树中继续") : tr("在新工作树中创建聊天分支"), icon: sourceIcon("worktree"), onClick: () => void fork(owner, true) }] : []),
        ] }] : []),
        ...(showAutomation ? [{ label: inventory?.state === "error" ? tr("定时任务不可用") : inventory?.state !== "ready"
          ? tr("正在加载定时任务…") : existingAutomation ? tr("编辑定时任务…") : tr("添加定时任务…"),
          icon: sourceIcon("scheduled-task"), disabled: inventory?.state !== "ready" || (!existingAutomation && automationEligibility !== "eligible"),
          tooltip: inventory?.error, onClick: () => openThreadAutomationEditor(owner, existingAutomation) }] : []),
      ] : []),
      ...(!owner.archived ? [{ label: "", separator: true }] : []),
      { label: tr("复制"), icon: sourceIcon("copy"), submenu: [
        { label: tr("复制深度链接"), icon: sourceIcon("copy"), shortcut: formatShortcut(shortcutBindings.copyDeeplink), onClick: () => void runThreadAction("copyDeeplink", owner) },
        { label: tr("复制为 Markdown"), icon: sourceIcon("copy"), onClick: () => void copyMarkdown(owner) },
        { label: tr("复制工作目录"), icon: sourceIcon("copy"), shortcut: formatShortcut(shortcutBindings.copyWorkingDirectory), disabled: !owner.workspaceRoot, onClick: () => void runThreadAction("copyWorkingDirectory", owner) },
      ] },
      ...(windowItems.length ? [{ label: "", separator: true }, ...windowItems] : []),
      ...(!owner.archived ? [{ label: "", separator: true }, { label: tr("归档"), icon: sourceIcon("archive"), shortcut: formatShortcut(shortcutBindings.archiveThread), onClick: () => void runThreadAction("archiveThread", owner) }] : []),
    ]} />}
  </>;
}
