import { normalizeToolDiff, type ToolCallRecord } from "../lib/tool-call-reducer";
import type {
  ArtifactPreview,
  ChatMessage,
  Citation,
  ContentBlock,
  MessageAttachmentRef,
  MessageUsage,
} from "../stores/types";

export type BackendTranscriptMessage = {
  id?: unknown;
  role?: unknown;
  content?: unknown;
  thinking?: unknown;
  blocks?: unknown;
  tool_calls?: unknown;
  toolCalls?: unknown;
  artifacts?: unknown;
  attachments?: unknown;
  attachmentRefs?: unknown;
  citations?: unknown;
  usage?: unknown;
  timestamp?: unknown;
  completedAt?: unknown;
  completed_at?: unknown;
};

const toTimestamp = (value: unknown): number => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Date.now();
};

const toRole = (value: unknown): ChatMessage["role"] => {
  if (value === "assistant" || value === "system") return value;
  return "user";
};

const toUsage = (value: unknown): MessageUsage | undefined => {
  if (!value || typeof value !== "object") return undefined;
  const usage = value as {
    input?: unknown;
    output?: unknown;
    cacheRead?: unknown;
    cacheWrite?: unknown;
    input_tokens?: unknown;
    output_tokens?: unknown;
    cache_read_input_tokens?: unknown;
    cache_creation_input_tokens?: unknown;
  };
  const input = Number(usage.input ?? usage.input_tokens ?? 0);
  const output = Number(usage.output ?? usage.output_tokens ?? 0);
  const cacheRead = Number(usage.cacheRead ?? usage.cache_read_input_tokens ?? 0);
  const cacheWrite = Number(usage.cacheWrite ?? usage.cache_creation_input_tokens ?? 0);
  if (!input && !output && !cacheRead && !cacheWrite) return undefined;
  return { input, output, cacheRead, cacheWrite };
};

const toArray = <T,>(value: unknown): T[] => Array.isArray(value) ? value as T[] : [];

const isProgressStage = (value: unknown): value is "status" | "planning" | "tool" | "approval" | "verification" | "final" =>
  value === "status" || value === "planning" || value === "tool" || value === "approval" || value === "verification" || value === "final";

const isProgressStatus = (value: unknown): value is "running" | "completed" | "failed" | "info" =>
  value === "running" || value === "completed" || value === "failed" || value === "info";

const isProgressPhase = (value: unknown): value is "orienting" | "planning" | "model" | "tool" | "approval" | "verify" | "final" | "recover" | "status" | "iteration" =>
  value === "orienting"
  || value === "planning"
  || value === "model"
  || value === "tool"
  || value === "approval"
  || value === "verify"
  || value === "final"
  || value === "recover"
  || value === "status"
  || value === "iteration";

const isProgressVisibility = (value: unknown): value is "timeline" | "compact" | "debug" =>
  value === "timeline" || value === "compact" || value === "debug";

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value ? value : undefined;

const booleanValue = (value: unknown): boolean | undefined =>
  typeof value === "boolean" ? value : undefined;

const numberValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const stringArrayValue = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const items = value.filter((item): item is string => typeof item === "string" && Boolean(item));
  return items.length ? items : undefined;
};

const toToolCallRecord = (value: unknown): ToolCallRecord | null => {
  if (!value || typeof value !== "object") return null;
  const tool = value as Record<string, unknown>;
  const id = String(tool.id ?? "").trim();
  const name = String(tool.name ?? "").trim();
  if (!id || !name) return null;
  const args = tool.args && typeof tool.args === "object" ? tool.args as Record<string, unknown> : {};
  const status = String(tool.status ?? "running");
  return {
    id,
    name,
    args,
    status: (status === "pending" || status === "running" || status === "success" || status === "failed" || status === "blocked" || status === "partial")
      ? status
      : "running",
    summary: typeof tool.summary === "string" ? tool.summary : undefined,
    artifactId: typeof tool.artifactId === "string"
      ? tool.artifactId
      : typeof tool.artifact_id === "string"
        ? tool.artifact_id
        : undefined,
    sourceUrl: typeof tool.sourceUrl === "string"
      ? tool.sourceUrl
      : typeof tool.source_url === "string"
        ? tool.source_url
        : undefined,
    extractionStatus: typeof tool.extractionStatus === "string"
      ? tool.extractionStatus
      : typeof tool.extraction_status === "string"
        ? tool.extraction_status
        : undefined,
    contentPreview: typeof tool.contentPreview === "string"
      ? tool.contentPreview
      : typeof tool.content_preview === "string"
        ? tool.content_preview
        : undefined,
    evidenceType: typeof tool.evidenceType === "string"
      ? tool.evidenceType
      : typeof tool.evidence_type === "string"
        ? tool.evidence_type
        : undefined,
    displaySummary: typeof tool.displaySummary === "string"
      ? tool.displaySummary
      : typeof tool.display_summary === "string"
        ? tool.display_summary
        : undefined,
    resultKind: typeof tool.resultKind === "string"
      ? tool.resultKind
      : typeof tool.result_kind === "string"
        ? tool.result_kind
        : undefined,
    activityKind: typeof tool.activityKind === "string"
      ? tool.activityKind
      : typeof tool.activity_kind === "string"
        ? tool.activity_kind
        : undefined,
    limitation: typeof tool.limitation === "string" ? tool.limitation : undefined,
    provider: typeof tool.provider === "string" ? tool.provider : undefined,
    providerErrorType: typeof tool.providerErrorType === "string"
      ? tool.providerErrorType
      : typeof tool.provider_error_type === "string"
        ? tool.provider_error_type
        : undefined,
    errorInfo: tool.errorInfo && typeof tool.errorInfo === "object"
      ? tool.errorInfo as ToolCallRecord["errorInfo"]
      : tool.error_info && typeof tool.error_info === "object"
        ? tool.error_info as ToolCallRecord["errorInfo"]
        : undefined,
    errorKind: typeof tool.errorKind === "string"
      ? tool.errorKind
      : typeof tool.error_kind === "string"
        ? tool.error_kind
        : undefined,
    userSummary: typeof tool.userSummary === "string"
      ? tool.userSummary
      : typeof tool.user_summary === "string"
        ? tool.user_summary
        : undefined,
    developerDetail: typeof tool.developerDetail === "string"
      ? tool.developerDetail
      : typeof tool.developer_detail === "string"
        ? tool.developer_detail
        : undefined,
    recoverable: typeof tool.recoverable === "boolean" ? tool.recoverable : undefined,
    projection: typeof tool.projection === "string" ? tool.projection : undefined,
    durationMs: typeof tool.durationMs === "number"
      ? tool.durationMs
      : typeof tool.duration_ms === "number"
        ? tool.duration_ms
        : undefined,
    displayHint: typeof tool.displayHint === "string"
      ? tool.displayHint
      : typeof tool.display_hint === "string"
        ? tool.display_hint
        : undefined,
    inputSummary: typeof tool.inputSummary === "string"
      ? tool.inputSummary
      : typeof tool.input_summary === "string"
        ? tool.input_summary
        : undefined,
    iterationId: typeof tool.iterationId === "string"
      ? tool.iterationId
      : typeof tool.iteration_id === "string"
        ? tool.iteration_id
        : undefined,
    phase: typeof tool.phase === "string" ? tool.phase : undefined,
    displayScope: typeof tool.displayScope === "string"
      ? tool.displayScope
      : typeof tool.display_scope === "string"
        ? tool.display_scope
        : undefined,
    panelHint: typeof tool.panelHint === "string"
      ? tool.panelHint
      : typeof tool.panel_hint === "string"
        ? tool.panel_hint
        : undefined,
    requiresAttention: typeof tool.requiresAttention === "boolean"
      ? tool.requiresAttention
      : typeof tool.requires_attention === "boolean"
        ? tool.requires_attention
        : undefined,
    startedAt: toTimestamp(tool.startedAt ?? tool.started_at),
    finishedAt: tool.finishedAt != null || tool.finished_at != null
      ? toTimestamp(tool.finishedAt ?? tool.finished_at)
      : undefined,
    outputPreview: typeof tool.outputPreview === "string"
      ? tool.outputPreview
      : typeof tool.output_preview === "string"
        ? tool.output_preview
        : undefined,
    stdoutPreview: typeof tool.stdoutPreview === "string"
      ? tool.stdoutPreview
      : typeof tool.stdout_preview === "string"
        ? tool.stdout_preview
        : undefined,
    stderrPreview: typeof tool.stderrPreview === "string"
      ? tool.stderrPreview
      : typeof tool.stderr_preview === "string"
        ? tool.stderr_preview
        : undefined,
    diff: normalizeToolDiff(tool.diff),
  };
};

const toBlocks = (value: unknown): ContentBlock[] | undefined => {
  const items = toArray<Record<string, unknown>>(value);
  if (!items.length) return undefined;
  const blocks: ContentBlock[] = [];
  for (const item of items) {
    const type = String(item.type ?? "").trim();
    if (type === "thinking") {
      blocks.push({
        type: "thinking",
        content: typeof item.content === "string" ? item.content : "",
        source: stringValue(item.source),
        visibility: stringValue(item.visibility),
        is_raw_provider_reasoning: booleanValue(item.is_raw_provider_reasoning),
      });
      continue;
    }
    if (type === "text") {
      blocks.push({
        type: "text",
        content: typeof item.content === "string" ? item.content : "",
        source: stringValue(item.source),
        visibility: stringValue(item.visibility),
        role: stringValue(item.role),
        phase: stringValue(item.phase),
      });
      continue;
    }
    if (type === "process") {
      const id = String(item.id ?? item.item_id ?? "").trim();
      const itemKind = String(item.itemKind ?? item.item_kind ?? item.kind ?? "").trim();
      if (!id || !itemKind) continue;
      blocks.push({
        type: "process",
        id,
        itemKind,
        content: typeof item.content === "string" ? item.content : "",
        title: stringValue(item.title),
        summary: stringValue(item.summary),
        source: stringValue(item.source),
        status: stringValue(item.status),
        role: stringValue(item.role),
        visibility: stringValue(item.visibility),
        loopId: stringValue(item.loopId ?? item.loop_id),
        iterationId: stringValue(item.iterationId ?? item.iteration_id),
        parentId: stringValue(item.parentId ?? item.parent_id),
        groupId: stringValue(item.groupId ?? item.group_id),
        stepId: stringValue(item.stepId ?? item.step_id),
        toolCallIds: stringArrayValue(item.toolCallIds ?? item.tool_call_ids),
        defaultCollapsed: booleanValue(item.defaultCollapsed ?? item.default_collapsed),
        displayScope: stringValue(item.displayScope ?? item.display_scope),
        panelHint: stringValue(item.panelHint ?? item.panel_hint),
        requiresAttention: booleanValue(item.requiresAttention ?? item.requires_attention),
        seq: numberValue(item.seq),
        order: numberValue(item.order),
        timestamp: toTimestamp(item.timestamp),
      });
      continue;
    }
    if (type === "tool_call") {
      const record = toToolCallRecord(item.record ?? item);
      if (record) blocks.push({ type: "tool_call", record });
      continue;
    }
    if (type === "progress") {
      const id = String(item.id ?? "").trim();
      const message = String(item.message ?? "").trim();
      if (!id || !message) continue;
      blocks.push({
        type: "progress",
        id,
        stage: isProgressStage(item.stage) ? item.stage : "status",
        phase: isProgressPhase(item.phase) ? item.phase : undefined,
        status: isProgressStatus(item.status) ? item.status : "info",
        message,
        label: typeof item.label === "string" ? item.label : undefined,
        summary: typeof item.summary === "string" ? item.summary : undefined,
        visibility: isProgressVisibility(item.visibility) ? item.visibility : undefined,
        detail: typeof item.detail === "string" ? item.detail : undefined,
        toolCallId: typeof item.toolCallId === "string"
          ? item.toolCallId
          : typeof item.tool_call_id === "string"
            ? item.tool_call_id
            : undefined,
        toolName: typeof item.toolName === "string"
          ? item.toolName
          : typeof item.tool_name === "string"
            ? item.tool_name
            : undefined,
        groupId: typeof item.groupId === "string"
          ? item.groupId
          : typeof item.group_id === "string"
            ? item.group_id
            : undefined,
        stepId: typeof item.stepId === "string"
          ? item.stepId
          : typeof item.step_id === "string"
            ? item.step_id
            : undefined,
        count: typeof item.count === "number" ? item.count : undefined,
        timestamp: toTimestamp(item.timestamp),
      });
    }
  }
  return blocks.length ? blocks : undefined;
};

const legacyBlocksFor = (
  message: BackendTranscriptMessage,
  toolCalls: ToolCallRecord[],
): ContentBlock[] | undefined => {
  const blocks: ContentBlock[] = [];
  if (typeof message.thinking === "string" && message.thinking) {
    blocks.push({ type: "thinking", content: message.thinking });
  }
  for (const record of toolCalls) {
    blocks.push({ type: "tool_call", record });
  }
  if (typeof message.content === "string" && message.content) {
    blocks.push({ type: "text", content: message.content });
  }
  return blocks.length ? blocks : undefined;
};

const toAttachmentRefs = (value: unknown): MessageAttachmentRef[] => {
  const items = toArray<Record<string, unknown>>(value);
  return items.flatMap((item) => {
    const name = String(item.file_name ?? item.name ?? "").trim();
    const artifactId = String(item.artifact_id ?? item.artifactId ?? "").trim();
    if (!name || !artifactId) return [];
    const kind = String(item.kind ?? "document");
    return [{
      id: String(item.id ?? artifactId),
      name,
      kind: kind === "image" ? "image" : kind === "document" ? "document" : "file",
      mediaType: String(item.media_type ?? item.mediaType ?? "application/octet-stream"),
      sizeBytes: Number(item.size_bytes ?? item.sizeBytes ?? 0),
      artifactId,
      docId: String(item.doc_id ?? item.docId ?? ""),
      indexedChunks: Number(item.indexed_chunks ?? item.indexedChunks ?? 0),
    }];
  });
};

const dedupeHydratedMessages = (messages: ChatMessage[]): ChatMessage[] => {
  const result: ChatMessage[] = [];
  for (const message of messages) {
    const previous = result.at(-1);
    const isDuplicateUserEcho = previous
      && previous.role === "user"
      && message.role === "user"
      && previous.content.trim() === message.content.trim()
      && !previous.attachmentRefs?.length
      && !message.attachmentRefs?.length;
    if (isDuplicateUserEcho) continue;
    result.push(message);
  }
  return result;
};

export const hydrateMessages = (messages: BackendTranscriptMessage[] | undefined): ChatMessage[] =>
  dedupeHydratedMessages((messages ?? []).map((message, index) => {
    const parsedBlocks = toBlocks(message.blocks);
    const fallbackToolCalls = toArray<unknown>(message.tool_calls ?? message.toolCalls).flatMap((item) => {
      const record = toToolCallRecord(item);
      return record ? [record] : [];
    });
    const blocks = parsedBlocks ?? legacyBlocksFor(message, fallbackToolCalls);
    return {
      id: typeof message.id === "string" && message.id ? message.id : `m-${index}-${Date.now().toString(36)}`,
      role: toRole(message.role),
      content: typeof message.content === "string" ? message.content : "",
      blocks,
      artifacts: toArray<ArtifactPreview>(message.artifacts),
      attachmentRefs: toAttachmentRefs(message.attachmentRefs ?? message.attachments),
      citations: toArray<Citation>(message.citations),
      usage: toUsage(message.usage),
      timestamp: toTimestamp(message.timestamp),
      completedAt: message.completedAt != null || message.completed_at != null
        ? toTimestamp(message.completedAt ?? message.completed_at)
        : undefined,
    };
  }));
