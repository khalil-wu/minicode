import { useEffect, useRef, useState } from "react";
import { ChevronRight, FileCog, Pencil, Plus, Bug, FlaskConical, Play, Settings2, Trash2 } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { ContextMenu } from "../components/ContextMenu";
import { environmentRequest, type EnvironmentDocument, type EnvironmentScript, type LocalEnvironment, type LocalEnvironmentAction, type ProjectEnvironments } from "../protocol/local-environments";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import "./SourceEnvironmentsTab.css";

type Project = { path: string; name: string };
type Draft = EnvironmentDocument & { environment: LocalEnvironment; selectionWorktree: string };
const platforms = ["default", "darwin", "linux", "win32"] as const;
const actionIcons = { tool: Settings2, run: Play, debug: Bug, test: FlaskConical };
const blankEnvironment = (name: string): LocalEnvironment => ({ version: 1, name, setup: { script: "" }, cleanup: { script: "" }, actions: [] });

export function SourceEnvironmentsTab({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const currentRoot = useAppStore(state => state.workingDirectory);
  const recent = useAppStore(state => state.recentWorkspaces);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [info, setInfo] = useState<ProjectEnvironments | null>(null);
  const [editPath, setEditPath] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [createOnLoad, setCreateOnLoad] = useState(false);
  const readOperation = useRef<AbortController | null>(null);
  const draft = editPath ? drafts[editPath] : null;
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void environmentRequest<{ projects: Project[] }>({}, undefined, "", controller.signal).then(result => setProjects(result.projects)).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, recent]);
  useEffect(() => {
    if (!active || !project) return;
    const controller = new AbortController();
    setInfo(null); setError("");
    void environmentRequest<ProjectEnvironments>({ workspace_root: project.path, execution_root: currentRoot }, undefined, "", controller.signal).then(setInfo).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, project, currentRoot, revision]);
  useEffect(() => () => readOperation.current?.abort(), []);
  useEffect(() => {
    if (info && createOnLoad) { setCreateOnLoad(false); createEnvironment(); }
  }, [info, createOnLoad]);
  const openEditor = async (configPath: string) => {
    setError("");
    if (drafts[configPath]) { setEditPath(configPath); return; }
    readOperation.current?.abort();
    const controller = readOperation.current = new AbortController();
    setBusy(true);
    try {
      const document = await environmentRequest<EnvironmentDocument>({ workspace_root: project!.path, config_path: configPath, execution_root: info!.worktree_path }, undefined, "", controller.signal);
      setDrafts(current => ({ ...current, [configPath]: { ...document, environment: document.environment || blankEnvironment(project!.name), selectionWorktree: info!.worktree_path } }));
      setEditPath(configPath);
    } catch (reason) { if (!controller.signal.aborted) setError(String(reason)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  const updateDraft = (environment: LocalEnvironment) => setDrafts(current => ({ ...current, [editPath!]: { ...current[editPath!], environment } }));
  const select = async (configPath: string, worktreePath = info!.worktree_path) => {
    await environmentRequest({}, { workspace_root: project!.path, config_path: configPath, worktree_path: worktreePath }, "/selection");
    window.dispatchEvent(new Event("local-environments:changed"));
    setRevision(value => value + 1);
  };
  const save = async () => {
    const submitted = draft!;
    const submittedProject = project!;
    setBusy(true); setError("");
    try {
      const environment = { ...submitted.environment, actions: submitted.environment.actions.filter(action => action.name.trim() || action.command.trim()) };
      const saved = await environmentRequest<EnvironmentDocument>({}, { workspace_root: submittedProject.path, config_path: submitted.config_path, expected_revision: submitted.revision, environment, worktree_path: submitted.selectionWorktree });
      setDrafts(current => ({ ...current, [submitted.config_path]: { ...saved, environment: saved.environment!, selectionWorktree: submitted.selectionWorktree } }));
      await select(saved.config_path, submitted.selectionWorktree);
      setEditPath(null);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const createEnvironment = () => {
    const path = info!.configs.length ? info!.default_config_path.replace(/environment\.toml$/, `environment-${crypto.randomUUID()}.toml`) : info!.default_config_path;
    void openEditor(path);
  };
  return <section className={`source-environments${draft ? " source-environment-editor" : ""}`} aria-label={t("本地环境")}>
    {project && <nav className="source-environment-breadcrumb" aria-label={t("设置导航")}><button type="button" disabled={busy} onClick={() => { setEditPath(null); setProject(null); setInfo(null); }}>{t("环境")}</button><ChevronRight size={14} /><button type="button" disabled={busy} onClick={() => setEditPath(null)}>{project.name}</button>{draft && <><ChevronRight size={14} /><span>{t("编辑")}</span></>}</nav>}
    {error && <p role="alert">{error}</p>}
    {!project ? <>
      <p className="settings-section-description">{t("本地环境用于配置项目工作树的设置脚本和操作。")} <button type="button" className="source-environment-learn" onClick={() => void openPreferredWebLink("https://developers.openai.com/codex/app/local-environments")}>{t("了解更多")}</button></p>
      <div className="source-environment-section-heading"><h3>{t("选择项目")}</h3><button type="button" className="settings-action-button" onClick={() => useAppStore.setState({ projectEditTarget: "create" })}>{t("添加项目")}</button></div>
      <div className="source-environment-projects">{projects.map(item => <div className="settings-card source-environment-project" key={item.path}><button type="button" className="source-environment-project-label" onClick={() => { setInfo(null); setProject(item); setEditPath(null); }}><FileCog size={18} /><span title={item.path}>{item.name}</span></button><button type="button" className="settings-icon-button" aria-label={t("创建项目环境 {name}", { name: item.name })} onClick={() => { setInfo(null); setProject(item); setEditPath(null); setCreateOnLoad(true); }}><Plus size={16} /></button></div>)}</div>
    </> : draft ? <>
      <header className="settings-page-heading"><h2>{t("编辑本地环境")}</h2></header>
      <div className="settings-card source-environment-name"><label>{t("名称")}<input aria-label={t("环境名称")} value={draft.environment.name} disabled={busy} onChange={event => updateDraft({ ...draft.environment, name: event.target.value })} /></label></div>
      <ScriptEditor kind="setup" value={draft.environment.setup} disabled={busy} onChange={setup => updateDraft({ ...draft.environment, setup })} />
      <ScriptEditor kind="cleanup" value={draft.environment.cleanup} disabled={busy} onChange={cleanup => updateDraft({ ...draft.environment, cleanup })} />
      <div className="source-environment-section-heading"><h3>{t("操作")}</h3><button type="button" className="settings-action-button" disabled={busy} onClick={() => updateDraft({ ...draft.environment, actions: [...draft.environment.actions, { name: "", command: "", icon: "tool" }] })}>{t("添加操作")}</button></div>
      <p className="settings-section-description">{t("这些操作可以运行任意命令，并显示在任务标题栏中。")}</p>
      <div className="source-environment-actions">{draft.environment.actions.length ? draft.environment.actions.map((action, index) => <ActionEditor key={index} action={action} index={index} disabled={busy} onChange={next => updateDraft({ ...draft.environment, actions: draft.environment.actions.map((item, at) => at === index ? next : item) })} onRemove={() => updateDraft({ ...draft.environment, actions: draft.environment.actions.filter((_, at) => at !== index) })} />) : <div className="settings-card source-environment-empty">{t("添加操作以从本地工具栏运行命令")}</div>}</div>
      <div className="source-environment-save"><button type="button" className="settings-action-button" disabled={busy || !draft.environment.name.trim() || draft.environment.actions.some(action => Boolean(action.name.trim()) !== Boolean(action.command.trim()))} onClick={() => void save()}>{busy ? t("正在保存…") : t("保存")}</button></div>
    </> : info ? <>
      <div className="source-environment-section-heading"><h3>{project.name}</h3><button type="button" className="settings-action-button" disabled={busy} onClick={createEnvironment}><Plus size={14} />{t("创建本地环境")}</button></div>
      {info.configs.length ? info.configs.map(document => <div className="settings-card source-environment-summary" key={document.config_path}><div className="source-environment-section-heading"><h3>{document.environment!.name}</h3><div><button type="button" className="settings-action-button" disabled={busy} aria-pressed={info.selected_config_path === document.config_path} onClick={() => { setBusy(true); void select(document.config_path).catch(reason => setError(String(reason))).finally(() => setBusy(false)); }}>{info.selected_config_path === document.config_path ? t("已选择") : t("选择")}</button><button type="button" className="settings-action-button" disabled={busy} aria-label={t("编辑环境 {name}", { name: document.environment!.name })} onClick={() => void openEditor(document.config_path)}><Pencil size={14} />{t("编辑")}</button></div></div><code>{document.config_path}</code><ScriptSummary label={t("设置脚本")} value={document.environment!.setup} /><ScriptSummary label={t("清理脚本")} value={document.environment!.cleanup} /><h4>{t("操作")}</h4>{document.environment!.actions.map((action, index) => <p key={index}>{action.name}<code>{action.command}</code></p>)}</div>) : <p className="settings-section-description">{t("尚未配置本地环境。")}</p>}
    </> : !error && <p role="status">{t("正在读取本地环境…")}</p>}
  </section>;
}

function ScriptSummary({ label, value }: { label: string; value: EnvironmentScript }) {
  const { t } = useI18n();
  const [platform, setPlatform] = useState<typeof platforms[number]>("default");
  const script = platform === "default" ? value.script : value[platform]?.script || value.script;
  return <div className="source-environment-script"><h4>{label}</h4><div className="source-environment-platforms" role="tablist" aria-label={label}>{platforms.map(item => <button type="button" role="tab" aria-selected={platform === item} key={item} onClick={() => setPlatform(item)}>{item === "default" ? t("默认") : item === "darwin" ? "macOS" : item === "linux" ? "Linux" : "Windows"}</button>)}</div><pre>{script || t("未配置脚本")}</pre></div>;
}

function ScriptEditor({ kind, value, disabled, onChange }: { kind: "setup" | "cleanup"; value: EnvironmentScript; disabled: boolean; onChange: (script: EnvironmentScript) => void }) {
  const { t } = useI18n();
  const [platform, setPlatform] = useState<typeof platforms[number]>("default");
  const [variables, setVariables] = useState(false);
  const editor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!variables) return;
    const dismiss = (event: PointerEvent) => { if (!editor.current!.contains(event.target as Node)) setVariables(false); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setVariables(false); } };
    document.addEventListener("pointerdown", dismiss); document.addEventListener("keydown", key, true);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", key, true); };
  }, [variables]);
  const label = kind === "setup" ? t("设置脚本") : t("清理脚本");
  const script = platform === "default" ? value.script : value[platform]?.script || "";
  return <div className="source-environment-script" ref={editor}><div className="source-environment-section-heading"><h3>{label}</h3>{kind === "setup" && <button type="button" className="settings-action-button" aria-expanded={variables} onClick={() => setVariables(!variables)}>{t("变量")}</button>}</div><p className="settings-section-description">{kind === "setup" ? t("创建工作树时在项目根目录运行") : t("清理工作树前在项目根目录运行")}</p><div className="source-environment-platforms" role="tablist" aria-label={label}>{platforms.map(item => <button type="button" role="tab" aria-selected={platform === item} key={item} onClick={() => setPlatform(item)}>{item === "default" ? t("默认") : item === "darwin" ? "macOS" : item === "linux" ? "Linux" : "Windows"}</button>)}</div><textarea aria-label={`${label} · ${platform}`} rows={6} disabled={disabled} value={script} placeholder={platform === "default" ? kind === "setup" ? 'cd "$CODEX_WORKTREE_PATH"\npip install -r requirements.txt\nnpm install\n./run/setup.sh' : "docker compose down --remove-orphans\nrm -rf .cache/tmp" : value.script || (kind === "setup" ? "python -m pip install -r requirements.txt\npnpm install" : "docker compose down --remove-orphans")} onChange={event => onChange(platform === "default" ? { ...value, script: event.target.value } : { ...value, [platform]: { ...value[platform], script: event.target.value } })} />{variables && <div className="source-environment-variables" role="dialog" aria-label={t("设置脚本环境变量")}><strong>{t("设置脚本环境变量")}</strong><span>{t("来源工作区路径")}</span><code>CODEX_SOURCE_TREE_PATH</code><span>{t("新工作树路径")}</span><code>CODEX_WORKTREE_PATH</code></div>}</div>;
}

function ActionEditor({ action, index, disabled, onChange, onRemove }: { action: LocalEnvironmentAction; index: number; disabled: boolean; onChange: (action: LocalEnvironmentAction) => void; onRemove: () => void }) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<HTMLButtonElement | null>(null);
  const Icon = actionIcons[action.icon || "tool"];
  return <div className="settings-card source-environment-action"><label>{t("名称")}<div className="source-environment-action-name"><button type="button" className="settings-icon-button" aria-label={t("操作图标 {index}", { index: index + 1 })} aria-haspopup="menu" disabled={disabled} onClick={event => setMenu(event.currentTarget)}><Icon size={18} /></button><input aria-label={t("操作名称 {index}", { index: index + 1 })} disabled={disabled} value={action.name} onChange={event => onChange({ ...action, name: event.target.value })} /></div></label><label>{t("操作脚本")}<textarea aria-label={t("操作脚本 {index}", { index: index + 1 })} disabled={disabled} value={action.command} placeholder="npm run dev" rows={4} onChange={event => onChange({ ...action, command: event.target.value })} /></label><div className="source-environment-section-heading"><div><span>{t("平台")}</span><div className="source-environment-platforms" role="group" aria-label={t("操作平台 {index}", { index: index + 1 })}>{([undefined, "darwin", "linux", "win32"] as const).map(platform => <button type="button" key={platform || "all"} aria-pressed={action.platform === platform} disabled={disabled} onClick={() => onChange({ ...action, platform })}>{platform === undefined ? t("所有平台") : platform === "darwin" ? "macOS" : platform === "linux" ? "Linux" : "Windows"}</button>)}</div></div><button type="button" className="settings-icon-button" aria-label={t("移除操作 {index}", { index: index + 1 })} disabled={disabled} onClick={onRemove}><Trash2 size={17} /></button></div>{menu && <ContextMenu position={{ x: menu.getBoundingClientRect().left, y: menu.getBoundingClientRect().bottom + 4 }} anchor={menu} onClose={() => setMenu(null)} items={(Object.keys(actionIcons) as Array<NonNullable<LocalEnvironmentAction["icon"]>>).map(icon => { const ActionIcon = actionIcons[icon]; return { label: icon === "tool" ? t("工具") : icon === "run" ? t("运行") : icon === "debug" ? t("调试") : t("测试"), icon: <ActionIcon size={16} />, checked: (action.icon || "tool") === icon, onClick: () => onChange({ ...action, icon }) }; })} />}</div>;
}
