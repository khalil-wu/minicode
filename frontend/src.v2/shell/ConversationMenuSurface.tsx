import { useEffect, useState } from "react";
import { RotateCcw } from "lucide-react";
import { useI18n } from "../i18n";
import { ContextMenu } from "../components/ContextMenu";
import { canOpenThreadWindow, openThreadWindow } from "./threadWindowActions";
import { useWorkspaceOpenMenu } from "./useWorkspaceOpenMenu";
import { desktop, type DesktopPetState } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "../stores";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { threadMenuIcon } from "./sidebarComponents";
import type { ConversationMenuProps } from "./ConversationMenu";

export const ConversationMenuSurface = ({
  anchor, position, menuId, conversationId, workspaceRoot, conversationType, environment, archived, isIsolated, canCopy, canHandoff,
  pinned = false, hasUnreadTurn = false, currentProject = "", projectOptions = [], onPin, onReadState, onProjectChange,
  sectionOptions = [], currentSection, onSectionChange, onNewSection,
  onRename, onCopy, onCopyLink, onCopyMarkdown, onHandoff, onArchive, onClose,
}: ConversationMenuProps) => {
  const { t: tr } = useI18n();
  const shortcutBindings = useAppStore(state => state.shortcutBindings);
  const [miniMuted, setMiniMuted] = useState(false);
  useEffect(() => {
    const pets = desktop()?.pets;
    if (!pets) return;
    let mounted = true;
    const apply = (state: DesktopPetState) => { if (mounted) setMiniMuted(state.mutedConversationIds.includes(conversationId)); };
    const unsubscribe = pets.onStateChanged(apply);
    void pets.getState().then(apply).catch(error => { if (mounted) pushToast(String(error), "error"); });
    return () => { mounted = false; unsubscribe(); };
  }, [conversationId]);
  const workspaceItems = useWorkspaceOpenMenu(!environment || environment === "local" ? workspaceRoot : undefined, true);
  const windowItems = [
    ...(canOpenThreadWindow({ archived, conversationType, environment }) ? [{ label: tr("在新窗口中打开"), icon: threadMenuIcon("new-window"), onClick: () => void openThreadWindow(conversationId) }] : []),
    ...workspaceItems,
  ];
  useEffect(() => {
    document.getElementById(menuId)!.querySelector<HTMLButtonElement>('button[role="menuitem"]')!.focus();
  }, [menuId]);
  const bounds = anchor!.getBoundingClientRect();
  return <ContextMenu id={menuId} ariaLabel={tr("会话操作")} className="mc-conversation-menu" itemFontSize="var(--text-xs)"
    anchor={position ? undefined : anchor!} returnFocusTo={anchor!}
    position={position ?? { x: bounds.right - 236, y: bounds.bottom + 6 }} onClose={onClose} items={[
      ...(!archived ? [
        { label: tr("重命名"), icon: threadMenuIcon("rename"), shortcut: formatShortcut(shortcutBindings.renameThread), onClick: onRename },
        { label: tr(pinned ? "取消固定" : "固定"), icon: threadMenuIcon(pinned ? "unpin" : "pin"), shortcut: formatShortcut(shortcutBindings.toggleThreadPin), onClick: onPin },
        { label: tr(hasUnreadTurn ? "标记为已读" : "标记为未读"), icon: threadMenuIcon("read"), onClick: onReadState },
        ...(onNewSection ? [{ label: tr("分组"), icon: threadMenuIcon("section"), submenu: [
          ...sectionOptions.map(section => ({ label: section.name, checked: section.id === currentSection, checkType: "checkbox" as const,
            onClick: () => onSectionChange!(section.id === currentSection ? null : section.id) })),
          ...(sectionOptions.length ? [{ label: "", separator: true }] : []),
          { label: tr("新建分组…"), icon: threadMenuIcon("project-target"), onClick: onNewSection },
        ] }] : []),
        ...(projectOptions.some(project => project.id !== currentProject) || currentProject ? [{ label: tr("项目"), icon: threadMenuIcon("project"), submenu: [
          ...projectOptions.filter(project => project.id !== currentProject).map(project => ({ label: project.name, icon: threadMenuIcon("project-target"), onClick: () => onProjectChange(project.id) })),
          ...(currentProject ? [{ label: tr("从 {name} 移出", { name: projectOptions.find(project => project.id === currentProject)?.name ?? currentProject }), icon: threadMenuIcon("remove-project"), onClick: () => onProjectChange("") }] : []),
        ] }] : []),
        { label: "", separator: true },
      ] : []),
      { label: tr("复制"), icon: threadMenuIcon("copy"), submenu: [
        { label: tr("复制深度链接"), icon: threadMenuIcon("copy"), shortcut: formatShortcut(shortcutBindings.copyDeeplink), onClick: onCopyLink },
        { label: tr("复制为 Markdown"), icon: threadMenuIcon("copy"), onClick: onCopyMarkdown },
        { label: tr("复制工作目录"), icon: threadMenuIcon("copy"), shortcut: formatShortcut(shortcutBindings.copyWorkingDirectory), disabled: !canCopy, onClick: onCopy },
      ] },
      ...(windowItems.length || canHandoff ? [{ label: "", separator: true }] : []),
      ...windowItems,
      ...(canHandoff ? [{ label: isIsolated ? tr("移到本地工作区") : tr("移到隔离工作区"), icon: threadMenuIcon(isIsolated ? "local" : "worktree"), onClick: onHandoff }] : []),
      ...(miniMuted ? [
        { label: "", separator: true },
        { label: tr("取消静音聊天"), onClick: () => { void desktop()!.pets.setNotificationMuted({ conversationId, muted: false }).catch(error => pushToast(String(error), "error")); } },
      ] : []),
      { label: "", separator: true },
      { label: archived ? tr("取消归档") : tr("归档"), icon: archived ? <RotateCcw size={14} /> : threadMenuIcon("archive"), shortcut: archived ? undefined : formatShortcut(shortcutBindings.archiveThread), onClick: onArchive },
    ]} />;
};
