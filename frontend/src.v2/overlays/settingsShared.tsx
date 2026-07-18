import React from "react";
import type { EffortLevel, PromptPersona } from "../stores/types";

// ── Types ──────────────────────────────────────────────────────────────

export type ProviderId = "anthropic" | "openai" | "deepseek" | "openrouter" | "custom";
export type Tab = "general" | "provider" | "connectors" | "scheduler" | "features" | "plugins" | "advanced";
export type CustomWireApi = "chat" | "responses" | "anthropic";
export type BackendProvider = "openai" | "anthropic" | "custom";

export type ProviderSection = {
  display_name?: string;
  name?: string;
  label?: string;
  has_api_key?: boolean;
  api_key?: string;
  base_url?: string;
  model?: string;
  available_models?: string[];
  models_source?: string;
  responses_reasoning_summary?: string;
  wire_api?: string;
  thinking_budget?: number;
  responses_stateful_continuation?: boolean;
  prompt_cache_retention?: string;
  reasoning_effort_levels?: EffortLevel[];
};

export type ProviderHistoryEntry = ProviderSection & {
  provider?: string;
  provider_id?: string;
  updated_at?: number;
};

export type LLMSettingsPayload = {
  provider?: string;
  active_model?: string;
  prompt_persona?: "minicode" | "codex" | string;
  openai?: ProviderSection;
  anthropic?: ProviderSection;
  custom?: ProviderSection;
  provider_history?: ProviderHistoryEntry[];
};

export type LLMCheckResult = {
  ok: boolean;
  provider: string;
  provider_id: string;
  base_url: string;
  model: string;
  wire_api: string;
  has_api_key: boolean;
  status_code?: number | null;
  message: string;
  hint?: string;
  models?: string[];
};

export type LLMModelsRefreshResult = {
  provider: string;
  provider_id: string;
  models: string[];
  selected_model: string;
  source: "live" | "preset" | string;
  source_message: string;
  generated_at?: number;
};

// ── Constants ──────────────────────────────────────────────────────────

export const PROVIDERS = [
  { id: "anthropic", label: "Anthropic", placeholder: "sk-ant-...", hasBaseUrl: true, defaultUrl: "", defaultModel: "claude-sonnet-4-6" },
  { id: "openai", label: "OpenAI", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.openai.com/v1", defaultModel: "gpt-5.4" },
  { id: "deepseek", label: "DeepSeek", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.deepseek.com/v1", defaultModel: "deepseek-v4-pro" },
  { id: "openrouter", label: "OpenRouter", placeholder: "sk-or-...", hasBaseUrl: true, defaultUrl: "https://openrouter.ai/api/v1", defaultModel: "anthropic/claude-sonnet-4" },
  { id: "custom", label: "Custom", placeholder: "API key", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
] as const;

export const EFFORT_LEVELS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "none", label: "none", desc: "Provider effort: none" },
  { id: "minimal", label: "minimal", desc: "Provider effort: minimal" },
  { id: "low", label: "low", desc: "Provider effort: low" },
  { id: "medium", label: "medium", desc: "Provider effort: medium" },
  { id: "high", label: "high", desc: "Provider effort: high" },
  { id: "xhigh", label: "xhigh", desc: "Provider effort: xhigh" },
  { id: "max", label: "max", desc: "Provider effort: max" },
];

// ── Helpers ────────────────────────────────────────────────────────────

export const toUiProvider = (payload: unknown): ProviderId => {
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

export const backendProvider = (provider: ProviderId): "openai" | "anthropic" | "custom" =>
  provider === "anthropic" ? "anthropic" : provider === "openai" ? "openai" : "custom";

export const defaultSectionForProvider = (provider: ProviderId): ProviderSection => {
  const cfg = PROVIDERS.find((item) => item.id === provider)!;
  return {
    base_url: cfg.defaultUrl,
    model: cfg.defaultModel,
    available_models: cfg.defaultModel ? [cfg.defaultModel] : [],
    wire_api: provider === "anthropic" ? "anthropic" : provider === "openai" ? "responses" : "chat",
    thinking_budget: 0,
    responses_reasoning_summary: "off",
    responses_stateful_continuation: provider === "openai",
    prompt_cache_retention: provider === "openai" ? "24h" : "",
  };
};

export const sectionForUiProvider = (payload: LLMSettingsPayload | null, provider: ProviderId): ProviderSection | undefined => {
  if (!payload) return undefined;
  const bp = backendProvider(provider);
  if (bp !== "custom") return payload[bp];
  const section = payload.custom;
  const host = section?.base_url ?? "";
  if (provider === "deepseek") return host.includes("deepseek.com") ? section : undefined;
  if (provider === "openrouter") return host.includes("openrouter.ai") ? section : undefined;
  return section;
};

const historyProviderForEntry = (entry: ProviderHistoryEntry): ProviderId => {
  const provider = String(entry.provider || "").trim().toLowerCase();
  const providerId = String(entry.provider_id || "").trim().toLowerCase();
  const host = String(entry.base_url || "").trim().toLowerCase();
  if (provider === "anthropic" || providerId === "anthropic_off") return "anthropic";
  if (provider === "openai" || providerId === "openai_official") return "openai";
  if (host.includes("deepseek.com") || providerId === "deepseek") return "deepseek";
  if (host.includes("openrouter.ai") || providerId === "openrouter") return "openrouter";
  return "custom";
};

export const historyForUiProvider = (
  payload: LLMSettingsPayload | null,
  provider: ProviderId,
): ProviderHistoryEntry[] => {
  const history = Array.isArray(payload?.provider_history) ? payload!.provider_history! : [];
  return history.filter((entry) => historyProviderForEntry(entry) === provider);
};

export const sectionFromHistoryEntry = (entry?: ProviderHistoryEntry): ProviderSection | undefined => {
  if (!entry) return undefined;
    return {
    display_name: entry.display_name,
    name: entry.name,
    label: entry.label,
    has_api_key: entry.has_api_key,
    api_key: entry.api_key,
    base_url: entry.base_url,
    model: entry.model,
    available_models: entry.available_models,
    models_source: entry.models_source,
    responses_reasoning_summary: entry.responses_reasoning_summary,
    wire_api: entry.wire_api,
    thinking_budget: entry.thinking_budget,
    responses_stateful_continuation: entry.responses_stateful_continuation,
    prompt_cache_retention: entry.prompt_cache_retention,
  };
};

export const savedOrHistorySectionForUiProvider = (
  payload: LLMSettingsPayload | null,
  provider: ProviderId,
): ProviderSection | undefined =>
  sectionForUiProvider(payload, provider) ?? sectionFromHistoryEntry(historyForUiProvider(payload, provider)[0]);

export const buildModelChoices = (models: string[], current: string): string[] => {
  const merged = [current, ...models]
    .map((model) => model.trim())
    .filter(Boolean);
  return Array.from(new Set(merged));
};

export const providerDisplayName = (
  section?: Pick<ProviderSection, "display_name" | "name" | "label">,
): string => {
  for (const value of [section?.display_name, section?.name, section?.label]) {
    const text = String(value || "").trim();
    if (text) return text;
  }
  return "";
};

export const effectiveCustomWireApi = (
  provider: ProviderId,
  baseUrl: string,
  wireApi: CustomWireApi,
): CustomWireApi => {
  if (provider === "anthropic") return "anthropic";
  if (provider === "openai") return wireApi === "responses" ? "responses" : "chat";
  const host = baseUrl.trim().toLowerCase();
  if (provider === "deepseek" || host.includes("api.deepseek.com")) return "chat";
  return wireApi;
};

export const isGptLikeModelId = (model: string): boolean => {
  const normalized = String(model || "").trim().replace(/_/g, "-").toLowerCase();
  return Boolean(
    normalized.startsWith("gpt-") ||
    normalized.includes("/gpt-") ||
    normalized.startsWith("codex") ||
    normalized.includes("/codex"),
  );
};

export const canChooseApiFormat = (provider: ProviderId, baseUrl: string): boolean =>
  provider === "openai" ||
  (backendProvider(provider) === "custom" && effectiveCustomWireApi(provider, baseUrl, "responses") === "responses");

export const defaultResponsesStatefulContinuation = (wireApi: string): boolean =>
  String(wireApi || "").trim().toLowerCase() === "responses";

export const defaultPromptCacheRetention = (wireApi: string): string =>
  defaultResponsesStatefulContinuation(wireApi) ? "24h" : "";

export const shouldShowResponsesFastPathHint = (
  provider: ProviderId,
  baseUrl: string,
  model: string,
  wireApi: string,
): boolean => {
  if (!isGptLikeModelId(model)) return false;
  if (!canChooseApiFormat(provider, baseUrl)) return false;
  const normalizedWire = String(wireApi || "chat").trim().toLowerCase();
  if (normalizedWire === "responses") return false;
  return effectiveCustomWireApi(provider, baseUrl, "responses") === "responses";
};

export const runtimeCapabilityEffortLevels = (value: unknown): EffortLevel[] => {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => String(item || "").trim().toLowerCase())
    .filter((item): item is EffortLevel =>
      item === "none" ||
      item === "minimal" ||
      item === "low" ||
      item === "medium" ||
      item === "high" ||
      item === "xhigh" ||
      item === "max"
    );
};

export const formatProviderError = (error: unknown): string => {
  const raw = error instanceof Error ? error.message : String(error);
  const text = raw.replace(/^Error:\s*/i, "").trim();
  if (/your request was blocked/i.test(text)) {
    return "gateway blocked the request. Check the gateway allowlist, Base URL, API format, and selected model.";
  }
  return text || "check key, URL, API format, and model";
};

export const normalizePromptPersona = (value: unknown): PromptPersona | undefined => {
  const text = String(value ?? "").trim().toLowerCase();
  if (text === "codex" || text === "minicode") return text;
  return undefined;
};

export const formatProviderCheckSummary = (result: LLMCheckResult): string => {
  if (result.ok) return `\u9274\u6743\u901A\u8FC7 \u00B7 ${result.provider_id} \u00B7 ${result.model || "\u672A\u9009\u62E9\u6A21\u578B"}`;
  const status = result.status_code ? `HTTP ${result.status_code}` : "\u672A\u901A\u8FC7";
  return `${status} \u00B7 ${result.provider_id} \u00B7 ${result.hint || result.message || "\u8BF7\u68C0\u67E5 key\u3001URL \u548C\u6A21\u578B\u662F\u5426\u5339\u914D"}`;
};

// ── Shared small components ────────────────────────────────────────────

export const Section = ({ title, description, children }: { title: string; description?: string; children?: React.ReactNode }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    <div>
      <div style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-primary)" }}>{title}</div>
      {description && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{description}</div>}
    </div>
    {children}
  </div>
);

export const SettingRow = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
    <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", width: 86 }}>{label}</span>
    {children}
  </div>
);

export const ProviderCheckPanel = ({ result }: { result: LLMCheckResult }) => (
  <div style={providerCheckPanelStyle}>
    <div style={providerCheckTitleStyle}>{result.ok ? "\u6A21\u578B\u9274\u6743\u901A\u8FC7" : "\u6A21\u578B\u9274\u6743\u5931\u8D25"}</div>
    <div style={providerCheckGridStyle}>
      <span>Provider</span><code>{result.provider_id || result.provider}</code>
      <span>Base URL</span><code>{result.base_url || "\u672A\u8BBE\u7F6E"}</code>
      <span>Model</span><code>{result.model || "\u672A\u8BBE\u7F6E"}</code>
      <span>Key</span><code>{result.has_api_key ? "\u5DF2\u914D\u7F6E" : "\u672A\u914D\u7F6E"}</code>
      {result.status_code != null && <><span>Status</span><code>{result.status_code}</code></>}
    </div>
    {!result.ok && (result.hint || result.message) && (
      <div style={providerCheckHintStyle}>{result.hint || result.message}</div>
    )}
  </div>
);

// ── Styles ─────────────────────────────────────────────────────────────

export const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, backgroundColor: "var(--backdrop-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: "var(--z-modal)", pointerEvents: "auto" };
export const modalStyle: React.CSSProperties = { width: "min(920px, 94vw)", height: "min(720px, 88vh)", backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md, 12px)", boxShadow: "var(--shadow-md)", overflow: "hidden", display: "flex", flexDirection: "column", pointerEvents: "auto" };
export const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)", backgroundColor: "var(--surface-base)" };
export const settingsBodyStyle: React.CSSProperties = { flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "64px minmax(0, 1fr)", backgroundColor: "var(--surface-base)" };
export const tabsStyle: React.CSSProperties = { display: "flex", flexDirection: "column", alignItems: "center", gap: 5, padding: "10px 8px", borderRight: "1px solid var(--border-subtle)", backgroundColor: "var(--surface-soft)", overflowY: "auto" };
export const contentStyle: React.CSSProperties = { padding: "18px 22px", overflow: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 18, minWidth: 0, backgroundColor: "var(--surface-base)" };
export const contentHeaderStyle: React.CSSProperties = { paddingBottom: 10, borderBottom: "1px solid var(--border-subtle)" };
export const closeBtn: React.CSSProperties = { backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--text-muted)", width: 30, height: 30, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 0 };
export const monoTextStyle: React.CSSProperties = { fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" };
export const inputStyle: React.CSSProperties = { width: "100%", backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", padding: "9px 10px", color: "var(--text-primary)", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", outline: "none", boxSizing: "border-box", transition: "border-color 150ms" };
export const selectInputStyle: React.CSSProperties = { ...inputStyle, cursor: "pointer", fontFamily: "var(--font-ui)", colorScheme: "light dark" };
export const hintLineStyle: React.CSSProperties = { marginTop: 6, color: "var(--text-muted)", fontSize: "var(--text-xs)", lineHeight: 1.4 };
export const primaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 34, borderRadius: "var(--radius-sm, 8px)", fontWeight: 600, cursor: "pointer", fontSize: "var(--text-sm)", backgroundColor: "var(--accent-primary)", color: "var(--text-on-accent, var(--text-primary))", border: 0, transition: "opacity 150ms" };
export const secondaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 34, borderRadius: "var(--radius-sm, 8px)", fontWeight: 600, cursor: "pointer", fontSize: "var(--text-sm)", backgroundColor: "var(--surface-soft)", color: "var(--text-secondary)", border: "1px solid var(--border-subtle)", transition: "background 150ms" };
export const preStyle: React.CSSProperties = { margin: 0, padding: 12, backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)", overflow: "auto", maxHeight: 200 };

export const tabButtonStyle = (active: boolean): React.CSSProperties => ({ width: 42, minHeight: 40, display: "flex", alignItems: "center", justifyContent: "center", padding: 0, backgroundColor: active ? "var(--surface-base)" : "transparent", border: `1px solid ${active ? "var(--border-subtle)" : "transparent"}`, borderRadius: "var(--radius-sm, 7px)", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-sm)", boxShadow: active ? "inset 3px 0 0 var(--accent-primary)" : "none", transition: "background var(--transition-micro), color var(--transition-micro), border-color var(--transition-micro)" });
export const choiceStyle = (active: boolean): React.CSSProperties => ({ border: active ? "1px solid var(--accent-primary)" : "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", padding: "5px 9px", cursor: "pointer", fontSize: "var(--text-xs)", backgroundColor: active ? "var(--accent-soft)" : "var(--surface-soft)", color: active ? "var(--accent-primary)" : "var(--text-secondary)" });
export const providerButtonStyle = (active: boolean): React.CSSProperties => ({ padding: "9px 10px", backgroundColor: active ? "var(--accent-soft)" : "var(--surface-soft)", border: active ? "1px solid var(--accent-primary)" : "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", cursor: "pointer", color: active ? "var(--accent-primary)" : "var(--text-secondary)", fontSize: "var(--text-sm)", fontWeight: active ? 600 : 400, textAlign: "left" });
export const providerGridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 };
export const actionBarStyle: React.CSSProperties = { position: "sticky", bottom: -18, display: "flex", justifyContent: "flex-end", gap: 8, padding: "12px 0 0", backgroundColor: "var(--surface-base)" };
export const statusStyle = (status: "idle" | "testing" | "success" | "error"): React.CSSProperties => ({ padding: "8px 10px", borderRadius: "var(--radius-sm, 4px)", backgroundColor: status === "success" ? "var(--state-success-soft)" : status === "error" ? "var(--state-danger-soft)" : "var(--surface-soft)", border: "1px solid var(--border-subtle)", color: status === "error" ? "var(--state-danger)" : status === "success" ? "var(--state-success)" : "var(--text-secondary)", fontSize: "var(--text-xs)" });
export const providerCheckPanelStyle: React.CSSProperties = { display: "grid", gap: 8, color: "var(--text-secondary)" };
export const providerCheckTitleStyle: React.CSSProperties = { fontWeight: 700, color: "inherit" };
export const providerCheckGridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "72px minmax(0, 1fr)", gap: "4px 10px", color: "var(--text-muted)" };
export const providerCheckHintStyle: React.CSSProperties = { color: "var(--state-danger)", lineHeight: 1.45 };

export const subTabBarStyle: React.CSSProperties = { display: "inline-flex", alignSelf: "flex-start", gap: 2, padding: 3, backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)" };
export const subTabStyle = (active: boolean): React.CSSProperties => ({ display: "inline-flex", alignItems: "center", gap: 7, height: 30, padding: "0 10px", border: 0, borderRadius: "var(--radius-sm, 6px)", backgroundColor: active ? "var(--surface-base)" : "transparent", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-sm)", fontWeight: 650 });
export const subTabCountStyle: React.CSSProperties = { color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)" };
export const emptyInlineStyle: React.CSSProperties = { padding: "12px 0", color: "var(--text-muted)", fontSize: "var(--text-sm)" };
export const mcpServerRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 10, minHeight: 44, padding: "8px 10px", backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 7px)" };
export const mcpNameStyle: React.CSSProperties = { flexShrink: 1, minWidth: 0, fontWeight: 650, fontSize: "var(--text-sm)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const mcpErrorStyle: React.CSSProperties = { fontSize: 11, color: "var(--state-danger)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const mcpTone = (state: string): string => {
  if (state === "connected") return "var(--state-success)";
  if (state === "error" || state === "failed" || state === "expired") return "var(--state-danger)";
  if (state === "auth_required" || state === "starting" || state === "connecting" || state === "reconnecting") return "var(--state-warning)";
  return "var(--text-muted)";
};
export const mcpDotStyle = (status: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, backgroundColor: mcpTone(status) });
export const statusChipStyle = (status: string): React.CSSProperties => ({ flexShrink: 0, padding: "1px 6px", borderRadius: "999px", border: "1px solid var(--border-subtle)", color: mcpTone(status), fontSize: 10, fontWeight: 700, textTransform: "uppercase" });
export const miniMetaStyle: React.CSSProperties = { flexShrink: 0, color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)" };
export const mcpActionBtnStyle: React.CSSProperties = { backgroundColor: "transparent", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 5px)", width: 28, height: 28, cursor: "pointer", color: "var(--text-muted)", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 };
export const marketplaceListStyle: React.CSSProperties = { display: "grid", gap: 1, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", overflow: "hidden" };
export const marketplaceRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 10, minHeight: 54, padding: "9px 11px", backgroundColor: "var(--surface-soft)", borderBottom: "1px solid var(--border-subtle)" };
export const marketplaceTitleStyle: React.CSSProperties = { fontSize: "var(--text-sm)", fontWeight: 650, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const marketplaceDescStyle: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const installedPillStyle: React.CSSProperties = { flexShrink: 0, padding: "3px 8px", borderRadius: "999px", color: "var(--state-success)", border: "1px solid color-mix(in oklch, var(--state-success) 35%, var(--border-subtle))", fontSize: 11, fontWeight: 650 };
export const compactInstallStyle: React.CSSProperties = { flexShrink: 0, height: 30, padding: "0 12px", borderRadius: "var(--radius-sm, 6px)", border: "1px solid var(--border-subtle)", backgroundColor: "var(--surface-base)", color: "var(--text-secondary)", fontSize: "var(--text-xs)", fontWeight: 650, cursor: "pointer" };
