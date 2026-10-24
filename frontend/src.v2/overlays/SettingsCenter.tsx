import { useI18n } from "../i18n";
import { lazy, memo, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  CalendarClock,
  Cpu,
  FlaskConical,
  Search,
  ServerOff,
} from "../lib/icons";
import { useAppStore } from "../stores";
import { visibleLeftSidebarWidth } from "../stores/shared-helpers";
import { fetchLLMSettings } from "../protocol/api";
import { runtime } from "../desktop/runtime";
import { openDictationProviderSettings } from "../lib/dictation-preferences";
import { pushToast } from "./ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useEscapeKey } from "../hooks/useFocusTrap";
import "./SettingsCenter.css";
import {
  type Tab,
  type ProviderId,
  type LLMSettingsPayload,
  toUiProvider,
} from "./settingsShared";
import { GeneralTab } from "./GeneralTab";
import { FeatureFlagsTab } from "./FeatureFlagsTab";
import { PluginsTab, PluginSettingsToolbar } from "./PluginsTab";
import { formatSettingsLoadError } from "./settingsLoad";
import { SETTINGS_OPTIONS } from "./settingsSearch";
import { formatShortcut, isCodeShortcut, SHORTCUT_DEFINITIONS } from "../lib/keyboard-shortcuts";
import { ImportSettingsTab } from "./ImportSettingsTab";
import { createPetChat } from "../pets/createPetChat";
import { SourcePetIcon } from "../pets/SourcePetIcon";
import { usePluginsFeature } from "../hooks/usePluginsFeature";
import { useComputerUseSettings } from "../hooks/useComputerUseSettings";
import { ComputerUseSourceIcon } from "../lib/computer-use-icons";
import { SettingsSourceIcon } from "./SettingsSourceIcon";

const OpenSourceLicenses = lazy(() => import("./OpenSourceLicenses").then(m => ({ default: m.OpenSourceLicenses })));
const VoiceSettings = lazy(() => import("./VoiceSettings").then(m => ({ default: m.VoiceSettings })));
const AppearanceTab = lazy(() => import("./AppearanceTab").then(m => ({ default: m.AppearanceTab })));
const PersonalizationTab = lazy(() => import("./PersonalizationTab").then(m => ({ default: m.PersonalizationTab })));
const BrowserIntegrationTab = lazy(() => import("./BrowserIntegrationTab").then(m => ({ default: m.BrowserIntegrationTab })));
const ArchivedTab = lazy(() => import("./ArchivedTab").then(m => ({ default: m.ArchivedTab })));
const WorkspaceGitTab = lazy(() => import("./WorkspaceGitTab").then(m => ({ default: m.WorkspaceGitTab })));
const ShortcutsTab = lazy(() => import("./ShortcutsTab").then(m => ({ default: m.ShortcutsTab })));
const ProviderTab = lazy(() => import("./ProviderTab").then(m => ({ default: m.ProviderTab })));
const ConnectorsTab = lazy(() => import("./ConnectorsTab").then(m => ({ default: m.ConnectorsTab })));
const ConnectionsTab = lazy(() => import("./ConnectionsTab").then(m => ({ default: m.ConnectionsTab })));
const SchedulerTab = lazy(() => import("./SchedulerTab").then(m => ({ default: m.SchedulerTab })));
const SourceEnvironmentsTab = lazy(() => import("./SourceEnvironmentsTab").then(m => ({ default: m.SourceEnvironmentsTab })));
const SkillsTab = lazy(() => import("./SkillsTab").then(m => ({ default: m.SkillsTab })));
const ConfigTab = lazy(() => import("./ConfigTab").then(m => ({ default: m.ConfigTab })));
const HooksTab = lazy(() => import("./HooksTab").then(m => ({ default: m.HooksTab })));
const StorageTab = lazy(() => import("./StorageTab").then(m => ({ default: m.StorageTab })));
const WorktreesTab = lazy(() => import("./WorktreesTab").then(m => ({ default: m.WorktreesTab })));
const SourcePetsTab = lazy(() => import("./SourcePetsTab").then(m => ({ default: m.SourcePetsTab })));
const ComputerUseSettings = lazy(() => import("./ComputerUseSettings").then(m => ({ default: m.ComputerUseSettings })));

export const SettingsCenter = memo(function SettingsCenter() {
  const { t: tr } = useI18n();
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const sidebarWidth = useAppStore((s) => s.leftSidebarExpandedWidth);
  const [windowWidth, setWindowWidth] = useState(window.innerWidth);
  useEffect(() => {
    const resize = () => setWindowWidth(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const pageRef = useRef<HTMLDivElement>(null);
  useEscapeKey(toggleSettings, settingsOpen, pageRef);
  useEffect(() => {
    if (!settingsOpen) return;
    const previous = document.activeElement as HTMLElement;
    const page = pageRef.current!;
    const frame = requestAnimationFrame(() => {
      if (document.activeElement === previous) page.querySelector<HTMLInputElement>(".settings-search input")!.focus();
    });
    return () => {
      cancelAnimationFrame(frame);
      if (previous.isConnected && (page.contains(document.activeElement) || document.activeElement === document.body)) previous.focus({ preventScroll: true });
    };
  }, [settingsOpen]);

  const activeTab = useAppStore((s) => s.settingsTab);
  const setActiveTab = useAppStore((s) => s.setSettingsTab);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const appMode = useAppStore((s) => s.appMode);
  const { enabled: pluginsEnabled } = usePluginsFeature();
  const computerUse = useComputerUseSettings(settingsOpen);
  const mcpCount = useAppStore((s) => s.mcpServers.length);
  const [skillCount, setSkillCount] = useState<number>();
  useEffect(() => { setSkillCount(undefined); }, [workingDirectory]);
  const [provider, setProvider] = useState<ProviderId>("custom");
  const [settingsPayload, setSettingsPayload] = useState<LLMSettingsPayload | null>(null);
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);
  const settingsLoadEpochRef = useRef(0);
  const [settingsLoadState, setSettingsLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [settingsLoadError, setSettingsLoadError] = useState("");
  const [settingsQuery, setSettingsQuery] = useState("");
  const activeTabButtonRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLElement>(null);
  const [toolbarHost, setToolbarHost] = useState<HTMLDivElement | null>(null);
  const [integrationSearch, setIntegrationSearch] = useState("");
  const [pluginCount, setPluginCount] = useState<number>();
  const [marketplaceCount, setMarketplaceCount] = useState<number>();
  const [licensesOpen, setLicensesOpen] = useState(false);
  const generalBeforeLicenses = useRef(0);
  useEffect(() => { if (activeTab !== "general") setLicensesOpen(false); }, [activeTab]);
  useLayoutEffect(() => {
    if (activeTab === "general" && contentRef.current) contentRef.current.scrollTop = licensesOpen ? 0 : generalBeforeLicenses.current;
  }, [licensesOpen]);
  const [marketplaceRequest, setMarketplaceRequest] = useState(0);
  const [mcpEditorRequest, setMcpEditorRequest] = useState(0);
  const pagePositions = useRef(new Map<Tab, number>());
  useLayoutEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = pagePositions.current.get(activeTab) || 0;
  }, [activeTab]);
  const [visitedTabs, setVisitedTabs] = useState<Tab[]>([activeTab]);
  const [searchTarget, setSearchTarget] = useState<{ tab: Tab; title: string; sourceTitle: string } | null>(null);
  useEffect(() => {
    if (settingsOpen) setVisitedTabs((tabs) => [...new Set([...tabs, activeTab])]);
  }, [activeTab, settingsOpen]);

  useEffect(() => {
    if (!settingsOpen || !searchTarget || searchTarget.tab !== activeTab) return;
    let highlighted: Element | undefined;
    let clearHighlight: number;
    const locate = () => {
      const pane = pageRef.current?.querySelector(`[data-settings-tab="${activeTab}"]`);
      const target = Array.from(pane?.querySelectorAll<HTMLElement>("h3, .settings-row-title, .settings-shortcut-action, .feature-flag-row span, [aria-label]") || [])
        .find((element) => [searchTarget.title, searchTarget.sourceTitle].includes(element.textContent?.trim() || "") || [searchTarget.title, searchTarget.sourceTitle].includes(element.getAttribute("aria-label") || ""));
      if (!target) return;
      for (let disclosure = target.closest("details"); disclosure; disclosure = disclosure.parentElement?.closest("details") ?? null) {
        disclosure.open = true;
      }
      highlighted = target.closest(".settings-row, .settings-shortcut-row, .feature-flag-row, section") || target;
      highlighted.classList.add("settings-search-highlight");
      highlighted.scrollIntoView?.({ block: "center", behavior: "auto" });
      const control = target.matches("input, textarea, select, button") ? target : highlighted.querySelector<HTMLElement>("input, textarea, select, button") || target;
      control.focus({ preventScroll: true });
      clearHighlight = window.setTimeout(() => highlighted?.classList.remove("settings-search-highlight"), 2200);
      observer.disconnect();
    };
    const observer = new MutationObserver(locate);
    observer.observe(pageRef.current!, { childList: true, subtree: true });
    locate();
    return () => { observer.disconnect(); window.clearTimeout(clearHighlight); highlighted?.classList.remove("settings-search-highlight"); };
  }, [activeTab, searchTarget, settingsOpen, settingsLoadState, pageRef]);

  const loadSettings = () => {
    const epoch = ++settingsLoadEpochRef.current;
    setSettingsLoadState("loading");
    setSettingsLoadError("");
    void fetchLLMSettings()
      .then((payload) => {
        if (epoch !== settingsLoadEpochRef.current) return;
        const p = toUiProvider(payload);
        const data = payload as LLMSettingsPayload;
        settingsPayloadRef.current = data;
        setSettingsPayload(data);
        setProvider(p);
        setSettingsLoadState("ready");
      })
      .catch((error: unknown) => {
        if (epoch !== settingsLoadEpochRef.current) return;
        setSettingsLoadError(formatSettingsLoadError(error));
        setSettingsLoadState("error");
      });
  };

  // Force a fresh settings load each time the dialog opens, so values changed
  // externally (provider/model/config) are reflected instead of a stale cached
  // payload from the first load. Within one open session, tab switches still
  // reuse the cached payload (no refetch spam).
  useEffect(() => {
    if (settingsOpen) {
      settingsPayloadRef.current = null;
    }
  }, [settingsOpen]);

  useEffect(() => {
    if (!settingsOpen) return;
    if (activeTab !== "general" && activeTab !== "provider") return;
    if (settingsPayloadRef.current) {
      setSettingsLoadState("ready");
      return;
    }
    let cancelled = false;
    const delayMs = activeTab === "general" ? 500 : 0;
    const timer = window.setTimeout(() => {
      if (!cancelled) loadSettings();
    }, delayMs);
    return () => {
      cancelled = true;
      settingsLoadEpochRef.current += 1;
      window.clearTimeout(timer);
    };
  }, [settingsOpen, activeTab]);

  useEffect(() => {
    if (settingsOpen && activeTab === "skills") {
      sendClientCommand({ type: "skills.list" }, { silent: true });
    }
    if (settingsOpen && activeTab === "connectors") {
      sendClientCommand({ type: "mcp.list" }, { silent: true });
    }
    if (settingsOpen && activeTab === "provider") {
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.provider" }, { silent: true });
    }
  }, [settingsOpen, activeTab, conversationId, workingDirectory]);

  useEffect(() => {
    if (!settingsOpen || !window.matchMedia("(max-width: 760px)").matches) return;
    const activeButton = activeTabButtonRef.current;
    if (!activeButton || typeof activeButton.scrollIntoView !== "function") return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    activeButton.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "nearest",
      inline: "center",
    });
  }, [activeTab, settingsOpen]);

  const tabs = [
    { id: "general" as const, group: tr("个人"), label: tr("常规"), description: tr("设置工作方式、消息跟进、过程展示与内容加载。"), keywords: "permissions composer notifications terminal updates", icon: <SettingsSourceIcon icon="general" /> },
    { id: "import" as const, group: tr("个人"), label: tr("导入"), description: tr("将其他 AI 应用的配置、项目和聊天导入 MiniCode"), keywords: "Claude Code Cursor", icon: <SettingsSourceIcon icon="import" /> },
    { id: "appearance" as const, group: tr("个人"), label: tr("外观"), description: tr("调整界面主题、字号与显示密度。"), keywords: tr("系统 浅色 深色 紧凑 字号"), icon: <SettingsSourceIcon icon="appearance" /> },
    { id: "archived" as const, group: tr("个人"), label: tr("已归档聊天"), description: tr("存放已归档任务及消息，并可恢复到任务列表。"), keywords: tr("会话 消息 恢复 隐藏"), icon: <SettingsSourceIcon icon="archived" /> },
    { id: "voice" as const, group: tr("个人"), label: tr("语音"), description: tr("设置麦克风与听写服务，录音转成可编辑草稿。"), keywords: tr("听写 录音 转录 麦克风 词汇"), icon: <SettingsSourceIcon icon="voice" /> },
    { id: "storage" as const, group: tr("个人"), label: tr("存储"), description: tr("管理保存在这台电脑上的聊天和工作树。"), keywords: "disk storage cleanup", icon: <SettingsSourceIcon icon="storage" /> },
    { id: "config" as const, group: tr("个人"), label: tr("配置"), description: tr("为新聊天配置权限、网络访问和智能体回复。"), keywords: "config model permissions", icon: <SettingsSourceIcon icon="config" /> },
    { id: "personalization" as const, group: tr("个人"), label: tr("个性化"), description: tr("管理主机记忆与自定义指令。"), keywords: tr("INSTRUCTIONS.md AGENTS.md 摘要 偏好 规则"), icon: <SettingsSourceIcon icon="personalization" /> },
    { id: "pets" as const, group: tr("个人"), label: tr("Mini 与宠物"), description: tr("Mini 管理聊天并显示需要关注的事项"), keywords: "mini pets avatar sprite hatch", icon: <SourcePetIcon active={activeTab === "pets"} /> },
    { id: "shortcuts" as const, group: tr("个人"), label: tr("快捷键"), description: tr("查看当前可用的键盘操作。"), keywords: tr("命令面板 终端 侧栏 发送 换行"), icon: <SettingsSourceIcon icon="shortcuts" /> },
    { id: "provider" as const, group: tr("个人"), label: tr("模型与 API"), description: tr("配置提供商、接口、认证与默认模型。"), keywords: tr("API 密钥 Base URL 推理 供应商"), icon: <Cpu /> },
    { id: "plugins" as const, group: tr("集成"), label: tr("插件"), description: tr("管理插件、技能和 MCP。"), keywords: tr("导入 Zip 验证 打包 Hook App MCP 技能 服务"), icon: <SettingsSourceIcon icon="plugins" /> },
    { id: "marketplace" as const, group: tr("集成"), label: tr("插件市场"), description: tr("管理插件市场"), keywords: "marketplace git source upgrade", icon: <SettingsSourceIcon icon="plugins" /> },
    { id: "skills" as const, group: tr("集成"), label: tr("技能"), description: tr("查看任务工作流，并选择用于下一条消息。"), keywords: tr("SKILL.md 工作流 市场 安装"), icon: <SettingsSourceIcon icon="skills" /> },
    { id: "connectors" as const, group: tr("集成"), label: "MCP", description: tr("安装和管理外部工具服务、连接与认证状态。"), keywords: tr("stdio HTTP 服务 工具 市场"), icon: <SettingsSourceIcon icon="mcp" /> },
    { id: "computerUse" as const, group: tr("集成"), label: tr("电脑操作"), description: tr("管理 MiniCode 如何使用电脑上的其他应用。"), keywords: "computer use apps approvals permissions 电脑 应用 授权", icon: <ComputerUseSourceIcon /> },
    { id: "browser" as const, group: tr("集成"), label: tr("浏览器"), description: tr("管理内置浏览器页面、下载与站点权限。"), keywords: tr("控制台 网络 下载 数据 网站"), icon: <SettingsSourceIcon icon="browser" /> },
    { id: "hooks" as const, group: tr("编码"), label: tr("钩子"), description: tr("管理来自配置和已启用插件的生命周期钩子。"), keywords: "hooks lifecycle command", icon: <SettingsSourceIcon icon="hooks" /> },
    { id: "connections" as const, group: tr("编码"), label: tr("连接"), description: tr("连接到远程设备。"), keywords: tr("SSH WSL 远程 设备"), icon: <SettingsSourceIcon icon="connections" /> },
    { id: "scheduler" as const, group: tr("集成"), label: tr("已安排"), description: tr("管理自动运行的任务与最近记录。"), keywords: tr("计划 Cron 时区 Worktree 自动"), icon: <CalendarClock /> },
    { id: "workspaceGit" as const, group: tr("编码"), label: "Git", description: tr("查看当前工作区的变更、分支与工作树。"), keywords: tr("diff 状态 文件 提交"), icon: <SettingsSourceIcon icon="workspaceGit" /> },
    { id: "features" as const, group: tr("编码"), label: tr("实验功能"), description: tr("启用或停用当前运行时支持的实验能力。"), keywords: tr("开关 运行时 SDK 预览"), icon: <FlaskConical /> },
    { id: "advanced" as const, group: tr("编码"), label: tr("环境"), description: tr("本地环境用于配置项目工作树的设置脚本和操作。"), keywords: "environment setup cleanup actions worktree", icon: <SettingsSourceIcon icon="advanced" /> },
    { id: "worktrees" as const, group: tr("编码"), label: tr("工作树"), description: tr("查看工作副本，并选择保留多少个旧工作树。"), keywords: "worktree root upstream cleanup", icon: <SettingsSourceIcon icon="worktrees" /> },
  ];
  const integrationOpen = ["plugins", "skills", "connectors", "marketplace"].includes(activeTab);
  useEffect(() => {
    if (settingsOpen && ((pluginsEnabled === false && ["plugins", "skills", "marketplace"].includes(activeTab)) || (activeTab === "plugins" && pluginCount === 0) || (activeTab === "skills" && skillCount === 0) || (activeTab === "marketplace" && marketplaceCount === 0))) {
      setActiveTab("connectors");
    }
  }, [activeTab, pluginCount, settingsOpen, setActiveTab, skillCount, marketplaceCount, pluginsEnabled]);
  const navigationTab = integrationOpen ? "plugins" : activeTab === "features" ? "config" : activeTab;
  const activeTabMeta = tabs.find((tab) => tab.id === navigationTab)!;
  const documentationHref = activeTab === "config" ? "https://developers.openai.com/codex/config-basic"
    : activeTab === "hooks" ? "https://developers.openai.com/codex/hooks" : null;
  const normalizedSettingsQuery = settingsQuery.trim().toLowerCase();
  const navigationTabs = tabs.filter(tab => !["import", "skills", "connectors", "marketplace", "provider", "features", "scheduler"].includes(tab.id)
    && (tab.id !== "computerUse" || computerUse.data?.navigation_visible));
  const visibleTabs = normalizedSettingsQuery
    ? navigationTabs.filter(tab => `${tab.label} ${tab.group} ${tab.description} ${tab.keywords}`.toLowerCase().includes(normalizedSettingsQuery))
    : navigationTabs;

  const searchableOptions = [...SETTINGS_OPTIONS.map((option) => ({ ...option, sourceTitle: option.title, title: tr(option.title), description: tr(option.description) })),
    ...SHORTCUT_DEFINITIONS.filter(shortcut => appMode === "code" || !isCodeShortcut(shortcut.id)).map((shortcut) => ({ tab: "shortcuts" as const, sourceTitle: shortcut.label, title: tr(shortcut.label), description: tr("快捷键绑定：{binding}", { binding: formatShortcut(shortcutBindings[shortcut.id]) || tr("未绑定") }), keywords: `${shortcut.action} ${formatShortcut(shortcutBindings[shortcut.id])}` })),
  ];
  const matchingOptions = normalizedSettingsQuery ? searchableOptions.filter((option) =>
    (option.tab !== "computerUse" || computerUse.data?.navigation_visible)
      && `${option.title} ${option.description} ${option.keywords}`.toLowerCase().includes(normalizedSettingsQuery)) : [];

  useEffect(() => {
    if (settingsOpen && activeTab === "computerUse" && computerUse.data && !computerUse.data.navigation_visible) setActiveTab("general");
  }, [settingsOpen, activeTab, computerUse.data, setActiveTab]);

  const handleProviderChange = (id: ProviderId) => {
    setProvider(id);
  };
  const handleSettingsPayloadChange = (payload: LLMSettingsPayload) => {
    settingsPayloadRef.current = payload;
    setSettingsPayload(payload);
  };

  return (
    <main
      ref={pageRef}
      className="settings-workspace settings-center"
      style={{ "--settings-sidebar-width": `${visibleLeftSidebarWidth(sidebarWidth, windowWidth)}px` } as import("react").CSSProperties}
      hidden={!settingsOpen}
      aria-label={tr("设置")}
      tabIndex={-1}
    >
      <aside className="settings-workspace-sidebar">
        <div className="settings-navigation-heading">
        <h2>{tr("设置")}</h2>
        </div>
        <label className="settings-search">
          <Search size={15} aria-hidden="true" />
          <input value={settingsQuery} onChange={(event) => { setSettingsQuery(event.target.value); setSearchTarget(null); }} placeholder={tr("搜索设置…")} aria-label={tr("搜索设置")} />
        </label>
        <nav className="settings-center-tabs" aria-label={tr("设置分类")}>
          {matchingOptions.length > 0 && <div className="settings-search-results" aria-label={tr("设置选项结果")}>
            {matchingOptions.map((option) => <button key={`${option.tab}:${option.title}`} type="button"
              className="settings-search-result" onClick={() => {
                if (option.tab === "provider" && option.sourceTitle === "听写模型" && runtime()?.remoteHost) {
                  void openDictationProviderSettings().catch(error => pushToast(error instanceof Error ? error.message : String(error), "error")); return;
                }
                setActiveTab(option.tab); setSearchTarget({ tab: option.tab, title: option.title, sourceTitle: option.sourceTitle });
              }}>
              <strong>{tabs.find((tab) => tab.id === option.tab)?.label} / {option.title}</strong><span>{option.description}</span>
            </button>)}
          </div>}
          {[tr("个人"), tr("集成"), tr("编码")].map((group) => {
            const groupTabs = visibleTabs.filter((tab) => tab.group === group);
            if (groupTabs.length === 0) return null;
            return (
              <div className="settings-nav-group" key={group}>
                <div className="settings-nav-group-label" aria-hidden="true">{group}</div>
                {groupTabs.map((tab) => (
                  <button
                    type="button"
                    className="settings-center-tab"
                    key={tab.id}
                    ref={navigationTab === tab.id ? activeTabButtonRef : undefined}
                    aria-label={tab.label}
                    aria-current={navigationTab === tab.id ? "page" : undefined}
                    onClick={() => { setSearchTarget(null); setActiveTab(tab.id); }}
                  >
                    <span className="settings-center-tab-icon" aria-hidden="true">{tab.icon}</span>
                    <span className="settings-center-tab-label">{tab.label}</span>
                  </button>
                ))}
              </div>
            );
          })}
          {visibleTabs.length === 0 && matchingOptions.length === 0 && <div className="settings-nav-empty">{tr("没有匹配的设置")}</div>}
        </nav>
      </aside>

      <header className="settings-workspace-header" aria-hidden="true" />

      <section className="settings-center-main" ref={contentRef} onScroll={(event) => { if (!(activeTab === "general" && licensesOpen)) pagePositions.current.set(activeTab, event.currentTarget.scrollTop); }} aria-labelledby="settings-page-title">
          <div className="settings-center-content">
            {!(activeTab === "general" && licensesOpen) && <header className="settings-page-heading">
              <div className="settings-page-heading-copy">
              <h2 id="settings-page-title">{activeTabMeta.label}</h2>
              {documentationHref && <p>{activeTabMeta.description}{" "}<a
                className="settings-learn-more" href={documentationHref} target="_blank" rel="noreferrer"
                onClick={event => { event.preventDefault(); void import("../lib/open-preferred-target").then(({ openPreferredWebLink }) => openPreferredWebLink(documentationHref)); }}
              >{tr("了解更多")}</a></p>}
              {integrationOpen && <p>{tr("管理插件、技能和 MCP。")}</p>}
              {activeTab === "computerUse" && <p>{tr("管理 MiniCode 如何使用电脑上的其他应用。")}</p>}
              </div>
              <div ref={setToolbarHost} className="settings-page-heading-actions">
                {integrationOpen && <PluginSettingsToolbar
                  onAddMarketplace={() => { setActiveTab("plugins"); setMarketplaceRequest(request => request + 1); }}
                  onAddMcpServer={() => { setActiveTab("connectors"); setMcpEditorRequest(request => request + 1); }}
                  onBrowseDirectory={() => useAppStore.getState().toggleSkillsMarketplace("settings", activeTab === "skills" ? "skills" : "plugins")}
                />}
              </div>
            </header>}
            {integrationOpen && <div className="settings-integration-controls"><nav className="settings-integration-tabs" aria-label={tr("插件能力")}>
              {([
                { id: "plugins", label: tr("插件"), name: tr("已安装插件"), count: pluginCount },
                { id: "connectors", label: "MCP", name: tr("MCP 服务"), count: mcpCount },
                { id: "skills", label: tr("技能"), name: tr("已安装技能"), count: skillCount },
                { id: "marketplace", label: tr("市场"), name: tr("插件市场"), count: marketplaceCount },
              ] as const).filter(item => item.id === "connectors" || (pluginsEnabled !== false && item.count !== undefined && item.count > 0)).map(item => <button key={item.id} type="button" aria-label={item.name} aria-pressed={activeTab === item.id}
                onClick={() => { setSearchTarget(null); setActiveTab(item.id); }}>{item.label}{item.count !== undefined && <span>{item.count}</span>}</button>)}
            </nav>
            <label className="settings-search settings-integration-search">
              <Search size={16} aria-hidden="true" />
              <input value={integrationSearch} onChange={event => setIntegrationSearch(event.target.value)}
                aria-label={activeTab === "connectors" ? tr("搜索 MCP 服务器") : activeTab === "skills" ? tr("搜索技能") : activeTab === "marketplace" ? tr("搜索插件市场") : tr("搜索插件")}
                placeholder={activeTab === "connectors" ? tr("搜索 MCP 服务器") : activeTab === "skills" ? tr("搜索技能") : activeTab === "marketplace" ? tr("搜索插件市场") : tr("搜索插件")} />
            </label></div>}
            <div className="settings-page-body">
              {Array.from(new Set([...visitedTabs, ...(settingsOpen ? [activeTab, ...(integrationOpen ? ["plugins" as const, "connectors" as const, "skills" as const] : [])] : [])])).filter(tab => tab !== "marketplace").map((tab) => <div
                className="settings-tab-pane" key={tab} data-settings-tab={tab === "plugins" && activeTab === "marketplace" ? "marketplace" : tab} hidden={tab !== activeTab && !(activeTab === "marketplace" && tab === "plugins")}>
              <Suspense fallback={<div className="settings-load-state" role="status" aria-label={tr("正在加载设置")}><span className="settings-load-state-spinner" aria-hidden="true" /></div>}>
              {tab === "appearance" && <AppearanceTab />}
              {tab === "import" && <ImportSettingsTab active={settingsOpen && tab === activeTab} />}
              {tab === "config" && <ConfigTab active={settingsOpen && tab === activeTab} />}
              {tab === "hooks" && <HooksTab active={settingsOpen && tab === activeTab} toolbarHost={toolbarHost} />}
              {tab === "storage" && <StorageTab active={settingsOpen && tab === activeTab} />}
              {tab === "worktrees" && <WorktreesTab active={settingsOpen && tab === activeTab} />}
              {tab === "personalization" && <PersonalizationTab active={settingsOpen && tab === activeTab} />}
              {tab === "shortcuts" && <ShortcutsTab active={settingsOpen && tab === activeTab} searchTarget={searchTarget?.tab === "shortcuts" ? searchTarget.title : undefined} />}
              {tab === "general" && (
                <><div hidden={licensesOpen}><GeneralTab active={settingsOpen && tab === activeTab && !licensesOpen}
                  onOpenLicenses={() => { generalBeforeLicenses.current = contentRef.current?.scrollTop || 0; setLicensesOpen(true); }}
                  importSettings={<ImportSettingsTab variant="general-row" active={settingsOpen && activeTab === "general" && !licensesOpen} />}
                /></div>{licensesOpen && <OpenSourceLicenses onBack={() => setLicensesOpen(false)} />}</>
              )}
              {tab === "provider" && (
                settingsLoadState === "error" && !settingsPayload ? (
                  <div className="settings-load-state" data-tone="danger" role="alert">
                    <span className="settings-load-state-icon" aria-hidden="true"><ServerOff /></span>
                    <div className="settings-load-state-copy">
                      <strong>{tr("模型设置暂时不可用")}</strong>
                      <span>{tr("检查后端连接后重试。")}</span>
                      <code>{settingsLoadError}</code>
                    </div>
                    <button type="button" className="settings-action-button" onClick={loadSettings}>{tr("重试")}</button>
                  </div>
                ) : !settingsPayload ? (
                  <div className="settings-load-state" aria-live="polite">
                    <span className="settings-load-state-spinner" aria-hidden="true" />
                    <div className="settings-load-state-copy"><strong>{tr("正在加载模型设置")}</strong><span>{tr("正在读取提供商与模型配置。")}</span></div>
                  </div>
                ) : <>
                  {settingsLoadState === "error" && <div className="settings-load-state" data-tone="danger" role="alert">
                    <div className="settings-load-state-copy"><strong>{tr("模型设置刷新失败，编辑草稿已保留")}</strong><code>{settingsLoadError}</code></div>
                    <button type="button" className="settings-action-button" onClick={loadSettings}>{tr("重试")}</button>
                  </div>}
                  <ProviderTab
                  selectedProvider={provider}
                  settingsPayload={settingsPayload}
                  settingsPayloadRef={settingsPayloadRef}
                  onProviderChange={handleProviderChange}
                  onSettingsPayloadChange={handleSettingsPayloadChange}
                  searchTarget={searchTarget?.tab === "provider" ? searchTarget.title : undefined}
                  isActive={settingsOpen && tab === activeTab}
                /></>
              )}
              {tab === "skills" && <SkillsTab active={settingsOpen && integrationOpen} search={integrationSearch} onSkillsChange={setSkillCount} />}
              {tab === "voice" && <VoiceSettings active={settingsOpen && tab === activeTab} />}
              {tab === "computerUse" && <ComputerUseSettings model={computerUse} />}
              {tab === "connectors" && <ConnectorsTab search={integrationSearch} editorRequest={mcpEditorRequest}
                onEditorChange={() => { if (contentRef.current && activeTab === "connectors") contentRef.current.scrollTop = 0; }} />}
              {tab === "connections" && <ConnectionsTab active={settingsOpen && tab === activeTab} />}
              {tab === "browser" && <BrowserIntegrationTab active={settingsOpen && tab === activeTab} />}
              {tab === "scheduler" && <SchedulerTab active={settingsOpen && tab === activeTab} title={tr("定时任务")} description={tr("自动运行的任务和最近结果。")} />}
              {tab === "workspaceGit" && <WorkspaceGitTab active={settingsOpen && tab === activeTab} />}
              {tab === "features" && <FeatureFlagsTab active={settingsOpen && tab === activeTab} searchTarget={searchTarget?.tab === "features" ? searchTarget.title : undefined} />}
              {tab === "plugins" && <PluginsTab managedToolbar search={integrationSearch} marketplaceRequest={marketplaceRequest}
                onPluginsChange={setPluginCount} onMarketplacesChange={setMarketplaceCount} managedView={activeTab === "marketplace" ? "marketplaces" : "plugins"}
                loadInventory={settingsOpen && integrationOpen} active={settingsOpen && (tab === activeTab || activeTab === "marketplace")}
                searchTarget={searchTarget?.tab === "plugins" ? searchTarget.title : undefined} />}
              {tab === "advanced" && <SourceEnvironmentsTab active={settingsOpen && tab === activeTab} />}
              {tab === "pets" && <SourcePetsTab active={settingsOpen && tab === activeTab} onCreatePet={createPetChat} />}
              {tab === "archived" && <ArchivedTab toolbarHost={toolbarHost} active={settingsOpen && tab === activeTab} />}
              </Suspense>
              </div>)}
            </div>
          </div>
      </section>
    </main>
  );
});
