import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Blend,
  Bot,
  CalendarClock,
  FlaskConical,
  Plug,
  Search,
  Settings,
  Wrench,
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
  runtimeCapabilityEffortLevels,
} from "./settingsShared";
import { GeneralTab } from "./GeneralTab";
import { ProviderTab } from "./ProviderTab";
import { ConnectorsTab } from "./ConnectorsTab";
import { SchedulerTab } from "./SchedulerTab";
import { AdvancedTab } from "./AdvancedTab";
import { FeatureFlagsTab } from "./FeatureFlagsTab";
import { PluginsTab } from "./PluginsTab";
import { formatSettingsLoadError } from "./settingsLoad";

export const SettingsCenter = () => {
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const permissionMode = useAppStore((s) => s.permissionMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const currentModel = useAppStore((s) => s.currentModel);
  const providerCapabilities = useAppStore((s) => s.runtimeCapabilities?.provider_capabilities);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const setPermissionMode = useAppStore((s) => s.setPermissionMode);
  const setEffortLevel = useAppStore((s) => s.setEffortLevel);
  const remoteImagePolicy = useAppStore((s) => s.remoteImagePolicy);
  const setRemoteImagePolicy = useAppStore((s) => s.setRemoteImagePolicy);
  const pageRef = useFocusTrap(settingsOpen);

  const [activeTab, setActiveTab] = useState<Tab>("general");
  const [provider, setProvider] = useState<ProviderId>("custom");
  const [settingsPayload, setSettingsPayload] = useState<LLMSettingsPayload | null>(null);
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);
  const settingsLoadEpochRef = useRef(0);
  const [settingsLoadState, setSettingsLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [settingsLoadError, setSettingsLoadError] = useState("");
  const [settingsQuery, setSettingsQuery] = useState("");

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
    if (activeTab !== "general" && activeTab !== "provider" && activeTab !== "advanced") return;
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
    if (settingsOpen && activeTab === "connectors") {
      sendClientCommand({ type: "mcp.list" }, { silent: true });
      sendClientCommand({ type: "connectors.marketplace.list" }, { silent: true });
    }
    if (settingsOpen && activeTab === "provider") {
      sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.provider" }, { silent: true });
    }
    if (settingsOpen && activeTab === "advanced") {
      sendClientCommand({ type: "env.list" }, { silent: true });
    }
    if (settingsOpen && activeTab === "scheduler") {
      sendClientCommand({ type: "scheduler.list" }, { silent: true });
    }
  }, [settingsOpen, activeTab]);

  useEffect(() => {
    const onSettingsTab = (event: Event) => {
      const tab = (event as CustomEvent<Tab>).detail;
      if (tab === "general" || tab === "provider" || tab === "connectors" || tab === "scheduler" || tab === "features" || tab === "plugins" || tab === "advanced") {
        setActiveTab(tab);
      }
    };
    window.addEventListener("minicode:settings-tab", onSettingsTab as EventListener);
    return () => window.removeEventListener("minicode:settings-tab", onSettingsTab as EventListener);
  }, []);

  if (!settingsOpen) return null;

  const tabs = [
    { id: "general" as const, group: "个人", label: "常规", description: "管理权限、内容显示和桌面更新。", icon: <Settings /> },
    { id: "provider" as const, group: "个人", label: "模型", description: "配置模型提供商、接口、凭据和推理强度。", icon: <Bot /> },
    { id: "connectors" as const, group: "集成", label: "连接", description: "将 MiniCode 连接到 MCP 服务和外部工具。", icon: <Plug /> },
    { id: "scheduler" as const, group: "集成", label: "已安排", description: "创建定时任务并查看最近运行结果。", icon: <CalendarClock /> },
    { id: "plugins" as const, group: "集成", label: "插件", description: "管理本地插件和插件开发工具。", icon: <Blend /> },
    { id: "features" as const, group: "编码", label: "实验功能", description: "预览开发中的功能并覆盖功能开关。", icon: <FlaskConical /> },
    { id: "advanced" as const, group: "编码", label: "高级", description: "管理环境变量和运行时诊断。", icon: <Wrench /> },
  ];
  const activeTabMeta = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const normalizedSettingsQuery = settingsQuery.trim().toLowerCase();
  const visibleTabs = normalizedSettingsQuery
    ? tabs.filter((tab) => `${tab.label} ${tab.group} ${tab.description}`.toLowerCase().includes(normalizedSettingsQuery))
    : tabs;
  const switchPermissionMode = (mode: typeof permissionMode) => {
    setPermissionMode(mode);
  };

  const handleProviderChange = (id: ProviderId) => {
    setProvider(id);
  };
  const handleSettingsPayloadChange = (payload: LLMSettingsPayload) => {
    settingsPayloadRef.current = payload;
    setSettingsPayload(payload);
  };

  const runtimeEffortLevels = runtimeCapabilityEffortLevels(providerCapabilities?.reasoning_effort_levels);
  const runtimeSupportsReasoningEffort = providerCapabilities?.reasoning_effort === true && runtimeEffortLevels.length > 0;
  const showReasoningEffort = runtimeSupportsReasoningEffort;

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
        <button type="button" className="settings-back-button" onClick={toggleSettings} title="返回应用" aria-label="返回应用">
          <ArrowLeft size={17} />
          <span>返回应用</span>
        </button>
        <label className="settings-search">
          <Search size={15} aria-hidden="true" />
          <input value={settingsQuery} onChange={(event) => setSettingsQuery(event.target.value)} placeholder="搜索设置…" aria-label="搜索设置" />
        </label>
        <nav className="settings-center-tabs" aria-label="设置分类">
          {["个人", "集成", "编码"].map((group) => {
            const groupTabs = visibleTabs.filter((tab) => tab.group === group);
            if (groupTabs.length === 0) return null;
            return (
              <div className="settings-nav-group" key={group}>
                <div className="settings-nav-group-label">{group}</div>
                {groupTabs.map((tab) => (
                  <button className="settings-center-tab" key={tab.id} aria-label={tab.label} title={tab.description} aria-current={activeTab === tab.id ? "page" : undefined} onClick={() => setActiveTab(tab.id)}>
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
          <div className="settings-center-content">
            <header className="settings-page-heading">
              <h2 id="settings-page-title">{activeTabMeta.label}</h2>
            </header>
            <div className="settings-page-body" key={activeTab}>
          {activeTab === "general" && (
            <GeneralTab
              permissionMode={permissionMode}
              effortLevel={effortLevel}
              currentModel={currentModel}
              showReasoningEffort={showReasoningEffort}
              effortOptions={runtimeEffortLevels}
              switchPermissionMode={switchPermissionMode}
              setEffortLevel={setEffortLevel}
              remoteImagePolicy={remoteImagePolicy}
              setRemoteImagePolicy={setRemoteImagePolicy}
            />
          )}
          {activeTab === "provider" && (
            settingsLoadState === "error" ? (
              <div role="alert" style={{ padding: 24, color: "var(--state-danger)" }}>
                <div>{settingsLoadError}</div>
                <button type="button" className="btn" onClick={loadSettings} style={{ marginTop: 12 }}>重试</button>
              </div>
            ) : settingsLoadState !== "ready" ? (
              <div style={{ padding: 24, color: "var(--text-muted)" }}>正在加载模型设置…</div>
            ) : <ProviderTab
              selectedProvider={provider}
              settingsPayload={settingsPayload}
              settingsPayloadRef={settingsPayloadRef}
              onProviderChange={handleProviderChange}
              onSettingsPayloadChange={handleSettingsPayloadChange}
            />
          )}
          {activeTab === "connectors" && <ConnectorsTab />}
          {activeTab === "scheduler" && <SchedulerTab title="已安排" />}
          {activeTab === "features" && <FeatureFlagsTab />}
          {activeTab === "plugins" && <PluginsTab />}
          {activeTab === "advanced" && <AdvancedTab />}
            </div>
          </div>
      </section>
    </main>
  );
};
