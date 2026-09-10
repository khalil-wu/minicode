import { ArrowLeft, BookOpenText, Check, ChevronDown, FolderOpen, Globe2, MessageSquarePlus, MoreHorizontal, RefreshCw, Search, Settings, Trash2 } from "../lib/icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useAppStore } from "../stores";
import type { MarketplaceSkill, SkillInfo } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout, LONG_HTTP_TIMEOUT_MS } from "../protocol/api";
import { BrandIcon } from "../components/BrandIcon";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { isDesktop, pickDirectory } from "../desktop/runtime";
import { openSettings } from "../lib/settings-navigation";
import { selectSkillForComposer } from "../lib/select-skill-for-composer";
import { showAlert, showConfirm, showPrompt } from "./DialogService";
import { PluginsTab } from "./PluginsTab";
import { pushToast } from "./ToastContainer";
import "./SkillsMarketplace.css";

const sourceLabels: Record<string, string> = { builtin: "系统", managed: "受管", plugin: "插件", user: "个人", workspace: "工作区" };
type MenuState = { kind: "add" | "skill"; position: { x: number; y: number }; items: ContextMenuItem[] };

export const SkillsMarketplace = () => {
  const open = useAppStore((s) => s.skillsMarketplaceOpen);
  const tab = useAppStore((s) => s.skillsMarketplaceTab);
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
      <button type="button" className="skills-icon-button" onClick={close} aria-label={returnTarget === "settings" ? "返回技能设置" : "返回应用"}><ArrowLeft /></button>
      <div className="skills-product-tabs" role="tablist" aria-label="扩展" onKeyDown={(event) => {
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
        <SkillsCatalog toolbarHost={toolbarHost} active={open && tab === "skills"} />
    </div>
  </main>;
};

const SkillsCatalog = ({ toolbarHost, active }: { toolbarHost: HTMLElement | null; active: boolean }) => {
  const availableSkills = useAppStore((s) => s.availableSkills);
  const marketplaceSkills = useAppStore((s) => s.marketplaceSkills);
  const [scope, setScope] = useState("builtin");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [operation, setOperation] = useState("");
  const pendingRef = useRef(false);
  const loadAbortRef = useRef<AbortController | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const loadMarketplace = useCallback(async (force = false) => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    setLoading(true); setLoadError("");
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/extensions/marketplace${force ? "?refresh=true" : ""}`, {
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
  }, [loadMarketplace, scope]);
  useEffect(() => { if (active) refresh(); }, [active, refresh]);
  useEffect(() => () => loadAbortRef.current?.abort(), []);
  useEffect(() => { if (!active) setMenu(null); }, [active]);

  const mutate = async (kind: "install" | "remove" | "import", name = "") => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setOperation(`${kind}:${name}`);
    try {
      let sourcePath: string | null = null;
      if (kind === "remove" && !await showConfirm({ title: "卸载技能", message: `确定卸载 ${name}？本地技能文件会被移除。`, confirmLabel: "卸载", danger: true })) return;
      if (kind === "import") {
        sourcePath = isDesktop() ? await pickDirectory() : await showPrompt({ title: "导入本地技能", message: "输入后端所在电脑上的技能文件夹路径。", placeholder: "包含 SKILL.md 的文件夹", confirmLabel: "导入" });
        if (!sourcePath?.trim()) return;
      }
      const response = await fetchWithTimeout(`${apiBase()}/api/skills/${kind === "remove" ? encodeURIComponent(name) : kind}`, {
        method: kind === "remove" ? "DELETE" : "POST", headers: authHeaders({ "content-type": "application/json" }),
        ...(kind !== "remove" ? { body: JSON.stringify(kind === "install" ? { skill_name: name } : { source_path: sourcePath?.trim() }) } : {}),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "技能操作超时，请刷新查看安装状态。" });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), "技能操作失败"));
      useAppStore.getState().setMarketplaceSkills(useAppStore.getState().marketplaceSkills.map((skill) => skill.name === name ? { ...skill, installed: kind === "install" } : skill));
      pushToast(kind === "remove" ? `已卸载技能：${name}` : kind === "import" ? "已导入本地技能" : `已安装技能：${name}`, "success");
      refresh(true);
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally {
      pendingRef.current = false; setOperation("");
    }
  };
  const q = query.trim().toLocaleLowerCase();
  const matches = (text: string) => !q || text.toLocaleLowerCase().includes(q);
  const installed = availableSkills.filter((skill) => matches(`${skill.name} ${skill.display_name ?? ""} ${skill.description}`));
  const scoped = installed.filter((skill) => skill.source_level === scope);
  const catalog = marketplaceSkills.filter((skill) => matches(`${skill.name} ${skill.title} ${skill.description}`));
  const installedNames = new Set(availableSkills.map((skill) => skill.name));
  const scopes = Object.entries(sourceLabels).filter(([key]) => key === "builtin" || key === "user" || key === scope || availableSkills.some((skill) => skill.source_level === key));
  const showSkillDetails = (skill: SkillInfo) => void showAlert({ title: skill.display_name || skill.name, message: `${skill.description}\n\n来源：${sourceLabels[skill.source_level ?? ""] ?? skill.source_level ?? "本地"}${skill.path ? `\n${skill.path}` : ""}${skill.user_invocable === false ? "\n此技能由模型按需调用。" : ""}` });
  const skillMenu = (skill: SkillInfo, target: HTMLButtonElement) => {
    const rect = target.getBoundingClientRect();
    setMenu({ kind: "skill", position: { x: rect.right - 200, y: rect.bottom + 4 }, items: [
      { label: "用于下一条消息", disabled: skill.user_invocable === false, icon: <MessageSquarePlus />, onClick: () => selectSkillForComposer(skill) },
      { label: "查看技能详情", onClick: () => showSkillDetails(skill) },
      ...(skill.source_level === "user" ? [{ label: "卸载技能", danger: true, disabled: Boolean(operation), icon: <Trash2 />, onClick: () => void mutate("remove", skill.name) }] : []),
      ...(skill.source_level === "plugin" ? [{ label: "管理所属插件", onClick: () => useAppStore.setState({ skillsMarketplaceTab: "plugins" }) }] : []),
    ] });
  };
  const renderInstalled = (skill: SkillInfo) => <article className="skills-catalog-row" key={`${skill.source_level}:${skill.path ?? skill.name}`}>
    <SkillLogo value={skill.display_name || skill.name} iconUrl={skill.icon_large || skill.icon} />
    <button type="button" className="skills-item-copy skills-item-open" onClick={() => showSkillDetails(skill)} aria-label={`查看技能详情 ${skill.name}`}><span className="skills-item-title"><strong>{skill.display_name || skill.name}</strong>{skill.active && <span>本轮已加载</span>}</span><span className="skills-item-description" title={skill.description}>{skill.short_description || skill.description}</span></button>
    <div className="skills-row-end"><Check className="skills-installed-check" aria-label="已安装" /><button type="button" className="skills-icon-button" onClick={(event) => skillMenu(skill, event.currentTarget)} aria-label={`管理技能 ${skill.name}`} aria-haspopup="menu"><MoreHorizontal /></button></div>
  </article>;
  return <div className="skills-workspace-content">
    {active && toolbarHost && createPortal(<>
      <button type="button" className="skills-icon-button" disabled={loading} onClick={() => refresh(true)} aria-label="刷新技能" title="刷新技能"><RefreshCw className={loading ? "settings-spin" : undefined} /></button>
      <button type="button" className="skills-icon-button" onClick={() => openSettings("skills")} aria-label="技能设置" title="技能设置"><Settings /></button>
      <button type="button" className="skills-create-button" disabled={Boolean(operation)} aria-haspopup="menu" aria-expanded={menu?.kind === "add"} onClick={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        setMenu({ kind: "add", position: { x: rect.right - 200, y: rect.bottom + 4 }, items: [{ label: "导入本地技能", icon: <FolderOpen />, onClick: () => void mutate("import") }, { label: "浏览公开技能", icon: <Globe2 />, onClick: () => setScope("public") }] });
      }}>添加 <ChevronDown /></button>
    </>, toolbarHost)}
    <header className="skills-page-heading"><h1>技能</h1><p>通过任务专用技能扩展 MiniCode</p></header>
    <label className="skills-search"><Search aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索技能" aria-label="搜索技能" /></label>
    <section className="skills-catalog-section" aria-label="已安装技能"><h2>已安装</h2><div className="skills-catalog-grid">{installed.slice(0, expanded ? undefined : 6).map(renderInstalled)}</div>
      {installed.length === 0 && <EmptyState title={q ? "没有匹配的技能" : "尚未安装技能"} hint="从公开目录安装，或添加本地技能文件夹。" />}
      {installed.length > 6 && <button type="button" className="skills-show-more" onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : `查看另外 ${installed.length - 6} 项`}</button>}
    </section>
    <div className="skills-catalog-toolbar"><div className="skills-scope-tabs" role="group" aria-label="技能来源">
      {scopes.map(([key, label]) => <button key={key} type="button" aria-pressed={scope === key} onClick={() => setScope(key)}>{label}</button>)}
      <button type="button" aria-pressed={scope === "public"} onClick={() => setScope("public")}>公开</button>
    </div></div>
    {scope === "public" ? <section className="skills-catalog-section" aria-label="公开技能"><h2>OpenAI 技能目录</h2>
      {loadError && <div className="skills-error" role="alert"><span>{loadError}</span><button type="button" disabled={loading} onClick={() => refresh(true)}>重试</button></div>}
      <div className="skills-catalog-grid">{catalog.map((skill) => <article className="skills-catalog-row" key={skill.name}>
        <SkillLogo value={skill.title} iconUrl={skill.iconUrl} />
        <div className="skills-item-copy"><div className="skills-item-title"><strong>{skill.title}</strong></div><p title={skill.description}>{skill.description}</p></div>
        <button type="button" className="skills-text-button" disabled={Boolean(operation) || skill.installed || installedNames.has(skill.name)} onClick={() => void mutate("install", skill.name)} aria-label={`安装技能 ${skill.name}`}>{operation === `install:${skill.name}` ? "安装中…" : skill.installed || installedNames.has(skill.name) ? "已安装" : "安装"}</button>
      </article>)}</div>
      {catalog.length === 0 && <EmptyState title={loading ? "正在加载目录" : "没有可显示的技能"} hint={q ? "尝试其他搜索词。" : "目录状态不会影响已安装技能的使用。"} />}
    </section> : <section className="skills-catalog-section" aria-label={`${sourceLabels[scope]}技能`}><div className="skills-catalog-grid">{scoped.map(renderInstalled)}</div>{scoped.length === 0 && <EmptyState title="没有匹配的技能" hint="选择其他来源或添加技能。" />}</section>}
    {active && menu && <ContextMenu items={menu.items} position={menu.position} onClose={() => setMenu(null)} />}
  </div>;
};

const SkillLogo = ({ value, iconUrl }: { value: string; iconUrl?: string }) => <span className="skills-logo" aria-hidden="true"><BrandIcon value={value} iconUrl={iconUrl} inferBrand={false} fallback="skill" size={32} /></span>;
const EmptyState = ({ title, hint }: { title: string; hint: string }) => <div className="skills-empty-state"><BookOpenText aria-hidden="true" /><strong>{title}</strong><span>{hint}</span></div>;
