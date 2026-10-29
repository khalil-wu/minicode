import { useI18n } from "../i18n";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  AtSign,
  Blocks,
  Check,
  ChevronDown,
  ChevronRight,
  LockKeyhole,
  Folder,
  LoaderCircle,
  RefreshCw,
  Search,
  Settings,
  Plus,
  PlugZap,
  Trash2,
} from "lucide-react";
import { BrandIcon } from "../components/BrandIcon";
import { ContextMenu } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import { invalidateConversationDraftSelection } from "../stores/conversation-drafts";
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
import { fetchJsonWithStartupRetry, formatSettingsLoadError } from "./settingsLoad";
import { showConfirm } from "./DialogService";
import "./PluginsTab.css";
import { openSettings } from "../lib/settings-navigation";
import { normalizeWorkspaceRoot, workspaceRootsEqual } from "../lib/workspace-path";
import { PluginMarketplaceDialog, type MarketplaceAddParams } from "./PluginMarketplaceDialog";
import { MarketplaceRemoveDialog, MarketplaceSettings, marketplaceName } from "./MarketplaceSettings";
import { InstalledSkillDetailsDialog } from "./InstalledSkillDetailsDialog";
import { useSkillSettings } from "./useSkillSettings";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import {
  captureExtensionSettingsOwner,
  extensionSettingsOwnerMatches,
  publishExtensionSettingsChange,
  subscribeExtensionSettingsChanges,
  type ExtensionSettingsOwner,
} from "../lib/extension-settings-events";

export type PluginEntry = {
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
  privacyPolicyUrl?: string;
  termsOfServiceUrl?: string;
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
  mcp_server_names?: string[];
  enablement_source?: string;
};

type PluginSettingsPayload = {
  plugins?: PluginEntry[];
  runtime_refresh?: { ok?: boolean; warnings?: string[]; errors?: string[]; refreshed?: string[] };
};

type CatalogPlugin = {
  id: string;
  name: string;
  description?: string;
  category?: string;
  load_error?: string;
  interface?: { displayName?: string; shortDescription?: string; developerName?: string; category?: string;
    capabilities?: string[]; websiteUrl?: string; privacyPolicyUrl?: string; termsOfServiceUrl?: string };
  version?: string;
  longDescription?: string;
  capabilities?: string[];
  skills?: string | string[];
  mcpServers?: string | Record<string, unknown>;
  apps?: string | unknown[] | Record<string, unknown>;
  hooks?: string | unknown[] | Record<string, unknown>;
  iconUrl?: string;
  iconDarkUrl?: string;
  defaultPrompt?: string[];
  skill_count?: number;
  mcp_server_count?: number;
  app_count?: number;
  hook_count?: number;
  installation?: { available: boolean; reason?: string };
};
export type PluginMarketplace = { name: string; status: string; error?: string; source: Record<string, string>; plugins: CatalogPlugin[];
  isBuiltIn?: boolean; directory?: "public" | "personal"; interface?: { displayName?: string }; materialized_path?: string };
type MarketplaceRemoval = { source: PluginMarketplace; api: string; headers: HeadersInit; workspaceRoot: string };

const runtimeErrors = (payload: PluginSettingsPayload): string[] => {
  if (payload.runtime_refresh?.ok !== false) return [];
  const errors = [...(payload.runtime_refresh.errors || []), ...(payload.runtime_refresh.warnings || [])];
  return errors.length ? errors : ["运行时能力刷新未完成。"];
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
  const errors = runtimeErrors(payload);
  const detail = errors.length > 0
    ? errors.join("；")
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

type PluginOperation = "" | "refresh" | "state" | "remove" | "install" | "marketplace";

const requireSuccessfulResponse = async (response: Response, fallback: string): Promise<void> => {
  if (response.ok) return;
  const text = await response.text().catch(() => "");
  throw new Error(errorMessageFromResponseText(text, fallback || `HTTP ${response.status}`));
};

export function PluginSettingsToolbar({ onAddMarketplace, onAddMcpServer, busy = false, onBrowseDirectory }: {
  onAddMarketplace: () => void;
  onAddMcpServer: () => void;
  busy?: boolean;
  onBrowseDirectory?: () => void;
}) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<HTMLElement | null>(null);
  return <div className="plugin-settings-toolbar">
    <button type="button" className="settings-action-button" onClick={onBrowseDirectory ?? (() => useAppStore.getState().toggleSkillsMarketplace("settings", "plugins"))}>{t("浏览目录")}</button>
    <button type="button" className="plugin-primary-button" disabled={busy} aria-haspopup="menu" aria-expanded={Boolean(menu)} onClick={event => setMenu(menu ? null : event.currentTarget)}>{t("添加")}<ChevronDown size={14} /></button>
    {menu && <ContextMenu position={{ x: 0, y: 0 }} anchor={menu} onClose={() => setMenu(null)} ariaLabel={t("添加插件")} items={[
      { label: t("创建插件"), icon: <AtSign />, disabled: true },
      { label: t("添加插件市场"), icon: <Plus />, onClick: onAddMarketplace },
      { label: t("添加 MCP 服务器"), icon: <PlugZap />, onClick: onAddMcpServer },
    ]} />}
  </div>;
}

export const PluginsTab = ({ catalog = false, toolbarHost, active = true, loadInventory = active, searchTarget, managedToolbar = false, search, marketplaceRequest = 0, onPluginsChange, onMarketplacesChange, managedView = "plugins" }: {
  catalog?: boolean; toolbarHost?: HTMLElement | null; active?: boolean; searchTarget?: string;
  managedToolbar?: boolean; search?: string; marketplaceRequest?: number; onPluginsChange?: (count: number) => void; loadInventory?: boolean;
  onMarketplacesChange?: (count: number) => void; managedView?: "plugins" | "marketplaces";
}) => {
  const { t } = useI18n();
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const [plugins, setPlugins] = useState<PluginEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [savingName, setSavingName] = useState("");
  const loadSeqRef = useRef(0);
  const operationRef = useRef<PluginOperation>("");
  const [activeOperation, setActiveOperation] = useState<PluginOperation>("");
  const [marketplaces, setMarketplaces] = useState<PluginMarketplace[]>([]);
  const [marketplaceError, setMarketplaceError] = useState("");
  const [marketplacesLoading, setMarketplacesLoading] = useState(false);
  const [removingMarketplace, setRemovingMarketplace] = useState<MarketplaceRemoval>();
  const [upgradingMarketplace, setUpgradingMarketplace] = useState("");
  const openMarketplaceRemoval = (source: PluginMarketplace) => setRemovingMarketplace({ source, api: apiBase(), headers: authHeaders(), workspaceRoot: workingDirectory });
  const removalOwnerActive = (target: MarketplaceRemoval) => target.api === apiBase()
    && new Headers(target.headers).get("X-MiniCode-Token") === new Headers(authHeaders()).get("X-MiniCode-Token");
  const [query, setQuery] = useState("");
  const [directory, setDirectory] = useState<"public" | "personal">("public");
  const [marketplaceDialogOpen, setMarketplaceDialogOpen] = useState(false);
  const [addMenu, setAddMenu] = useState<{ x: number; y: number } | null>(null);
  const marketplaceRequestRef = useRef<AbortController | null>(null);
  const [detail, setDetail] = useState<{ id: string; installed?: PluginEntry; catalog?: CatalogPlugin; source?: PluginMarketplace } | null>(null);
  const [skillDetailPath, setSkillDetailPath] = useState<string | null>(null);
  const detailedPlugin = detail ? plugins.find(plugin => (plugin.id || plugin.name) === detail.id) : undefined;
  const { pluginSkills, loading: skillsLoading, error: skillsError, pendingPath: pendingSkillPath, reload: reloadSkills, toggle: toggleSkill } = useSkillSettings(active && Boolean(detailedPlugin));
  useEffect(() => { setSkillDetailPath(null); }, [detail?.id, workingDirectory]);
  const tryPlugin = (plugin: PluginEntry, name: string, prompt = plugin.defaultPrompt?.map(value => value.trim()).find(Boolean) || "") => {
    const state = useAppStore.getState();
    state.addSelectedMention({ kind: "plugin", configName: plugin.id || plugin.name, name, path: `plugin://${plugin.id || plugin.name}` });
    if (prompt) state.setDraft(state.draft ? `${state.draft}\n${prompt}` : prompt);
    useAppStore.setState({ settingsOpen: false, skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
    const chat = state.panelSlots.find(slot => slot.kind === "chat");
    if (chat) {
      const maximized = state.panelSlots.find(slot => slot.maximized && slot.id !== chat.id);
      if (maximized) state.togglePanelMaximized(maximized.id);
      state.focusPanel(chat.id);
    } else state.addPanel({ id: "main-chat", kind: "chat", label: t("对话") });
    window.requestAnimationFrame(() => window.dispatchEvent(new Event("composer:focus")));
  };
  const requestedPluginId = useAppStore(state => state.skillsMarketplaceSelectedPluginId);
  const [runtimeWarnings, setRuntimeWarnings] = useState<string[]>([]);
  const detailRef = useRef<HTMLElement>(null);
  const detailTrigger = useRef<HTMLElement | null>(null);
  const openDetail = (next: NonNullable<typeof detail>) => { detailTrigger.current = document.activeElement as HTMLElement; setDetail(next); };
  useEffect(() => {
    if (!catalog || !active || !requestedPluginId) return;
    const installed = plugins.find(plugin => (plugin.id || plugin.name) === requestedPluginId);
    if (installed) {
      openDetail({ id: requestedPluginId, installed });
      useAppStore.setState({ skillsMarketplaceSelectedPluginId: null });
      return;
    }
    for (const source of marketplaces) {
      const entry = source.plugins.find(plugin => plugin.id === requestedPluginId);
      if (entry) {
        openDetail({ id: requestedPluginId, catalog: entry, source });
        useAppStore.setState({ skillsMarketplaceSelectedPluginId: null });
        return;
      }
    }
  }, [catalog, active, requestedPluginId, plugins, marketplaces]);
  const closeDetail = () => { setDetail(null); window.requestAnimationFrame(() => detailTrigger.current?.focus()); };
  useEffect(() => { if (detail) detailRef.current?.focus(); }, [detail]);
  useEffect(() => { if (searchTarget === "插件来源" || searchTarget === "添加插件市场") setMarketplaceDialogOpen(true); }, [searchTarget]);
  useEffect(() => { if (marketplaceRequest > 0) setMarketplaceDialogOpen(true); }, [marketplaceRequest]);

  const loadMarketplaces = useCallback(async () => {
    marketplaceRequestRef.current?.abort();
    const controller = new AbortController();
    marketplaceRequestRef.current = controller;
    setMarketplaceError("");
    setMarketplacesLoading(true);
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/marketplaces`, { cache: "no-store", headers: authHeaders(), signal: controller.signal });
      await requireSuccessfulResponse(response, "插件来源加载失败");
      const payload = await response.json() as { marketplaces: PluginMarketplace[] };
      if (!controller.signal.aborted) {
        setMarketplaces(payload.marketplaces);
        onMarketplacesChange?.(payload.marketplaces.filter(source => !source.isBuiltIn).length);
        return true;
      }
    } catch (error) {
      if (!controller.signal.aborted) setMarketplaceError(operationError(error));
      return false;
    } finally {
      if (!controller.signal.aborted) setMarketplacesLoading(false);
    }
  }, [onMarketplacesChange]);
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

  const readPluginSettings = useCallback((workspaceRoot: string) => {
    const query = new URLSearchParams({ workspace_root: workspaceRoot });
    return fetchJsonWithStartupRetry<PluginSettingsPayload>(`${apiBase()}/api/plugins?${query}`, {
      cache: "no-store", headers: authHeaders(),
    });
  }, []);

  const applyMutationPayload = async (payload: PluginSettingsPayload, owner: ExtensionSettingsOwner) => {
    publishExtensionSettingsChange("plugins", owner, loadSeqRef);
    publishExtensionSettingsChange("skills", owner, loadSeqRef);
    const state = useAppStore.getState();
    if (extensionSettingsOwnerMatches(owner) && state.activeConversationDraftId) {
      void invalidateConversationDraftSelection(state.activeConversationDraftId);
    }
    // User-level writes affect whichever project is visible when they commit.
    const workspaceRoot = useAppStore.getState().workingDirectory;
    const ownsView = () => workspaceRootsEqual(workspaceRoot, useAppStore.getState().workingDirectory);
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setLoadError("");
    setRuntimeWarnings(payload.runtime_refresh?.ok === false ? payload.runtime_refresh.warnings || ["运行时尚未完成插件加载，请刷新后查看组件状态。"] : []);
    try {
      const scopedPayload = await readPluginSettings(workspaceRoot);
      const updatedPlugins = normalizePlugins(scopedPayload.plugins);
      if (loadSeqRef.current === seq && ownsView()) {
        setPlugins(updatedPlugins);
        onPluginsChange?.(updatedPlugins.length);
      }
      return { plugins: updatedPlugins, workspaceRoot };
    } catch (error) {
      if (loadSeqRef.current === seq && ownsView()) setLoadError(formatSettingsLoadError(error));
      throw error;
    } finally {
      if (loadSeqRef.current === seq && ownsView()) setLoading(false);
    }
  };

  const refresh = useCallback(async (options: { showToast?: boolean } = {}) => {
    const workspaceRoot = useAppStore.getState().workingDirectory;
    const seq = loadSeqRef.current + 1;
    loadSeqRef.current = seq;
    setLoading(true);
    setLoadError("");
    try {
      const payload = await readPluginSettings(workspaceRoot);
      if (loadSeqRef.current !== seq || !workspaceRootsEqual(workspaceRoot, useAppStore.getState().workingDirectory)) return;
      const inventory = normalizePlugins(payload.plugins);
      setPlugins(inventory);
      onPluginsChange?.(inventory.length);
      setRuntimeWarnings(payload.runtime_refresh?.ok === false ? payload.runtime_refresh.warnings || ["运行时尚未完成插件加载，请刷新后查看组件状态。"] : []);
      if (options.showToast) pushToast("插件列表已刷新", "success");
    } catch (error) {
      if (loadSeqRef.current !== seq || !workspaceRootsEqual(workspaceRoot, useAppStore.getState().workingDirectory)) return;
      const message = formatSettingsLoadError(error);
      setLoadError(message);
      if (options.showToast) pushToast(`插件设置加载失败：${message}`, "error");
    } finally {
      if (loadSeqRef.current === seq) setLoading(false);
    }
  }, [readPluginSettings, workingDirectory, onPluginsChange]);

  useEffect(() => { setPlugins([]); setDetail(null); setRuntimeWarnings([]); }, [workingDirectory]);

  useEffect(() => {
    if (loadInventory) { void refresh(); void loadMarketplaces(); }
  }, [loadInventory, refresh, loadMarketplaces]);
  useEffect(() => {
    if (!loadInventory) return;
    return subscribeExtensionSettingsChanges("plugins", loadSeqRef, () => { void refresh(); });
  }, [loadInventory, refresh]);
  useEffect(() => {
    if (!loadInventory) return;
    return subscribeExtensionSettingsChanges("marketplaces", marketplaceRequestRef, () => { void loadMarketplaces(); });
  }, [loadInventory, loadMarketplaces]);
  useEffect(() => { if (!active) setAddMenu(null); }, [active]);

  const setPluginEnabled = async (plugin: PluginEntry, enabled: boolean) => {
    if (plugin.policy_managed) return;
    if (!beginOperation("state")) return;
    const owner = captureExtensionSettingsOwner();
    const pluginKey = plugin.id || plugin.name;
    setSavingName(pluginKey);
    let committed = false;
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
      committed = true;
      const payload = await response.json() as PluginSettingsPayload;
      const runtimeReady = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.plugins" }, { silent: true });
      const inventory = await applyMutationPayload(payload, owner);
      const updated = inventory.plugins.find((item) => (item.id || item.name) === pluginKey)!;
      if (runtimeReady && !workspaceRootsEqual(inventory.workspaceRoot, workingDirectory)) {
        pushToast(`${t("已保存")} · ${plugin.name}`, "success");
      } else if (runtimeReady && updated.enabled !== enabled) {
        pushToast(`插件设置已保存，但${enabled ? "尚未启用" : "尚未停用"}：${pluginErrorText(updated) || "请检查插件依赖与策略"}`, "warning");
      } else if (runtimeReady) pushToast(`${enabled ? "已启用" : "已停用"}插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`${committed ? "插件设置已保存，但状态刷新失败" : "插件更新失败"}：${operationError(error)}`, committed ? "warning" : "error");
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
    const owner = captureExtensionSettingsOwner();
    const pluginKey = plugin.id || plugin.name;
    setSavingName(pluginKey);
    let committed = false;
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
      committed = true;
      const payload = await response.json() as PluginSettingsPayload;
      const runtimeReady = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      await applyMutationPayload(payload, owner);
      if (runtimeReady) pushToast(`已卸载插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`${committed ? "插件已卸载，但状态刷新失败" : "插件卸载失败"}：${operationError(error)}`, committed ? "warning" : "error");
    } finally {
      setSavingName("");
      endOperation("remove");
    }
  };

  const busy = Boolean(activeOperation) || loading;
  const q = (search ?? query).trim().toLocaleLowerCase();
  const filteredPlugins = useMemo(() => plugins.filter((plugin) => !q || `${plugin.name} ${plugin.displayName} ${plugin.description}`.toLocaleLowerCase().includes(q)), [plugins, q]);
  const installedPluginIds = useMemo(() => new Set(plugins.map(plugin => plugin.id || plugin.name)), [plugins]);
  const directorySources = useMemo(() => marketplaces.filter(source => (source.directory ?? "personal") === directory), [marketplaces, directory]);
  const catalogSections = useMemo(() => {
    const sections = new Map<string, { title: string; items: { plugin: CatalogPlugin; source: PluginMarketplace }[] }>();
    for (const source of directorySources) for (const plugin of source.plugins) {
      if (q && ![plugin.name, plugin.description, plugin.interface?.displayName, plugin.interface?.shortDescription,
        plugin.interface?.category, plugin.category, marketplaceName(source)].filter(Boolean).join(" ").toLocaleLowerCase().includes(q)) continue;
      const title = directory === "personal" ? marketplaceName(source) : plugin.interface?.category || plugin.category || marketplaceName(source);
      const key = directory === "personal" ? source.name : title;
      let section = sections.get(key);
      if (!section) { section = { title, items: [] }; sections.set(key, section); }
      section.items.push({ plugin, source });
    }
    return Array.from(sections, ([id, section]) => ({ id, ...section }));
  }, [directorySources, directory, q]);
  const pluginLogo = (plugin: PluginEntry, size: number) => <BrandIcon
    value={plugin.displayName || plugin.name} inferBrand={false} size={size}
    iconUrl={resolvedTheme === "dark" && plugin.iconVariants?.includes("logo-dark")
      ? pluginAssetResourceUrlWithToken(plugin.path, "logo-dark") : plugin.iconUrl}
    websiteUrl={plugin.websiteUrl}
  />;
  const catalogLogo = (plugin: CatalogPlugin, size: number) => <BrandIcon value={plugin.interface?.displayName || plugin.name}
    inferBrand={false} fallback="plugin" size={size} iconUrl={resolvedTheme === "dark" ? plugin.iconDarkUrl || plugin.iconUrl : plugin.iconUrl} />;

  const installPlugin = async (plugin: CatalogPlugin, marketplace: string) => {
    if (!beginOperation("install")) return;
    const owner = captureExtensionSettingsOwner();
    setSavingName(plugin.id);
    let committed = false;
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/install`, {
        method: "POST", headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ plugin_name: plugin.name, marketplace, refresh_marketplace: false }),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "插件安装超时，请刷新查看安装状态。" });
      await requireSuccessfulResponse(response, "安装插件失败");
      committed = true;
      const payload = await response.json() as PluginSettingsPayload;
      const ready = reportRuntimeRefresh(payload);
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "plugins.install" }, { silent: true });
      await applyMutationPayload(payload, owner);
      if (ready) pushToast(`已安装插件：${plugin.name}`, "success");
    } catch (error) {
      pushToast(`${committed ? "插件已安装，但状态刷新失败" : "插件安装失败"}：${operationError(error)}`, committed ? "warning" : "error");
    } finally {
      setSavingName(""); endOperation("install");
    }
  };

  const upgradeMarketplace = async (name: string) => {
    if (!beginOperation("marketplace")) return;
    const owner = captureExtensionSettingsOwner();
    setUpgradingMarketplace(name);
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/plugins/marketplaces/${encodeURIComponent(name)}/refresh`, {
        method: "POST", headers: authHeaders({ "content-type": "application/json" }),
      }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "插件来源操作超时，请刷新查看状态。" });
      await requireSuccessfulResponse(response, "插件来源更新失败");
      publishExtensionSettingsChange("marketplaces", owner, marketplaceRequestRef);
      await loadMarketplaces();
      pushToast("插件目录已同步", "success");
    } catch (error) {
      pushToast(operationError(error), "error");
    } finally {
      setUpgradingMarketplace("");
      endOperation("marketplace");
    }
  };

  const addMarketplace = async (params: MarketplaceAddParams) => {
    const owner = captureExtensionSettingsOwner();
    const response = await fetchWithTimeout(`${apiBase()}/api/plugins/marketplaces`, {
      method: "POST", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify(params),
    }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "添加插件市场超时，请刷新查看状态。" });
    await requireSuccessfulResponse(response, "添加插件市场失败");
    const payload = await response.json() as { marketplace: PluginMarketplace; already_added: boolean };
    publishExtensionSettingsChange("marketplaces", owner, marketplaceRequestRef);
    const reloaded = await loadMarketplaces();
    pushToast(payload.already_added ? `已添加过插件市场：${payload.marketplace.name}` : `已添加插件市场：${payload.marketplace.name}`, "success");
    if (!reloaded) pushToast(t("插件市场已添加，但目录刷新失败。"), "warning");
    if (catalog) setDirectory("personal");
  };

  const loadMarketplacePlugins = useCallback(async (name: string, signal: AbortSignal) => {
    const target = removingMarketplace!;
    const query = new URLSearchParams({ workspace_root: target.workspaceRoot });
    const response = await fetchWithTimeout(`${target.api}/api/plugins?${query}`, { cache: "no-store", headers: target.headers, signal });
    await requireSuccessfulResponse(response, "无法加载已安装插件");
    const payload = await response.json() as PluginSettingsPayload;
    return normalizePlugins(payload.plugins).filter(plugin => plugin.marketplace?.toLocaleLowerCase() === name.toLocaleLowerCase());
  }, [removingMarketplace]);

  const uninstallMarketplacePlugin = async (plugin: PluginEntry) => {
    const target = removingMarketplace!;
    const response = await fetchWithTimeout(`${target.api}/api/plugins/${encodeURIComponent(plugin.id || plugin.name)}`, {
      method: "DELETE", headers: target.headers,
    }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: "卸载插件超时，请重试。" });
    await requireSuccessfulResponse(response, "卸载插件失败");
    const payload = await response.json() as PluginSettingsPayload;
    if (removalOwnerActive(target)) {
      const owner = { base: target.api, token: new Headers(target.headers).get("X-MiniCode-Token") || "" };
      void applyMutationPayload(payload, owner).catch(error => {
        pushToast(`插件已卸载，但状态刷新失败：${operationError(error)}`, "warning");
      });
      sendClientCommand({ type: "skills.list" }, { silent: true });
      sendClientCommand({ type: "mcp.list" }, { silent: true });
    }
    return runtimeErrors(payload);
  };

  const refreshMarketplaceRuntime = async () => {
    const target = removingMarketplace!;
    const response = await fetchWithTimeout(`${target.api}/api/plugins/runtime/refresh`, { method: "POST", headers: target.headers }, { timeoutMs: LONG_HTTP_TIMEOUT_MS });
    await requireSuccessfulResponse(response, "运行时能力刷新失败");
    const payload = await response.json() as PluginSettingsPayload;
    const errors = runtimeErrors(payload);
    if (errors.length) throw new Error(errors.join("\n"));
  };

  const removeMarketplace = async (source: PluginMarketplace) => {
    const target = removingMarketplace!;
    const response = await fetchWithTimeout(`${target.api}/api/plugins/marketplaces/${encodeURIComponent(source.name)}`, { method: "DELETE", headers: target.headers });
    await requireSuccessfulResponse(response, "移除插件市场失败");
    const payload = await response.json() as { marketplaces: PluginMarketplace[] };
    if (removalOwnerActive(target)) {
      marketplaceRequestRef.current?.abort();
      setMarketplacesLoading(false);
      setMarketplaceError("");
      setMarketplaces(payload.marketplaces);
      onMarketplacesChange?.(payload.marketplaces.filter(entry => !entry.isBuiltIn).length);
      publishExtensionSettingsChange("marketplaces", {
        base: target.api, token: new Headers(target.headers).get("X-MiniCode-Token") || "",
      }, marketplaceRequestRef);
    }
    pushToast(t("已移除插件市场：{name}", { name: marketplaceName(source) }), "success");
  };

  return (
    <div className={catalog ? "plugins-catalog" : "plugins-settings"}>
      {managedView === "marketplaces" ? <MarketplaceSettings marketplaces={marketplaces} search={search || ""} loading={marketplacesLoading}
        error={marketplaceError} busy={busy} upgrading={upgradingMarketplace} onRetry={() => void loadMarketplaces()} onUpgrade={source => void upgradeMarketplace(source.name)} onRemove={openMarketplaceRemoval} /> : <>
      {detail && (() => {
        const installed = plugins.find((plugin) => (plugin.id || plugin.name) === detail.id);
        const entry = detail.catalog;
        const name = installed?.displayName || entry?.interface?.displayName || installed?.name || entry?.name || detail.id;
        const starterPrompts = (installed?.defaultPrompt || []).map(prompt => prompt.trim()).filter(Boolean);
        const componentSkills = installed ? pluginSkills.filter(skill => skill.source_level === "plugin" && skill.path
          && normalizeWorkspaceRoot(skill.path).startsWith(`${normalizeWorkspaceRoot(installed.path)}/`)) : [];
        const developer = installed?.developerName || entry?.interface?.developerName;
        const category = installed?.category || entry?.interface?.category || entry?.category;
        const capabilities = installed?.capabilities || entry?.interface?.capabilities || entry?.capabilities;
        const version = installed?.version || entry?.version;
        const informationLinks = [
          { label: t("插件网站"), url: installed?.websiteUrl || entry?.interface?.websiteUrl, required: true },
          { label: t("插件隐私政策"), url: installed?.privacyPolicyUrl || entry?.interface?.privacyPolicyUrl },
          { label: t("插件服务条款"), url: installed?.termsOfServiceUrl || entry?.interface?.termsOfServiceUrl },
        ];
        const hasInformation = developer || category || capabilities?.length || version || informationLinks.some(link => link.url);
        const failures = installed?.load_errors || (entry?.load_error ? [entry.load_error] : []);
        const mcpPrefix = `plugin:${installed?.id || detail.id}:`;
        const declaredMcp = (installed?.mcp_server_names || []).map((name) => ({ name, id: `${mcpPrefix}${name}` }));
        const componentMcp = mcpServers.filter((server) => declaredMcp.some((component) => component.id === server.name));
        const observedNames = new Set(componentMcp.map((server) => server.name));
        const missingMcp = declaredMcp.filter((component) => !observedNames.has(component.id)).map((component) => component.name);
        const disconnected = componentMcp.filter((server) => !['connected', 'disabled'].includes(server.phase || server.status));
        const status = failures.length ? "加载失败" : !installed ? "尚未安装" : !installed.enabled ? "已安装 · 已停用" : disconnected.length ? "需要连接或登录" : missingMcp.length ? "组件状态待同步" : "已启用";
        const openConfiguration = (tab: "connectors" | "skills") => { useAppStore.setState({ skillsMarketplaceOpen: false }); openSettings(tab); };
        return <section ref={detailRef} tabIndex={-1} className="plugin-detail-page" aria-label={t("插件详情")} onKeyDown={(event) => {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeDetail(); }
        }}>
          <nav className="plugin-detail-breadcrumb" aria-label={t("插件位置")}><button type="button" className="plugin-detail-back" aria-label={t("返回插件列表")} onClick={closeDetail}>{t("插件")}<ChevronDown size={14} /></button><span>{name}</span></nav>
          <div className="plugin-detail-heading">{installed ? pluginLogo(installed, 40) : entry ? catalogLogo(entry, 40) : null}<div><h2>{name}</h2><span>{status}{installed?.version || entry?.version ? ` · ${installed?.version || entry?.version}` : ""}</span></div>
            {installed ? <button type="button" className="plugin-primary-button plugin-detail-try" disabled={busy || Boolean(installed.policy_managed && !installed.enabled)} onClick={() => installed.enabled ? tryPlugin(installed, name) : void setPluginEnabled(installed, true)}>{installed.enabled ? t("立即试用") : t("启用插件")}</button>
              : entry && detail.source && <button type="button" className="plugin-primary-button plugin-detail-try" disabled={busy || Boolean(entry.load_error) || entry.installation?.available === false}
                title={entry.installation?.reason || entry.load_error} onClick={() => void installPlugin(entry, detail.source!.name)}>{savingName === entry.id ? <LoaderCircle size={16} className="settings-spin" /> : <Plus size={16} />}{savingName === entry.id ? t("安装中…") : t("安装插件")}</button>}</div>
          <p className="plugin-detail-description">{installed?.longDescription || entry?.longDescription || installed?.description || entry?.description || installed?.shortDescription || entry?.interface?.shortDescription || t("此来源未提供用途说明。")}</p>
          {installed?.enabled && starterPrompts.length ? <div className="plugin-starter-prompts" aria-label="插件起始提示">
            {starterPrompts.map(prompt => <button key={prompt} type="button" onClick={() => tryPlugin(installed, name, prompt)}>{prompt}<ChevronDown size={15} /></button>)}
          </div> : null}

          {(failures.length > 0 || runtimeWarnings.length > 0) && <div className="plugin-detail-issues" role="status"><strong>{t("需要处理")}</strong>
            {failures.map((failure) => <p key={failure}>{failure === "dependency-unsatisfied" ? `插件依赖尚未满足：${installed?.dependencies?.join("、") || "查看插件清单中的依赖"}` : failure}</p>)}
            {runtimeWarnings.map((warning) => <p key={warning}>{warning}</p>)}
            <button type="button" className="skills-text-button" onClick={() => { void refresh({ showToast: true }); void reloadSkills(); }} disabled={busy}>刷新加载状态</button>
          </div>}
          <section className="plugin-detail-components" aria-label="插件组件"><h3>包含的组件</h3>
            {installed ? <>
              {Boolean(installed.mcp_server_count) && <div><span>MCP 服务器 · {installed.mcp_server_count}</span><button type="button" className="skills-text-button" onClick={() => openConfiguration("connectors")}>配置 MCP 服务</button></div>}
              {componentMcp.map((server) => <p className="plugin-component-status" key={server.name}>{server.name.slice(mcpPrefix.length)} · {server.phase === "auth_required" || server.phase === "expired" ? t("需要登录") : (server.phase || server.status) === "connected" ? t("已连接") : t("尚未连接")}{server.lastError ? ` · ${server.lastError}` : ""}</p>)}
              {missingMcp.map((name) => <p className="plugin-component-status" key={name}>{name} · 状态未知（待同步）</p>)}
              {(componentSkills.length > 0 || skillsLoading || skillsError) && <section className="plugin-detail-skills" aria-label={t("技能")}><h3>{t("技能")}{!skillsLoading && ` · ${componentSkills.length}`}</h3>
                {skillsLoading && <p aria-live="polite">{t("正在读取技能…")}</p>}
                {skillsError && <p role="alert">{skillsError}</p>}
                {componentSkills.map(skill => <div className="plugin-detail-skill" key={skill.path}>
                  <BrandIcon value={skill.display_name || skill.name} inferBrand={false} fallback="skill" size={24} iconUrl={skill.icon_large || skill.icon} />
                  <button type="button" className="plugin-detail-skill-copy" onClick={() => setSkillDetailPath(skill.path)} aria-label={`${t("查看技能详情")} ${skill.name}`}><strong>{skill.display_name || skill.name}</strong><span>{skill.short_description || skill.description}</span></button>
                  {installed.enabled && <button type="button" className="settings-toggle" role="switch" aria-checked={skill.enabled} data-active={skill.enabled} disabled={skillsLoading || Boolean(pendingSkillPath)}
                    aria-label={`${skill.enabled ? t("停用") : t("启用")} ${skill.display_name || skill.name}`} onClick={() => void toggleSkill(skill)}><span /></button>}
                </div>)}
              </section>}
              {Boolean(installed.app_count) && <div><span>App · {installed.app_count}{installed.runtime_support?.apps === false ? "（仅清单，当前未执行）" : ""}</span></div>}
              {Boolean(installed.hook_count) && <div><span>Hook · {installed.hook_count}{installed.runtime_support?.hooks === false ? "（当前未执行）" : ""}</span></div>}
            </> : <>
              {entry?.skills && <div><span>{t("技能")}</span></div>}
              {entry?.mcpServers && Object.keys(entry.mcpServers).length > 0 && <div><span>MCP 服务器</span></div>}
              {entry?.apps && Object.keys(entry.apps).length > 0 && <div><span>App</span></div>}
              {entry?.hooks && Object.keys(entry.hooks).length > 0 && <div><span>Hook</span></div>}
              {!entry?.skills && !entry?.mcpServers && !entry?.apps && !entry?.hooks && <p>{entry?.capabilities?.length ? entry.capabilities.join("、") : t("来源尚未提供完整组件清单，安装后可查看技能、MCP、App 和 Hook 的实际状态。")}</p>}
            </>}
          </section>
          {hasInformation && <section className="plugin-detail-registration" aria-label={t("插件信息")}><h3>{t("插件信息")}</h3><dl className="plugin-detail-meta">
            {Boolean(capabilities?.length) && <div><dt>{t("插件能力")}</dt><dd>{capabilities!.join(", ")}</dd></div>}
            {developer && <div><dt>{t("插件开发者")}</dt><dd>{developer}</dd></div>}
            {category && <div><dt>{t("插件分类")}</dt><dd>{category}</dd></div>}
            {version && <div><dt>{t("插件版本")}</dt><dd>{version}</dd></div>}
            {informationLinks.filter(link => link.url || link.required).map(link => <div key={link.label}><dt>{link.label}</dt><dd>{link.url
              ? <a href={link.url} onClick={event => { event.preventDefault(); void openPreferredWebLink(link.url!); }}>{link.url}</a>
              : t("插件信息不可用")}</dd></div>)}
          </dl></section>}
          {entry?.installation?.available === false && !installed && <p className="plugin-section-description" role="status">{entry.installation.reason}</p>}
          <div className="plugin-detail-actions">{installed?.enabled && !installed.policy_managed ? <button type="button" className="skills-text-button" disabled={busy} onClick={() => void setPluginEnabled(installed, false)}>{t("停用插件")}</button> : null}
            {installed?.managed && !installed.policy_managed && <button type="button" className="skills-text-button" disabled={busy} onClick={() => void removePlugin(installed)}>{t("卸载插件")}</button>}
          </div>
          {installed && <details className="plugin-detail-location"><summary>{t("本地文件")}</summary><code>{installed.path}</code>{installed.manifest_path && <code>{installed.manifest_path}</code>}</details>}
          {skillDetailPath && (() => {
            const selected = componentSkills.find(skill => skill.path === skillDetailPath);
            return active && selected && <InstalledSkillDetailsDialog skill={selected} workspaceRoot={workingDirectory} pending={skillsLoading || Boolean(pendingSkillPath)}
              readOnly={!installed?.enabled} onToggle={() => void toggleSkill(selected)} onClose={() => setSkillDetailPath(null)} />;
          })()}
        </section>;
      })()}
      <div className="plugin-list-page" hidden={Boolean(detail)}>
      {!catalog && !managedToolbar && (!toolbarHost || active) && !detail && (() => {
        const toolbar = <PluginSettingsToolbar busy={busy} onAddMarketplace={() => setMarketplaceDialogOpen(true)} onAddMcpServer={() => openSettings("connectors")} />;
        return toolbarHost ? createPortal(toolbar, toolbarHost) : toolbar;
      })()}
      {catalog && <>
        <header className="plugin-catalog-page-header">
          <div className="skills-page-heading"><h1>{t("插件")}</h1><p>{t("连接插件，让 MiniCode 在你的各类工具中协同工作")}</p></div>
          <div className="plugin-catalog-toolbar">
            <label className="skills-search"><Search aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("搜索插件")} aria-label={t("搜索插件")} /></label>
            <button type="button" className="skills-icon-button" disabled={busy || marketplacesLoading} onClick={() => { void refresh({ showToast: true }); void loadMarketplaces(); }} aria-label={t("刷新插件")} title={t("刷新插件")}><RefreshCw className={loading || marketplacesLoading ? "settings-spin" : undefined} /></button>
            <button type="button" className="skills-icon-button" onClick={() => openSettings("plugins")} aria-label={t("插件设置")} title={t("插件设置")}><Settings /></button>
            <button type="button" className="skills-create-button" disabled={busy} aria-haspopup="menu" aria-expanded={Boolean(addMenu)} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setAddMenu({ x: rect.right - 200, y: rect.bottom + 4 }); }}>{t("添加")}<ChevronDown /></button>
          </div>
        </header>
        <div className="plugin-directory-tabs skills-scope-tabs" role="tablist" aria-label={t("插件目录")} onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? "public" : event.key === "End" ? "personal" : directory === "public" ? "personal" : "public";
          setDirectory(next); event.currentTarget.querySelector<HTMLButtonElement>(`[data-directory="${next}"]`)?.focus();
        }}>
          {(["public", "personal"] as const).map(value => <button type="button" key={value} role="tab" data-directory={value} id={`plugin-directory-${value}`} aria-controls="plugin-directory-results"
            aria-selected={directory === value} tabIndex={directory === value ? 0 : -1} onClick={() => setDirectory(value)}>{value === "public" ? t("公开") : t("个人")}</button>)}
        </div>
      </>}
      {(!catalog || plugins.length > 0 || loadError) && <section className={catalog ? "plugin-catalog-section" : "settings-group plugin-settings-group"}>
        {catalog && <div className="settings-section-heading"><h3 className="settings-section-title">{t("已安装插件")}</h3></div>}
        {loadError && (
          <div className="plugin-load-error" role="alert">
            <div><strong>插件设置加载失败。</strong><code>{loadError}</code></div>
            <button type="button" onClick={() => void refresh({ showToast: true })} disabled={busy}>{t("重试")}</button>
          </div>
        )}

        {catalog && plugins.length > 0 ? <div className="plugin-installed-strip" aria-label={t("已安装插件")}>
          {filteredPlugins.map((plugin) => <button type="button" key={plugin.id || plugin.name} className="plugin-installed-tile" disabled={busy}
            data-enabled={plugin.enabled} title={`${plugin.displayName || plugin.name} · ${plugin.enabled ? t("已启用") : t("已停用")}`}
            aria-label={`管理插件 ${plugin.id || plugin.name}`} onClick={() => openDetail({ id: plugin.id || plugin.name, installed: plugin })}>
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
                    <button type="button" className="plugin-detail-link" aria-label={`查看插件详情 ${pluginKey}`} onClick={() => openDetail({ id: pluginKey, installed: plugin })}>{displayName}</button>
                    {catalog && <span className="plugin-local-state">
                      {saving
                        ? activeOperation === "remove" ? "正在卸载…" : "正在更新…"
                        : plugin.load_errors?.length ? t("加载失败")
                        : plugin.policy_managed
                          ? plugin.enabled ? "策略启用" : "策略停用"
                          : plugin.enabled ? t("已启用") : t("已停用")}
                    </span>}
                    {plugin.policy_managed && (
                      <span className="plugin-policy-lock" title="此插件状态由组织策略管理" aria-label="组织策略锁定">
                        <LockKeyhole aria-hidden="true" />
                      </span>
                    )}
                  </div>
                  <p title={plugin.longDescription || description}>{description}</p>
                  {catalog && plugin.marketplace && <span className="plugin-origin">{plugin.marketplace}</span>}
                  {(plugin.load_errors?.length ?? 0) > 0 && <p className="plugin-runtime-error" role="status">{pluginErrorText(plugin)}</p>}
                  {catalog && (plugin.developerName || (plugin.capabilities?.length ?? 0) > 0) && (
                    <small>{[
                      plugin.developerName ? `开发者：${plugin.developerName}` : "",
                      ...(plugin.capabilities ?? []).slice(0, 3),
                    ].filter(Boolean).join(" · ")}</small>
                  )}
                </div>
                <label className="plugin-switch">
                  <input
                    type="checkbox"
                    role="switch"
                    aria-checked={plugin.enabled}
                    aria-label={`${plugin.enabled ? t("停用") : t("启用")}插件 ${pluginKey}`}
                    checked={plugin.enabled}
                    disabled={busy || plugin.policy_managed}
                    onChange={(event) => void setPluginEnabled(plugin, event.currentTarget.checked)}
                  />
                  <span><i /></span>
                </label>
                <ChevronRight className="plugin-row-chevron" size={14} aria-hidden="true" />
              </article>
            );
          })}
          {loading && plugins.length === 0 && <div className="plugin-empty">正在加载插件…</div>}
          {!loading && plugins.length === 0 && !loadError && (
            <div className="plugin-empty">
              <span className="plugin-empty-icon"><Blocks aria-hidden="true" /></span>
              <strong>{t("还没有本地插件")}</strong>
              <p>{t("从插件市场浏览并安装插件。")}</p>
            </div>
          )}
        </div>}
      </section>}
      {!catalog && marketplaceError && <div className="plugin-load-error" role="alert" aria-label={t("插件来源加载失败")}><span>{marketplaceError}</span><button type="button" onClick={() => void loadMarketplaces()}>{t("重试来源")}</button></div>}

      {catalog && <section className="plugin-marketplace-catalog" id="plugin-directory-results" role="tabpanel" aria-labelledby={`plugin-directory-${directory}`}>
        {marketplaceError && <div className="plugin-load-error" role="alert" aria-label="插件来源加载失败"><span>{marketplaceError}</span><button type="button" onClick={() => void loadMarketplaces()}>重试来源</button></div>}
        {directorySources.filter(source => source.error).map(source => <div className="plugin-load-error" role="alert" key={source.name}><span>{marketplaceName(source)}：{source.error}</span><button type="button" disabled={busy} onClick={() => void upgradeMarketplace(source.name)}>{t("重试")}</button></div>)}
        {marketplacesLoading && marketplaces.length === 0 && <div className="plugin-empty" role="status">{t("正在加载插件…")}</div>}
        {catalogSections.map(section => <section className="plugin-source-catalog" key={section.id} aria-label={section.title}>
          <div className="plugin-catalog-heading"><h3>{section.title}</h3></div>
            <div className="plugin-discover-grid">{section.items.map(({ plugin, source }) => {
              const installed = installedPluginIds.has(plugin.id);
              const title = plugin.interface?.displayName || plugin.name;
              const installing = activeOperation === "install" && savingName === plugin.id;
              return <article className="plugin-discover-row" key={`${source.name}:${plugin.id}`} aria-busy={installing}>
                <button type="button" className="plugin-catalog-open plugin-detail-link" aria-label={`查看插件详情 ${plugin.id}`} onClick={() => openDetail({ id: plugin.id, catalog: plugin, source })}>
                  <span className="plugin-local-icon">{catalogLogo(plugin, 32)}</span>
                  <span className="plugin-local-copy"><span className="plugin-local-title"><span>{title}</span><Folder size={14} aria-hidden="true" /></span><span className="plugin-section-description" title={plugin.load_error || plugin.installation?.reason || plugin.description}>{plugin.load_error || plugin.interface?.shortDescription || plugin.description}</span></span>
                </button>
                <button type="button" className="plugin-catalog-install skills-icon-button" disabled={busy || installed || Boolean(plugin.load_error) || plugin.installation?.available === false}
                  title={plugin.installation?.reason || plugin.load_error || (installed ? t("已安装") : t("安装插件"))}
                  onClick={() => void installPlugin(plugin, source.name)} aria-label={`安装插件 ${plugin.id}`}>{installing ? <LoaderCircle className="settings-spin" /> : installed ? <Check /> : <Plus />}</button>
              </article>;
            })}</div>
          </section>)}
        {!marketplacesLoading && !marketplaceError && catalogSections.length === 0 && <div className="plugin-empty" role="status"><strong>{t("没有找到插件")}</strong>
          {directory === "personal" && directorySources.length === 0 && <p>{t("添加 GitHub 仓库、Git 地址或本地插件市场。")}</p>}
        </div>}
      </section>}

      {active && addMenu && <ContextMenu position={addMenu} onClose={() => setAddMenu(null)} items={[
        { label: t("创建插件"), icon: <AtSign />, disabled: true },
        { label: t("添加插件市场"), icon: <Plus />, onClick: () => setMarketplaceDialogOpen(true) },
      ]} />}
      </div>
      </>}
      {removingMarketplace && <MarketplaceRemoveDialog marketplace={removingMarketplace.source} onLoadInstalled={loadMarketplacePlugins}
        onUninstall={uninstallMarketplacePlugin} onRefreshRuntime={refreshMarketplaceRuntime}
        onRemove={() => removeMarketplace(removingMarketplace.source)} onClose={() => setRemovingMarketplace(undefined)} />}
      {marketplaceDialogOpen && <PluginMarketplaceDialog onAdd={addMarketplace} onClose={() => setMarketplaceDialogOpen(false)} />}
    </div>
  );
};
