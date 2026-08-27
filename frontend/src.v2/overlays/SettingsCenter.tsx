import { useEffect, useRef, useState } from "react";
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
  Palette,
  Puzzle,
  Search,
  ServerOff,
  SlidersHorizontal,
  Sparkles,
  SquareTerminal,
  UserRoundCog,
} from "lucide-react";
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

export const SettingsCenter = () => {
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const remoteImagePolicy = useAppStore((s) => s.remoteImagePolicy);
  const setRemoteImagePolicy = useAppStore((s) => s.setRemoteImagePolicy);
  const pageRef = useFocusTrap(settingsOpen);

  const activeTab = useAppStore((s) => s.settingsTab);
  const setActiveTab = useAppStore((s) => s.setSettingsTab);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const [provider, setProvider] = useState<ProviderId>("custom");
  const [settingsPayload, setSettingsPayload] = useState<LLMSettingsPayload | null>(null);
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);
  const settingsLoadEpochRef = useRef(0);
  const [settingsLoadState, setSettingsLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [settingsLoadError, setSettingsLoadError] = useState("");
  const [settingsQuery, setSettingsQuery] = useState("");
  const activeTabButtonRef = useRef<HTMLButtonElement>(null);

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
    if (settingsOpen && activeTab === "advanced") {
      sendClientCommand({ type: "env.list" }, { silent: true });
    }
    if (settingsOpen && activeTab === "scheduler") {
      sendClientCommand({
        type: "scheduler.list",
        owner_conversation_id: conversationId ?? undefined,
        workspace_root: workingDirectory || undefined,
      }, { silent: true });
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

  if (!settingsOpen) return null;

  const tabs = [
    { id: "general" as const, group: "个人", label: "常规", description: "设置工作方式、消息跟进、过程展示与内容加载。", keywords: "协作 代码 远程 Markdown 图片", icon: <SlidersHorizontal /> },
    { id: "appearance" as const, group: "个人", label: "外观", description: "调整界面主题、字号与显示密度。", keywords: "系统 浅色 深色 紧凑 缩放", icon: <Palette /> },
    { id: "personalization" as const, group: "个人", label: "个性化", description: "管理影响模型行为的指令、任务记忆与来源。", keywords: "INSTRUCTIONS.md AGENTS.md 摘要 偏好 规则", icon: <UserRoundCog /> },
    { id: "shortcuts" as const, group: "个人", label: "快捷键", description: "查看当前可用的键盘操作。", keywords: "命令面板 终端 侧栏 发送 换行", icon: <Keyboard /> },
    { id: "provider" as const, group: "个人", label: "模型", description: "配置提供商、接口、认证与默认模型。", keywords: "API 密钥 Base URL 推理 供应商", icon: <Cpu /> },
    { id: "plugins" as const, group: "集成", label: "插件", description: "安装和管理包含多类能力的插件包。", keywords: "导入 Zip 验证 打包 Hook App", icon: <Puzzle /> },
    { id: "skills" as const, group: "集成", label: "技能", description: "查看任务工作流，并选择用于下一条消息。", keywords: "SKILL.md 工作流 市场 安装", icon: <Sparkles /> },
    { id: "connectors" as const, group: "集成", label: "MCP", description: "安装和管理外部工具服务、连接与认证状态。", keywords: "stdio HTTP 服务 工具 市场", icon: <Cable /> },
    { id: "browser" as const, group: "集成", label: "浏览器", description: "管理内置浏览器页面、下载与站点权限。", keywords: "控制台 网络 下载 数据 网站", icon: <Globe2 /> },
    { id: "scheduler" as const, group: "集成", label: "已安排", description: "管理自动运行的任务与最近记录。", keywords: "计划 Cron 时区 Worktree 自动", icon: <CalendarClock /> },
    { id: "workspaceGit" as const, group: "编码", label: "Git 与工作树", description: "查看当前工作区的变更、分支与工作树。", keywords: "diff 状态 文件 提交", icon: <GitBranch /> },
    { id: "features" as const, group: "编码", label: "实验功能", description: "启用或停用当前运行时支持的实验能力。", keywords: "开关 运行时 SDK 预览", icon: <FlaskConical /> },
    { id: "advanced" as const, group: "编码", label: "环境", description: "检查开发工具、环境变量与运行诊断。", keywords: "Git Python Node.js Docker Ollama 后端 运行状态", icon: <SquareTerminal /> },
    { id: "archived" as const, group: "已归档", label: "已归档任务", description: "存放已归档任务及消息，并可恢复到任务列表。", keywords: "会话 消息 恢复 隐藏", icon: <Archive /> },
  ];
  const activeTabMeta = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const normalizedSettingsQuery = settingsQuery.trim().toLowerCase();
  const visibleTabs = normalizedSettingsQuery
    ? tabs.filter((tab) => `${tab.label} ${tab.group} ${tab.description} ${tab.keywords}`.toLowerCase().includes(normalizedSettingsQuery))
    : tabs;

  const updateSettingsQuery = (value: string) => {
    setSettingsQuery(value);
    const query = value.trim().toLowerCase();
    if (!query) return;
    const matches = tabs.filter((tab) => `${tab.label} ${tab.group} ${tab.description} ${tab.keywords}`.toLowerCase().includes(query));
    if (matches.length > 0 && !matches.some((tab) => tab.id === activeTab)) {
      setActiveTab(matches[0].id);
    }
  };

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
      aria-label="设置"
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
        <button type="button" className="settings-back-button" onClick={toggleSettings} aria-label="返回应用">
          <ArrowLeft size={16} />
          <span>返回应用</span>
        </button>
        <label className="settings-search">
          <Search size={15} aria-hidden="true" />
          <input value={settingsQuery} onChange={(event) => updateSettingsQuery(event.target.value)} placeholder="搜索设置…" aria-label="搜索设置" />
        </label>
        <nav className="settings-center-tabs" aria-label="设置分类">
          {["个人", "集成", "编码", "已归档"].map((group) => {
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
                    ref={activeTab === tab.id ? activeTabButtonRef : undefined}
                    aria-label={tab.label}
                    aria-current={activeTab === tab.id ? "page" : undefined}
                    onClick={() => setActiveTab(tab.id)}
                  >
                    <span className="settings-center-tab-icon" aria-hidden="true">{tab.icon}</span>
                    <span className="settings-center-tab-label">{tab.label}</span>
                  </button>
                ))}
              </div>
            );
          })}
          {visibleTabs.length === 0 && <div className="settings-nav-empty">没有匹配的设置</div>}
        </nav>
      </aside>

      <header className="settings-workspace-header" aria-hidden="true" />

      <section className="settings-center-main" aria-labelledby="settings-page-title">
          <div className="settings-center-content" key={activeTab}>
            <header className="settings-page-heading">
              <h2 id="settings-page-title">{activeTabMeta.label}</h2>
              <p>{activeTabMeta.description}</p>
            </header>
            <div className="settings-page-body">
              {activeTab === "appearance" && <AppearanceTab />}
              {activeTab === "personalization" && <PersonalizationTab />}
              {activeTab === "shortcuts" && <ShortcutsTab />}
              {activeTab === "general" && (
                <GeneralTab
                  remoteImagePolicy={remoteImagePolicy}
                  setRemoteImagePolicy={setRemoteImagePolicy}
                />
              )}
              {activeTab === "provider" && (
                settingsLoadState === "error" ? (
                  <div className="settings-load-state" data-tone="danger" role="alert">
                    <span className="settings-load-state-icon" aria-hidden="true"><ServerOff /></span>
                    <div className="settings-load-state-copy">
                      <strong>模型设置暂时不可用</strong>
                      <span>检查后端连接后重试。</span>
                      <code>{settingsLoadError}</code>
                    </div>
                    <button type="button" className="settings-action-button" onClick={loadSettings}>重试</button>
                  </div>
                ) : settingsLoadState !== "ready" ? (
                  <div className="settings-load-state" aria-live="polite">
                    <span className="settings-load-state-spinner" aria-hidden="true" />
                    <div className="settings-load-state-copy"><strong>正在加载模型设置</strong><span>正在读取提供商与模型配置。</span></div>
                  </div>
                ) : <ProviderTab
                  selectedProvider={provider}
                  settingsPayload={settingsPayload}
                  settingsPayloadRef={settingsPayloadRef}
                  onProviderChange={handleProviderChange}
                  onSettingsPayloadChange={handleSettingsPayloadChange}
                />
              )}
              {activeTab === "skills" && <SkillsTab onReturnToApp={toggleSettings} />}
              {activeTab === "connectors" && <ConnectorsTab />}
              {activeTab === "browser" && <BrowserIntegrationTab />}
              {activeTab === "scheduler" && <SchedulerTab title="定时任务" description="自动运行的任务和最近结果。" />}
              {activeTab === "workspaceGit" && <WorkspaceGitTab />}
              {activeTab === "features" && <FeatureFlagsTab />}
              {activeTab === "plugins" && <PluginsTab />}
              {activeTab === "advanced" && <AdvancedTab />}
              {activeTab === "archived" && <ArchivedTab />}
            </div>
          </div>
      </section>
    </main>
  );
};
