import { useCallback, useEffect, useState } from "react";
import { Archive, Copy, Download, Files, Folder, MessageSquarePlus, MoreHorizontal, Pencil, RotateCcw } from "../lib/icons";
import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import type { ClientCommand } from "../protocol/events";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { showPrompt } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { copyText } from "../lib/clipboard";
import { setConversationArchived } from "../chat/archiveConversation";

type MenuOwner = { id: string; title: string; archived: boolean; workspaceRoot: string; transcript: string };

/** Header actions capture the selected conversation, just like sidebar actions. */
export function ChatActionsButton() {
  const conversationId = useAppStore((state) => state.conversationId);
  const conversation = useAppStore((state) => state.conversations.find((item) => item.id === state.conversationId));
  const pendingOwner = useAppStore((state) => state.pendingConversationSwitchId);
  const workspaceRoot = useAppStore((state) => state.workingDirectory);
  const sideChatOpen = useAppStore((state) => state.sideChatOpen);
  const [menu, setMenu] = useState<{ owner: MenuOwner; position: { x: number; y: number } } | null>(null);
  const [busy, setBusy] = useState(false);
  const closeMenu = useCallback(() => setMenu(null), []);
  useEffect(closeMenu, [conversationId, pendingOwner, workspaceRoot, closeMenu]);

  const runCommand = async (command: ClientCommand) => {
    setBusy(true);
    try {
      const result = command.type === "conversation.archive" || command.type === "conversation.unarchive"
        ? await setConversationArchived(command.conversation_id, command.type === "conversation.archive")
        : await sendClientCommandAwaitResult(command, command.type);
      if (!commandResultSucceeded(result)) pushToast(result.message || "聊天操作未完成，请重试。", "error");
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally { setBusy(false); }
  };
  const rename = async (owner: MenuOwner) => {
    const value = await showPrompt({ title: "重命名聊天", message: "聊天名称", defaultValue: owner.title, confirmLabel: "保存" });
    const title = value?.trim();
    if (title && title !== owner.title) await runCommand({ type: "conversation.rename", conversation_id: owner.id, title });
  };
  const owner = menu?.owner;

  return <>
    <button type="button" className="btn-ghost mc-icon-button" aria-label="聊天操作" title="聊天操作"
      aria-haspopup="menu" aria-expanded={Boolean(menu)} disabled={!conversation || Boolean(pendingOwner) || busy}
      onClick={(event) => {
        if (menu) { closeMenu(); return; }
        const rect = event.currentTarget.getBoundingClientRect();
        const state = useAppStore.getState();
        setMenu({ position: { x: rect.right - 228, y: rect.bottom + 6 }, owner: {
          id: conversation!.id, title: conversation!.title, archived: Boolean(conversation!.archived),
          workspaceRoot: conversation!.worktreePath || conversation!.workspaceRoot || workspaceRoot,
          transcript: state.messages.map((message) => `${message.role === "user" ? "用户" : "助手"}\n${message.content}`).join("\n\n"),
        } });
      }}><MoreHorizontal size={17} /></button>
    {menu && owner && owner.id === conversationId && !pendingOwner && <ContextMenu position={menu.position} onClose={closeMenu} items={[
      { label: "重命名", icon: <Pencil size={15} />, onClick: () => void rename(owner) },
      { label: sideChatOpen ? "打开侧边聊天" : "新建侧边聊天", icon: <MessageSquarePlus size={15} />,
        onClick: () => useAppStore.getState().setRightStackTab("sidechat") },
      { label: "克隆会话", icon: <Files size={15} />, onClick: () => void runCommand({ type: "conversation.clone", conversation_id: owner.id, activate: false }) },
      { label: "", separator: true },
      { label: "复制对话文本", icon: <Copy size={15} />, disabled: !owner.transcript, onClick: () => void copyText(owner.transcript, "对话文本") },
      { label: "导出会话树", icon: <Download size={15} />, onClick: () => void runCommand({ type: "conversation.export", conversation_id: owner.id, include_descendants: true }) },
      ...(owner.workspaceRoot ? [{ label: "复制工作区路径", icon: <Folder size={15} />, onClick: () => void copyText(owner.workspaceRoot, "工作区路径") }] : []),
      { label: "", separator: true },
      { label: owner.archived ? "取消归档" : "归档", icon: owner.archived ? <RotateCcw size={15} /> : <Archive size={15} />,
        onClick: () => void runCommand({ type: owner.archived ? "conversation.unarchive" : "conversation.archive", conversation_id: owner.id, archived: !owner.archived }) },
    ]} />}
  </>;
}
