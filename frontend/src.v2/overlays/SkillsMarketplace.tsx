import { ArrowLeft, BookOpenText, Check, FolderOpen, Globe2, RefreshCw, Search, Settings } from "../lib/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import type { MarketplaceSkill } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout, LONG_HTTP_TIMEOUT_MS } from "../protocol/api";
import { SkillIcon } from "../components/SkillIcon";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { isDesktop, pickDirectory } from "../desktop/runtime";
import { openSettings } from "../lib/settings-navigation";
import { selectSkillForComposer } from "../lib/select-skill-for-composer";
import { readLS, writeLS } from "../stores/shared-helpers";
import { showPrompt } from "./DialogService";
import { PluginsTab } from "./PluginsTab";
import { InstalledSkillDetailsDialog } from "./InstalledSkillDetailsDialog";
import { SkillDetailsDialog, type SettingsSkill } from "./SkillDetailsDialog";
import { useSkillSettings } from "./useSkillSettings";
import { pushToast } from "./ToastContainer";
import { captureExtensionSettingsOwner, extensionSettingsOwnerMatches } from "../lib/extension-settings-events";
import "./SkillsMarketplace.css";

const sourceLabels: Record<string, string> = { builtin: "系统", managed: "受管", user: "个人", workspace: "工作区" };
type MenuState = { position: { x: number; y: number }; items: ContextMenuItem[] };
type PublicSkill = MarketplaceSkill & { iconAsset?: string; shortDescription?: string };

export const SkillsMarketplace = () => {
  const open = useAppStore((s) => s.skillsMarketplaceOpen);
  const tab = useAppStore((s) => s.skillsMarketplaceTab);
  const leftSidebarVisible = useAppStore(state => state.leftSidebarWidth > 0);
  const returnTarget = useAppStore((s) => s.skillsMarketplaceReturnTarget);
  const [toolbarHost, setToolbarHost] = useState<HTMLDivElement | null>(null);
  const pageRef = useRef<HTMLElement>(null);
  const close = useCallback(() => {
    useAppStore.getState().toggleSkillsMarketplace();
    if (returnTarget === "settings") openSettings("skills");
  }, [returnTarget]);
  useEffect(() => {
    if (!open) return;
    const trigger = document.activeElement as HTMLElement | null;
    const page = pageRef.current;
    page?.focus();
    return () => {
      if (page?.contains(document.activeElement) || document.activeElement === document.body) trigger?.focus();
    };
  }, [open]);
  const selectTab = (next: "plugins" | "skills") => useAppStore.setState({ skillsMarketplaceTab: next });
  return <main ref={pageRef} className="skills-workspace" aria-label="插件与技能" tabIndex={-1} hidden={!open}
    onKeyDown={(event) => {
      if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
      event.preventDefault(); event.stopPropagation(); close();
    }}>
    <header className="skills-workspace-toolbar">
      <button type="button" className="skills-icon-button" hidden={leftSidebarVisible && returnTarget !== "settings"} onClick={close} aria-label={returnTarget === "settings" ? "返回技能设置" : "返回应用"}><ArrowLeft /></button>
      <div data-sidebar-visible={leftSidebarVisible} className="skills-product-tabs" role="tablist" aria-label="扩展" onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? "plugins" : event.key === "End" ? "skills" : tab === "plugins" ? "skills" : "plugins";
        selectTab(next);
        event.currentTarget.querySelector<HTMLButtonElement>(`#extensions-tab-${next}`)?.focus();
      }}>
        {(["plugins", "skills"] as const).map((item) => <button key={item} type="button" role="tab" id={`extensions-tab-${item}`} aria-controls={`extensions-panel-${item}`} aria-selected={tab === item} tabIndex={tab === item ? 0 : -1} onClick={() => selectTab(item)}>{item === "plugins" ? "插件" : "技能"}</button>)}
      </div>
      <div ref={setToolbarHost} className="skills-toolbar-actions" />
    </header>
    <div className="skills-workspace-scroll" hidden={tab !== "plugins"} role="tabpanel" id="extensions-panel-plugins" aria-labelledby="extensions-tab-plugins">
      <div className="skills-workspace-content"><PluginsTab catalog toolbarHost={toolbarHost} active={open && tab === "plugins"} /></div>
    </div>
    <div className="skills-workspace-scroll" hidden={tab !== "skills"} role="tabpanel" id="extensions-panel-skills" aria-labelledby="extensions-tab-skills">
        <SkillsCatalog active={open && tab === "skills"} />
    </div>
  </main>;
};

const SkillsCatalog = ({ active }: { active: boolean }) => {
  const marketplaceSkills = useAppStore((s) => s.marketplaceSkills) as PublicSkill[];
  const { workspaceRoot, skills: inventory, loading: skillsLoading, error: settingsError, pendingPath, reload: reloadSkills, toggle: toggleSkill, uninstall: uninstallSkill, settingsCommitted } = useSkillSettings(active, undefined, true);
  const [scope, setScope] = useState("");
  const [query, setQuery] = useState("");
  const [overviewExpanded, setOverviewExpanded] = useState(false);
  const [groupExpanded, setGroupExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [operation, setOperation] = useState("");
  const pendingRef = useRef(false);
  const loadAbortRef = useRef<AbortController | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [detail, setDetail] = useState<{ kind: "public"; skill: PublicSkill } | null>(null);
  const [installedDetailPath, setInstalledDetailPath] = useState<string | null>(null);
  useEffect(() => { setInstalledDetailPath(null); }, [workspaceRoot]);
  const loadMarketplace = useCallback(async (force = false) => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    setLoading(true); setLoadError("");
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/skills/marketplace${force ? "?refresh=true" : ""}`, {
        cache: "no-store", headers: authHeaders(), signal: controller.signal,
      }, { timeoutMessage: "技能目录加载超时，请重试。" });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), `目录请求失败（${response.status}）`));
      const payload = await response.json();
      if (controller.signal.aborted) return;
      useAppStore.getState().setMarketplaceSkills((payload.skills ?? []).map((skill: MarketplaceSkill) => ({ ...skill, triggers: skill.triggers ?? [] })));
      const status = payload.source_status?.openai_skills;
      if (status?.ok === false) setLoadError(`OpenAI 技能目录暂不可用${status.error ? `：${status.error}` : ""}`);
      else if (status?.source === "disabled") setLoadError("公开技能目录已由此环境停用，可继续导入和使用本地技能。");
    } catch (error) {
      if (!controller.signal.aborted) setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);
  const refresh = useCallback((force = false) => {
    if (scope === "public") void loadMarketplace(force);
    sendClientCommand({ type: "skills.list" }, { silent: true });
    void reloadSkills();
  }, [loadMarketplace, scope, reloadSkills]);
  useEffect(() => { if (active && scope === "public") void loadMarketplace(); }, [active, scope, loadMarketplace]);
  useEffect(() => () => loadAbortRef.current?.abort(), []);
  useEffect(() => { if (!active) setMenu(null); }, [active]);

  const mutate = async (kind: "install" | "import", name = "") => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setOperation(`${kind}:${name}`);
    const owner = captureExtensionSettingsOwner();
    const headers = authHeaders({ "content-type": "application/json" });
    try {
      let sourcePath: string | null = null;
      if (kind === "import") {
        sourcePath = isDesktop() ? await pickDirectory() : await showPrompt({ title: "导入本地技能", message: "输入后端所在电脑上的技能文件夹路径。", placeholder: "包含 SKILL.md 的文件夹", confirmLabel: "导入" });
        if (!sourcePath?.trim()) return;
      }
      const response = await fetchWithTimeout(`${owner.base}/api/skills/${kind}`, {
        method: "POST", headers,
        body: JSON.stringify(kind === "install" ? { skill_name: name } : { source_path: sourcePath?.trim() }),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "技能操作超时，请刷新查看安装状态。" });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), "技能操作失败"));
      if (extensionSettingsOwnerMatches(owner)) {
        const store = useAppStore.getState();
        store.setMarketplaceSkills(store.marketplaceSkills.map((skill) => skill.name === name ? { ...skill, installed: kind === "install" } : skill));
        if (scope === "public") void loadMarketplace(true);
      }
      await settingsCommitted(owner);
      pushToast(kind === "import" ? "已导入本地技能" : `已安装技能：${name}`, "success");
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally {
      pendingRef.current = false; setOperation("");
    }
  };
  const allInstalled = useMemo(() => {
    const priority: Record<string, number> = { workspace: 0, user: 1, builtin: 2, managed: 3 };
    const byName = new Map<string, SettingsSkill>();
    for (const skill of inventory) {
      const previous = byName.get(skill.name);
      const rank = priority[skill.source_level || ""] ?? 4;
      const previousRank = priority[previous?.source_level || ""] ?? 4;
      if (!previous || rank < previousRank || (rank === previousRank && skill.path.localeCompare(previous.path) < 0)) byName.set(skill.name, skill);
    }
    return [...byName.values()].sort((a, b) => (a.display_name || a.name).localeCompare(b.display_name || b.name) || a.name.localeCompare(b.name));
  }, [inventory]);
  const q = query.trim().toLocaleLowerCase();
  const matches = (text: string) => !q || text.toLocaleLowerCase().includes(q);
  const selected = allInstalled.find(skill => skill.path === installedDetailPath);
  const installed = allInstalled.filter((skill) => matches(`${skill.name} ${skill.display_name ?? ""} ${skill.short_description ?? ""} ${skill.description}`));
  const catalog = marketplaceSkills.filter((skill) => matches(`${skill.name} ${skill.title} ${skill.shortDescription || ""} ${skill.description}`));
  const installedNames = new Set(allInstalled.map((skill) => skill.name));
  const detailInstalled = detail && (installedNames.has(detail.skill.name) || (marketplaceSkills.some((skill) => skill.name === detail.skill.name && skill.installed)));
  const groups = new Map<string, { title: string; skills: SettingsSkill[] }>();
  for (const skill of allInstalled) {
    const id = skill.catalog_scope?.group_id || skill.source_level || "user";
    const title = skill.catalog_scope?.group_name || sourceLabels[skill.source_level || "user"];
    const group = groups.get(id) ?? { title, skills: [] };
    group.skills.push(skill);
    groups.set(id, group);
  }
  const selectedScope = scope === "public" || groups.has(scope) ? scope : groups.keys().next().value ?? "";
  const scoped = groups.get(selectedScope)?.skills ?? [];
  const creator = inventory.find(skill => skill.name === "skill-creator" && skill.source_level === "builtin" && skill.enabled && skill.user_invocable !== false);
  const createSkill = creator ? () => {
    const state = useAppStore.getState();
    state.startConversationDraft({ forceNew: true, workspaceRoot: workspaceRoot || undefined,
      bindWorkspace: Boolean(workspaceRoot), appMode: state.appMode });
    const first = readLS("minicode.skillCreatorPrefillShown") !== "true";
    writeLS("minicode.skillCreatorPrefillShown", "true");
    selectSkillForComposer({ ...creator, default_prompt: first ? "help me create a skill" : "" }, { prefillDefaultPrompt: true });
  } : undefined;
  const showSkillDetails = (skill: SettingsSkill) => { setInstalledDetailPath(skill.path); setMenu(null); };
  const renderInstalled = (skill: SettingsSkill) => <article className="skills-catalog-row" key={`${skill.source_level}:${skill.path ?? skill.name}`}>
    <SkillLogo value={skill.display_name || skill.name} iconUrl={skill.icon_large || skill.icon} />
    <button type="button" className="skills-item-copy skills-item-open" onClick={() => showSkillDetails(skill)} aria-label={`查看技能详情 ${skill.name}`}><span className="skills-item-title"><strong>{skill.display_name || skill.name}</strong>{!skill.enabled && <span>已停用</span>}</span><span className="skills-item-description" title={skill.description}>{skill.short_description || skill.description}</span></button>
    <Check className="skills-installed-check" aria-label="已安装" />
  </article>;
  const showMore = (skills: SettingsSkill[], expanded: boolean, change: (value: boolean) => void) => {
    if (skills.length <= 6) return null;
    const hidden = skills.slice(6);
    const names = hidden.slice(0, 2).map(skill => skill.display_name || skill.name).join(", ");
    const remaining = hidden.length - 2;
    return <button type="button" className="skills-show-more" onClick={() => change(!expanded)}>{expanded ? "收起" : `查看${names}${remaining > 0 ? `，另有 ${remaining} 项` : ""}`}</button>;
  };
  return <div className="skills-workspace-content skills-catalog-content">
    {active && selected && <InstalledSkillDetailsDialog skill={selected} workspaceRoot={selected.catalog_scope?.workspace_root ?? workspaceRoot} pending={Boolean(pendingPath || operation) || skillsLoading} onToggle={() => void toggleSkill(selected)}
      onUninstall={() => uninstallSkill(selected)} onClose={() => setInstalledDetailPath(null)} />}
    {settingsError && <div className="skills-detail-note" role="alert">{settingsError}</div>}
    {active && detail && <SkillDetailsDialog skill={{ name: detail.skill.name, display_name: detail.skill.title,
      description: detail.skill.description, path: detail.skill.path || "", enabled: false }} workspaceRoot="" pending={Boolean(operation)} readOnly
      canOpen={false} canReveal={false} onOpen={() => {}} onReveal={() => {}} onToggle={() => {}} onTry={() => {}} onClose={() => setDetail(null)}
      publicPreview={{ name: detail.skill.name, iconAsset: detail.skill.iconAsset, installed: Boolean(detailInstalled), onInstall: () => void mutate("install", detail.skill.name) }} />}
    <div>
    <header className="skills-catalog-page-header">
      <div className="skills-page-heading"><h1>技能</h1><p>通过任务专用技能扩展 MiniCode</p></div>
      <div className="skills-catalog-actions">
        <label className="skills-search"><Search aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索技能" aria-label="搜索技能" /></label>
        <button type="button" className="skills-icon-button" disabled={loading || skillsLoading} onClick={() => refresh(true)} aria-label="刷新技能" title="刷新技能"><RefreshCw className={loading || skillsLoading ? "settings-spin" : undefined} /></button>
        <button type="button" className="skills-icon-button" onClick={() => openSettings("skills")} aria-label="技能设置" title="技能设置"><Settings /></button>
        <button type="button" className="skills-create-button" disabled={Boolean(operation)} aria-haspopup="menu" aria-expanded={Boolean(menu)} onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setMenu({ position: { x: rect.right - 200, y: rect.bottom + 4 }, items: [
            { label: "创建技能", disabled: !creator, onClick: createSkill },
            { label: "导入本地技能", icon: <FolderOpen />, onClick: () => void mutate("import") },
            { label: "浏览公开技能", icon: <Globe2 />, onClick: () => setScope("public") },
          ] });
        }}>添加</button>
      </div>
    </header>
    {!q && <section className="skills-catalog-section skills-overview" aria-label="已安装技能"><h2>已安装</h2><div className="skills-catalog-grid">{allInstalled.slice(0, overviewExpanded ? undefined : 6).map(renderInstalled)}</div>
      {!skillsLoading && allInstalled.length === 0 && <EmptyState title="尚未安装技能" hint="添加本地技能文件夹。" />}
      {showMore(allInstalled, overviewExpanded, setOverviewExpanded)}
    </section>}
    {!q && <div className="skills-catalog-toolbar"><div className="skills-scope-tabs" role="group" aria-label="技能来源">
      {[...groups].map(([key, group]) => <button key={key} type="button" aria-pressed={selectedScope === key} onClick={() => { setScope(key); setGroupExpanded(false); }}>{group.title}</button>)}
      {(scope === "public" || marketplaceSkills.length > 0) && <button type="button" aria-pressed={selectedScope === "public"} onClick={() => setScope("public")}>推荐</button>}
    </div></div>}
    {q && <section className="skills-catalog-section" aria-label="技能搜索结果"><div className="skills-catalog-grid">{installed.map(renderInstalled)}</div>
      {!skillsLoading && installed.length === 0 && (scope !== "public" || catalog.length === 0) && <EmptyState title="没有匹配的技能" hint="尝试其他搜索词。" />}</section>}
    {selectedScope === "public" ? <section className="skills-catalog-section" aria-label="公开技能">
      {loadError && <div className="skills-error" role="alert"><span>{loadError}</span><button type="button" disabled={loading} onClick={() => refresh(true)}>重试</button></div>}
      <div className="skills-catalog-grid">{catalog.map((skill) => <article className="skills-catalog-row" key={skill.name}>
        <SkillLogo value={skill.title} iconUrl={skill.iconUrl} iconAsset={skill.iconAsset} />
        <button type="button" className="skills-item-copy skills-item-open" aria-label={`查看公开技能详情 ${skill.name}`} onClick={() => setDetail({ kind: "public", skill })}><span className="skills-item-title"><strong>{skill.title}</strong></span><span className="skills-item-description">{skill.shortDescription || skill.description}</span></button>
        <button type="button" className="skills-text-button" disabled={Boolean(operation) || skill.installed || installedNames.has(skill.name)} onClick={() => void mutate("install", skill.name)} aria-label={`安装技能 ${skill.name}`}>{operation === `install:${skill.name}` ? "安装中…" : skill.installed || installedNames.has(skill.name) ? "已安装" : "安装"}</button>
      </article>)}</div>
      {catalog.length === 0 && <EmptyState title={loading ? "正在加载目录" : "没有可显示的技能"} hint={q ? "尝试其他搜索词。" : "目录状态不会影响已安装技能的使用。"} />}
    </section> : !q && scoped.length > 0 && <section className="skills-catalog-section" aria-label={`${groups.get(selectedScope)?.title}技能`}><div className="skills-catalog-grid">{scoped.slice(0, groupExpanded ? undefined : 6).map(renderInstalled)}</div>{showMore(scoped, groupExpanded, setGroupExpanded)}</section>}
    {active && menu && <ContextMenu items={menu.items} position={menu.position} onClose={() => setMenu(null)} />}
    </div>
  </div>;
};

const SkillLogo = ({ value, iconUrl, iconAsset }: { value: string; iconUrl?: string; iconAsset?: string }) => <span className="skills-logo" aria-hidden="true"><SkillIcon value={value} iconUrl={iconUrl} iconAsset={iconAsset} /></span>;
const EmptyState = ({ title, hint }: { title: string; hint: string }) => <div className="skills-empty-state"><BookOpenText aria-hidden="true" /><strong>{title}</strong><span>{hint}</span></div>;
