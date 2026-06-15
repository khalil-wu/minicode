import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { useAppStore } from "../stores";
import { fetchLLMSettings } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { isDesktop } from "../desktop/runtime";
import {
  type Tab,
  type ProviderId,
  type LLMSettingsPayload,
  toUiProvider,
  backendProvider,
  sectionForUiProvider,
  defaultSectionForProvider,
  buildModelChoices,
  Section,
  supportsReasoningEffort,
  backdropStyle,
  modalStyle,
  headerStyle,
  settingsBodyStyle,
  tabsStyle,
  contentStyle,
  contentHeaderStyle,
  closeBtn,
  tabButtonStyle,
  choiceStyle,
} from "./settingsShared";
import { GeneralTab } from "./GeneralTab";
import { ProviderTab } from "./ProviderTab";
import { ConnectorsTab } from "./ConnectorsTab";
import { SchedulerTab } from "./SchedulerTab";
import { AdvancedTab } from "./AdvancedTab";

export const SettingsCenter = () => {
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const permissionMode = useAppStore((s) => s.permissionMode);
  const effortLevel = useAppStore((s) => s.effortLevel);
  const currentModel = useAppStore((s) => s.currentModel);
  const availableModels = useAppStore((s) => s.availableModels);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const setPermissionMode = useAppStore((s) => s.setPermissionMode);
  const setEffortLevel = useAppStore((s) => s.setEffortLevel);

  const [activeTab, setActiveTab] = useState<Tab>("general");
  const [provider, setProvider] = useState<ProviderId>("deepseek");
  const [settingsPayload, setSettingsPayload] = useState<LLMSettingsPayload | null>(null);
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);

  useEffect(() => {
    if (!settingsOpen) return;
    fetchLLMSettings()
      .then((payload) => {
        const p = toUiProvider(payload);
        const data = payload as LLMSettingsPayload;
        settingsPayloadRef.current = data;
        setSettingsPayload(data);
        setProvider(p);
      })
      .catch(() => undefined);
  }, [settingsOpen]);

  useEffect(() => {
    if (settingsOpen && activeTab === "connectors") {
      sendClientCommand({ type: "mcp.list" });
      sendClientCommand({ type: "connectors.marketplace.list" });
    }
    if (settingsOpen && activeTab === "advanced") {
      sendClientCommand({ type: "env.list" });
    }
    if (settingsOpen && activeTab === "scheduler") {
      sendClientCommand({ type: "scheduler.list" });
    }
  }, [settingsOpen, activeTab]);

  useEffect(() => {
    if (!settingsOpen) return;
    const onSettingsTab = (event: Event) => {
      const tab = (event as CustomEvent<Tab>).detail;
      if (tab === "general" || tab === "provider" || tab === "connectors" || tab === "scheduler" || tab === "advanced" || tab === "diagnostics") {
        setActiveTab(tab);
      }
    };
    window.addEventListener("minicode:settings-tab", onSettingsTab as EventListener);
    return () => window.removeEventListener("minicode:settings-tab", onSettingsTab as EventListener);
  }, [settingsOpen]);

  if (!settingsOpen) return null;

  const tabs = [
    { id: "general" as const, label: "General", desc: "Approvals and status" },
    { id: "provider" as const, label: "Models", desc: "Provider, key, endpoint" },
    { id: "connectors" as const, label: "Connectors", desc: "MCP and marketplace" },
    { id: "scheduler" as const, label: "Automations", desc: "Recurring agent runs" },
    { id: "advanced" as const, label: "Advanced", desc: "Runtime and environment" },
    ...(isDesktop() ? [{ id: "diagnostics" as const, label: "Diagnostics", desc: "Desktop health checks" }] : []),
  ];
  const activeTabMeta = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];

  const switchPermissionMode = async (mode: typeof permissionMode) => {
    if (mode === "bypass" && permissionMode !== "bypass") {
      const { showConfirm } = await import("./DialogService");
      const ok = await showConfirm({
        title: "Enable Full access",
        message: "Full access will run edits and commands without approval prompts. Continue?",
        confirmLabel: "Enable",
        danger: true,
      });
      if (!ok) return;
    }
    setPermissionMode(mode);
  };

  const handleProviderChange = (id: ProviderId) => {
    setProvider(id);
  };
  const handleSettingsPayloadChange = (payload: LLMSettingsPayload) => {
    settingsPayloadRef.current = payload;
    setSettingsPayload(payload);
  };

  const activeProviderSection = sectionForUiProvider(settingsPayload, provider);
  const showReasoningEffort = supportsReasoningEffort(provider, activeProviderSection);

  return (
    <div className="overlay-backdrop" onClick={toggleSettings} style={backdropStyle}>
      <div className="modal-content" role="dialog" aria-modal="true" aria-label="Settings" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => { if (e.key === "Escape") toggleSettings(); }} style={modalStyle}>
        <div style={headerStyle}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18, color: "var(--text-primary)", fontWeight: 700 }}>Settings</h2>
            <div style={{ marginTop: 2, color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>Configure MiniCode without leaving the workspace.</div>
          </div>
          <button onClick={toggleSettings} style={closeBtn} aria-label="Close settings"><X size={16} /></button>
        </div>

        <div style={settingsBodyStyle}>
        <nav style={tabsStyle} aria-label="Settings sections">
          {tabs.map((tab) => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)} style={tabButtonStyle(activeTab === tab.id)}>
              <span style={{ fontWeight: 650 }}>{tab.label}</span>
              <span style={{ color: "var(--text-muted)", fontSize: 11, lineHeight: 1.25 }}>{tab.desc}</span>
            </button>
          ))}
        </nav>

        <div style={contentStyle}>
          <div style={contentHeaderStyle}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)" }}>{activeTabMeta.label}</div>
            <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{activeTabMeta.desc}</div>
          </div>
          {activeTab === "general" && (
            <GeneralTab
              permissionMode={permissionMode}
              effortLevel={effortLevel}
              currentModel={currentModel}
              showReasoningEffort={showReasoningEffort}
              switchPermissionMode={switchPermissionMode}
              setEffortLevel={setEffortLevel}
            />
          )}
          {activeTab === "provider" && (
            <ProviderTab
              settingsPayloadRef={settingsPayloadRef}
              onProviderChange={handleProviderChange}
              onSettingsPayloadChange={handleSettingsPayloadChange}
            />
          )}
          {activeTab === "connectors" && <ConnectorsTab />}
          {activeTab === "scheduler" && <SchedulerTab />}
          {activeTab === "advanced" && (
            <AdvancedTab
              currentModel={currentModel}
              provider={provider}
              availableModels={availableModels}
            />
          )}
        </div>
        </div>
      </div>
    </div>
  );
};
