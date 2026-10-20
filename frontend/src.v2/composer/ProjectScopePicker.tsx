import { useI18n } from "../i18n";
import { useEffect, useRef, useState } from "react";
import { Check, Folder, GitBranch, Laptop, Plus, Search, X } from "../lib/icons";
import { useAppStore } from "../stores";
import { activateWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { projectExecutionRoot, projectOwnsWorkspace, projectWorkspaceRoots } from "../lib/project-registry";
import { canonicalWorkspacePath, workspaceDisplayName } from "../lib/workspace-display";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { isDesktop } from "../desktop/runtime";

const projectName = (root: string) => workspaceDisplayName(canonicalWorkspacePath(root));

export function ProjectScopePicker() {
  const { t: tr } = useI18n();
  const workingDirectory = useAppStore(state => state.workingDirectory);
  const projects = useAppStore(state => state.recentWorkspaces);
  const workspaceGit = useAppStore(state => state.workspaceGit);
  const conversation = useAppStore(state => state.conversations.find(item => item.id === state.conversationId));
  const workspaceRoot = conversation?.executionCwd && !conversation.workspaceRoot && !conversation.worktreePath ? "" : workingDirectory;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const picker = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); trigger.current!.focus(); };
  useEffect(() => {
    const openPicker = () => { setQuery(""); setOpen(true); };
    window.addEventListener("composer:open-project-picker", openPicker);
    return () => window.removeEventListener("composer:open-project-picker", openPicker);
  }, []);
  useEffect(() => { setOpen(false); }, [workspaceRoot]);
  useEffect(() => {
    if (!open) return;
    picker.current!.querySelector<HTMLInputElement>('input')!.focus();
    const outside = (event: MouseEvent) => { if (!picker.current!.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);
  const selectedProject = projects.find(project => project.path === conversation?.projectId)
    ?? projects.find(project => projectOwnsWorkspace(project, workspaceRoot));
  const filtered = projects.filter(project => `${project.name} ${project.path} ${projectWorkspaceRoots(project).join(" ")}`.toLowerCase().includes(query.toLowerCase()));
  const branch = workspaceRoot && workspaceGit && workspaceRootsEqual(workspaceGit.currentPath, workspaceRoot)
    ? workspaceGit.branch
    : workspaceRoot && conversation && workspaceRootsEqual(conversation.worktreePath || conversation.workspaceRoot, workspaceRoot) ? conversation.gitBranch : undefined;
  return <div ref={picker} className="composer-project-scope">
    <button ref={trigger} type="button" aria-label={tr("选择项目")} aria-expanded={open} aria-haspopup="dialog" title={workspaceRoot} onClick={() => { setOpen(value => !value); setQuery(""); }}>
      <Folder size={16} /><span>{selectedProject?.name || (workspaceRoot ? projectName(workspaceRoot) : tr("选择项目"))}</span>
    </button>
    {workspaceRoot && isDesktop() && <span className="composer-scope-location"><Laptop size={16} /><span>{workspaceGit?.isWorktree && workspaceRootsEqual(workspaceGit.currentPath, workspaceRoot) ? tr("工作树") : tr("本地")}</span></span>}
    {branch && <button type="button" className="composer-scope-branch" aria-label={tr("查看分支 {branch}", { branch })} title={branch} onClick={() => useAppStore.getState().openBottomTab("git")}><GitBranch size={16} /><span>{branch}</span></button>}
    {open && <div className="mc-dropdown-menu composer-project-menu" role="dialog" aria-label={tr("选择项目")} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      if (event.target instanceof HTMLInputElement && event.key !== "ArrowDown") return;
      event.preventDefault();
      const buttons = Array.from(picker.current!.querySelectorAll<HTMLButtonElement>('.composer-project-menu button'));
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }}>
      <label><Search size={15} /><input aria-label={tr("搜索项目")} placeholder={tr("搜索项目")} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <div className="composer-project-options">
      {filtered.map(project => <button type="button" key={project.path} aria-pressed={project.path === selectedProject?.path} title={projectWorkspaceRoots(project).join("\n")} onClick={() => {
        close(); if (!projectOwnsWorkspace(project, workspaceRoot)) void activateWorkspaceFolder(projectExecutionRoot(project), "cowork");
      }}><Folder size={16} /><span>{project.name || projectName(project.path)}</span>{project.path === selectedProject?.path && <Check size={16} className="composer-project-selected" aria-hidden="true" />}</button>)}
      {!filtered.length && <p className="composer-add-status">{tr("没有匹配的项目")}</p>}
      </div>
      <div className="composer-project-actions">
        <button type="button" onClick={() => { close(); useAppStore.setState({ projectEditTarget: "create" }); }}><Plus size={16} /><span>{tr("新建项目")}</span></button>
        <button type="button" aria-pressed={!workspaceRoot} onClick={() => { close(); if (workspaceRoot) void useAppStore.getState().createConversation({ bindWorkspace: false, appMode: "cowork" }); }}><X size={16} /><span>{tr("不在项目中工作")}</span></button>
      </div>
    </div>}
  </div>;
}
