import {
  ArrowLeft,
  Check,
  FolderOpen,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import type { SkillInfo } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";
import {
  apiBase,
  authHeaders,
  fetchWithTimeout,
  LONG_HTTP_TIMEOUT_MS,
} from "../protocol/api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { BrandIcon } from "../components/BrandIcon";
import { showConfirm } from "./DialogService";
import { openSettings } from "../lib/settings-navigation";
import { pickDirectory } from "../desktop/runtime";
import "./SkillsMarketplace.css";

type Scope = "builtin" | "personal";
type MarketplaceSourceStatus = Record<string, { ok?: boolean; error?: string }>;

// Mirrors the backend's source_level vocabulary
// (backend/skills/loader.py: managed / plugin / user / workspace / builtin).
const sourceLabel = (source?: string) => ({
  managed: "受管",
  plugin: "插件",
  user: "个人",
  workspace: "工作区",
  builtin: "内置",
}[source ?? ""] ?? source ?? "本地");

// `DELETE /api/skills/{name}` is served by `remove_user_skill`, which only
// deletes inside the user skills directory (backend/skills/marketplace.py), i.e.
// the `user` level. Every other source has to be managed where it comes from, so
// the button explains that source instead of claiming everything is 内置.
const removalBlockedReason = (source?: string): string => ({
  builtin: "内置技能不能卸载",
  managed: "受管技能由管理员策略提供，不能在此卸载",
  plugin: "插件提供的技能请在插件中管理",
  workspace: "工作区技能属于项目文件，请在项目中删除",
}[source ?? ""] ?? "该来源的技能不能在此卸载");

const marketplaceLoadWarning = (sourceStatus: MarketplaceSourceStatus | undefined): string => {
  if (!sourceStatus) return "";
  const failed = Object.entries(sourceStatus).filter(([source, status]) => source === "openai_skills" && status?.ok === false);
  if (failed.length === 0) return "";
  return failed.map(([source, status]) => {
    const label = source === "openai_skills" ? "OpenAI 技能目录" : source;
    return `${label} 暂不可用${status.error ? `：${status.error}` : ""}`;
  }).join("；");
};

export const SkillsMarketplace = () => {
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const skillsMarketplaceReturnTarget = useAppStore((s) => s.skillsMarketplaceReturnTarget);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const availableSkills = useAppStore((s) => s.availableSkills);
  const [scope, setScope] = useState<Scope>("builtin");
  const [query, setQuery] = useState("");
  const [removing, setRemoving] = useState<Set<string>>(new Set());
  const [loadState, setLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [loadError, setLoadError] = useState("");
  const [loadWarning, setLoadWarning] = useState("");
  const loadEpochRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const operationControllersRef = useRef(new Map<string, AbortController>());
  const pendingOperationsRef = useRef(new Set<string>());
  const pageRef = useFocusTrap(skillsMarketplaceOpen);

  const closeMarketplace = useCallback(() => {
    const returnToSettings = skillsMarketplaceReturnTarget === "settings";
    toggleSkillsMarketplace();
    if (returnToSettings) openSettings("skills");
  }, [skillsMarketplaceReturnTarget, toggleSkillsMarketplace]);

  const loadMarketplace = useCallback(async (forceRefresh = false, announce = false) => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    const epoch = ++loadEpochRef.current;
    setLoadState("loading");
    setLoadError("");
    setLoadWarning("");
    try {
      const refreshQuery = forceRefresh ? "?refresh=true" : "";
      const marketplaceRes = await fetchWithTimeout(
        `${apiBase()}/api/extensions/marketplace${refreshQuery}`,
        {
          cache: "no-store",
          headers: authHeaders(),
          signal: controller.signal,
        },
        { timeoutMessage: "技能目录加载超时，请重试。" },
      );
      if (!marketplaceRes.ok) throw new Error(`市场请求失败（${marketplaceRes.status}）`);
      const marketplacePayload = await marketplaceRes.json();
      if (epoch !== loadEpochRef.current) return;
      const skills = Array.isArray(marketplacePayload.skills) ? marketplacePayload.skills : [];
      useAppStore.getState().setMarketplaceSkills(skills.map((skill: Record<string, unknown>) => ({
        name: String(skill.name ?? ""),
        title: String(skill.title ?? skill.name ?? ""),
        description: String(skill.description ?? ""),
        triggers: Array.isArray(skill.triggers) ? skill.triggers.map(String) : [],
        installed: Boolean(skill.installed),
        source: String(skill.source ?? ""),
        path: String(skill.path ?? ""),
        iconUrl: String(skill.iconUrl ?? ""),
        websiteUrl: String(skill.websiteUrl ?? ""),
      })).filter((skill: { name: string }) => skill.name));
      setLoadWarning(marketplaceLoadWarning(marketplacePayload.source_status as MarketplaceSourceStatus | undefined));
      setLoadState("ready");
      if (announce) pushToast("技能目录已刷新", "success");
    } catch (error) {
      if (controller.signal.aborted) return;
      if (epoch !== loadEpochRef.current) return;
      const message = `能力数据加载失败：${error instanceof Error ? error.message : String(error)}`;
      setLoadError(message);
      setLoadState("error");
      pushToast(message, "warning");
    }
  }, []);

  const refreshAll = useCallback((forceRefresh = false, announce = false) => {
    void loadMarketplace(forceRefresh, announce);
    sendClientCommand({ type: "skills.list" }, { silent: true });
  }, [loadMarketplace]);

  useEffect(() => {
    if (!skillsMarketplaceOpen) return;
    refreshAll(false);
    return () => {
      loadEpochRef.current += 1;
      loadAbortRef.current?.abort();
      loadAbortRef.current = null;
      for (const controller of operationControllersRef.current.values()) {
        controller.abort();
      }
      operationControllersRef.current.clear();
      pendingOperationsRef.current.clear();
    };
  }, [refreshAll, skillsMarketplaceOpen]);

  useEffect(() => {
    if (!skillsMarketplaceOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMarketplace();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeMarketplace, skillsMarketplaceOpen]);

  const q = query.trim().toLowerCase();
  const installedFiltered = useMemo(() => availableSkills.filter((skill) => (
    skill.source_level !== "builtin" && (!q || `${skill.name} ${skill.display_name ?? ""} ${skill.description}`.toLowerCase().includes(q))
  )), [availableSkills, q]);
  const builtinFiltered = useMemo(() => availableSkills.filter((skill) => (
    skill.source_level === "builtin" && (!q || `${skill.name} ${skill.display_name ?? ""} ${skill.description}`.toLowerCase().includes(q))
  )), [availableSkills, q]);

  if (!skillsMarketplaceOpen) return null;

  const removeSkill = async (name: string) => {
    const confirmed = await showConfirm({
      title: "卸载技能",
      message: `确定卸载 ${name}？本地安装的技能文件会被移除。`,
      confirmLabel: "卸载",
      danger: true,
    });
    if (!confirmed) return;
    const operationKey = `remove:${name}`;
    if (pendingOperationsRef.current.has(operationKey)) return;
    pendingOperationsRef.current.add(operationKey);
    const controller = new AbortController();
    operationControllersRef.current.set(operationKey, controller);
    setRemoving((previous) => new Set(previous).add(name));
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/skills/${encodeURIComponent(name)}`,
        {
          method: "DELETE",
          headers: authHeaders(),
          signal: controller.signal,
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: `卸载技能 ${name} 超时，请重试。`,
        },
      );
      if (!response.ok) throw new Error(await response.text());
      pushToast(`已移除技能：${name}`, "success");
      await loadMarketplace(true, false);
    } catch (error) {
      if (controller.signal.aborted) return;
      pushToast(`技能移除失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      pendingOperationsRef.current.delete(operationKey);
      operationControllersRef.current.delete(operationKey);
      setRemoving((previous) => {
        const next = new Set(previous);
        next.delete(name);
        return next;
      });
      if (!controller.signal.aborted) sendClientCommand({ type: "skills.list" }, { silent: true });
    }
  };

  const importSkill = async () => {
    const sourcePath = await pickDirectory();
    if (!sourcePath) return;
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/skills/import`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ source_path: sourcePath }),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "导入技能超时，请重试。" });
      if (!response.ok) throw new Error(await response.text());
      pushToast("已导入本地技能", "success");
      refreshAll(true, false);
    } catch (error) {
      pushToast(`技能导入失败：${error instanceof Error ? error.message : String(error)}`, "error");
    }
  };

  const installedItems = availableSkills;
  const resultCount = scope === "builtin" ? builtinFiltered.length : installedFiltered.length;
  const loadingMarketplace = loadState === "loading";
  const failedMarketplace = loadState === "error";
  const catalogSourceCount = availableSkills.length;
  // A refresh must not blank already usable backend state. Keep cached rows
  // interactive while the HTTP catalog and websocket status refresh in the
  // background; reserve the blocking loader for a true first load.
  const blockingMarketplaceLoad = loadingMarketplace && catalogSourceCount === 0;

  return (
    <main ref={pageRef} className="skills-workspace" role="dialog" aria-modal="true" aria-label="技能" tabIndex={-1}>
      <header className="skills-workspace-toolbar">
        <button type="button" className="skills-icon-button skills-back" onClick={closeMarketplace} aria-label={skillsMarketplaceReturnTarget === "settings" ? "返回技能设置" : "返回应用"} title={skillsMarketplaceReturnTarget === "settings" ? "返回技能设置" : "返回应用"}>
          <ArrowLeft />
        </button>
        <strong className="skills-workspace-title">技能</strong>
        <div className="skills-toolbar-actions">
          <button type="button" className="skills-icon-button" onClick={() => void importSkill()} aria-label="导入本地技能" title="导入本地技能"><FolderOpen /></button>
          <button type="button" className="skills-icon-button" onClick={() => refreshAll(true, true)} disabled={loadingMarketplace} aria-label="刷新" title="刷新"><RefreshCw className={loadingMarketplace ? "settings-spin" : undefined} /></button>
        </div>
      </header>

      <div className="skills-workspace-scroll">
        <div className="skills-workspace-content">
          <header className="skills-page-heading">
            <h1>技能</h1>
            <p>按需加载 SKILL.md 中的专门知识与工作流。</p>
          </header>

          <label className="skills-search">
            <Search aria-hidden="true" />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索技能" aria-label="搜索技能" />
            {query && <span>{resultCount} 项</span>}
          </label>

          <section className="skills-installed-summary" aria-label="已安装摘要">
            <div className="skills-section-title-row">
              <h2>已安装</h2>
            </div>
            {installedItems.length > 0 ? (
              <div className="skills-icon-strip">
                {installedItems.slice(0, 8).map((item, index) => (
              <button key={item.name} type="button" className={`skills-logo skills-logo-${index % 5}`} title={item.name} onClick={() => setScope(item.source_level === "builtin" ? "builtin" : "personal")}>
                    <BrandIcon
                      value={`${item.name} ${item.display_name || ""}`}
                      iconUrl={item.icon}
                      fallback="skill"
                      size={20}
                    />
                  </button>
                ))}
                {installedItems.length > 8 && <span className="skills-installed-more">+{installedItems.length - 8}</span>}
              </div>
            ) : (
              <p className="skills-installed-empty">还没有已安装的技能</p>
            )}
          </section>

          <div className="skills-catalog-toolbar">
            <div className="skills-scope-tabs" role="tablist" aria-label="来源">
              <button type="button" role="tab" aria-selected={scope === "builtin"} onClick={() => setScope("builtin")}>内置</button>
              <button type="button" role="tab" aria-selected={scope === "personal"} onClick={() => setScope("personal")}>本地</button>
            </div>
          </div>

          {loadWarning && !failedMarketplace && <div className="skills-warning" role="status">{loadWarning}，当前显示可用的本地精选内容。</div>}
          {blockingMarketplaceLoad && <EmptyState title="正在加载" hint="正在同步技能和安装状态。" />}
          {failedMarketplace && (
            <div className="skills-error" role="alert">
              <span>{loadError}</span>
              <button type="button" onClick={() => refreshAll(true)}>重试</button>
            </div>
          )}
          {!blockingMarketplaceLoad && !failedMarketplace && scope === "builtin" && (
            <CatalogSection title="MiniCode 内置">
              {builtinFiltered.length > 0 ? builtinFiltered.map((skill, index) => <InstalledRow key={skill.name} skill={skill} index={index} removing={false} onRemove={() => undefined} />) : <EmptyState title="暂无内置技能" hint="内置技能随 MiniCode 一起提供。" />}
            </CatalogSection>
          )}
          {!blockingMarketplaceLoad && !failedMarketplace && scope === "personal" && (
            <CatalogSection title="已安装">
              {installedFiltered.length > 0
                ? installedFiltered.map((skill, index) => <InstalledRow key={skill.name} skill={skill} index={index} removing={removing.has(skill.name)} onRemove={() => void removeSkill(skill.name)} />)
                : <EmptyState title={availableSkills.length === 0 ? "尚未导入技能" : "没有匹配的技能"} hint="点击右上角文件夹图标导入本地 SKILL.md。" />}
            </CatalogSection>
          )}
        </div>
      </div>
    </main>
  );
};

const CatalogSection = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <section className="skills-catalog-section">
    <h2>{title}</h2>
    <div className="skills-catalog-grid">{children}</div>
  </section>
);

const ItemLogo = ({
  index,
  kind,
  value,
  iconUrl,
  websiteUrl,
}: {
  index: number;
  kind: "plugin" | "skill";
  value: string;
  iconUrl?: string;
  websiteUrl?: string;
}) => (
  <span className={`skills-logo skills-logo-${index % 5}`} aria-hidden="true">
    <BrandIcon
      value={value}
      fallback={kind === "plugin" ? "plugin" : "skill"}
      size={22}
      iconUrl={iconUrl}
      websiteUrl={websiteUrl}
    />
  </span>
);

const InstalledRow = ({ skill, removing, onRemove, index }: { skill: SkillInfo; removing: boolean; onRemove: () => void; index: number }) => {
  const canRemove = skill.source_level === "user";
  const blockedReason = canRemove ? "" : removalBlockedReason(skill.source_level);
  const activate = () => {
    useAppStore.getState().addSelectedSkill({ name: skill.name, path: skill.path, description: skill.description, sourceLevel: skill.source_level });
  };
  return (
    <article className="skills-catalog-row">
      <ItemLogo index={index} kind="skill" value={skill.display_name || skill.name} iconUrl={skill.icon} />
      <div className="skills-item-copy">
        <div className="skills-item-title">
          <strong>{skill.display_name || skill.name}</strong>
          {skill.active && <span className="skills-state-label"><Check />已启用</span>}
          {skill.source_level && <span>{sourceLabel(skill.source_level)}</span>}
        </div>
        <p>{skill.description || "暂无说明"}</p>
      </div>
      <div className="skills-item-actions">
        <button type="button" className="skills-text-button" onClick={activate}>使用</button>
        <button type="button" className="skills-icon-button" onClick={onRemove} disabled={!canRemove || removing} aria-label={canRemove ? `卸载技能 ${skill.name}` : `${skill.name}：${blockedReason}`} title={canRemove ? "卸载" : blockedReason}>{removing ? <RefreshCw className="settings-spin" /> : <Trash2 />}</button>
      </div>
    </article>
  );
};


const EmptyState = ({ title, hint }: { title: string; hint: string }) => (
  <div className="skills-empty-state">
    <Sparkles aria-hidden="true" />
    <strong>{title}</strong>
    <span>{hint}</span>
  </div>
);
