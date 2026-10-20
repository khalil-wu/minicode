import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { showConfirm } from "../overlays/DialogService";
import { setConversationArchived } from "../chat/archiveConversation";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { canonicalWorkspacePath } from "../lib/workspace-display";
import { projectWorkspaceRoots } from "../lib/project-registry";

export function ProjectContextMenu({ path, position, anchor, onClose, onEdit }: { path: string; position: { x: number; y: number }; anchor?: HTMLElement; onClose: () => void; onEdit?: () => void }) {
  const { t } = useI18n();
  const state = useAppStore.getState();
  const project = state.recentWorkspaces.find(item => item.path === path)!;
  const roots = projectWorkspaceRoots(project);
  const chats = state.conversations.filter(chat => !chat.archived && (chat.projectId === path || roots.some(root => canonicalWorkspacePath(root) === canonicalWorkspacePath(chat.workspaceRoot || chat.worktreePath))));
  const remove = async () => {
    if (!await showConfirm({ title: t("移除项目"), message: t("从项目列表移除“{name}”？文件和聊天记录会保留。", { name: project.name }), confirmLabel: t("移除") })) return;
    try {
      const result = await sendClientCommandAwaitResult({ type: "workspace.recent.remove", path, preserve_history: true }, "workspace.recent.remove");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
    } catch (reason) { pushToast(String(reason), "error"); }
  };
  const archive = async () => {
    if (!await showConfirm({ title: t("归档聊天"), message: t("归档“{name}”中的所有聊天？", { name: project.name }), confirmLabel: t("归档") })) return;
    try {
      for (const chat of chats) { const result = await setConversationArchived(chat.id, true); if (!commandResultSucceeded(result)) throw new Error(result.message); }
    } catch (reason) { pushToast(String(reason), "error"); }
  };
  return <ContextMenu position={position} anchor={anchor} onClose={onClose} items={[
    { label: t("编辑项目"), onClick: onEdit || (() => useAppStore.setState({ projectEditTarget: path })) },
    { label: t("归档聊天"), disabled: chats.length === 0, onClick: () => { void archive(); } },
    { label: t("移除项目"), onClick: () => { void remove(); } },
  ]} />;
}
