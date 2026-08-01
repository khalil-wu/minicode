import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Check, KeyRound, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { ModelBrandIcon } from "../components/ModelBrandIcon";
import {
  type ProviderId,
  type CustomWireApi,
  type LLMSettingsPayload,
  type LLMCheckResult,
  type LLMModelsRefreshResult,
  type ProviderHistoryEntry,
  type ProviderSection,
  PROVIDERS,
  Section,
  ProviderCheckPanel,
  backendProvider,
  defaultSectionForProvider,
  savedOrHistorySectionForUiProvider,
  sectionFromHistoryEntry,
  buildModelChoices,
  effectiveCustomWireApi,
  canChooseApiFormat,
  defaultPromptCacheRetention,
  defaultResponsesStatefulContinuation,
  providerDisplayName,
  formatProviderError,
  formatProviderCheckSummary,
  inputStyle,
  selectInputStyle,
  choiceStyle,
  statusStyle,
  actionBarStyle,
  primaryActionStyle,
  secondaryActionStyle,
} from "./settingsShared";

const cardIdentityForDraft = (provider: ProviderId, section: Pick<ProviderSection, "base_url">): string => {
  const endpoint = String(section.base_url || "").trim().toLowerCase().replace(/\/+$/, "");
  return endpoint ? `endpoint::${endpoint}` : `provider::${provider}`;
};

type ProviderDraft = {
  provider: ProviderId;
  displayName: string;
  apiKey: string;
  baseUrl: string;
  modelName: string;
  availableModelList: string[];
  customWireApi: CustomWireApi;
  thinkingBudget: number;
  responsesReasoningSummary: string;
  responsesStatefulContinuation: boolean;
  promptCacheRetention: string;
};

type ProviderCard = {
  key: string;
  provider: ProviderId;
  title: string;
  subtitle: string;
  model: string;
  wireApi: string;
  hasApiKey: boolean;
  section: ProviderSection;
  entry?: ProviderHistoryEntry;
  historyIndex?: number;
  source: "saved" | "history" | "preset";
};

export const ProviderTab = ({
  selectedProvider,
  settingsPayload,
  settingsPayloadRef,
  onProviderChange,
  onSettingsPayloadChange,
}: {
  selectedProvider: ProviderId;
  settingsPayload: LLMSettingsPayload | null;
  settingsPayloadRef: React.MutableRefObject<LLMSettingsPayload | null>;
  onProviderChange: (id: ProviderId) => void;
  onSettingsPayloadChange?: (payload: LLMSettingsPayload) => void;
}) => {
  const [provider, setProvider] = useState<ProviderId>(selectedProvider);
  const [displayName, setDisplayName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [hasStoredApiKey, setHasStoredApiKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [availableModelList, setAvailableModelList] = useState<string[]>([]);
  const [customWireApi, setCustomWireApi] = useState<CustomWireApi>("chat");
  const [thinkingBudget, setThinkingBudget] = useState(0);
  const [responsesReasoningSummary, setResponsesReasoningSummary] = useState("auto");
  const [responsesStatefulContinuation, setResponsesStatefulContinuation] = useState(true);
  const [promptCacheRetention, setPromptCacheRetention] = useState("24h");
  const [saving, setSaving] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [connectionResult, setConnectionResult] = useState<LLMCheckResult | null>(null);
  const [modelsStatus, setModelsStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [modelsResult, setModelsResult] = useState<LLMModelsRefreshResult | null>(null);
  const [modelsSource, setModelsSource] = useState("");
  const [deletingHistoryKey, setDeletingHistoryKey] = useState("");
  const [pendingDeleteKey, setPendingDeleteKey] = useState("");
  const [providerView, setProviderView] = useState<"list" | "detail">("list");
  const [activeIdentityOverride, setActiveIdentityOverride] = useState("");
  const detailDraftPinnedRef = useRef(false);

  const providerConfig = PROVIDERS.find((p) => p.id === provider)!;
  const effectiveWireApi = effectiveCustomWireApi(provider, baseUrl, customWireApi);
  const responsesFastPathEnabled = effectiveWireApi === "responses";
  const showApiFormat = canChooseApiFormat(provider, baseUrl);
  const showFixedAnthropicFormat = backendProvider(provider) === "anthropic";
  const modelChoices = buildModelChoices(availableModelList, modelName);

  const draftFromState = (): ProviderDraft => ({
    provider,
    displayName,
    apiKey,
    baseUrl,
    modelName,
    availableModelList,
    customWireApi,
    thinkingBudget,
    responsesReasoningSummary,
    responsesStatefulContinuation,
    promptCacheRetention,
  });

  const applyProviderSection = (nextProvider: ProviderId, section?: ProviderSection) => {
    const fallback = defaultSectionForProvider(nextProvider);
    setDisplayName(providerDisplayName(section));
    setApiKey(section?.api_key ?? "");
    setHasStoredApiKey(Boolean(section?.has_api_key));
    setBaseUrl(section?.base_url ?? fallback.base_url ?? "");
    setModelName(section?.model ?? fallback.model ?? "");
    const savedModels = section?.available_models ?? fallback.available_models ?? [];
    const models = buildModelChoices(savedModels, section?.model ?? fallback.model ?? "");
    setAvailableModelList(models);
    setModelsSource(section?.models_source ?? "");
    if (backendProvider(nextProvider) === "anthropic") {
      setCustomWireApi("anthropic");
    } else {
      const wire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
      setCustomWireApi(effectiveCustomWireApi(nextProvider, section?.base_url ?? fallback.base_url ?? "", wire as CustomWireApi));
    }
    setThinkingBudget(Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0);
    setResponsesReasoningSummary(section?.responses_reasoning_summary ?? fallback.responses_reasoning_summary ?? "auto");
    const rawWire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
    const effectiveWire = effectiveCustomWireApi(nextProvider, section?.base_url ?? fallback.base_url ?? "", rawWire as CustomWireApi);
    setResponsesStatefulContinuation(Boolean(section?.responses_stateful_continuation ?? defaultResponsesStatefulContinuation(effectiveWire)));
    setPromptCacheRetention(section?.prompt_cache_retention ?? defaultPromptCacheRetention(effectiveWire));
  };

  const pinDetailDraft = () => {
    detailDraftPinnedRef.current = true;
  };

  const clearDetailDraftPin = () => {
    detailDraftPinnedRef.current = false;
  };

  const openProviderList = () => {
    clearDetailDraftPin();
    setProviderView("list");
  };

  useEffect(() => {
    if (detailDraftPinnedRef.current) return;
    setProvider(selectedProvider);
    applyProviderSection(selectedProvider, savedOrHistorySectionForUiProvider(settingsPayload, selectedProvider));
  }, [selectedProvider, settingsPayload]);

  const selectProviderPreset = (id: ProviderId) => {
    pinDetailDraft();
    setProvider(id);
    applyProviderSection(id, defaultSectionForProvider(id));
    setDisplayName("");
    setApiKey("");
    setHasStoredApiKey(false);
    setConnectionStatus("idle");
    setConnectionResult(null);
        setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
    setPendingDeleteKey("");
    onProviderChange(id);
  };

  const addProvider = () => {
    setProvider("custom");
    applyProviderSection("custom", {
      display_name: "",
      base_url: "",
      model: "",
      available_models: [],
      wire_api: "chat",
      thinking_budget: 0,
      responses_reasoning_summary: "auto",
      responses_stateful_continuation: false,
      prompt_cache_retention: "",
    });
    setDisplayName("");
    setApiKey("");
    setHasStoredApiKey(false);
        setConnectionStatus("idle");
    setConnectionResult(null);
    setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
    setPendingDeleteKey("");
    pinDetailDraft();
    setProviderView("detail");
    onProviderChange("custom");
  };

  const providerForHistoryEntry = (entry: ProviderHistoryEntry): ProviderId => {
    const rawProvider = String(entry.provider || "").trim().toLowerCase();
    const providerId = String(entry.provider_id || "").trim().toLowerCase();
    if (rawProvider === "anthropic" || providerId === "anthropic") return "anthropic";
    if (rawProvider === "openai" || providerId === "openai") return "openai";
    return "custom";
  };

  const historyEntryKey = (entry: ProviderHistoryEntry, index = 0) =>
    [
      entry.provider || backendProvider(provider),
      entry.provider_id || "",
      entry.base_url || "",
      entry.wire_api || "",
      entry.model || "",
      index,
    ].join("::");

  const deleteHistoryEntry = async (entry: ProviderHistoryEntry, index: number) => {
    const key = historyEntryKey(entry, index);
    if (pendingDeleteKey !== key) {
      setPendingDeleteKey(key);
      return;
    }
    setDeletingHistoryKey(key);
    try {
      const res = await fetch(`${apiBase()}/api/llm/provider-history`, {
        method: "DELETE",
        headers: jsonAuthHeaders(),
        body: JSON.stringify({
          confirm_sensitive_change: true,
          provider: entry.provider || backendProvider(provider),
          provider_id: entry.provider_id || "",
          base_url: entry.base_url || "",
          model: entry.model || "",
          wire_api: entry.wire_api || "",
          clear_api_key: true,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const savedPayload = await res.json() as LLMSettingsPayload;
      settingsPayloadRef.current = savedPayload;
      onSettingsPayloadChange?.(savedPayload);
      applyProviderSection(provider, savedOrHistorySectionForUiProvider(savedPayload, provider));
      setPendingDeleteKey("");
      pushToast("提供商配置已删除", "success");
    } catch (error) {
      pushToast(`提供商删除失败：${formatProviderError(error)}`, "error");
    } finally {
      setDeletingHistoryKey("");
    }
  };

  const payload = (draft = draftFromState(), options: { activate?: boolean; includeProvider?: boolean } = {}) => {
    const bp = backendProvider(draft.provider);
    const wireApi = effectiveCustomWireApi(draft.provider, draft.baseUrl, draft.customWireApi);
    const section = {
      display_name: draft.displayName.trim(),
      ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
      base_url: draft.baseUrl,
      model: draft.modelName,
            available_models: buildModelChoices(draft.availableModelList, draft.modelName),
      models_source: modelsSource || undefined,
      thinking_budget: backendProvider(draft.provider) === "anthropic" || wireApi === "anthropic" ? draft.thinkingBudget : undefined,
      wire_api: bp === "openai" || bp === "custom" ? wireApi : undefined,
      responses_reasoning_summary: bp === "openai" || wireApi === "responses" ? draft.responsesReasoningSummary : undefined,
      responses_stateful_continuation: wireApi === "responses" ? draft.responsesStatefulContinuation : undefined,
      prompt_cache_retention: wireApi === "responses" ? draft.promptCacheRetention : undefined,
    };
    return {
      confirm_sensitive_change: true,
      ...(options.activate || options.includeProvider ? { provider: bp } : {}),
      openai: bp === "openai" ? section : {},
      anthropic: bp === "anthropic" ? section : {},
      custom: bp === "custom" ? section : {},
    };
  };

  const jsonAuthHeaders = (): HeadersInit => {
    try {
      return authHeaders({ "content-type": "application/json" });
    } catch {
      return { "content-type": "application/json" };
    }
  };

  const saveProvider = async (draft = draftFromState(), options: { activate?: boolean; quiet?: boolean } = {}) => {
    setSaving(true);
    setConnectionStatus("testing");
    setConnectionResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/llm/settings`, {
        method: "PUT",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload(draft, { activate: options.activate })),
      });
      if (!res.ok) throw new Error(await res.text());
      const saved = await res.json();
      const savedPayload = saved as LLMSettingsPayload;
      settingsPayloadRef.current = savedPayload;
      onSettingsPayloadChange?.(savedPayload);
      const bp = backendProvider(draft.provider);
      const section = savedPayload[bp] as ProviderSection | undefined;
      setProvider(draft.provider);
      onProviderChange(draft.provider);
      setDisplayName(providerDisplayName(section) || draft.displayName);
      // Settings responses never echo secrets. A successful replacement is
      // immediately cleared from component state after it reaches the vault.
      setApiKey("");
      setHasStoredApiKey(Boolean(section?.has_api_key || draft.apiKey.trim()));
      setBaseUrl(section?.base_url ?? draft.baseUrl);
      const active = String(section?.model || draft.modelName || saved.active_model || "");
      if (active) {
        setModelName(active);
      }
            if (section?.available_models) {
        setAvailableModelList(section.available_models);
      }
      setModelsSource(section?.models_source ?? "");
      setThinkingBudget(Number(section?.thinking_budget ?? draft.thinkingBudget) || 0);
      setResponsesReasoningSummary(section?.responses_reasoning_summary ?? draft.responsesReasoningSummary);
      setResponsesStatefulContinuation(Boolean(section?.responses_stateful_continuation ?? draft.responsesStatefulContinuation));
      setPromptCacheRetention(section?.prompt_cache_retention ?? draft.promptCacheRetention);
      const appliedWireApi = bp === "openai" || bp === "custom"
        ? effectiveCustomWireApi(draft.provider, section?.base_url || draft.baseUrl, ((section?.wire_api as CustomWireApi | undefined) || effectiveCustomWireApi(draft.provider, draft.baseUrl, draft.customWireApi)))
        : undefined;
      if (appliedWireApi) setCustomWireApi(appliedWireApi);
            if (options.activate) {
        setActiveIdentityOverride(cardIdentityForDraft(draft.provider, {
          base_url: section?.base_url || draft.baseUrl,
        }));
        sendClientCommand({
          type: "llm.config.set",
          provider: bp,
          source: "settings.provider.activate",
        }, { silent: true });
      } else {
        sendClientCommand({
          type: "llm.config.set",
          provider: bp,
          source: "settings.provider.save",
        }, { silent: true });
      }
      setConnectionStatus("idle");
      if (!options.quiet) {
        pushToast(
          options.activate
            ? `提供商已启用 · 模型：${active || draft.modelName}`
            : `提供商已保存 · 模型：${active || draft.modelName}`,
          "success",
        );
      }
    } catch (error) {
      setConnectionStatus("error");
      pushToast(`提供商保存失败：${String(error)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const draftFromSection = (nextProvider: ProviderId, section?: ProviderSection): ProviderDraft => {
    const fallback = defaultSectionForProvider(nextProvider);
    const nextBaseUrl = section?.base_url ?? fallback.base_url ?? "";
    const nextModel = section?.model ?? fallback.model ?? "";
    const savedModels = section?.available_models ?? fallback.available_models ?? [];
    const nextModels = buildModelChoices(savedModels, nextModel);
    const wire = backendProvider(nextProvider) === "anthropic"
      ? "anthropic"
      : effectiveCustomWireApi(
        nextProvider,
        nextBaseUrl,
        (section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat") as CustomWireApi,
      );
    return {
      provider: nextProvider,
      displayName: providerDisplayName(section),
      apiKey: section?.api_key ?? "",
      baseUrl: nextBaseUrl,
      modelName: nextModel,
      availableModelList: nextModels,
      customWireApi: wire,
      thinkingBudget: Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0,
      responsesReasoningSummary: section?.responses_reasoning_summary ?? fallback.responses_reasoning_summary ?? "auto",
      responsesStatefulContinuation: Boolean(section?.responses_stateful_continuation ?? defaultResponsesStatefulContinuation(wire)),
      promptCacheRetention: section?.prompt_cache_retention ?? defaultPromptCacheRetention(wire),
    };
  };

  const editProviderCard = (card: ProviderCard) => {
    pinDetailDraft();
    setProvider(card.provider);
    applyProviderSection(card.provider, card.section);
    setConnectionStatus("idle");
    setConnectionResult(null);
        setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
    setPendingDeleteKey("");
    setProviderView("detail");
    onProviderChange(card.provider);
  };

  const useProviderCard = async (card: ProviderCard) => {
    clearDetailDraftPin();
    setProvider(card.provider);
    applyProviderSection(card.provider, card.section);
    setPendingDeleteKey("");
    await saveProvider(draftFromSection(card.provider, card.section), { activate: true });
  };

  const testConnection = async () => {
    setConnectionStatus("testing");
    setConnectionResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/llm/check`, {
        method: "POST",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload(undefined, { includeProvider: true })),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as LLMCheckResult;
      setConnectionResult(data);
      if (data.models?.length) {
        setAvailableModelList(data.models);
        const nextModel = data.model || (modelName ? "" : data.models[0]);
        if (nextModel) {
          setModelName(nextModel);
        }
      }
      setConnectionStatus(data.ok ? "success" : "error");
      pushToast(formatProviderCheckSummary(data), data.ok ? "success" : "error");
    } catch (error) {
      setConnectionStatus("error");
      pushToast(`连接失败：${formatProviderError(error)}`, "error");
    }
  };

  const discoverModels = async () => {
    setModelsStatus("loading");
    setModelsResult(null);
    try {
      const res = await fetch(`${apiBase()}/api/llm/models/refresh`, {
        method: "POST",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload(undefined, { includeProvider: true })),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json() as LLMModelsRefreshResult;
      setModelsResult(data);
            if (data.source === "live") {
        const nextModel = data.selected_model || modelName || data.models[0] || "";
        const models = buildModelChoices(data.models ?? [], nextModel);
        setAvailableModelList(models);
        setModelsSource("live");
        if (nextModel) {
          setModelName(nextModel);
        }
        setModelsStatus("success");
        pushToast(`已发现 ${models.length} 个模型`, "success");
      } else {
        setModelsStatus("error");
        setModelsSource("");
        pushToast("实时发现未返回模型列表，已保留手动输入的模型。", "info");
      }
    } catch (error) {
      setModelsStatus("error");
      pushToast(`模型发现失败：${formatProviderError(error)}`, "error");
    }
  };

  const historyLabel = (entry: ProviderHistoryEntry) => {
    const name = providerDisplayName(entry);
    if (name) return name;
    try {
      const url = new URL(String(entry.base_url || ""));
      return url.host || entry.base_url || entry.model || "已保存配置";
    } catch {
      return entry.base_url || entry.model || "已保存配置";
    }
  };

  const hostLabel = (value: string) => {
    try {
      const url = new URL(value);
      return url.host || value;
    } catch {
      return value || "未配置接口";
    }
  };

  const providerTypeLabel = (nextProvider: ProviderId) =>
    nextProvider === "custom"
      ? "自定义网关"
      : PROVIDERS.find((item) => item.id === nextProvider)?.label ?? "提供商";

  const wireApiLabel = (wireApi: string) => {
    const normalized = wireApi.trim().toLowerCase();
    if (normalized === "responses") return "Responses";
    if (normalized === "anthropic") return "Anthropic Messages";
    return "Chat Completions";
  };

  const updateWireApi = (next: CustomWireApi) => {
    const effectiveNext = effectiveCustomWireApi(provider, baseUrl, next);
    setCustomWireApi(effectiveNext);
    setResponsesStatefulContinuation(defaultResponsesStatefulContinuation(effectiveNext));
    setPromptCacheRetention(defaultPromptCacheRetention(effectiveNext));
  };

  const fallbackCardTitle = (nextProvider: ProviderId, section: ProviderSection) => {
    if (nextProvider === "custom") {
      const host = section.base_url ? hostLabel(section.base_url) : "";
      return host && host !== "未配置接口" ? host : "自定义网关";
    }
    return providerTypeLabel(nextProvider);
  };

  const cardTitle = (nextProvider: ProviderId, section: ProviderSection, entry?: ProviderHistoryEntry) =>
    providerDisplayName(section) || providerDisplayName(entry) || fallbackCardTitle(nextProvider, section);

  const draftTitle = displayName.trim() || fallbackCardTitle(provider, { base_url: baseUrl });

  const cardKeyFor = (nextProvider: ProviderId, section: ProviderSection, suffix: string) =>
    `${cardIdentityForDraft(nextProvider, section)}::${suffix}`;

  const providerCards = (() => {
    const cards: ProviderCard[] = [];
    const seen = new Set<string>();
    const history = settingsPayload?.provider_history ?? [];
    history.forEach((entry, index) => {
      const entryProvider = providerForHistoryEntry(entry);
      const section = sectionFromHistoryEntry(entry) ?? defaultSectionForProvider(entryProvider);
      const identity = cardKeyFor(entryProvider, section, "saved").replace(/::saved$/, "");
      if (seen.has(identity)) return;
      seen.add(identity);
      cards.push({
        key: cardKeyFor(entryProvider, section, `history-${index}`),
        provider: entryProvider,
        title: historyLabel(entry),
        subtitle: providerTypeLabel(entryProvider),
        model: section.model || "选择模型",
        wireApi: effectiveCustomWireApi(
          entryProvider,
          section.base_url || "",
          (section.wire_api === "responses" || section.wire_api === "anthropic" ? section.wire_api : "chat") as CustomWireApi,
        ),
        hasApiKey: Boolean(entry.has_api_key || entry.api_key),
        section,
        entry,
        historyIndex: index,
        source: "history",
      });
    });
    return cards;
  })();

  const activeCardIdentity = (() => {
    const activeProvider = toActiveUiProvider(settingsPayload);
    const activeSection = activeProvider ? savedOrHistorySectionForUiProvider(settingsPayload, activeProvider) : undefined;
    return activeIdentityOverride || (activeProvider && activeSection ? cardKeyFor(activeProvider, activeSection, "saved").replace(/::saved$/, "") : "");
  })();
  const draftCardIdentity = cardIdentityForDraft(provider, {
    base_url: baseUrl,
  });

  const providerList = (
    <>
      <div style={providerListHeaderStyle}>
        <div style={{ minWidth: 0 }}>
          <div style={providerListTitleStyle}>模型提供商</div>
        </div>
        <button type="button" onClick={addProvider} style={addProviderButtonStyle}>
          <Plus size={14} />
          <span>添加提供商</span>
        </button>
      </div>

      {providerCards.length > 0 ? (
        <div style={providerCardListStyle}>
        {providerCards.map((card) => {
          const cardIdentity = card.key.replace(/::(?:saved|history-\d+)$/, "");
          const active = activeCardIdentity === cardIdentity;
          const editing = draftCardIdentity === cardIdentity;
          return (
            <div key={card.key} style={providerCardStyle(active, editing)}>
              <button
                type="button"
                onClick={() => editProviderCard(card)}
                style={providerCardMainStyle}
                title={[card.subtitle, card.section.base_url, card.model, wireApiLabel(card.wireApi)].filter(Boolean).join(" · ")}
              >
                <ModelBrandIcon
                  model={card.model}
                  provider={`${card.provider} ${card.title} ${card.section.base_url || ""}`}
                  websiteUrl={card.section.base_url}
                  size={21}
                  framed
                />
                <span style={{ minWidth: 0, display: "grid", gap: 3 }}>
                  <span style={providerCardTitleStyle}>{card.title}</span>
                  <span style={providerCardUrlStyle}>{card.section.base_url || "未配置接口地址"}</span>
                  <span style={providerCardMetaStyle}>
                    <span style={metaTextStyle}>{card.subtitle}</span>
                    <span style={dotStyle}>·</span>
                    <span style={metaTextStyle}>{wireApiLabel(card.wireApi)}</span>
                    <span style={dotStyle}>·</span>
                    {card.model}
                    <span style={dotStyle}>·</span>
                    <KeyRound size={14} />
                    {card.hasApiKey ? "已配置密钥" : "无密钥"}
                  </span>
                </span>
              </button>
              <div style={providerCardActionsStyle}>
                <button
                  type="button"
                  onClick={() => editProviderCard(card)}
                  style={iconActionStyle}
                  aria-label={`编辑 ${card.title}`}
                  title="编辑"
                >
                  <Pencil size={14} />
                </button>
                <button
                  type="button"
                  onClick={() => void useProviderCard(card)}
                  disabled={saving && editing}
                  style={useActionStyle(active)}
                  aria-label={active ? `${card.title} 已启用` : `使用 ${card.title}`}
                  title={active ? "已启用" : "使用提供商"}
                >
                  {active ? <Check size={14} /> : <Play size={14} />}
                  <span>{active ? "已启用" : "使用"}</span>
                </button>
                {card.entry && card.historyIndex != null && (
                  pendingDeleteKey === historyEntryKey(card.entry, card.historyIndex) ? (
                    <button
                      onClick={() => deleteHistoryEntry(card.entry!, card.historyIndex!)}
                      disabled={deletingHistoryKey === historyEntryKey(card.entry, card.historyIndex)}
                      style={historyDeleteConfirmStyle}
                    >
                      {deletingHistoryKey === historyEntryKey(card.entry, card.historyIndex) ? "正在删除…" : "删除"}
                    </button>
                  ) : (
                    <button
                      onClick={() => deleteHistoryEntry(card.entry!, card.historyIndex!)}
                      style={deleteIconActionStyle}
                      title="删除已保存的提供商"
                      aria-label={`删除已保存的提供商 ${card.title}`}
                    >
                      <Trash2 size={14} />
                    </button>
                  )
                )}
              </div>
            </div>
          );
        })}
        </div>
      ) : (
        <div style={emptyProviderListStyle}>
          <div style={emptyProviderTitleStyle}>尚未配置提供商</div>
          <div>添加提供商配置，以选择接口地址、API 密钥、模型和 API 格式。</div>
        </div>
      )}

      {connectionStatus !== "idle" && (
        <div style={statusStyle(connectionStatus)}>
          {connectionStatus === "testing" && "正在应用提供商…"}
          {connectionStatus === "success" && "提供商已就绪"}
          {connectionStatus === "error" && "提供商应用失败"}
        </div>
      )}
    </>
  );

  const providerDetails = (
    <>
      <div style={detailHeaderStyle}>
        <button type="button" onClick={openProviderList} style={backButtonStyle}>返回</button>
        <div style={{ minWidth: 0 }}>
          <div style={detailTitleStyle}>{draftTitle}</div>
        </div>
      </div>

      <Section title="显示名称">
        <input
          type="text"
          aria-label="提供商显示名称"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder={provider === "custom" && baseUrl ? hostLabel(baseUrl) : `${providerConfig.label} 配置`}
          spellCheck={false}
          style={{ ...inputStyle, fontFamily: "var(--font-ui)" }}
        />
      </Section>

      <Section title="提供商类型">
        <div style={presetGridStyle}>
          {PROVIDERS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => selectProviderPreset(item.id)}
              style={presetButtonStyle(provider === item.id)}
            >
              <ModelBrandIcon model={item.defaultModel} provider={item.id} size={19} framed />
              <span style={presetTitleStyle}>{item.label}</span>
            </button>
          ))}
        </div>
      </Section>

      <Section title="API 密钥">
        <input
          type="password"
          aria-label="API 密钥"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={hasStoredApiKey ? "已保存密钥；输入新密钥以替换" : providerConfig.placeholder}
          autoComplete="new-password"
          spellCheck={false}
          style={inputStyle}
        />
      </Section>

      {providerConfig.hasBaseUrl && (
        <Section title="接口地址">
          <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" style={inputStyle} />
        </Section>
      )}

      {showApiFormat && (
        <Section title="API 格式">
          <select
            aria-label="API 格式"
            value={customWireApi}
            onChange={(e) => updateWireApi(e.target.value as CustomWireApi)}
            style={selectInputStyle}
          >
            <option value="chat">OpenAI Chat Completions</option>
            <option value="responses">OpenAI Responses</option>
            {backendProvider(provider) === "custom" && <option value="anthropic">Anthropic Messages</option>}
          </select>
        </Section>
      )}

      {showFixedAnthropicFormat && (
        <Section title="API 格式">
          <div style={readOnlyFormatStyle}>Anthropic Messages</div>
        </Section>
      )}

      {responsesFastPathEnabled && (
        <Section title="Responses 快速路径">
          <label style={checkboxRowStyle}>
            <input
              type="checkbox"
              checked={responsesStatefulContinuation}
              onChange={(event) => setResponsesStatefulContinuation(event.target.checked)}
            />
            <span>有状态续接</span>
          </label>
          <SettingSelect
            label="提示词缓存"
            value={promptCacheRetention || "off"}
            onChange={(value) => setPromptCacheRetention(value === "off" ? "" : value)}
            options={[
              { value: "24h", label: "保留 24 小时" },
              { value: "in_memory", label: "仅内存" },
              { value: "off", label: "关闭" },
            ]}
          />
        </Section>
      )}

      {(backendProvider(provider) === "anthropic" || effectiveWireApi === "anthropic") && (
        <Section title="思考预算">
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

      <Section title="模型">
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
        {modelsStatus !== "idle" && (
          <div style={{ ...statusStyle(modelsStatus === "error" ? "error" : modelsStatus === "loading" ? "testing" : "success"), marginTop: 8 }}>
            {modelsStatus === "loading" && "正在发现模型…"}
            {modelsStatus === "success" && modelsResult && (
              <span>
                已加载实时模型列表
                {modelsResult.source_message ? ` · ${modelsResult.source_message}` : ""}
              </span>
            )}
            {modelsStatus === "error" && (
              <span>
                {modelsResult ? "未返回实时模型列表，已保留手动输入的模型" : "模型发现失败"}
                {modelsResult?.source_message ? ` · ${modelsResult.source_message}` : ""}
              </span>
            )}
          </div>
        )}
      </Section>

      {connectionStatus !== "idle" && (
        <div style={statusStyle(connectionStatus)}>
          {connectionStatus === "testing" && "\u6B63\u5728\u68C0\u67E5\u6A21\u578B\u9274\u6743..."}
          {connectionStatus !== "testing" && connectionResult && (
            <ProviderCheckPanel result={connectionResult} />
          )}
          {connectionStatus === "success" && !connectionResult && "提供商已就绪"}
          {connectionStatus === "error" && !connectionResult && "提供商检查失败"}
        </div>
      )}

      <div style={actionBarStyle}>
        <button onClick={discoverModels} disabled={modelsStatus === "loading"} style={secondaryActionStyle}>
          {modelsStatus === "loading" ? "正在发现…" : "发现模型"}
        </button>
        <button onClick={testConnection} disabled={connectionStatus === "testing"} style={secondaryActionStyle}>检查鉴权</button>
        <button onClick={() => void saveProvider()} disabled={saving} style={primaryActionStyle}>{saving ? "正在保存…" : "保存"}</button>
      </div>
    </>
  );

  return providerView === "list" ? providerList : providerDetails;
};

const toActiveUiProvider = (payload: LLMSettingsPayload | null): ProviderId | "" => {
  if (!payload) return "";
  const active = String(payload.provider || "").trim().toLowerCase();
  if (active === "anthropic") return "anthropic";
  if (active === "openai") return "openai";
  if (active === "custom") return "custom";
  return "";
};

const SettingSelect = ({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) => (
  <label style={settingSelectRowStyle}>
    <span style={settingSelectLabelStyle}>{label}</span>
    <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)} style={selectInputStyle}>
      {options.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
  </label>
);

const readOnlyFormatStyle = {
  ...inputStyle,
  fontFamily: "var(--font-ui)",
  color: "var(--text-secondary)",
};

const checkboxRowStyle: CSSProperties = {
  minHeight: 32,
  display: "flex",
  alignItems: "center",
  gap: 8,
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
};

const settingSelectRowStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "96px minmax(0, 1fr)",
  alignItems: "center",
  gap: 10,
};

const settingSelectLabelStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
};

const detailHeaderStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "auto minmax(0, 1fr)",
  alignItems: "center",
  gap: 12,
  paddingBottom: 12,
  borderBottom: "1px solid var(--border-subtle)",
};

const backButtonStyle: CSSProperties = {
  height: 30,
  padding: "0 10px",
  borderRadius: "var(--radius-sm, 6px)",
  border: "1px solid var(--border-subtle)",
  backgroundColor: "var(--surface-soft)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};

const detailTitleStyle: CSSProperties = {
  color: "var(--text-primary)",
  fontSize: 16,
  fontWeight: 750,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const presetGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
  gap: 8,
};

const presetButtonStyle = (active: boolean): CSSProperties => ({
  minHeight: 52,
  display: "grid",
  gridTemplateColumns: "30px minmax(0, 1fr)",
  alignItems: "center",
  gap: 9,
  padding: "8px 9px",
  borderRadius: "var(--radius-sm, 8px)",
  border: active ? "1px solid var(--accent-primary)" : "1px solid var(--border-subtle)",
  backgroundColor: active ? "var(--accent-soft)" : "var(--surface-soft)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  textAlign: "left",
});

const presetTitleStyle: CSSProperties = {
  display: "block",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const providerListHeaderStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) auto",
  alignItems: "center",
  gap: 12,
  paddingBottom: 2,
};

const providerListTitleStyle: CSSProperties = {
  color: "var(--text-primary)",
  fontSize: 17,
  fontWeight: 640,
};

const addProviderButtonStyle: CSSProperties = {
  height: 36,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 7,
  padding: "0 12px",
  borderRadius: 10,
  border: 0,
  backgroundColor: "var(--accent-primary)",
  color: "var(--text-on-accent, var(--text-primary))",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 620,
  whiteSpace: "nowrap",
};

const providerCardListStyle: CSSProperties = {
  display: "grid",
  gap: 0,
  overflow: "hidden",
  border: "1px solid var(--border-subtle)",
  borderRadius: 17,
  backgroundColor: "var(--surface-base)",
};

const emptyProviderListStyle: CSSProperties = {
  minHeight: 132,
  display: "grid",
  placeItems: "center",
  gap: 4,
  padding: "22px 16px",
  border: "1px dashed var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
  backgroundColor: "var(--surface-soft)",
  color: "var(--text-muted)",
  textAlign: "center",
  fontSize: "var(--text-xs)",
};

const emptyProviderTitleStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
};


const providerCardStyle = (active: boolean, editing: boolean): CSSProperties => ({
  minHeight: 82,
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) 186px",
  alignItems: "center",
  gap: 12,
  padding: "11px 14px",
  border: 0,
  borderBottom: "1px solid var(--border-subtle)",
  borderRadius: 0,
  backgroundColor: active
    ? "var(--surface-active)"
    : editing
      ? "var(--surface-hover)"
      : "var(--surface-base)",
  boxShadow: active ? "inset 3px 0 0 var(--accent-primary)" : "none",
});

const providerCardMainStyle: CSSProperties = {
  width: "100%",
  minHeight: 54,
  minWidth: 0,
  display: "grid",
  gridTemplateColumns: "40px minmax(0, 1fr)",
  alignItems: "center",
  gap: 10,
  border: 0,
  backgroundColor: "transparent",
  padding: 0,
  color: "inherit",
  textAlign: "left",
  cursor: "pointer",
};

const providerCardTitleStyle: CSSProperties = {
  color: "var(--text-primary)",
  fontSize: 15,
  fontWeight: 640,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const providerCardUrlStyle: CSSProperties = {
  color: "var(--text-muted)",
  fontSize: 13,
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const providerCardMetaStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
  color: "var(--text-muted)",
  fontSize: 12,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const metaTextStyle: CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const dotStyle: CSSProperties = {
  color: "var(--border-strong, var(--border-subtle))",
};

const providerCardActionsStyle: CSSProperties = {
  width: 186,
  display: "grid",
  gridTemplateColumns: "30px 76px 68px",
  alignItems: "center",
  justifyContent: "end",
  gap: 6,
  flexShrink: 0,
};

const iconActionStyle: CSSProperties = {
  width: 30,
  height: 30,
  borderRadius: 9,
  border: "1px solid var(--border-subtle)",
  backgroundColor: "var(--surface-base)",
  color: "var(--text-muted)",
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
};

const useActionStyle = (active: boolean): CSSProperties => ({
  width: 76,
  height: 30,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  padding: 0,
  borderRadius: 9,
  border: active ? "1px solid var(--accent-primary)" : 0,
  backgroundColor: active ? "var(--surface-base)" : "var(--accent-primary)",
  color: active ? "var(--accent-primary)" : "var(--text-on-accent, var(--text-primary))",
  cursor: active ? "default" : "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
});

const deleteIconActionStyle: CSSProperties = {
  ...iconActionStyle,
  justifySelf: "end",
};

const historyDeleteConfirmStyle: CSSProperties = {
  width: 68,
  height: 30,
  border: "1px solid color-mix(in oklch, var(--state-danger) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 6px)",
  backgroundColor: "rgba(239, 68, 68, 0.12)",
  color: "var(--state-danger)",
  padding: 0,
  fontSize: "var(--text-2xs)",
  fontWeight: 600,
  cursor: "pointer",
};
