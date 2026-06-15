export type CapabilitySource = "doctor" | "status" | "unknown";

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
  commands?: AgentCapabilityNamedItem[];
  skills?: AgentCapabilityNamedItem[];
  composer_commands?: AgentCapabilityNamedItem[];
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
  skill_bridge?: boolean;
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
  description?: unknown;
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
  const toolsTotal = finiteNumber(summary?.tools_total) ?? arrayLength(capabilities.tools) ?? exposure.total;
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
  const label = capabilityFlagLabel(summary?.skill_bridge);
  const count = finiteNumber(summary?.skills);
  if (summary?.skill_bridge === undefined && count != null) return `${count} ${count === 1 ? "skill" : "skills"}`;
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
