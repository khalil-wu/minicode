import { useEffect, useRef, useState } from "react";
import {
  Blocks,
  CalendarClock,
  FlaskConical,
  Plug,
  SlidersHorizontal,
  Wrench,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { fetchLLMSettings } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useFocusTrap } from "../hooks/useFocusTrap";
import {
  type Tab,
  type ProviderId,
  type LLMSettingsPayload,
  toUiProvider,
  backendProvider,
  runtimeCapabilityEffortLevels,
  backdropStyle,
  modalStyle,
  headerStyle,
  settingsBodyStyle,
  tabsStyle,
  contentStyle,
  closeBtn,
  tabButtonStyle,
} from "./settingsShared";
import { GeneralTab } from "./GeneralTab";
import { ProviderTab } from "./ProviderTab";
import { ConnectorsTab } from "./ConnectorsTab";
import { SchedulerTab } from "./SchedulerTab";
import { AdvancedTab } from "./AdvancedTab";
import { FeatureFlagsTab } from "./FeatureFlagsTab";
import { PluginsTab } from "./PluginsTab";
import { formatSettingsLoadError } from "./settingsLoad";
import { ModelProviderIcon } from "../components/ModelProviderIcon";

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
  const dialogRef = useFocusTrap(settingsOpen);

  const [activeTab, setActiveTab] = useState<Tab>("general");
  const [provider, setProvider] = useState<ProviderId>("deepseek");
  const [settingsPayload, setSettingsPayload] = useState<LLMSettingsPayload | null>(null);
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);
  const settingsLoadEpochRef = useRef(0);
  const [settingsLoadState, setSettingsLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [settingsLoadError, setSettingsLoadError] = useState("");

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
    { id: "general" as const, label: "General", icon: <SlidersHorizontal /> },
    { id: "provider" as const, label: "Models", icon: <ModelProviderIcon model={currentModel} size={17} /> },
    { id: "connectors" as const, label: "Connectors", icon: <Plug /> },
    { id: "scheduler" as const, label: "Automations", icon: <CalendarClock /> },
    { id: "features" as const, label: "Feature Flags", icon: <FlaskConical /> },
    { id: "plugins" as const, label: "Plugins", icon: <Blocks /> },
    { id: "advanced" as const, label: "Advanced", icon: <Wrench /> },
  ];
  const activeTabLabel = tabs.find((tab) => tab.id === activeTab)?.label ?? "Settings";
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
    <div className="overlay-backdrop" onClick={toggleSettings} style={backdropStyle}>
      <div ref={dialogRef} className="modal-content settings-center" role="dialog" aria-modal="true" aria-label="Settings" tabIndex={-1} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          toggleSettings();
        }
      }} style={modalStyle}>
        <div style={headerStyle}>
          <h2 style={{ margin: 0, fontSize: 16, color: "var(--text-primary)", fontWeight: 700 }}>{activeTabLabel}</h2>
          <button type="button" className="mc-icon-button settings-center-close" onClick={toggleSettings} style={closeBtn} title="Close settings" aria-label="Close settings"><X size={16} /></button>
        </div>

        <div className="settings-center-body" style={settingsBodyStyle}>
        <nav className="settings-center-tabs" style={tabsStyle} aria-label="Settings sections">
          {tabs.map((tab) => (
            <button className="settings-center-tab" key={tab.id} aria-label={tab.label} title={tab.label} aria-current={activeTab === tab.id ? "page" : undefined} onClick={() => setActiveTab(tab.id)} style={tabButtonStyle(activeTab === tab.id)}>
              <span className="settings-center-tab-icon" aria-hidden="true">{tab.icon}</span>
              <span className="settings-center-tab-label">{tab.label}</span>
            </button>
          ))}
        </nav>

        <div className="settings-center-content" style={contentStyle}>
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
                <button type="button" className="mc-button" onClick={loadSettings} style={{ marginTop: 12 }}>Retry</button>
              </div>
            ) : settingsLoadState !== "ready" ? (
              <div style={{ padding: 24, color: "var(--text-muted)" }}>Loading model settings…</div>
            ) : <ProviderTab
              selectedProvider={provider}
              settingsPayload={settingsPayload}
              settingsPayloadRef={settingsPayloadRef}
              onProviderChange={handleProviderChange}
              onSettingsPayloadChange={handleSettingsPayloadChange}
            />
          )}
          {activeTab === "connectors" && <ConnectorsTab />}
          {activeTab === "scheduler" && <SchedulerTab title="Schedules" />}
          {activeTab === "features" && <FeatureFlagsTab />}
          {activeTab === "plugins" && <PluginsTab />}
          {activeTab === "advanced" && <AdvancedTab />}
        </div>
        </div>
      </div>
    </div>
  );
};
