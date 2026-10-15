import { useI18n } from "../i18n";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Archive,
  Cable,
  CalendarClock,
  Cpu,
  FlaskConical,
  GitBranch,
  Globe2,
  Keyboard,
  Mic,
  Settings,
  Puzzle,
  Search,
  ServerOff,
  Sun,
  BookOpenText,
  SquareTerminal,
  UserRoundCog,
} from "../lib/icons";
import { useAppStore } from "../stores";
import { fetchLLMSettings } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useFocusTrap } from "../hooks/useFocusTrap";
import "./SettingsCenter.css";
import {
  type Tab,
  type ProviderId,
  type LLMSettingsPayload,
  toUiProvider,
} from "./settingsShared";
import { GeneralTab } from "./GeneralTab";
import { VoiceSettings } from "./VoiceSettings";
import { AppearanceTab } from "./AppearanceTab";
import { PersonalizationTab } from "./PersonalizationTab";
import { BrowserIntegrationTab } from "./BrowserIntegrationTab";
import { ArchivedTab } from "./ArchivedTab";
import { WorkspaceGitTab } from "./WorkspaceGitTab";
import { ShortcutsTab } from "./ShortcutsTab";
import { ProviderTab } from "./ProviderTab";
import { ConnectorsTab } from "./ConnectorsTab";
import { SchedulerTab } from "./SchedulerTab";
import { AdvancedTab } from "./AdvancedTab";
import { FeatureFlagsTab } from "./FeatureFlagsTab";
import { PluginsTab } from "./PluginsTab";
import { SkillsTab } from "./SkillsTab";
import { formatSettingsLoadError } from "./settingsLoad";
import { SETTINGS_OPTIONS } from "./settingsSearch";
import { formatShortcut, SHORTCUT_DEFINITIONS } from "../lib/keyboard-shortcuts";
import { FEATURE_FLAG_LABELS } from "./FeatureFlagsTab";

export const SettingsCenter = () => {
  const { t: tr } = useI18n();
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const remoteImagePolicy = useAppStore((s) => s.remoteImagePolicy);
  const setRemoteImagePolicy = useAppStore((s) => s.setRemoteImagePolicy);
  const pageRef = useFocusTrap(settingsOpen);

  const activeTab = useAppStore((s) => s.settingsTab);
  const setActiveTab = useAppStore((s) => s.setSettingsTab);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const mcpCount = useAppStore((s) => s.mcpServers.length);
  const skillCount = useAppStore((s) => s.availableSkills.length);
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
  const pagePositions = useRef(new Map<Tab, number>());
  useLayoutEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = pagePositions.current.get(activeTab) || 0;
  }, [activeTab]);
  const [visitedTabs, setVisitedTabs] = useState<Tab[]>([activeTab]);
  const [searchTarget, setSearchTarget] = useState<{ tab: Tab; title: string; sourceTitle: string } | null>(null);
  useEffect(() => {
    if (settingsOpen) setVisitedTabs((tabs) => tabs.includes(activeTab) ? tabs : [...tabs, activeTab]);
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
    { id: "general" as const, group: tr("个人"), label: tr("常规"), description: tr("设置工作方式、消息跟进、过程展示与内容加载。"), keywords: tr("协作 代码 远程 Markdown 图片"), icon: <Settings /> },
    { id: "appearance" as const, group: tr("个人"), label: tr("外观"), description: tr("调整界面主题、字号与显示密度。"), keywords: tr("系统 浅色 深色 紧凑 字号"), icon: <Sun /> },
    { id: "voice" as const, group: tr("个人"), label: tr("语音"), description: tr("设置麦克风与听写服务，录音转成可编辑草稿。"), keywords: tr("听写 录音 转录 麦克风 词汇"), icon: <Mic /> },
    { id: "personalization" as const, group: tr("个人"), label: tr("个性化"), description: tr("管理影响模型行为的指令、任务记忆与来源。"), keywords: tr("INSTRUCTIONS.md AGENTS.md 摘要 偏好 规则"), icon: <UserRoundCog /> },
    { id: "shortcuts" as const, group: tr("个人"), label: tr("快捷键"), description: tr("查看当前可用的键盘操作。"), keywords: tr("命令面板 终端 侧栏 发送 换行"), icon: <Keyboard /> },
    { id: "provider" as const, group: tr("个人"), label: tr("模型"), description: tr("配置提供商、接口、认证与默认模型。"), keywords: tr("API 密钥 Base URL 推理 供应商"), icon: <Cpu /> },
    { id: "plugins" as const, group: tr("集成"), label: tr("插件"), description: tr("管理插件、技能和 MCP。"), keywords: tr("导入 Zip 验证 打包 Hook App MCP 技能 服务"), icon: <Puzzle /> },
    { id: "skills" as const, group: tr("集成"), label: tr("技能"), description: tr("查看任务工作流，并选择用于下一条消息。"), keywords: tr("SKILL.md 工作流 市场 安装"), icon: <BookOpenText /> },
    { id: "connectors" as const, group: tr("集成"), label: "MCP", description: tr("安装和管理外部工具服务、连接与认证状态。"), keywords: tr("stdio HTTP 服务 工具 市场"), icon: <Cable /> },
    { id: "browser" as const, group: tr("集成"), label: tr("浏览器"), description: tr("管理内置浏览器页面、下载与站点权限。"), keywords: tr("控制台 网络 下载 数据 网站"), icon: <Globe2 /> },
    { id: "scheduler" as const, group: tr("集成"), label: tr("已安排"), description: tr("管理自动运行的任务与最近记录。"), keywords: tr("计划 Cron 时区 Worktree 自动"), icon: <CalendarClock /> },
    { id: "workspaceGit" as const, group: tr("编码"), label: tr("Git 与工作树"), description: tr("查看当前工作区的变更、分支与工作树。"), keywords: tr("diff 状态 文件 提交"), icon: <GitBranch /> },
    { id: "features" as const, group: tr("编码"), label: tr("实验功能"), description: tr("启用或停用当前运行时支持的实验能力。"), keywords: tr("开关 运行时 SDK 预览"), icon: <FlaskConical /> },
    { id: "advanced" as const, group: tr("编码"), label: tr("环境"), description: tr("检查开发工具、环境变量与运行诊断。"), keywords: tr("Git Python Node.js Docker Ollama 后端 运行状态"), icon: <SquareTerminal /> },
    { id: "archived" as const, group: tr("已归档"), label: tr("已归档任务"), description: tr("存放已归档任务及消息，并可恢复到任务列表。"), keywords: tr("会话 消息 恢复 隐藏"), icon: <Archive /> },
  ];
  const integrationOpen = ["plugins", "skills", "connectors"].includes(activeTab);
  const navigationTab = integrationOpen ? "plugins" : activeTab;
  const activeTabMeta = tabs.find((tab) => tab.id === navigationTab)!;
  const normalizedSettingsQuery = settingsQuery.trim().toLowerCase();
  const visibleTabs = normalizedSettingsQuery
    ? tabs.filter((tab) => !["skills", "connectors"].includes(tab.id) && `${tab.label} ${tab.group} ${tab.description} ${tab.keywords}`.toLowerCase().includes(normalizedSettingsQuery))
    : tabs.filter((tab) => !["skills", "connectors"].includes(tab.id));

  const searchableOptions = [...SETTINGS_OPTIONS.map((option) => ({ ...option, sourceTitle: option.title, title: tr(option.title), description: tr(option.description) })),
    ...SHORTCUT_DEFINITIONS.map((shortcut) => ({ tab: "shortcuts" as const, sourceTitle: shortcut.label, title: tr(shortcut.label), description: tr("快捷键绑定：{binding}", { binding: formatShortcut(shortcutBindings[shortcut.id]) || tr("未绑定") }), keywords: `${shortcut.action} ${formatShortcut(shortcutBindings[shortcut.id])}` })),
    ...Object.entries(FEATURE_FLAG_LABELS).map(([key, flag]) => ({ tab: "features" as const, sourceTitle: flag.title, title: tr(flag.title), description: tr(flag.description), keywords: key })),
  ];
  const matchingOptions = normalizedSettingsQuery ? searchableOptions.filter((option) =>
    `${option.title} ${option.description} ${option.keywords}`.toLowerCase().includes(normalizedSettingsQuery)) : [];

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
      hidden={!settingsOpen}
      aria-label={tr("设置")}
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          toggleSettings();
        }
      }}
    >
      <aside className="settings-workspace-sidebar">
        <div className="settings-navigation-heading">
        <button type="button" className="settings-back-button" onClick={toggleSettings} aria-label={tr("返回应用")}>
          <ArrowLeft size={16} />
        </button>
        <h2>{tr("设置")}</h2>
        </div>
        <label className="settings-search">
          <Search size={15} aria-hidden="true" />
          <input value={settingsQuery} onChange={(event) => { setSettingsQuery(event.target.value); setSearchTarget(null); }} placeholder={tr("搜索设置…")} aria-label={tr("搜索设置")} />
        </label>
        <nav className="settings-center-tabs" aria-label={tr("设置分类")}>
          {matchingOptions.length > 0 && <div className="settings-search-results" aria-label={tr("设置选项结果")}>
            {matchingOptions.map((option) => <button key={`${option.tab}:${option.title}`} type="button"
              className="settings-search-result" onClick={() => { setActiveTab(option.tab); setSearchTarget({ tab: option.tab, title: option.title, sourceTitle: option.sourceTitle }); }}>
              <strong>{tabs.find((tab) => tab.id === option.tab)?.label} / {option.title}</strong><span>{option.description}</span>
            </button>)}
          </div>}
          {[tr("个人"), tr("集成"), tr("编码"), tr("已归档")].map((group) => {
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

      <section className="settings-center-main" ref={contentRef} onScroll={(event) => pagePositions.current.set(activeTab, event.currentTarget.scrollTop)} aria-labelledby="settings-page-title">
          <div className="settings-center-content">
            <header className="settings-page-heading">
              <div className="settings-page-heading-copy">
              <h2 id="settings-page-title">{activeTabMeta.label}</h2>
              <p>{activeTabMeta.description}</p>
              </div>
              <div ref={setToolbarHost} className="settings-page-heading-actions" />
            </header>
            {integrationOpen && <nav className="settings-integration-tabs" aria-label={tr("插件能力")}>
              {([
                { id: "plugins", label: tr("插件"), name: tr("已安装插件"), count: undefined },
                { id: "skills", label: tr("技能"), name: tr("技能管理"), count: skillCount },
                { id: "connectors", label: "MCP", name: tr("MCP 服务"), count: mcpCount },
              ] as const).map(item => <button key={item.id} type="button" aria-label={item.name} aria-pressed={activeTab === item.id}
                onClick={() => { setSearchTarget(null); setActiveTab(item.id); }}>{item.label}{item.count !== undefined && <span>{item.count}</span>}</button>)}
            </nav>}
            <div className="settings-page-body">
              {Array.from(new Set([...visitedTabs, ...(settingsOpen ? [activeTab] : [])])).map((tab) => <div
                className="settings-tab-pane" key={tab} data-settings-tab={tab} hidden={tab !== activeTab}>
              {tab === "appearance" && <AppearanceTab />}
              {tab === "personalization" && <PersonalizationTab active={settingsOpen && tab === activeTab} />}
              {tab === "shortcuts" && <ShortcutsTab active={settingsOpen && tab === activeTab} searchTarget={searchTarget?.tab === "shortcuts" ? searchTarget.title : undefined} />}
              {tab === "general" && (
                <GeneralTab
                  remoteImagePolicy={remoteImagePolicy}
                  setRemoteImagePolicy={setRemoteImagePolicy}
                />
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
              {tab === "skills" && <SkillsTab onReturnToApp={toggleSettings} />}
              {tab === "voice" && <VoiceSettings active={settingsOpen && tab === activeTab} />}
              {tab === "connectors" && <ConnectorsTab />}
              {tab === "browser" && <BrowserIntegrationTab active={settingsOpen && tab === activeTab} />}
              {tab === "scheduler" && <SchedulerTab active={settingsOpen && tab === activeTab} title={tr("定时任务")} description={tr("自动运行的任务和最近结果。")} />}
              {tab === "workspaceGit" && <WorkspaceGitTab active={settingsOpen && tab === activeTab} />}
              {tab === "features" && <FeatureFlagsTab active={settingsOpen && tab === activeTab} searchTarget={searchTarget?.tab === "features" ? searchTarget.title : undefined} />}
              {tab === "plugins" && <PluginsTab toolbarHost={toolbarHost} active={settingsOpen && tab === activeTab} searchTarget={searchTarget?.tab === "plugins" ? searchTarget.title : undefined} />}
              {tab === "advanced" && <AdvancedTab active={settingsOpen && tab === activeTab} />}
              {tab === "archived" && <ArchivedTab />}
              </div>)}
            </div>
          </div>
      </section>
    </main>
  );
};
