import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import {
  secondaryActionStyle,
  subTabBarStyle,
  subTabStyle,
  subTabCountStyle,
  emptyInlineStyle,
} from "./settingsShared";
import { fetchJsonWithStartupRetry, formatSettingsLoadError } from "./settingsLoad";

type FeatureFlagEntry = {
  name: string;
  default: boolean;
  enabled: boolean;
  source: "default" | "settings" | "env" | string;
  override?: boolean | null;
  env_var?: string;
  env_override?: boolean | null;
};

type FeatureFlagPayload = {
  flags?: FeatureFlagEntry[];
};

type DraftOverride = "default" | "on" | "off";

const flagLabels: Record<string, { title: string; description: string; group: "Runtime" | "Plugins" | "MCP" | "UI" | "SDK" }> = {
  reactive_compact: { title: "响应式上下文压缩", description: "在提示词预算耗尽前允许运行时压缩上下文。", group: "Runtime" },
  plugin_lifecycle_api: { title: "插件生命周期 API", description: "启用本地插件列表和启停管理接口。", group: "Plugins" },
  plugin_skills: { title: "插件技能", description: "发现本地插件中包含的 SKILL.md。", group: "Plugins" },
  sdk_query: { title: "SDK 查询", description: "启用 Python SDK 查询入口。", group: "SDK" },
  mcp_roots: { title: "MCP 根目录", description: "响应 MCP 服务的 roots/list 请求。", group: "MCP" },
  mcp_sampling: { title: "MCP 采样", description: "允许 MCP 服务请求宿主模型采样。", group: "MCP" },
  mcp_elicitation: { title: "MCP 结构化提问", description: "允许 MCP 服务向用户请求结构化输入。", group: "MCP" },
  mcp_websocket_transport: { title: "MCP WebSocket 传输", description: "启用 MCP WebSocket 连接。", group: "MCP" },
  mcp_streamable_http_transport: { title: "MCP 流式 HTTP 传输", description: "启用 MCP Streamable HTTP 连接。", group: "MCP" },
  global_search: { title: "全局搜索", description: "启用快速打开和会话搜索入口。", group: "UI" },
  agent_editor: { title: "智能体编辑器", description: "启用自定义子智能体编辑界面。", group: "UI" },
  agent_trace_export_v1: { title: "智能体轨迹导出", description: "允许导出智能体轨迹用于诊断。", group: "Runtime" },
};

const GROUPS = ["Runtime", "Plugins", "MCP", "UI", "SDK"] as const;
const GROUP_LABELS: Record<typeof GROUPS[number], string> = {
  Runtime: "运行时",
  Plugins: "插件",
  MCP: "MCP",
  UI: "界面",
  SDK: "SDK",
};

const jsonHeaders = (): HeadersInit => {
  try {
    return authHeaders({ "content-type": "application/json" });
  } catch {
    return { "content-type": "application/json" };
  }
};

const overrideToDraft = (value: boolean | null | undefined): DraftOverride => {
  if (value === true) return "on";
  if (value === false) return "off";
  return "default";
};

const draftToOverride = (value: DraftOverride): boolean | null => {
  if (value === "on") return true;
  if (value === "off") return false;
  return null;
};

const flagTitle = (flag: FeatureFlagEntry): string => flagLabels[flag.name]?.title ?? flag.name.replace(/_/g, " ");
const flagGroup = (flag: FeatureFlagEntry): typeof GROUPS[number] => flagLabels[flag.name]?.group ?? "Runtime";

export const FeatureFlagsTab = () => {
  const [flags, setFlags] = useState<FeatureFlagEntry[]>([]);
  const [drafts, setDrafts] = useState<Record<string, DraftOverride>>({});
  const [loading, setLoading] = useState(false);
  const [savingName, setSavingName] = useState("");
  const [loadError, setLoadError] = useState("");
  const [group, setGroup] = useState<typeof GROUPS[number]>("Runtime");
  const loadSeqRef = useRef(0);

  const refresh = useCallback(async (options: { showToast?: boolean } = {}) => {
    const seq = loadSeqRef.current + 1;
    loadSeqRef.current = seq;
    setLoading(true);
    setLoadError("");
    try {
      const payload = await fetchJsonWithStartupRetry<FeatureFlagPayload>(`${apiBase()}/api/settings/feature-flags`, {
        cache: "no-store",
        headers: authHeaders(),
      }, { cacheKey: "settings.feature_flags" });
      if (loadSeqRef.current !== seq) return;
      const nextFlags = Array.isArray(payload.flags) ? payload.flags : [];
      setFlags(nextFlags);
      setDrafts(Object.fromEntries(nextFlags.map((flag) => [flag.name, overrideToDraft(flag.override)])));
    } catch (error) {
      if (loadSeqRef.current !== seq) return;
      const message = formatSettingsLoadError(error);
      setLoadError(message);
      if (options.showToast) pushToast(`功能开关加载失败：${message}`, "error");
    } finally {
      if (loadSeqRef.current === seq) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const counts = useMemo(() => {
    const result = Object.fromEntries(GROUPS.map((name) => [name, 0])) as Record<typeof GROUPS[number], number>;
    for (const flag of flags) result[flagGroup(flag)] += 1;
    return result;
  }, [flags]);

  const visibleFlags = flags.filter((flag) => flagGroup(flag) === group);

  const saveFlag = async (flag: FeatureFlagEntry, nextDraft: DraftOverride) => {
    if (flag.source === "env") return;
    setDrafts((current) => ({ ...current, [flag.name]: nextDraft }));
    setSavingName(flag.name);
    try {
      const res = await fetch(`${apiBase()}/api/settings/feature-flags`, {
        method: "PUT",
        headers: jsonHeaders(),
        body: JSON.stringify({ flags: { [flag.name]: draftToOverride(nextDraft) } }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json() as FeatureFlagPayload;
      const nextFlags = Array.isArray(payload.flags) ? payload.flags : [];
      setFlags(nextFlags);
      setDrafts(Object.fromEntries(nextFlags.map((item) => [item.name, overrideToDraft(item.override)])));
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.feature_flags" }, { silent: true });
      pushToast(`功能开关已保存：${flagTitle(flag)}`, "success");
    } catch (error) {
      setDrafts((current) => ({ ...current, [flag.name]: overrideToDraft(flag.override) }));
      pushToast(`功能开关保存失败：${String(error)}`, "error");
    } finally {
      setSavingName("");
    }
  };

  return (
    <>
      <div className="feature-flag-groups" style={subTabBarStyle}>
        {GROUPS.map((item) => (
          <button key={item} type="button" onClick={() => setGroup(item)} style={subTabStyle(group === item)}>
            {GROUP_LABELS[item]}
            <span style={subTabCountStyle}>{counts[item]}</span>
          </button>
        ))}
      </div>

      {loadError && (
        <div style={loadErrorStyle}>
          <span>功能开关加载失败。</span>
          <code style={loadErrorCodeStyle}>{loadError}</code>
          <button type="button" onClick={() => void refresh({ showToast: true })} disabled={loading} style={retryButtonStyle}>重试</button>
        </div>
      )}

      <div className="feature-flag-list" style={flagListStyle}>
        {visibleFlags.map((flag) => {
          const draft = drafts[flag.name] ?? overrideToDraft(flag.override);
          const lockedByEnv = flag.source === "env";
          return (
            <div key={flag.name} className="feature-flag-row" style={flagRowStyle} title={flagLabels[flag.name]?.description}>
              <div style={{ minWidth: 0 }}>
                <span style={flagNameStyle}>{flagTitle(flag)}</span>
              </div>

              <div className="feature-flag-control" style={controlWrapStyle}>
                <select
                  aria-label={`覆盖 ${flagTitle(flag)}`}
                  title={lockedByEnv && flag.env_var ? `由 ${flag.env_var} 管理` : `${flag.name} · 默认${flag.default ? "开启" : "关闭"}`}
                  value={lockedByEnv ? "default" : draft}
                  disabled={lockedByEnv || savingName === flag.name}
                  onChange={(event) => void saveFlag(flag, event.target.value as DraftOverride)}
                  style={selectStyle}
                >
                  <option value="default">{lockedByEnv ? `环境变量 · ${flag.enabled ? "开启" : "关闭"}` : `默认 · ${flag.default ? "开启" : "关闭"}`}</option>
                  <option value="on">开启</option>
                  <option value="off">关闭</option>
                </select>
                {savingName === flag.name && <RotateCw size={14} className="animate-spin" aria-label="正在保存" style={savingStyle} />}
              </div>
            </div>
          );
        })}
        {loading && visibleFlags.length === 0 && (
          <div style={emptyInlineStyle}>正在加载功能开关…</div>
        )}
        {!loading && visibleFlags.length === 0 && !loadError && (
          <div style={emptyInlineStyle}>这个分组中没有功能开关。</div>
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button className="feature-flag-refresh" aria-label="刷新功能开关" onClick={() => void refresh({ showToast: true })} disabled={loading} style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}>
          <RotateCw size={14} className={loading ? "animate-spin" : undefined} />
          刷新
        </button>
      </div>
    </>
  );
};

const flagRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) auto",
  gap: 12,
  alignItems: "center",
  minHeight: 48,
  padding: "7px 11px",
  background: "var(--surface-base)",
  borderBottom: "1px solid var(--border-subtle)",
};

const flagListStyle: React.CSSProperties = {
  display: "grid",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  overflow: "hidden",
};

const flagNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
  color: "var(--text-primary)",
};

const loadErrorStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) auto",
  alignItems: "center",
  gap: "6px 10px",
  padding: "10px 12px",
  border: "1px solid color-mix(in oklch, var(--state-danger) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--state-danger-soft)",
  color: "var(--state-danger)",
  fontSize: "var(--text-xs)",
};

const loadErrorCodeStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
};

const retryButtonStyle: React.CSSProperties = {
  ...secondaryActionStyle,
  gridColumn: "2",
  gridRow: "1 / span 2",
  height: 30,
};

const controlWrapStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const selectStyle: React.CSSProperties = {
  height: 32,
  minWidth: 104,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  padding: "0 8px",
  colorScheme: "light dark",
};

const savingStyle: React.CSSProperties = {
  color: "var(--text-muted)",
};
