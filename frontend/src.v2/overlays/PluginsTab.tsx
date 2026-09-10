import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  Blocks,
  CheckCircle2,
  ChevronDown,
  FolderArchive,
  FolderOpen,
  LockKeyhole,
  RefreshCw,
  Search,
  Settings,
  Trash2,
} from "lucide-react";
import { BrandIcon } from "../components/BrandIcon";
import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
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
  iconVariants?: ("composer" | "logo" | "logo-dark")[];
  marketplace?: string;
  load_errors?: string[];
  dependencies?: string[];
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

type CatalogPlugin = {
  id: string;
  name: string;
  description?: string;
  category?: string;
  load_error?: string;
  interface?: { displayName?: string; shortDescription?: string; developerName?: string };
};
type PluginMarketplace = { name: string; status: string; error?: string; source: Record<string, string>; plugins: CatalogPlugin[] };

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

const pluginErrorText = (plugin: PluginEntry): string => (plugin.load_errors ?? []).map((error) =>
  error === "dependency-unsatisfied"
    ? `插件依赖尚未满足${plugin.dependencies?.length ? `，请检查：${plugin.dependencies.join("、")}` : ""}`
    : error,
).join("；");

type PluginOperation = "" | "refresh" | "state" | "remove" | "import" | "pick" | "validate" | "package" | "install" | "marketplace";

const requireSuccessfulResponse = async (response: Response, fallback: string): Promise<void> => {
  if (response.ok) return;
  const text = await response.text().catch(() => "");
  throw new Error(errorMessageFromResponseText(text, fallback || `HTTP ${response.status}`));
};

export const PluginsTab = ({ catalog = false, toolbarHost, active = true }: { catalog?: boolean; toolbarHost?: HTMLElement | null; active?: boolean }) => {
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
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
  const [marketplaces, setMarketplaces] = useState<PluginMarketplace[]>([]);
  const [marketplaceError, setMarketplaceError] = useState("");
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [managementOpen, setManagementOpen] = useState(!catalog);
  const [sourceName, setSourceName] = useState("");
  const [sourceKind, setSourceKind] = useState("github");
  const [sourceLocator, setSourceLocator] = useState("");
  const [addMenu, setAddMenu] = useState<{ x: number; y: number } | null>(null);
  const marketplaceRequestRef = useRef<AbortController | null>(null);

  const loadMarketplaces = useCallback(async () => {
    marketplaceRequestRef.current?.abort();
    const controller = new AbortController();
    marketplaceRequestRef.current = controller;
    setMarketplaceError("");
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/marketplaces`, { cache: "no-store", headers: authHeaders(), signal: controller.signal });
      await requireSuccessfulResponse(response, "插件来源加载失败");
      const payload = await response.json() as { marketplaces: PluginMarketplace[] };
      if (!controller.signal.aborted) setMarketplaces(payload.marketplaces);
    } catch (error) {
      if (!controller.signal.aborted) setMarketplaceError(operationError(error));
    }
  }, []);
  useEffect(() => () => marketplaceRequestRef.current?.abort(), []);

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
    if (active) { void refresh(); void loadMarketplaces(); }
    else setAddMenu(null);
  }, [active, refresh, loadMarketplaces]);

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
      const updatedPlugins = normalizePlugins(payload.plugins);
      setPlugins(updatedPlugins);
      const runtimeReady = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.plugins" }, { silent: true });
      const updated = updatedPlugins.find((item) => (item.id || item.name) === pluginKey)!;
      if (runtimeReady && updated.enabled !== enabled) {
        pushToast(`插件设置已保存，但${enabled ? "尚未启用" : "尚未停用"}：${pluginErrorText(updated) || "请检查插件依赖与策略"}`, "warning");
      } else if (runtimeReady) pushToast(`${enabled ? "已启用" : "已停用"}插件：${plugin.name}`, "success");
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
  const q = query.trim().toLocaleLowerCase();
  const filteredPlugins = plugins.filter((plugin) => !q || `${plugin.name} ${plugin.displayName} ${plugin.description}`.toLocaleLowerCase().includes(q));
  const pluginLogo = (plugin: PluginEntry, size: number) => <BrandIcon
    value={plugin.displayName || plugin.name} inferBrand={false} size={size}
    iconUrl={resolvedTheme === "dark" && plugin.iconVariants?.includes("logo-dark")
      ? pluginAssetResourceUrlWithToken(plugin.path, "logo-dark") : plugin.iconUrl}
    websiteUrl={plugin.websiteUrl}
  />;

  const installPlugin = async (plugin: CatalogPlugin, marketplace: string) => {
    if (!beginOperation("install")) return;
    setSavingName(plugin.id);
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/install`, {
        method: "POST", headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ plugin_name: plugin.name, marketplace, refresh_marketplace: false }),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "插件安装超时，请刷新查看安装状态。" });
      await requireSuccessfulResponse(response, "安装插件失败");
      const payload = await response.json() as PluginSettingsPayload;
      setPlugins(normalizePlugins(payload.plugins));
      const ready = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "plugins.install" }, { silent: true });
      if (ready) pushToast(`已安装插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`插件安装失败：${operationError(error)}`, "error");
    } finally {
      setSavingName(""); endOperation("install");
    }
  };

  const changeMarketplace = async (action: "add" | "refresh" | "remove", name: string) => {
    if (!beginOperation("marketplace")) return;
    try {
      if (action === "remove" && !await showConfirm({ title: "移除插件来源", message: `从目录移除 ${name}？已安装的插件仍会保留。`, confirmLabel: "移除", danger: true })) return;
      const source = { source: sourceKind, [sourceKind === "github" ? "repo" : sourceKind === "directory" ? "path" : "url"]: sourceLocator.trim() };
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/marketplaces${action === "add" ? "" : `/${encodeURIComponent(name)}${action === "refresh" ? "/refresh" : ""}`}`, {
        method: action === "remove" ? "DELETE" : "POST", headers: authHeaders({ "content-type": "application/json" }),
        ...(action === "add" ? { body: JSON.stringify({ name: name.trim(), source }) } : {}),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "插件来源操作超时，请刷新查看状态。" });
      await requireSuccessfulResponse(response, "插件来源更新失败");
      if (action === "add") { setSourceName(""); setSourceLocator(""); }
      if (action === "remove" && sourceFilter === name) setSourceFilter("");
      await loadMarketplaces();
      pushToast(action === "add" ? "已添加来源，点击同步加载插件目录。" : action === "refresh" ? "插件目录已同步" : "已移除插件来源", "success");
    } catch (error) {
      pushToast(operationError(error), "error");
    } finally {
      endOperation("marketplace");
    }
  };

  return (
    <div className={catalog ? "plugins-catalog" : undefined}>
      {catalog && active && toolbarHost && createPortal(<>
        <button type="button" className="skills-icon-button" disabled={busy} onClick={() => { void refresh({ showToast: true }); void loadMarketplaces(); }} aria-label="刷新插件" title="刷新插件"><RefreshCw className={loading ? "settings-spin" : undefined} /></button>
        <button type="button" className="skills-icon-button" onClick={() => setManagementOpen(!managementOpen)} aria-label="管理插件与来源" aria-pressed={managementOpen} title="管理插件与来源"><Settings /></button>
        <button type="button" className="skills-create-button" disabled={busy} aria-haspopup="menu" aria-expanded={Boolean(addMenu)} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setAddMenu({ x: rect.right - 200, y: rect.bottom + 4 }); }}>添加 <ChevronDown /></button>
      </>, toolbarHost)}
      {catalog && <label className="skills-search"><Search aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索插件" aria-label="搜索插件" /></label>}
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

        {catalog && !managementOpen && plugins.length > 0 ? <div className="plugin-installed-strip" aria-label="已安装插件">
          {filteredPlugins.map((plugin) => <button type="button" key={plugin.id || plugin.name} className="plugin-installed-tile" disabled={busy}
            data-enabled={plugin.enabled} title={`${plugin.displayName || plugin.name} · ${plugin.enabled ? "已启用" : "已停用"}`}
            aria-label={`管理插件 ${plugin.id || plugin.name}`} onClick={() => setManagementOpen(true)}>
            {pluginLogo(plugin, 32)}
          </button>)}
          {filteredPlugins.length === 0 && <p className="plugin-section-description">没有匹配的已安装插件。</p>}
        </div> : <div className="plugin-local-list">
          {filteredPlugins.map((plugin) => {
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
                  {pluginLogo(plugin, 28)}
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
                  {catalog && plugin.marketplace && <span className="plugin-origin">{plugin.marketplace}</span>}
                  {(plugin.load_errors?.length ?? 0) > 0 && <p className="plugin-runtime-error" role="status">{pluginErrorText(plugin)}</p>}
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
                    aria-label={`${plugin.enabled ? "停用" : "启用"}插件 ${pluginKey}`}
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
                    aria-label={`卸载插件 ${pluginKey}`}
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
                onClick={() => {
                  setManagementOpen(true);
                  if (isDesktop()) void choosePluginDirectory();
                  else window.requestAnimationFrame(() => importInputRef.current?.focus());
                }}
                disabled={busy}
              >
                {isDesktop() ? "选择插件文件夹" : "填写插件路径"}
              </button>
            </div>
          )}
        </div>}
      </Section>

      <section className="plugin-marketplace-catalog" aria-label="插件目录">
        <div className="plugin-catalog-heading"><h2>插件目录</h2><select aria-label="插件来源" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="">全部来源</option>{marketplaces.map((source) => <option key={source.name} value={source.name}>{source.name}</option>)}</select></div>
        {marketplaceError && <div className="plugin-load-error" role="alert" aria-label="插件来源加载失败"><span>{marketplaceError}</span><button type="button" onClick={() => void loadMarketplaces()}>重试来源</button></div>}
        {marketplaces.filter((source) => !sourceFilter || source.name === sourceFilter).map((source) => {
          const items = source.plugins.filter((plugin) => !q || `${plugin.name} ${plugin.description ?? ""} ${plugin.interface?.displayName ?? ""}`.toLocaleLowerCase().includes(q));
          return <section className="plugin-source-catalog" key={source.name}><div className="plugin-catalog-heading"><h3>{source.name}</h3><button type="button" className="plugin-icon-button" disabled={busy} onClick={() => void changeMarketplace("refresh", source.name)} aria-label={`同步来源 ${source.name}`} title="同步来源"><RefreshCw /></button></div>
            {source.error && <p role="alert">{source.error}</p>}
            {source.status !== "ready" && <p className="plugin-section-description">同步来源后显示可安装的插件。</p>}
            <div className="plugin-discover-grid">{items.map((plugin) => {
              const installed = plugins.some((item) => item.id === plugin.id);
              const title = plugin.interface?.displayName || plugin.name;
              return <article className="plugin-discover-row" key={plugin.id}><span className="plugin-local-icon"><BrandIcon value={title} inferBrand={false} fallback="plugin" size={28} /></span>
                <div className="plugin-local-copy"><strong>{title}</strong><p title={plugin.load_error || plugin.description}>{plugin.load_error || plugin.interface?.shortDescription || plugin.description || source.name}</p>{plugin.category && <small>{plugin.category}</small>}</div>
                <button type="button" className="skills-text-button" disabled={busy || installed || Boolean(plugin.load_error)} onClick={() => void installPlugin(plugin, source.name)} aria-label={`安装插件 ${plugin.id}`}>{savingName === plugin.id ? "安装中…" : installed ? "已安装" : "安装"}</button>
              </article>;
            })}</div>
            {source.status === "ready" && items.length === 0 && <p className="plugin-section-description">没有匹配的插件。</p>}
          </section>;
        })}
        {!marketplaceError && marketplaces.length === 0 && <div className="plugin-empty"><Blocks /><strong>尚未添加插件来源</strong><p>添加 GitHub 仓库、Git 地址或本地插件市场。</p><button type="button" onClick={() => setManagementOpen(true)}>添加插件来源</button></div>}
      </section>

      {managementOpen && <>
      <Section title="插件来源" description="来源名称需与市场清单中的名称一致。">
        <form className="plugin-source-form" onSubmit={(event) => { event.preventDefault(); void changeMarketplace("add", sourceName); }}>
          <input aria-label="来源名称" placeholder="来源名称" value={sourceName} onChange={(event) => setSourceName(event.target.value)} required disabled={busy} />
          <select aria-label="来源类型" value={sourceKind} onChange={(event) => setSourceKind(event.target.value)} disabled={busy}><option value="github">GitHub</option><option value="git">Git URL</option><option value="directory">本地文件夹</option></select>
          <input aria-label="来源地址" placeholder={sourceKind === "github" ? "owner/repository" : sourceKind === "git" ? "https://…/repo.git" : "插件市场文件夹路径"} value={sourceLocator} onChange={(event) => setSourceLocator(event.target.value)} required disabled={busy} />
          <button type="submit" disabled={busy || !sourceName.trim() || !sourceLocator.trim()}>添加来源</button>
        </form>
        {marketplaces.map((source) => <div className="plugin-source-record" key={source.name}><div><strong>{source.name}</strong><p>{source.source.repo || source.source.url || source.source.path}</p></div><button type="button" className="plugin-icon-button" disabled={busy} aria-label={`移除来源 ${source.name}`} onClick={() => void changeMarketplace("remove", source.name)}><Trash2 /></button></div>)}
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
      </>}
      {active && addMenu && <ContextMenu position={addMenu} onClose={() => setAddMenu(null)} items={[
        { label: "导入本地插件", icon: <FolderOpen />, onClick: () => { setManagementOpen(true); window.requestAnimationFrame(() => importInputRef.current?.focus()); } },
        { label: "添加插件来源", icon: <Blocks />, onClick: () => setManagementOpen(true) },
      ]} />}
    </div>
  );
};
