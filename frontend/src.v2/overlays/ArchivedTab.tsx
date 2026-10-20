import { useI18n } from "../i18n";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, ArrowLeft, ChevronDown, ChevronRight, Folder, ListFilter, MoreHorizontal, Search, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import type { ConversationMeta } from "../stores/types";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { commandResultSucceeded, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import type { ConversationRecordPayload } from "../protocol/conversation-types";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { hydrateMessages } from "../chat/transcriptHydration";
import { UserMessageCell } from "../chat/cells/UserMessageCell";
import { AssistantMarkdownCell } from "../chat/cells/AssistantMarkdownCell";
import "./ArchivedTab.css";

type ArchiveMenu = { anchor: HTMLButtonElement; kind: "filter" | "project"; project?: string };

export const ArchivedTab = ({ toolbarHost, active = true }: { toolbarHost?: HTMLElement | null; active?: boolean }) => {
  const { t, locale } = useI18n();
  const conversations = useAppStore(state => state.conversations);
  const workspaces = useAppStore(state => state.recentWorkspaces);
  const archived = conversations.filter(conversation => conversation.archived);
  const [restoringIds, setRestoringIds] = useState<Record<string, boolean>>({});
  const [deletingIds, setDeletingIds] = useState<Record<string, boolean>>({});
  const [query, setQuery] = useState("");
  const [project, setProject] = useState<string | null>(null);
  const [sort, setSort] = useState<"updated" | "created" | "alphabetical">("updated");
  const [type, setType] = useState<"all" | "local">("all");
  const [menu, setMenu] = useState<ArchiveMenu | null>(null);
  const [projectMenu, setProjectMenu] = useState<HTMLButtonElement | null>(null);
  const [previewConversation, setPreviewConversation] = useState<ConversationMeta | null>(null);
  const projectRoot = (conversation: ConversationMeta) => conversation.projectId || conversation.workspaceRoot || conversation.worktreePath || "";
  const projectName = (root: string) => workspaces.find(workspace => canonicalWorkspacePath(workspace.path) === canonicalWorkspacePath(root))?.name || workspaceDisplayName(root, t("本机"));
  const projects = [...new Map([...workspaces.map(workspace => workspace.path), ...archived.map(projectRoot)].map(root => [canonicalWorkspacePath(root), root])).values()];
  const matching = archived.filter(conversation => {
    const root = projectRoot(conversation);
    return `${conversation.title} ${root} ${projectName(root)}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
      && (project === null || canonicalWorkspacePath(root) === canonicalWorkspacePath(project));
  }).sort((a, b) => sort === "alphabetical" ? a.title.localeCompare(b.title, locale)
    : sort === "created" ? (b.createdAt || "").localeCompare(a.createdAt || "") : b.updatedAt.localeCompare(a.updatedAt));
  const groups = new Map<string, ConversationMeta[]>();
  for (const conversation of matching) {
    const root = projectRoot(conversation);
    const entries = groups.get(root);
    if (entries) entries.push(conversation); else groups.set(root, [conversation]);
  }
  const busy = Object.keys(restoringIds).length > 0 || Object.keys(deletingIds).length > 0;
  const restoreConversation = async (conversation: ConversationMeta) => {
    setRestoringIds(current => ({ ...current, [conversation.id]: true }));
    try {
      const result = await sendClientCommandAwaitResult({ type: "conversation.unarchive", conversation_id: conversation.id, archived: false }, "conversation.unarchive");
      if (!commandResultSucceeded(result)) {
        pushToast(t("恢复任务失败：{v0}", { v0: result.message || t("后端未返回具体原因") }), "error");
        return;
      }
      pushToast(t("已恢复任务：{v0}", { v0: conversation.title }), "success");
    } catch (reason) { pushToast(t("恢复任务失败：{v0}", { v0: reason instanceof Error ? reason.message : String(reason) }), "error"); }
    finally { setRestoringIds(current => { const next = { ...current }; delete next[conversation.id]; return next; }); }
  };
  const deleteConversations = async (items: ConversationMeta[], kind: "single" | "project" | "all") => {
    setDeletingIds(current => ({ ...current, ...Object.fromEntries(items.map(item => [item.id, true])) }));
    try {
      const confirmed = await showConfirm({
        title: kind === "all" ? t("删除所有已归档聊天？") : kind === "project" ? t("删除项目内所有已归档聊天？") : t("删除已归档聊天？"),
        message: kind === "all" ? t("这会永久删除所有本地已归档聊天。") : kind === "project" ? t("这会永久删除此项目中的 {count} 个本地已归档聊天。", { count: items.length }) : t("这会永久删除此已归档聊天。"),
        confirmLabel: t("删除"), danger: true,
      });
      if (!confirmed) return;
      for (const conversation of items) {
        const deleted = await sendConversationDeleteCommand({ type: "conversation.delete", conversation_id: conversation.id, cleanup_worktree: Boolean(conversation.gitIsolated) });
        if (!deleted) return;
      }
    } finally {
      setDeletingIds(current => { const next = { ...current }; for (const item of items) delete next[item.id]; return next; });
    }
  };
  const deleteAll = archived.length > 0 && previewConversation === null ? <button type="button" className="settings-action-button" data-danger="true" disabled={busy} onClick={() => void deleteConversations(archived, "all")}><Trash2 size={14} />{t("全部删除")}</button> : null;
  const menuItems: ContextMenuItem[] = menu?.kind === "project" ? [{ label: t("删除项目中的全部聊天"), icon: <Trash2 size={15} />, danger: true, disabled: busy, onClick: () => void deleteConversations(archived.filter(conversation => canonicalWorkspacePath(projectRoot(conversation)) === canonicalWorkspacePath(menu.project!)), "project") }] : [
    { label: t("类型"), content: <span className="settings-archive-menu-heading">{t("类型")}</span> },
    { label: t("全部聊天"), checked: type === "all", onClick: () => setType("all") },
    { label: t("本地"), checked: type === "local", onClick: () => setType("local") },
    { label: "", separator: true },
    { label: t("排序方式"), content: <span className="settings-archive-menu-heading">{t("排序方式")}</span> },
    ...(["updated", "created", "alphabetical"] as const).map(value => ({ label: value === "updated" ? t("更新时间") : value === "created" ? t("创建时间") : t("按字母排序"), checked: sort === value, onClick: () => setSort(value) })),
  ];
  if (previewConversation) return <ArchivedConversation conversation={previewConversation} onBack={() => setPreviewConversation(null)} />;
  return <section className="settings-archived-chats" aria-label={t("已归档聊天")}>
    {toolbarHost && active ? createPortal(deleteAll, toolbarHost) : <div className="settings-archive-delete-all">{deleteAll}</div>}
    <div className="settings-archive-toolbar">
      <label className="settings-search"><Search size={15} /><input aria-label={t("搜索已归档任务")} placeholder={t("搜索已归档聊天")} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <button type="button" className="settings-action-button settings-archive-filter" aria-label={t("归档类型和排序")} aria-haspopup="menu" aria-expanded={menu?.kind === "filter"} onClick={event => setMenu({ kind: "filter", anchor: event.currentTarget })}><ListFilter size={15} />{type === "local" ? t("本地") : t("全部聊天")}<ChevronDown size={14} /></button>
      <button type="button" className="settings-action-button settings-archive-filter" aria-label={t("归档项目")} aria-haspopup="menu" aria-expanded={projectMenu !== null} onClick={event => setProjectMenu(event.currentTarget)}><Folder size={15} />{project === null ? t("全部项目") : projectName(project)}<ChevronDown size={14} /></button>
    </div>
    <div className="settings-archive-groups">{[...groups].map(([root, entries]) => <section className="settings-archive-project" aria-label={projectName(root)} key={root}>
      <div className="settings-archive-project-heading"><Folder size={16} aria-hidden="true" /><h3 title={root}>{projectName(root)}</h3><span>{t("{count} 个聊天", { count: entries.length })}</span><button type="button" className="settings-icon-button" aria-label={t("归档项目操作 {project}", { project: projectName(root) })} aria-haspopup="menu" aria-expanded={menu?.kind === "project" && menu.project === root} disabled={busy} onClick={event => setMenu({ kind: "project", project: root, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button></div>
      <div className="settings-card settings-archive-project-chats">{entries.map(conversation => <div className="settings-archive-chat-row" key={conversation.id}>
        <div className="settings-archive-copy"><button type="button" className="settings-archive-title" onClick={() => setPreviewConversation(conversation)}>{conversation.title || t("未命名任务")}</button><time dateTime={conversation.updatedAt}>{new Date(conversation.updatedAt).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" })}</time></div>
        <div className="settings-archive-actions"><button type="button" className="settings-icon-button" aria-label={t("删除 {v0}", { v0: conversation.title || t("未命名任务") })} disabled={busy} onClick={() => void deleteConversations([conversation], "single")}><Trash2 size={16} /></button><button type="button" className="settings-action-button" aria-label={t("恢复 {v0}", { v0: conversation.title || t("未命名任务") })} disabled={busy} onClick={() => void restoreConversation(conversation)}>{restoringIds[conversation.id] ? t("恢复中…") : t("取消归档")}</button></div>
      </div>)}</div>
    </section>)}</div>
    {matching.length === 0 && <div className="settings-archive-empty"><Archive aria-hidden="true" /><strong>{archived.length ? t("没有匹配的任务") : t("没有已归档任务")}</strong><span>{archived.length ? t("试试其他标题或项目。") : t("从会话菜单归档的任务会显示在这里。")}</span></div>}
    {menu && <ContextMenu position={{ x: menu.anchor.getBoundingClientRect().right - 220, y: menu.anchor.getBoundingClientRect().bottom + 6 }} anchor={menu.anchor} onClose={() => setMenu(null)} items={menuItems} />}
    {projectMenu && <ContextMenu position={{ x: projectMenu.getBoundingClientRect().right - 240, y: projectMenu.getBoundingClientRect().bottom + 6 }} anchor={projectMenu} onClose={() => setProjectMenu(null)} items={[
      { label: t("全部项目"), checked: project === null, onClick: () => setProject(null) },
      { label: "", separator: true },
      ...projects.map(root => ({ label: projectName(root), tooltip: root, icon: <Folder size={15} />, checked: project === root, onClick: () => setProject(root) })),
    ]} />}
  </section>;
};

function ArchivedConversation({ conversation, onBack }: { conversation: ConversationMeta; onBack: () => void }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<ConversationRecordPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [beforeMessageId, setBeforeMessageId] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const sessionId = getWebSocket()?.sessionId;
    if (!sessionId) { setError(t("连接后端后可读取完整归档对话。")); return; }
    let cancelled = false;
    setLoading(true); setError("");
    const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(conversation.id)}/messages`);
    url.searchParams.set("session_id", sessionId);
    url.searchParams.set("limit", "30");
    if (beforeMessageId) url.searchParams.set("before_message_id", beforeMessageId);
    void fetchWithTimeout(url, { headers: authHeaders() }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<ConversationRecordPayload>;
    }).then(page => {
      if (!cancelled) setPreview(current => beforeMessageId && current ? { ...page, transcript: [...(page.transcript || []), ...(current.transcript || [])] } : page);
    }).catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [conversation.id, beforeMessageId, version]);
  const messages = hydrateMessages(preview?.transcript || []).filter(message => message.role === "user" || message.role === "assistant");
  return <section className="settings-archive-detail" aria-label={t("只读预览 {v0}", { v0: conversation.title || t("未命名任务") })}>
    <nav className="settings-archive-breadcrumb" aria-label={t("设置导航")}><button type="button" aria-label={t("返回已归档聊天")} onClick={onBack}><ArrowLeft size={16} /></button><button type="button" onClick={() => useAppStore.getState().setSettingsTab("general")}>{t("设置")}</button><ChevronRight size={14} aria-hidden="true" /><button type="button" onClick={onBack}>{t("已归档聊天")}</button><ChevronRight size={14} aria-hidden="true" /><span aria-current="page">{conversation.title}</span></nav>
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{conversation.title}</h2></div></header>
    {loading && <p role="status">{t("正在读取对话…")}</p>}
    {error && <p role="alert">{error}<button type="button" className="settings-action-button" onClick={() => setVersion(current => current + 1)}>{t("重试")}</button></p>}
    {preview?.transcript_page?.has_more && <button type="button" className="settings-action-button" disabled={loading} onClick={() => setBeforeMessageId(preview.transcript_page!.before_message_id)}>{t("读取更早的消息")}</button>}
    <div className="settings-archive-detail-transcript">{messages.length > 0 ? messages.map(message => <article key={message.id} data-role={message.role}>{message.role === "user" ? <UserMessageCell isTranscriptMode conversationId={conversation.id} cell={{
      kind: "user_message", id: message.id, content: message.content, createdAt: message.timestamp,
      contextRefs: message.contextRefs, quotedMessage: message.quotedMessage, messageSource: message.messageSource,
      attachments: message.attachmentRefs?.map(attachment => ({ id: attachment.id, artifactId: attachment.artifactId, docId: attachment.docId, name: attachment.name, type: attachment.mediaType, size: attachment.sizeBytes, dataUrl: attachment.dataUrl })),
    }} /> : <AssistantMarkdownCell isTranscriptMode conversationId={conversation.id} workspaceRoot={conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot} cell={{
      kind: "assistant_markdown", id: message.id, messageId: message.id, markdownSource: message.content, phase: "final", copyable: false,
      citations: message.citations, attachments: message.replyAttachments, artifacts: message.artifacts, createdAt: message.timestamp,
      failureMessage: message.failureMessage, failureRecoverable: message.failureRecoverable,
    }} />}</article>) : preview && conversation.summary ? <article data-role="summary"><MarkdownRenderer content={conversation.summary} conversationId={conversation.id} workspaceRoot={conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot} /></article> : null}</div>
  </section>;
}
