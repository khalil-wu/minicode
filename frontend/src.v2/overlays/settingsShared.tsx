import React from "react";
import type { EffortLevel } from "../stores/types";

// ── Types ──────────────────────────────────────────────────────────────

export type ProviderId = "anthropic" | "openai" | "custom";
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
  { id: "anthropic", label: "Anthropic", placeholder: "sk-ant-...", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
  { id: "openai", label: "OpenAI", placeholder: "sk-...", hasBaseUrl: true, defaultUrl: "https://api.openai.com/v1", defaultModel: "" },
  { id: "custom", label: "自定义", placeholder: "API 密钥", hasBaseUrl: true, defaultUrl: "", defaultModel: "" },
] as const;

export const EFFORT_LEVELS: { id: EffortLevel; label: string; desc: string }[] = [
  { id: "none", label: "关闭", desc: "不使用额外推理" },
  { id: "minimal", label: "最低", desc: "最低推理强度" },
  { id: "low", label: "低", desc: "较低推理强度" },
  { id: "medium", label: "中", desc: "中等推理强度" },
  { id: "high", label: "高", desc: "较高推理强度" },
  { id: "xhigh", label: "极高", desc: "极高推理强度" },
  { id: "max", label: "最高", desc: "最高推理强度" },
];

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
    model: cfg.defaultModel,
    available_models: cfg.defaultModel ? [cfg.defaultModel] : [],
    wire_api: provider === "anthropic" ? "anthropic" : provider === "openai" ? "responses" : "chat",
    thinking_budget: 0,
    responses_reasoning_summary: "off",
    responses_stateful_continuation: false,
    prompt_cache_retention: "",
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
  void baseUrl;
  if (provider === "anthropic") return "anthropic";
  if (provider === "openai") return wireApi === "responses" ? "responses" : "chat";
  return wireApi;
};

export const canChooseApiFormat = (provider: ProviderId, baseUrl: string): boolean => {
  void baseUrl;
  return provider !== "anthropic";
};

export const defaultResponsesStatefulContinuation = (wireApi: string): boolean => {
  void wireApi;
  return false;
};

export const defaultPromptCacheRetention = (wireApi: string): string => {
  void wireApi;
  return "";
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
    return "网关阻止了请求，请检查网关白名单、Base URL、API 格式和所选模型。";
  }
  return text || "请检查密钥、URL、API 格式和模型";
};

export const formatProviderCheckSummary = (result: LLMCheckResult): string => {
  if (result.ok) return `\u751F\u6210\u6D4B\u8BD5\u901A\u8FC7 \u00B7 ${result.provider_id} \u00B7 ${result.model || "\u672A\u9009\u62E9\u6A21\u578B"}`;
  const status = result.status_code ? `HTTP ${result.status_code}` : "\u672A\u901A\u8FC7";
  return `${status} \u00B7 ${result.provider_id} \u00B7 ${result.hint || result.message || "\u8BF7\u68C0\u67E5 key\u3001URL \u548C\u6A21\u578B\u662F\u5426\u5339\u914D"}`;
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

export const SettingRow = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
    <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", width: 86 }}>{label}</span>
    {children}
  </div>
);

export const ProviderCheckPanel = ({ result }: { result: LLMCheckResult }) => (
  <div style={providerCheckPanelStyle}>
    <div style={providerCheckTitleStyle}>{result.ok ? "\u8FDE\u63A5\u4E0E\u751F\u6210\u6D4B\u8BD5\u901A\u8FC7" : "\u8FDE\u63A5\u6216\u751F\u6210\u6D4B\u8BD5\u5931\u8D25"}</div>
    <div style={providerCheckGridStyle}>
      <span>提供商</span><code>{result.provider_id || result.provider}</code>
      <span>接口地址</span><code>{result.base_url || "\u672A\u8BBE\u7F6E"}</code>
      <span>模型</span><code>{result.model || "\u672A\u8BBE\u7F6E"}</code>
      <span>密钥</span><code>{result.has_api_key ? "\u5DF2\u914D\u7F6E" : "\u672A\u914D\u7F6E"}</code>
      {result.status_code != null && <><span>状态</span><code>{result.status_code}</code></>}
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
export const inputStyle: React.CSSProperties = { width: "100%", minHeight: 38, backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: 10, padding: "8px 11px", color: "var(--text-primary)", fontSize: 13, fontFamily: "var(--font-mono)", outline: "none", boxSizing: "border-box", transition: "border-color 150ms, box-shadow 150ms" };
export const selectInputStyle: React.CSSProperties = { ...inputStyle, cursor: "pointer", fontFamily: "var(--font-ui)", colorScheme: "light dark" };
export const hintLineStyle: React.CSSProperties = { marginTop: 6, color: "var(--text-muted)", fontSize: "var(--text-xs)", lineHeight: 1.4 };
export const primaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 36, borderRadius: 10, fontWeight: 620, cursor: "pointer", fontSize: 13, backgroundColor: "var(--accent-primary)", color: "var(--text-on-accent, var(--text-primary))", border: 0, transition: "opacity 150ms" };
export const secondaryActionStyle: React.CSSProperties = { padding: "0 16px", height: 36, borderRadius: 10, fontWeight: 600, cursor: "pointer", fontSize: 13, backgroundColor: "var(--surface-base)", color: "var(--text-secondary)", border: "1px solid var(--border-subtle)", transition: "background 150ms" };
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

export const subTabBarStyle: React.CSSProperties = { width: "fit-content", display: "inline-flex", alignSelf: "start", gap: 2, padding: 3, backgroundColor: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: 12 };
export const subTabStyle = (active: boolean): React.CSSProperties => ({ display: "inline-flex", alignItems: "center", gap: 7, height: 32, padding: "0 12px", border: 0, borderRadius: 9, backgroundColor: active ? "var(--surface-base)" : "transparent", color: active ? "var(--text-primary)" : "var(--text-secondary)", cursor: "pointer", fontSize: 13, fontWeight: 620, boxShadow: active ? "0 1px 3px color-mix(in oklch, black 7%, transparent)" : "none" });
export const subTabCountStyle: React.CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)" };
export const emptyInlineStyle: React.CSSProperties = { padding: "12px 0", color: "var(--text-muted)", fontSize: "var(--text-sm)" };
export const mcpServerRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 12, minHeight: 62, padding: "9px 12px", backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: 13 };
export const mcpNameStyle: React.CSSProperties = { flexShrink: 1, minWidth: 0, fontWeight: 650, fontSize: "var(--text-sm)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const mcpErrorStyle: React.CSSProperties = { fontSize: "var(--text-2xs)", color: "var(--state-danger)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const mcpTone = (state: string): string => {
  if (state === "connected") return "var(--state-success)";
  if (state === "error" || state === "failed" || state === "expired") return "var(--state-danger)";
  if (state === "auth_required" || state === "starting" || state === "connecting" || state === "reconnecting") return "var(--state-warning)";
  return "var(--text-muted)";
};
export const mcpDotStyle = (status: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, backgroundColor: mcpTone(status) });
export const statusChipStyle = (status: string): React.CSSProperties => ({ flexShrink: 0, padding: "1px 6px", borderRadius: "999px", border: "1px solid var(--border-subtle)", color: mcpTone(status), fontSize: "var(--text-3xs)", fontWeight: 700, textTransform: "uppercase" });
export const miniMetaStyle: React.CSSProperties = { flexShrink: 0, color: "var(--text-muted)", fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)" };
export const mcpActionBtnStyle: React.CSSProperties = { backgroundColor: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: 9, width: 32, height: 32, cursor: "pointer", color: "var(--text-muted)", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 };
export const marketplaceListStyle: React.CSSProperties = { display: "grid", gap: 0, border: "1px solid var(--border-subtle)", borderRadius: 16, overflow: "hidden", backgroundColor: "var(--surface-base)" };
export const marketplaceRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 12, minHeight: 70, padding: "11px 14px", backgroundColor: "var(--surface-base)", borderBottom: "1px solid var(--border-subtle)" };
export const marketplaceTitleStyle: React.CSSProperties = { fontSize: "var(--text-sm)", fontWeight: 650, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const marketplaceDescStyle: React.CSSProperties = { fontSize: "var(--text-xxs)", color: "var(--text-muted)", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const installedPillStyle: React.CSSProperties = { flexShrink: 0, padding: "3px 8px", borderRadius: "999px", color: "var(--state-success)", border: "1px solid color-mix(in oklch, var(--state-success) 35%, var(--border-subtle))", fontSize: "var(--text-2xs)", fontWeight: 650 };
export const compactInstallStyle: React.CSSProperties = { flexShrink: 0, height: 34, padding: "0 13px", borderRadius: 10, border: "1px solid var(--border-subtle)", backgroundColor: "var(--surface-base)", color: "var(--text-secondary)", fontSize: 13, fontWeight: 620, cursor: "pointer" };
