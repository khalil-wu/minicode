import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { showConfirm, showPrompt } from "../overlays/DialogService";
import { setConversationArchived } from "../chat/archiveConversation";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { projectOwnsConversation, projectSourceFolders, saveProject } from "../lib/project-registry";
import { useSidebarSections, type SidebarSection } from "../protocol/thread-metadata";
import { threadMenuIcon } from "../shell/sidebarComponents";
import { MenuSourceIcon } from "../lib/menu-source-icons";
import { desktop, isDesktop } from "../desktop/runtime";

export function ProjectContextMenu({ path, position, anchor, onClose, onEdit }: { path: string; position: { x: number; y: number }; anchor?: HTMLElement; onClose: () => void; onEdit?: () => void }) {
  const { t } = useI18n();
  const state = useAppStore.getState();
  const { sections, mutate } = useSidebarSections();
  const currentSection = sections.find(section => section.item_keys.some(key => key.startsWith("project:")
    && key.slice(8).replace(/\\/g, "/").toLowerCase() === path.replace(/\\/g, "/").toLowerCase()));
  const move = async (sectionId: string | null) => {
    try { await mutate("/sections/move", { kind: "project", id: path, section_id: sectionId }); }
    catch (error) { pushToast(String(error), "error"); }
  };
  const createSection = async () => {
    const name = await showPrompt({ title: t("新建分组"), message: t("按你的方式组织聊天和项目"), placeholder: t("分组名称"), allowEmpty: true, confirmLabel: t("创建分组") });
    if (name === null) return;
    try { const result = await mutate<{ section: SidebarSection }>("/sections", { name: name.trim() || t("新建分组"),
      ...(currentSection ? { after_section_id: currentSection.id } : {}) }); await move(result.section.id); }
    catch (error) { pushToast(String(error), "error"); }
  };
  const project = state.recentWorkspaces.find(item => item.path === path)!;
  const sourceFolders = projectSourceFolders(project);
  const chats = state.conversations.filter(chat => !chat.archived && projectOwnsConversation(project, chat));
  const unreadChats = chats.filter(chat => chat.hasUnreadTurn);
  const markRead = async () => {
    try {
      await Promise.all(unreadChats.map(async chat => {
        const result = await sendClientCommandAwaitResult({ type: "conversation.read_state.set", conversation_id: chat.id, has_unread_turn: false }, "conversation.read_state.set");
        if (!commandResultSucceeded(result)) throw new Error(result.message);
      }));
    } catch (reason) { pushToast(String(reason), "error"); }
  };
  const remove = async () => {
    if (!await showConfirm({ title: t("移除项目"), message: t("从项目列表移除“{name}”？文件和聊天记录会保留。", { name: project.name }), confirmLabel: t("移除") })) return;
    try {
      const result = await sendClientCommandAwaitResult({ type: "workspace.recent.remove", path }, "workspace.recent.remove");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
    } catch (reason) { pushToast(String(reason), "error"); }
  };
  const archive = async () => {
    if (!await showConfirm({ title: t("归档聊天"), message: t("归档“{name}”中的所有聊天？", { name: project.name }), confirmLabel: t("归档") })) return;
    try {
      for (const chat of chats) { const result = await setConversationArchived(chat.id, true); if (!commandResultSucceeded(result)) throw new Error(result.message); }
    } catch (reason) { pushToast(String(reason), "error"); }
  };
  return <ContextMenu className="mc-project-menu" align="start" style={{ minWidth: "min(160px, calc(100vw - 16px))" }} position={position} anchor={anchor} onClose={onClose} items={[
    { label: t(project.pinned !== false ? "取消固定" : "固定"), icon: threadMenuIcon(project.pinned !== false ? "unpin" : "pin"),
      onClick: () => { void saveProject({ path, pinned: project.pinned === false }).catch(reason => pushToast(String(reason), "error")); } },
    { label: t("编辑"), icon: <MenuSourceIcon kind="settings" />, onClick: onEdit || (() => useAppStore.setState({ projectEditTarget: path })) },
    { label: "", separator: true },
    { label: t("分组"), icon: threadMenuIcon("section"), submenu: [
      ...sections.map(section => ({ label: section.name, checked: section.id === currentSection?.id,
        checkType: "checkbox" as const, onClick: () => { void move(section.id === currentSection?.id ? null : section.id); } })),
      ...(sections.length ? [{ label: "", separator: true }] : []),
      { label: t("新建分组…"), icon: threadMenuIcon("project-target"), onClick: () => { void createSection(); } },
    ] },
    ...(isDesktop() && sourceFolders.length === 1 ? [{ label: t("在资源管理器中打开"), icon: threadMenuIcon("project-folder"),
      onClick: () => { void desktop()!.openPreferredPath(sourceFolders[0], undefined, { targetId: "fileManager", workspaceRoot: sourceFolders[0] }).catch(reason => pushToast(String(reason), "error")); } }] : []),
    { label: "", separator: true },
    ...(unreadChats.length ? [{ label: t("全部标记为已读"), icon: threadMenuIcon("mark-read-all"), onClick: () => { void markRead(); } }] : []),
    { label: t("归档聊天"), icon: threadMenuIcon("archive"), disabled: chats.length === 0, onClick: () => { void archive(); } },
    { label: "", separator: true },
    { label: t("移除项目"), icon: threadMenuIcon("project-remove"), onClick: () => { void remove(); } },
  ]} />;
}
