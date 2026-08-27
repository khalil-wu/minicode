import { useState } from "react";
import { Archive, RotateCcw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { workspaceDisplayName } from "../lib/workspace-display";
import { Section } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";

export const ArchivedTab = () => {
  const conversations = useAppStore((s) => s.conversations);
  const archived = conversations.filter((conversation) => conversation.archived);
  const [restoringIds, setRestoringIds] = useState<Record<string, boolean>>({});
  const [deletingIds, setDeletingIds] = useState<Record<string, boolean>>({});

  const restoreConversation = async (conversationId: string, title: string) => {
    if (restoringIds[conversationId]) return;
    setRestoringIds((current) => ({ ...current, [conversationId]: true }));
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.unarchive",
        conversation_id: conversationId,
        archived: false,
      }, "conversation.unarchive");
      if (!commandResultSucceeded(result)) {
        pushToast(`恢复任务失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast(`已恢复任务：${title}`, "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`恢复任务失败：${message}`, "error");
    } finally {
      setRestoringIds((current) => {
        const next = { ...current };
        delete next[conversationId];
        return next;
      });
    }
  };

  const deleteConversation = async (conversationId: string, title: string, cleanupWorktree: boolean) => {
    if (restoringIds[conversationId] || deletingIds[conversationId]) return;
    const confirmed = await showConfirm({
      title: "删除已归档任务",
      message: `确定删除“${title}”？此操作无法撤销。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!confirmed) return;
    setDeletingIds((current) => ({ ...current, [conversationId]: true }));
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.delete",
        conversation_id: conversationId,
        cleanup_worktree: cleanupWorktree,
      }, "conversation.delete");
      if (!commandResultSucceeded(result)) {
        pushToast(`删除任务失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast(`已删除任务：${title}`, "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`删除任务失败：${message}`, "error");
    } finally {
      setDeletingIds((current) => {
        const next = { ...current };
        delete next[conversationId];
        return next;
      });
    }
  };

  return (
    <Section title="已归档任务" description={`${archived.length} 个任务已从会话列表隐藏。`}>
      <div className="settings-archive-list">
        {archived.length > 0 ? archived.map((conversation) => (
          <div className="settings-archive-row" key={conversation.id}>
            <span className="settings-archive-icon" aria-hidden="true"><Archive /></span>
            <div className="settings-archive-copy">
              <strong>{conversation.title || "未命名任务"}</strong>
              <span>{workspaceDisplayName(conversation.worktreePath || conversation.workspaceRoot, "本机")}</span>
            </div>
            <div className="settings-archive-actions">
              <button
                type="button"
                className="settings-action-button"
                aria-label={`恢复 ${conversation.title || "未命名任务"}`}
                disabled={Boolean(restoringIds[conversation.id])}
                onClick={() => void restoreConversation(conversation.id, conversation.title || "未命名任务")}
              >
                <RotateCcw className={restoringIds[conversation.id] ? "settings-spin" : undefined} />
                {restoringIds[conversation.id] ? "恢复中…" : "恢复"}
              </button>
              <button
                type="button"
                className="settings-action-button"
                data-danger="true"
                aria-label={`删除 ${conversation.title || "未命名任务"}`}
                disabled={Boolean(restoringIds[conversation.id] || deletingIds[conversation.id])}
                onClick={() => void deleteConversation(conversation.id, conversation.title || "未命名任务", Boolean(conversation.gitIsolated))}
              >
                <Trash2 />
                {deletingIds[conversation.id] ? "删除中…" : "删除"}
              </button>
            </div>
          </div>
        )) : (
          <div className="settings-archive-empty">
            <Archive aria-hidden="true" />
            <strong>没有已归档任务</strong>
            <span>从会话菜单归档的任务会显示在这里。</span>
          </div>
        )}
      </div>
    </Section>
  );
};
