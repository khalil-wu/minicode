import { useI18n } from "../i18n";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, ChevronDown, Folder, ListFilter, MoreHorizontal, Search, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import type { ConversationMeta } from "../stores/types";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { commandResultSucceeded, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { setConversationArchived } from "../chat/archiveConversation";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { useConversationSearch } from "../hooks/useConversationSearch";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { searchResultConversation } from "../protocol/conversation-search";
import "./ArchivedTab.css";

type ArchiveMenu = { anchor: HTMLButtonElement; kind: "filter" | "project"; project?: string };
const EMPTY_CONVERSATIONS: never[] = [];

export const ArchivedTab = ({ toolbarHost, active = true }: { toolbarHost?: HTMLElement | null; active?: boolean }) => {
  const { t, locale } = useI18n();
  const conversations = useAppStore(state => active ? state.conversations : EMPTY_CONVERSATIONS);
  const workspaces = useAppStore(state => state.recentWorkspaces);
  const runtimeCapabilities = useAppStore(state => state.runtimeCapabilities);
  const archived = conversations.filter(conversation => conversation.archived && conversation.conversationType !== "side_chat");
  const [restoringIds, setRestoringIds] = useState<Record<string, boolean>>({});
  const [deletingIds, setDeletingIds] = useState<Record<string, boolean>>({});
  const [query, setQuery] = useState("");
  const [project, setProject] = useState<string | null>(null);
  const [sort, setSort] = useState<"updated" | "created" | "alphabetical">("updated");
  const [type, setType] = useState<"all" | "local">("all");
  const [menu, setMenu] = useState<ArchiveMenu | null>(null);
  const [projectMenu, setProjectMenu] = useState<HTMLButtonElement | null>(null);
  const bodySearch = useConversationSearch({ query, enabled: active && capabilityFeatureEnabled(runtimeCapabilities, "global_search", true),
    archived: true, project, sort });
  const searchEnd = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const target = searchEnd.current;
    if (!target || !bodySearch.hasMore || bodySearch.loading || bodySearch.error) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) bodySearch.loadMore();
    }, { root: target.closest(".settings-center-content"), rootMargin: "120px" });
    observer.observe(target);
    return () => observer.disconnect();
  }, [bodySearch.hasMore, bodySearch.loading, bodySearch.error, bodySearch.loadMore]);
  const projectRoot = (conversation: ConversationMeta) => conversation.projectId ?? (conversation.workspaceRoot || conversation.worktreePath || "");
  const projectName = (root: string) => root ? workspaces.find(workspace => canonicalWorkspacePath(workspace.path) === canonicalWorkspacePath(root))?.name || workspaceDisplayName(root) : t("无项目");
  const projects = [...new Map([...workspaces.map(workspace => workspace.path), ...archived.map(projectRoot)].map(root => [canonicalWorkspacePath(root), root])).values()];
  const contentMatches = bodySearch.results.filter(result => result.conversation.archived
    && result.conversation.conversation_type !== "side_chat"
    && !conversations.some(c => c.id === result.conversation.id && !c.archived));
  const bodyIds = new Set(contentMatches.map(result => result.conversation.id));
  const candidates = [...new Map([...contentMatches.map(searchResultConversation), ...archived].map(c => [c.id, c])).values()];
  const matching = candidates.filter(conversation => {
    const root = projectRoot(conversation);
    return (bodyIds.has(conversation.id) || `${conversation.title} ${root} ${projectName(root)}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
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
      const result = await setConversationArchived(conversation.id, false);
      if (!commandResultSucceeded(result)) {
        pushToast(t("恢复任务失败：{v0}", { v0: result.message || t("后端未返回具体原因") }), "error");
        return;
      }
      pushToast(t("已恢复任务：{v0}", { v0: conversation.title }), "success", undefined, { id: `unarchive-thread-${conversation.id}`, label: t("查看"), onClick: () => {
        useAppStore.getState().requestConversationSwitch(conversation.id);
        useAppStore.setState({ settingsOpen: false });
      } });
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
  const deleteAll = archived.length > 0 ? <button type="button" className="settings-action-button" data-danger="true" disabled={busy} onClick={() => void deleteConversations(archived, "all")}><Trash2 size={14} />{t("全部删除")}</button> : null;
  const menuItems: ContextMenuItem[] = menu?.kind === "project" ? [{ label: t("删除项目中的全部聊天"), icon: <Trash2 size={15} />, danger: true, disabled: busy, onClick: () => void deleteConversations(archived.filter(conversation => canonicalWorkspacePath(projectRoot(conversation)) === canonicalWorkspacePath(menu.project!)), "project") }] : [
    { label: t("类型"), content: <span className="settings-archive-menu-heading">{t("类型")}</span> },
    { label: t("全部聊天"), checked: type === "all", onClick: () => setType("all") },
    { label: t("本地"), checked: type === "local", onClick: () => setType("local") },
    { label: "", separator: true },
    { label: t("排序方式"), content: <span className="settings-archive-menu-heading">{t("排序方式")}</span> },
    ...(["updated", "created", "alphabetical"] as const).map(value => ({ label: value === "updated" ? t("更新时间") : value === "created" ? t("创建时间") : t("按字母排序"), checked: sort === value, onClick: () => setSort(value) })),
  ];
  return <section className="settings-archived-chats" aria-label={t("已归档聊天")}>
    {toolbarHost && active ? createPortal(deleteAll, toolbarHost) : <div className="settings-archive-delete-all">{deleteAll}</div>}
    <div className="settings-archive-toolbar">
      <label className="settings-search"><Search size={15} /><input aria-label={t("搜索已归档任务")} placeholder={t("搜索已归档聊天")} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <button type="button" className="settings-action-button settings-archive-filter" aria-label={t("归档类型和排序")} aria-haspopup="menu" aria-expanded={menu?.kind === "filter"} onClick={event => setMenu({ kind: "filter", anchor: event.currentTarget })}><ListFilter size={15} /><span>{type === "local" ? t("本地") : t("全部聊天")}</span><ChevronDown size={14} /></button>
      <button type="button" className="settings-action-button settings-archive-filter" aria-label={t("归档项目")} aria-haspopup="menu" aria-expanded={projectMenu !== null} onClick={event => setProjectMenu(event.currentTarget)}><Folder size={15} /><span>{project === null ? t("全部项目") : projectName(project)}</span><ChevronDown size={14} /></button>
    </div>
    <div className="settings-archive-groups">{[...groups].map(([root, entries]) => <section className="settings-group" aria-label={projectName(root)} key={root}>
      {project === null && <div className="settings-group-title settings-archive-project-heading"><Folder size={16} aria-hidden="true" /><h3 title={root}>{projectName(root)}</h3><span>{t("{count} 个聊天", { count: entries.length })}</span>{root && <button type="button" className="settings-icon-button settings-archive-icon-action" aria-label={t("归档项目操作 {project}", { project: projectName(root) })} aria-haspopup="menu" aria-expanded={menu?.kind === "project" && menu.project === root} disabled={busy} onClick={event => setMenu({ kind: "project", project: root, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button>}</div>}
      <div className="settings-card settings-archive-project-chats">{entries.map(conversation => <div className="settings-row" key={conversation.id}>
        <div className="settings-row-copy"><span className="settings-row-title settings-archive-title">{conversation.title || t("未命名任务")}</span><time className="settings-row-description settings-archive-date" dateTime={conversation.updatedAt}>{t("{date}，{time}", { date: new Date(conversation.updatedAt).toLocaleDateString(locale, { year: "numeric", month: "short", day: "numeric" }), time: new Date(conversation.updatedAt).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" }) })}{project !== null && root ? ` • ${projectName(root)}` : ""}</time></div>
        <div className="settings-row-control"><button type="button" className="settings-icon-button settings-archive-icon-action" aria-label={t("删除 {v0}", { v0: conversation.title || t("未命名任务") })} disabled={busy} onClick={() => void deleteConversations([conversation], "single")}><Trash2 size={16} /></button><button type="button" className="settings-action-button" aria-label={t("恢复 {v0}", { v0: conversation.title || t("未命名任务") })} disabled={busy} onClick={() => void restoreConversation(conversation)}>{restoringIds[conversation.id] ? t("恢复中…") : t("取消归档")}</button></div>
      </div>)}</div>
    </section>)}</div>
    <div ref={searchEnd} className="settings-archive-search-status">
      {bodySearch.loading && <p role="status">{t("正在搜索…")}</p>}
      {bodySearch.error && <p role="alert">{bodySearch.error}</p>}
    </div>
    {matching.length === 0 && !bodySearch.loading && !bodySearch.error && <div className="settings-archive-empty"><Archive aria-hidden="true" /><strong>{archived.length ? t("没有匹配的任务") : t("没有已归档任务")}</strong><span>{archived.length ? t("试试其他标题或项目。") : t("从会话菜单归档的任务会显示在这里。")}</span></div>}
    {menu && <ContextMenu position={{ x: menu.anchor.getBoundingClientRect().right - 220, y: menu.anchor.getBoundingClientRect().bottom + 6 }} anchor={menu.anchor} onClose={() => setMenu(null)} items={menuItems} />}
    {projectMenu && <ContextMenu position={{ x: projectMenu.getBoundingClientRect().right - 240, y: projectMenu.getBoundingClientRect().bottom + 6 }} anchor={projectMenu} onClose={() => setProjectMenu(null)} items={[
      { label: t("全部项目"), checked: project === null, onClick: () => setProject(null) },
      { label: "", separator: true },
      ...projects.map(root => ({ label: projectName(root), tooltip: root, icon: <Folder size={15} />, checked: project === root, onClick: () => setProject(root) })),
    ]} />}
  </section>;
};
