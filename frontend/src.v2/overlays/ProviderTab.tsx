import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ArrowLeft, Check, ExternalLink, LoaderCircle, LogIn, LogOut, Pencil, Play, Plus, RefreshCw, Save, ShieldCheck, Trash2 } from "lucide-react";
import { pushToast } from "./ToastContainer";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ProviderOAuthCommand } from "../protocol/events";
import { ModelBrandIcon } from "../components/ModelBrandIcon";
import { SelectMenu } from "../components/SelectMenu";
import { showConfirm } from "./DialogService";
import type { EffortLevel } from "../stores/types";
import { useAppStore } from "../stores";
import { selectableModelsForProvider } from "../lib/provider-models";
import { isDesktop, openExternal } from "../desktop/runtime";
import {
  type ProviderId,
  type CustomWireApi,
  type LLMSettingsPayload,
  type LLMCheckResult,
  type LLMModelsRefreshResult,
  type ProviderHistoryEntry,
  type ProviderModelMetadata,
  type ProviderProxyMode,
  type ProviderSection,
  PROVIDERS,
  Section,
  backendProvider,
  defaultSectionForProvider,
  savedOrHistorySectionForUiProvider,
  sectionFromHistoryEntry,
  buildModelChoices,
  effectiveCustomWireApi,
  canChooseApiFormat,
  defaultPromptCacheRetention,
  promptCacheRetentionAfterWireChange,
  providerDisplayName,
  formatProviderError,
  inputStyle,
  statusStyle,
  actionBarStyle,
  primaryActionStyle,
  secondaryActionStyle,
} from "./settingsShared";

const credentialEndpointIdentity = (value: string): string => {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    const path = parsed.pathname.replace(/\/+$/, "");
    const scopedPath = path.toLowerCase() === "/v1" ? "" : path;
    return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${scopedPath}`;
  } catch {
    return raw.replace(/[?#].*$/, "").replace(/\/+$/, "").replace(/\/v1$/i, "").toLowerCase();
  }
};

const isDedicatedImageModel = (value: string): boolean =>
  String(value || "").trim().toLowerCase().split("/").pop()?.startsWith("gpt-image-") === true;

const DRAFT_MODEL_PREFIX = "__draft_model_";

const isDraftModelId = (value: string): boolean =>
  String(value || "").startsWith(DRAFT_MODEL_PREFIX);

const cardIdentityForDraft = (
  provider: ProviderId,
  section: Pick<ProviderSection, "base_url" | "wire_api">,
): string => {
  const endpoint = credentialEndpointIdentity(String(section.base_url || ""));
  return endpoint
    ? `${provider}::${endpoint}`
    : `provider::${provider}`;
};

const normalizeEffortLevels = (value: unknown): EffortLevel[] => {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value
    .map((item) => String(item || "").trim().toLowerCase())
    .filter((item): item is EffortLevel => Boolean(item))));
};

type ProviderDraft = {
  provider: ProviderId;
  displayName: string;
  apiKey: string;
  headers: Record<string, string>;
  authHeader: boolean;
  baseUrl: string;
  modelName: string;
  smallFastModel: string;
  availableModelList: string[];
  modelsSource: string;
  modelMetadata: Record<string, ProviderModelMetadata>;
  modelLabels: Record<string, string>;
  configuredReasoningEffort: string;
  reasoningEffortLevels: EffortLevel[];
  customWireApi: CustomWireApi;
  proxyMode: ProviderProxyMode;
  thinkingBudget: number;
  responsesReasoningSummary: string;
  promptCacheRetention: string;
  maxTokens: number;
  imageModel: string;
  imageSize: string;
  imageQuality: string;
};

type ProviderCard = {
  key: string;
  provider: ProviderId;
  title: string;
  subtitle: string;
  model: string;
  wireApi: string;
  section: ProviderSection;
  entry?: ProviderHistoryEntry;
  historyIndex?: number;
  source: "saved" | "history" | "preset";
};

type ProviderOperation = "" | "save" | "models" | "delete" | "oauth";

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
  const activeConversationId = useAppStore((state) => state.conversationId);
  const oauthFlow = useAppStore((state) => {
    const owner = state.conversationId?.trim();
    if (!owner) return undefined;
    return state.providerOAuthFlowsByConversation[owner]?.[backendProvider(provider)];
  });
  const [displayName, setDisplayName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [providerHeaders, setProviderHeaders] = useState<Record<string, string>>({});
  const [providerAuthHeader, setProviderAuthHeader] = useState(false);
  const [oauthSupported, setOauthSupported] = useState(false);
  const [oauthConfigured, setOauthConfigured] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [smallFastModel, setSmallFastModel] = useState("");
  const [availableModelList, setAvailableModelList] = useState<string[]>([]);
  const [discoveredModelList, setDiscoveredModelList] = useState<string[]>([]);
  const [modelMetadata, setModelMetadata] = useState<Record<string, ProviderModelMetadata>>({});
  const [modelLabels, setModelLabels] = useState<Record<string, string>>({});
  const [modelAuthState, setModelAuthState] = useState<Record<string, "checking" | "ok" | "error">>({});
  const [configuredReasoningEffort, setConfiguredReasoningEffort] = useState("");
  const [reasoningEffortLevels, setReasoningEffortLevels] = useState<EffortLevel[]>([]);
  const [customWireApi, setCustomWireApi] = useState<CustomWireApi>("chat");
  const [proxyMode, setProxyMode] = useState<ProviderProxyMode>("inherit");
  const [thinkingBudget, setThinkingBudget] = useState(0);
  const [responsesReasoningSummary, setResponsesReasoningSummary] = useState("off");
  const [promptCacheRetention, setPromptCacheRetention] = useState("24h");
  const [maxTokens, setMaxTokens] = useState(0);
  const [imageModel, setImageModel] = useState("");
  const [imageSize, setImageSize] = useState("1024x1024");
  const [imageQuality, setImageQuality] = useState("");
  const [saving, setSaving] = useState(false);
  const [modelsStatus, setModelsStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [modelsResult, setModelsResult] = useState<LLMModelsRefreshResult | null>(null);
  const [modelsSource, setModelsSource] = useState("");
  const [deletingHistoryKey, setDeletingHistoryKey] = useState("");
  const [activeOperation, setActiveOperation] = useState<ProviderOperation>("");
  const [providerView, setProviderView] = useState<"list" | "detail">("list");
  const [activeIdentityOverride, setActiveIdentityOverride] = useState("");
  const detailDraftPinnedRef = useRef(false);
  const draftModelCounterRef = useRef(0);
  const operationRef = useRef<ProviderOperation>("");
  const [oauthNow, setOauthNow] = useState(() => Date.now());

  const beginOperation = (operation: Exclude<ProviderOperation, "">): boolean => {
    if (operationRef.current) return false;
    operationRef.current = operation;
    setActiveOperation(operation);
    return true;
  };

  const endOperation = (operation: Exclude<ProviderOperation, "">) => {
    if (operationRef.current !== operation) return;
    operationRef.current = "";
    setActiveOperation("");
  };

  const busy = Boolean(activeOperation);

  const providerConfig = PROVIDERS.find((p) => p.id === provider)!;
  const effectiveWireApi = effectiveCustomWireApi(provider, baseUrl, customWireApi);
  const responsesCachingEnabled = effectiveWireApi === "responses";
  const showApiFormat = canChooseApiFormat(provider, baseUrl);
  const showFixedAnthropicFormat = backendProvider(provider) === "anthropic";
  const configuredModelList = availableModelList.filter((item) => item.trim() && !isDraftModelId(item));
  const hasIncompleteModelMapping = availableModelList.some(isDraftModelId);
  const hasRequiredBaseUrl = provider !== "custom" || Boolean(baseUrl.trim());
  const hasRequiredModel = configuredModelList.length > 0;
  const canDiscoverModels = !busy && hasRequiredBaseUrl;
  const canSaveProvider = !busy && hasRequiredBaseUrl && hasRequiredModel && !hasIncompleteModelMapping;

  const draftFromState = (): ProviderDraft => ({
    provider,
    displayName,
    apiKey,
    headers: providerHeaders,
    authHeader: providerAuthHeader,
    baseUrl,
    modelName,
    smallFastModel,
    availableModelList,
    modelsSource,
    modelMetadata,
    modelLabels,
    configuredReasoningEffort,
    reasoningEffortLevels,
    customWireApi,
    proxyMode,
    thinkingBudget,
    responsesReasoningSummary,
    promptCacheRetention,
    maxTokens,
    imageModel,
    imageSize,
    imageQuality,
  });

  const applyCapabilitySection = (
    nextProvider: ProviderId,
    section: ProviderSection | undefined,
    selectedModel: string,
  ) => {
    const metadata = section?.model_metadata ?? {};
    const declared = metadata[selectedModel];
    const levels = normalizeEffortLevels(
      declared?.reasoning_effort_levels ?? section?.reasoning_effort_levels,
    );
    const configured = String(
      section?.configured_reasoning_effort ?? section?.reasoning_effort ?? "",
    ).trim().toLowerCase();
    const wireApi = effectiveCustomWireApi(
      nextProvider,
      section?.base_url ?? "",
      (section?.wire_api === "responses" || section?.wire_api === "anthropic"
        ? section.wire_api
        : "chat") as CustomWireApi,
    );
    setModelMetadata(metadata);
    setModelLabels(section?.model_labels ?? {});
    setConfiguredReasoningEffort(wireApi === "anthropic" ? "" : configured);
    setReasoningEffortLevels(wireApi === "anthropic" ? [] : levels);
  };

  const selectModel = (nextModel: string) => {
    setModelName(nextModel);
    const declared = modelMetadata[nextModel];
    const levels = normalizeEffortLevels(declared?.reasoning_effort_levels);
    setReasoningEffortLevels(effectiveWireApi === "anthropic" ? [] : levels);
  };

  const applyProviderSection = (nextProvider: ProviderId, section?: ProviderSection) => {
    const fallback = defaultSectionForProvider(nextProvider);
    setDisplayName(providerDisplayName(section));
    setApiKey(section?.api_key ?? "");
    setProviderHeaders(section?.headers ?? {});
    setProviderAuthHeader(Boolean(section?.auth_header));
    setBaseUrl(section?.base_url ?? fallback.base_url ?? "");
    const selectedModel = section?.model ?? fallback.model ?? "";
    setModelName(selectedModel);
    setSmallFastModel(section?.small_fast_model ?? fallback.small_fast_model ?? "");
    const savedModels = section?.available_models ?? fallback.available_models ?? [];
    const models = buildModelChoices(savedModels, section?.model ?? fallback.model ?? "");
    setAvailableModelList(models);
    setDiscoveredModelList([]);
    setModelAuthState({});
    setModelsSource(section?.models_source ?? "");
    if (backendProvider(nextProvider) === "anthropic") {
      setCustomWireApi("anthropic");
    } else {
      const wire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
      setCustomWireApi(effectiveCustomWireApi(nextProvider, section?.base_url ?? fallback.base_url ?? "", wire as CustomWireApi));
    }
    setProxyMode(section?.proxy_mode ?? fallback.proxy_mode ?? "inherit");
    setThinkingBudget(Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0);
    setMaxTokens(Math.max(0, Number(section?.max_tokens ?? fallback.max_tokens ?? 0) || 0));
    setResponsesReasoningSummary(section?.responses_reasoning_summary ?? fallback.responses_reasoning_summary ?? "off");
    const rawWire = section?.wire_api === "anthropic" || section?.wire_api === "responses" ? section.wire_api : "chat";
    const effectiveWire = effectiveCustomWireApi(nextProvider, section?.base_url ?? fallback.base_url ?? "", rawWire as CustomWireApi);
    setPromptCacheRetention(section?.prompt_cache_retention ?? defaultPromptCacheRetention(effectiveWire));
    setImageModel(section?.image_model ?? fallback.image_model ?? "");
    setImageSize(section?.image_size ?? fallback.image_size ?? "1024x1024");
    setImageQuality(section?.image_quality ?? fallback.image_quality ?? "");
    applyCapabilitySection(nextProvider, section ?? fallback, selectedModel);
  };

  const pinDetailDraft = () => {
    detailDraftPinnedRef.current = true;
  };

  const clearDetailDraftPin = () => {
    detailDraftPinnedRef.current = false;
  };

  const openProviderList = () => {
    if (operationRef.current) return;
    clearDetailDraftPin();
    setProviderView("list");
  };

  useEffect(() => {
    if (detailDraftPinnedRef.current) return;
    setProvider(selectedProvider);
    applyProviderSection(selectedProvider, savedOrHistorySectionForUiProvider(settingsPayload, selectedProvider));
  }, [selectedProvider, settingsPayload]);

  useEffect(() => {
    if (!oauthFlow?.expiresAt) return undefined;
    setOauthNow(Date.now());
    const timer = window.setInterval(() => setOauthNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [oauthFlow?.expiresAt]);

  useEffect(() => {
    let active = true;
    setOauthSupported(false);
    setOauthConfigured(false);
    const owner = activeConversationId?.trim();
    if (!owner) return () => { active = false; };
    void sendClientCommandAwaitResult(
      {
        type: "llm.provider.oauth.status",
        provider: backendProvider(provider),
        conversation_id: owner,
      } as ProviderOAuthCommand,
      "llm.provider.oauth.status",
      { timeoutMs: 10_000, silent: true },
    ).then((result) => {
      if (!active || !commandResultSucceeded(result)) return;
      const data = (result as { data?: Record<string, unknown> }).data ?? {};
      setOauthSupported(Boolean(data.oauth_supported));
      setOauthConfigured(Boolean(data.configured));
    }).catch(() => undefined);
    return () => { active = false; };
  }, [provider, activeConversationId]);

  const oauthAction = async (action: "login" | "logout") => {
    const owner = activeConversationId?.trim();
    if (!owner) {
      pushToast("请先打开一个会话，再进行提供商 OAuth 登录。", "warning");
      return;
    }
    if (!beginOperation("oauth")) return;
    const providerId = backendProvider(provider);
    try {
      const command: ProviderOAuthCommand = {
        type: `llm.provider.oauth.${action}`,
        provider: providerId,
        conversation_id: owner,
      };
      const result = await sendClientCommandAwaitResult(command, command.type, { timeoutMs: 300_000 });
      if (!commandResultSucceeded(result)) throw new Error(String((result as { message?: string }).message || "OAuth 操作失败"));
      setOauthConfigured(action === "login");
      useAppStore.getState().clearProviderOAuthFlow(owner, providerId);
      pushToast(action === "login" ? "提供商已登录" : "已退出提供商登录", "success");
    } catch (error) {
      const message = formatProviderError(error);
      const state = useAppStore.getState();
      const existing = state.providerOAuthFlowsByConversation[owner]?.[providerId];
      state.setProviderOAuthFlow({
        ...existing,
        conversationId: owner,
        provider: providerId,
        phase: "error",
        message,
        updatedAt: Date.now(),
      });
      pushToast(`OAuth 操作失败：${message}`, "error");
    } finally {
      endOperation("oauth");
    }
  };

  const openOAuthTarget = async (target: string) => {
    if (!isSafeOAuthUrl(target)) {
      pushToast("OAuth 链接不是安全的 HTTP(S) 地址，已拒绝打开。", "error");
      return;
    }
    try {
      let opened = false;
      if (isDesktop()) {
        opened = (await openExternal(target)) === true;
      } else if (typeof window !== "undefined" && typeof window.open === "function") {
        const popup = window.open(target, "_blank", "noopener,noreferrer");
        if (popup) popup.opener = null;
        opened = Boolean(popup);
      }
      if (!opened) pushToast("浏览器未能打开授权链接，请检查弹窗设置后重试。", "warning");
    } catch (error) {
      pushToast(`打开授权链接失败：${formatProviderError(error)}`, "error");
    }
  };

  const selectProviderPreset = (id: ProviderId) => {
    if (operationRef.current) return;
    pinDetailDraft();
    setProvider(id);
    applyProviderSection(id, defaultSectionForProvider(id));
    setDisplayName("");
    setApiKey("");
    setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
    onProviderChange(id);
  };

  const clearDiscoveredModels = () => {
    setDiscoveredModelList([]);
    setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
  };

  const addProvider = () => {
    if (operationRef.current) return;
    setProvider("custom");
    applyProviderSection("custom", {
      display_name: "",
      base_url: "",
      model: "",
      available_models: [],
      wire_api: "chat",
      proxy_mode: "inherit",
      thinking_budget: 0,
      responses_reasoning_summary: "off",
      prompt_cache_retention: "",
    });
    setDisplayName("");
    setApiKey("");
    setModelsStatus("idle");
    setModelsResult(null);
    setModelsSource("");
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
    if (!beginOperation("delete")) return;
    const confirmed = await showConfirm({
      title: "删除提供商配置",
      message: `确定删除“${historyLabel(entry)}”吗？保存的配置和凭据将被移除。`,
      confirmLabel: "删除",
      cancelLabel: "取消",
      danger: true,
    });
    if (!confirmed) {
      endOperation("delete");
      return;
    }
    setDeletingHistoryKey(key);
    try {
      const res = await fetchWithTimeout(`${apiBase()}/api/llm/provider-history`, {
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
      }, { timeoutMessage: "删除提供商配置超时，请重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText));
      }
      const savedPayload = await res.json() as LLMSettingsPayload;
      settingsPayloadRef.current = savedPayload;
      onSettingsPayloadChange?.(savedPayload);
      applyProviderSection(provider, savedOrHistorySectionForUiProvider(savedPayload, provider));
      pushToast("提供商配置已删除", "success");
    } catch (error) {
      pushToast(`提供商删除失败：${formatProviderError(error)}`, "error");
    } finally {
      setDeletingHistoryKey("");
      endOperation("delete");
    }
  };

  const payload = (draft = draftFromState(), options: { activate?: boolean; includeProvider?: boolean } = {}) => {
    const bp = backendProvider(draft.provider);
    const wireApi = effectiveCustomWireApi(draft.provider, draft.baseUrl, draft.customWireApi);
    const configuredModels = draft.availableModelList
      .map((item) => item.trim())
      .filter((item) => item && !isDraftModelId(item));
    const activeModel = configuredModels.includes(draft.modelName.trim())
      ? draft.modelName.trim()
      : configuredModels[0] || "";
    const configuredModelMetadata = Object.fromEntries(
      configuredModels
        .filter((id) => draft.modelMetadata[id])
        .map((id) => [id, draft.modelMetadata[id]]),
    );
    const configuredModelLabels = Object.fromEntries(
      configuredModels.map((id) => [id, id]),
    );
    const section = {
      display_name: draft.displayName.trim(),
      ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
      headers: draft.headers,
      auth_header: draft.authHeader,
      base_url: draft.baseUrl.trim(),
      model: activeModel,
      small_fast_model: draft.smallFastModel.trim(),
      available_models: configuredModels,
      models_source: draft.modelsSource || undefined,
      model_metadata: configuredModelMetadata,
      model_labels: configuredModelLabels,
      reasoning_effort: wireApi !== "anthropic" && (bp === "openai" || bp === "custom") ? draft.configuredReasoningEffort : undefined,
      reasoning_effort_levels: wireApi !== "anthropic" && (bp === "openai" || bp === "custom") ? draft.reasoningEffortLevels : undefined,
      thinking_budget: backendProvider(draft.provider) === "anthropic" || wireApi === "anthropic" ? draft.thinkingBudget : undefined,
      wire_api: bp === "openai" || bp === "custom" ? wireApi : undefined,
      proxy_mode: draft.proxyMode,
      responses_reasoning_summary: bp === "openai" || wireApi === "responses" ? draft.responsesReasoningSummary : undefined,
      prompt_cache_retention: wireApi === "responses" ? draft.promptCacheRetention : undefined,
      max_tokens: Math.max(0, Math.trunc(draft.maxTokens || 0)),
      // Images are a capability of this Provider profile. New saves never
      // create a second endpoint/key; old image_* fields remain readable by
      // the backend for migration compatibility.
      image_mode: "inherit",
      image_base_url: "",
      image_model: draft.imageModel.trim()
        || (isDedicatedImageModel(activeModel)
          ? activeModel
          : configuredModels.find(isDedicatedImageModel) || ""),
      image_size: draft.imageSize || "1024x1024",
      image_quality: draft.imageQuality,
    };
    return {
      confirm_sensitive_change: true,
      ...(options.activate || options.includeProvider ? { provider: bp } : {}),
      [bp]: section,
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
    if (!beginOperation("save")) return;
    setSaving(true);
    try {
      const res = await fetchWithTimeout(`${apiBase()}/api/llm/settings`, {
        method: "PUT",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload(draft, { activate: options.activate })),
      }, { timeoutMessage: "保存提供商配置超时，请重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText));
      }
      const saved = await res.json();
      const savedPayload = saved as LLMSettingsPayload;
      settingsPayloadRef.current = savedPayload;
      onSettingsPayloadChange?.(savedPayload);
      const bp = backendProvider(draft.provider);
      const section = savedPayload[bp] as ProviderSection | undefined;
      setProvider(draft.provider);
      onProviderChange(draft.provider);
      setDisplayName(providerDisplayName(section) || draft.displayName);
      // The local settings surface intentionally echoes the endpoint-scoped
      // key so reopening a profile never turns a valid key into a fake
      // "Missing API key" state.
      setApiKey(section?.api_key ?? draft.apiKey);
      setBaseUrl(section?.base_url ?? draft.baseUrl);
      setSmallFastModel(section?.small_fast_model ?? draft.smallFastModel);
      const active = String(section?.model || draft.modelName || saved.active_model || "");
      if (active) {
        setModelName(active);
      }
      if (section?.available_models) {
        setAvailableModelList(section.available_models);
      }
      const savedModels = section?.available_models ?? draft.availableModelList.filter((id) => !isDraftModelId(id));
      const identityModelLabels = Object.fromEntries(savedModels.map((id) => [id, id]));
      setModelLabels(identityModelLabels);
      setModelsSource(section?.models_source ?? "");
      setProxyMode(section?.proxy_mode ?? draft.proxyMode);
      setThinkingBudget(Number(section?.thinking_budget ?? draft.thinkingBudget) || 0);
      setMaxTokens(Math.max(0, Number(section?.max_tokens ?? draft.maxTokens) || 0));
      setImageModel(section?.image_model ?? draft.imageModel);
      setImageSize(section?.image_size ?? draft.imageSize);
      setImageQuality(section?.image_quality ?? draft.imageQuality);
      setResponsesReasoningSummary(section?.responses_reasoning_summary ?? draft.responsesReasoningSummary);
      setPromptCacheRetention(section?.prompt_cache_retention ?? draft.promptCacheRetention);
      applyCapabilitySection(draft.provider, section, active || draft.modelName);
      // Update the Composer from the same response that was just persisted.
      // The websocket projection remains authoritative, but waiting for it
      // leaves a visible window where the old provider/model is still shown.
      const composerModels = selectableModelsForProvider(
        section?.available_models ?? draft.availableModelList,
        active || draft.modelName,
        bp,
        section?.models_source ?? draft.modelsSource,
      );
      useAppStore.setState({
        currentProvider: bp,
        currentModel: active || draft.modelName,
        availableModels: composerModels,
        availableModelLabels: identityModelLabels,
        modelsSource: section?.models_source ?? draft.modelsSource ?? "",
        currentProviderId: bp,
        currentProviderBaseUrl: section?.base_url ?? draft.baseUrl,
        currentWireApi: section?.wire_api ?? effectiveCustomWireApi(
          draft.provider,
          section?.base_url ?? draft.baseUrl,
          draft.customWireApi,
        ),
      });
      const appliedWireApi = bp === "openai" || bp === "custom"
        ? effectiveCustomWireApi(draft.provider, section?.base_url || draft.baseUrl, ((section?.wire_api as CustomWireApi | undefined) || effectiveCustomWireApi(draft.provider, draft.baseUrl, draft.customWireApi)))
        : undefined;
      if (appliedWireApi) setCustomWireApi(appliedWireApi);
            if (options.activate) {
        setActiveIdentityOverride(cardIdentityForDraft(draft.provider, {
          base_url: section?.base_url || draft.baseUrl,
          wire_api: section?.wire_api || effectiveCustomWireApi(
            draft.provider,
            section?.base_url || draft.baseUrl,
            draft.customWireApi,
          ),
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
      if (!options.quiet) {
        pushToast(
          options.activate
            ? `提供商已启用 · 模型：${active || draft.modelName}`
            : `提供商已保存 · 模型：${active || draft.modelName}`,
          "success",
        );
      }
      setProviderView("list");
      clearDetailDraftPin();
    } catch (error) {
      pushToast(`提供商保存失败：${formatProviderError(error)}`, "error");
    } finally {
      setSaving(false);
      endOperation("save");
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
      headers: section?.headers ?? {},
      authHeader: section?.auth_header === true,
      baseUrl: nextBaseUrl,
      modelName: nextModel,
      smallFastModel: section?.small_fast_model ?? fallback.small_fast_model ?? "",
      availableModelList: nextModels,
      modelsSource: section?.models_source ?? "",
      modelMetadata: section?.model_metadata ?? {},
      modelLabels: section?.model_labels ?? {},
      configuredReasoningEffort: String(
        section?.configured_reasoning_effort ?? section?.reasoning_effort ?? "",
      ).trim().toLowerCase(),
      reasoningEffortLevels: normalizeEffortLevels(section?.reasoning_effort_levels),
      customWireApi: wire,
      proxyMode: section?.proxy_mode ?? fallback.proxy_mode ?? "inherit",
      thinkingBudget: Number(section?.thinking_budget ?? fallback.thinking_budget ?? 0) || 0,
      responsesReasoningSummary: section?.responses_reasoning_summary ?? fallback.responses_reasoning_summary ?? "off",
      promptCacheRetention: section?.prompt_cache_retention ?? defaultPromptCacheRetention(wire),
      maxTokens: Math.max(0, Number(section?.max_tokens ?? fallback.max_tokens ?? 0) || 0),
      imageModel: section?.image_model ?? fallback.image_model ?? "",
      imageSize: section?.image_size ?? fallback.image_size ?? "1024x1024",
      imageQuality: section?.image_quality ?? fallback.image_quality ?? "",
    };
  };

  const editProviderCard = (card: ProviderCard) => {
    if (operationRef.current) return;
    pinDetailDraft();
    setProvider(card.provider);
    applyProviderSection(card.provider, card.section);
    setModelsStatus("idle");
    setModelsResult(null);
    setProviderView("detail");
    onProviderChange(card.provider);
  };

  const useProviderCard = async (card: ProviderCard) => {
    if (operationRef.current) return;
    clearDetailDraftPin();
    setProvider(card.provider);
    applyProviderSection(card.provider, card.section);
    await saveProvider(draftFromSection(card.provider, card.section), { activate: true });
  };

  const addModelMapping = () => {
    draftModelCounterRef.current += 1;
    const draftId = `${DRAFT_MODEL_PREFIX}${draftModelCounterRef.current}`;
    setAvailableModelList((current) => [...current, draftId]);
    setModelMetadata((current) => ({ ...current, [draftId]: {} }));
    setModelLabels((current) => ({ ...current, [draftId]: "" }));
  };

  const updateModelMappingId = (previousId: string, rawNextId: string) => {
    const nextId = String(rawNextId || "").trim();
    if (!nextId || isDraftModelId(nextId)) return;
    if (availableModelList.some((item) => item === nextId && item !== previousId)) {
      pushToast("这个模型已经添加。", "warning");
      return;
    }

    setAvailableModelList((current) => current.map((item) => item === previousId ? nextId : item));
    setModelMetadata((current) => {
      const next = { ...current };
      next[nextId] = next[previousId] ?? next[nextId] ?? {};
      delete next[previousId];
      return next;
    });
    setModelLabels((current) => {
      const next = { ...current };
      next[nextId] = String(next[previousId] || next[nextId] || nextId).trim() || nextId;
      delete next[previousId];
      return next;
    });
    setModelAuthState((current) => {
      const next = { ...current };
      if (next[previousId]) next[nextId] = next[previousId];
      delete next[previousId];
      return next;
    });
    if (!modelName.trim() || isDraftModelId(modelName) || modelName === previousId) {
      selectModel(nextId);
    }
  };

  const removeModelMapping = (modelId: string) => {
    const next = availableModelList.filter((item) => item !== modelId);
    setAvailableModelList(next);
    setModelMetadata((current) => {
      const copy = { ...current };
      delete copy[modelId];
      return copy;
    });
    setModelLabels((current) => {
      const copy = { ...current };
      delete copy[modelId];
      return copy;
    });
    setModelAuthState((current) => {
      const copy = { ...current };
      delete copy[modelId];
      return copy;
    });
    if (modelName === modelId) {
      selectModel(next.find((item) => !isDraftModelId(item)) || "");
    }
  };

  const updateModelContext = (modelId: string, rawValue: string) => {
    const value = Math.max(0, Math.trunc(Number(rawValue) || 0));
    setModelMetadata((current) => {
      const existing = { ...(current[modelId] ?? {}) };
      if (value > 0) existing.context_window = value;
      else delete existing.context_window;
      return { ...current, [modelId]: existing };
    });
  };

  const checkModelAuth = async (modelId: string) => {
    if (modelAuthState[modelId] === "checking") return;
    setModelAuthState((current) => ({ ...current, [modelId]: "checking" }));
    try {
      const draft = draftFromState();
      const res = await fetchWithTimeout(`${apiBase()}/api/llm/check`, {
        method: "POST",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload({ ...draft, modelName: modelId }, { includeProvider: true })),
      }, { timeoutMessage: "模型鉴权检查超时，请重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText));
      }
      const result = await res.json() as LLMCheckResult;
      setModelAuthState((current) => ({ ...current, [modelId]: result.ok ? "ok" : "error" }));
      pushToast(`${modelId}：${result.ok ? "鉴权通过" : (result.message || "鉴权失败")}`, result.ok ? "success" : "error");
    } catch (error) {
      setModelAuthState((current) => ({ ...current, [modelId]: "error" }));
      pushToast(`模型鉴权失败：${formatProviderError(error)}`, "error");
    }
  };

  const discoverModels = async () => {
    if (!beginOperation("models")) return;
    setModelsStatus("loading");
    setModelsResult(null);
    try {
      const res = await fetchWithTimeout(`${apiBase()}/api/llm/models/refresh`, {
        method: "POST",
        headers: jsonAuthHeaders(),
        body: JSON.stringify(payload(undefined, { includeProvider: true })),
      }, { timeoutMessage: "发现模型超时，请检查接口后重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText));
      }
      const data = await res.json() as LLMModelsRefreshResult;
      setModelsResult(data);
      setProxyMode(data.proxy_mode ?? proxyMode);
      if (data.source === "live") {
        const discovered = buildModelChoices(data.models ?? [], "");
        const models = availableModelList.filter((item) => !isDraftModelId(item));
        const nextModel = models.includes(modelName) ? modelName : models[0] || "";
        setDiscoveredModelList(discovered);
        setModelsSource("live");
        setModelMetadata((current) => ({ ...current, ...(data.model_metadata ?? {}) }));
        setConfiguredReasoningEffort(data.configured_reasoning_effort || "");
        setReasoningEffortLevels(normalizeEffortLevels(data.reasoning_effort_levels));
        const bp = backendProvider(provider);
        const previousPayload = settingsPayloadRef.current ?? {};
        const previousSection = previousPayload[bp] ?? {};
        const nextSection: ProviderSection = {
          ...previousSection,
          proxy_mode: data.proxy_mode ?? proxyMode,
          available_models: models,
          model: nextModel,
          models_source: "live",
          model_metadata: { ...modelMetadata, ...(data.model_metadata ?? {}) },
          model_labels: Object.fromEntries(models.map((id) => [id, id])),
          configured_reasoning_effort: data.configured_reasoning_effort || "",
          effective_reasoning_effort: data.effective_reasoning_effort || "",
          reasoning_effort_supported: Boolean(data.reasoning_effort_supported),
          reasoning_effort_levels: normalizeEffortLevels(data.reasoning_effort_levels),
          context_window: Number(data.context_window || 0),
          context_window_source: data.context_window_source || "",
          context_window_verified: Boolean(data.context_window_verified),
          max_context_window: Number(data.max_context_window || 0),
          max_context_window_source: data.max_context_window_source || "",
          max_context_window_verified: Boolean(data.max_context_window_verified),
          max_output_tokens: Number(data.max_output_tokens || 0),
          max_output_tokens_source: data.max_output_tokens_source || "",
          max_output_tokens_verified: Boolean(data.max_output_tokens_verified),
          default_reasoning_effort: data.default_reasoning_effort || "",
          default_reasoning_summary: data.default_reasoning_summary || "",
        };
        const nextPayload: LLMSettingsPayload = {
          ...previousPayload,
          [bp]: nextSection,
        };
        settingsPayloadRef.current = nextPayload;
        onSettingsPayloadChange?.(nextPayload);
        setModelsStatus("success");
        applyCapabilitySection(provider, nextSection, nextModel);
        pushToast(`已获取 ${discovered.length} 个模型，可在新增映射中选择。`, "success");
      } else {
        setModelsStatus("error");
        setModelsSource("");
        pushToast(modelDiscoveryFailureSummary(data), data.retryable ? "warning" : "error");
      }
    } catch (error) {
      setModelsStatus("error");
      pushToast(`模型发现失败：${formatProviderError(error)}`, "error");
    } finally {
      endOperation("models");
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
    setPromptCacheRetention((current) =>
      promptCacheRetentionAfterWireChange(current, effectiveNext));
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
    wire_api: effectiveWireApi,
  });
  const oauthRemainingSeconds = oauthFlow?.expiresAt
    ? Math.max(0, Math.ceil((oauthFlow.expiresAt - oauthNow) / 1_000))
    : undefined;
  const oauthTargets: Array<{ url: string; label: string }> = [];
  const addOAuthTarget = (url: string | undefined, label: string) => {
    if (!url || oauthTargets.some((target) => target.url === url)) return;
    oauthTargets.push({ url, label });
  };
  addOAuthTarget(oauthFlow?.url, "授权页面");
  addOAuthTarget(oauthFlow?.verificationUri, "设备验证页面");
  for (const link of oauthFlow?.links ?? []) addOAuthTarget(link.url, link.label || "相关页面");

  const providerList = (
    <div className="provider-settings">
      <div className="provider-settings-header">
        <div className="provider-settings-heading">
          <h3>模型提供商</h3>
          <p>保存接口、凭据和模型配置，并选择新任务使用的提供商。</p>
        </div>
        <button type="button" onClick={addProvider} disabled={busy} className="provider-add-button">
          <Plus size={14} />
          <span>添加提供商</span>
        </button>
      </div>

      {providerCards.length > 0 ? (
        <div className="provider-card-list">
        {providerCards.map((card) => {
          const cardIdentity = card.key.replace(/::(?:saved|history-\d+)$/, "");
          const active = activeCardIdentity === cardIdentity;
          const editing = draftCardIdentity === cardIdentity;
          return (
            <div key={card.key} className="provider-card" data-active={active ? "true" : "false"} data-editing={editing ? "true" : "false"}>
              <button
                type="button"
                onClick={() => editProviderCard(card)}
                disabled={busy}
                className="provider-card-main"
                title={[card.subtitle, card.section.base_url, card.model, wireApiLabel(card.wireApi)].filter(Boolean).join(" · ")}
              >
                <ModelBrandIcon
                  model={card.model}
                  provider={`${card.provider} ${card.title} ${card.section.base_url || ""}`}
                  websiteUrl={card.section.base_url}
                  size={21}
                  framed
                />
                <span className="provider-card-copy">
                  <span className="provider-card-title">{card.title}</span>
                  <span className="provider-card-url">{card.section.base_url || "未配置接口地址"}</span>
                  <span className="provider-card-meta">
                    <span>{card.subtitle}</span>
                    <i aria-hidden="true" />
                    <span>{card.model}</span>
                  </span>
                </span>
              </button>
              <div className="provider-card-actions">
                <button
                  type="button"
                  onClick={() => editProviderCard(card)}
                  disabled={busy}
                  className="provider-icon-action"
                  aria-label={`编辑 ${card.title}`}
                  title="编辑"
                >
                  <Pencil size={14} />
                </button>
                <button
                  type="button"
                  onClick={() => void useProviderCard(card)}
                  disabled={active || busy}
                  className="provider-use-action"
                  data-active={active ? "true" : "false"}
                  aria-label={active ? `${card.title} 已启用` : `使用 ${card.title}`}
                  title={active ? "已启用" : "使用提供商"}
                >
                  {active ? <Check size={14} /> : <Play size={14} />}
                  <span>{active ? "已启用" : "使用"}</span>
                </button>
                {card.entry && card.historyIndex != null && (
                  <button
                    type="button"
                    onClick={() => void deleteHistoryEntry(card.entry!, card.historyIndex!)}
                    disabled={busy}
                    className="provider-icon-action provider-delete-action"
                    title="删除已保存的提供商"
                    aria-label={deletingHistoryKey === historyEntryKey(card.entry, card.historyIndex)
                      ? `正在删除提供商 ${card.title}`
                      : `删除已保存的提供商 ${card.title}`}
                  >
                    {deletingHistoryKey === historyEntryKey(card.entry, card.historyIndex)
                      ? <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                      : <Trash2 size={14} />}
                  </button>
                )}
              </div>
            </div>
          );
        })}
        </div>
      ) : (
        <div className="provider-empty-list">
          <div className="provider-empty-title">尚未配置提供商</div>
          <div>添加提供商，填写接口地址、API 密钥和模型即可开始使用。</div>
        </div>
      )}
    </div>
  );

  const providerDetails = (
    <div className="provider-settings provider-settings-detail">
      <div className="provider-detail-header">
        <button type="button" onClick={openProviderList} disabled={busy} className="provider-detail-back" aria-label="返回提供商列表" title="返回提供商列表"><ArrowLeft /></button>
        <div className="provider-detail-heading">
          <div>{draftTitle}</div>
          <p>配置提供商的连接方式、凭据和默认模型。</p>
        </div>
      </div>

      <Section title="显示名称">
        <input
          type="text"
          aria-label="提供商显示名称"
          value={displayName}
          disabled={busy}
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
              disabled={busy}
              style={presetButtonStyle(provider === item.id)}
            >
              <ModelBrandIcon model={item.defaultModel} provider={item.id} size={19} framed />
              <span style={presetTitleStyle}>{item.label}</span>
            </button>
          ))}
        </div>
      </Section>

      {providerConfig.hasBaseUrl && (
        <Section title="接口地址">
          <input
            type="text"
            aria-label="接口地址"
            value={baseUrl}
            disabled={busy}
            required
            onChange={(event) => {
              setBaseUrl(event.target.value);
              clearDiscoveredModels();
            }}
            placeholder="https://api.example.com/v1"
            style={inputStyle}
          />
        </Section>
      )}

      <Section title="API 密钥">
        <input
          type="text"
          aria-label="API 密钥"
          value={apiKey}
          disabled={busy}
          onChange={(event) => {
            setApiKey(event.target.value);
            clearDiscoveredModels();
          }}
          placeholder={providerConfig.placeholder}
          autoComplete="off"
          spellCheck={false}
          style={inputStyle}
        />
      </Section>

      {showApiFormat && (
        <Section title="API 格式">
          <SelectMenu
            ariaLabel="API 格式"
            value={customWireApi}
            disabled={busy}
            onValueChange={(value) => updateWireApi(value as CustomWireApi)}
          >
            <option value="chat">OpenAI Chat Completions</option>
            <option value="responses">OpenAI Responses</option>
            {backendProvider(provider) === "custom" && <option value="anthropic">Anthropic Messages</option>}
          </SelectMenu>
        </Section>
      )}

      {showFixedAnthropicFormat && (
        <Section title="API 格式">
          <div style={readOnlyFormatStyle}>Anthropic Messages</div>
        </Section>
      )}

      <Section title="模型" description="只显示你添加的模型；模型 ID 会原样出现在 Composer 中。">
        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, flexWrap: "wrap" }}>
            <button type="button" onClick={discoverModels} disabled={!canDiscoverModels} title={hasRequiredBaseUrl ? "从接口获取模型列表" : "请先填写接口地址"} style={secondaryActionStyle}>
              <RefreshCw size={14} />{modelsStatus === "loading" ? "正在获取…" : "获取模型列表"}
            </button>
            <button type="button" onClick={addModelMapping} disabled={busy} title="添加一个模型映射" style={secondaryActionStyle}>
              <Plus size={14} />添加模型
            </button>
          </div>
          {modelsStatus !== "idle" && (
            <div style={statusStyle(modelsStatus === "error" ? "error" : modelsStatus === "loading" ? "testing" : "success")}>
              {modelsStatus === "loading" && "正在获取模型列表…"}
              {modelsStatus === "success" && modelsResult && (
                <span>
                  已获取 {discoveredModelList.length} 个候选模型
                  {modelsResult.source_message ? ` · ${modelsResult.source_message}` : ""}
                </span>
              )}
              {modelsStatus === "error" && (
                <span>{modelsResult ? modelDiscoveryFailureSummary(modelsResult) : "模型列表获取失败；仍可手动填写模型 ID。"}</span>
              )}
            </div>
          )}
          {availableModelList.length === 0 && (
            <div style={{ padding: "18px 12px", border: "1px dashed var(--border-subtle)", borderRadius: 8, color: "var(--text-tertiary)", fontSize: "var(--mc-font-body)", textAlign: "center" }}>
              尚未添加模型
            </div>
          )}
          {availableModelList.map((modelId) => {
            const metadata = modelMetadata[modelId] ?? {};
            const auth = modelAuthState[modelId];
            const isDraft = isDraftModelId(modelId);
            const selectableCandidates = !isDraft && !discoveredModelList.includes(modelId)
              ? [modelId, ...discoveredModelList]
              : discoveredModelList;
            return (
              <div key={modelId} style={{ display: "grid", gridTemplateColumns: "minmax(220px, 1fr) 120px auto auto", gap: 6, alignItems: "center" }}>
                {discoveredModelList.length > 0 ? (
                  <SelectMenu
                    ariaLabel={`${modelId} 实际请求模型`}
                    value={isDraft ? "" : modelId}
                    disabled={busy}
                    onValueChange={(value) => updateModelMappingId(modelId, value)}
                    className="mc-select-menu-mono"
                  >
                    <option value="">选择实际请求模型</option>
                    {selectableCandidates.map((candidate) => (
                      <option
                        key={candidate}
                        value={candidate}
                        disabled={availableModelList.some((item) => item === candidate && item !== modelId)}
                      >
                        {candidate}
                      </option>
                    ))}
                  </SelectMenu>
                ) : (
                  <input
                    aria-label={`${modelId} 实际请求模型`}
                    defaultValue={isDraft ? "" : modelId}
                    disabled={busy}
                    onBlur={(event) => updateModelMappingId(modelId, event.target.value)}
                    placeholder="实际请求模型 ID"
                    spellCheck={false}
                    style={{ ...inputStyle, fontFamily: "var(--font-mono)" }}
                  />
                )}
                <input
                  type="number"
                  min={0}
                  step={1024}
                  aria-label={`${modelId} 上下文窗口`}
                  defaultValue={metadata.context_window || ""}
                  disabled={busy}
                  onBlur={(event) => updateModelContext(modelId, event.target.value)}
                  placeholder="例如 128000"
                  style={inputStyle}
                />
                <button type="button" onClick={() => void checkModelAuth(modelId)} disabled={busy || isDraft || auth === "checking"} title={isDraft ? "请先选择或填写模型" : "检查该模型鉴权"} style={secondaryActionStyle}>
                  <ShieldCheck size={14} />{auth === "checking" ? "检查中" : auth === "ok" ? "已通过" : auth === "error" ? "重试" : "鉴权"}
                </button>
                <button type="button" onClick={() => removeModelMapping(modelId)} disabled={busy} title="移除模型映射" style={secondaryActionStyle} aria-label={`移除 ${modelId}`}><Trash2 size={14} /></button>
              </div>
            );
          })}
        </div>
      </Section>

      {(oauthSupported || oauthFlow) && (
        <Section title="OAuth 登录">
          <div style={{ display: "grid", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={capabilityValueStyle}>{oauthConfigured ? "已登录" : oauthFlow ? oauthPhaseLabel(oauthFlow.phase) : "未登录"}</span>
            {oauthConfigured ? (
              <button type="button" onClick={() => void oauthAction("logout")} disabled={busy} title="退出 OAuth 登录" style={secondaryActionStyle}>
                <LogOut size={14} />{activeOperation === "oauth" ? "正在退出…" : "退出登录"}
              </button>
            ) : (
              <button type="button" onClick={() => void oauthAction("login")} disabled={busy} title="登录 OAuth 提供商" style={secondaryActionStyle}>
                <LogIn size={14} />{activeOperation === "oauth" ? "正在登录…" : "登录"}
              </button>
            )}
            </div>

            {oauthFlow && (
              <div style={oauthFlowPanelStyle} aria-live="polite">
                <div style={oauthFlowHeaderStyle}>
                  <strong>{oauthPhaseLabel(oauthFlow.phase)}</strong>
                  <span>{oauthFlow.provider}</span>
                </div>
                {oauthFlow.instructions && <p style={oauthFlowMessageStyle}>{oauthFlow.instructions}</p>}
                {oauthFlow.message && <p style={oauthFlowMessageStyle}>{oauthFlow.message}</p>}
                {oauthFlow.userCode && (
                  <div style={oauthDeviceCodeRowStyle}>
                    <span>设备码</span>
                    <code style={oauthDeviceCodeStyle}>{oauthFlow.userCode}</code>
                  </div>
                )}
                {(oauthFlow.intervalSeconds || oauthFlow.expiresInSeconds) && (
                  <div style={oauthMetaStyle}>
                    {oauthFlow.intervalSeconds && <span>轮询间隔：{oauthFlow.intervalSeconds} 秒</span>}
                    {oauthFlow.expiresInSeconds && (
                      <span>
                        有效期：{oauthFlow.expiresInSeconds} 秒
                        {oauthRemainingSeconds !== undefined ? ` · 剩余 ${formatOAuthDuration(oauthRemainingSeconds)}` : ""}
                      </span>
                    )}
                  </div>
                )}
                {oauthTargets.map((target) => (
                  <div key={target.url} style={oauthLinkRowStyle}>
                    <div style={{ minWidth: 0 }}>
                      <div style={oauthLinkLabelStyle}>{target.label}</div>
                      <div style={oauthUrlStyle}>{target.url}</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => void openOAuthTarget(target.url)}
                      style={secondaryActionStyle}
                      title={`在系统浏览器中打开${target.label}`}
                    >
                      <ExternalLink size={14} />打开
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Section>
      )}

      <details style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 4 }}>
        <summary style={{ cursor: "pointer", padding: "10px 0", color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
          高级设置
        </summary>
        <div style={{ display: "grid", gap: 10 }}>
      <Section
        title="网络连接"
        description={proxyMode === "direct"
          ? "该 Provider 将忽略 MiniCode 与系统进程代理，直接连接上方接口地址。"
          : "遵循 MiniCode 的 LLM 代理、系统进程代理与 NO_PROXY 绕过规则。"}
      >
        <SelectMenu
          ariaLabel="网络连接"
          value={proxyMode}
          disabled={busy}
          onValueChange={(value) => setProxyMode(value as ProviderProxyMode)}
        >
          <option value="inherit">跟随全局代理</option>
          <option value="direct">直连（忽略代理）</option>
        </SelectMenu>
      </Section>

      {responsesCachingEnabled && (
        <Section title="Responses 提示词缓存" description="仅 Responses API 使用；Chat Completions 与 Anthropic Messages 不发送这个字段。">
          <SettingSelect
            label="提示词缓存"
            value={promptCacheRetention || "off"}
            disabled={busy}
            onChange={(value) => setPromptCacheRetention(value === "off" ? "" : value)}
            options={[
              { value: "24h", label: "保留 24 小时" },
              { value: "in_memory", label: "仅内存" },
              { value: "off", label: "关闭" },
            ]}
          />
        </Section>
      )}

      {effectiveWireApi === "responses" && (
        <Section title="Responses 推理摘要" description="控制是否请求 Provider 返回可见的推理摘要。">
          <div style={{ display: "grid", gap: 10 }}>
            <SettingSelect
              label="推理摘要"
              value={responsesReasoningSummary || "off"}
              disabled={busy}
              onChange={setResponsesReasoningSummary}
              options={[
                { value: "off", label: "关闭" },
                { value: "auto", label: "自动" },
                { value: "detailed", label: "详细" },
              ]}
            />
          </div>
        </Section>
      )}

      {(backendProvider(provider) === "anthropic" || effectiveWireApi === "anthropic") && (
        <Section title="扩展思考 Token 预算" description="仅 Anthropic Messages 使用；0 表示关闭扩展思考，不是上下文窗口。">
          <input
            type="number"
            min={0}
            step={512}
            aria-label="思考预算"
            value={thinkingBudget}
            disabled={busy}
            onChange={(e) => setThinkingBudget(Math.max(0, Number(e.target.value) || 0))}
            placeholder="0"
            style={inputStyle}
          />
        </Section>
      )}

      <Section title="请求最大输出 Token" description="0 表示自动/未设置，MiniCode 会从请求中省略该字段；正数才会作为本次输出上限发送。">
        <input
          type="number"
          min={0}
          step={256}
          aria-label="请求最大输出 Token"
          value={maxTokens}
          disabled={busy}
          onChange={(event) => setMaxTokens(Math.max(0, Math.trunc(Number(event.target.value) || 0)))}
          placeholder="0（自动）"
          style={inputStyle}
        />
      </Section>

      <Section title="辅助模型">
        <input
          type="text"
          aria-label="辅助模型"
          value={smallFastModel}
          disabled={busy}
          onChange={(e) => setSmallFastModel(e.target.value)}
          placeholder={modelName || "默认使用主模型"}
          style={inputStyle}
        />
      </Section>
        </div>
      </details>

      <div className="provider-detail-actions" style={actionBarStyle}>
        <button type="button" onClick={() => void saveProvider()} disabled={!canSaveProvider} title={!hasRequiredBaseUrl ? "请先填写接口地址" : !hasRequiredModel ? "请先添加模型" : hasIncompleteModelMapping ? "请完成或删除空模型行" : "保存提供商"} style={primaryActionStyle}><Save size={14} />{saving ? "正在保存…" : "保存"}</button>
      </div>
    </div>
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

const isSafeOAuthUrl = (value: string): boolean => {
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:")
      && !parsed.username
      && !parsed.password;
  } catch {
    return false;
  }
};

const oauthPhaseLabel = (phase: string): string => ({
  auth_url: "等待浏览器授权",
  device_code: "等待设备码验证",
  info: "授权提示",
  progress: "正在授权",
  error: "授权失败",
}[phase] || "OAuth 授权");

const formatOAuthDuration = (seconds: number): string => {
  const remaining = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(remaining / 60);
  const tail = String(remaining % 60).padStart(2, "0");
  return `${minutes}:${tail}`;
};

const MODEL_DISCOVERY_FAILURE_LABELS: Record<string, string> = {
  configuration_error: "配置不完整",
  authentication_failed: "鉴权失败",
  models_endpoint_not_found: "模型列表接口不存在",
  rate_limited: "请求频率受限",
  provider_unavailable: "Provider 暂时不可用",
  network_error: "网络连接失败",
  model_list_empty: "Provider 返回了空模型列表",
  model_discovery_failed: "模型发现失败",
};

const modelDiscoveryFailureSummary = (result: LLMModelsRefreshResult): string => {
  const kind = String(result.failure_kind || "model_discovery_failed").trim();
  const label = MODEL_DISCOVERY_FAILURE_LABELS[kind] || "模型发现失败";
  const details = [
    result.status_code ? `HTTP ${result.status_code}` : "",
    String(result.message || result.source_message || "").trim(),
    String(result.hint || "").trim(),
    result.retryable ? "可以重试" : "",
    "已保留手动输入的模型",
  ].filter(Boolean);
  return [label, ...Array.from(new Set(details))].join(" · ");
};

const SettingSelect = ({
  label,
  value,
  disabled = false,
  onChange,
  options,
}: {
  label: string;
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) => (
  <div style={settingSelectRowStyle}>
    <span style={settingSelectLabelStyle}>{label}</span>
    <SelectMenu ariaLabel={label} value={value} disabled={disabled} onValueChange={onChange}>
      {options.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </SelectMenu>
  </div>
);

const readOnlyFormatStyle = {
  ...inputStyle,
  fontFamily: "var(--font-ui)",
  color: "var(--text-secondary)",
};

const settingSelectRowStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "96px minmax(0, 1fr)",
  alignItems: "center",
  gap: 10,
};

const settingSelectLabelStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--mc-font-body)",
};

const capabilityValueStyle: CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--mc-font-body)",
  fontWeight: "var(--fw-semibold)",
};

const inlineWarningStyle: CSSProperties = {
  padding: "8px 10px",
  border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 8px)",
  background: "color-mix(in oklch, var(--state-warning) 6%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--mc-font-caption)",
  lineHeight: 1.5,
};

const oauthFlowPanelStyle: CSSProperties = {
  display: "grid",
  gap: 9,
  padding: 10,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
  background: "var(--surface-soft)",
};

const oauthFlowHeaderStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 10,
  color: "var(--text-primary)",
  fontSize: "var(--mc-font-body)",
};

const oauthFlowMessageStyle: CSSProperties = {
  margin: 0,
  color: "var(--text-secondary)",
  fontSize: "var(--mc-font-body)",
  lineHeight: 1.5,
  whiteSpace: "pre-wrap",
};

const oauthDeviceCodeRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 12,
  color: "var(--text-secondary)",
  fontSize: "var(--mc-font-body)",
};

const oauthDeviceCodeStyle: CSSProperties = {
  padding: "5px 9px",
  borderRadius: 6,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-primary)",
  fontSize: "var(--mc-font-body)",
  fontWeight: "var(--fw-bold)",
  letterSpacing: "0.08em",
  userSelect: "all",
};

const oauthMetaStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "5px 12px",
  color: "var(--text-tertiary)",
  fontSize: "var(--mc-font-caption)",
};

const oauthLinkRowStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) auto",
  alignItems: "center",
  gap: 10,
};

const oauthLinkLabelStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--mc-font-caption)",
  fontWeight: "var(--fw-semibold)",
};

const oauthUrlStyle: CSSProperties = {
  marginTop: 2,
  color: "var(--text-tertiary)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--mc-font-caption)",
  overflowWrap: "anywhere",
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
  backgroundColor: active ? "var(--surface-active)" : "var(--surface-soft)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  textAlign: "left",
});

const presetTitleStyle: CSSProperties = {
  display: "block",
  color: "var(--text-primary)",
  fontSize: "var(--mc-font-body)",
  fontWeight: "var(--fw-bold)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
