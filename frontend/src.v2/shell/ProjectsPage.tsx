import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, MoreHorizontal, Pin, Plus, Search, SquarePen } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { projectExecutionRoot, projectOwnsConversation, projectWorkspaceRoots } from "../lib/project-registry";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { sendClientCommand } from "../protocol/ws-outbox";
import { activateWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { ProjectContextMenu } from "../workspace/ProjectContextMenu";
import { ProjectDialog } from "../overlays/ProjectDialog";
import { ProjectMarker } from "../components/ProjectMarker";
import { saveProject } from "../lib/project-registry";
import { pushToast } from "../overlays/ToastContainer";
import type { RecentWorkspaceProjection } from "../stores/types";
import "./ProjectsPage.css";

export function ProjectsPage() {
  const { t, locale } = useI18n();
  const workspaces = useAppStore(state => state.recentWorkspaces);
  const conversations = useAppStore(state => state.conversations);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"name" | "updated">("updated");
  const [expanded, setExpanded] = useState(new Set<string>());
  const [menu, setMenu] = useState<{ path: string; anchor: HTMLButtonElement } | null>(null);
  const [editor, setEditor] = useState<{ project?: RecentWorkspaceProjection } | null>(null);
  useEffect(() => { sendClientCommand({ type: "workspace.recent" }, { silent: true }); }, []);
  const projects = useMemo(() => workspaces.filter(workspace => `${workspace.name} ${workspace.path}`.toLowerCase().includes(query.trim().toLowerCase()))
    .map(workspace => ({ ...workspace, chats: conversations.filter(conversation => !conversation.archived
      && projectOwnsConversation(workspace, conversation)) }))
    .sort((left, right) => sort === "name" ? (left.name || workspaceDisplayName(left.path)).localeCompare(right.name || workspaceDisplayName(right.path), locale)
      : right.lastOpened - left.lastOpened), [workspaces, conversations, query, sort, locale]);
  const close = () => useAppStore.setState({ projectsOpen: false });
  return <main className="mc-projects-page" aria-labelledby="projects-title">
    <div className="mc-projects-content">
      <h1 id="projects-title">{t("项目")}</h1>
      <div className="mc-projects-toolbar">
        <label className="mc-projects-search"><Search size={16} /><input aria-label={t("搜索项目")} placeholder={t("搜索项目")} value={query} onChange={event => setQuery(event.target.value)} /></label>
        <button className="settings-action-button" type="button" onClick={() => setEditor({})}><Plus size={15} />{t("创建")}</button>
      </div>
      <div className="mc-projects-columns"><button type="button" aria-pressed={sort === "name"} onClick={() => setSort("name")}>{t("名称")}</button><button type="button" aria-pressed={sort === "updated"} onClick={() => setSort("updated")}>{t("已更新")}</button></div>
      <div className="mc-projects-list">
        {projects.map(project => <section className="mc-project-row" key={project.path}>
          <div className="mc-project-heading">
            <ProjectMarker name={project.name} appearance={project.appearance} onChange={appearance => { void saveProject({ path: project.path, appearance }).catch(reason => pushToast(String(reason), "error")); }} />
            <button type="button" className="mc-project-name" title={projectWorkspaceRoots(project).join("\n")} onClick={() => { void activateWorkspaceFolder(projectExecutionRoot(project), "cowork").then(path => { if (path) close(); }); }}>{project.name || workspaceDisplayName(project.path)}</button>
            <button type="button" className="mc-project-icon" aria-label={t("展开/折叠项目")} aria-expanded={expanded.has(project.path)} onClick={() => setExpanded(current => { const next = new Set(current); next.has(project.path) ? next.delete(project.path) : next.add(project.path); return next; })}>{expanded.has(project.path) ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>
            <time dateTime={new Date(project.lastOpened * 1000).toISOString()}>{new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(project.lastOpened * 1000)}</time>
            <button type="button" className="mc-project-icon" aria-label={t("项目操作")} aria-haspopup="menu" aria-expanded={menu?.path === project.path} onClick={event => setMenu({ path: project.path, anchor: event.currentTarget })}><MoreHorizontal size={17} /></button>
            <button type="button" className="mc-project-icon" aria-label={t(project.pinned !== false ? "取消置顶项目" : "置顶项目")} aria-pressed={project.pinned !== false} onClick={() => { void saveProject({ path: project.path, pinned: project.pinned === false }).catch(reason => pushToast(String(reason), "error")); }}><Pin size={16} fill={project.pinned !== false ? "currentColor" : "none"} /></button>
            <button type="button" className="mc-project-icon" aria-label={t("在项目中开始新聊天")} onClick={() => { void useAppStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: projectExecutionRoot(project), appMode: "cowork" }).then(created => { if (created) close(); }); }}><SquarePen size={17} /></button>
          </div>
          {expanded.has(project.path) && <div className="mc-project-chats">{project.chats.map(chat => <button type="button" key={chat.id} onClick={() => { useAppStore.getState().switchConversation(chat.id); close(); }}>{chat.title}</button>)}</div>}
        </section>)}
      </div>
    </div>
    {menu && <ProjectContextMenu path={menu.path} position={{ x: menu.anchor.getBoundingClientRect().right, y: menu.anchor.getBoundingClientRect().bottom }} anchor={menu.anchor} onClose={() => setMenu(null)} onEdit={() => setEditor({ project: workspaces.find(project => project.path === menu.path)! })} />}
    {editor && <ProjectDialog project={editor.project} onClose={() => setEditor(null)} onSaved={() => setEditor(null)} />}
  </main>;
}
