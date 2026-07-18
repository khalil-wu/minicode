import type {
  PromptCacheDiagnostic,
  PromptSectionDelta,
  PromptSectionSummary,
  PromptSectionSummaryRow,
  ProviderRawMetadata,
  ProviderRawOutputItem,
  ProviderTimelineEvent,
} from "../stores/types";
import { promptCacheHitRate } from "./cacheUsage";

export interface ProviderUsageSummary {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  promptCacheTotal?: number;
  promptCacheHitRate?: number;
  reasoning: number;
  provider?: string;
}

export interface ProviderTraceExportPackage {
  kind: "minicode_provider_trace_export";
  exported_at: string;
  provider: string;
  model: string;
  finish_reason: string;
  event_type: string;
  usage: ProviderUsageSummary;
  cache_hit_rate: number | null;
  continuation_summary: string;
  output_sequence: string;
  output_phase_counts: string;
  response_lifecycle: string;
  provider_timeline_sequence: string;
  provider_timeline_event_counts: string;
  request_summary: ProviderRawMetadata["request_summary"];
  stateful_continuation?: ProviderRawMetadata["stateful_continuation"];
  loop_metrics?: ProviderRawMetadata["loop_metrics"];
  output_items: ProviderRawMetadata["output_items"];
  provider_timeline: ProviderRawMetadata["provider_timeline"];
  safety: ProviderRawMetadata["safety"];
  prompt_cache_diagnostic?: ProviderRawMetadata["prompt_cache_diagnostic"];
  diagnostics: string[];
  request_diff_summary?: string[];
}

export interface ProviderSafeRequestPackage {
  kind: "minicode_provider_safe_request";
  provider: string;
  model: string;
  wire_api: string;
  endpoint: string;
  request_params: Record<string, unknown>;
  prompt: {
    redacted: true;
    instructions_len?: number;
    instructions_sent_len?: number;
    instructions_omitted_by_continuation?: boolean;
    instructions_hash?: string;
    instructions_full_hash?: string;
  };
  tools: {
    redacted: true;
    tools_len?: number;
    tools_chars?: number;
    tools_hash?: string;
    tool_names?: string[];
    tool_schema_hashes?: Record<string, string>;
    largest_tools?: ProviderRawMetadata["request_summary"] extends infer Summary
      ? Summary extends { largest_tools?: infer Largest }
        ? Largest
        : unknown
      : unknown;
  };
  input: {
    redacted: true;
    input_items_len?: number;
    input_items_sent_len?: number;
    input_items_omitted_by_continuation?: number;
    input_items_logical_len?: number;
    input_chars?: number;
    input_item_counts?: Record<string, number>;
    largest_input_items?: ProviderRawMetadata["request_summary"] extends infer Summary
      ? Summary extends { largest_input_items?: infer Largest }
        ? Largest
        : unknown
      : unknown;
    duplicate_input_content?: ProviderRawMetadata["request_summary"] extends infer Summary
      ? Summary extends { duplicate_input_content?: infer Duplicates }
        ? Duplicates
        : unknown
      : unknown;
    previous_response_id_present?: boolean;
    previous_response_id_hash?: string;
  };
  metadata: {
    redacted: true;
    keys?: string[];
  };
  note: string;
}

export interface ProviderTimelineRow {
  event: string;
  detail: string;
  tone: "muted" | "accent" | "warning";
}

export interface PromptSectionDeltaSummary {
  overview: string;
  layerSummary: string;
  changedSections: string;
}

export const providerLoopMetricsSummary = (raw?: ProviderRawMetadata | null): string => {
  const metrics = raw?.loop_metrics ?? {};
  const providerCalls = numberField(metrics.provider_call_count);
  const iteration = numberField(metrics.iteration);
  const iterationLimit = numberField(metrics.iteration_limit);
  const hardLimit = numberField(metrics.iteration_hard_limit);
  const toolBatches = numberField(metrics.tool_batch_count);
  const tools = numberField(metrics.tool_call_count);
  const elapsedMs = numberField(metrics.elapsed_ms);
  const parts = [
    providerCalls > 0 ? `${providerCalls} provider call${providerCalls === 1 ? "" : "s"}` : "provider calls n/a",
    iterationLimit > 0 ? `iter ${iteration}/${iterationLimit}${hardLimit > iterationLimit ? `/${hardLimit}` : ""}` : iteration > 0 ? `iter ${iteration}` : "iter n/a",
    `${toolBatches} tool batch${toolBatches === 1 ? "" : "es"}`,
    `${tools} tool${tools === 1 ? "" : "s"}`,
    elapsedMs > 0 ? `${elapsedMs}ms` : "elapsed n/a",
  ];
  return parts.join(" · ");
};

const numberField = (value: unknown): number => {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
};

const optionalNumberField = (value: unknown): number | undefined => {
  if (value == null) return undefined;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : undefined;
};

const recordField = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.map((item) => String(item ?? "").trim()).filter(Boolean)
    : [];

const SENSITIVE_EXPORT_KEY_RE = /^(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|password|cookie|set-cookie)$/i;
const PROMPT_CONTENT_EXPORT_KEY_RE = /^(?:instructions|system|system_prompt|developer_prompt|prompt|encrypted_content|content|text|arguments)$/i;

const shouldOmitExportKey = (key: string): boolean =>
  SENSITIVE_EXPORT_KEY_RE.test(key) || PROMPT_CONTENT_EXPORT_KEY_RE.test(key);

const sanitizedMetadataKeys = (value: unknown): unknown => {
  if (!Array.isArray(value)) return value;
  return value.filter((item) => typeof item !== "string" || !shouldOmitExportKey(item));
};

export const sanitizeProviderTraceExportValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map((item) => sanitizeProviderTraceExportValue(item));
  if (!value || typeof value !== "object") return value;
  const next: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (shouldOmitExportKey(key)) continue;
    next[key] = sanitizeProviderTraceExportValue(key === "metadata_keys" ? sanitizedMetadataKeys(item) : item);
  }
  return next;
};

export const providerUsageSummary = (raw?: ProviderRawMetadata | null): ProviderUsageSummary => {
  const usage = raw?.usage ?? {};
  const promptDetails = recordField(usage.prompt_tokens_details);
  const completionDetails = recordField(usage.completion_tokens_details);
  const deepSeekCacheHit = numberField(usage.prompt_cache_hit_tokens);
  const deepSeekCacheMiss = numberField(usage.prompt_cache_miss_tokens);
  const deepSeekPromptTotal =
    deepSeekCacheHit > 0 || deepSeekCacheMiss > 0
      ? deepSeekCacheHit + deepSeekCacheMiss
      : undefined;
  return {
    input: numberField(usage.input_tokens ?? usage.prompt_tokens ?? deepSeekPromptTotal ?? usage.input),
    output: numberField(usage.output_tokens ?? usage.completion_tokens ?? usage.output),
    cacheRead: numberField(
      usage.cache_read_input_tokens
        ?? usage.cached_prompt_tokens
        ?? usage.prompt_cache_hit_tokens
        ?? promptDetails.cached_tokens
        ?? usage.cacheRead,
    ),
    cacheWrite: numberField(usage.cache_creation_input_tokens ?? usage.cacheWrite),
    promptCacheTotal: optionalNumberField(usage.prompt_cache_total_tokens ?? deepSeekPromptTotal ?? usage.promptCacheTotal),
    promptCacheHitRate: optionalNumberField(usage.prompt_cache_hit_rate ?? usage.promptCacheHitRate),
    reasoning: numberField(usage.reasoning_output_tokens ?? completionDetails.reasoning_tokens ?? usage.reasoning),
    provider: raw?.provider,
  };
};

export const providerCacheHitRate = (usage: ProviderUsageSummary): number | null => {
  return promptCacheHitRate(usage);
};

export const providerCacheDiagnosis = (raw?: ProviderRawMetadata | null): string => {
  const summary = raw?.request_summary ?? {};
  const usage = providerUsageSummary(raw);
  const hit = providerCacheHitRate(usage);
  const cacheState = summary.prompt_cache_key_present
    ? `prompt cache key ${summary.prompt_cache_key_hash || "present"}`
    : "no prompt cache key";
  const hitState = hit == null ? "no cache read" : `${hit}% hit`;
  const stability = [
    summary.instructions_hash ? `prompt ${summary.instructions_hash}` : "prompt untracked",
    summary.tools_hash ? `tools ${summary.tools_hash}` : "tools untracked",
  ].join(" · ");
  return `${cacheState} · ${hitState} · ${stability}`;
};

export const providerContinuationLabel = (raw?: ProviderRawMetadata | null): string => {
  const summary = raw?.request_summary ?? {};
  return summary.previous_response_id_present
    ? `stateful continuation ${summary.previous_response_id_hash || "present"}`
    : "full request / no previous response";
};

export const providerContinuationDetail = (raw?: ProviderRawMetadata | null): string => {
  const summary = raw?.request_summary ?? {};
  if (!summary.previous_response_id_present) return "No stateful continuation";
  const hash = summary.previous_response_id_hash || "present";
  const counts = summary.input_item_counts ?? {};
  const inputLen = numberField(summary.input_items_len);
  const toolOutputs = numberField(counts.function_call_output);
  const nonToolInput = Object.entries(counts)
    .filter(([key, value]) => key !== "function_call_output" && numberField(value) > 0)
    .map(([key, value]) => `${key} ${numberField(value)}`)
    .join(", ");
  if (inputLen <= 0) return `Continuation ${hash} with empty captured input`;
  if (toolOutputs > 0 && !nonToolInput) return `Continuation ${hash} with tool outputs only (${toolOutputs})`;
  return `Continuation ${hash} with ${inputLen} captured input items${nonToolInput ? ` (${nonToolInput})` : ""}`;
};

export const providerInstructionsTransportSummary = (raw?: ProviderRawMetadata | null): string => {
  const summary = raw?.request_summary ?? {};
  const total = numberField(summary.instructions_len);
  const sent = numberField(summary.instructions_sent_len);
  if (total <= 0) return "no instructions";
  if (summary.instructions_omitted_by_continuation === true) {
    return `sent 0/${total} chars via previous_response_id`;
  }
  if (sent > 0 && sent !== total) return `sent ${sent}/${total} chars`;
  if (sent > 0) return `sent ${sent} chars`;
  return `tracked ${total} chars`;
};

export const providerStatefulContinuationSummary = (raw?: ProviderRawMetadata | null): string => {
  const stateful = raw?.stateful_continuation;
  if (!stateful?.configured) return "stateful not configured";
  if (stateful.enabled === false) {
    return stateful.disabled_reason
      ? `stateful disabled (${stateful.disabled_reason})`
      : "stateful disabled";
  }
  if (stateful.reset_reason) return `stateful reset (${stateful.reset_reason})`;
  if (stateful.used) {
    const omitted = numberField(stateful.input_items_omitted);
    return omitted > 0 ? `stateful used, omitted ${omitted} input items` : "stateful used";
  }
  if (stateful.stored_response_id_hash) {
    const covered = numberField(stateful.covered_items);
    return covered > 0 ? `stateful stored, covered ${covered} items` : "stateful stored";
  }
  return stateful.enabled ? "stateful ready" : "stateful pending";
};

export const providerRequestModeSummary = (raw?: ProviderRawMetadata | null): string => {
  const summary = raw?.request_summary ?? {};
  const params = recordField(summary.request_params);
  const wire = String(summary.wire_api || raw?.provider || "unknown").trim() || "unknown";
  const stateful = raw?.stateful_continuation?.configured
    ? providerStatefulContinuationSummary(raw)
    : summary.previous_response_id_present
    ? "stateful used"
    : params.store === true
      ? "stateful ready"
      : "stateful off";
  const retention = typeof params.prompt_cache_retention === "string" && params.prompt_cache_retention.trim()
    ? `retention ${params.prompt_cache_retention.trim()}`
    : "retention off";
  const store = "store" in params ? `store ${String(params.store)}` : "store n/a";
  return `${wire} · ${stateful} · ${retention} · ${store}`;
};

export const providerTraceDiagnostics = (raw?: ProviderRawMetadata | null): string[] => {
  if (!raw) return ["missing provider trace"];
  const diagnostics: string[] = [];
  const summary = raw.request_summary ?? {};
  const metrics = raw.loop_metrics ?? {};
  const outputItems = Array.isArray(raw.output_items) ? raw.output_items : [];
  const timeline = Array.isArray(raw.provider_timeline) ? raw.provider_timeline : [];
  const usage = providerUsageSummary(raw);
  const hit = providerCacheHitRate(usage);
  const providerCalls = numberField(metrics.provider_call_count);
  const toolBatches = numberField(metrics.tool_batch_count);
  const toolCalls = numberField(metrics.tool_call_count);
  const iterationLimit = numberField(metrics.iteration_limit);
  const iterationHardLimit = numberField(metrics.iteration_hard_limit);
  const inputChars = numberField(summary.input_chars);
  const largestInputItems = Array.isArray(summary.largest_input_items) ? summary.largest_input_items : [];
  const duplicateInputContent = Array.isArray(summary.duplicate_input_content) ? summary.duplicate_input_content : [];
  const hasMessage = outputItems.some((item) => item.type === "message");
  const hasToolCall = outputItems.some((item) => item.type === "function_call" || item.type === "web_search_call");
  const hasReasoning = outputItems.some((item) => item.type === "reasoning");
  const commentaryCount = outputItems.filter((item) => item.type === "message" && item.phase === "commentary").length;
  const finalAnswerCount = outputItems.filter((item) => item.type === "message" && item.phase === "final_answer").length;
  const finalAnswerIndex = outputItems.findIndex((item) => item.type === "message" && item.phase === "final_answer");
  const toolAfterFinal = finalAnswerIndex >= 0 && outputItems.slice(finalAnswerIndex + 1).some((item) => item.type === "function_call" || item.type === "web_search_call");
  const hasCompletionEvent = timeline.some((item) => /completed|incomplete|done|finish|failed|error|cancel|abort|interrupt/i.test(item.event));
  const hasProblemLifecycle = timeline.some((item) =>
    /response\.(incomplete|failed|error)/i.test(item.event) ||
    /incomplete|failed|error|cancel|abort|interrupt/i.test(String(item.status ?? item.finish_reason ?? "")),
  );
  const maybeAborted = /cancel|abort|interrupt/i.test(`${raw.event_type ?? ""} ${raw.finish_reason ?? ""}`);
  const continuationToolOnly =
    summary.previous_response_id_present &&
    numberField(summary.input_item_counts?.function_call_output) > 0 &&
    Object.entries(summary.input_item_counts ?? {}).every(([key, value]) => key === "function_call_output" || numberField(value) <= 0);
  const model = String(raw.model || summary.model || "").toLowerCase();
  const wireApi = String(summary.wire_api || "").toLowerCase();
  const isGptLike = /(^|[/:-])gpt-|codex/.test(model);
  const requestParams = recordField(summary.request_params);
  const toolNames = stringList(summary.tool_names);

  if (!outputItems.length) diagnostics.push("no output_items summary");
  if (!hasMessage && !hasToolCall) diagnostics.push("no visible message or tool call item");
  if (hasToolCall && commentaryCount === 0 && !hasReasoning) diagnostics.push("tool call without commentary or reasoning context");
  if (finalAnswerCount === 1) diagnostics.push("final_answer phase present");
  if (finalAnswerCount > 1) diagnostics.push(`multiple final_answer phases: ${finalAnswerCount}`);
  if (toolAfterFinal) diagnostics.push("tool call appears after final_answer phase");
  if (continuationToolOnly) diagnostics.push("stateful continuation carries tool outputs only");
  if (isGptLike && wireApi === "chat") {
    diagnostics.push("GPT-like model is using chat completions; switch this provider to Responses to enable previous_response_id continuation");
  }
  if (wireApi === "responses" && summary.prompt_cache_key_present === false) {
    diagnostics.push("Responses request missing prompt_cache_key; stable prompt cache routing is disabled");
  }
  if (isGptLike && wireApi === "responses" && requestParams.store !== true && !summary.previous_response_id_present) {
    diagnostics.push("Responses request not stored; previous_response_id continuation cannot be used on the next turn");
  }
  if (toolNames.length === 1 && toolNames[0] === "minicode_app") {
    diagnostics.push("single minicode_app bridge tool detected; current backend tool/cache path may be bypassed");
  }
  if (raw.stateful_continuation?.configured && raw.stateful_continuation.enabled === false) {
    diagnostics.push(
      raw.stateful_continuation.disabled_reason
        ? `stateful continuation disabled: ${raw.stateful_continuation.disabled_reason}`
        : "stateful continuation disabled by provider request shape",
    );
  }
  if (hasToolCall && raw.finish_reason && !/tool|stop|completed|end/i.test(raw.finish_reason)) {
    diagnostics.push(`tool call with unusual finish reason: ${raw.finish_reason}`);
  }
  if (hasProblemLifecycle) diagnostics.push("provider lifecycle has incomplete/error boundary");
  if (summary.turn_aborted_marker_present) diagnostics.push("turn_aborted marker present in captured input");
  if (maybeAborted) diagnostics.push("turn may have been aborted or interrupted");
  if (!hasCompletionEvent) diagnostics.push("timeline has no completion event");
  if (usage.input <= 0 && usage.output <= 0 && usage.reasoning <= 0) diagnostics.push("usage is missing or zero");
  if (inputChars >= 24_000) diagnostics.push(`large provider input payload: ${inputChars} chars`);
  if (largestInputItems.some((item) => ["system", "developer"].includes(String(item?.role || "").toLowerCase()))) {
    diagnostics.push("instruction-role item appears in provider input payload");
  }
  if (duplicateInputContent.length > 0) {
    diagnostics.push("duplicate provider input content detected");
  }
  if (duplicateInputContent.some((item) => ["system", "developer"].includes(String(item?.role || "").toLowerCase()))) {
    diagnostics.push("duplicate instruction-role content in provider input payload");
  }
  if (raw.safety?.has_encrypted_reasoning) diagnostics.push("encrypted reasoning present; content redacted");
  if (raw.safety?.synthetic_trace || raw.event_type === "synthetic.tool_calls_no_done") {
    diagnostics.push("synthetic provider trace: stream ended without provider DONE");
  }
  if (raw.prompt_cache_diagnostic?.prompt_section_delta?.status === "changed") {
    diagnostics.push("prompt section delta captured");
  }
  if (providerCalls >= 6) diagnostics.push(`high provider-call count: ${providerCalls}`);
  if (toolBatches >= 5) diagnostics.push(`high tool-batch count: ${toolBatches}`);
  if (toolCalls >= 20) diagnostics.push(`high tool-call count: ${toolCalls}`);
  if (hit != null && hit >= 80 && (providerCalls >= 4 || toolBatches >= 4)) {
    diagnostics.push("cache hit is high; latency is likely loop/tool-bound");
  }
  if (metrics.dynamic_iteration_budget_enabled && iterationLimit > 0 && iterationHardLimit > iterationLimit) {
    diagnostics.push(`dynamic iteration window active: ${iterationLimit}/${iterationHardLimit}`);
  }
  if (diagnostics.length === 0) diagnostics.push("trace contract looks healthy");
  return diagnostics;
};

export const providerPromptSectionSummary = (summary?: PromptSectionSummary): string => {
  if (!summary || typeof summary !== "object") return "No prompt section summary";
  const totalChars = numberField(summary.total_chars);
  const totalSections = numberField(summary.section_count);
  const layers = recordField(summary.layers);
  const layerParts = (["stable", "context", "volatile"] as const)
    .map((layer) => {
      const info = recordField(layers[layer]);
      const chars = numberField(info.chars);
      const sections = numberField(info.sections);
      if (chars <= 0 && sections <= 0) return "";
      const cacheBreakSections = numberField(info.cache_break_sections);
      const suffix = cacheBreakSections > 0 ? `, ${cacheBreakSections} cache-break` : "";
      return `${layer} ${chars} chars / ${sections} sections${suffix}`;
    })
    .filter(Boolean);
  return `${totalSections} sections · ${totalChars} chars${layerParts.length ? ` · ${layerParts.join(" · ")}` : ""}`;
};

const promptSectionRowLabel = (row?: PromptSectionSummaryRow): string => {
  if (!row || typeof row !== "object") return "";
  const name = String(row.name || "section").trim();
  const layer = String(row.layer || "unknown").trim();
  const chars = numberField(row.chars);
  const cacheBreak = row.cache_break ? " cache-break" : "";
  return `${name} (${layer}, ${chars} chars${cacheBreak})`;
};

export const providerPromptLargestSections = (summary?: PromptSectionSummary, limit = 3): string => {
  if (!summary || typeof summary !== "object" || !Array.isArray(summary.largest_sections) || summary.largest_sections.length === 0) {
    return "No largest-section summary";
  }
  const visible = summary.largest_sections
    .slice(0, limit)
    .map((row) => `${String(row.name || "section")} (${String(row.layer || "unknown")}, ${numberField(row.chars)} chars)`);
  return visible.join(" · ");
};

export const providerLargestToolsSummary = (summary?: ProviderRawMetadata["request_summary"], limit = 3): string => {
  if (!summary || typeof summary !== "object" || !Array.isArray(summary.largest_tools) || summary.largest_tools.length === 0) {
    return "No tool-size summary";
  }
  const visible = summary.largest_tools
    .slice(0, limit)
    .map((row) => `${String(row?.name || "tool")} (${numberField(row?.chars)} chars)`);
  return visible.join(" · ");
};

export const providerLargestInputItemsSummary = (summary?: ProviderRawMetadata["request_summary"], limit = 3): string => {
  if (!summary || typeof summary !== "object" || !Array.isArray(summary.largest_input_items) || summary.largest_input_items.length === 0) {
    return "No input-size summary";
  }
  const visible = summary.largest_input_items
    .slice(0, limit)
    .map((row) => {
      const role = row?.role ? `:${row.role}` : "";
      const name = row?.name ? `:${row.name}` : "";
      return `#${numberField(row?.index)} ${String(row?.type || "item")}${role}${name} (${numberField(row?.chars)} chars)`;
    });
  return visible.join(" · ");
};

export const providerDuplicateInputSummary = (summary?: ProviderRawMetadata["request_summary"], limit = 3): string => {
  if (!summary || typeof summary !== "object" || !Array.isArray(summary.duplicate_input_content) || summary.duplicate_input_content.length === 0) {
    return "No duplicate input content";
  }
  const visible = summary.duplicate_input_content
    .slice(0, limit)
    .map((row) => {
      const role = row?.role ? `:${row.role}` : "";
      const count = numberField(row?.count);
      return `${String(row?.type || "item")}${role} x${count} (${numberField(row?.chars)} chars)`;
    });
  return visible.join(" · ");
};

export const providerPromptSectionDeltaSummary = (delta?: PromptSectionDelta | null): PromptSectionDeltaSummary => {
  if (!delta || typeof delta !== "object" || delta.status !== "changed") {
    return {
      overview: "Prompt sections unchanged",
      layerSummary: "No layer delta",
      changedSections: "No section changes",
    };
  }
  const added = stringList(delta.added);
  const removed = stringList(delta.removed);
  const changedSections = Array.isArray(delta.changed_sections) ? delta.changed_sections : [];
  const overviewParts = [
    added.length ? `added ${added.join(", ")}` : "",
    removed.length ? `removed ${removed.join(", ")}` : "",
    changedSections.length ? `changed ${changedSections.length} section${changedSections.length === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  const layerCharDeltas = recordField(delta.layer_char_deltas);
  const layerSummary = (["stable", "context", "volatile"] as const)
    .map((layer) => {
      const value = Number(layerCharDeltas[layer] ?? 0);
      if (!Number.isFinite(value) || value === 0) return "";
      return `${layer} ${value > 0 ? "+" : ""}${value} chars`;
    })
    .filter(Boolean)
    .join(" · ") || "No layer delta";
  const changedLabel = changedSections
    .slice(0, 4)
    .map((row) => {
      if (!row || typeof row !== "object") return "";
      const name = String(row.name || "section").trim();
      const changes = stringList(row.changes);
      const charDelta = Number(row.chars_delta ?? 0);
      const deltaText = Number.isFinite(charDelta) && charDelta !== 0 ? `, ${charDelta > 0 ? "+" : ""}${charDelta} chars` : "";
      return `${name}${changes.length ? ` [${changes.join(", ")}${deltaText}]` : deltaText}`;
    })
    .filter(Boolean)
    .join(" · ") || "No section changes";
  return {
    overview: overviewParts.join(" · ") || "Prompt sections changed",
    layerSummary,
    changedSections: changedLabel,
  };
};

export const providerPromptCacheDiagnosticSummary = (diagnostic?: PromptCacheDiagnostic | null): string => {
  if (!diagnostic || typeof diagnostic !== "object") return "No cache-break diagnostic";
  const reason = String(diagnostic.reason || "").trim() || "cache diagnostic";
  const tokenDrop = numberField(diagnostic.token_drop);
  return tokenDrop > 0 ? `${reason} · token drop ${tokenDrop}` : reason;
};

const itemLabel = (item: ProviderRawOutputItem): string => {
  if (item.type === "message") {
    const role = item.role ? `:${item.role}` : "";
    const phase = item.phase ? `:${item.phase}` : "";
    const contentTypes = item.content_types?.length ? `:${item.content_types.join("+")}` : "";
    return `message${role}${phase}${contentTypes}`;
  }
  if (item.type === "function_call") {
    return `function_call${item.name ? `:${item.name}` : ""}`;
  }
  if (item.type === "web_search_call") {
    return `web_search_call${item.action_type ? `:${item.action_type}` : ""}`;
  }
  if (item.type === "reasoning") {
    return item.has_encrypted_content ? "reasoning:encrypted" : "reasoning";
  }
  return item.type || "item";
};

export const providerOutputSequence = (items?: ProviderRawOutputItem[]): string =>
  Array.isArray(items) && items.length ? items.map(itemLabel).join(" -> ") : "No output items";

export const providerOutputPhaseCounts = (items?: ProviderRawOutputItem[], limit = 8): string => {
  if (!Array.isArray(items) || !items.length) return "No message phases";
  const counts = new Map<string, number>();
  for (const item of items) {
    if (item.type !== "message") continue;
    const phase = item.phase?.trim() || "unphased";
    counts.set(phase, (counts.get(phase) ?? 0) + 1);
  }
  if (!counts.size) return "No message phases";
  const entries = [...counts.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const visible = entries.slice(0, limit).map(([phase, count]) => `${phase} x${count}`);
  const hidden = entries.length - visible.length;
  return hidden > 0 ? `${visible.join(", ")}, +${hidden} more` : visible.join(", ");
};

const timelineStringField = (item: ProviderTimelineEvent, key: string): string => {
  const value = item[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
};

export const providerResponseLifecycle = (items?: ProviderTimelineEvent[]): string => {
  if (!Array.isArray(items) || !items.length) return "No response lifecycle";
  const boundaryEvents = ["response.created", "response.completed", "response.incomplete", "response.failed", "response.error"];
  const counts = new Map<string, number>();
  const responseIds = new Set<string>();
  for (const item of items) {
    if (boundaryEvents.includes(item.event)) counts.set(item.event, (counts.get(item.event) ?? 0) + 1);
    const responseIdHash = timelineStringField(item, "response_id_hash");
    if (responseIdHash) responseIds.add(responseIdHash);
  }
  const eventSummary = boundaryEvents
    .filter((event) => counts.has(event))
    .map((event) => `${event} x${counts.get(event)}`)
    .join(" -> ") || "no response boundary events";
  const ids = [...responseIds].slice(0, 4);
  const idSummary = ids.length ? `response ${ids.join(", ")}${responseIds.size > ids.length ? `, +${responseIds.size - ids.length} more` : ""}` : "id untracked";
  return `${eventSummary} · ${idSummary}`;
};

const timelineEventLabel = (item: ProviderTimelineEvent): string => {
  const details = [
    timelineStringField(item, "response_id_hash") ? `response:${timelineStringField(item, "response_id_hash")}` : "",
    item.item_type,
    item.name,
    item.status ? `status:${item.status}` : "",
    item.finish_reason ? `finish:${item.finish_reason}` : "",
    typeof item.delta_chars === "number" ? `+${item.delta_chars} chars` : "",
    typeof item.arguments_chars === "number" ? `${item.arguments_chars} arg chars` : "",
    typeof item.output_items_len === "number" ? `${item.output_items_len} items` : "",
    typeof item.annotation_count === "number" ? `${item.annotation_count} citations` : "",
    typeof item.omitted === "number" ? `${item.omitted} omitted` : "",
  ].filter(Boolean);
  return details.length ? `${item.event}(${details.join(", ")})` : item.event;
};

export const providerTimelineSequence = (items?: ProviderTimelineEvent[], limit = 32): string => {
  if (!Array.isArray(items) || !items.length) return "No provider timeline";
  const visible = items.slice(0, limit).map(timelineEventLabel);
  const extra = items.length > limit ? [`... ${items.length - limit} more`] : [];
  return [...visible, ...extra].join(" -> ");
};

export const providerTimelineEventCounts = (items?: ProviderTimelineEvent[], limit = 8): string => {
  if (!Array.isArray(items) || !items.length) return "No provider events";
  const counts = new Map<string, number>();
  for (const item of items) {
    const event = item.event || "unknown";
    counts.set(event, (counts.get(event) ?? 0) + 1);
  }
  const entries = [...counts.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const visible = entries.slice(0, limit).map(([event, count]) => `${event} x${count}`);
  const hidden = entries.length - visible.length;
  return hidden > 0 ? `${visible.join(", ")}, +${hidden} more` : visible.join(", ");
};

export const providerTimelineRows = (items?: ProviderTimelineEvent[], limit = 24): ProviderTimelineRow[] => {
  if (!Array.isArray(items) || !items.length) return [];
  return items.slice(0, limit).map((item) => {
    const detail = [
      item.item_type ? `item ${item.item_type}` : "",
      item.name ? `tool ${item.name}` : "",
      item.status ? `status ${item.status}` : "",
      item.finish_reason ? `finish ${item.finish_reason}` : "",
      typeof item.delta_chars === "number" ? `+${item.delta_chars} chars` : "",
      typeof item.arguments_chars === "number" ? `${item.arguments_chars} arg chars` : "",
      typeof item.output_items_len === "number" ? `${item.output_items_len} output items` : "",
      item.usage_present ? "usage" : "",
      typeof item.omitted === "number" ? `${item.omitted} omitted` : "",
    ].filter(Boolean).join(" · ");
    const eventText = item.event || "unknown";
    const lower = `${eventText} ${item.status ?? ""} ${item.finish_reason ?? ""}`.toLowerCase();
    const tone = /failed|error|incomplete|abort|cancel|interrupt/.test(lower)
      ? "warning"
      : /completed|done|finish/.test(lower)
        ? "accent"
        : "muted";
    return {
      event: eventText,
      detail: detail || "event captured",
      tone,
    };
  });
};

export const providerInputDeltaSummary = (
  previous?: Record<string, number>,
  current?: Record<string, number>,
): string => {
  const keys = Array.from(new Set([...Object.keys(previous ?? {}), ...Object.keys(current ?? {})])).sort();
  const parts = keys
    .map((key) => {
      const diff = numberField(current?.[key]) - numberField(previous?.[key]);
      return diff === 0 ? "" : `${key} ${diff > 0 ? "+" : ""}${diff}`;
    })
    .filter(Boolean);
  return parts.join(", ");
};

export const providerToolDeltaSummary = (previous?: string[], current?: string[]): string => {
  const before = new Set(Array.isArray(previous) ? previous : []);
  const after = new Set(Array.isArray(current) ? current : []);
  const added = [...after].filter((name) => !before.has(name)).sort();
  const removed = [...before].filter((name) => !after.has(name)).sort();
  const parts = [
    ...added.map((name) => `+${name}`),
    ...removed.map((name) => `-${name}`),
  ];
  return parts.slice(0, 8).join(", ") + (parts.length > 8 ? `, +${parts.length - 8} more` : "");
};

export const providerToolSchemaDeltaSummary = (
  previous?: Record<string, string>,
  current?: Record<string, string>,
): string => {
  const before = previous ?? {};
  const after = current ?? {};
  const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)])).sort();
  const parts = keys
    .map((key) => {
      if (!(key in before)) return `+${key}`;
      if (!(key in after)) return `-${key}`;
      return before[key] === after[key] ? "" : `~${key}`;
    })
    .filter(Boolean);
  return parts.slice(0, 8).join(", ") + (parts.length > 8 ? `, +${parts.length - 8} more` : "");
};

export const providerParamsDeltaSummary = (
  previous?: Record<string, unknown>,
  current?: Record<string, unknown>,
): string => {
  const keys = Array.from(new Set([...Object.keys(previous ?? {}), ...Object.keys(current ?? {})])).sort();
  const changedKeys = keys.filter((key) => JSON.stringify(previous?.[key] ?? null) !== JSON.stringify(current?.[key] ?? null));
  return changedKeys.slice(0, 8).join(", ") + (changedKeys.length > 8 ? `, +${changedKeys.length - 8} more` : "");
};

const providerParamKeyDeltaSummary = (
  previous?: string[],
  current?: string[],
  options?: { ignore?: string[] },
): string => {
  const ignored = new Set(options?.ignore ?? []);
  const before = new Set(Array.isArray(previous) ? previous : []);
  const after = new Set(Array.isArray(current) ? current : []);
  const added = [...after].filter((key) => !ignored.has(key) && !before.has(key)).sort();
  const removed = [...before].filter((key) => !ignored.has(key) && !after.has(key)).sort();
  const parts = [
    ...added.map((key) => `+${key}`),
    ...removed.map((key) => `-${key}`),
  ];
  return parts.slice(0, 8).join(", ") + (parts.length > 8 ? `, +${parts.length - 8} more` : "");
};

export const providerRequestDiffSummary = (
  previous: ProviderRawMetadata["request_summary"] | undefined,
  current: ProviderRawMetadata["request_summary"] | undefined,
): string[] => {
  if (!previous) return ["No previous provider request"];
  const changed = (fields: Array<keyof NonNullable<ProviderRawMetadata["request_summary"]>>) =>
    fields.some((field) => JSON.stringify(previous?.[field] ?? null) !== JSON.stringify(current?.[field] ?? null));
  const stablePromptChanged = changed(["instructions_hash"]);
  const promptBytesChanged = JSON.stringify(previous.instructions_full_hash ?? null) !==
    JSON.stringify(current?.instructions_full_hash ?? null) ||
    numberField(previous.instructions_len) !== numberField(current?.instructions_len);
  const promptSummary = stablePromptChanged
    ? "Stable prompt changed"
    : promptBytesChanged
      ? "Dynamic prompt changed"
      : "Prompt unchanged";
  const delta = (before?: number, after?: number) => numberField(after) - numberField(before);
  const inputDelta = delta(previous.input_items_len, current?.input_items_len);
  const logicalInputDelta = delta(previous.input_items_logical_len ?? previous.input_items_len, current?.input_items_logical_len ?? current?.input_items_len);
  const omittedByContinuation = numberField(current?.input_items_omitted_by_continuation);
  const inputDetail = providerInputDeltaSummary(previous.input_item_counts, current?.input_item_counts);
  const toolDetail = providerToolDeltaSummary(previous.tool_names, current?.tool_names);
  const toolSchemaDetail = providerToolSchemaDeltaSummary(previous.tool_schema_hashes, current?.tool_schema_hashes);
  const combinedToolDetail = toolDetail && toolSchemaDetail
    ? `names ${toolDetail}; schema ${toolSchemaDetail}`
    : toolDetail || (toolSchemaDetail ? `schema ${toolSchemaDetail}` : "");
  const paramsDetail = providerParamsDeltaSummary(previous.request_params, current?.request_params);
  const paramKeysDetail = providerParamKeyDeltaSummary(previous.request_param_keys, current?.request_param_keys, {
    ignore: ["previous_response_id"],
  });
  const combinedParamsDetail = paramsDetail && paramKeysDetail
    ? `${paramsDetail}; keys ${paramKeysDetail}`
    : paramsDetail || (paramKeysDetail ? `keys ${paramKeysDetail}` : "");
  const toolsChanged = changed(["tools_hash", "tools_len", "tools_chars", "tool_names", "tool_schema_hashes"]);
  const paramsChanged = changed(["request_params"]) || Boolean(paramKeysDetail);
  const abortMarkerChanged = changed(["turn_aborted_marker_present"]);
  const abortMarkerSummary = abortMarkerChanged
    ? current?.turn_aborted_marker_present ? "Abort marker appeared" : "Abort marker cleared"
    : current?.turn_aborted_marker_present ? "Abort marker present" : "";
  const continuationChanged = changed(["previous_response_id_present", "previous_response_id_hash"]);
  const continuationDetail = omittedByContinuation > 0
    ? `Continuation used; omitted ${omittedByContinuation} input items`
    : continuationChanged
      ? "Continuation changed"
      : "Continuation unchanged";
  const scaffoldChanged = changed([
    "instructions_hash",
    "tools_hash",
    "tools_len",
    "tools_chars",
    "tool_names",
    "tool_schema_hashes",
    "request_params",
    "prompt_cache_key_present",
    "prompt_cache_key_hash",
    "metadata_keys",
  ]);
  const scaffoldSummary = scaffoldChanged
    ? "Request scaffold changed"
    : inputDelta === 0 && logicalInputDelta === 0 && !inputDetail
      ? "Stable request scaffold; input shape unchanged"
      : `Stable request scaffold; sent input ${inputDelta > 0 ? "+" : ""}${inputDelta}${logicalInputDelta !== inputDelta ? `, logical ${logicalInputDelta > 0 ? "+" : ""}${logicalInputDelta}` : ""}`;
  return [
    promptSummary,
    toolsChanged ? `Tools changed${combinedToolDetail ? ` (${combinedToolDetail})` : ""}` : `Tools unchanged${typeof current?.tools_len === "number" ? ` (${current.tools_len} tools${typeof current.tools_chars === "number" ? `, ${current.tools_chars} chars` : ""})` : ""}`,
    paramsChanged ? `Params changed${combinedParamsDetail ? ` (${combinedParamsDetail})` : ""}` : "Params unchanged",
    changed(["prompt_cache_key_present", "prompt_cache_key_hash"]) ? "Cache routing changed" : "Cache routing unchanged",
    continuationDetail,
    inputDelta === 0 && !inputDetail ? "Input item count unchanged" : `Input items ${inputDelta > 0 ? "+" : ""}${inputDelta}${inputDetail ? ` (${inputDetail})` : ""}`,
    ...(omittedByContinuation > 0 ? [`Logical input items ${logicalInputDelta > 0 ? "+" : ""}${logicalInputDelta}`] : []),
    changed(["metadata_keys"]) ? "Metadata keys changed" : "Metadata keys unchanged",
    ...(abortMarkerSummary ? [abortMarkerSummary] : []),
    scaffoldSummary,
  ];
};

export const providerTraceExportPackage = (
  raw: ProviderRawMetadata,
  exportedAt = new Date().toISOString(),
  previous?: ProviderRawMetadata["request_summary"],
): ProviderTraceExportPackage => {
  const usage = providerUsageSummary(raw);
  const requestDiffSummary = previous ? providerRequestDiffSummary(previous, raw.request_summary ?? {}) : undefined;
  return {
    kind: "minicode_provider_trace_export",
    exported_at: exportedAt,
    provider: raw.provider ?? "",
    model: raw.model ?? raw.request_summary?.model ?? "",
    finish_reason: raw.finish_reason ?? "",
    event_type: raw.event_type ?? "",
    usage,
    cache_hit_rate: providerCacheHitRate(usage),
    continuation_summary: providerContinuationDetail(raw),
    output_sequence: providerOutputSequence(raw.output_items),
    output_phase_counts: providerOutputPhaseCounts(raw.output_items),
    response_lifecycle: providerResponseLifecycle(raw.provider_timeline),
    provider_timeline_sequence: providerTimelineSequence(raw.provider_timeline),
    provider_timeline_event_counts: providerTimelineEventCounts(raw.provider_timeline),
    request_summary: sanitizeProviderTraceExportValue(raw.request_summary ?? {}) as ProviderRawMetadata["request_summary"],
    stateful_continuation: sanitizeProviderTraceExportValue(raw.stateful_continuation ?? {}) as ProviderRawMetadata["stateful_continuation"],
    loop_metrics: sanitizeProviderTraceExportValue(raw.loop_metrics ?? {}) as ProviderRawMetadata["loop_metrics"],
    output_items: sanitizeProviderTraceExportValue(raw.output_items ?? []) as ProviderRawMetadata["output_items"],
    provider_timeline: sanitizeProviderTraceExportValue(raw.provider_timeline ?? []) as ProviderRawMetadata["provider_timeline"],
    safety: sanitizeProviderTraceExportValue(raw.safety ?? { redacted_prompt: true }) as ProviderRawMetadata["safety"],
    prompt_cache_diagnostic: sanitizeProviderTraceExportValue(raw.prompt_cache_diagnostic ?? {}) as ProviderRawMetadata["prompt_cache_diagnostic"],
    diagnostics: providerTraceDiagnostics(raw),
    request_diff_summary: requestDiffSummary,
  };
};

export const providerTraceExportJson = (
  raw: ProviderRawMetadata,
  previous?: ProviderRawMetadata["request_summary"],
): string => JSON.stringify(providerTraceExportPackage(raw, new Date().toISOString(), previous), null, 2);

const providerEndpointPath = (wireApi?: string, provider?: string): string => {
  const wire = String(wireApi || provider || "").toLowerCase();
  if (wire.includes("anthropic")) return "/v1/messages";
  if (wire.includes("chat")) return "/v1/chat/completions";
  return "/v1/responses";
};

export const providerSafeRequestPackage = (raw: ProviderRawMetadata): ProviderSafeRequestPackage => {
  const summary = raw.request_summary ?? {};
  const endpoint = providerEndpointPath(summary.wire_api, raw.provider);
  return {
    kind: "minicode_provider_safe_request",
    provider: raw.provider ?? "",
    model: raw.model ?? summary.model ?? "",
    wire_api: summary.wire_api ?? "",
    endpoint,
    request_params: sanitizeProviderTraceExportValue(summary.request_params ?? {}) as Record<string, unknown>,
    prompt: {
      redacted: true,
      instructions_len: summary.instructions_len,
      instructions_sent_len: summary.instructions_sent_len,
      instructions_omitted_by_continuation: summary.instructions_omitted_by_continuation,
      instructions_hash: summary.instructions_hash,
      instructions_full_hash: summary.instructions_full_hash,
    },
    tools: {
      redacted: true,
      tools_len: summary.tools_len,
      tools_chars: summary.tools_chars,
      tools_hash: summary.tools_hash,
      tool_names: summary.tool_names,
      tool_schema_hashes: summary.tool_schema_hashes,
      largest_tools: summary.largest_tools,
    },
    input: {
    redacted: true,
    input_items_len: summary.input_items_len,
    input_items_sent_len: summary.input_items_sent_len,
    input_items_omitted_by_continuation: summary.input_items_omitted_by_continuation,
    input_items_logical_len: summary.input_items_logical_len,
    input_chars: summary.input_chars,
      input_item_counts: summary.input_item_counts,
      largest_input_items: summary.largest_input_items,
      duplicate_input_content: summary.duplicate_input_content,
      previous_response_id_present: summary.previous_response_id_present,
      previous_response_id_hash: summary.previous_response_id_hash,
    },
    metadata: {
      redacted: true,
      keys: Array.isArray(summary.metadata_keys)
        ? sanitizedMetadataKeys(summary.metadata_keys) as string[]
        : undefined,
    },
    note: "Safe diagnostic request skeleton only; prompt text, message content, tool arguments, and secrets are intentionally omitted.",
  };
};

export const providerSafeRequestJson = (raw: ProviderRawMetadata): string =>
  JSON.stringify(providerSafeRequestPackage(raw), null, 2);

export const providerCurlSkeleton = (raw: ProviderRawMetadata): string => {
  const request = providerSafeRequestPackage(raw);
  const body = {
    model: request.model || "<model>",
    ...request.request_params,
    instructions: `<redacted len=${request.prompt.instructions_len ?? 0} hash=${request.prompt.instructions_hash || "none"}>`,
    tools: `<redacted count=${request.tools.tools_len ?? 0} hash=${request.tools.tools_hash || "none"}>`,
    input: `<redacted items=${request.input.input_items_len ?? 0}>`,
    metadata: { keys: request.metadata.keys ?? [] },
  };
  return [
    `curl https://api.example.com${request.endpoint} \\`,
    `  -H "Authorization: Bearer $PROVIDER_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify(body, null, 2).replace(/'/g, "'\\''")}'`,
    "",
    `# ${request.note}`,
  ].join("\n");
};

export const providerTraceExportJsonl = (raws: ProviderRawMetadata[]): string =>
  raws
    .map((raw, index) => providerTraceExportPackage(raw, new Date().toISOString(), raws[index - 1]?.request_summary))
    .map((item) => JSON.stringify(item))
    .join("\n");

export const providerTracePayloadFromExport = (value: unknown): Record<string, unknown> | null => {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (item.kind === "provider_trace") return item;
  if (item.kind !== "minicode_provider_trace_export") return null;
  return {
    kind: "provider_trace",
    provider: typeof item.provider === "string" ? item.provider : "",
    model: typeof item.model === "string" ? item.model : "",
    finish_reason: typeof item.finish_reason === "string" ? item.finish_reason : "",
    event_type: typeof item.event_type === "string" ? item.event_type : "",
    usage: item.usage && typeof item.usage === "object" ? item.usage : {},
    output_items: Array.isArray(item.output_items) ? item.output_items : [],
    provider_timeline: Array.isArray(item.provider_timeline) ? item.provider_timeline : [],
    request_summary: item.request_summary && typeof item.request_summary === "object" ? item.request_summary : {},
    stateful_continuation: item.stateful_continuation && typeof item.stateful_continuation === "object" ? item.stateful_continuation : {},
    loop_metrics: item.loop_metrics && typeof item.loop_metrics === "object" ? item.loop_metrics : {},
    safety: item.safety && typeof item.safety === "object" ? item.safety : { redacted_prompt: true },
    prompt_cache_diagnostic: item.prompt_cache_diagnostic && typeof item.prompt_cache_diagnostic === "object" ? item.prompt_cache_diagnostic : {},
  };
};

export const providerTracePayloadFromDone = (
  raw: ProviderRawMetadata | undefined,
  fallbackUsage?: ProviderUsageSummary,
): Record<string, unknown> | null => {
  if (!raw || typeof raw !== "object") return null;
  const usage = fallbackUsage
    ? {
        input_tokens: fallbackUsage.input,
        output_tokens: fallbackUsage.output,
        cache_read_input_tokens: fallbackUsage.cacheRead,
        cache_creation_input_tokens: fallbackUsage.cacheWrite,
        prompt_cache_total_tokens: fallbackUsage.promptCacheTotal,
        prompt_cache_hit_rate: fallbackUsage.promptCacheHitRate,
        reasoning_output_tokens: fallbackUsage.reasoning,
      }
    : raw.usage ?? {};
  return {
    kind: "provider_trace",
    provider: raw.provider ?? "",
    model: raw.model ?? raw.request_summary?.model ?? "",
    finish_reason: raw.finish_reason ?? "",
    event_type: raw.event_type ?? "",
    usage,
    output_items: raw.output_items ?? [],
    provider_timeline: raw.provider_timeline ?? [],
    request_summary: raw.request_summary ?? {},
    stateful_continuation: raw.stateful_continuation ?? {},
    loop_metrics: raw.loop_metrics ?? {},
    safety: raw.safety ?? { redacted_prompt: true },
    prompt_cache_diagnostic: raw.prompt_cache_diagnostic ?? {},
    trace_id: raw.trace_id,
  };
};

export const providerRequestDiff = (
  previous: ProviderRawMetadata["request_summary"] | undefined,
  current: ProviderRawMetadata["request_summary"] | undefined,
): Array<{ label: string; before: unknown; after: unknown; changed: boolean }> => {
  const fields: Array<keyof NonNullable<ProviderRawMetadata["request_summary"]>> = [
    "instructions_hash",
    "instructions_full_hash",
    "instructions_len",
    "tools_hash",
    "tools_len",
    "tools_chars",
    "tool_names",
    "tool_schema_hashes",
    "prompt_cache_key_present",
    "prompt_cache_key_hash",
    "previous_response_id_present",
    "previous_response_id_hash",
    "request_params",
    "request_param_keys",
    "turn_aborted_marker_present",
    "input_items_len",
    "input_items_sent_len",
    "input_items_omitted_by_continuation",
    "input_items_logical_len",
    "input_chars",
    "input_item_counts",
    "largest_input_items",
    "duplicate_input_content",
    "metadata_keys",
    "prompt_section_summary",
  ];
  return fields.map((field) => {
    const before = previous?.[field];
    const after = current?.[field];
    return {
      label: field,
      before,
      after,
      changed: JSON.stringify(before ?? null) !== JSON.stringify(after ?? null),
    };
  });
};
