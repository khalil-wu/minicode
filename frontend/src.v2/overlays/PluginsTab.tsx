import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Archive, CheckCircle2, FolderArchive, FolderOpen, RefreshCw } from "lucide-react";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { isDesktop, pickDirectory } from "../desktop/runtime";
import {
  Section,
  inputStyle,
  primaryActionStyle,
  secondaryActionStyle,
  monoTextStyle,
  emptyInlineStyle,
  miniMetaStyle,
} from "./settingsShared";
import { fetchJsonWithStartupRetry, formatSettingsLoadError } from "./settingsLoad";

type PluginEntry = {
  name: string;
  description?: string;
  version?: string;
  path: string;
  manifest_path?: string;
  command_count?: number;
  skill_count?: number;
  enabled: boolean;
  disabled?: boolean;
};

type PluginValidation = {
  ok: boolean;
  plugin?: {
    name?: string;
    command_count?: number;
    skill_count?: number;
    file_count?: number;
    total_bytes?: number;
  };
  warnings?: string[];
  errors?: string[];
};

type PluginPackageResult = {
  ok: boolean;
  package?: {
    name?: string;
    path?: string;
    file_count?: number;
    total_bytes?: number;
  };
  validation?: PluginValidation;
};

type PluginSettingsPayload = {
  plugins?: PluginEntry[];
};

const jsonHeaders = (): HeadersInit => {
  try {
    return authHeaders({ "content-type": "application/json" });
  } catch {
    return { "content-type": "application/json" };
  }
};

export const PluginsTab = () => {
  const [plugins, setPlugins] = useState<PluginEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [savingName, setSavingName] = useState("");
  const [importPath, setImportPath] = useState("");
  const [importing, setImporting] = useState(false);
  const [checkingPath, setCheckingPath] = useState("");
  const [validation, setValidation] = useState<PluginValidation | null>(null);
  const [packageResult, setPackageResult] = useState<PluginPackageResult | null>(null);
  const loadSeqRef = useRef(0);

  const refresh = useCallback(async (options: { showToast?: boolean } = {}) => {
    const seq = loadSeqRef.current + 1;
    loadSeqRef.current = seq;
    setLoading(true);
    setLoadError("");
    try {
      const payload = await fetchJsonWithStartupRetry<PluginSettingsPayload>(`${apiBase()}/api/plugins`, {
        cache: "no-store",
        headers: authHeaders(),
      }, { cacheKey: "settings.plugins" });
      if (loadSeqRef.current !== seq) return;
      setPlugins(Array.isArray(payload.plugins) ? payload.plugins : []);
    } catch (error) {
      if (loadSeqRef.current !== seq) return;
      const message = formatSettingsLoadError(error);
      setLoadError(message);
      if (options.showToast) pushToast(`Plugin settings load failed: ${message}`, "error");
    } finally {
      if (loadSeqRef.current === seq) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const counts = useMemo(() => {
    const enabled = plugins.filter((plugin) => plugin.enabled).length;
    const commands = plugins.reduce((sum, plugin) => sum + Number(plugin.command_count || 0), 0);
    const skills = plugins.reduce((sum, plugin) => sum + Number(plugin.skill_count || 0), 0);
    return { enabled, commands, skills };
  }, [plugins]);

  const setPluginEnabled = async (plugin: PluginEntry, enabled: boolean) => {
    setSavingName(plugin.name);
    try {
      const res = await fetch(`${apiBase()}/api/plugins/${encodeURIComponent(plugin.name)}/state`, {
        method: "PUT",
        headers: jsonHeaders(),
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json() as PluginSettingsPayload;
      setPlugins(Array.isArray(payload.plugins) ? payload.plugins : []);
      sendClientCommand({ type: "commands.list" }, { silent: true });
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.plugins" }, { silent: true });
      pushToast(`${enabled ? "Enabled" : "Disabled"} plugin: ${plugin.name}`, "success");
    } catch (error) {
      pushToast(`Plugin update failed: ${String(error)}`, "error");
    } finally {
      setSavingName("");
    }
  };

  const importPlugin = async (kind: "directory" | "package" = "directory") => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    setImporting(true);
    try {
      const res = await fetch(`${apiBase()}/api/plugins/import`, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ source_path: sourcePath }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json() as PluginSettingsPayload & { imported?: { name?: string } };
      setPlugins(Array.isArray(payload.plugins) ? payload.plugins : []);
      setImportPath("");
      setValidation(null);
      setPackageResult(null);
      sendClientCommand({ type: "commands.list" }, { silent: true });
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: `settings.plugins.import.${kind}` }, { silent: true });
      pushToast(`Imported plugin ${kind === "package" ? "package" : "folder"}: ${payload.imported?.name || sourcePath}`, "success");
    } catch (error) {
      pushToast(`Plugin import failed: ${String(error)}`, "error");
    } finally {
      setImporting(false);
    }
  };

  const choosePluginDirectory = async () => {
    const selected = await pickDirectory();
    if (selected) setImportPath(selected);
  };

  const validatePlugin = async () => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    setCheckingPath("validate");
    setPackageResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/plugins/validate`, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ source_path: sourcePath }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json() as PluginValidation;
      setValidation(payload);
      pushToast(payload.ok ? "Plugin validation passed." : "Plugin validation found issues.", payload.ok ? "success" : "warning");
    } catch (error) {
      pushToast(`Plugin validation failed: ${String(error)}`, "error");
    } finally {
      setCheckingPath("");
    }
  };

  const packagePlugin = async () => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    setCheckingPath("package");
    try {
      const res = await fetch(`${apiBase()}/api/plugins/package`, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ source_path: sourcePath }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json() as PluginPackageResult;
      setPackageResult(payload);
      setValidation(payload.validation ?? null);
      pushToast(`Packaged plugin: ${payload.package?.name || sourcePath}`, "success");
    } catch (error) {
      pushToast(`Plugin package failed: ${String(error)}`, "error");
    } finally {
      setCheckingPath("");
    }
  };

  return (
    <>
      <Section title="Local Plugins">
        <div style={summaryBarStyle}>
          <span style={summaryItemStyle}>{counts.enabled}/{plugins.length} enabled</span>
          <span style={summaryItemStyle}>{counts.commands} commands</span>
          <span style={summaryItemStyle}>{counts.skills} skills</span>
          {loading && <span style={loadingPillStyle}>Loading...</span>}
          <button type="button" onClick={() => void refresh({ showToast: true })} disabled={loading} style={iconButtonStyle} title="Refresh plugins" aria-label="Refresh plugins">
            <RefreshCw size={14} />
          </button>
        </div>
      </Section>

      {loadError && (
        <div style={loadErrorStyle}>
          <span>Plugin settings failed to load.</span>
          <code style={loadErrorCodeStyle}>{loadError}</code>
          <button type="button" onClick={() => void refresh({ showToast: true })} disabled={loading} style={retryButtonStyle}>Retry</button>
        </div>
      )}

      <div style={importRowStyle}>
        <input
          value={importPath}
          onChange={(event) => setImportPath(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void importPlugin();
          }}
          placeholder="Plugin folder or .minicode-plugin.zip path"
          aria-label="Plugin folder or package path"
          style={{ ...inputStyle, minWidth: 0, fontFamily: "var(--font-mono)" }}
        />
        {isDesktop() && (
          <button type="button" onClick={choosePluginDirectory} style={iconButtonStyle} title="Choose plugin folder" aria-label="Choose plugin folder">
            <FolderOpen size={14} />
          </button>
        )}
        <button type="button" onClick={() => void importPlugin("directory")} disabled={!importPath.trim() || importing} style={primaryActionStyle}>
          {importing ? "Importing..." : "Import"}
        </button>
      </div>

      <div style={packageBarStyle}>
        <button type="button" onClick={() => void importPlugin("package")} disabled={!importPath.trim() || importing} style={compactActionStyle}>
          <FolderArchive size={14} />
          <span>Import Zip</span>
        </button>
        <button type="button" onClick={validatePlugin} disabled={!importPath.trim() || Boolean(checkingPath)} style={compactActionStyle}>
          <CheckCircle2 size={14} />
          <span>{checkingPath === "validate" ? "Checking..." : "Validate"}</span>
        </button>
        <button type="button" onClick={packagePlugin} disabled={!importPath.trim() || Boolean(checkingPath)} style={compactActionStyle}>
          <Archive size={14} />
          <span>{checkingPath === "package" ? "Packaging..." : "Package"}</span>
        </button>
        {packageResult?.package?.path && (
          <span title={packageResult.package.path} style={{ ...monoTextStyle, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {packageResult.package.path}
          </span>
        )}
      </div>

      {validation && (
        <div style={validationPanelStyle(validation.ok)}>
          <div style={validationTitleStyle}>
            {validation.ok ? "Validation passed" : "Validation needs attention"}
            {validation.plugin && (
              <span style={miniMetaStyle}>
                {Number(validation.plugin.command_count || 0)} cmd · {Number(validation.plugin.skill_count || 0)} skills · {Number(validation.plugin.file_count || 0)} files
              </span>
            )}
          </div>
          {[...(validation.errors || []), ...(validation.warnings || [])].slice(0, 4).map((item) => (
            <div key={item} style={validationLineStyle}>{item}</div>
          ))}
        </div>
      )}

      <div style={pluginListStyle}>
        {plugins.map((plugin) => {
          const saving = savingName === plugin.name;
          return (
            <div key={`${plugin.name}:${plugin.path}`} style={pluginRowStyle(plugin.enabled)}>
              <span style={statusDotStyle(plugin.enabled)} />
              <div style={{ minWidth: 0, display: "grid", gap: 4 }}>
                <div style={pluginTitleLineStyle}>
                  <span style={pluginNameStyle}>{plugin.name}</span>
                  <span style={stateBadgeStyle(plugin.enabled)}>{plugin.enabled ? "enabled" : "disabled"}</span>
                  {plugin.version && <span style={miniMetaStyle}>v{plugin.version}</span>}
                  <span style={miniMetaStyle}>{Number(plugin.command_count || 0)} cmd</span>
                  <span style={miniMetaStyle}>{Number(plugin.skill_count || 0)} skills</span>
                </div>
                {plugin.description && <div style={descriptionStyle}>{plugin.description}</div>}
                <div title={plugin.path} style={{ ...monoTextStyle, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {plugin.path}
                </div>
              </div>
              <label style={toggleLabelStyle}>
                <input
                  type="checkbox"
                  aria-label={`${plugin.enabled ? "Disable" : "Enable"} plugin ${plugin.name}`}
                  checked={plugin.enabled}
                  disabled={saving}
                  onChange={(event) => void setPluginEnabled(plugin, event.currentTarget.checked)}
                  style={toggleInputStyle}
                />
                <span style={toggleTrackStyle(plugin.enabled, saving)}>
                  <span style={toggleThumbStyle(plugin.enabled)} />
                </span>
              </label>
            </div>
          );
        })}
        {loading && plugins.length === 0 && (
          <div style={emptyInlineStyle}>Loading plugins...</div>
        )}
        {!loading && plugins.length === 0 && !loadError && (
          <div style={emptyInlineStyle}>No local plugins found in the configured plugin roots.</div>
        )}
      </div>
    </>
  );
};

const summaryBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
};

const summaryItemStyle: React.CSSProperties = {
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  padding: "0 9px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

const loadingPillStyle: React.CSSProperties = {
  ...summaryItemStyle,
  color: "var(--accent-primary)",
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

const iconButtonStyle: React.CSSProperties = {
  ...secondaryActionStyle,
  width: 32,
  padding: 0,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};

const compactActionStyle: React.CSSProperties = {
  ...secondaryActionStyle,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
};

const pluginListStyle: React.CSSProperties = {
  display: "grid",
  gap: 8,
};

const importRowStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) auto auto",
  alignItems: "center",
  gap: 8,
  padding: "10px 12px",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
};

const packageBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const validationPanelStyle = (ok: boolean): React.CSSProperties => ({
  display: "grid",
  gap: 5,
  padding: "10px 12px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: ok ? "var(--state-success-soft)" : "var(--state-warning-soft)",
  color: ok ? "var(--state-success)" : "var(--state-warning)",
});

const validationTitleStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 10,
  fontSize: "var(--text-sm)",
  fontWeight: 700,
};

const validationLineStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.4,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const pluginRowStyle = (enabled: boolean): React.CSSProperties => ({
  display: "grid",
  gridTemplateColumns: "8px minmax(0, 1fr) auto",
  alignItems: "center",
  gap: 12,
  minHeight: 76,
  padding: "11px 12px",
  background: enabled ? "var(--surface-soft)" : "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  opacity: enabled ? 1 : 0.72,
});

const statusDotStyle = (enabled: boolean): React.CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: enabled ? "var(--state-success)" : "var(--text-muted)",
});

const pluginTitleLineStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const pluginNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
};

const descriptionStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const stateBadgeStyle = (enabled: boolean): React.CSSProperties => ({
  flexShrink: 0,
  padding: "2px 7px",
  borderRadius: "999px",
  color: enabled ? "var(--state-success)" : "var(--text-muted)",
  border: "1px solid var(--border-subtle)",
  fontSize: 10,
  fontWeight: 750,
  textTransform: "uppercase",
});

const toggleLabelStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  cursor: "pointer",
};

const toggleInputStyle: React.CSSProperties = {
  position: "absolute",
  opacity: 0,
  pointerEvents: "none",
};

const toggleTrackStyle = (enabled: boolean, saving: boolean): React.CSSProperties => ({
  width: 38,
  height: 22,
  padding: 2,
  borderRadius: 999,
  background: enabled ? "var(--accent-primary)" : "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  opacity: saving ? 0.55 : 1,
  boxSizing: "border-box",
  transition: "background 120ms, opacity 120ms",
});

const toggleThumbStyle = (enabled: boolean): React.CSSProperties => ({
  display: "block",
  width: 16,
  height: 16,
  borderRadius: "50%",
  background: "var(--surface-base)",
  boxShadow: "var(--shadow-sm)",
  transform: enabled ? "translateX(16px)" : "translateX(0)",
  transition: "transform 120ms",
});
