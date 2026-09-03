import { normalizeToolDiff, type ToolCallRecord } from "../lib/tool-call-reducer";
import { workspacePathComparisonKey } from "../lib/workspace-path";
import {
  isHiddenProviderReasoning,
  isTransientProviderReasoning,
  providerReasoningType,
} from "../lib/provider-reasoning";
import {
  isAgentProgressPhase,
  isAgentProgressProviderState,
} from "../protocol/streaming-types";
import type {
  ArtifactPreview,
  ChatMessage,
  Citation,
  ContentBlock,
  MessageAttachmentRef,
  MessageContextRef,
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
  context_refs?: unknown;
  contextRefs?: unknown;
  reply_attachments?: unknown;
  replyAttachments?: unknown;
  citations?: unknown;
  usage?: unknown;
  timestamp?: unknown;
  completedAt?: unknown;
  completed_at?: unknown;
  durationMs?: unknown;
  duration_ms?: unknown;
  isStreaming?: unknown;
  is_streaming?: unknown;
  isThinkingStreaming?: unknown;
  is_thinking_streaming?: unknown;
  terminalStatus?: unknown;
  terminal_status?: unknown;
  terminationReason?: unknown;
  termination_reason?: unknown;
  failureMessage?: unknown;
  failure_message?: unknown;
  failureRecoverable?: unknown;
  failure_recoverable?: unknown;
  steered?: unknown;
  steer_target_message_id?: unknown;
  metadata?: unknown;
};

export type HydrateMessagesOptions = {
  live?: boolean;
};

const toTimestamp = (value: unknown, fallback = 0): number => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
};

const toRole = (value: unknown): ChatMessage["role"] => {
  if (value === "user") return "user";
  if (value === "assistant" || value === "system") return value;
  if (
    value === "tool"
    || value === "tool_result"
    || value === "toolResult"
    || value === "bashExecution"
    || value === "custom"
    || value === "branchSummary"
    || value === "compactionSummary"
  ) return "system";
  return "system";
};

const transcriptContent = (message: BackendTranscriptMessage): string => {
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const block = item as Record<string, unknown>;
        const text = block.text ?? block.content ?? block.thinking;
        return typeof text === "string" && text ? [text] : [];
      })
      .join("\n");
  }
  const source = message as BackendTranscriptMessage & Record<string, unknown>;
  const role = String(message.role ?? "");
  if (role === "bashExecution") {
    const command = typeof source.command === "string" ? source.command : "";
    const output = typeof source.output === "string" ? source.output : "";
    const exitCode = typeof source.exitCode === "number" ? source.exitCode : undefined;
    const status = source.cancelled === true
      ? "Command cancelled"
      : exitCode != null && exitCode !== 0
        ? `Command exited with code ${exitCode}`
        : "";
    return [command ? `$ ${command}` : "", output, status].filter(Boolean).join("\n");
  }
  if (role === "branchSummary" || role === "compactionSummary") {
    return typeof source.summary === "string" ? source.summary : "";
  }
  return "";
};

const transcriptNoticeTitle = (message: BackendTranscriptMessage): string | undefined => {
  const role = String(message.role ?? "");
  if (role === "bashExecution") return "命令执行记录";
  if (role === "branchSummary") return "分支摘要";
  if (role === "compactionSummary") return "上下文压缩摘要";
  if (role === "custom") return "运行时记录";
  if (role === "tool" || role === "tool_result" || role === "toolResult") {
    const source = message as BackendTranscriptMessage & Record<string, unknown>;
    const toolName = String(source.name ?? source.toolName ?? "工具").trim() || "工具";
    return `${toolName} 结果`;
  }
  if (role === "developer") return "开发者上下文";
  return undefined;
};

const normalizeTerminalStatus = (value: unknown): ChatMessage["terminalStatus"] => {
  if (value === "completed" || value === "partial" || value === "failed" || value === "interrupted") {
    return value;
  }
  if (value === "cancelled") return "interrupted";
  return undefined;
};

const toUsage = (value: unknown): MessageUsage | undefined => {
  if (!value || typeof value !== "object") return undefined;
  const usage = value as {
    input?: unknown;
    ordinaryInput?: unknown;
    inputIncludesCacheRead?: unknown;
    inputIncludesCacheWrite?: unknown;
    output?: unknown;
    cacheRead?: unknown;
    cacheWrite?: unknown;
    promptCacheTotal?: unknown;
    promptCacheHitRate?: unknown;
    reasoning?: unknown;
    input_tokens?: unknown;
    ordinary_input_tokens?: unknown;
    input_includes_cache_read?: unknown;
    input_includes_cache_write?: unknown;
    output_tokens?: unknown;
    cache_read_input_tokens?: unknown;
    cache_creation_input_tokens?: unknown;
    prompt_cache_total_tokens?: unknown;
    prompt_cache_hit_rate?: unknown;
    reasoning_output_tokens?: unknown;
  };
  const input = Number(usage.input ?? usage.input_tokens ?? 0);
  const ordinaryInput = Number(
    usage.ordinaryInput ?? usage.ordinary_input_tokens,
  );
  const output = Number(usage.output ?? usage.output_tokens ?? 0);
  const cacheRead = Number(usage.cacheRead ?? usage.cache_read_input_tokens ?? 0);
  const cacheWrite = Number(usage.cacheWrite ?? usage.cache_creation_input_tokens ?? 0);
  const promptCacheTotal = Number(usage.promptCacheTotal ?? usage.prompt_cache_total_tokens);
  const promptCacheHitRate = Number(usage.promptCacheHitRate ?? usage.prompt_cache_hit_rate);
  const reasoning = Number(usage.reasoning ?? usage.reasoning_output_tokens ?? 0);
  if (
    !input
    && !output
    && !Number.isFinite(ordinaryInput)
    && !cacheRead
    && !cacheWrite
    && !reasoning
    && !Number.isFinite(promptCacheTotal)
    && !Number.isFinite(promptCacheHitRate)
  ) return undefined;
  const normalized: MessageUsage = {
    input,
    output,
    cacheRead,
    cacheWrite,
    reasoning,
  };
  if (Number.isFinite(ordinaryInput)) normalized.ordinaryInput = ordinaryInput;
  const inputIncludesCacheRead = usage.inputIncludesCacheRead ?? usage.input_includes_cache_read;
  const inputIncludesCacheWrite = usage.inputIncludesCacheWrite ?? usage.input_includes_cache_write;
  if (typeof inputIncludesCacheRead === "boolean") normalized.inputIncludesCacheRead = inputIncludesCacheRead;
  if (typeof inputIncludesCacheWrite === "boolean") normalized.inputIncludesCacheWrite = inputIncludesCacheWrite;
  if (Number.isFinite(promptCacheTotal)) normalized.promptCacheTotal = promptCacheTotal;
  if (Number.isFinite(promptCacheHitRate)) normalized.promptCacheHitRate = promptCacheHitRate;
  return normalized;
};

const toArray = <T,>(value: unknown): T[] => Array.isArray(value) ? value as T[] : [];

const isProgressStage = (value: unknown): value is "status" | "planning" | "tool" | "approval" | "verification" | "final" =>
  value === "status" || value === "planning" || value === "tool" || value === "approval" || value === "verification" || value === "final";

const isProgressStatus = (value: unknown): value is "running" | "completed" | "partial" | "failed" | "info" =>
  value === "running" || value === "completed" || value === "partial" || value === "failed" || value === "info";

const isProgressVisibility = (value: unknown): value is "timeline" | "compact" | "debug" =>
  value === "timeline" || value === "compact" || value === "debug";

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value ? value : undefined;

const toMessageSource = (value: unknown): ChatMessage["messageSource"] => {
  if (!value || typeof value !== "object") return undefined;
  const metadata = value as Record<string, unknown>;
  if (String(metadata.source ?? "").trim().toLowerCase() !== "scheduled_task") {
    return undefined;
  }
  const taskId = stringValue(metadata.scheduled_task_id ?? metadata.scheduledTaskId);
  const runId = stringValue(metadata.scheduled_run_id ?? metadata.scheduledRunId);
  return {
    kind: "scheduled_task",
    ...(taskId ? { taskId } : {}),
    ...(runId ? { runId } : {}),
  };
};

const booleanValue = (value: unknown): boolean | undefined =>
  typeof value === "boolean" ? value : undefined;

const numberValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const nonNegativeNumberValue = (value: unknown): number | undefined => {
  const number = numberValue(value);
  return number == null ? undefined : Math.max(0, number);
};

const stringArrayValue = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const items = value.filter((item): item is string => typeof item === "string" && Boolean(item));
  return items.length ? items : undefined;
};

const toOutputFiles = (value: unknown): ToolCallRecord["outputFiles"] => {
  const files = toArray<Record<string, unknown>>(value).flatMap((item) => {
    const path = String(item.path ?? "").trim();
    if (!path) return [];
    const size = Number(item.size ?? 0);
    return [{
      path,
      name: stringValue(item.name),
      size: Number.isFinite(size) && size > 0 ? size : 0,
      mimeType: stringValue(item.mimeType ?? item.mime_type),
      isImage: booleanValue(item.isImage ?? item.is_image),
    }];
  });
  return files.length ? files : undefined;
};

const toToolCallRecord = (value: unknown): ToolCallRecord | null => {
  if (!value || typeof value !== "object") return null;
  const tool = value as Record<string, unknown>;
  const id = String(tool.id ?? "").trim();
  const name = String(tool.name ?? "").trim();
  if (!id || !name) return null;
  const args = tool.args && typeof tool.args === "object" ? tool.args as Record<string, unknown> : {};
  const rawStatus = String(tool.status ?? "running").toLowerCase();
  const status = rawStatus === "completed"
    ? "success"
    : rawStatus === "error"
      ? "failed"
      : rawStatus === "waiting_approval"
        ? "pending"
        : rawStatus;
  return {
    id,
    name,
    args,
    status: (status === "pending" || status === "running" || status === "success" || status === "failed" || status === "blocked" || status === "partial" || status === "timeout" || status === "cancelled")
      ? status
      : "running",
    transition: stringValue(tool.transition ?? tool.tool_transition),
    waitingOn: stringValue(tool.waitingOn ?? tool.waiting_on),
    blockingReason: stringValue(tool.blockingReason ?? tool.blocking_reason),
    summary: typeof tool.summary === "string" ? tool.summary : undefined,
    artifactId: typeof tool.artifactId === "string"
      ? tool.artifactId
      : typeof tool.artifact_id === "string"
        ? tool.artifact_id
        : undefined,
    artifactKind: typeof tool.artifactKind === "string"
      ? tool.artifactKind
      : typeof tool.artifact_kind === "string"
        ? tool.artifact_kind
        : undefined,
    // Older child transcripts stored the image MIME under the generic
    // media/mime keys and did not persist artifact_kind. Preserve those
    // aliases so the activity projection can still identify screenshots.
    artifactMediaType: typeof tool.artifactMediaType === "string"
      ? tool.artifactMediaType
      : typeof tool.artifact_media_type === "string"
        ? tool.artifact_media_type
        : typeof tool.mediaType === "string"
          ? tool.mediaType
          : typeof tool.media_type === "string"
            ? tool.media_type
            : typeof tool.mimeType === "string"
              ? tool.mimeType
              : typeof tool.mime_type === "string"
                ? tool.mime_type
                : undefined,
    artifactBytes: typeof tool.artifactBytes === "number"
      ? tool.artifactBytes
      : typeof tool.artifact_bytes === "number"
        ? tool.artifact_bytes
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
        : "generic",
    activityKind: typeof tool.activityKind === "string"
      ? tool.activityKind
      : typeof tool.activity_kind === "string"
        ? tool.activity_kind
        : "genericTool",
    visibility: typeof tool.visibility === "string" ? tool.visibility : undefined,
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
    groupId: typeof tool.groupId === "string"
      ? tool.groupId
      : typeof tool.group_id === "string"
        ? tool.group_id
        : undefined,
    stepId: typeof tool.stepId === "string"
      ? tool.stepId
      : typeof tool.step_id === "string"
        ? tool.step_id
        : undefined,
    turnId: typeof tool.turnId === "string"
      ? tool.turnId
      : typeof tool.turn_id === "string"
        ? tool.turn_id
        : undefined,
    taskId: typeof tool.taskId === "string"
      ? tool.taskId
      : typeof tool.task_id === "string"
        ? tool.task_id
        : undefined,
    seq: numberValue(tool.seq),
    scopeMigrationCount: numberValue(tool.scopeMigrationCount ?? tool.scope_migration_count),
    iterationId: typeof tool.iterationId === "string"
      ? tool.iterationId
      : typeof tool.iteration_id === "string"
        ? tool.iteration_id
        : undefined,
    phase: typeof tool.phase === "string" ? tool.phase : undefined,
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
    outputFiles: toOutputFiles(tool.outputFiles ?? tool.output_files),
    cleanupReceipt: tool.cleanupReceipt && typeof tool.cleanupReceipt === "object"
      ? tool.cleanupReceipt as Record<string, unknown>
      : tool.cleanup_receipt && typeof tool.cleanup_receipt === "object"
        ? tool.cleanup_receipt as Record<string, unknown>
        : undefined,
    supersededToolCallIds: toArray<unknown>(
      tool.supersededToolCallIds ?? tool.superseded_tool_call_ids,
    ).map((value) => String(value || "").trim()).filter(Boolean),
    removedFilePaths: toArray<unknown>(
      tool.removedFilePaths ?? tool.removed_file_paths,
    ).map((value) => String(value || "").trim()).filter(Boolean),
    temporaryRemoved: booleanValue(tool.temporaryRemoved ?? tool.temporary_removed),
    diff: normalizeToolDiff(tool.diff),
  };
};

export const normalizeContentBlocks = (value: unknown): ContentBlock[] | undefined => {
  const items = toArray<Record<string, unknown>>(value);
  if (!items.length) return undefined;
  const blocks: ContentBlock[] = [];
  const toolBlockIndexes = new Map<string, number>();
  for (const item of items) {
    const type = String(item.type ?? "").trim();
    if (type === "thinking") {
      if (isHiddenProviderReasoning(item) || isTransientProviderReasoning(item)) continue;
      const reasoningType = providerReasoningType(item);
      const itemId = stringValue(item.item_id ?? item.itemId);
      const contentIndex = numberValue(item.content_index ?? item.contentIndex);
      const lifecycle = stringValue(item.lifecycle);
      blocks.push({
        type: "thinking",
        content: typeof item.content === "string" ? item.content : "",
        source: stringValue(item.source),
        visibility: stringValue(item.visibility),
        phase: stringValue(item.phase),
        providerReasoningType: reasoningType || undefined,
        ...(itemId ? { item_id: itemId } : {}),
        ...(contentIndex !== undefined && Number.isSafeInteger(contentIndex) && contentIndex >= 0
          ? { content_index: contentIndex }
          : {}),
        ...(lifecycle ? { lifecycle } : {}),
      });
      continue;
    }
    if (type === "text") {
      const legacyVisibility = stringValue(item.visibility);
      const legacyPhase = stringValue(item.phase);
      const legacyRole = stringValue(item.role);
      const legacySource = stringValue(item.source);
      if (
        legacyVisibility === "timeline"
        || legacyVisibility === "debug"
        || legacyPhase === "commentary"
        || legacyRole === "runtime"
        || ["model_preamble", "post_tool", "runtime"].includes(legacySource || "")
      ) {
        blocks.push({
          type: "process",
          id: stringValue(item.itemId ?? item.item_id) || `legacy-process-${blocks.length}`,
          itemKind: "process_text",
          content: typeof item.content === "string" ? item.content : "",
          source: legacySource || "runtime",
          role: legacyRole,
          status: "completed",
          visibility: legacyVisibility === "debug" ? "debug" : "timeline",
          timestamp: toTimestamp(item.timestamp),
        });
        continue;
      }
      const isStreaming = booleanValue(item.isStreaming ?? item.is_streaming) === true
        || legacyVisibility === "draft"
        || legacyVisibility === "unsealed";
      blocks.push({
        type: "text",
        itemId: stringValue(item.itemId ?? item.item_id) || "agent-message",
        content: typeof item.content === "string" ? item.content : "",
        source: legacySource || (isStreaming ? undefined : "model_final"),
        status: stringValue(item.status) || (isStreaming ? "in_progress" : "completed"),
        isStreaming,
        providerRaw: item.providerRaw && typeof item.providerRaw === "object"
          ? item.providerRaw as Extract<ContentBlock, { type: "text" }>["providerRaw"]
          : item.provider_raw && typeof item.provider_raw === "object"
            ? item.provider_raw as Extract<ContentBlock, { type: "text" }>["providerRaw"]
            : undefined,
        finishReason: stringValue(item.finishReason ?? item.finish_reason),
      });
      continue;
    }
    if (type === "process") {
      const id = String(item.id ?? item.item_id ?? "").trim();
      const itemKind = String(item.itemKind ?? item.item_kind ?? item.kind ?? "").trim();
      if (
        !id
        || !itemKind
        || String(item.status ?? "").trim().toLowerCase() === "retracted"
      ) continue;
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
        skillName: stringValue(item.skillName ?? item.skill_name),
        triggerMode: stringValue(item.triggerMode ?? item.trigger_mode),
        sourceLevel: stringValue(item.sourceLevel ?? item.source_level),
        reason: stringValue(item.reason),
        tokenEstimate: numberValue(item.tokenEstimate ?? item.token_estimate),
        seq: numberValue(item.seq),
        order: numberValue(item.order),
        timestamp: toTimestamp(item.timestamp),
      });
      continue;
    }
    if (type === "tool_call") {
      const record = toToolCallRecord(item.record ?? item);
      if (record) {
        const existingIndex = toolBlockIndexes.get(record.id);
        if (existingIndex == null) {
          toolBlockIndexes.set(record.id, blocks.length);
          blocks.push({ type: "tool_call", record });
        } else {
          const existing = blocks[existingIndex];
          if (existing?.type === "tool_call") {
            const terminal = new Set(["success", "failed", "blocked", "partial", "timeout", "cancelled"]);
            const keepExistingTerminal = terminal.has(existing.record.status) && !terminal.has(record.status);
            if (!keepExistingTerminal) {
              blocks[existingIndex] = {
                type: "tool_call",
                record: {
                  ...existing.record,
                  ...record,
                  startedAt: Math.min(existing.record.startedAt, record.startedAt),
                },
              };
            }
          }
        }
      }
      continue;
    }
    if (type === "progress") {
      const id = String(item.id ?? "").trim();
      const message = String(item.message ?? "").trim();
      if (!id || !message) continue;
      const rawProviderState = item.providerState ?? item.provider_state;
      const providerState = isAgentProgressProviderState(rawProviderState)
        ? rawProviderState
        : undefined;
      blocks.push({
        type: "progress",
        id,
        stage: isProgressStage(item.stage) ? item.stage : "status",
        phase: isAgentProgressPhase(item.phase) ? item.phase : undefined,
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
        count: nonNegativeNumberValue(item.count),
        iterationId: stringValue(item.iterationId ?? item.iteration_id),
        ephemeral: booleanValue(item.ephemeral),
        retryAttempt: nonNegativeNumberValue(item.retryAttempt ?? item.retry_attempt),
        maxRetries: nonNegativeNumberValue(item.maxRetries ?? item.max_retries),
        retryAfterMs: nonNegativeNumberValue(item.retryAfterMs ?? item.retry_after_ms),
        errorMessage: stringValue(item.errorMessage ?? item.error_message),
        operationId: stringValue(item.operationId ?? item.operation_id),
        providerState,
        timestamp: toTimestamp(item.timestamp),
      });
    }
  }
  return blocks.length ? blocks : undefined;
};

const legacyBlocksFor = (
  message: BackendTranscriptMessage,
  role: ChatMessage["role"],
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
    blocks.push(role === "assistant"
      ? {
          type: "text",
          itemId: "agent-message",
          content: message.content,
          source: "model_final",
          status: "completed",
          isStreaming: false,
        }
      : { type: "text", content: message.content });
  }
  return blocks.length ? blocks : undefined;
};

const normalizeText = (value: string): string => value.trim().replace(/\s+/g, " ");

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
      dataUrl: typeof item.dataUrl === "string" ? item.dataUrl : undefined,
      inputSource: item.input_source === "pasted_text" || item.inputSource === "pasted_text"
        ? "pasted_text"
        : "upload",
      sourceCharCount: Number(item.source_char_count ?? item.sourceCharCount ?? 0) || undefined,
    }];
  });
};

const toContextRefs = (value: unknown): MessageContextRef[] => {
  return toArray<Record<string, unknown>>(value).reduce<MessageContextRef[]>((refs, item) => {
    const kind = String(item.kind ?? "").trim();
    const name = String(item.name ?? "").trim();
    const path = String(item.path ?? "").trim();
    if (kind === "skill" && name) {
      refs.push({ kind, name, path: path || undefined });
      return refs;
    }
    if (kind === "plugin") {
      const configName = String(item.configName ?? item.config_name ?? name).trim();
      if (configName && path.startsWith("plugin://")) {
        refs.push({ kind, name: name || configName, configName, path });
      }
    }
    return refs;
  }, []);
};

const toReplyAttachments = (value: unknown): ChatMessage["replyAttachments"] => {
  const items = toArray<Record<string, unknown>>(value);
  const seenPaths = new Set<string>();
  const attachments = items.flatMap((item) => {
    const path = String(item.path ?? "").trim();
    const pathKey = workspacePathComparisonKey(path);
    if (!path || seenPaths.has(pathKey)) return [];
    seenPaths.add(pathKey);
    const size = Number(item.size ?? 0);
    return [{
      path,
      size: Number.isFinite(size) && size > 0 ? size : 0,
      isImage: item.isImage === true || item.is_image === true,
    }];
  });
  return attachments.length ? attachments : undefined;
};

const dedupeHydratedMessages = (messages: ChatMessage[]): ChatMessage[] => {
  const result: ChatMessage[] = [];
  for (const message of messages) {
    const previous = result.at(-1);
    const isDuplicateUserEcho = previous
      && previous.role === "user"
      && message.role === "user"
      && previous.id === message.id;
    if (isDuplicateUserEcho) continue;
    result.push(message);
  }
  return result;
};

export const normalizeLiveTranscriptMessages = (
  messages: ChatMessage[],
  live: boolean,
): ChatMessage[] => {
  if (!live) return messages;
  let lastAssistantIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === "assistant") {
      lastAssistantIndex = index;
      break;
    }
  }
  if (lastAssistantIndex < 0) return messages;
  const assistant = messages[lastAssistantIndex];
  if (
    assistant.isStreaming === true
    || assistant.terminalStatus != null
    || assistant.completedAt != null
    || assistant.durationMs != null
  ) return messages;
  return messages.map((message, index) => index === lastAssistantIndex
    ? { ...message, isStreaming: true }
    : message);
};

export const hydrateMessages = (
  messages: BackendTranscriptMessage[] | undefined,
  options: HydrateMessagesOptions = {},
): ChatMessage[] => {
  const hydrated = (messages ?? []).map((message, index) => {
    const role = toRole(message.role);
    const content = transcriptContent(message);
    const parsedBlocks = normalizeContentBlocks(message.blocks);
    const fallbackToolCalls = toArray<unknown>(message.tool_calls ?? message.toolCalls).flatMap((item) => {
      const record = toToolCallRecord(item);
      return record ? [record] : [];
    });
    // Structured transcripts are authoritative: hydration never infers a
    // final answer from message.content or block position.
    // Only the pre-block legacy schema is upgraded at this compatibility edge.
    const blocks = parsedBlocks ?? legacyBlocksFor(message, role, fallbackToolCalls);
    const timestamp = toTimestamp(message.timestamp, index);
    return {
      id: typeof message.id === "string" && message.id ? message.id : `m-${index}-${timestamp}`,
      role,
      content,
      messageSource: toMessageSource(message.metadata),
      blocks,
      artifacts: toArray<ArtifactPreview>(message.artifacts),
      attachmentRefs: toAttachmentRefs(message.attachmentRefs ?? message.attachments),
      contextRefs: toContextRefs(message.contextRefs ?? message.context_refs),
      replyAttachments: toReplyAttachments(message.replyAttachments ?? message.reply_attachments),
      citations: toArray<Citation>(message.citations),
      usage: toUsage(message.usage),
      timestamp,
      completedAt: message.completedAt != null || message.completed_at != null
        ? toTimestamp(message.completedAt ?? message.completed_at, timestamp)
        : undefined,
      durationMs: message.durationMs != null || message.duration_ms != null
        ? toTimestamp(message.durationMs ?? message.duration_ms)
        : undefined,
      isStreaming: typeof (message.isStreaming ?? message.is_streaming) === "boolean"
        ? Boolean(message.isStreaming ?? message.is_streaming)
        : undefined,
      isThinkingStreaming: typeof (message.isThinkingStreaming ?? message.is_thinking_streaming) === "boolean"
        ? Boolean(message.isThinkingStreaming ?? message.is_thinking_streaming)
        : undefined,
      terminalStatus: normalizeTerminalStatus(message.terminalStatus ?? message.terminal_status),
      terminationReason: typeof (message.terminationReason ?? message.termination_reason) === "string"
        ? String(message.terminationReason ?? message.termination_reason)
        : undefined,
      failureMessage: typeof (message.failureMessage ?? message.failure_message) === "string"
        ? String(message.failureMessage ?? message.failure_message)
        : undefined,
      failureRecoverable: typeof (message.failureRecoverable ?? message.failure_recoverable) === "boolean"
        ? Boolean(message.failureRecoverable ?? message.failure_recoverable)
        : undefined,
      steeredIntoMessageId: message.steered === true && typeof message.steer_target_message_id === "string"
        ? message.steer_target_message_id
        : undefined,
      ...(role === "system" && transcriptNoticeTitle(message)
        ? { systemNoticeTitle: transcriptNoticeTitle(message) }
        : {}),
    };
  });

  const projected: ChatMessage[] = [];
  const pendingToolResults = new Map<string, {
    content: string;
    failed: boolean;
    timestamp: number;
    projectedIndex: number;
  }>();
  const mergedToolResultIndexes = new Set<number>();
  for (let index = 0; index < hydrated.length; index += 1) {
    const source = (messages ?? [])[index] as (BackendTranscriptMessage & Record<string, unknown>) | undefined;
    const message = hydrated[index];
    if (!source || !message) continue;
    const sourceRole = String(source.role ?? "");
    if (sourceRole === "tool" || sourceRole === "tool_result" || sourceRole === "toolResult") {
      const callId = String(source.tool_call_id ?? source.toolCallId ?? "").trim();
      let merged = false;
      for (let messageIndex = projected.length - 1; messageIndex >= 0 && !merged; messageIndex -= 1) {
        const candidate = projected[messageIndex];
        for (const block of candidate?.blocks ?? []) {
          if (block.type !== "tool_call" || block.record.id !== callId) continue;
          block.record.status = source.is_error === true || source.isError === true ? "failed" : "success";
          block.record.outputPreview = message.content;
          block.record.finishedAt = message.timestamp;
          merged = true;
          break;
        }
      }
      if (merged) continue;
      if (callId) {
        const projectedIndex = projected.length;
        const toolName = String(source.name ?? source.toolName ?? "tool").trim() || "tool";
        message.content = message.content ? `${toolName}: ${message.content}` : `${toolName} completed`;
        message.role = "system";
        projected.push(message);
        pendingToolResults.set(callId, {
          content: message.content,
          failed: source.is_error === true || source.isError === true,
          timestamp: message.timestamp,
          projectedIndex,
        });
        continue;
      }
      const toolName = String(source.name ?? source.toolName ?? "tool").trim() || "tool";
      message.content = message.content ? `${toolName}: ${message.content}` : `${toolName} completed`;
      message.role = "system";
    }
    if (sourceRole === "custom" && source.display === false) continue;
    for (const block of message.blocks ?? []) {
      if (block.type !== "tool_call") continue;
      const pending = pendingToolResults.get(block.record.id);
      if (!pending) continue;
      block.record.status = pending.failed ? "failed" : "success";
      block.record.outputPreview = pending.content;
      block.record.finishedAt = pending.timestamp;
      mergedToolResultIndexes.add(pending.projectedIndex);
      pendingToolResults.delete(block.record.id);
    }
    if (
      !message.content
      && !(message.blocks?.length)
      && !(message.terminalStatus === "failed" && message.failureMessage)
    ) continue;
    projected.push(message);
  }
  const projectedMessages = dedupeHydratedMessages(
    projected.filter((_message, index) => !mergedToolResultIndexes.has(index)),
  );
  return normalizeLiveTranscriptMessages(projectedMessages, Boolean(options.live));
};
