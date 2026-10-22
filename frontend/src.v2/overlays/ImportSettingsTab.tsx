import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowDown, AtSign, BookOpen, Bot, Braces, ChevronDown, FolderClosed, Info, Layers, LoaderCircle, MessageCircle, Settings, Terminal, Workflow, X } from "../lib/icons";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Tip } from "../components/Tooltip";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout, LONG_HTTP_TIMEOUT_MS } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import "./ProjectDialog.css";
import "./ImportSettingsTab.css";
import { projectOwnsWorkspace, projectWorkspaceRoots } from "../lib/project-registry";
import { openManagedSettingsTarget } from "../desktop/runtime";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import { ImportProviderIcon } from "./ImportProviderIcon";

type ItemType = "sessions" | "projects" | "instructions" | "settings" | "skills" | "plugins" | "mcp" | "agents" | "hooks" | "commands" | "memory";
type Item = { id: string; provider_id: string; item_type: ItemType; title: string; source_path: string; target_path: string; workspace_root: string; supported: boolean; reason: string };
type ImportResult = Item & { status: "success" | "skipped" | "unsupported" | "error"; message: string };
type ImportOutcome = { results: ImportResult[]; setup_errors?: string[]; workspace_root?: string };
type ImportReceipt = ImportOutcome & { history: History & { workspace_root: string } };
type ImportProgress = ImportOutcome & { id: string | null; detection_id?: string; status: "running" | "success" | "error" | "unchanged"; items: Item[]; error?: string };
type History = ImportOutcome & { id: string; completed_at: string; automatic: boolean };
type ImportState = { providers: { id: string; name: string }[]; history: History[]; sync: { enabled: boolean; item_types: ItemType[]; last_error?: string }; connected_provider_ids: string[]; item_types: ItemType[]; running_import: { id: string; item_types: ItemType[] } | null; import_progress: ImportProgress | null };
type Detection = { detection_id: string; items: Item[]; issues: { provider_id: string; source_path: string; status: string; message: string }[] };

const EN = {
  title: "Import from another AI app", subtitle: "Bring setup, projects, and chats from other AI apps into MiniCode", detected: "Detected setup that can be added to MiniCode",
  generalTitle: "Import work from other AI apps", generalDescription: "Bring over your setup, projects, and recent chats", importedSetup: "Imported agent setup", lastImported: "Last imported {relativeTime}", generalChecking: "Checking", noData: "No data detected", importAgain: "Import again", viewImported: "View imported files", tryAgain: "Try again",
  resultTitle: "Imported external agent config", resultSubtitle: "Selected config was copied into MiniCode", resultErrorTitle: "Import failed", resultErrorSubtitle: "Some config could not be imported. Check the selected items and try again", resultUnchangedTitle: "No content imported", resultUnchangedSubtitle: "Review the results for the selected items", resultRunningTitle: "Importing external agent config", resultRunningSubtitle: "Hang tight, this may take a few moments", scrollToBottom: "Scroll to bottom", userConfig: "User config", currentProject: "Current project", ok: "OK",
  checking: "Checking for imports", checkingDetail: "Looking for compatible setup, projects, and recent chats", empty: "Import from other AI apps", emptyDetail: "No importable setup found", noNew: "No new items found",
  import: "Import", importTo: "Import", importing: "Importing", importProgress: "Import in progress", checkAgain: "Check again", retry: "Retry", review: "Select items to import", reviewDetail: "Choose what to bring over. Your existing setup won’t be affected.", cancel: "Cancel", close: "Close",
  dialogTitle: "Import from other AI apps", appsFound: "Apps found", continue: "Continue", tools: "Tools & setup", toolsDetail: "Settings, instructions, plugins, skills", projectsDetail: "Use your existing project folders", chatsDetail: "Recent chats", showTools: "Show Tools & setup details", hideTools: "Hide Tools & setup details", info: "What will be imported for", imported: "Import results",
  syncTitle: "Automatic sync", sync: "Keep imports in sync", syncDetail: "Automatically sync new and updated content from connected sources", paused: "Sync paused. Your content selections are saved", content: "Content", customize: "Customize", afterImport: "Available after your first import", save: "Save",
  history: "Import history", automatic: "Automatic sync", viewMore: "View more", noneSelected: "No content selected", categories: { sessions: "Chat sessions", projects: "Projects", instructions: "Instructions", settings: "Settings", skills: "Skills", plugins: "Plugins", mcp: "MCP servers", agents: "Agents", hooks: "Hooks", commands: "Commands", memory: "Memory" },
  status: { success: "Imported", skipped: "Skipped", unsupported: "Unsupported", error: "Failed" },
};
const ZH: typeof EN = {
  title: "从其他 AI 应用导入", subtitle: "将其他 AI 应用的配置、项目和聊天导入 MiniCode", detected: "检测到可添加到 MiniCode 的配置",
  generalTitle: "从其他 AI 应用导入", generalDescription: "导入配置、项目和近期聊天", importedSetup: "已导入智能体配置", lastImported: "上次导入：{relativeTime}", generalChecking: "正在检查", noData: "未检测到数据", importAgain: "再次导入", viewImported: "查看导入的文件", tryAgain: "重试",
  resultTitle: "已导入外部智能体配置", resultSubtitle: "所选配置已复制到 MiniCode", resultErrorTitle: "导入失败", resultErrorSubtitle: "部分配置未能导入，请检查所选内容后重试", resultUnchangedTitle: "未导入内容", resultUnchangedSubtitle: "查看所选内容的处理结果", resultRunningTitle: "正在导入外部智能体配置", resultRunningSubtitle: "请稍候，这可能需要一些时间", scrollToBottom: "滚动到底部", userConfig: "用户配置", currentProject: "当前项目", ok: "确定",
  checking: "正在检查可导入的内容", checkingDetail: "查找兼容的配置、项目和近期聊天", empty: "从其他 AI 应用导入", emptyDetail: "未找到可导入的配置", noNew: "未发现新内容",
  import: "导入", importTo: "导入", importing: "正在导入", importProgress: "正在导入", checkAgain: "再次检查", retry: "重试", review: "选择要导入的内容", reviewDetail: "选择要导入的内容。现有配置不会受到影响。", cancel: "取消", close: "关闭",
  dialogTitle: "从其他 AI 应用导入", appsFound: "找到的应用", continue: "继续", tools: "工具与配置", toolsDetail: "设置、指令、插件、技能", projectsDetail: "使用现有项目文件夹", chatsDetail: "最近的聊天", showTools: "展开工具与配置详情", hideTools: "收起工具与配置详情", info: "将导入的内容", imported: "导入结果",
  syncTitle: "自动同步", sync: "保持导入内容同步", syncDetail: "自动同步已连接来源中新增和更新的内容", paused: "同步已暂停，内容选择会保留", content: "内容", customize: "自定义", afterImport: "首次导入后可用", save: "保存",
  history: "导入历史", automatic: "自动同步", viewMore: "查看更多", noneSelected: "未选择内容", categories: { sessions: "聊天会话", projects: "项目", instructions: "指令", settings: "设置", skills: "技能", plugins: "插件", mcp: "MCP 服务", agents: "Agent", hooks: "Hook", commands: "命令", memory: "记忆" },
  status: { success: "已导入", skipped: "已跳过", unsupported: "不支持", error: "导入失败" },
};

function completedImport(outcome: ImportOutcome, id: string | null): ImportProgress {
  return { ...outcome, id, items: outcome.results,
    status: outcome.setup_errors?.length || outcome.results.some(result => result.status === "error") ? "error"
      : outcome.results.some(result => result.status === "success") ? "success" : "unchanged" };
}

async function request<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const response = await fetchWithTimeout(new URL(`/api/settings/import${path}`, apiBase()), {
    method: body === undefined ? "GET" : method,
    headers: authHeaders(body === undefined ? undefined : { "Content-Type": "application/json" }),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }, { timeoutMs: LONG_HTTP_TIMEOUT_MS });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<T>;
}

export function ImportSettingsTab({ active = true, variant = "page" }: {
  active?: boolean;
  variant?: "page" | "general-row";
}) {
  const { locale } = useI18n();
  const text = locale === "en-US" ? EN : ZH;
  const workspace = useAppStore(state => state.workingDirectory);
  const project = useAppStore(state => state.recentWorkspaces.find(item => projectOwnsWorkspace(item, state.workingDirectory)));
  const roots = useMemo(() => project ? projectWorkspaceRoots(project) : workspace ? [workspace] : [], [project, workspace]);
  const rootsKey = JSON.stringify(roots);
  const [state, setState] = useState<ImportState>();
  const [detection, setDetection] = useState<Detection>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<{ provider?: string } | null>(null);
  const [progress, setProgress] = useState<ImportProgress | null>(null);
  const [importing, setImporting] = useState(false);
  const [syncSaving, setSyncSaving] = useState(false);
  const [customize, setCustomize] = useState(false);
  const [showAllHistory, setShowAllHistory] = useState(false);
  const sequence = useRef(0);
  const receiveState = (next: ImportState) => {
    setState(next);
    setProgress(current => {
      const remote = next.import_progress;
      if (!current) return remote?.status === "running" ? remote : null;
      if (current.status !== "running") return current;
      if (remote && (remote.id === current.id || remote.detection_id === current.detection_id)) {
        return importing ? { ...remote, status: "running" } : remote;
      }
      const history = next.history.find(entry => entry.id === current.id);
      return !importing && history ? completedImport(history, history.id) : current;
    });
  };
  const load = async () => {
    const revision = ++sequence.current;
    setLoading(true); setError("");
    try {
      const [, nextDetection] = await Promise.all([
        request<ImportState>("").then(next => { if (sequence.current === revision) receiveState(next); }),
        request<Detection>("/detect", { workspace_roots: JSON.parse(rootsKey), provider_ids: ["claude", "cursor"], include_home: true, max_session_age_days: 30, max_sessions: 50 }),
      ]);
      if (sequence.current === revision) setDetection(nextDetection);
    } catch (reason) { if (sequence.current === revision) setError(String(reason)); }
    finally { if (sequence.current === revision) setLoading(false); }
  };
  useEffect(() => {
    if (!active) return;
    setSelection(null);
    void load();
    return () => { sequence.current++; };
  }, [active, rootsKey]);
  useEffect(() => {
    if (!active || !(importing || state?.running_import || progress?.status === "running")) return;
    const timer = window.setInterval(() => { void request<ImportState>("").then(next => {
      receiveState(next);
      if (!next.running_import && !importing) void load();
    }).catch(reason => setError(String(reason))); }, 2000);
    return () => window.clearInterval(timer);
  }, [active, state?.running_import?.id, progress?.status, importing, rootsKey]);

  const apply = async (itemIds: string[]) => {
    const plan = detection!;
    const owner = workspace;
    setImporting(true); setError("");
    setSelection(null);
    setProgress({ id: null, detection_id: plan.detection_id, workspace_root: owner, status: "running", items: plan.items.filter(item => itemIds.includes(item.id)), results: [] });
    let imported: ImportReceipt | undefined;
    try {
      imported = await request<ImportReceipt>("", { detection_id: plan.detection_id, item_ids: itemIds, workspace_root: owner });
      const outcome = imported;
      setProgress(current => current?.detection_id === plan.detection_id
        ? completedImport({ ...outcome, workspace_root: outcome.history.workspace_root }, outcome.history.id) : current);
      sendClientCommand({ type: "conversation.list" }, { silent: true });
      sendClientCommand({ type: "workspace.recent" }, { silent: true });
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "commands.list" }, { silent: true });
      receiveState(await request<ImportState>(""));
    } catch (reason) {
      if (!imported) setProgress(current => current?.detection_id === plan.detection_id ? { ...current, status: "error", error: String(reason) } : current);
      else setError(String(reason));
    }
    finally { setImporting(false); }
  };
  const saveSync = async (enabled: boolean, itemTypes: ItemType[]) => {
    setSyncSaving(true); setError("");
    try { setState(await request<ImportState>("/sync", { enabled, item_types: itemTypes }, "PUT")); setCustomize(false); }
    catch (reason) { setError(String(reason)); }
    finally { setSyncSaving(false); }
  };
  const detectedProviders = state?.providers.filter(provider => detection?.items.some(item => item.provider_id === provider.id)) || [];
  const connected = Boolean(state?.connected_provider_ids.length);
  const closeSelection = () => {
    setSelection(null);
    void load();
  };
  const selectionDialog = <ImportSelectionDialog key={detection?.detection_id || "detecting"} text={text} providers={detectedProviders} preselectedProvider={selection?.provider} detection={detection} loading={loading} error={error} busy={importing || Boolean(state?.running_import)} onClose={closeSelection} onRetry={() => void load()} onImport={apply} />;
  const resultDialog = active && progress && <ImportedFilesDialog text={text} progress={progress} onClose={() => { setProgress(null); void load(); }} />;
  if (variant === "general-row") {
    const busy = importing || Boolean(state?.running_import);
    const hasImports = detection?.items.some(item => item.supported) === true;
    const latestResult = state?.history[0];
    const latestImport = state?.history.find(history => history.results.some(result => result.status === "success"));
    const detectionFailed = Boolean(error) && !detection;
    const minutes = latestImport ? Math.floor((Date.now() - Date.parse(latestImport.completed_at)) / 60_000) : 0;
    const relativeTime = new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(
      -(minutes < 60 ? minutes : minutes < 1440 ? Math.floor(minutes / 60) : Math.floor(minutes / 1440)),
      minutes < 60 ? "minute" : minutes < 1440 ? "hour" : "day",
    );
    return <>
      <div className="settings-row">
        <div className="settings-row-copy"><div className="settings-row-title">{latestImport ? text.importedSetup : text.generalTitle}</div>
          <div className="settings-row-description">{latestImport ? text.lastImported.replace("{relativeTime}", relativeTime) : text.generalDescription}</div></div>
        <div className="settings-row-control"><button type="button" className="settings-action-button"
          disabled={loading || busy || (!detectionFailed && !hasImports && !latestResult)} aria-busy={busy}
          onClick={() => { if (detectionFailed) void load(); else if (!hasImports && latestResult) setProgress(completedImport(latestResult, latestResult.id)); else setSelection({}); }}>
          {busy && <LoaderCircle className="animate-spin" size={14} />}
          {busy ? text.importing : loading ? text.generalChecking : detectionFailed ? text.tryAgain : hasImports ? latestImport ? text.importAgain : text.import : latestResult ? text.viewImported : text.noData}
        </button></div>
      </div>
      {active && selection && selectionDialog}
      {resultDialog}
      {error && detection && <p className="settings-import-error" role="alert">{error}</p>}
    </>;
  }
  return <div className="settings-external-import">
    <p className="settings-page-description">{text.subtitle}</p>
    <section className="settings-group"><h3 className="settings-group-title">{text.title}</h3>{detectedProviders.length > 0 && <p>{text.detected}</p>}
      <div className="settings-card">
        {importing || state?.running_import ? <div className="settings-row" role="status"><div className="settings-row-copy"><div className="settings-row-title">{text.importProgress}</div>{state?.running_import && <div className="settings-row-description">{state.running_import.item_types.map(type => text.categories[type]).join("、")}</div>}</div><LoaderCircle className="animate-spin" size={17} /></div> : loading ? <div className="settings-row" role="status"><div className="settings-row-copy"><div className="settings-row-title">{text.checking}</div><div className="settings-row-description">{text.checkingDetail}</div></div><LoaderCircle className="animate-spin" size={17} /></div> : error && !detection ? <div className="settings-row"><p role="alert">{error}</p><button type="button" className="settings-action-button" onClick={() => void load()}>{text.retry}</button></div> : detectedProviders.length === 0 ? <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{state?.history.length ? text.noNew : text.empty}</div><div className="settings-row-description">{text.emptyDetail}</div></div><button type="button" className="settings-action-button" disabled={loading || !state?.history.length} onClick={() => void load()}>{state?.history.length ? text.checkAgain : text.import}</button></div> : detectedProviders.map(source => <div className="settings-row" key={source.id}><div className="settings-row-copy"><div className="settings-row-title">{source.name}</div></div><button type="button" className="settings-action-button" disabled={!detection!.items.some(item => item.provider_id === source.id && item.supported)} aria-label={`${text.import} ${source.name}`} onClick={() => setSelection({ provider: source.id })}>{text.import}</button></div>)}
      </div>
      {detection?.issues.map((issue, index) => <p className="settings-import-issue" key={`${issue.source_path}:${index}`}><strong>{issue.status === "unsupported" ? text.status.unsupported : text.status.error}</strong> · {issue.source_path}<br />{issue.message}</p>)}
    </section>
    {state && <section className="settings-group"><h3 className="settings-group-title">{text.syncTitle}</h3><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{text.sync}</div><div className="settings-row-description">{!connected ? text.afterImport : state.sync.enabled ? text.syncDetail : text.paused}</div></div><button type="button" role="switch" className="settings-toggle" aria-label={text.sync} aria-checked={state.sync.enabled} data-active={state.sync.enabled} disabled={!connected || syncSaving} onClick={() => void saveSync(!state.sync.enabled, state.sync.item_types)}><span /></button></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{text.content}</div><div className="settings-row-description">{connected ? state.sync.item_types.map(type => text.categories[type]).join("、") || text.noneSelected : text.afterImport}</div></div><button type="button" className="settings-action-button" disabled={!connected || syncSaving} onClick={() => setCustomize(true)}>{text.customize}</button></div>
    </div>{state.sync.last_error && <p role="alert">{state.sync.last_error}</p>}</section>}
    {error && detection && <p role="alert">{error}</p>}
    {Boolean(state?.history.length) && <section className="settings-group"><h3 className="settings-group-title">{text.history}</h3><div className="settings-card">{(showAllHistory ? state!.history : state!.history.slice(0, 3)).map(history => <details key={history.id} className="settings-import-history"><summary><span>{new Date(history.completed_at).toLocaleString(locale)}{history.automatic ? ` · ${text.automatic}` : ""}</span><span>{Object.entries(text.status).map(([status, label]) => { const count = history.results.filter(result => result.status === status).length; return count ? `${label} ${count}` : ""; }).filter(Boolean).join(" · ")}</span></summary><div>{history.results.map((result, index) => <p key={`${result.id}:${index}`}><strong>{text.status[result.status]} · {result.title}</strong><br /><span>{result.message || result.target_path}</span></p>)}{history.setup_errors?.map((message, index) => <p key={`setup-${index}`} role="alert">{message}</p>)}</div></details>)}</div>{!showAllHistory && state!.history.length > 3 && <button type="button" className="settings-action-button" onClick={() => setShowAllHistory(true)}>{text.viewMore}</button>}</section>}
    {active && selection && selectionDialog}
    {resultDialog}
    {active && customize && state && <SyncSelectionDialog text={text} itemTypes={state.item_types} selected={state.sync.item_types} busy={syncSaving} onClose={() => setCustomize(false)} onSave={types => void saveSync(state.sync.enabled, types)} />}
  </div>;
}

const TOOL_CATEGORIES: ItemType[] = ["settings", "instructions", "skills", "plugins", "mcp", "agents", "hooks", "commands", "memory"];
const CATEGORY_ICONS = { settings: Braces, instructions: BookOpen, skills: Layers, plugins: AtSign, mcp: Braces, agents: Bot, hooks: Workflow, commands: Terminal, memory: BookOpen };

function ImportSelectionDialog({ text, providers, preselectedProvider, detection, loading, error, busy, onClose, onRetry, onImport }: {
  text: typeof EN; providers: { id: string; name: string }[]; preselectedProvider?: string; detection?: Detection;
  loading: boolean; error: string; busy: boolean; onClose: () => void; onRetry: () => void; onImport: (ids: string[]) => Promise<void>;
}) {
  const [step, setStep] = useState<"providers" | "items">("providers");
  const [chosenProviders, setChosenProviders] = useState(() => new Set(preselectedProvider ? [preselectedProvider] : providers.filter(provider => detection!.items.some(item => item.provider_id === provider.id && item.supported)).map(provider => provider.id)));
  const [selected, setSelected] = useState(() => new Set(detection?.items.filter(item => item.supported).map(item => item.id)));
  const [toolsExpanded, setToolsExpanded] = useState(false);
  const dialog = useFocusTrap(true);
  const toolsId = useId();
  const items = detection?.items.filter(item => chosenProviders.has(item.provider_id)) || [];
  const tools = items.filter(item => !item.workspace_root && TOOL_CATEGORIES.includes(item.item_type));
  const projects = items.filter(item => item.item_type === "projects" || Boolean(item.workspace_root) && item.item_type !== "sessions");
  const chats = items.filter(item => item.item_type === "sessions");
  const projectRoots = [...new Set(projects.map(item => item.workspace_root))];
  const chosenItems = items.filter(item => selected.has(item.id));
  const disabled = busy;
  const toggle = (group: Item[], enabled: boolean) => setSelected(current => {
    const next = new Set(current);
    for (const item of group.filter(item => item.supported)) enabled ? next.add(item.id) : next.delete(item.id);
    return next;
  });
  const paths = (group: Item[]) => group.map(item => `${item.title}\n${item.source_path} → ${item.target_path}${item.reason ? `\n${item.reason}` : ""}`).join("\n\n");
  const info = (title: string, content: string) => <Tip content={<span className="settings-import-tooltip">{content}</span>}><button type="button" className="settings-import-info" aria-label={`${text.info} ${title}`}><Info size={14} /></button></Tip>;
  const title = step === "providers" ? text.dialogTitle : text.review;
  const submit = async () => {
    if (step === "providers") setStep("items");
    else await onImport(chosenItems.map(item => item.id));
  };
  return createPortal(<div ref={dialog} className="mc-project-dialog-backdrop" onClick={() => { if (!busy) onClose(); }}>
    <form role="dialog" aria-modal="true" aria-label={title} className="mc-project-dialog settings-import-dialog" tabIndex={-1} onClick={event => event.stopPropagation()}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!busy) onClose(); } }}
      onSubmit={event => { event.preventDefault(); void submit(); }}>
      <header><h2>{title}</h2><button type="button" className="settings-import-close" aria-label={text.close} disabled={busy} onClick={onClose}><X size={17} /></button></header>
      {step === "items" && <p className="settings-import-subtitle">{text.reviewDetail}</p>}
      {loading ? <div className="settings-import-loading" role="status"><LoaderCircle className="animate-spin" size={18} /><span>{text.checking}</span></div> : step === "providers" ? <div>
        <p className="settings-import-list-label">{text.appsFound}</p>
        <div className="settings-import-providers" role="list" aria-label={text.appsFound}>{providers.map(provider => {
          const supported = detection!.items.some(item => item.provider_id === provider.id && item.supported);
          return <div key={provider.id} role="listitem" className="settings-import-provider"><span className="settings-import-provider-icon" data-provider={provider.id}><ImportProviderIcon provider={provider.id === "claude" ? "claude" : "cursor"} /></span><span>{provider.name}</span>
            <button type="button" role="switch" className="settings-toggle" aria-label={provider.name} aria-checked={chosenProviders.has(provider.id)} data-active={chosenProviders.has(provider.id)} disabled={!supported || busy} onClick={() => setChosenProviders(current => { const next = new Set(current); next.has(provider.id) ? next.delete(provider.id) : next.add(provider.id); return next; })}><span /></button>
          </div>;
        })}</div>{providers.length === 0 && !error && <p className="settings-import-empty">{text.emptyDetail}</p>}
      </div> : <div className="settings-import-content" role="group" aria-label={text.review}>
        {tools.length > 0 && <>
          <ImportChoiceRow title={text.tools} description={text.toolsDetail} icon={<Settings size={20} />} items={tools} selected={selected} disabled={disabled} onChange={enabled => toggle(tools, enabled)}
            trailing={<button type="button" className="settings-import-disclosure" aria-label={toolsExpanded ? text.hideTools : text.showTools} aria-expanded={toolsExpanded} aria-controls={toolsId} onClick={() => setToolsExpanded(!toolsExpanded)}><ChevronDown size={16} data-expanded={toolsExpanded} /></button>} />
          {toolsExpanded && <div id={toolsId} role="group" aria-label={text.tools} className="settings-import-tools">{TOOL_CATEGORIES.map(type => {
            const group = tools.filter(item => item.item_type === type);
            if (!group.length) return null;
            const Icon = CATEGORY_ICONS[type as keyof typeof CATEGORY_ICONS];
            const source = [...new Set(group.map(item => item.source_path))];
            const target = [...new Set(group.map(item => item.target_path))];
            const label = text.categories[type];
            return <ImportChoiceRow key={type} title={label} count={group.length > 1 ? group.length : undefined} description={`${source.join(", ")} → ${target.join(", ")}`} icon={<Icon size={19} />} items={group} selected={selected} disabled={disabled} onChange={enabled => toggle(group, enabled)} trailing={info(label, paths(group))} />;
          })}</div>}
        </>}
        {projects.length > 0 && <ImportChoiceRow title={text.categories.projects} count={projectRoots.length} description={text.projectsDetail} icon={<FolderClosed size={20} />} items={projects} selected={selected} disabled={disabled} onChange={enabled => toggle(projects, enabled)} trailing={info(text.categories.projects, paths(projects))} />}
        {chats.length > 0 && <ImportChoiceRow title={text.categories.sessions} count={chats.length} description={text.chatsDetail} icon={<MessageCircle size={20} />} items={chats} selected={selected} disabled={disabled} onChange={enabled => toggle(chats, enabled)} trailing={info(text.categories.sessions, paths(chats))} />}
      </div>}
      {error && <p className="settings-import-error" role="alert">{error}</p>}
      {detection?.issues.filter(issue => chosenProviders.has(issue.provider_id)).map((issue, index) => <p className="settings-import-issue" key={`${issue.source_path}:${index}`}>{issue.source_path}<br />{issue.message}</p>)}
      <footer>
        <button type="button" className="settings-action-button settings-import-cancel" disabled={busy} onClick={onClose}>{text.cancel}</button>
        {error && !detection ? <button type="button" className="settings-action-button" data-primary="true" onClick={onRetry}>{text.retry}</button> : <button type="submit" className="settings-action-button" data-primary="true" disabled={loading || busy || chosenItems.length === 0}>{busy ? text.importing : step === "providers" ? text.continue : text.importTo}</button>}
      </footer>
    </form>
  </div>, document.body);
}

const IMPORTED_FILE_TYPES: ItemType[] = ["instructions", "settings", "mcp", "agents", "hooks", "commands"];

function ImportedFilesDialog({ text, progress, onClose }: { text: typeof EN; progress: ImportProgress; onClose: () => void }) {
  const dialog = useFocusTrap(true);
  const content = useRef<HTMLDivElement>(null);
  const [showScroll, setShowScroll] = useState(false);
  const [openError, setOpenError] = useState("");
  const running = progress.status === "running";
  const hasErrors = progress.status === "error";
  const hasImports = progress.status === "success";
  const title = running ? text.resultRunningTitle : hasErrors ? text.resultErrorTitle : hasImports ? text.resultTitle : text.resultUnchangedTitle;
  const groups = [...new Set(progress.items.map(item => item.workspace_root))].sort((left, right) => left === "" ? -1 : right === "" ? 1 : 0);
  const updateScroll = () => { const element = content.current!; setShowScroll(element.scrollHeight - element.scrollTop - element.clientHeight > 8); };
  const openImportedFile = async (item: ImportResult) => {
    const owner = progress.workspace_root ?? useAppStore.getState().workingDirectory;
    setOpenError("");
    try {
      const opened = await openManagedSettingsTarget({ kind: "import-item", importId: progress.id!, itemId: item.id, workspaceRoot: owner });
      if (opened && dialog.current) {
        onClose();
        const state = useAppStore.getState();
        if (state.settingsOpen) state.toggleSettings();
      }
    } catch (reason) { setOpenError(reason instanceof Error ? reason.message : String(reason)); }
  };
  useEffect(() => {
    const frame = requestAnimationFrame(updateScroll);
    window.addEventListener("resize", updateScroll);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("resize", updateScroll); };
  }, [progress]);
  return createPortal(<div className="mc-project-dialog-backdrop" onClick={() => { if (!running) onClose(); }}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
      className="mc-project-dialog settings-import-files-dialog" onClick={event => event.stopPropagation()} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!running) onClose(); }
      }}>
      <div className="settings-import-files-scroll"><div ref={content} className="settings-import-files-content" onScroll={updateScroll}>
        <header className="settings-import-files-heading">
          <span className="settings-import-result-icon" data-status={progress.status}>
            {running ? <LoaderCircle size={24} className="animate-spin" /> : <SettingsSourceIcon icon={hasErrors ? "importError" : hasImports ? "importSuccess" : "themeImport"} size={24} />}
          </span>
          <div><h2>{title}</h2><p>{running ? text.resultRunningSubtitle : hasErrors ? text.resultErrorSubtitle : hasImports ? text.resultSubtitle : text.resultUnchangedSubtitle}</p></div>
        </header>
        {groups.map(root => <section className="settings-import-files-group" key={root}>
          <h3>{root ? text.currentProject : text.userConfig}{root && <span>{root}</span>}</h3>
          {progress.items.filter(item => item.workspace_root === root).map(item => {
            const result = progress.results.find(result => result.id === item.id);
            return <div className="settings-import-file-result" key={item.id}>
            <div><strong>{item.title}</strong><div className="settings-import-file-path">
              {!running && result?.status === "success" && IMPORTED_FILE_TYPES.includes(item.item_type)
                ? <a href={result.target_path} onClick={event => {
                  event.preventDefault(); void openImportedFile(result);
                }}>{result.target_path}</a> : <span>{item.target_path}</span>}
            </div>{result?.message && <p>{result.message}</p>}</div>
            {running ? <span className="settings-import-file-status" aria-label={text.importing}><LoaderCircle size={16} className="animate-spin" /></span>
              : result && <span className="settings-import-file-status" data-status={result.status} aria-label={text.status[result.status]}>
                {result.status === "success" ? <SettingsSourceIcon icon="check" size={14} /> : result.status === "error" ? <SettingsSourceIcon icon="importError" size={16} /> : text.status[result.status]}
              </span>}
          </div>; })}
        </section>)}
        {progress.error && <p className="settings-import-error" role="alert">{progress.error}</p>}
        {openError && <p className="settings-import-error" role="alert">{openError}</p>}
        {progress.setup_errors?.map((error, index) => <p key={index} className="settings-import-error" role="alert">{error}</p>)}
      </div>{showScroll && <button type="button" className="settings-import-scroll-bottom" aria-label={text.scrollToBottom} onClick={() => content.current!.scrollTo({ top: content.current!.scrollHeight, behavior: "smooth" })}><ArrowDown size={18} aria-hidden="true" /></button>}</div>
      {hasImports && <footer><button type="button" className="settings-action-button" data-primary="true" onClick={onClose}>{text.ok}</button></footer>}
    </div>
  </div>, document.body);
}

function ImportChoiceRow({ title, count, description, icon, items, selected, disabled, trailing, onChange }: {
  title: string; count?: number; description: string; icon: ReactNode; items: Item[]; selected: Set<string>; disabled: boolean; trailing?: ReactNode; onChange: (checked: boolean) => void;
}) {
  const id = useId();
  const supported = items.filter(item => item.supported);
  const checked = supported.length > 0 && supported.every(item => selected.has(item.id));
  const mixed = !checked && supported.some(item => selected.has(item.id));
  return <div className="settings-import-choice">
    <span className="settings-import-choice-icon">{icon}</span><label className="settings-import-choice-copy" htmlFor={id}><span>{title}{count !== undefined ? ` (${count})` : ""}</span><small title={description}>{description}</small></label>
    {trailing}<input id={id} type="checkbox" aria-label={title} checked={checked} aria-checked={mixed ? "mixed" : checked} ref={element => { if (element) element.indeterminate = mixed; }} disabled={disabled || supported.length === 0} onChange={event => onChange(event.target.checked)} />
  </div>;
}

function SyncSelectionDialog({ text, itemTypes, selected, busy, onClose, onSave }: { text: typeof EN; itemTypes: ItemType[]; selected: ItemType[]; busy: boolean; onClose: () => void; onSave: (types: ItemType[]) => void }) {
  const [types, setTypes] = useState(() => new Set(selected));
  const dialog = useFocusTrap(true);
  return createPortal(<div ref={dialog} className="mc-project-dialog-backdrop"><form role="dialog" aria-modal="true" aria-label={text.content} className="mc-project-dialog settings-import-dialog" tabIndex={-1} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!busy) onClose(); } }} onSubmit={event => { event.preventDefault(); onSave([...types]); }}><h2>{text.content}</h2><div className="settings-import-content">{itemTypes.map(type => <label key={type} className="settings-import-item"><input type="checkbox" disabled={busy} checked={types.has(type)} onChange={event => setTypes(current => { const next = new Set(current); event.target.checked ? next.add(type) : next.delete(type); return next; })} /><span>{text.categories[type]}</span></label>)}</div><footer><button type="button" className="settings-action-button" disabled={busy} onClick={onClose}>{text.cancel}</button><button type="submit" className="settings-action-button" data-primary="true" disabled={busy} >{text.save}</button></footer></form></div>, document.body);
}

export default ImportSettingsTab;
