export type CapabilitySource = "doctor" | "status" | "runtime" | "unknown";

export interface DoctorPayload {
  backend?: Record<string, unknown>;
  llm?: Record<string, unknown>;
  mcp?: unknown[];
  capabilities?: AgentCapabilitiesPayload;
  capabilitySource?: CapabilitySource;
  workspace?: Record<string, unknown>;
  git?: Record<string, unknown>;
  preview?: Record<string, unknown>;
  terminal?: Record<string, unknown>;
  error?: string;
}

export interface AgentCapabilitiesPayload {
  summary?: AgentCapabilitySummary;
  tools?: AgentCapabilityTool[];
  tool_views?: AgentCapabilityToolView[];
  tool_runtime_metadata?: Record<string, unknown>;
  provider_capabilities?: AgentProviderCapabilities;
  commands?: AgentCapabilityNamedItem[];
  skills?: AgentCapabilityNamedItem[];
  composer_commands?: AgentCapabilityNamedItem[];
  feature_flags?: Record<string, AgentCapabilityFeatureFlag>;
  permission?: AgentCapabilityPermission;
  mcp_registry_version?: number;
  version?: number;
}

export interface AgentProviderCapabilities {
  provider?: unknown;
  model?: unknown;
  wire_api?: unknown;
  provider_id?: unknown;
  base_url?: unknown;
  streaming?: unknown;
  tool_calling?: unknown;
  parallel_tool_calls?: unknown;
  json_mode?: unknown;
  reasoning_effort?: unknown;
  reasoning_effort_levels?: unknown;
  vision?: unknown;
  native_pdf?: unknown;
  image_generation?: unknown;
  stateful_continuation?: unknown;
  confidence?: unknown;
  limitations?: unknown;
  adapters?: unknown;
}

export interface AgentCapabilityFeatureFlag {
  enabled?: unknown;
  source?: unknown;
}

export interface AgentCapabilityPermission {
  mode?: string;
  profile?: string;
  source?: string;
  workspace_scope?: string;
  sandbox_status?: {
    os?: string;
    network?: string;
  };
}

export interface AgentCapabilitySummary {
  tools_total?: number;
  direct_tools?: number;
  core_tools?: number;
  deferred_tools?: number;
  hidden_tools?: number;
  mcp_proxy_tools?: number;
  commands?: number;
  skills?: number;
  mcp_resource_bridge?: boolean;
  deferred_bridge?: boolean;
  skill_catalog?: boolean;
}

export interface AgentCapabilityTool {
  name?: unknown;
  function?: {
    name?: unknown;
  };
}

export interface AgentCapabilityToolView {
  name?: unknown;
  exposure?: unknown;
  direct?: unknown;
  schema_available?: unknown;
  toolset?: unknown;
  capability?: unknown;
  permission?: unknown;
  read_only?: unknown;
  short_description?: unknown;
}

export interface AgentCapabilityNamedItem {
  name?: unknown;
  command?: unknown;
  label?: unknown;
  description?: unknown;
  type?: unknown;
  enabled?: unknown;
  args?: unknown;
  display_name?: unknown;
  short_description?: unknown;
  icon?: unknown;
  icon_large?: unknown;
  brand_color?: unknown;
  version?: unknown;
  mcp_dependencies?: unknown;
  allow_implicit_invocation?: unknown;
  default_prompt?: unknown;
  source_level?: unknown;
  level?: unknown;
  active?: unknown;
  usage?: unknown;
}

export interface ToolViewSummary {
  total?: number;
  direct: string[];
  deferred: string[];
  hidden: string[];
  core?: number;
  hasViews: boolean;
}

export const capabilityHasInventory = (
  capabilities: AgentCapabilitiesPayload | undefined,
): boolean => (
  Array.isArray(capabilities?.tools)
  || Array.isArray(capabilities?.tool_views)
  || Array.isArray(capabilities?.commands)
  || Array.isArray(capabilities?.skills)
  || Array.isArray(capabilities?.composer_commands)
);

export const capabilityHasDetails = (
  capabilities: AgentCapabilitiesPayload | undefined,
): boolean => (
  capabilityHasInventory(capabilities) || Boolean(capabilities?.summary)
);

export const mergeCapabilities = (
  primary: AgentCapabilitiesPayload | undefined,
  fallback: AgentCapabilitiesPayload | undefined,
): AgentCapabilitiesPayload | undefined => {
  if (!primary) return withDerivedCapabilitySummary(fallback);
  if (!fallback) return withDerivedCapabilitySummary(primary);
  return withDerivedCapabilitySummary({
    ...fallback,
    ...primary,
    summary: primary.summary ?? fallback.summary,
    tools: primary.tools ?? fallback.tools,
    tool_views: primary.tool_views ?? fallback.tool_views,
    commands: primary.commands ?? fallback.commands,
    skills: primary.skills ?? fallback.skills,
    composer_commands: primary.composer_commands ?? fallback.composer_commands,
    feature_flags: primary.feature_flags ?? fallback.feature_flags,
  });
};

const finiteNumber = (value: unknown): number | null => (
  typeof value === "number" && Number.isFinite(value) ? value : null
);

const arrayLength = (items: unknown[] | undefined): number | undefined => (
  Array.isArray(items) ? items.length : undefined
);

export const withDerivedCapabilitySummary = (
  capabilities: AgentCapabilitiesPayload | undefined,
): AgentCapabilitiesPayload | undefined => {
  if (!capabilities) return undefined;
  const summary = capabilities.summary;
  const exposure = summarizeToolViews(capabilities.tool_views);
  const toolsTotal = finiteNumber(summary?.tools_total) ?? (exposure.hasViews ? exposure.total : arrayLength(capabilities.tools));
  const directTools = finiteNumber(summary?.direct_tools) ?? (exposure.hasViews ? exposure.direct.length : undefined);
  const coreTools = finiteNumber(summary?.core_tools) ?? exposure.core;
  const deferredTools = finiteNumber(summary?.deferred_tools) ?? (exposure.hasViews ? exposure.deferred.length : undefined);
  const hiddenTools = finiteNumber(summary?.hidden_tools) ?? (exposure.hasViews ? exposure.hidden.length : undefined);
  const commands = finiteNumber(summary?.commands) ?? arrayLength(capabilities.commands);
  const skills = finiteNumber(summary?.skills) ?? arrayLength(capabilities.skills);
  if (
    summary
    || toolsTotal != null
    || directTools != null
    || coreTools != null
    || deferredTools != null
    || hiddenTools != null
    || commands != null
    || skills != null
  ) {
    return {
      ...capabilities,
      summary: {
        ...summary,
        ...(toolsTotal != null ? { tools_total: toolsTotal } : {}),
        ...(directTools != null ? { direct_tools: directTools } : {}),
        ...(coreTools != null ? { core_tools: coreTools } : {}),
        ...(deferredTools != null ? { deferred_tools: deferredTools } : {}),
        ...(hiddenTools != null ? { hidden_tools: hiddenTools } : {}),
        ...(commands != null ? { commands } : {}),
        ...(skills != null ? { skills } : {}),
      },
    };
  }
  return capabilities;
};

export const formatCapabilitySource = (source: CapabilitySource | undefined): string => {
  if (source === "doctor") return "Doctor";
  if (source === "status") return "Status fallback";
  if (source === "runtime") return "Runtime";
  return "Unknown";
};

export const formatAgentToolCounts = (summary: AgentCapabilitySummary | undefined): string => {
  const total = finiteNumber(summary?.tools_total);
  const direct = finiteNumber(summary?.direct_tools);
  if (total == null && direct == null) return "Unknown";
  if (total == null) return `${direct} direct`;
  if (direct == null) return `${total} total`;
  return `${total} total / ${direct} direct`;
};

export const formatDeferredCapability = (summary: AgentCapabilitySummary | undefined): string => {
  const label = capabilityFlagLabel(summary?.deferred_bridge);
  const count = finiteNumber(summary?.deferred_tools);
  if (summary?.deferred_bridge === undefined && count != null) return `${count} deferred`;
  return count == null ? label : `${label} (${count})`;
};

export const formatSkillCapability = (summary: AgentCapabilitySummary | undefined): string => {
  const label = capabilityFlagLabel(summary?.skill_catalog);
  const count = finiteNumber(summary?.skills);
  if (summary?.skill_catalog === undefined && count != null) return `${count} ${count === 1 ? "skill" : "skills"}`;
  return count == null ? label : `${label} (${count})`;
};

export const formatMcpProxyCount = (summary: AgentCapabilitySummary | undefined): string => {
  const count = finiteNumber(summary?.mcp_proxy_tools);
  if (count == null) return "Unknown";
  return count === 1 ? "1 dynamic tool" : `${count} dynamic tools`;
};

export const formatExposureBreakdown = (summary: AgentCapabilitySummary | undefined): string => {
  const core = finiteNumber(summary?.core_tools);
  const deferred = finiteNumber(summary?.deferred_tools);
  const hidden = finiteNumber(summary?.hidden_tools);
  if (core == null && deferred == null && hidden == null) return "Unknown";
  return `${core ?? 0} core / ${deferred ?? 0} deferred / ${hidden ?? 0} hidden`;
};

export const formatInventoryCount = (
  items: unknown[] | undefined,
  summaryCount: number | undefined,
  singular: string,
  plural: string,
): string => {
  const count = finiteNumber(summaryCount) ?? (Array.isArray(items) ? items.length : null);
  if (count == null) return "Unknown";
  return `${count} ${count === 1 ? singular : plural}`;
};

export const capabilityFlagLabel = (ready: boolean | undefined): string => {
  if (ready === true) return "Ready";
  if (ready === false) return "Missing";
  return "Unknown";
};

export type ProviderCapabilityTone = "ready" | "missing" | "unavailable" | "unknown";

const providerCapabilityValue = (
  supported: boolean | undefined,
  required = false,
): { value: string; tone: ProviderCapabilityTone } => {
  if (supported === true) return { value: "Ready", tone: "ready" };
  if (supported === false && required) return { value: "Missing", tone: "missing" };
  if (supported === false) return { value: "Unavailable", tone: "unavailable" };
  return { value: "Unknown", tone: "unknown" };
};

const capabilityBool = (value: unknown): boolean | undefined => {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const text = value.trim().toLowerCase();
    if (["1", "true", "yes", "on"].includes(text)) return true;
    if (["0", "false", "no", "off"].includes(text)) return false;
  }
  return undefined;
};

const capabilityText = (value: unknown): string => (
  typeof value === "string" ? value.trim() : ""
);

export const formatProviderCapabilityTitle = (
  capabilities: AgentProviderCapabilities | undefined,
): string => {
  if (!capabilities) return "Provider capability unknown";
  const provider = capabilityText(capabilities.provider) || "provider";
  const model = capabilityText(capabilities.model) || "model";
  const wireApi = capabilityText(capabilities.wire_api);
  return wireApi ? `${provider} / ${model} / ${wireApi}` : `${provider} / ${model}`;
};

export const providerCapabilityRows = (
  capabilities: AgentProviderCapabilities | undefined,
): { label: string; value: string; supported?: boolean; tone: ProviderCapabilityTone }[] => {
  if (!capabilities) return [];
  const row = (label: string, supported: boolean | undefined, required = false) => ({
    label,
    supported,
    ...providerCapabilityValue(supported, required),
  });
  return [
    row("Streaming", capabilityBool(capabilities.streaming), true),
    row("Tool calling", capabilityBool(capabilities.tool_calling), true),
    row("Parallel tools", capabilityBool(capabilities.parallel_tool_calls)),
    row("Stateful", capabilityBool(capabilities.stateful_continuation)),
    row("Reasoning effort", capabilityBool(capabilities.reasoning_effort)),
    row("JSON mode", capabilityBool(capabilities.json_mode)),
    row("Vision", capabilityBool(capabilities.vision)),
    row("Native PDF", capabilityBool(capabilities.native_pdf)),
    row("Image generation", capabilityBool(capabilities.image_generation)),
  ];
};

export const providerCapabilityLimitations = (
  capabilities: AgentProviderCapabilities | undefined,
): string[] => {
  const limitations = capabilities?.limitations;
  if (!Array.isArray(limitations)) return [];
  return limitations
    .map((item) => formatProviderCapabilityLimitation(String(item ?? "").trim()))
    .filter(Boolean);
};

const formatProviderCapabilityLimitation = (value: string): string => {
  if (value === "stateful_continuation_requires_responses_api") {
    return "Stateful continuation requires Responses API";
  }
  if (value === "gpt_like_chat_completions_no_stateful_continuation") {
    return "GPT-like models on Chat Completions cannot use stateful continuation; use Responses to enable previous_response_id";
  }
  if (value === "image_generation_model_requires_responses_api") {
    return "Image generation models require Responses API";
  }
  if (value === "known_text_only_image_provider") {
    return "This provider/model is text-only for image inputs";
  }
  if (value.startsWith("unsupported_openai_wire_api:")) {
    return `Unsupported API format: ${value.slice("unsupported_openai_wire_api:".length)}`;
  }
  return value.replace(/_/g, " ");
};

export const capabilityFeatureEnabled = (
  capabilities: AgentCapabilitiesPayload | null | undefined,
  name: string,
  defaultEnabled = true,
): boolean => {
  const flags = capabilities?.feature_flags;
  if (!flags || typeof flags !== "object") return defaultEnabled;
  const flag = flags[name];
  if (!flag || typeof flag !== "object") return defaultEnabled;
  return typeof flag.enabled === "boolean" ? flag.enabled : defaultEnabled;
};

const cleanCapabilityName = (value: unknown): string => (
  typeof value === "string" ? value.trim() : ""
);

export const capabilityToolNames = (tools: AgentCapabilityTool[] | undefined): string[] => {
  if (!Array.isArray(tools)) return [];
  return tools
    .map((tool) => cleanCapabilityName(tool.function?.name) || cleanCapabilityName(tool.name))
    .filter((name) => name.length > 0);
};

export const capabilityItemNames = (items: AgentCapabilityNamedItem[] | undefined): string[] => {
  if (!Array.isArray(items)) return [];
  return items
    .map((item) => cleanCapabilityName(item.command) || cleanCapabilityName(item.name))
    .filter((name) => name.length > 0);
};

export const formatCapabilityPreview = (names: string[], limit = 4): string => {
  if (!names.length) return "None";
  const visible = names.slice(0, limit).join(", ");
  const remaining = names.length - limit;
  return remaining > 0 ? `${visible}, +${remaining} more` : visible;
};

export const summarizeToolViews = (
  toolViews: AgentCapabilityToolView[] | undefined,
): ToolViewSummary => {
  if (!Array.isArray(toolViews)) {
    return { total: undefined, direct: [], deferred: [], hidden: [], core: undefined, hasViews: false };
  }
  const direct: string[] = [];
  const deferred: string[] = [];
  const hidden: string[] = [];
  let core = 0;

  toolViews.forEach((view) => {
    const name = cleanCapabilityName(view.name);
    if (!name) return;
    const exposure = cleanCapabilityName(view.exposure).toLowerCase();
    const isHidden = exposure === "hidden" || view.schema_available === false;
    const isDirect = view.direct === true && !isHidden;
    if (exposure === "core") core += 1;
    if (isHidden) {
      hidden.push(name);
    } else if (isDirect) {
      direct.push(name);
    } else {
      deferred.push(name);
    }
  });

  return { total: toolViews.length, direct, deferred, hidden, core, hasViews: true };
};
