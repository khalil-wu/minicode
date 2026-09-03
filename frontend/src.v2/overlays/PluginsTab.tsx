import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  Blocks,
  CheckCircle2,
  FolderArchive,
  FolderOpen,
  LockKeyhole,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { BrandIcon } from "../components/BrandIcon";
import { pushToast } from "./ToastContainer";
import {
  apiBase,
  authHeaders,
  errorMessageFromResponseText,
  fetchWithTimeout,
  LONG_HTTP_TIMEOUT_MS,
  pluginAssetResourceUrlWithToken,
} from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { isDesktop, pickDirectory } from "../desktop/runtime";
import { Section } from "./settingsShared";
import { fetchJsonWithStartupRetry, formatSettingsLoadError } from "./settingsLoad";
import { showConfirm } from "./DialogService";
import "./PluginsTab.css";

type PluginEntry = {
  id?: string;
  name: string;
  displayName?: string;
  description?: string;
  shortDescription?: string;
  longDescription?: string;
  developerName?: string;
  category?: string;
  capabilities?: string[];
  version?: string;
  websiteUrl?: string;
  iconUrl?: string;
  iconVariant?: "composer" | "logo" | "logo-dark";
  brandColor?: string;
  defaultPrompt?: string[];
  path: string;
  manifest_path?: string;
  skill_count?: number;
  mcp_server_count?: number;
  app_count?: number;
  hook_count?: number;
  runtime_support?: {
    skills?: boolean;
    mcp_servers?: boolean;
    apps?: boolean;
    hooks?: boolean;
  };
  enabled: boolean;
  disabled?: boolean;
  managed?: boolean;
  policy_managed?: boolean;
  managed_enabled?: boolean | null;
};

type PluginValidation = {
  ok: boolean;
  plugin?: {
    name?: string;
    skill_count?: number;
    mcp_server_count?: number;
    app_count?: number;
    hook_count?: number;
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
  runtime_refresh?: { ok?: boolean; warnings?: string[]; refreshed?: string[] };
};

const normalizePlugins = (plugins: PluginEntry[] | undefined): PluginEntry[] => (
  (Array.isArray(plugins) ? plugins : []).map((plugin) => ({
    ...plugin,
    iconUrl: plugin.iconUrl || (plugin.iconVariant
      ? pluginAssetResourceUrlWithToken(plugin.path, plugin.iconVariant)
      : undefined),
  }))
);

const reportRuntimeRefresh = (payload: PluginSettingsPayload): boolean => {
  const refresh = payload.runtime_refresh;
  if (!refresh || refresh.ok !== false) return true;
  const detail = Array.isArray(refresh.warnings) && refresh.warnings.length > 0
    ? refresh.warnings.join("；")
    : "运行时能力刷新失败，请重试或重启 MiniCode";
  pushToast(`插件配置已保存，但尚未完全加载：${detail}`, "warning");
  return false;
};

const operationError = (error: unknown): string =>
  error instanceof Error ? error.message : String(error || "未知错误");

type PluginOperation = "" | "refresh" | "state" | "remove" | "import" | "pick" | "validate" | "package";

const requireSuccessfulResponse = async (response: Response, fallback: string): Promise<void> => {
  if (response.ok) return;
  const text = await response.text().catch(() => "");
  throw new Error(errorMessageFromResponseText(text, fallback || `HTTP ${response.status}`));
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
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const operationRef = useRef<PluginOperation>("");
  const [activeOperation, setActiveOperation] = useState<PluginOperation>("");

  const beginOperation = useCallback((operation: Exclude<PluginOperation, "">): boolean => {
    if (operationRef.current) return false;
    operationRef.current = operation;
    setActiveOperation(operation);
    return true;
  }, []);

  const endOperation = useCallback((operation: Exclude<PluginOperation, "">) => {
    if (operationRef.current !== operation) return;
    operationRef.current = "";
    setActiveOperation("");
  }, []);

  const refresh = useCallback(async (options: { showToast?: boolean } = {}) => {
    if (!beginOperation("refresh")) return;
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
      setPlugins(normalizePlugins(payload.plugins));
      if (options.showToast) pushToast("插件列表已刷新", "success");
    } catch (error) {
      if (loadSeqRef.current !== seq) return;
      const message = formatSettingsLoadError(error);
      setLoadError(message);
      if (options.showToast) pushToast(`插件设置加载失败：${message}`, "error");
    } finally {
      if (loadSeqRef.current === seq) setLoading(false);
      endOperation("refresh");
    }
  }, [beginOperation, endOperation]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const counts = useMemo(() => {
    const enabled = plugins.filter((plugin) => plugin.enabled).length;
    const skills = plugins.reduce((sum, plugin) => sum + Number(plugin.skill_count || 0), 0);
    const mcpServers = plugins.reduce((sum, plugin) => sum + Number(plugin.mcp_server_count || 0), 0);
    return { enabled, skills, mcpServers };
  }, [plugins]);

  const setPluginEnabled = async (plugin: PluginEntry, enabled: boolean) => {
    if (plugin.policy_managed) return;
    if (!beginOperation("state")) return;
    const pluginKey = plugin.id || plugin.name;
    setSavingName(pluginKey);
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/plugins/${encodeURIComponent(pluginKey)}/state`,
        {
          method: "PUT",
          headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ enabled }),
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: `${enabled ? "启用" : "停用"}插件超时，请重试。`,
        },
      );
      await requireSuccessfulResponse(response, `${enabled ? "启用" : "停用"}插件失败`);
      const payload = await response.json() as PluginSettingsPayload;
      setPlugins(normalizePlugins(payload.plugins));
      const runtimeReady = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.plugins" }, { silent: true });
      if (runtimeReady) pushToast(`${enabled ? "已启用" : "已停用"}插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`插件更新失败：${operationError(error)}`, "error");
    } finally {
      setSavingName("");
      endOperation("state");
    }
  };

  const removePlugin = async (plugin: PluginEntry) => {
    if (!plugin.managed || plugin.policy_managed) return;
    if (!beginOperation("remove")) return;
    const confirmed = await showConfirm({
      title: "卸载插件",
      message: `确定卸载插件“${plugin.displayName || plugin.name}”？插件文件及其托管能力会被移除。`,
      confirmLabel: "卸载",
      danger: true,
    });
    if (!confirmed) {
      endOperation("remove");
      return;
    }
    const pluginKey = plugin.id || plugin.name;
    setSavingName(pluginKey);
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/plugins/${encodeURIComponent(pluginKey)}`,
        {
          method: "DELETE",
          headers: authHeaders(),
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: "卸载插件超时，请重试。",
        },
      );
      await requireSuccessfulResponse(response, "卸载插件失败");
      const payload = await response.json() as PluginSettingsPayload;
      setPlugins(normalizePlugins(payload.plugins));
      const runtimeReady = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      if (runtimeReady) pushToast(`已卸载插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`插件卸载失败：${operationError(error)}`, "error");
    } finally {
      setSavingName("");
      endOperation("remove");
    }
  };

  const importPlugin = async (kind: "directory" | "package" = "directory") => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    if (!beginOperation("import")) return;
    setImporting(true);
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/plugins/import`,
        {
          method: "POST",
          headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ source_path: sourcePath }),
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: "导入插件超时，请检查插件包后重试。",
        },
      );
      await requireSuccessfulResponse(response, "导入插件失败");
      const payload = await response.json() as PluginSettingsPayload & { imported?: { name?: string } };
      setPlugins(normalizePlugins(payload.plugins));
      const runtimeReady = reportRuntimeRefresh(payload);
      setImportPath("");
      setValidation(null);
      setPackageResult(null);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: `settings.plugins.import.${kind}` }, { silent: true });
      if (runtimeReady) {
        pushToast(`已导入插件${kind === "package" ? "包" : "文件夹"}：${payload.imported?.name || sourcePath}`, "success");
      }
    } catch (error) {
      pushToast(`插件导入失败：${operationError(error)}`, "error");
    } finally {
      setImporting(false);
      endOperation("import");
    }
  };

  const choosePluginDirectory = async () => {
    if (!beginOperation("pick")) return;
    try {
      const selected = await pickDirectory();
      if (selected) setImportPath(selected);
    } catch (error) {
      pushToast(`无法选择插件文件夹：${operationError(error)}`, "error");
    } finally {
      endOperation("pick");
    }
  };

  const validatePlugin = async () => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    if (!beginOperation("validate")) return;
    setCheckingPath("validate");
    setPackageResult(null);
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/plugins/validate`,
        {
          method: "POST",
          headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ source_path: sourcePath }),
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: "验证插件超时，请检查插件目录后重试。",
        },
      );
      await requireSuccessfulResponse(response, "验证插件失败");
      const payload = await response.json() as PluginValidation;
      setValidation(payload);
      pushToast(payload.ok ? "插件验证通过。" : "插件验证发现问题。", payload.ok ? "success" : "warning");
    } catch (error) {
      pushToast(`插件验证失败：${operationError(error)}`, "error");
    } finally {
      setCheckingPath("");
      endOperation("validate");
    }
  };

  const packagePlugin = async () => {
    const sourcePath = importPath.trim();
    if (!sourcePath) return;
    if (!beginOperation("package")) return;
    setCheckingPath("package");
    try {
      const response = await fetchWithTimeout(
        `${apiBase()}/api/plugins/package`,
        {
          method: "POST",
          headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ source_path: sourcePath }),
        },
        {
          timeoutMs: LONG_HTTP_TIMEOUT_MS,
          timeoutMessage: "打包插件超时，请重试。",
        },
      );
      await requireSuccessfulResponse(response, "打包插件失败");
      const payload = await response.json() as PluginPackageResult;
      if (!payload.ok) {
        const details = payload.validation?.errors?.join("；") || "插件打包未完成";
        throw new Error(details);
      }
      setPackageResult(payload);
      setValidation(payload.validation ?? null);
      pushToast(`插件已打包：${payload.package?.name || sourcePath}`, "success");
    } catch (error) {
      pushToast(`插件打包失败：${operationError(error)}`, "error");
    } finally {
      setCheckingPath("");
      endOperation("package");
    }
  };

  const busy = Boolean(activeOperation);

  return (
    <>
      <Section title="已安装插件" description="能力包；启用后加载技能、MCP、App 和 Hook。">
        <div className="plugin-summary">
          <div className="plugin-summary-item"><strong>{counts.enabled}</strong><span>已启用</span></div>
          <div className="plugin-summary-item"><strong>{plugins.length}</strong><span>已安装</span></div>
          <div className="plugin-summary-item"><strong>{counts.skills}</strong><span>技能</span></div>
          <div className="plugin-summary-item"><strong>{counts.mcpServers}</strong><span>MCP</span></div>
          <button type="button" onClick={() => void refresh({ showToast: true })} disabled={busy} className="plugin-icon-button" title="刷新插件" aria-label="刷新插件"><RefreshCw className={loading ? "settings-spin" : undefined} /></button>
        </div>

        {loadError && (
          <div className="plugin-load-error" role="alert">
            <div><strong>插件设置加载失败。</strong><code>{loadError}</code></div>
            <button type="button" onClick={() => void refresh({ showToast: true })} disabled={busy}>重试</button>
          </div>
        )}

        <div className="plugin-local-list">
          {plugins.map((plugin) => {
            const pluginKey = plugin.id || plugin.name;
            const saving = savingName === pluginKey;
            const displayName = plugin.displayName || plugin.name;
            const description = plugin.shortDescription || plugin.description || "本地 MiniCode 插件";
            return (
              <article
                key={`${pluginKey}:${plugin.path}`}
                className="plugin-local-row"
                data-enabled={plugin.enabled}
                aria-busy={saving}
              >
                <span className="plugin-local-icon">
                  <BrandIcon
                    value={`${displayName} ${description}`}
                    size={21}
                    iconUrl={plugin.iconUrl}
                    websiteUrl={plugin.websiteUrl}
                  />
                </span>
                <div className="plugin-local-copy">
                  <div className="plugin-local-title">
                    <strong>{displayName}</strong>
                    <span className="plugin-local-state">
                      {saving
                        ? activeOperation === "remove" ? "正在卸载…" : "正在更新…"
                        : plugin.policy_managed
                          ? plugin.enabled ? "策略启用" : "策略停用"
                          : plugin.enabled ? "已启用" : "已停用"}
                    </span>
                    {plugin.policy_managed && (
                      <span className="plugin-policy-lock" title="此插件状态由组织策略管理" aria-label="组织策略锁定">
                        <LockKeyhole aria-hidden="true" />
                      </span>
                    )}
                    {plugin.version && <span>v{plugin.version}</span>}
                    {Number(plugin.skill_count || 0) > 0 && <span>{Number(plugin.skill_count)} 个技能</span>}
                    {Number(plugin.mcp_server_count || 0) > 0 && <span>{Number(plugin.mcp_server_count)} 个 MCP</span>}
                    {Number(plugin.app_count || 0) > 0 && <span>{Number(plugin.app_count)} 个 App{plugin.runtime_support?.apps === false ? "（仅清单）" : ""}</span>}
                    {Number(plugin.hook_count || 0) > 0 && <span>{Number(plugin.hook_count)} 个 Hook{plugin.runtime_support?.hooks === false ? "（未执行）" : ""}</span>}
                    {plugin.category && <span>{plugin.category}</span>}
                  </div>
                  <p title={plugin.longDescription || description}>{description}</p>
                  {(plugin.developerName || (plugin.capabilities?.length ?? 0) > 0) && (
                    <small>{[
                      plugin.developerName ? `开发者：${plugin.developerName}` : "",
                      ...(plugin.capabilities ?? []).slice(0, 3),
                    ].filter(Boolean).join(" · ")}</small>
                  )}
                  <code title={plugin.path}>{plugin.path}</code>
                </div>
                <label className="plugin-switch">
                  <input
                    type="checkbox"
                    aria-label={`${plugin.enabled ? "停用" : "启用"}插件 ${plugin.name}`}
                    checked={plugin.enabled}
                    disabled={busy || plugin.policy_managed}
                    onChange={(event) => void setPluginEnabled(plugin, event.currentTarget.checked)}
                  />
                  <span><i /></span>
                </label>
                {plugin.managed && !plugin.policy_managed && (
                  <button
                    type="button"
                    className="plugin-icon-button"
                    aria-label={`卸载插件 ${plugin.name}`}
                    title="卸载插件"
                    disabled={busy}
                    onClick={() => void removePlugin(plugin)}
                  >
                    <Trash2 />
                  </button>
                )}
              </article>
            );
          })}
          {loading && plugins.length === 0 && <div className="plugin-empty">正在加载插件…</div>}
          {!loading && plugins.length === 0 && !loadError && (
            <div className="plugin-empty">
              <span className="plugin-empty-icon"><Blocks aria-hidden="true" /></span>
              <strong>还没有本地插件</strong>
              <p>导入插件文件夹或 Zip。</p>
              <button
                type="button"
                onClick={() => isDesktop() ? void choosePluginDirectory() : importInputRef.current?.focus()}
                disabled={busy}
              >
                {isDesktop() ? "选择插件文件夹" : "填写插件路径"}
              </button>
            </div>
          )}
        </div>
      </Section>

      <Section title="插件开发" description="验证、导入或打包本地插件。">
        <div className="plugin-dev-card">
          <p className="plugin-section-description">路径只用于本次操作。</p>
          <div className="plugin-import-row">
            <input
              ref={importInputRef}
              value={importPath}
              disabled={busy}
              onChange={(event) => setImportPath(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === "Enter" && !busy) void importPlugin(); }}
              placeholder="插件文件夹或 .zip 路径"
              aria-label="插件文件夹或安装包路径"
            />
            {isDesktop() && <button type="button" onClick={choosePluginDirectory} disabled={busy} className="plugin-icon-button" title="选择插件文件夹" aria-label="选择插件文件夹"><FolderOpen /></button>}
            <button type="button" onClick={() => void importPlugin("directory")} disabled={!importPath.trim() || busy} className="plugin-primary-button">{importing ? "正在导入…" : "导入"}</button>
          </div>
          <div className="plugin-dev-actions">
            <button type="button" onClick={() => void importPlugin("package")} disabled={!importPath.trim() || busy}><FolderArchive /><span>导入 Zip</span></button>
            <button type="button" onClick={validatePlugin} disabled={!importPath.trim() || busy}><CheckCircle2 /><span>{checkingPath === "validate" ? "正在检查…" : "验证"}</span></button>
            <button type="button" onClick={packagePlugin} disabled={!importPath.trim() || busy}><Archive /><span>{checkingPath === "package" ? "正在打包…" : "打包"}</span></button>
            {packageResult?.package?.path && <code title={packageResult.package.path}>{packageResult.package.path}</code>}
          </div>
          {validation && (
            <div className="plugin-validation" data-valid={validation.ok}>
              <div>
                <strong>{validation.ok ? "验证通过" : "验证需要处理"}</strong>
                {validation.plugin && <span>{Number(validation.plugin.skill_count || 0)} 个技能 · {Number(validation.plugin.mcp_server_count || 0)} 个 MCP · {Number(validation.plugin.app_count || 0)} 个 App · {Number(validation.plugin.hook_count || 0)} 个 Hook · {Number(validation.plugin.file_count || 0)} 个文件</span>}
              </div>
              {[...(validation.errors || []), ...(validation.warnings || [])].slice(0, 4).map((item) => <p key={item}>{item}</p>)}
            </div>
          )}
        </div>
      </Section>
    </>
  );
};
