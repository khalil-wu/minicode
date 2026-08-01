import {
  ArrowLeft,
  Blend,
  Check,
  CircleEllipsis,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import type { MarketplaceSkill, SkillInfo } from "../stores/types";
import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { BrandIcon } from "../components/BrandIcon";
import "./SkillsMarketplace.css";

type Scope = "public" | "personal";
type MarketplaceSourceStatus = Record<string, { ok?: boolean; error?: string }>;

const sourceLabel = (source?: string) => ({
  global: "全局",
  user: "个人",
  workspace: "工作区",
  project: "项目",
  builtin: "内置",
}[source ?? ""] ?? source);

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
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const availableSkills = useAppStore((s) => s.availableSkills);
  const marketplaceSkills = useAppStore((s) => s.marketplaceSkills);
  const [scope, setScope] = useState<Scope>("public");
  const [query, setQuery] = useState("");
  const [installing, setInstalling] = useState<Set<string>>(new Set());
  const [removing, setRemoving] = useState<Set<string>>(new Set());
  const [loadState, setLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [loadError, setLoadError] = useState("");
  const [loadWarning, setLoadWarning] = useState("");
  const loadEpochRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const pageRef = useFocusTrap(skillsMarketplaceOpen);

  const loadMarketplace = useCallback(async (forceRefresh = false) => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    const epoch = ++loadEpochRef.current;
    setLoadState("loading");
    setLoadError("");
    setLoadWarning("");
    try {
      const refreshQuery = forceRefresh ? "?refresh=true" : "";
      const marketplaceRes = await fetch(`${apiBase()}/api/extensions/marketplace${refreshQuery}`, {
        cache: "no-store",
        headers: authHeaders(),
        signal: controller.signal,
      });
      if (!marketplaceRes.ok) throw new Error(`市场请求失败（${marketplaceRes.status}）`);
      const marketplacePayload = await marketplaceRes.json();
      if (epoch !== loadEpochRef.current) return;
      const skills = Array.isArray(marketplacePayload.skills) ? marketplacePayload.skills : [];
      useAppStore.getState().setMarketplaceSkills(skills.map((skill: any) => ({
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
    } catch (error) {
      if (controller.signal.aborted) return;
      if (epoch !== loadEpochRef.current) return;
      const message = `能力数据加载失败：${error instanceof Error ? error.message : String(error)}`;
      setLoadError(message);
      setLoadState("error");
      pushToast(message, "warning");
    }
  }, []);

  const refreshAll = useCallback((forceRefresh = false) => {
    void loadMarketplace(forceRefresh);
    const ws = getWebSocket();
    ws?.send({ type: "skills.list" });
  }, [loadMarketplace]);

  useEffect(() => {
    if (!skillsMarketplaceOpen) return;
    refreshAll(false);
    return () => {
      loadEpochRef.current += 1;
      loadAbortRef.current?.abort();
      loadAbortRef.current = null;
    };
  }, [refreshAll, skillsMarketplaceOpen]);

  useEffect(() => {
    if (!skillsMarketplaceOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        toggleSkillsMarketplace();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [skillsMarketplaceOpen, toggleSkillsMarketplace]);

  const q = query.trim().toLowerCase();
  const installedFiltered = useMemo(() => availableSkills.filter((skill) => (
    !q || `${skill.name} ${skill.display_name ?? ""} ${skill.description}`.toLowerCase().includes(q)
  )), [availableSkills, q]);
  const discoverFiltered = useMemo(() => marketplaceSkills.filter((skill) => (
    !q || `${skill.name} ${skill.title} ${skill.description} ${skill.triggers.join(" ")}`.toLowerCase().includes(q)
  )), [marketplaceSkills, q]);

  if (!skillsMarketplaceOpen) return null;

  const installSkill = async (name: string) => {
    setInstalling((previous) => new Set(previous).add(name));
    try {
      const response = await fetch(`${apiBase()}/api/skills/install`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ skill_name: name }),
      });
      if (!response.ok) throw new Error(await response.text());
      pushToast(`已安装技能：${name}`, "success");
      await loadMarketplace(true);
    } catch (error) {
      pushToast(`技能安装失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setInstalling((previous) => {
        const next = new Set(previous);
        next.delete(name);
        return next;
      });
      getWebSocket()?.send({ type: "skills.list" });
    }
  };

  const removeSkill = async (name: string) => {
    setRemoving((previous) => new Set(previous).add(name));
    try {
      const response = await fetch(`${apiBase()}/api/skills/${encodeURIComponent(name)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(await response.text());
      pushToast(`已移除技能：${name}`, "success");
      await loadMarketplace(true);
    } catch (error) {
      pushToast(`技能移除失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setRemoving((previous) => {
        const next = new Set(previous);
        next.delete(name);
        return next;
      });
      getWebSocket()?.send({ type: "skills.list" });
    }
  };

  const installedItems = availableSkills;
  const resultCount = scope === "public" ? discoverFiltered.length : installedFiltered.length;
  const loadingMarketplace = loadState === "loading";
  const failedMarketplace = loadState === "error";
  const catalogSourceCount = scope === "public" ? marketplaceSkills.length : availableSkills.length;
  // A refresh must not blank already usable backend state. Keep cached rows
  // interactive while the HTTP catalog and websocket status refresh in the
  // background; reserve the blocking loader for a true first load.
  const blockingMarketplaceLoad = loadingMarketplace && catalogSourceCount === 0;

  return (
    <main ref={pageRef} className="skills-workspace" role="dialog" aria-modal="true" aria-label="技能" tabIndex={-1}>
      <header className="skills-workspace-toolbar">
        <button type="button" className="skills-icon-button skills-back" onClick={toggleSkillsMarketplace} aria-label="返回应用" title="返回应用">
          <ArrowLeft />
        </button>
        <strong className="skills-workspace-title">技能</strong>
        <div className="skills-toolbar-actions">
          <button type="button" className="skills-icon-button" onClick={() => refreshAll(true)} aria-label="刷新" title="刷新"><RefreshCw /></button>
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
                  <button key={item.name} type="button" className={`skills-logo skills-logo-${index % 5}`} title={item.name} onClick={() => setScope("personal")}>
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
              <button type="button" role="tab" aria-selected={scope === "public"} onClick={() => setScope("public")}>公开</button>
              <button type="button" role="tab" aria-selected={scope === "personal"} onClick={() => setScope("personal")}>个人</button>
            </div>
            <button type="button" className="skills-icon-button" aria-label="筛选" title="筛选当前列表"><CircleEllipsis /></button>
          </div>

          {loadWarning && !failedMarketplace && <div className="skills-warning" role="status">{loadWarning}，当前显示可用的本地精选内容。</div>}
          {blockingMarketplaceLoad && <EmptyState title="正在加载" hint="正在同步技能和安装状态。" />}
          {failedMarketplace && (
            <div className="skills-error" role="alert">
              <span>{loadError}</span>
              <button type="button" onClick={() => refreshAll(true)}>重试</button>
            </div>
          )}
          {!blockingMarketplaceLoad && !failedMarketplace && scope === "public" && (
            <CatalogSections
              items={discoverFiltered}
              emptyTitle={marketplaceSkills.length === 0 ? "市场暂无技能" : "没有匹配的技能"}
              renderItem={(skill, index) => <DiscoverRow key={skill.name} skill={skill} index={index} installing={installing.has(skill.name)} onInstall={() => void installSkill(skill.name)} />}
            />
          )}
          {!blockingMarketplaceLoad && !failedMarketplace && scope === "personal" && (
            <CatalogSection title="已安装">
              {installedFiltered.length > 0
                ? installedFiltered.map((skill, index) => <InstalledRow key={skill.name} skill={skill} index={index} removing={removing.has(skill.name)} onRemove={() => void removeSkill(skill.name)} />)
                : <EmptyState title={availableSkills.length === 0 ? "尚未安装技能" : "没有匹配的技能"} hint="从公开目录安装技能，或在本地添加 SKILL.md。" />}
            </CatalogSection>
          )}
        </div>
      </div>
    </main>
  );
};

const CatalogSections = <T extends { name: string }>({
  items,
  emptyTitle,
  renderItem,
}: {
  items: T[];
  emptyTitle: string;
  renderItem: (item: T, index: number) => React.ReactNode;
}) => {
  if (items.length === 0) return <EmptyState title={emptyTitle} hint="调整搜索条件，或稍后刷新目录。" />;
  const splitAt = Math.min(4, items.length);
  return (
    <>
      <CatalogSection title="精选">{items.slice(0, splitAt).map(renderItem)}</CatalogSection>
      {items.length > splitAt && <CatalogSection title="效率工具">{items.slice(splitAt).map((item, index) => renderItem(item, index + splitAt))}</CatalogSection>}
    </>
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
  const canRemove = skill.source_level === "global" || skill.source_level === "user";
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
        <button type="button" className="skills-icon-button" onClick={onRemove} disabled={!canRemove || removing} aria-label={`卸载技能 ${skill.name}`} title={canRemove ? "卸载" : "内置技能不能卸载"}><Trash2 /></button>
      </div>
    </article>
  );
};

const DiscoverRow = ({ skill, installing, onInstall, index }: { skill: MarketplaceSkill; installing: boolean; onInstall: () => void; index: number }) => (
  <article className="skills-catalog-row">
    <ItemLogo
      index={index}
      kind="skill"
      value={`${skill.title} ${skill.name} ${skill.source || ""}`}
      iconUrl={skill.iconUrl}
      websiteUrl={skill.websiteUrl}
    />
    <div className="skills-item-copy">
      <div className="skills-item-title"><strong>{skill.title}</strong>{skill.name !== skill.title && <span>{skill.name}</span>}</div>
      <p>{skill.description || "暂无说明"}</p>
    </div>
    <div className="skills-item-actions">
      <button type="button" className="skills-text-button" onClick={onInstall} disabled={skill.installed || installing}>
        {skill.installed ? "已安装" : installing ? "安装中…" : "安装"}
      </button>
    </div>
  </article>
);


const EmptyState = ({ title, hint }: { title: string; hint: string }) => (
  <div className="skills-empty-state">
    <Blend aria-hidden="true" />
    <strong>{title}</strong>
    <span>{hint}</span>
  </div>
);
