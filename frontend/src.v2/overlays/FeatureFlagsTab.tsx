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
  reactive_compact: { title: "Reactive compaction", description: "Allow runtime context compaction before the prompt budget is exhausted.", group: "Runtime" },
  coordinator_mode: { title: "Coordinator mode", description: "Constrain leader agents to orchestration-first tools.", group: "Runtime" },
  plugin_commands: { title: "Plugin commands", description: "Discover slash commands from local plugin manifests.", group: "Plugins" },
  plugin_template_commands: { title: "Plugin template commands", description: "Enable prompt-template slash commands from plugin manifests.", group: "Plugins" },
  plugin_protocol_commands: { title: "Plugin protocol commands", description: "Enable plugin commands that dispatch registered runtime commands.", group: "Plugins" },
  plugin_local_ui_commands: { title: "Plugin local UI commands", description: "Enable safe local UI actions from plugin commands.", group: "Plugins" },
  plugin_local_jsx_commands: { title: "Plugin local JSX panels", description: "Enable allowlisted interactive plugin command panels.", group: "Plugins" },
  plugin_lifecycle_api: { title: "Plugin lifecycle API", description: "Enable local plugin list and enable/disable management endpoints.", group: "Plugins" },
  plugin_skills: { title: "Plugin skills", description: "Discover SKILL.md entries bundled inside local plugins.", group: "Plugins" },
  sdk_query: { title: "SDK query", description: "Enable the Python SDK query entrypoint.", group: "SDK" },
  mcp_roots: { title: "MCP roots", description: "Answer roots/list requests from MCP servers.", group: "MCP" },
  mcp_sampling: { title: "MCP sampling", description: "Allow MCP servers to request host LLM sampling.", group: "MCP" },
  mcp_elicitation: { title: "MCP elicitation", description: "Allow MCP servers to ask the user for structured input.", group: "MCP" },
  mcp_websocket_transport: { title: "MCP WebSocket transport", description: "Enable MCP WebSocket connections.", group: "MCP" },
  mcp_streamable_http_transport: { title: "MCP Streamable HTTP transport", description: "Enable Streamable HTTP MCP connections.", group: "MCP" },
  global_search: { title: "Global search", description: "Enable Quick Open and conversation search entrypoints.", group: "UI" },
  agent_editor: { title: "Agent editor", description: "Enable the custom subagent editor overlay.", group: "UI" },
  agent_trace_export_v1: { title: "Agent trace export", description: "Enable exporting agent trace data for diagnostics.", group: "Runtime" },
};

const GROUPS = ["Runtime", "Plugins", "MCP", "UI", "SDK"] as const;

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
      if (options.showToast) pushToast(`Feature flags load failed: ${message}`, "error");
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
      pushToast(`Feature flag saved: ${flagTitle(flag)}`, "success");
    } catch (error) {
      setDrafts((current) => ({ ...current, [flag.name]: overrideToDraft(flag.override) }));
      pushToast(`Feature flag save failed: ${String(error)}`, "error");
    } finally {
      setSavingName("");
    }
  };

  return (
    <>
      <div className="feature-flag-groups" style={subTabBarStyle}>
        {GROUPS.map((item) => (
          <button key={item} type="button" onClick={() => setGroup(item)} style={subTabStyle(group === item)}>
            {item}
            <span style={subTabCountStyle}>{counts[item]}</span>
          </button>
        ))}
      </div>

      {loadError && (
        <div style={loadErrorStyle}>
          <span>Feature flags failed to load.</span>
          <code style={loadErrorCodeStyle}>{loadError}</code>
          <button type="button" onClick={() => void refresh({ showToast: true })} disabled={loading} style={retryButtonStyle}>Retry</button>
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
                  aria-label={`Override ${flagTitle(flag)}`}
                  title={lockedByEnv && flag.env_var ? `Managed by ${flag.env_var}` : `${flag.name} · default ${flag.default ? "on" : "off"}`}
                  value={lockedByEnv ? "default" : draft}
                  disabled={lockedByEnv || savingName === flag.name}
                  onChange={(event) => void saveFlag(flag, event.target.value as DraftOverride)}
                  style={selectStyle}
                >
                  <option value="default">{lockedByEnv ? `Environment · ${flag.enabled ? "On" : "Off"}` : `Default · ${flag.default ? "On" : "Off"}`}</option>
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
                {savingName === flag.name && <RotateCw size={13} className="animate-spin" aria-label="Saving" style={savingStyle} />}
              </div>
            </div>
          );
        })}
        {loading && visibleFlags.length === 0 && (
          <div style={emptyInlineStyle}>Loading feature flags...</div>
        )}
        {!loading && visibleFlags.length === 0 && !loadError && (
          <div style={emptyInlineStyle}>No feature flags in this group.</div>
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button className="feature-flag-refresh" aria-label="Refresh feature flags" onClick={() => void refresh({ showToast: true })} disabled={loading} style={{ ...secondaryActionStyle, display: "inline-flex", alignItems: "center", gap: 7 }}>
          <RotateCw size={14} className={loading ? "animate-spin" : undefined} />
          Refresh
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
