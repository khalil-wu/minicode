import { useEffect, useRef, useState } from "react";
import { RefreshCw, Trash2, X } from "lucide-react";
import { useAppStore } from "../stores";
import type { EffortLevel } from "../stores/types";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders, fetchLLMSettings } from "../protocol/api";
import { isDesktop, desktop, runtime, exportDiagnostics, revealPath } from "../desktop/runtime";
import { sendClientCommand } from "../protocol/ws-outbox";

const PROVIDERS = [
  { id: "anthropic", label: "Anthropic", placeholder: "sk-ant-...", hasBaseUrl: true, defaultUrl: "", defaultModel: "claude-sonnet-4-6" },
  { id: "openai", label: "OpenAI", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.openai.com/v1", defaultModel: "gpt-4o" },
  { id: "deepseek", label: "DeepSeek", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.deepseek.com/v1", defaultModel: "deepseek-v4-pro" },
  { id: "openrouter", label: "OpenRouter", placeholder: "sk-or-...", hasBaseUrl: true, defaultUrl: "https://openrouter.ai/api/v1", defaultModel: "anthropic/claude-sonnet-4" },
  { id: "custom", label: "Custom", placeholder: "API key", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
] as const;

type ProviderId = (typeof PROVIDERS)[number]["id"];
type Tab = "general" | "provider" | "connectors" | "scheduler" | "advanced" | "diagnostics";
type CustomWireApi = "chat" | "responses" | "anthropic";
type BackendProvider = "openai" | "anthropic" | "custom";
type ProviderSection = {
  has_api_key?: boolean;
  base_url?: string;
  model?: string;
  available_models?: string[];
  wire_api?: string;
  thinking_budget?: number;
};
type LLMSettingsPayload = {
  provider?: string;
  openai?: ProviderSection;
  anthropic?: ProviderSection;
  custom?: ProviderSection;
};

const toUiProvider = (payload: unknown): ProviderId => {
  const value = payload as { provider?: string; custom?: { base_url?: string } };
  if (value.provider === "anthropic") return "anthropic";
  if (value.provider === "custom") {
    const host = value.custom?.base_url ?? "";
    if (host.includes("deepseek.com")) return "deepseek";
    if (host.includes("openrouter.ai")) return "openrouter";
    return "custom";
  }
  return "openai";
};

const backendProvider = (provider: ProviderId): "openai" | "anthropic" | "custom" =>
  provider === "anthropic" ? "anthropic" : provider === "openai" ? "openai" : "custom";

const defaultSectionForProvider = (provider: ProviderId): ProviderSection => {
  const cfg = PROVIDERS.find((item) => item.id === provider)!;
  return {
    base_url: cfg.defaultUrl,
    model: cfg.defaultModel,
    available_models: cfg.defaultModel ? [cfg.defaultModel] : [],
    wire_api: backendProvider(provider) === "custom" ? "chat" : undefined,
    thinking_budget: 0,
  };
};

const sectionForUiProvider = (payload: LLMSettingsPayload | null, provider: ProviderId): ProviderSection | undefined => {
  if (!payload) return undefined;
  const bp = backendProvider(provider);
  if (bp !== "custom") return payload[bp];
  const section = payload.custom;
  const host = section?.base_url ?? "";
  if (provider === "deepseek") return host.includes("deepseek.com") ? section : undefined;
  if (provider === "openrouter") return host.includes("openrouter.ai") ? section : undefined;
  return section;
};

const buildModelChoices = (models: string[], current: string): string[] => {
  const merged = [current, ...models]
    .map((model) => model.trim())
    .filter(Boolean);
  return Array.from(new Set(merged));
};

const formatProviderError = (error: unknown): string => {
  const raw = error instanceof Error ? error.message : String(error);
  const text = raw.replace(/^Error:\s*/i, "").trim();
  if (/your request was blocked/i.test(text)) {
    return "gateway blocked the request. Check the gateway allowlist, Base URL, API format, and selected model.";
  }
  return text || "check key, URL, API format, and model";
};

const savedKeyPreview = (placeholder: string): string =>
  placeholder.startsWith("sk-") ? "sk-********************************" : "********************************";

const EFFORT_LEVELS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "low", label: "Low", desc: "Fast, minimal reasoning" },
  { id: "medium", label: "Medium", desc: "Balanced speed and depth" },
  { id: "high", label: "High", desc: "Default depth for coding work" },
  { id: "max", label: "Max", desc: "Slowest, deepest reasoning" },
];

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
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com/v1");
  const [modelName, setModelName] = useState("deepseek-chat");
  const [customWireApi, setCustomWireApi] = useState<CustomWireApi>("chat");
  const [thinkingBudget, setThinkingBudget] = useState(0);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [diagResult, setDiagResult] = useState<Record<string, unknown> | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);
  const [savedKeyByProvider, setSavedKeyByProvider] = useState<Record<BackendProvider, boolean>>({
    openai: false,
    anthropic: false,
    custom: false,
  });
  const settingsPayloadRef = useRef<LLMSettingsPayload | null>(null);

  useEffect(() => {
    if (!settingsOpen) return;
    setConnectionStatus("idle");
    setSaving(false);
    setApiKey("");
    setShowKey(false);
    fetchLLMSettings()
      .then((payload) => {
        const p = toUiProvider(payload);
        const data = payload as LLMSettingsPayload;
        settingsPayloadRef.current = data;
        setSavedKeyByProvider({
          openai: Boolean(data.openai?.has_api_key),
          anthropic: Boolean(data.anthropic?.has_api_key),
          custom: Boolean(data.custom?.has_api_key),
        });
        setProvider(p);
        applyProviderSection(p, sectionForUiProvider(data, p));
        setApiKey("");
      })
      .catch(() => undefined);
  }, [settingsOpen]);

  const applyProviderSection = (nextProvider: ProviderId, section?: ProviderSection) => {
    const fallback = defaultSectionForProvider(nextProvider);
    setBaseUrl(section?.base_url ?? fallback.base_url ?? "");
    setModelName(section?.model ?? fallback.model ?? "");
    const models = buildModelChoices(section?.available_models ?? fallback.available_models ?? [], section?.model ?? fallback.model ?? "");
    useAppStore.getState().setAvailableModels(models);
    if (section?.model || fallback.model) useAppStore.getState().setCurrentModel(section?.model ?? fallback.model ?? "");
    if (backendProvider(nextProvider) === "custom") {
      const wire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
      setCustomWireApi(wire);
    } else {
      setCustomWireApi("chat");
    }
    setThinkingBudget(Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0);
  };

  useEffect(() => {
    if (settingsOpen && activeTab === "connectors") {
      sendClientCommand({ type: "mcp.list" });
      sendClientCommand({ type: "connectors.marketplace.list" } as never);
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

  const providerConfig = PROVIDERS.find((p) => p.id === provider)!;
  const modelChoices = buildModelChoices(availableModels, modelName);
  const tabs = [
    { id: "general" as const, label: "General", desc: "Approvals and status" },
    { id: "provider" as const, label: "Models", desc: "Provider, key, endpoint" },
    { id: "connectors" as const, label: "Connectors", desc: "MCP and marketplace" },
    { id: "scheduler" as const, label: "Scheduled", desc: "Recurring tasks" },
    { id: "advanced" as const, label: "Advanced", desc: "Runtime and environment" },
    ...(isDesktop() ? [{ id: "diagnostics" as const, label: "Diagnostics", desc: "Desktop health checks" }] : []),
  ];
  const activeTabMeta = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];

  const switchPermissionMode = async (mode: typeof permissionMode) => {
    if (mode === "bypass" && permissionMode !== "bypass") {
      const { showConfirm } = await import("./DialogService");
      const ok = await showConfirm({
        title: "Enable Bypass mode",
        message: "Bypass mode will run edits and commands without approval prompts. Continue?",
        confirmLabel: "Enable",
        danger: true,
      });
      if (!ok) return;
    }
    setPermissionMode(mode);
  };

  const handleProviderChange = (id: ProviderId) => {
    setProvider(id);
    applyProviderSection(id, sectionForUiProvider(settingsPayloadRef.current, id));
    setApiKey("");
    setShowKey(false);
    setConnectionStatus("idle");
  };

  const payload = () => {
    const bp = backendProvider(provider);
    const section = {
      api_key: apiKey,
      base_url: baseUrl,
      model: modelName,
      available_models: buildModelChoices(availableModels, modelName),
      thinking_budget: backendProvider(provider) === "anthropic" || customWireApi === "anthropic" ? thinkingBudget : undefined,
      wire_api: bp === "custom" ? customWireApi : undefined,
    };
    return {
      confirm_sensitive_change: true,
      provider: bp,
      openai: bp === "openai" ? section : {},
      anthropic: bp === "anthropic" ? section : {},
      custom: bp === "custom" ? section : {},
    };
  };

  const saveProvider = async () => {
    setSaving(true);
    setConnectionStatus("testing");
    try {
      const res = await fetch(`${apiBase()}/api/llm/settings`, {
        method: "PUT",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify(payload()),
      });
      if (!res.ok) throw new Error(await res.text());
      const saved = await res.json();
      settingsPayloadRef.current = saved as LLMSettingsPayload;
      const bp = backendProvider(provider);
      const section = saved[bp] as ProviderSection | undefined;
      setSavedKeyByProvider({
        openai: Boolean((saved as LLMSettingsPayload).openai?.has_api_key),
        anthropic: Boolean((saved as LLMSettingsPayload).anthropic?.has_api_key),
        custom: Boolean((saved as LLMSettingsPayload).custom?.has_api_key),
      });
      setApiKey("");
      setShowKey(false);
      const active = String(section?.model || modelName || saved.active_model || "");
      if (active) {
        setModelName(active);
        useAppStore.getState().setCurrentModel(active);
      }
      if (section?.available_models) useAppStore.getState().setAvailableModels(section.available_models);
      setThinkingBudget(Number(section?.thinking_budget ?? thinkingBudget) || 0);
      sendClientCommand({
        type: "llm.config.set",
        provider: bp,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        base_url: section?.base_url || baseUrl || undefined,
        model: section?.model || active || modelName || undefined,
        wire_api: section?.wire_api || (bp === "custom" ? customWireApi : undefined),
      });
      setConnectionStatus("success");
      pushToast(`Provider saved - model: ${active || modelName}`, "success");
    } catch (error) {
      setConnectionStatus("error");
      pushToast(`Provider save failed: ${String(error)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    setConnectionStatus("testing");
    try {
      const res = await fetch(`${apiBase()}/api/llm/models/refresh`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify(payload()),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as { models?: string[]; selected_model?: string };
      if (data.models?.length) {
        useAppStore.getState().setAvailableModels(data.models);
        const nextModel = data.selected_model || (modelName ? "" : data.models[0]);
        if (nextModel) {
          setModelName(nextModel);
          useAppStore.getState().setCurrentModel(nextModel);
        }
      }
      setConnectionStatus("success");
      pushToast("Connection checked", "success");
    } catch (error) {
      setConnectionStatus("error");
      pushToast(`Connection failed: ${formatProviderError(error)}`, "error");
    }
  };

  return (
    <div className="overlay-backdrop" onClick={toggleSettings} style={backdropStyle}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => { if (e.key === "Escape") toggleSettings(); }} style={modalStyle}>
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
            <>
              <Section title="Permissions" description="Control how tools and edits are approved">
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {(["ask_permissions", "acceptEdits", "plan", "auto", "bypass"] as const).map((m) => (
                    <button key={m} onClick={() => switchPermissionMode(m)} style={choiceStyle(permissionMode === m)}>
                      {m === "ask_permissions" ? "Ask" : m === "acceptEdits" ? "Accept" : m === "plan" ? "Plan" : m === "auto" ? "Auto" : "Bypass"}
                    </button>
                  ))}
                </div>
              </Section>

              <Section title="Current Model">
                <div style={monoTextStyle}>{currentModel || "Not configured"}</div>
              </Section>

              <Section title="Reasoning Effort" description="Advanced tuning. High is the default for coding work; change this only when speed or cost matters.">
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {EFFORT_LEVELS.map((level) => (
                    <button key={level.id} onClick={() => setEffortLevel(level.id)} style={choiceStyle(effortLevel === level.id)} title={level.desc}>
                      {level.label}
                    </button>
                  ))}
                </div>
              </Section>
            </>
          )}

          {activeTab === "provider" && (
            <>
              <Section title="Provider" description="Use DeepSeek, OpenAI, Anthropic, OpenRouter, or any OpenAI-compatible gateway">
                <div style={providerGridStyle}>
                  {PROVIDERS.map((p) => (
                    <button key={p.id} onClick={() => handleProviderChange(p.id)} style={providerButtonStyle(provider === p.id)}>
                      {p.label}
                    </button>
                  ))}
                </div>
              </Section>

              <Section title="API Key">
                {savedKeyByProvider[backendProvider(provider)] && !apiKey && (
                  <div style={savedKeyStateStyle}>
                    <span style={savedKeyBadgeStyle}>Saved</span>
                    <span style={savedKeyPreviewStyle}>{savedKeyPreview(providerConfig.placeholder)}</span>
                    <span style={savedKeyStateCopyStyle}>Stored locally and currently in use.</span>
                  </div>
                )}
                <div style={{ position: "relative" }}>
                  <input
                    type={showKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={savedKeyByProvider[backendProvider(provider)] ? "Paste a new key only if you want to replace the saved one" : providerConfig.placeholder}
                    style={{ ...inputStyle, paddingRight: 60 }}
                  />
                  <button onClick={() => setShowKey(!showKey)} style={showButtonStyle}>{showKey ? "Hide" : "Show"}</button>
                </div>
                <div style={hintLineStyle}>
                  {savedKeyByProvider[backendProvider(provider)]
                    ? "The saved key is active now. Leave this field empty to keep using it, or paste a new key to replace it."
                    : "No saved key for this provider yet."}
                </div>
              </Section>

              {providerConfig.hasBaseUrl && (
                <Section title="Base URL">
                  <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" style={inputStyle} />
                </Section>
              )}

              {backendProvider(provider) === "custom" && (
                <Section title="API Format">
                  <select
                    value={customWireApi}
                    onChange={(e) => setCustomWireApi(e.target.value as CustomWireApi)}
                    style={inputStyle}
                  >
                    <option value="chat">OpenAI Chat Completions</option>
                    <option value="responses">OpenAI Responses</option>
                    <option value="anthropic">Anthropic Messages</option>
                  </select>
                </Section>
              )}

              {(backendProvider(provider) === "anthropic" || customWireApi === "anthropic") && (
                <Section title="Thinking Budget" description="0 disables extended thinking. MiniCode enables it only for complex, tool, or multimodal turns.">
                  <input
                    type="number"
                    min={0}
                    step={512}
                    value={thinkingBudget}
                    onChange={(e) => setThinkingBudget(Math.max(0, Number(e.target.value) || 0))}
                    placeholder="0"
                    style={inputStyle}
                  />
                </Section>
              )}

              <Section title="Model">
                <input type="text" value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder={providerConfig.defaultModel || "model-name"} style={inputStyle} />
                {modelChoices.length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
                    {modelChoices.slice(0, 16).map((model) => (
                      <button key={model} onClick={() => setModelName(model)} style={choiceStyle(modelName === model)}>
                        {model}
                      </button>
                    ))}
                  </div>
                )}
              </Section>

              {connectionStatus !== "idle" && (
                <div style={statusStyle(connectionStatus)}>
                  {connectionStatus === "testing" && "Testing connection..."}
                  {connectionStatus === "success" && "Provider ready"}
                  {connectionStatus === "error" && "Provider check failed"}
                </div>
              )}

              <div style={actionBarStyle}>
                <button onClick={testConnection} disabled={connectionStatus === "testing"} style={secondaryActionStyle}>Test</button>
                <button onClick={saveProvider} disabled={saving} style={primaryActionStyle}>{saving ? "Saving..." : "Save & Apply"}</button>
              </div>
            </>
          )}

          {activeTab === "connectors" && <ConnectorsTabContent />}

          {activeTab === "scheduler" && <SchedulerTabContent />}

          {activeTab === "advanced" && (
            <>
              <Section title="Session">
                <div style={{ display: "grid", gap: 6, ...monoTextStyle }}>
                  <div>Model: {currentModel || "none"}</div>
                  <div>Provider: {provider}</div>
                  <div>Models: {availableModels.length > 0 ? availableModels.join(", ") : "none"}</div>
                </div>
              </Section>
              <Section title="Shortcuts">
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 24px", fontSize: "var(--text-xs)" }}>
                  {["Ctrl+K Command Palette", "Ctrl+, Settings", "Ctrl+J Terminal", "Ctrl+B Sidebar", "Enter Send", "Shift+Enter New Line", "/ Commands", "@ Mentions"].map((item) => <div key={item}>{item}</div>)}
                </div>
              </Section>
              <AdvancedEnvSection />
            </>
          )}

          {activeTab === "diagnostics" && isDesktop() && (
            <>
              <Section title="Platform">
                <div style={{ display: "grid", gap: 6, ...monoTextStyle }}>
                  <div>Platform: {desktop()?.platformInfo.platform}</div>
                  <div>Architecture: {desktop()?.platformInfo.arch}</div>
                  <div>Backend URL: {runtime()?.apiBaseUrl || "default"}</div>
                </div>
              </Section>
              <Section title="Export">
                <button
                  onClick={async () => {
                    setDiagLoading(true);
                    try {
                      const result = await exportDiagnostics();
                      setDiagResult((result ?? null) as Record<string, unknown> | null);
                      pushToast("Diagnostics exported", "success");
                    } catch {
                      pushToast("Export failed", "error");
                    }
                    setDiagLoading(false);
                  }}
                  disabled={diagLoading}
                  style={secondaryActionStyle}
                >
                  {diagLoading ? "Exporting..." : "Export Diagnostics"}
                </button>
                {diagResult && "logPath" in diagResult && <button onClick={() => revealPath(diagResult.logPath as string)} style={secondaryActionStyle}>Reveal Log File</button>}
              </Section>
              {diagResult && <pre style={preStyle}>{JSON.stringify(diagResult, null, 2)}</pre>}
            </>
          )}
        </div>
        </div>
      </div>
    </div>
  );
};

const Section = ({ title, description, children }: { title: string; description?: string; children?: React.ReactNode }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    <div>
      <div style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-primary)" }}>{title}</div>
      {description && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{description}</div>}
    </div>
    {children}
  </div>
);

const SettingRow = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
    <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", width: 86 }}>{label}</span>
    {children}
  </div>
);

const ConnectorsTabContent = () => {
  const mcpServers = useAppStore((s) => s.mcpServers);
  const marketplaceConnectors = useAppStore((s) => s.marketplaceConnectors);
  const [mode, setMode] = useState<"servers" | "marketplace">("servers");
  const [newServerName, setNewServerName] = useState("");
  const [newServerCommand, setNewServerCommand] = useState("");
  const [newServerArgs, setNewServerArgs] = useState("");
  const [newServerTransport, setNewServerTransport] = useState<"stdio" | "http">("stdio");
  const [newServerUrl, setNewServerUrl] = useState("");

  return (
    <>
      <div style={subTabBarStyle}>
        <button type="button" onClick={() => setMode("servers")} style={subTabStyle(mode === "servers")}>
          Servers
          <span style={subTabCountStyle}>{mcpServers.length}</span>
        </button>
        <button type="button" onClick={() => setMode("marketplace")} style={subTabStyle(mode === "marketplace")}>
          Marketplace
          <span style={subTabCountStyle}>{marketplaceConnectors.length}</span>
        </button>
      </div>

      {mode === "servers" && (
        <>
          <Section title="MCP Servers" description="Connectors stay here; prompts and tools should not flood the slash menu.">
            {mcpServers.length === 0 && <div style={emptyInlineStyle}>No MCP servers configured.</div>}
            {mcpServers.map((server) => (
              <div key={server.name} style={mcpServerRowStyle}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                    <span style={mcpDotStyle(server.status)} />
                    <span style={mcpNameStyle}>{server.name}</span>
                    <span style={statusChipStyle(server.status)}>{server.status}</span>
                    <span style={miniMetaStyle}>{server.transport || "stdio"}</span>
                    <span style={miniMetaStyle}>{server.tools ?? 0} tools</span>
                  </div>
                  {server.lastError && <div style={mcpErrorStyle}>{server.lastError}</div>}
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  <button onClick={() => sendClientCommand({ type: "mcp.restart", name: server.name })} style={mcpActionBtnStyle} title="Restart" aria-label={`Restart ${server.name}`}><RefreshCw size={14} /></button>
                  <button onClick={() => sendClientCommand({ type: "mcp.remove", name: server.name })} style={mcpActionBtnStyle} title="Remove" aria-label={`Remove ${server.name}`}><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </Section>

          <Section title="Add Server">
            <div style={{ display: "grid", gap: 8 }}>
              <div style={{ display: "flex", gap: 8 }}>
                <input type="text" value={newServerName} onChange={(e) => setNewServerName(e.target.value)} placeholder="Server name" style={{ ...inputStyle, flex: 1 }} />
                <select value={newServerTransport} onChange={(e) => setNewServerTransport(e.target.value as "stdio" | "http")} style={{ ...inputStyle, width: 90 }}>
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                </select>
              </div>
              {newServerTransport === "stdio" ? (
                <>
                  <input type="text" value={newServerCommand} onChange={(e) => setNewServerCommand(e.target.value)} placeholder="Command (python, npx, uvx...)" style={inputStyle} />
                  <input type="text" value={newServerArgs} onChange={(e) => setNewServerArgs(e.target.value)} placeholder="Args" style={inputStyle} />
                </>
              ) : (
                <input type="text" value={newServerUrl} onChange={(e) => setNewServerUrl(e.target.value)} placeholder="http://localhost:8080/mcp" style={inputStyle} />
              )}
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                <button onClick={() => sendClientCommand({ type: "mcp.list" })} style={secondaryActionStyle}>Refresh</button>
                <button
                  onClick={() => {
                    if (!newServerName.trim()) return;
                    sendClientCommand({
                      type: "mcp.add",
                      name: newServerName.trim(),
                      transport: newServerTransport,
                      command: newServerTransport === "stdio" ? newServerCommand.trim() : undefined,
                      args: newServerTransport === "stdio" ? newServerArgs.split(/\s+/).filter(Boolean) : undefined,
                      url: newServerTransport === "http" ? newServerUrl.trim() : undefined,
                    });
                    setNewServerName("");
                    setNewServerCommand("");
                    setNewServerArgs("");
                    setNewServerUrl("");
                  }}
                  disabled={!newServerName.trim()}
                  style={primaryActionStyle}
                >
                  Add Server
                </button>
              </div>
            </div>
          </Section>
        </>
      )}

      {mode === "marketplace" && (
        <Section title="Marketplace" description="Curated MCP connectors install into the server list.">
          {marketplaceConnectors.length === 0 && <div style={emptyInlineStyle}>No marketplace entries loaded yet.</div>}
          {marketplaceConnectors.length > 0 && (
            <div style={marketplaceListStyle}>
              {marketplaceConnectors.map((c) => (
                <div key={c.name} style={marketplaceRowStyle}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                      <div style={marketplaceTitleStyle}>{c.title}</div>
                      <span style={miniMetaStyle}>{c.transport}</span>
                    </div>
                    <div style={marketplaceDescStyle}>{c.description}</div>
                  </div>
                  {c.installed ? (
                    <span style={installedPillStyle}>Installed</span>
                  ) : (
                    <button
                      onClick={() => {
                        sendClientCommand({ type: "connectors.marketplace.install", name: c.name } as never);
                        setTimeout(() => {
                          sendClientCommand({ type: "mcp.list" });
                          sendClientCommand({ type: "connectors.marketplace.list" } as never);
                        }, 1000);
                      }}
                      style={compactInstallStyle}
                    >
                      Install
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}
    </>
  );
};

const SchedulerTabContent = () => {
  const scheduledTasks = useAppStore((s) => s.scheduledTasks);
  const [newTaskName, setNewTaskName] = useState("");
  const [newTaskPrompt, setNewTaskPrompt] = useState("");
  const [newTaskSchedule, setNewTaskSchedule] = useState("0 * * * *");

  return (
    <Section title="Scheduled Tasks" description="Cron-like tasks that automatically create sessions and run prompts on a schedule">
      {scheduledTasks.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {scheduledTasks.map((t) => (
            <div key={t.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", background: "var(--bg-secondary)", borderRadius: 4 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: t.enabled ? "var(--state-success)" : "var(--text-muted)", flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: "var(--text-sm)", fontWeight: 500, color: "var(--text-primary)" }}>{t.name}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{t.schedule}</div>
              </div>
              {t.last_run_at && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>Last: {new Date(t.last_run_at).toLocaleString()}</span>}
              <button
                onClick={() => sendClientCommand({ type: "scheduler.toggle", task_id: t.id, enabled: !t.enabled } as never)}
                style={{ ...secondaryActionStyle, padding: "2px 6px", fontSize: 11 }}
              >
                {t.enabled ? "Disable" : "Enable"}
              </button>
              <button
                onClick={() => sendClientCommand({ type: "scheduler.remove", task_id: t.id } as never)}
                style={{ ...secondaryActionStyle, padding: "2px 6px", fontSize: 11, color: "var(--text-error, #e55)" }}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}
      {scheduledTasks.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No scheduled tasks configured.</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 10, padding: "10px", background: "var(--bg-secondary)", borderRadius: 6 }}>
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginBottom: 2 }}>Add New Task</div>
        <input
          placeholder="Task name"
          value={newTaskName}
          onChange={(e) => setNewTaskName(e.target.value)}
          style={inputStyle}
        />
        <textarea
          placeholder="Prompt to run"
          value={newTaskPrompt}
          onChange={(e) => setNewTaskPrompt(e.target.value)}
          rows={3}
          style={{ ...inputStyle, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 12 }}
        />
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            placeholder="Cron (e.g. 0 9 * * 1-5)"
            value={newTaskSchedule}
            onChange={(e) => setNewTaskSchedule(e.target.value)}
            style={{ ...inputStyle, flex: 1, fontFamily: "var(--font-mono)", fontSize: 12 }}
          />
          <button
            onClick={() => {
              if (!newTaskName || !newTaskPrompt || !newTaskSchedule) return;
              sendClientCommand({ type: "scheduler.add", name: newTaskName, prompt: newTaskPrompt, schedule: newTaskSchedule } as never);
              setNewTaskName("");
              setNewTaskPrompt("");
              setNewTaskSchedule("0 * * * *");
            }}
            disabled={!newTaskName || !newTaskPrompt}
            style={secondaryActionStyle}
          >
            Add Task
          </button>
        </div>
      </div>
    </Section>
  );
};

const AdvancedEnvSection = () => {
  const envVars = useAppStore((s) => s.envVars);
  const [newEnvName, setNewEnvName] = useState("");
  const [newEnvValue, setNewEnvValue] = useState("");
  const [newEnvDescription, setNewEnvDescription] = useState("");

  return (
    <Section title="Environment Variables" description="Encrypted local vault for secrets injected into tool execution">
      {envVars.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {envVars.map((v) => (
            <div key={v.name} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 8px", background: "var(--bg-secondary)", borderRadius: 4 }}>
              <span style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{v.name}</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{v.scope}</span>
              <button
                onClick={() => {
                  sendClientCommand({ type: "env.delete", name: v.name } as never);
                }}
                style={{ ...secondaryActionStyle, padding: "2px 6px", fontSize: 11, color: "var(--text-error, #e55)" }}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}
      {envVars.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No environment variables configured.</div>}
      <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
        <input
          placeholder="NAME"
          value={newEnvName}
          onChange={(e) => setNewEnvName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ""))}
          style={{ ...inputStyle, width: 120, fontFamily: "var(--font-mono)", fontSize: 12 }}
        />
        <input
          placeholder="Value"
          type="password"
          value={newEnvValue}
          onChange={(e) => setNewEnvValue(e.target.value)}
          style={{ ...inputStyle, flex: 1, minWidth: 120 }}
        />
        <input
          placeholder="Description (optional)"
          value={newEnvDescription}
          onChange={(e) => setNewEnvDescription(e.target.value)}
          style={{ ...inputStyle, flex: 1, minWidth: 120 }}
        />
        <button
          onClick={() => {
            if (!newEnvName || !newEnvValue) return;
            sendClientCommand({ type: "env.set", name: newEnvName, value: newEnvValue, description: newEnvDescription } as never);
            setNewEnvName("");
            setNewEnvValue("");
            setNewEnvDescription("");
          }}
          disabled={!newEnvName || !newEnvValue}
          style={secondaryActionStyle}
        >
          Add
        </button>
      </div>
    </Section>
  );
};

const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, background: "rgba(0,0,0,0.38)", backdropFilter: "none", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100, animation: "none" };
const modalStyle: React.CSSProperties = { width: "min(920px, 94vw)", height: "min(720px, 88vh)", background: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md, 12px)", boxShadow: "var(--shadow-md)", overflow: "hidden", display: "flex", flexDirection: "column" };
const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-base)" };
const settingsBodyStyle: React.CSSProperties = { flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "220px minmax(0, 1fr)", background: "var(--surface-base)" };
const tabsStyle: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 4, padding: 10, borderRight: "1px solid var(--border-subtle)", background: "var(--surface-base)", overflowY: "auto" };
const contentStyle: React.CSSProperties = { padding: "18px 22px", overflow: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 18, minWidth: 0, background: "var(--surface-base)" };
const contentHeaderStyle: React.CSSProperties = { display: "grid", gap: 3, paddingBottom: 12, borderBottom: "1px solid var(--border-subtle)" };
const closeBtn: React.CSSProperties = { background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--text-muted)", width: 30, height: 30, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 0 };
const monoTextStyle: React.CSSProperties = { fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" };
const inputStyle: React.CSSProperties = { width: "100%", background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", padding: "9px 10px", color: "var(--text-primary)", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", outline: "none", boxSizing: "border-box", transition: "border-color 150ms" };
const hintLineStyle: React.CSSProperties = { marginTop: 6, color: "var(--text-muted)", fontSize: "var(--text-xs)", lineHeight: 1.4 };
const savedKeyStateStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
  marginBottom: 8,
  padding: "8px 10px",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
};
const savedKeyBadgeStyle: React.CSSProperties = {
  padding: "2px 7px",
  borderRadius: "999px",
  background: "var(--accent-soft)",
  color: "var(--accent-primary)",
  fontSize: "10px",
  fontWeight: 700,
  letterSpacing: "0.02em",
  textTransform: "uppercase",
};
const savedKeyPreviewStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  color: "var(--text-secondary)",
};
const savedKeyStateCopyStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
};
const showButtonStyle: React.CSSProperties = { position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", background: "transparent", border: 0, color: "var(--text-muted)", cursor: "pointer", fontSize: "var(--text-xs)" };
const primaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 34, borderRadius: "var(--radius-sm, 8px)", fontWeight: 600, cursor: "pointer", fontSize: "var(--text-sm)", background: "var(--accent-primary)", color: "var(--text-on-accent, var(--text-primary))", border: 0, transition: "opacity 150ms" };
const secondaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 34, borderRadius: "var(--radius-sm, 8px)", fontWeight: 600, cursor: "pointer", fontSize: "var(--text-sm)", background: "var(--surface-soft)", color: "var(--text-secondary)", border: "1px solid var(--border-subtle)", transition: "background 150ms" };
const preStyle: React.CSSProperties = { margin: 0, padding: 12, background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)", overflow: "auto", maxHeight: 200 };

const tabButtonStyle = (active: boolean): React.CSSProperties => ({ minHeight: 48, display: "grid", gap: 2, padding: "8px 10px", background: active ? "var(--surface-soft)" : "transparent", border: `1px solid ${active ? "var(--border-subtle)" : "transparent"}`, borderRadius: "var(--radius-sm, 7px)", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-sm)", textAlign: "left", boxShadow: active ? "inset 2px 0 0 var(--accent-primary)" : "none", transition: "background 100ms, color 100ms, border-color 100ms" });
const choiceStyle = (active: boolean): React.CSSProperties => ({ border: active ? "1px solid var(--accent-primary)" : "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", padding: "5px 9px", cursor: "pointer", fontSize: "var(--text-xs)", background: active ? "var(--accent-soft)" : "var(--surface-soft)", color: active ? "var(--accent-primary)" : "var(--text-secondary)" });
const providerButtonStyle = (active: boolean): React.CSSProperties => ({ padding: "9px 10px", background: active ? "var(--accent-soft)" : "var(--surface-soft)", border: active ? "1px solid var(--accent-primary)" : "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", cursor: "pointer", color: active ? "var(--accent-primary)" : "var(--text-secondary)", fontSize: "var(--text-sm)", fontWeight: active ? 600 : 400, textAlign: "left" });
const providerGridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 };
const actionBarStyle: React.CSSProperties = { position: "sticky", bottom: -18, display: "flex", justifyContent: "flex-end", gap: 8, padding: "12px 0 0", background: "var(--surface-base)" };
const statusStyle = (status: "idle" | "testing" | "success" | "error"): React.CSSProperties => ({ padding: "8px 10px", borderRadius: "var(--radius-sm, 4px)", background: status === "success" ? "var(--state-success-soft)" : status === "error" ? "var(--state-danger-soft)" : "var(--surface-soft)", border: "1px solid var(--border-subtle)", color: status === "error" ? "var(--state-danger)" : status === "success" ? "var(--state-success)" : "var(--text-secondary)", fontSize: "var(--text-xs)" });

const subTabBarStyle: React.CSSProperties = { display: "inline-flex", alignSelf: "flex-start", gap: 2, padding: 3, background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)" };
const subTabStyle = (active: boolean): React.CSSProperties => ({ display: "inline-flex", alignItems: "center", gap: 7, height: 30, padding: "0 10px", border: 0, borderRadius: "var(--radius-sm, 6px)", background: active ? "var(--surface-base)" : "transparent", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-sm)", fontWeight: 650 });
const subTabCountStyle: React.CSSProperties = { color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)" };
const emptyInlineStyle: React.CSSProperties = { padding: "12px 0", color: "var(--text-muted)", fontSize: "var(--text-sm)" };
const mcpServerRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 10, minHeight: 44, padding: "8px 10px", background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 7px)" };
const mcpNameStyle: React.CSSProperties = { flexShrink: 1, minWidth: 0, fontWeight: 650, fontSize: "var(--text-sm)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const mcpErrorStyle: React.CSSProperties = { fontSize: 11, color: "var(--state-danger)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const mcpDotStyle = (status: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, background: status === "connected" ? "var(--state-success)" : status === "error" ? "var(--state-danger)" : status === "starting" || status === "reconnecting" ? "var(--state-warning)" : "var(--text-muted)" });
const statusChipStyle = (status: string): React.CSSProperties => ({ flexShrink: 0, padding: "1px 6px", borderRadius: "999px", border: "1px solid var(--border-subtle)", color: status === "connected" ? "var(--state-success)" : status === "error" ? "var(--state-danger)" : "var(--text-muted)", fontSize: 10, fontWeight: 700, textTransform: "uppercase" });
const miniMetaStyle: React.CSSProperties = { flexShrink: 0, color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)" };
const mcpActionBtnStyle: React.CSSProperties = { background: "transparent", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 5px)", width: 28, height: 28, cursor: "pointer", color: "var(--text-muted)", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 };
const marketplaceListStyle: React.CSSProperties = { display: "grid", gap: 1, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", overflow: "hidden" };
const marketplaceRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 10, minHeight: 54, padding: "9px 11px", background: "var(--surface-soft)", borderBottom: "1px solid var(--border-subtle)" };
const marketplaceTitleStyle: React.CSSProperties = { fontSize: "var(--text-sm)", fontWeight: 650, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const marketplaceDescStyle: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const installedPillStyle: React.CSSProperties = { flexShrink: 0, padding: "3px 8px", borderRadius: "999px", color: "var(--state-success)", border: "1px solid color-mix(in oklch, var(--state-success) 35%, var(--border-subtle))", fontSize: 11, fontWeight: 650 };
const compactInstallStyle: React.CSSProperties = { flexShrink: 0, height: 30, padding: "0 12px", borderRadius: "var(--radius-sm, 6px)", border: "1px solid var(--border-subtle)", background: "var(--surface-base)", color: "var(--text-secondary)", fontSize: "var(--text-xs)", fontWeight: 650, cursor: "pointer" };
