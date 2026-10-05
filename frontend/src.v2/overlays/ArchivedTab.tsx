import { Fragment, useEffect, useState } from "react";
import { Archive, MoreHorizontal, RotateCcw, Search, Trash2, X } from "lucide-react";
import { useAppStore } from "../stores";
import { SelectMenu } from "../components/SelectMenu";
import { commandResultSucceeded, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { workspaceDisplayName } from "../lib/workspace-display";
import { Section } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import type { ConversationRecordPayload } from "../protocol/conversation-types";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { hydrateMessages } from "../chat/transcriptHydration";

export const ArchivedTab = () => {
  const conversations = useAppStore((s) => s.conversations);
  const archived = conversations.filter((conversation) => conversation.archived);
  const [restoringIds, setRestoringIds] = useState<Record<string, boolean>>({});
  const [deletingIds, setDeletingIds] = useState<Record<string, boolean>>({});
  const [query, setQuery] = useState("");
  const [project, setProject] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [preview, setPreview] = useState<ConversationRecordPayload | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [beforeMessageId, setBeforeMessageId] = useState("");
  const [previewVersion, setPreviewVersion] = useState(0);
  const projects = Array.from(new Set(archived.map((conversation) => conversation.worktreePath || conversation.workspaceRoot || "")));
  const matching = archived.filter((conversation) => {
    const root = conversation.worktreePath || conversation.workspaceRoot || "";
    const date = new Date(conversation.updatedAt);
    const day = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    return `${conversation.title} ${root} ${workspaceDisplayName(root, "本机")}`.toLowerCase().includes(query.trim().toLowerCase())
      && (!project || root === project) && (!fromDate || day >= fromDate) && (!toDate || day <= toDate);
  }).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  const selected = archived.find((conversation) => conversation.id === previewId);
  const previewMessages = hydrateMessages(preview?.transcript || []).filter((message) => message.role === "user" || message.role === "assistant");

  useEffect(() => {
    if (!previewId) return;
    const sessionId = getWebSocket()?.sessionId;
    if (!sessionId) { setPreviewError("连接后端后可读取完整归档对话。"); return; }
    let cancelled = false;
    setPreviewLoading(true); setPreviewError("");
    const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(previewId)}/messages`);
    url.searchParams.set("session_id", sessionId);
    url.searchParams.set("limit", "30");
    if (beforeMessageId) url.searchParams.set("before_message_id", beforeMessageId);
    void fetchWithTimeout(url, { headers: authHeaders() }).then(async (response) => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<ConversationRecordPayload>;
    }).then((page) => {
      if (cancelled) return;
      setPreview((current) => beforeMessageId && current ? { ...page, transcript: [...(page.transcript || []), ...(current.transcript || [])] } : page);
    }).catch((error: unknown) => {
      if (!cancelled) setPreviewError(error instanceof Error ? error.message : String(error));
    }).finally(() => { if (!cancelled) setPreviewLoading(false); });
    return () => { cancelled = true; };
  }, [previewId, beforeMessageId, previewVersion]);

  const openPreview = (id: string) => {
    if (id === previewId) { setPreviewId(null); return; }
    setPreview(null); setBeforeMessageId(""); setPreviewError(""); setPreviewId(id);
  };

  const restoreConversation = async (conversationId: string, title: string) => {
    if (restoringIds[conversationId] || deletingIds[conversationId]) return;
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
    setDeletingIds((current) => ({ ...current, [conversationId]: true }));
    try {
      const confirmed = await showConfirm({
        title: "删除已归档任务",
        message: `确定删除“${title}”？此操作无法撤销。`,
        confirmLabel: "删除",
        danger: true,
      });
      if (!confirmed) return;
      const deleted = await sendConversationDeleteCommand({
        type: "conversation.delete",
        conversation_id: conversationId,
        cleanup_worktree: cleanupWorktree,
      });
      if (!deleted) return;
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
      {archived.length > 0 && <div className="settings-archive-filters">
        <label className="settings-search"><Search size={15} /><input aria-label="搜索已归档任务" placeholder="搜索标题或项目…" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <SelectMenu ariaLabel="归档项目" className="settings-select" style={{ width: 220 }} value={project} onValueChange={setProject}><option value="">全部项目</option>{projects.map((root) => <option key={root} value={root}>{workspaceDisplayName(root, "本机")}{root ? ` · ${root}` : ""}</option>)}</SelectMenu>
        <label>最后活动自<input type="date" aria-label="归档最后活动开始日期" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label>
        <label>至<input type="date" aria-label="归档最后活动结束日期" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label>
        <span className="settings-archive-count">{matching.length} / {archived.length} 个任务</span>
      </div>}
      <div className="settings-archive-list">
        {matching.length > 0 ? matching.map((conversation) => (
          <Fragment key={conversation.id}>
          <div className="settings-archive-row" key={conversation.id}>
            <span className="settings-archive-icon" aria-hidden="true"><Archive /></span>
            <div className="settings-archive-copy">
              <button type="button" className="settings-archive-title" aria-expanded={previewId === conversation.id} onClick={() => openPreview(conversation.id)}>{conversation.title || "未命名任务"}</button>
              <span title={conversation.worktreePath || conversation.workspaceRoot}>{workspaceDisplayName(conversation.worktreePath || conversation.workspaceRoot, "本机")}</span>
              <time dateTime={conversation.updatedAt}>最后活动 {new Date(conversation.updatedAt).toLocaleString()}</time>
            </div>
            <div className="settings-archive-actions">
              <button
                type="button"
                className="settings-action-button"
                aria-label={`恢复 ${conversation.title || "未命名任务"}`}
                disabled={Boolean(restoringIds[conversation.id] || deletingIds[conversation.id])}
                onClick={() => void restoreConversation(conversation.id, conversation.title || "未命名任务")}
              >
                <RotateCcw className={restoringIds[conversation.id] ? "settings-spin" : undefined} />
                {restoringIds[conversation.id] ? "恢复中…" : "恢复"}
              </button>
              <details className="settings-archive-more"><summary aria-label={`更多操作 ${conversation.title || "未命名任务"}`}><MoreHorizontal size={16} /></summary><button
                type="button"
                className="settings-action-button"
                data-danger="true"
                aria-label={`删除 ${conversation.title || "未命名任务"}`}
                disabled={Boolean(restoringIds[conversation.id] || deletingIds[conversation.id])}
                onClick={() => void deleteConversation(conversation.id, conversation.title || "未命名任务", Boolean(conversation.gitIsolated))}
              >
                <Trash2 />
                {deletingIds[conversation.id] ? "删除中…" : "删除"}
              </button></details>
            </div>
          </div>
          {previewId === conversation.id && <section className="settings-archive-preview" aria-label={`只读预览 ${conversation.title || "未命名任务"}`}>
            <div className="settings-archive-preview-heading"><strong>归档对话 · 只读预览</strong><button type="button" className="settings-icon-button" aria-label="关闭归档预览" onClick={() => setPreviewId(null)}><X size={16} /></button></div>
            {previewLoading && <p role="status">正在读取对话…</p>}
            {previewError && <p role="alert">{previewError}<button type="button" className="settings-action-button" onClick={() => setPreviewVersion((version) => version + 1)}>重试</button></p>}
            {preview?.transcript_page?.has_more && <button type="button" className="settings-action-button" disabled={previewLoading} onClick={() => setBeforeMessageId(preview.transcript_page!.before_message_id)}>读取更早的消息</button>}
            <div className="settings-archive-transcript">{previewMessages.length > 0 ? previewMessages.map((message) => <article key={message.id} data-role={message.role}>
              <div className="settings-archive-message-label">{message.role === "user" ? "你" : "助手"}</div>
              <MarkdownRenderer content={message.content} conversationId={conversation.id} workspaceRoot={conversation.worktreePath || conversation.workspaceRoot} />
            </article>) : selected?.summary ? <article data-role="summary">
              <MarkdownRenderer content={selected.summary} conversationId={conversation.id} workspaceRoot={conversation.worktreePath || conversation.workspaceRoot} />
            </article> : null}</div>
          </section>}
          </Fragment>
        )) : (
          <div className="settings-archive-empty">
            <Archive aria-hidden="true" />
            <strong>{archived.length ? "没有匹配的任务" : "没有已归档任务"}</strong>
            <span>{archived.length ? "试试其他标题、项目或日期。" : "从会话菜单归档的任务会显示在这里。"}</span>
            {archived.length > 0 && <button type="button" className="settings-action-button" onClick={() => { setQuery(""); setProject(""); setFromDate(""); setToDate(""); }}>清除筛选</button>}
          </div>
        )}
      </div>
    </Section>
  );
};
