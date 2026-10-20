import { useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, Folder, X } from "../lib/icons";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { isDesktop, pickWorkspaceDirectory } from "../desktop/runtime";
import { ContextMenu } from "../components/ContextMenu";
import { ProjectMarker } from "../components/ProjectMarker";
import { saveProject } from "../lib/project-registry";
import { workspaceDisplayName } from "../lib/workspace-display";
import type { RecentWorkspaceProjection } from "../stores/types";
import type { ProjectAppearance } from "../lib/project-appearance";
import { showPrompt } from "./DialogService";
import { useAppStore } from "../stores";
import "./ProjectDialog.css";

export function ProjectDialogHost() {
  const target = useAppStore(state => state.projectEditTarget);
  const project = useAppStore(state => state.recentWorkspaces.find(item => item.path === target));
  const close = () => useAppStore.setState({ projectEditTarget: null });
  return target ? <ProjectDialog key={target} project={target === "create" ? undefined : project} onClose={close} onSaved={close} /> : null;
}

export function ProjectDialog({ project, onClose, onSaved }: { project?: RecentWorkspaceProjection; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [name, setName] = useState(project?.name || "");
  const [folders, setFolders] = useState(project?.sourceFolders || (project ? [project.path] : []));
  const [appearance, setAppearance] = useState<ProjectAppearance | null>(project?.appearance || null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const dialog = useFocusTrap(true);
  const addFolder = async () => {
    const selected = isDesktop() ? await pickWorkspaceDirectory() : await showPrompt({ title: t("选择来源文件夹"), message: t("输入已在桌面端授权的本机文件夹路径。"), confirmLabel: t("添加") });
    if (!selected) return;
    setFolders(current => [...new Set([...current, selected])]);
    setName(current => current || workspaceDisplayName(selected));
  };
  const save = async () => {
    setSaving(true); setError("");
    try { await saveProject({ ...(project ? { path: project.path } : {}), name: name.trim(), source_folders: folders, appearance }); onSaved(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  };
  return createPortal(<div ref={dialog} className="mc-project-dialog-backdrop" onClick={() => { if (!saving) onClose(); }}><form className="mc-project-dialog" role="dialog" aria-modal="true" aria-label={project ? t("编辑项目") : t("创建项目")} tabIndex={-1}
    onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!saving) onClose(); } }} onSubmit={event => { event.preventDefault(); void save(); }}>
    <h2>{project ? t("编辑项目") : t("创建项目")}</h2>
    <div className="mc-project-name-input"><ProjectMarker name={name || t("新项目")} appearance={appearance} onChange={setAppearance} /><input aria-label={t("项目名称")} placeholder={t("项目名称")} value={name} autoFocus required disabled={saving} onChange={event => setName(event.target.value)} /></div>
    <section className="mc-project-source-folders"><h3>{t("来源文件夹")}</h3>
      {folders.map(folder => <div className="mc-project-source-folder" key={folder}><Folder size={17} /><span title={folder}>{folder}</span><button type="button" className="mc-project-icon" aria-label={t("移除文件夹 {path}", { path: folder })} disabled={saving} onClick={() => setFolders(current => current.filter(path => path !== folder))}><X size={15} /></button></div>)}
      <button type="button" className="settings-action-button" disabled={saving} aria-haspopup="menu" aria-expanded={Boolean(menu)} onClick={event => { const bounds = event.currentTarget.getBoundingClientRect(); setMenu({ x: bounds.left, y: bounds.bottom + 4 }); }}>{t("添加此电脑上的文件夹")}<ChevronDown size={14} /></button>
    </section>
    {error && <p role="alert">{error}</p>}
    <footer><button type="button" className="settings-action-button" disabled={saving} onClick={onClose}>{t("取消")}</button><button type="submit" className="settings-action-button" data-primary="true" disabled={saving || !name.trim()}>{saving ? t("保存中…") : project ? t("保存") : t("创建")}</button></footer>
    {menu && <ContextMenu position={menu} onClose={() => setMenu(null)} items={[{ label: t("添加此电脑上的文件夹"), icon: <Folder size={16} />, onClick: () => { void addFolder().catch(reason => setError(String(reason))); } }]} />}
  </form></div>, document.body);
}
