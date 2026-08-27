import React from "react";
import type { EffortLevel, SettingsTab } from "../stores/types";

// ── Types ──────────────────────────────────────────────────────────────

export type ProviderId = "anthropic" | "openai" | "custom";
export type Tab = SettingsTab;
export type CustomWireApi = "chat" | "responses" | "anthropic";
export type ProviderProxyMode = "inherit" | "direct";

export type ProviderModelMetadata = {
  context_window?: number;
  max_context_window?: number;
  max_output_tokens?: number;
  reasoning_effort_levels?: EffortLevel[];
  default_reasoning_effort?: string;
  default_reasoning_summary?: string;
  source?: string;
};

export type ProviderSection = {
  display_name?: string;
  name?: string;
  label?: string;
  has_api_key?: boolean;
  api_key?: string;
  headers?: Record<string, string>;
  auth_header?: boolean;
  base_url?: string;
  model?: string;
  small_fast_model?: string;
  available_models?: string[];
  models_source?: string;
  model_metadata?: Record<string, ProviderModelMetadata>;
  model_labels?: Record<string, string>;
  reasoning_effort?: string;
  configured_reasoning_effort?: string;
  effective_reasoning_effort?: string;
  reasoning_effort_supported?: boolean;
  responses_reasoning_summary?: string;
  /** Per-request output ceiling. Zero is the persisted Auto/Unset sentinel. */
  max_tokens?: number;
  wire_api?: string;
  proxy_mode?: ProviderProxyMode;
  thinking_budget?: number;
  prompt_cache_retention?: string;
  reasoning_effort_levels?: EffortLevel[];
  context_window?: number;
  context_window_source?: string;
  context_window_verified?: boolean;
  max_context_window?: number;
  max_context_window_source?: string;
  max_context_window_verified?: boolean;
  max_output_tokens?: number;
  max_output_tokens_source?: string;
  max_output_tokens_verified?: boolean;
  default_reasoning_effort?: string;
  default_reasoning_summary?: string;
  image_mode?: "disabled" | "inherit" | "custom";
  has_image_api_key?: boolean;
  image_api_key?: string;
  image_base_url?: string;
  image_model?: string;
  image_size?: string;
  image_quality?: string;
};

export type ProviderHistoryEntry = ProviderSection & {
  provider?: string;
  provider_id?: string;
  updated_at?: number;
};

export type LLMSettingsPayload = {
  provider?: string;
  active_model?: string;
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
  proxy_mode?: ProviderProxyMode;
  generation_kind?: "text" | "image";
  has_api_key: boolean;
  status_code?: number | null;
  model_discovery_ok?: boolean | null;
  generation_ok?: boolean | null;
  failure_kind?: string;
  retryable?: boolean;
  message: string;
  hint?: string;
  image_generation_ok?: boolean | null;
  image_status_code?: number | null;
  image_failure_kind?: string;
  image_retryable?: boolean;
  image_message?: string;
  image_hint?: string;
  image_model?: string;
  models?: string[];
};

export type LLMModelsRefreshResult = {
  provider: string;
  provider_id: string;
  models: string[];
  selected_model: string;
  proxy_mode?: ProviderProxyMode;
  source: "live" | "preset" | string;
  source_message: string;
  status_code?: number | null;
  failure_kind?: string;
  retryable?: boolean;
  message?: string;
  hint?: string;
  model_metadata?: Record<string, ProviderModelMetadata>;
  reasoning_effort_levels?: EffortLevel[];
  configured_reasoning_effort?: string;
  effective_reasoning_effort?: string;
  reasoning_effort_supported?: boolean;
  context_window?: number;
  context_window_source?: string;
  context_window_verified?: boolean;
  max_context_window?: number;
  max_context_window_source?: string;
  max_context_window_verified?: boolean;
  max_output_tokens?: number;
  max_output_tokens_source?: string;
  max_output_tokens_verified?: boolean;
  default_reasoning_effort?: string;
  default_reasoning_summary?: string;
  generated_at?: number;
};

// ── Constants ──────────────────────────────────────────────────────────

export const PROVIDERS = [
  { id: "anthropic", label: "Anthropic", placeholder: "sk-ant-...", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
  { id: "openai", label: "OpenAI", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.openai.com/v1", defaultModel: "" },
  { id: "custom", label: "自定义", placeholder: "API 密钥", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
] as const;

// ── Helpers ────────────────────────────────────────────────────────────

export const toUiProvider = (payload: unknown): ProviderId => {
  const value = payload as LLMSettingsPayload;
  const rawProvider = String(value.provider || "custom").trim().toLowerCase();
  if (rawProvider === "anthropic") return "anthropic";
  if (rawProvider === "openai") return "openai";
  if (rawProvider === "custom") return "custom";
  return "custom";
};

export const backendProvider = (provider: ProviderId): "openai" | "anthropic" | "custom" =>
  provider === "anthropic" ? "anthropic" : provider === "openai" ? "openai" : "custom";

export const defaultSectionForProvider = (provider: ProviderId): ProviderSection => {
  const cfg = PROVIDERS.find((item) => item.id === provider)!;
  return {
    base_url: cfg.defaultUrl,
    headers: {},
    auth_header: false,
    model: "",
    available_models: [],
    wire_api: provider === "anthropic" ? "anthropic" : provider === "openai" ? "responses" : "chat",
    proxy_mode: "inherit",
    thinking_budget: 0,
    responses_reasoning_summary: "off",
    max_tokens: 0,
    prompt_cache_retention: "",
    model_metadata: {},
    model_labels: {},
    reasoning_effort_levels: [],
    configured_reasoning_effort: "",
    effective_reasoning_effort: "",
    reasoning_effort_supported: false,
    image_mode: "inherit",
    image_base_url: "",
    image_model: "",
    image_size: "1024x1024",
    image_quality: "",
  };
};

export const sectionForUiProvider = (payload: LLMSettingsPayload | null, provider: ProviderId): ProviderSection | undefined => {
  if (!payload) return undefined;
  return payload[backendProvider(provider)];
};

const historyProviderForEntry = (entry: ProviderHistoryEntry): ProviderId => {
  const provider = String(entry.provider || "").trim().toLowerCase();
  const providerId = String(entry.provider_id || "").trim().toLowerCase();
  if (provider === "anthropic" || providerId === "anthropic") return "anthropic";
  if (provider === "openai" || providerId === "openai") return "openai";
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
    headers: entry.headers,
    auth_header: entry.auth_header,
    base_url: entry.base_url,
    model: entry.model,
    small_fast_model: entry.small_fast_model,
    available_models: entry.available_models,
    models_source: entry.models_source,
    model_metadata: entry.model_metadata,
    model_labels: entry.model_labels,
    reasoning_effort: entry.reasoning_effort,
    configured_reasoning_effort: entry.configured_reasoning_effort,
    effective_reasoning_effort: entry.effective_reasoning_effort,
    reasoning_effort_supported: entry.reasoning_effort_supported,
    responses_reasoning_summary: entry.responses_reasoning_summary,
    max_tokens: entry.max_tokens,
    wire_api: entry.wire_api,
    proxy_mode: entry.proxy_mode,
    thinking_budget: entry.thinking_budget,
    prompt_cache_retention: entry.prompt_cache_retention,
    reasoning_effort_levels: entry.reasoning_effort_levels,
    context_window: entry.context_window,
    context_window_source: entry.context_window_source,
    context_window_verified: entry.context_window_verified,
    max_context_window: entry.max_context_window,
    max_context_window_source: entry.max_context_window_source,
    max_context_window_verified: entry.max_context_window_verified,
    max_output_tokens: entry.max_output_tokens,
    max_output_tokens_source: entry.max_output_tokens_source,
    max_output_tokens_verified: entry.max_output_tokens_verified,
    default_reasoning_effort: entry.default_reasoning_effort,
    default_reasoning_summary: entry.default_reasoning_summary,
    image_mode: entry.image_mode,
    has_image_api_key: entry.has_image_api_key,
    image_api_key: entry.image_api_key,
    image_base_url: entry.image_base_url,
    image_model: entry.image_model,
    image_size: entry.image_size,
    image_quality: entry.image_quality,
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
  void baseUrl;
  if (provider === "anthropic") return "anthropic";
  if (provider === "openai") return wireApi === "responses" ? "responses" : "chat";
  return wireApi;
};

export const canChooseApiFormat = (provider: ProviderId, baseUrl: string): boolean => {
  void baseUrl;
  return provider !== "anthropic";
};

export const defaultPromptCacheRetention = (wireApi: string): string => {
  void wireApi;
  return "";
};

export const promptCacheRetentionAfterWireChange = (
  current: string,
  nextWireApi: string,
): string => {
  void nextWireApi;
  // Retention is an independent Responses preference. The save boundary
  // already omits it for other wire APIs, so switching the selector must not
  // erase a value the user may return to before saving.
  return current;
};

export const formatProviderError = (error: unknown): string => {
  const raw = error instanceof Error ? error.message : String(error);
  const text = raw.replace(/^Error:\s*/i, "").trim();
  if (/your request was blocked/i.test(text)) {
    return "网关阻止了请求，请检查网关白名单、Base URL、API 格式和所选模型。";
  }
  return text || "请检查密钥、URL、API 格式和模型";
};

// ── Shared small components ────────────────────────────────────────────

export const Section = ({ title, description, children }: { title: string; description?: string; children?: React.ReactNode }) => (
  <section className="settings-section">
    <div className="settings-section-heading">
      <div className="settings-section-title">{title}</div>
      {description && <p className="settings-section-description">{description}</p>}
    </div>
    {children}
  </section>
);

// ── Styles ─────────────────────────────────────────────────────────────

export const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, backgroundColor: "var(--backdrop-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: "var(--z-modal)", pointerEvents: "auto" };
export const modalStyle: React.CSSProperties = { width: "min(920px, 94vw)", height: "min(720px, 88vh)", backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-strong-overlay)", overflow: "hidden", display: "flex", flexDirection: "column", pointerEvents: "auto" };
export const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)", backgroundColor: "var(--surface-base)" };
export const contentStyle: React.CSSProperties = { padding: "18px 22px", overflow: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 18, minWidth: 0, backgroundColor: "var(--surface-base)" };
export const closeBtn: React.CSSProperties = { backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--mc-control-radius)", color: "var(--text-muted)", width: 36, height: 36, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center", padding: 0 };
export const inputStyle: React.CSSProperties = { width: "100%", minHeight: 38, backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: "8px 11px", color: "var(--text-primary)", fontSize: "var(--mc-font-body)", fontFamily: "var(--font-ui)", outline: "none", boxSizing: "border-box", transition: "border-color var(--transition-fast), background-color var(--transition-fast)" };
export const primaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 40, borderRadius: "var(--radius-md)", fontWeight: "var(--fw-semibold)", cursor: "pointer", fontSize: "var(--mc-font-body)", backgroundColor: "var(--accent-primary)", color: "var(--text-on-accent, var(--text-primary))", border: 0, transition: "opacity var(--transition-fast)" };
export const secondaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 40, borderRadius: "var(--radius-md)", fontWeight: "var(--fw-semibold)", cursor: "pointer", fontSize: "var(--mc-font-body)", backgroundColor: "var(--surface-base)", color: "var(--text-secondary)", border: "1px solid var(--border-subtle)", transition: "background-color var(--transition-fast)" };
export const preStyle: React.CSSProperties = { margin: 0, padding: 12, backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", fontSize: "var(--mc-font-caption)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)", overflow: "auto", maxHeight: 200 };

export const actionBarStyle: React.CSSProperties = { position: "sticky", bottom: -18, display: "flex", justifyContent: "flex-end", gap: 8, padding: "12px 0 0", backgroundColor: "var(--surface-base)" };
export const statusStyle = (status: "idle" | "testing" | "success" | "warning" | "error"): React.CSSProperties => ({ padding: "9px 12px", borderRadius: "var(--radius-sm, 8px)", backgroundColor: status === "success" ? "var(--state-success-soft)" : status === "warning" ? "var(--state-warning-soft)" : status === "error" ? "var(--state-danger-soft)" : "var(--surface-soft)", border: "1px solid var(--border-subtle)", color: status === "error" ? "var(--state-danger)" : status === "warning" ? "var(--state-warning)" : status === "success" ? "var(--state-success)" : "var(--text-secondary)", fontSize: "var(--mc-font-secondary)" });
export const providerCheckPanelStyle: React.CSSProperties = { display: "grid", gap: 8, color: "var(--text-secondary)" };
export const providerCheckTitleStyle: React.CSSProperties = { fontWeight: "var(--fw-bold)", color: "inherit" };
export const providerCheckGridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "72px minmax(0, 1fr)", gap: "4px 10px", color: "var(--text-muted)" };
export const providerCheckHintStyle: React.CSSProperties = { color: "var(--state-danger)", lineHeight: 1.45 };

export const subTabBarStyle: React.CSSProperties = { width: "fit-content", display: "inline-flex", alignSelf: "start", gap: 2, padding: 3, backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: 12 };
export const subTabStyle = (active: boolean): React.CSSProperties => ({ display: "inline-flex", alignItems: "center", gap: 7, height: 38, padding: "0 13px", border: 0, borderRadius: "var(--radius-sm, 8px)", backgroundColor: active ? "var(--surface-base)" : "transparent", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: "var(--mc-font-secondary)", fontWeight: "var(--fw-semibold)", boxShadow: active ? "0 1px 3px color-mix(in oklch, black 7%, transparent)" : "none" });
export const subTabCountStyle: React.CSSProperties = { color: "var(--text-muted)", fontSize: "var(--mc-font-caption)", fontFamily: "var(--font-ui)", fontVariantNumeric: "tabular-nums" };
export const emptyInlineStyle: React.CSSProperties = { padding: "12px 0", color: "var(--text-muted)", fontSize: "var(--text-sm)" };
export const mcpServerRowStyle: React.CSSProperties = { minHeight: 62, padding: "9px 12px", backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: 8 };
export const mcpNameStyle: React.CSSProperties = { flexShrink: 1, minWidth: 0, fontWeight: "var(--fw-semibold)", fontSize: "var(--text-sm)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const mcpErrorStyle: React.CSSProperties = { fontSize: "var(--mc-font-secondary)", color: "var(--state-danger)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const mcpTone = (state: string): string => {
  if (state === "connected") return "var(--state-success)";
  if (state === "error" || state === "failed" || state === "expired") return "var(--state-danger)";
  if (state === "auth_required" || state === "starting" || state === "connecting" || state === "reconnecting") return "var(--state-warning)";
  return "var(--text-muted)";
};
export const mcpDotStyle = (status: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, backgroundColor: mcpTone(status) });
export const statusChipStyle = (status: string): React.CSSProperties => ({ flexShrink: 0, padding: "4px 8px", borderRadius: "999px", border: "1px solid var(--border-subtle)", color: mcpTone(status), fontSize: "var(--mc-font-caption)", fontWeight: "var(--fw-bold)", textTransform: "uppercase" });
export const miniMetaStyle: React.CSSProperties = { flexShrink: 0, color: "var(--text-muted)", fontSize: "var(--mc-font-caption)", fontFamily: "var(--font-ui)" };
export const mcpActionBtnStyle: React.CSSProperties = { backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: 9, width: 32, height: 32, cursor: "pointer", color: "var(--text-muted)", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 };
