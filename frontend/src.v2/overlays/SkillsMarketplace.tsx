import { Boxes, Plug, RefreshCw, Search, Sparkles, Trash2, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";

type Category = "skills" | "skills-market" | "mcp" | "mcp-market";

const CATEGORIES: { id: Category; label: string; icon: React.ReactNode }[] = [
  { id: "skills", label: "技能", icon: <Wrench size={14} /> },
  { id: "skills-market", label: "技能市场", icon: <Sparkles size={14} /> },
  { id: "mcp", label: "MCP 服务", icon: <Plug size={14} /> },
  { id: "mcp-market", label: "MCP 市场", icon: <Boxes size={14} /> },
];

export const SkillsMarketplace = () => {
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const availableSkills = useAppStore((s) => s.availableSkills);
  const marketplaceSkills = useAppStore((s) => s.marketplaceSkills);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const marketplaceConnectors = useAppStore((s) => s.marketplaceConnectors);
  const [category, setCategory] = useState<Category>("skills");
  const [query, setQuery] = useState("");
  const [installing, setInstalling] = useState<Set<string>>(new Set());
  const [removing, setRemoving] = useState<Set<string>>(new Set());

  const loadSkills = async () => {
    try {
      const [statusRes, marketplaceRes] = await Promise.all([
        fetch(`${apiBase()}/api/status`, { cache: "no-store", headers: authHeaders() }),
        fetch(`${apiBase()}/api/skills/marketplace`, { cache: "no-store", headers: authHeaders() }),
      ]);
      if (statusRes.ok) {
        const payload = await statusRes.json();
        const skills = Array.isArray(payload.skills) ? payload.skills : [];
        useAppStore.getState().setAvailableSkills(skills.map((skill: any) => ({
          name: String(skill.name ?? ""),
          description: String(skill.description ?? ""),
          version: skill.version ? String(skill.version) : undefined,
          triggers: Array.isArray(skill.triggers) ? skill.triggers.map(String) : [],
          source_level: String(skill.source_level ?? skill.level ?? "builtin"),
          active: Boolean(skill.active),
        })).filter((skill: { name: string }) => skill.name));
      }
      if (marketplaceRes.ok) {
        const payload = await marketplaceRes.json();
        const skills = Array.isArray(payload.skills) ? payload.skills : [];
        useAppStore.getState().setMarketplaceSkills(skills.map((skill: any) => ({
          name: String(skill.name ?? ""),
          title: String(skill.title ?? skill.name ?? ""),
          description: String(skill.description ?? ""),
          triggers: Array.isArray(skill.triggers) ? skill.triggers.map(String) : [],
          installed: Boolean(skill.installed),
        })).filter((skill: { name: string }) => skill.name));
      }
    } catch (error) {
      pushToast(`Failed to load skills: ${error instanceof Error ? error.message : String(error)}`, "warning");
    }
  };

  const refreshAll = () => {
    void loadSkills();
    const ws = getWebSocket();
    ws?.send({ type: "skills.list" });
    ws?.send({ type: "skills.marketplace.list" });
    ws?.send({ type: "mcp.list" });
    sendClientCommand({ type: "connectors.marketplace.list" });
  };

  useEffect(() => {
    if (!skillsMarketplaceOpen) return;
    refreshAll();
  }, [skillsMarketplaceOpen]);

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

  const q = query.toLowerCase();

  const installedFiltered = useMemo(() => (
    q ? availableSkills.filter((s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)) : availableSkills
  ), [availableSkills, q]);

  const discoverFiltered = useMemo(() => (
    q ? marketplaceSkills.filter((s) => s.name.toLowerCase().includes(q) || s.title.toLowerCase().includes(q) || s.description.toLowerCase().includes(q) || s.triggers.some((t) => t.toLowerCase().includes(q))) : marketplaceSkills
  ), [marketplaceSkills, q]);

  const serversFiltered = useMemo(() => (
    q ? mcpServers.filter((s) => s.name.toLowerCase().includes(q)) : mcpServers
  ), [mcpServers, q]);

  const connectorsFiltered = useMemo(() => (
    q ? marketplaceConnectors.filter((c) => c.name.toLowerCase().includes(q) || c.title.toLowerCase().includes(q) || c.description.toLowerCase().includes(q)) : marketplaceConnectors
  ), [marketplaceConnectors, q]);

  if (!skillsMarketplaceOpen) return null;

  const installSkill = async (name: string) => {
    setInstalling((prev) => new Set(prev).add(name));
    try {
      const res = await fetch(`${apiBase()}/api/skills/install`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ skill_name: name }),
      });
      if (!res.ok) throw new Error(await res.text());
      pushToast(`Installed skill: ${name}`, "success");
      await loadSkills();
    } catch (error) {
      pushToast(`Failed to install skill: ${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setInstalling((prev) => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
      getWebSocket()?.send({ type: "skills.list" });
      getWebSocket()?.send({ type: "skills.marketplace.list" });
    }
  };

  const removeSkill = async (name: string) => {
    setRemoving((prev) => new Set(prev).add(name));
    try {
      const res = await fetch(`${apiBase()}/api/skills/${encodeURIComponent(name)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(await res.text());
      pushToast(`Removed skill: ${name}`, "success");
      await loadSkills();
    } catch (error) {
      pushToast(`Failed to remove skill: ${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setRemoving((prev) => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
      getWebSocket()?.send({ type: "skills.list" });
      getWebSocket()?.send({ type: "skills.marketplace.list" });
    }
  };

  const installConnector = (name: string) => {
    sendClientCommand({ type: "connectors.marketplace.install", name });
    pushToast(`Installing connector: ${name}`, "info");
    setTimeout(() => {
      getWebSocket()?.send({ type: "mcp.list" });
      sendClientCommand({ type: "connectors.marketplace.list" });
    }, 1000);
  };

  const counts: Record<Category, number> = {
    skills: availableSkills.length,
    "skills-market": marketplaceSkills.length,
    mcp: mcpServers.length,
    "mcp-market": marketplaceConnectors.length,
  };

  const searchPlaceholder = category === "skills" ? "搜索已安装技能"
    : category === "skills-market" ? "搜索技能市场"
    : category === "mcp" ? "搜索 MCP 服务"
    : "搜索 MCP 市场";

  return (
    <div className="overlay-backdrop" onClick={toggleSkillsMarketplace} style={backdropStyle}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()} style={modalStyle}>
        <div style={headerStyle}>
          <div>
            <h2 style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)", fontWeight: 700 }}>能力中心</h2>
            <div style={{ marginTop: 2, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
              管理技能与 MCP 连接器。用 @skill 或 /skills 在对话里临时挂载技能。
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button onClick={refreshAll} aria-label="刷新" title="刷新" style={closeBtn}><RefreshCw size={15} /></button>
            <button onClick={toggleSkillsMarketplace} aria-label="关闭" style={closeBtn}><X size={16} /></button>
          </div>
        </div>

        <div style={toolbarStyle}>
          <div style={tabBarStyle}>
            {CATEGORIES.map((c) => (
              <button key={c.id} onClick={() => setCategory(c.id)} style={tabStyle(category === c.id)}>
                {c.icon}
                <span>{c.label}</span>
                <span style={countTagStyle(category === c.id)}>{counts[c.id]}</span>
              </button>
            ))}
          </div>
          <div style={searchWrapStyle}>
            <Search size={14} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={searchPlaceholder} style={searchStyle} />
          </div>
        </div>

        <div style={listWrapStyle}>
          {category === "skills" && (
            installedFiltered.length === 0 ? (
              <EmptyState title={availableSkills.length === 0 ? "尚未安装技能" : "无匹配项"} hint="在「技能市场」安装精选技能,或在本地添加 SKILL.md 文件。" />
            ) : (
              <div style={listStyle}>
                {installedFiltered.map((skill) => (
                  <InstalledRow key={skill.name} skill={skill} removing={removing.has(skill.name)} onRemove={() => removeSkill(skill.name)} />
                ))}
              </div>
            )
          )}

          {category === "skills-market" && (
            discoverFiltered.length === 0 ? (
              <EmptyState title={marketplaceSkills.length === 0 ? "市场暂无条目" : "无匹配项"} hint="连接后端后会自动填充精选技能。" />
            ) : (
              <div style={listStyle}>
                {discoverFiltered.map((skill) => (
                  <DiscoverRow key={skill.name} skill={skill} installing={installing.has(skill.name)} onInstall={() => installSkill(skill.name)} />
                ))}
              </div>
            )
          )}

          {category === "mcp" && (
            serversFiltered.length === 0 ? (
              <EmptyState title={mcpServers.length === 0 ? "尚未配置 MCP 服务" : "无匹配项"} hint="在「MCP 市场」一键安装,或在设置 → 连接器里添加自定义服务器。" />
            ) : (
              <div style={listStyle}>
                {serversFiltered.map((server) => (
                  <ServerRow key={server.name} server={server} />
                ))}
              </div>
            )
          )}

          {category === "mcp-market" && (
            connectorsFiltered.length === 0 ? (
              <EmptyState title={marketplaceConnectors.length === 0 ? "市场暂无连接器" : "无匹配项"} hint="连接后端后会自动填充精选 MCP 连接器。" />
            ) : (
              <div style={listStyle}>
                {connectorsFiltered.map((c) => (
                  <ConnectorRow key={c.name} connector={c} onInstall={() => installConnector(c.name)} />
                ))}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
};

const InstalledRow = ({
  skill,
  removing,
  onRemove,
}: {
  skill: { name: string; description: string; version?: string; triggers?: string[]; source_level?: string; active?: boolean };
  removing: boolean;
  onRemove: () => void;
}) => {
  const canRemove = skill.source_level === "global" || skill.source_level === "user";
  const activate = () => {
    useAppStore.getState().addSelectedSkill({
      name: skill.name,
      description: skill.description,
      sourceLevel: skill.source_level,
    });
    getWebSocket()?.send({ type: "load_skill", skill_name: skill.name });
  };
  return (
  <div style={rowStyle}>
    <span style={rowIconStyle}><Wrench size={15} /></span>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7, minWidth: 0 }}>
        <span style={skillNameStyle}>{skill.name}</span>
        {skill.active && <span style={activeTagStyle}>active</span>}
        {skill.source_level && <span style={tagStyle}>{skill.source_level}</span>}
        {skill.version && <span style={tagStyle}>v{skill.version}</span>}
      </div>
      <p style={descStyle}>{skill.description || "(no description)"}</p>
      {skill.triggers && skill.triggers.length > 0 && <TagList tags={skill.triggers} />}
    </div>
    <div style={rowActionsStyle}>
      <button type="button" onClick={activate} style={secondaryButtonStyle}>
        Use
      </button>
      <button
        type="button"
        onClick={onRemove}
        disabled={!canRemove || removing}
        title={canRemove ? `Uninstall ${skill.name}` : "Only user-installed skills can be removed here"}
        style={removeButtonStyle(!canRemove || removing)}
      >
        {removing ? "Removing..." : "Uninstall"}
      </button>
    </div>
  </div>
  );
};

const DiscoverRow = ({ skill, installing, onInstall }: {
  skill: { name: string; title: string; description: string; triggers: string[]; installed: boolean };
  installing: boolean;
  onInstall: () => void;
}) => (
  <div style={rowStyle}>
    <span style={rowIconStyle}><Wrench size={15} /></span>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
        <div style={{ fontWeight: 650, color: "var(--text-primary)", fontSize: "var(--text-sm)" }}>{skill.title}</div>
        <span style={tagStyle}>{skill.name}</span>
      </div>
      <p style={descStyle}>{skill.description}</p>
      {skill.triggers.length > 0 && <TagList tags={skill.triggers} />}
    </div>
    <div style={rowActionsStyle}>
      <button onClick={onInstall} disabled={skill.installed || installing} style={installButtonStyle(skill.installed || installing)}>
        {skill.installed ? "Installed" : installing ? "Installing..." : "Install"}
      </button>
    </div>
  </div>
);

const ServerRow = ({ server }: { server: import("../stores/types").McpServerStatus }) => {
  const phase = server.phase ?? server.status;
  return (
    <div style={rowStyle}>
      <span style={rowIconStyle}><Plug size={15} /></span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, minWidth: 0 }}>
          <span style={statusDotStyle(phase)} />
          <span style={skillNameStyle}>{server.name}</span>
          <span style={tagStyle}>{String(phase).replace(/_/g, " ")}</span>
          <span style={tagStyle}>{server.transport || "stdio"}</span>
          <span style={tagStyle}>{server.tools ?? 0} tools</span>
          {server.requiresUserAction && <span style={warnTagStyle}>需要操作</span>}
        </div>
        {server.lastError && <p style={{ ...descStyle, color: "var(--state-danger)" }}>{server.lastError}</p>}
        {server.requiresUserAction && server.setupHint && <p style={descStyle}>{server.setupHint}</p>}
      </div>
      <div style={rowActionsStyle}>
        <button onClick={() => sendClientCommand({ type: "mcp.restart", name: server.name })} style={iconActionStyle} title="重启" aria-label={`Restart ${server.name}`}><RefreshCw size={14} /></button>
        <button onClick={() => sendClientCommand({ type: "mcp.remove", name: server.name })} style={iconActionStyle} title="移除" aria-label={`Remove ${server.name}`}><Trash2 size={14} /></button>
      </div>
    </div>
  );
};

const ConnectorRow = ({ connector, onInstall }: {
  connector: import("../stores/types").MarketplaceConnector;
  onInstall: () => void;
}) => {
  const authLabel = !connector.auth || connector.auth === "none" ? null : connector.auth === "local_app" ? "local app" : connector.auth.replace(/_/g, " ");
  return (
    <div style={rowStyle}>
      <span style={rowIconStyle}><Boxes size={15} /></span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, minWidth: 0 }}>
          <div style={{ fontWeight: 650, color: "var(--text-primary)", fontSize: "var(--text-sm)" }}>{connector.title}</div>
          <span style={tagStyle}>{connector.transport}</span>
          {authLabel && <span style={tagStyle}>{authLabel}</span>}
          {connector.requiresUserAction && <span style={warnTagStyle}>需要配置</span>}
        </div>
        <p style={descStyle}>{connector.description}</p>
        {connector.setupHint && <p style={descStyle}>{connector.setupHint}</p>}
        {connector.docsUrl && (
          <a href={connector.docsUrl} target="_blank" rel="noreferrer" style={{ ...tagStyle, display: "inline-block", marginTop: 6, color: "var(--accent-primary)" }}>Docs</a>
        )}
      </div>
      <div style={rowActionsStyle}>
        {connector.installed ? (
          <span style={installButtonStyle(true)}>Installed</span>
        ) : (
          <button onClick={onInstall} style={installButtonStyle(false)}>Install</button>
        )}
      </div>
    </div>
  );
};

const TagList = ({ tags }: { tags: string[] }) => (
  <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 7 }}>
    {tags.slice(0, 4).map((t) => <span key={t} style={tagStyle}>{t}</span>)}
  </div>
);

const EmptyState = ({ title, hint }: { title: string; hint: string }) => (
  <div style={{ padding: "30px 16px", textAlign: "center" }}>
    <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", marginBottom: 6 }}>{title}</div>
    <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{hint}</div>
  </div>
);

const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, background: "var(--backdrop-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: "var(--z-modal)", pointerEvents: "auto" };
const modalStyle: React.CSSProperties = { width: "min(820px, 94vw)", maxHeight: "84vh", background: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md, 10px)", boxShadow: "var(--shadow-md)", overflow: "hidden", display: "flex", flexDirection: "column", pointerEvents: "auto" };
const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)" };
const toolbarStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 12, padding: "10px 18px", borderBottom: "1px solid var(--border-subtle)", flexWrap: "wrap" };
const tabBarStyle: React.CSSProperties = { display: "inline-flex", gap: 3, padding: 3, background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)" };
const closeBtn: React.CSSProperties = { background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--text-muted)", cursor: "pointer", width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" };
const searchWrapStyle: React.CSSProperties = { flex: 1, minWidth: 180, height: 34, display: "flex", alignItems: "center", gap: 8, padding: "0 10px", background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 7px)", color: "var(--text-muted)" };
const searchStyle: React.CSSProperties = { width: "100%", background: "transparent", border: 0, color: "var(--text-primary)", fontSize: "var(--text-sm)", outline: "none", boxSizing: "border-box" };
const listWrapStyle: React.CSSProperties = { flex: 1, overflowY: "auto", padding: "12px 18px" };
const listStyle: React.CSSProperties = { display: "grid", gap: 1, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", overflow: "hidden" };
const rowStyle: React.CSSProperties = { display: "flex", alignItems: "flex-start", gap: 11, padding: "11px 12px", background: "var(--surface-soft)", borderBottom: "1px solid var(--border-subtle)" };
const rowIconStyle: React.CSSProperties = { width: 26, height: 26, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: "var(--radius-sm, 6px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)", flexShrink: 0 };
const rowActionsStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 6, flexShrink: 0 };
const skillNameStyle: React.CSSProperties = { fontFamily: "var(--font-mono)", fontWeight: 650, color: "var(--text-primary)", fontSize: "var(--text-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const descStyle: React.CSSProperties = { margin: "4px 0 0", fontSize: "var(--text-xs)", color: "var(--text-secondary)", lineHeight: 1.45 };
const tagStyle: React.CSSProperties = { fontSize: 10, fontFamily: "var(--font-mono)", padding: "1px 6px", borderRadius: "var(--radius-sm, 4px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)" };
const activeTagStyle: React.CSSProperties = { ...tagStyle, color: "var(--state-success)", border: "1px solid color-mix(in oklch, var(--state-success) 35%, var(--border-subtle))" };
const warnTagStyle: React.CSSProperties = { ...tagStyle, color: "var(--state-warning)", border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))" };

const statusDotStyle = (phase: string): React.CSSProperties => {
  const ok = phase === "connected" || phase === "ready";
  const bad = phase === "error" || phase === "offline" || phase === "disconnected";
  return {
    width: 7,
    height: 7,
    borderRadius: "50%",
    flexShrink: 0,
    background: ok ? "var(--state-success)" : bad ? "var(--state-danger)" : "var(--state-warning)",
  };
};

const tabStyle = (active: boolean): React.CSSProperties => ({ height: 30, padding: "0 11px", display: "inline-flex", alignItems: "center", gap: 6, background: active ? "var(--surface-base)" : "transparent", border: 0, borderRadius: "var(--radius-sm, 6px)", color: active ? "var(--text-primary)" : "var(--text-muted)", cursor: "pointer", fontSize: "var(--text-sm)", fontWeight: 650 });
const countTagStyle = (active: boolean): React.CSSProperties => ({ fontSize: 10, fontFamily: "var(--font-mono)", padding: "0 5px", borderRadius: 999, background: active ? "var(--accent-soft, var(--surface-soft))" : "var(--surface-base)", color: active ? "var(--accent-primary)" : "var(--text-muted)", minWidth: 16, textAlign: "center", lineHeight: "16px" });
const installButtonStyle = (disabled: boolean): React.CSSProperties => ({ background: disabled ? "var(--surface-base)" : "var(--accent-primary)", color: disabled ? "var(--text-muted)" : "var(--text-primary)", border: disabled ? "1px solid var(--border-subtle)" : 0, borderRadius: "var(--radius-sm, 4px)", padding: "5px 12px", fontSize: "var(--text-xs)", fontWeight: 600, cursor: disabled ? "default" : "pointer" });
const secondaryButtonStyle: React.CSSProperties = { background: "var(--surface-base)", color: "var(--accent-primary)", border: "1px solid color-mix(in oklch, var(--accent-primary) 35%, var(--border-subtle))", borderRadius: "var(--radius-sm, 4px)", padding: "5px 10px", fontSize: "var(--text-xs)", fontWeight: 600, cursor: "pointer" };
const removeButtonStyle = (disabled: boolean): React.CSSProperties => ({ background: "var(--surface-base)", color: disabled ? "var(--text-muted)" : "var(--state-danger)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", padding: "5px 10px", fontSize: "var(--text-xs)", fontWeight: 600, cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.6 : 1 });
const iconActionStyle: React.CSSProperties = { background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", padding: "5px 7px", cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center" };
