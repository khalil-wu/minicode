import { useState } from "react";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import {
  type ProviderId,
  type CustomWireApi,
  type LLMSettingsPayload,
  type LLMCheckResult,
  type ProviderSection,
  PROVIDERS,
  Section,
  ProviderCheckPanel,
  backendProvider,
  defaultSectionForProvider,
  sectionForUiProvider,
  buildModelChoices,
  effectiveCustomWireApi,
  canChooseApiFormat,
  formatProviderError,
  formatProviderCheckSummary,
  savedKeyPreview,
  providerGridStyle,
  providerButtonStyle,
  inputStyle,
  choiceStyle,
  hintLineStyle,
  savedKeyStateStyle,
  savedKeyBadgeStyle,
  savedKeyPreviewStyle,
  savedKeyStateCopyStyle,
  showButtonStyle,
  statusStyle,
  actionBarStyle,
  primaryActionStyle,
  secondaryActionStyle,
} from "./settingsShared";

export const ProviderTab = ({
  settingsPayloadRef,
  onProviderChange,
  onSettingsPayloadChange,
}: {
  settingsPayloadRef: React.MutableRefObject<LLMSettingsPayload | null>;
  onProviderChange: (id: ProviderId) => void;
  onSettingsPayloadChange?: (payload: LLMSettingsPayload) => void;
}) => {
  const currentModel = useAppStore((s) => s.currentModel);
  const availableModels = useAppStore((s) => s.availableModels);

  const [provider, setProvider] = useState<ProviderId>("deepseek");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com/v1");
  const [modelName, setModelName] = useState("deepseek-chat");
  const [customWireApi, setCustomWireApi] = useState<CustomWireApi>("chat");
  const [thinkingBudget, setThinkingBudget] = useState(0);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [connectionResult, setConnectionResult] = useState<LLMCheckResult | null>(null);
  const [savedKeyByProvider, setSavedKeyByProvider] = useState<Record<"openai" | "anthropic" | "custom", boolean>>({
    openai: false,
    anthropic: false,
    custom: false,
  });

  const providerConfig = PROVIDERS.find((p) => p.id === provider)!;
  const modelChoices = buildModelChoices(availableModels, modelName);
  const effectiveWireApi = effectiveCustomWireApi(provider, baseUrl, customWireApi);
  const showApiFormat = canChooseApiFormat(provider, baseUrl);

  const applyProviderSection = (nextProvider: ProviderId, section?: ProviderSection) => {
    const fallback = defaultSectionForProvider(nextProvider);
    setBaseUrl(section?.base_url ?? fallback.base_url ?? "");
    setModelName(section?.model ?? fallback.model ?? "");
    const models = buildModelChoices(section?.available_models ?? fallback.available_models ?? [], section?.model ?? fallback.model ?? "");
    useAppStore.getState().setAvailableModels(models);
    if (section?.model || fallback.model) useAppStore.getState().setCurrentModel(section?.model ?? fallback.model ?? "");
    if (backendProvider(nextProvider) === "custom") {
      const wire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
      setCustomWireApi(effectiveCustomWireApi(nextProvider, section?.base_url ?? fallback.base_url ?? "", wire as CustomWireApi));
    } else {
      setCustomWireApi("chat");
    }
    setThinkingBudget(Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0);
  };

  const handleProviderChange = (id: ProviderId) => {
    setProvider(id);
    applyProviderSection(id, sectionForUiProvider(settingsPayloadRef.current, id));
    setApiKey("");
    setShowKey(false);
    setConnectionStatus("idle");
    setConnectionResult(null);
    onProviderChange(id);
  };

  const payload = () => {
    const bp = backendProvider(provider);
    const section = {
      api_key: apiKey,
      base_url: baseUrl,
      model: modelName,
      available_models: buildModelChoices(availableModels, modelName),
      thinking_budget: backendProvider(provider) === "anthropic" || effectiveWireApi === "anthropic" ? thinkingBudget : undefined,
      wire_api: bp === "custom" ? effectiveWireApi : undefined,
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
    setConnectionResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/llm/settings`, {
        method: "PUT",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify(payload()),
      });
      if (!res.ok) throw new Error(await res.text());
      const saved = await res.json();
      const savedPayload = saved as LLMSettingsPayload;
      settingsPayloadRef.current = savedPayload;
      onSettingsPayloadChange?.(savedPayload);
      const bp = backendProvider(provider);
      const section = savedPayload[bp] as ProviderSection | undefined;
      setSavedKeyByProvider({
        openai: Boolean(savedPayload.openai?.has_api_key),
        anthropic: Boolean(savedPayload.anthropic?.has_api_key),
        custom: Boolean(savedPayload.custom?.has_api_key),
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
      const appliedWireApi = bp === "custom"
        ? effectiveCustomWireApi(provider, section?.base_url || baseUrl, ((section?.wire_api as CustomWireApi | undefined) || effectiveWireApi))
        : undefined;
      if (appliedWireApi) setCustomWireApi(appliedWireApi);
      sendClientCommand({
        type: "llm.config.set",
        provider: bp,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        base_url: section?.base_url || baseUrl || undefined,
        model: section?.model || active || modelName || undefined,
        wire_api: appliedWireApi,
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
    setConnectionResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/llm/check`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify(payload()),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as LLMCheckResult;
      setConnectionResult(data);
      if (data.models?.length) {
        useAppStore.getState().setAvailableModels(data.models);
        const nextModel = data.model || (modelName ? "" : data.models[0]);
        if (nextModel) {
          setModelName(nextModel);
          useAppStore.getState().setCurrentModel(nextModel);
        }
      }
      setConnectionStatus(data.ok ? "success" : "error");
      pushToast(formatProviderCheckSummary(data), data.ok ? "success" : "error");
    } catch (error) {
      setConnectionStatus("error");
      pushToast(`Connection failed: ${formatProviderError(error)}`, "error");
    }
  };

  return (
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

      {showApiFormat && (
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

      {(backendProvider(provider) === "anthropic" || effectiveWireApi === "anthropic") && (
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
          {connectionStatus === "testing" && "\u6B63\u5728\u68C0\u67E5\u6A21\u578B\u9274\u6743..."}
          {connectionStatus !== "testing" && connectionResult && (
            <ProviderCheckPanel result={connectionResult} />
          )}
          {connectionStatus === "success" && !connectionResult && "Provider ready"}
          {connectionStatus === "error" && !connectionResult && "Provider check failed"}
        </div>
      )}

      <div style={actionBarStyle}>
        <button onClick={testConnection} disabled={connectionStatus === "testing"} style={secondaryActionStyle}>Check auth</button>
        <button onClick={saveProvider} disabled={saving} style={primaryActionStyle}>{saving ? "Saving..." : "Save & Apply"}</button>
      </div>
    </>
  );
};
