import { useAppStore } from "../stores";
import type {
  CommandOutputChunkEvent,
  ImageChunkEvent,
  ServerEvent,
  ToolOutputDeltaEvent,
  ToolResultEvent,
} from "../protocol/events";
import { isReplayedEvent as isReplayedChatEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { applyUserMessageQueueUpdate } from "./sessionEvents";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import {
  isCommandToolRecord,
  isTerminalToolCallStatus,
  normalizeToolDiff,
  reduceToolCallResult,
  reduceToolCallStart,
  type ToolCallRecord,
} from "../lib/tool-call-reducer";
import { pushToast } from "../overlays/ToastContainer";
import type { ChatMessage, ChatSlice, PendingToolCallResume } from "../stores/types";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { resetSendDeduplication } from "./sendChatMessage";
import { addInspectorPayload } from "./inspectorEntries";
import { providerTracePayloadFromDone, type ProviderUsageSummary } from "./providerTrace";
import { normalizeContentBlocks } from "./transcriptHydration";
import { projectArtifactPreviewEvent } from "./artifactEvents";
import { isHiddenProviderReasoning } from "../lib/provider-reasoning";
import { eventMessageId, stableTextHash } from "../lib/identity";

interface ChatStreamHandlers {
  textStreamBuffer: StreamBuffer;
  thinkingStreamBuffer?: StreamBuffer;
}

const flushLiveBuffers = ({ textStreamBuffer, thinkingStreamBuffer }: ChatStreamHandlers) => {
  thinkingStreamBuffer?.flush();
  textStreamBuffer.flush();
};

const adoptGeneratedConversation = (conversationId?: string) => {
  const targetId = conversationId?.trim();
  if (!targetId) return;
  useAppStore.setState((state) => {
    if (state.conversationId || state.sideChats[targetId]) return state;
    // 只采用当前会话的消息，如果没有conversationId说明是首次，应该使用当前messages
    const messages = state.messages;
    const conversations = state.conversations.some((conversation) => conversation.id === targetId)
      ? state.conversations
      : [{ id: targetId, title: "新会话", updatedAt: new Date().toISOString() }, ...state.conversations];
    return {
      conversationId: targetId,
      conversations,
      conversationMessages: {
        ...state.conversationMessages,
        [targetId]: messages,
      },
      conversationStreaming: {
        ...state.conversationStreaming,
        [targetId]: true,
      },
      isStreaming: true,
    };
  });
};

const clearMissingWorkspaceBinding = (conversationId?: string) => {
  const owner = conversationId?.trim();
  if (!owner) return;
  useAppStore.setState((state) => {
    const targetId = owner;
    return {
      workingDirectory: "",
      workspaceGit: null,
      fileTreeVersion: state.fileTreeVersion + 1,
      conversations: state.conversations.map((conversation) =>
        !targetId || conversation.id === targetId
          ? { ...conversation, workspaceRoot: "", worktreePath: "", gitIsolated: false }
          : conversation,
      ),
    };
  });
};

const isStaleApprovalResponseError = (content: string): boolean =>
  /^(?:Approval|Question) request '.+' is no longer pending$/i.test(content.trim());

const isTransientCommandBacklogError = (err: { error_type?: string; error_code?: string }, message: string): boolean =>
  err.error_code === "command.backlog" ||
  (err.error_type === "rate_limit" && /too many pending commands/i.test(message));

// A recoverable error does not seal the turn, so its sanitized text has to
// survive until the terminal `done` arrives and decides the real status.
// Keyed by the same conversation/message identity as the typed stream items so
// concurrent turns in one conversation cannot consume each other's error.
type PendingRecoverableFailure = { text: string; recoverable: true };
const pendingRecoverableFailures = new Map<string, PendingRecoverableFailure>();
const MAX_PENDING_RECOVERABLE_FAILURES = 256;

const recoverableFailureKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__unowned__"}:${messageId?.trim() || "__latest__"}`;

const rememberRecoverableFailureText = (
  conversationId: string | undefined,
  messageId: string | undefined,
  text: string,
) => {
  const key = recoverableFailureKey(conversationId, messageId);
  if (!text.trim()) return;
  // A provider can disconnect after a recoverable error and before DONE. Keep
  // a bounded FIFO so those orphaned entries cannot grow for the lifetime of
  // the desktop process.
  if (!pendingRecoverableFailures.has(key)) {
    while (pendingRecoverableFailures.size >= MAX_PENDING_RECOVERABLE_FAILURES) {
      const oldest = pendingRecoverableFailures.keys().next().value;
      if (typeof oldest !== "string") break;
      pendingRecoverableFailures.delete(oldest);
    }
  }
  pendingRecoverableFailures.set(key, { text, recoverable: true });
};

const takeRecoverableFailure = (
  conversationId: string | undefined,
  messageId: string | undefined,
): PendingRecoverableFailure | undefined => {
  const key = recoverableFailureKey(conversationId, messageId);
  const fallbackKey = recoverableFailureKey(conversationId);
  const failure = pendingRecoverableFailures.get(key)
    ?? pendingRecoverableFailures.get(fallbackKey);
  pendingRecoverableFailures.delete(key);
  if (fallbackKey !== key) pendingRecoverableFailures.delete(fallbackKey);
  return failure;
};

const recoverMissingConversation = (conversationId?: string) => {
  const missingId = conversationId?.trim();
  if (!missingId) return;
  useAppStore.getState().finishAgentProgress(missingId, "failed");
  useAppStore.getState().finishStreaming(missingId, undefined, "failed");
  useAppStore.getState().clearPendingProviderProgress(missingId);
  useAppStore.setState((state) => {
    const remaining = state.conversations.filter((conversation) => conversation.id !== missingId);
    const { [missingId]: _messages, ...conversationMessages } = state.conversationMessages;
    const { [missingId]: _streaming, ...conversationStreaming } = state.conversationStreaming;
    return {
      conversations: remaining,
      conversationMessages,
      conversationStreaming,
      ...(state.conversationId === missingId
        ? {
            conversationId: null,
            activeGoal: null,
            messages: [] as ChatMessage[],
            isStreaming: false,
            toolCallCount: 0,
          }
        : {}),
    };
  });
  sendClientCommand({ type: "conversation.list" });
};

const CHAT_SCOPED_EVENT_TYPES = new Set<string>([
  "thinking_delta",
  "thinking",
  "text_delta",
  "agent_message.delta",
  "item.started",
  "item.completed",
  "image_chunk",
  "tool_call",
  "tool_output_delta",
  "command_output_chunk",
  "tool_result",
  "permission.decision",
  "agent.item",
  "done",
  "stream_resume",
]);

type ToolCallScope = { turnId?: string; iterationId?: string; stepId?: string };

type ToolCallResolution = {
  record?: ToolCallRecord;
  /** Scope used to locate the record in the store update. */
  matchScope?: ToolCallScope;
  migrated?: boolean;
  reason?: string;
};

const scopeFields: Array<[keyof ToolCallScope, keyof ToolCallRecord]> = [
  ["turnId", "turnId"],
  ["iterationId", "iterationId"],
  ["stepId", "stepId"],
];

const toolCallScopeFromEvent = (event: { turn_id?: string; iteration_id?: string; step_id?: string }): ToolCallScope | undefined => {
  const scope: ToolCallScope = {};
  if (event.turn_id) scope.turnId = event.turn_id;
  if (event.iteration_id) scope.iterationId = event.iteration_id;
  if (event.step_id) scope.stepId = event.step_id;
  return scope.turnId || scope.iterationId || scope.stepId ? scope : undefined;
};

const toolCallMatchesScope = (
  candidate: ReturnType<typeof getToolCallsFromMessage>[number],
  scope?: ToolCallScope,
): boolean => {
  if (!scope) return true;
  // A supplied scope field is an assertion, not a hint. A candidate that does
  // not carry that field cannot satisfy the assertion (the one explicit
  // lifecycle migration is resolved below before this matcher is used).
  return scopeFields.every(([incomingKey, recordKey]) => {
    const incoming = scope[incomingKey];
    return !incoming || candidate[recordKey] === incoming;
  });
};

const toolCallScopeFromRecord = (record: ToolCallRecord): ToolCallScope | undefined => {
  const scope: ToolCallScope = {};
  if (record.turnId) scope.turnId = record.turnId;
  if (record.iterationId) scope.iterationId = record.iterationId;
  if (record.stepId) scope.stepId = record.stepId;
  return scope.turnId || scope.iterationId || scope.stepId ? scope : undefined;
};

const finiteEventSeq = (event: unknown): number | undefined => {
  const value = Number((event as { seq?: unknown }).seq);
  return Number.isSafeInteger(value) && value >= 0 ? value : undefined;
};

const canMigrateToolCallScope = (
  record: ToolCallRecord,
  incoming: ToolCallScope,
  messageId?: string,
): boolean => {
  if (!messageId || isTerminalToolCallStatus(record.status)) return false;
  if ((record.scopeMigrationCount ?? 0) >= 1) return false;
  let conflicts = 0;
  let additions = 0;
  for (const [incomingKey, recordKey] of scopeFields) {
    const next = incoming[incomingKey];
    if (!next) continue;
    const previous = record[recordKey];
    if (!previous) additions += 1;
    else if (previous !== next) conflicts += 1;
  }
  // Adding missing lifecycle metadata is harmless; changing one identity
  // field is the compatibility bridge for pre-refactor streams whose first
  // event used run_id and later events used the assistant turn id. Two or
  // more conflicting fields are a different call and must not be projected.
  return conflicts <= 1 && (conflicts > 0 || additions > 0);
};

const legacyImageArtifactId = (
  event: ServerEvent,
  conversationId: string,
  image: ImageChunkEvent,
): string => {
  const imageData = "image_data" in image && typeof image.image_data === "string"
    ? image.image_data
    : "";
  const eventIdentity = event.event_id
    || (Number.isSafeInteger(event.seq) ? `seq:${event.seq}` : "")
    || event.timestamp
    || `${imageData.length}:${imageData.slice(0, 64)}:${imageData.slice(-64)}`;
  return `legacy-image-${stableTextHash(
    `${conversationId}:${image.message_id}:${image.media_type}:${eventIdentity}`,
  )}`;
};

const decodedBase64ByteLength = (value: string): number => {
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((value.length * 3) / 4) - padding);
};

const eventTurnId = (event: unknown): string | undefined => {
  const value = (event as { turn_id?: unknown; turnId?: unknown }).turn_id
    ?? (event as { turn_id?: unknown; turnId?: unknown }).turnId;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const isHiddenReasoningEvent = (event: unknown): boolean => {
  if (!event || typeof event !== "object") return false;
  const payload = event as Record<string, unknown>;
  if (payload.type !== "thinking" && payload.type !== "thinking_delta") return false;
  return isHiddenProviderReasoning(payload);
};

const messagesForConversation = (conversationId?: string, messageId?: string): ChatMessage[] => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return [];
  const messages = targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  const targetMessageId = messageId?.trim();
  return targetMessageId ? messages.filter((message) => message.id === targetMessageId) : messages;
};

const hasStreamingAssistantForConversation = (conversationId?: string, messageId?: string): boolean =>
  messagesForConversation(conversationId, messageId).some((message) =>
    message.role === "assistant" && (message.isStreaming || message.isThinkingStreaming),
  );

const hasTerminalAssistantForConversation = (conversationId?: string, messageId?: string): boolean =>
  Boolean(messageId) && messagesForConversation(conversationId, messageId).some((message) =>
    message.role === "assistant" &&
    !message.isStreaming &&
    !message.isThinkingStreaming &&
    Boolean(message.terminalStatus),
  );

const clearStreamingFlagIfNoLiveAssistant = (conversationId?: string) => {
  if (!conversationId?.trim()) return;
  if (hasStreamingAssistantForConversation(conversationId)) return;
  useAppStore.setState((state) => {
    const targetId = conversationId.trim();
    return {
      ...(!targetId || targetId === state.conversationId ? { isStreaming: false } : {}),
      conversationStreaming: targetId
        ? { ...state.conversationStreaming, [targetId]: false }
        : state.conversationStreaming,
    };
  });
};

const resolveToolCall = (
  id: string,
  conversationId?: string,
  scope?: ToolCallScope,
  messageId?: string,
  incomingSeq?: number,
): ToolCallResolution => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return { reason: "missing_conversation_owner" };
  const allMessages = targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  const messages = messageId ? allMessages.filter((message) => message.id === messageId) : allMessages;
  const candidates = messages.flatMap((message) => getToolCallsFromMessage(message))
    .filter((candidate) => candidate.id === id);
  if (candidates.length === 0) return { reason: "tool_call_not_found" };
  const freshCandidates = incomingSeq === undefined
    ? candidates
    : candidates.filter((candidate) => {
        const previous = typeof candidate.seq === "number" && Number.isSafeInteger(candidate.seq)
          ? candidate.seq
          : undefined;
        return previous === undefined || incomingSeq > previous;
      });
  if (freshCandidates.length === 0) return { reason: "stale_event_seq" };
  if (!scope) {
    if (freshCandidates.length !== 1) return { reason: "ambiguous_unscoped_tool_call" };
    return { record: freshCandidates[0] };
  }
  const exact = freshCandidates.filter((candidate) => toolCallMatchesScope(candidate, scope));
  if (exact.length === 1) return { record: exact[0], matchScope: scope };
  if (exact.length > 1) return { reason: "ambiguous_scoped_tool_call" };
  if (freshCandidates.length === 1 && canMigrateToolCallScope(freshCandidates[0], scope, messageId)) {
    return {
      record: freshCandidates[0],
      matchScope: toolCallScopeFromRecord(freshCandidates[0]),
      migrated: true,
    };
  }
  return { reason: "tool_call_scope_mismatch" };
};

const recordToolProjectionRejection = (
  id: string,
  event: ServerEvent,
  resolution: ToolCallResolution,
): void => {
  if (resolution.record) return;
  addInspectorPayload("tool_call", id || "unknown", {
    event: event.type,
    projected: false,
    projection_reason: resolution.reason || "unresolved_tool_call",
    conversation_id: (event as unknown as { conversation_id?: unknown }).conversation_id,
    message_id: eventMessageId(event),
    turn_id: eventTurnId(event),
    iteration_id: (event as unknown as { iteration_id?: unknown }).iteration_id,
    step_id: (event as unknown as { step_id?: unknown }).step_id,
    seq: finiteEventSeq(event),
  });
};

const latestRunningCommandTool = (conversationId?: string, messageId?: string) => {
  const records = messagesForConversation(conversationId, messageId).flatMap(getToolCallsFromMessage);
  return records
    .filter((record) =>
      isCommandToolRecord(record) &&
      (record.status === "running" || record.status === "pending"),
    )
    .at(-1);
};

const appendBoundedOutput = (current: string | undefined, chunk: string): string => {
  const maxChars = 64 * 1024;
  const omissionPattern = /\n\[\.\.\. (\d+) characters omitted \.\.\.\]\n/;
  const existing = current ?? "";
  const previousMarker = existing.match(omissionPattern);
  const previousOmitted = Number(previousMarker?.[1] ?? 0) || 0;
  const retained = previousMarker
    ? existing.replace(omissionPattern, "")
    : existing;
  const combined = `${retained}${chunk}`;
  const totalChars = previousOmitted + combined.length;
  if (totalChars <= maxChars) return combined;

  let marker = "";
  let retainedChars = maxChars;
  let omitted = Math.max(0, totalChars - retainedChars);
  // The marker width depends on the omitted count. Two passes reach a stable
  // width even when the count crosses a decimal boundary.
  for (let pass = 0; pass < 2; pass += 1) {
    marker = `\n[... ${omitted} characters omitted ...]\n`;
    retainedChars = Math.max(0, maxChars - marker.length);
    omitted = Math.max(0, totalChars - retainedChars);
  }
  marker = `\n[... ${omitted} characters omitted ...]\n`;
  retainedChars = Math.max(0, maxChars - marker.length);
  const headChars = Math.floor(retainedChars / 2);
  const tailChars = retainedChars - headChars;
  return `${combined.slice(0, headChars)}${marker}${combined.slice(-tailChars)}`;
};

const staleTurnEventKeys = new Set<string>();
const MAX_STALE_TURN_EVENT_KEYS = 256;
const latestResumeEventSeqs = new Map<string, number>();
const MAX_RESUME_EVENT_SEQ_KEYS = 256;

const turnEventKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__unowned__"}:${messageId?.trim() || ""}`;

const rememberResumeEventSeq = (key: string, eventSeq: number) => {
  if (latestResumeEventSeqs.has(key)) latestResumeEventSeqs.delete(key);
  latestResumeEventSeqs.set(key, eventSeq);
  while (latestResumeEventSeqs.size > MAX_RESUME_EVENT_SEQ_KEYS) {
    const oldest = latestResumeEventSeqs.keys().next().value;
    if (typeof oldest !== "string") break;
    latestResumeEventSeqs.delete(oldest);
  }
};

const streamResumeRejectionReason = (
  event: {
    turn_id?: string;
    stream_status?: string;
    last_event_type?: string;
    event_seq?: number;
  },
  conversationId: string,
  messageId?: string,
): string | undefined => {
  const owner = conversationId.trim();
  const targetMessageId = messageId?.trim();
  if (!owner || !targetMessageId) return "missing_owner_or_message";

  const target = messagesForConversation(owner, targetMessageId).find((message) =>
    message.role === "assistant",
  );
  if (target?.terminalStatus || (target && !target.isStreaming && target.completedAt)) {
    return "target_already_terminal";
  }
  const incomingTurnId = String(event.turn_id || "").trim();
  if (incomingTurnId && target?.turnId && target.turnId !== incomingTurnId) {
    return "turn_mismatch";
  }
  const otherStreamingAssistant = messagesForConversation(owner).some((message) =>
    message.role === "assistant"
    && message.id !== targetMessageId
    && Boolean(message.isStreaming || message.isThinkingStreaming),
  );
  if (otherStreamingAssistant && !target?.isStreaming && !target?.isThinkingStreaming) {
    return "newer_turn_is_streaming";
  }

  const streamStatus = String(event.stream_status || "").trim().toLowerCase();
  if (["completed", "partial", "failed", "cancelled", "interrupted"].includes(streamStatus)) {
    return "snapshot_is_terminal";
  }
  const lastEventType = String(event.last_event_type || "").trim().toLowerCase();
  if (["done", "error", "agent.run.completed"].includes(lastEventType)) {
    return "snapshot_crossed_terminal_fence";
  }

  const eventSeq = Number(event.event_seq);
  if (Number.isSafeInteger(eventSeq) && eventSeq >= 0) {
    const key = `${turnEventKey(owner, targetMessageId)}:${incomingTurnId}`;
    const previous = latestResumeEventSeqs.get(key);
    if (previous !== undefined && eventSeq <= previous) return "stale_event_seq";
  }
  return undefined;
};

const rememberStaleTurnEventKey = (key: string) => {
  // A stale key is only consumed when its late event actually arrives. A turn
  // that is abandoned mid-flight (disconnect, conversation delete) never
  // delivers one, so keep a bounded FIFO instead of growing for the lifetime
  // of the desktop process.
  if (staleTurnEventKeys.has(key)) return;
  while (staleTurnEventKeys.size >= MAX_STALE_TURN_EVENT_KEYS) {
    const oldest = staleTurnEventKeys.values().next().value;
    if (typeof oldest !== "string") break;
    staleTurnEventKeys.delete(oldest);
  }
  staleTurnEventKeys.add(key);
};

const markStaleTurnEventIfMissing = (conversationId?: string, messageId?: string): boolean => {
  // An event without an owner is inspector-only. Never compare it with the
  // active conversation's stream or add it to the stale-turn fence.
  if (!conversationId?.trim()) return false;
  if (!messageId) return false;
  // DONE is the turn's delivery fence. Replayed or delayed item/tool events
  // must never mutate an assistant message that already crossed it.
  if (hasTerminalAssistantForConversation(conversationId, messageId)) return true;
  if (hasStreamingAssistantForConversation(conversationId, messageId)) return false;
  if (!hasStreamingAssistantForConversation(conversationId)) return false;
  rememberStaleTurnEventKey(turnEventKey(conversationId, messageId));
  return true;
};
const activateQueuedTurnFromFirstStreamEvent = (conversationId?: string, messageId?: string): void => {
  const targetConversationId = conversationId?.trim();
  const targetMessageId = messageId?.trim();
  if (!targetConversationId || !targetMessageId) return;
  const queued = messagesForConversation(targetConversationId, targetMessageId).some((message) =>
    message.role === "assistant" && message.queueState === "queued",
  );
  if (!queued) return;
  applyUserMessageQueueUpdate({
    type: "user_message.queue.updated",
    status: "dequeued",
    conversation_id: targetConversationId,
    message_id: targetMessageId,
  });
};

const consumeKnownStaleTurnEvent = (conversationId?: string, messageId?: string): boolean => {
  if (!messageId) return false;
  const key = turnEventKey(conversationId, messageId);
  if (!staleTurnEventKeys.has(key)) return false;
  staleTurnEventKeys.delete(key);
  return true;
};

const terminalMessageIdForEvent = (conversationId?: string, messageId?: string): string | undefined => {
  if (!messageId) return undefined;
  return messageId;
};

const resolveTerminalEventTarget = (
  conversationId?: string,
  messageId?: string,
  turnId?: string,
): { stale: boolean; messageId?: string } => {
  if (messageId && hasTerminalAssistantForConversation(conversationId, messageId)) {
    return { stale: true, messageId };
  }
  const streamingAssistants = messagesForConversation(conversationId).filter((message) =>
    message.role === "assistant" && (message.isStreaming || message.isThinkingStreaming),
  );
  if (messageId) {
    const exact = streamingAssistants.find((message) => message.id === messageId);
    if (exact) {
      return {
        stale: Boolean(turnId && exact.turnId && exact.turnId !== turnId),
        messageId: exact.id,
      };
    }
  }
  if (turnId) {
    const byTurn = streamingAssistants.find((message) => message.turnId === turnId);
    if (byTurn) return { stale: false, messageId: byTurn.id };
  }
  if (streamingAssistants.length > 0 && (messageId || turnId)) return { stale: true };
  return { stale: false, messageId };
};

const outputPreviewUpdates = (
  current: ReturnType<typeof getToolCallsFromMessage>[number] | undefined,
  chunk: string,
  stream?: string,
) => {
  const outputPreview = appendBoundedOutput(current?.outputPreview, chunk);
  const streamName = stream?.toLowerCase() === "stderr" ? "stderr" : "stdout";
  if (streamName === "stderr") {
    return {
      outputPreview,
      stderrPreview: appendBoundedOutput(current?.stderrPreview, chunk),
    };
  }
  return {
    outputPreview,
    stdoutPreview: appendBoundedOutput(current?.stdoutPreview, chunk),
  };
};

const usageFromDoneEvent = (e: ServerEvent): NonNullable<ChatSlice["lastUsage"]> | undefined => {
  const usage = (e as unknown as { usage?: { input_tokens?: number; ordinary_input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number; cache_deleted_input_tokens?: number; prompt_cache_total_tokens?: number; prompt_cache_hit_rate?: number; reasoning_output_tokens?: number; input_includes_cache_read?: boolean; input_includes_cache_write?: boolean } }).usage;
  if (!usage) return undefined;
  const result: NonNullable<ChatSlice["lastUsage"]> = {
    input: usage.input_tokens ?? 0,
    ordinaryInput: usage.ordinary_input_tokens,
    inputIncludesCacheRead: usage.input_includes_cache_read,
    inputIncludesCacheWrite: usage.input_includes_cache_write,
    output: usage.output_tokens ?? 0,
    cacheRead: usage.cache_read_input_tokens ?? 0,
    cacheWrite: usage.cache_creation_input_tokens ?? 0,
    reasoning: usage.reasoning_output_tokens ?? 0,
  };
  if (Number(usage.cache_deleted_input_tokens ?? 0) > 0) {
    result.cacheDeleted = Number(usage.cache_deleted_input_tokens);
  }
  if (Number.isFinite(Number(usage.prompt_cache_total_tokens))) {
    result.promptCacheTotal = Number(usage.prompt_cache_total_tokens);
  }
  if (Number.isFinite(Number(usage.prompt_cache_hit_rate))) {
    result.promptCacheHitRate = Number(usage.prompt_cache_hit_rate);
  }
  return result;
};

const providerRawFromDoneEvent = (e: ServerEvent): Record<string, unknown> | undefined => {
  const value = (e as unknown as { provider_raw?: unknown }).provider_raw;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
};

const providerUsageFromDoneUsage = (
  usage: NonNullable<ChatSlice["lastUsage"]> | undefined,
): ProviderUsageSummary | undefined =>
  usage ? {
    input: usage.input,
    ordinaryInput: usage.ordinaryInput,
    inputIncludesCacheRead: usage.inputIncludesCacheRead,
    inputIncludesCacheWrite: usage.inputIncludesCacheWrite,
    output: usage.output,
    cacheRead: usage.cacheRead,
    cacheWrite: usage.cacheWrite,
    cacheDeleted: usage.cacheDeleted,
    promptCacheTotal: usage.promptCacheTotal,
    promptCacheHitRate: usage.promptCacheHitRate,
    reasoning: usage.reasoning ?? 0,
  } : undefined;

export const handleChatStreamEvent = (
  e: ServerEvent,
  conversationId: string | undefined,
  handlers: ChatStreamHandlers,
): boolean => {
  try {
  const eventOwner = (e as unknown as { conversation_id?: unknown }).conversation_id;
  const explicitConversationId = typeof eventOwner === "string" && eventOwner.trim()
    ? eventOwner.trim()
    : undefined;
  const fallbackConversationId = conversationId?.trim() || undefined;
  // Errors may legitimately be session/global. Never borrow the renderer's
  // active conversation for an unowned error, otherwise an invalid command or
  // transport failure can terminate an unrelated in-flight turn.
  conversationId = e.type === "error"
    ? explicitConversationId
    : explicitConversationId ?? fallbackConversationId;
  if (CHAT_SCOPED_EVENT_TYPES.has(e.type) && !conversationId) {
    addInspectorPayload("message", `unowned:${e.type}:${eventMessageId(e) || "event"}`, {
      event: e.type,
      unowned: true,
      payload: e,
    });
    return true;
  }
  const { textStreamBuffer, thinkingStreamBuffer } = handlers;
  const s = useAppStore.getState();
  if (conversationId && e.type !== "done" && e.type !== "error") {
    adoptGeneratedConversation(conversationId);
    activateQueuedTurnFromFirstStreamEvent(conversationId, eventMessageId(e));
    s.bindStreamingTurn(conversationId, eventMessageId(e), eventTurnId(e));
  }
  switch (e.type) {
    case "thinking_delta":
    case "thinking": {
      const ev = e as unknown as {
        content?: string;
        source?: string;
        visibility?: string;
        phase?: string;
        provider_reasoning_type?: string;
        item_id?: string;
        content_index?: number;
        lifecycle?: "start" | "delta" | "end" | string;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.lifecycle === "start") {
        flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
        s.settleThinking(conversationId, messageId);
      }
      if (!isHiddenReasoningEvent(e) && ev.content) {
        // Don't force-flush the text buffer here: thinking is a parallel visual
        // channel, not a sequence boundary. The text buffer flushes on its own
        // rAF (≤16ms); flushing here defeated coalescing during interleaved
        // text+thinking streaming. (text→thinking ordering is preserved because
        // GLM/Claude emit thinking BEFORE answer text, and tool_call/agent.item
        // still flush at real boundaries below.)
        // Route through the rAF thinking buffer (one store update per frame
        // instead of one per token) when available; fall back to direct write.
        const thinkingMeta = {
          source: ev.source,
          visibility: ev.visibility,
          phase: ev.phase,
          providerReasoningType: ev.provider_reasoning_type,
          item_id: ev.item_id,
          content_index: ev.content_index,
          lifecycle: ev.lifecycle,
        };
        if (thinkingStreamBuffer) {
          thinkingStreamBuffer.push(ev.content, conversationId, undefined, thinkingMeta, messageId);
        } else {
          useAppStore.getState().appendThinkingChunk(ev.content, conversationId, thinkingMeta, messageId);
        }
      }
      if (ev.lifecycle === "end") {
        flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
        useAppStore.getState().settleThinking(conversationId, messageId);
      }
      return true;
    }
    case "item.started": {
      const ev = e as unknown as {
        item?: { id?: string; type?: string; source?: string };
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.item?.type === "agent_message") {
        // An item start is an ordered content boundary. Settle reasoning first
        // so a pending final reasoning delta cannot land behind the empty text
        // block created below and split one reasoning item into two cells.
        flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
        s.startAgentMessage(
          ev.item.id || "agent-message",
          conversationId,
          messageId,
          ev.item.source,
        );
      }
      return true;
    }
    case "agent_message.delta": {
      const ev = e as unknown as { item_id?: string; delta?: string; source?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.delta) {
        textStreamBuffer.push(
          ev.delta,
          conversationId,
          ev.item_id || "agent-message",
          { source: ev.source },
          messageId,
        );
      }
      return true;
    }
    case "item.completed": {
      const ev = e as unknown as {
        item?: { id?: string; type?: string; text?: string; source?: string; status?: string };
        finish_reason?: string;
        provider_raw?: Record<string, unknown>;
        attachments?: Array<{ path: string; size: number; is_image: boolean }>;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (ev.item?.type === "agent_message") {
        s.completeAgentMessage(
          {
            id: ev.item.id || "agent-message",
            text: ev.item.text || "",
            source: ev.item.source,
            status: ev.item.status,
          },
          conversationId,
          { providerRaw: ev.provider_raw, finishReason: ev.finish_reason },
          messageId,
        );
        if (Array.isArray(ev.attachments) && ev.attachments.length > 0) {
          s.setFinalAnswerAttachments(
            conversationId,
            ev.attachments.map((attachment) => ({
              path: attachment.path,
              size: attachment.size,
              isImage: attachment.is_image,
            })),
            messageId,
          );
        }
      }
      return true;
    }
    case "image_chunk": {
      const ev = e as ImageChunkEvent;
      const messageId = eventMessageId(e);
      const artifactId = legacyImageArtifactId(e, conversationId || "", ev);
      if ("image_data_omitted" in ev && ev.image_data_omitted) {
        addInspectorPayload("artifact", artifactId, {
          event: ev.type,
          conversation_id: conversationId,
          message_id: ev.message_id,
          media_type: ev.media_type,
          image_data_size: ev.image_data_size,
          image_data_omitted: true,
          replayed: true,
          projected: false,
        });
        return true;
      }
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (!("image_data" in ev) || typeof ev.image_data !== "string") return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      s.settleThinking(conversationId, messageId);
      const imageData = ev.image_data;
      const imageFormat = ev.media_type.replace(/^image\//, "").toUpperCase();
      const projected = projectArtifactPreviewEvent({
        type: "artifact.preview",
        conversation_id: conversationId || ev.conversation_id,
        message_id: ev.message_id,
        artifact_id: artifactId,
        kind: "image",
        summary: `Generated ${imageFormat} image (legacy stream)`,
        bytes: decodedBase64ByteLength(imageData),
        media_type: ev.media_type,
        url: `data:${ev.media_type};base64,${imageData}`,
      }, conversationId || ev.conversation_id);
      if (!projected) {
        addInspectorPayload("artifact", artifactId, {
          event: ev.type,
          conversation_id: conversationId,
          message_id: ev.message_id,
          media_type: ev.media_type,
          image_data_size: imageData.length,
          decoded_bytes: decodedBase64ByteLength(imageData),
          replayed: false,
          projected: false,
        });
      }
      return true;
    }
    case "tool_call": {
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const scope = toolCallScopeFromEvent(e);
      const resolution = resolveToolCall(
        e.id,
        conversationId,
        scope,
        messageId,
        finiteEventSeq(e),
      );
      const existing = resolution.record;
      if (existing) {
        const nextStatus = isTerminalToolCallStatus(existing.status)
          ? existing.status
          : e.status === "pending"
            ? "pending"
            : "running";
        const patch: Partial<ToolCallRecord> = {
          args: e.args ?? {},
          status: nextStatus,
          displayHint: e.display_hint ?? existing.displayHint,
          inputSummary: e.input_summary ?? existing.inputSummary,
          resultKind: e.result_kind ?? existing.resultKind,
          activityKind: e.activity_kind ?? existing.activityKind,
          visibility: e.visibility ?? existing.visibility,
          groupId: e.group_id ?? existing.groupId,
          stepId: e.step_id ?? existing.stepId,
          turnId: e.turn_id ?? existing.turnId,
          iterationId: e.iteration_id ?? existing.iterationId,
          phase: e.phase ?? existing.phase,
          diff: normalizeToolDiff(e.diff) ?? existing.diff,
          seq: e.seq ?? existing.seq,
          ...(resolution.migrated
            ? { scopeMigrationCount: (existing.scopeMigrationCount ?? 0) + 1 }
            : {}),
        };
        s.updateToolCall(e.id, patch, conversationId, resolution.matchScope, messageId);
      } else if (resolution.reason === "tool_call_not_found") {
        const record = reduceToolCallStart(new Map(), e).get(e.id);
        if (record) s.appendToolCallBlock(record, conversationId, messageId);
      } else {
        recordToolProjectionRejection(e.id, e, resolution);
      }
      addInspectorPayload("tool_call", e.id, {
        event: "tool_call",
        name: e.name,
        args: e.args ?? {},
        result_kind: e.result_kind,
        activity_kind: e.activity_kind,
        visibility: e.visibility,
        display_hint: e.display_hint,
        input_summary: e.input_summary,
        turn_id: e.turn_id,
        iteration_id: e.iteration_id,
        phase: e.phase,
        seq: e.seq,
        projected: Boolean(existing) || resolution.reason === "tool_call_not_found",
        ...(resolution.reason && resolution.reason !== "tool_call_not_found"
          ? { projection_reason: resolution.reason }
          : {}),
      });
      return true;
    }
    case "tool_output_delta": {
      const delta = e as ToolOutputDeltaEvent;
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (delta.id && delta.output) {
        const scope = toolCallScopeFromEvent(delta);
        const resolution = resolveToolCall(
          delta.id,
          conversationId,
          scope,
          messageId,
          finiteEventSeq(delta),
        );
        const existing = resolution.record;
        if (existing) {
          const patch: Partial<ToolCallRecord> = {
            ...outputPreviewUpdates(existing, delta.output, delta.stream),
            ...(resolution.migrated
              ? {
                  ...(delta.turn_id ? { turnId: delta.turn_id } : {}),
                  ...(delta.iteration_id ? { iterationId: delta.iteration_id } : {}),
                  ...(delta.step_id ? { stepId: delta.step_id } : {}),
                  scopeMigrationCount: (existing.scopeMigrationCount ?? 0) + 1,
                }
              : {}),
            ...(finiteEventSeq(delta) !== undefined ? { seq: finiteEventSeq(delta) } : {}),
          };
          s.updateToolCall(delta.id, patch, conversationId, resolution.matchScope, messageId);
        } else {
          recordToolProjectionRejection(delta.id, delta, resolution);
        }
        addInspectorPayload("tool_call", delta.id, {
          event: "tool_output_delta",
          stream: delta.stream ?? "stdout",
          output: delta.output,
          turn_id: delta.turn_id,
          iteration_id: delta.iteration_id,
          step_id: delta.step_id,
          seq: finiteEventSeq(delta),
          projected: Boolean(existing),
          ...(resolution.reason ? { projection_reason: resolution.reason } : {}),
        });
      }
      return true;
    }
    case "command_output_chunk": {
      const ev = e as CommandOutputChunkEvent;
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const toolCallId = String(ev.tool_call_id || ev.id || "").trim();
      const scope = toolCallScopeFromEvent(ev);
      const commandResolution = toolCallId
        ? resolveToolCall(toolCallId, conversationId, scope, messageId, finiteEventSeq(ev))
        : undefined;
      const commandTool = toolCallId
        ? commandResolution?.record
        : latestRunningCommandTool(conversationId, messageId);
      if (commandTool && ev.content) {
        s.updateToolCall(
          commandTool.id,
          outputPreviewUpdates(commandTool, ev.content, ev.stream),
          conversationId,
          // With no tool_call_id the selected latest running command is the
          // compatibility identity.  Its persisted record may predate the
          // turn/iteration scope fields, so applying the event's scope here
          // would reject an otherwise unambiguous legacy output chunk.  An
          // explicit id still uses the resolved scope and remains strict.
          toolCallId ? commandResolution?.matchScope : undefined,
          messageId,
        );
      }
      if (toolCallId && commandResolution && !commandResolution.record) {
        recordToolProjectionRejection(toolCallId, ev, commandResolution);
      }
      addInspectorPayload(
        toolCallId || commandTool ? "tool_call" : "message",
        toolCallId || commandTool?.id || messageId || "command-output",
        {
          event: ev.type,
          conversation_id: conversationId,
          message_id: ev.message_id,
          turn_id: ev.turn_id,
          id: ev.id,
          tool_call_id: ev.tool_call_id,
          stream: ev.stream,
          output: ev.content,
          projected: Boolean(commandTool),
          ...(commandResolution?.reason ? { projection_reason: commandResolution.reason } : {}),
        },
      );
      return true;
    }
    case "tool_result": {
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      for (const supersededId of e.superseded_tool_call_ids ?? []) {
        if (!supersededId || supersededId === e.id) continue;
        useAppStore.getState().updateToolCall(supersededId, {
          temporaryRemoved: true,
          diff: undefined,
        }, conversationId, undefined, messageId);
      }
      if (Array.isArray(e.output_files) && e.output_files.length > 0) {
        useAppStore.getState().setFinalAnswerAttachments(
          conversationId,
          e.output_files.map((file) => ({
            path: file.path,
            size: file.size,
            isImage: Boolean(file.is_image || file.mime_type?.startsWith("image/")),
          })),
          messageId,
        );
      }
      const scope = toolCallScopeFromEvent(e);
      const resolution = resolveToolCall(
        e.id,
        conversationId,
        scope,
        messageId,
        finiteEventSeq(e),
      );
      const toolCall = resolution.record;
      if (toolCall) {
        const updated = reduceToolCallResult(new Map([[e.id, toolCall]]), e).get(e.id);
        if (updated) {
          if (resolution.migrated) {
            updated.scopeMigrationCount = (toolCall.scopeMigrationCount ?? 0) + 1;
            if (e.turn_id) updated.turnId = e.turn_id;
            if (e.iteration_id) updated.iterationId = e.iteration_id;
            if (e.step_id) updated.stepId = e.step_id;
          }
          s.updateToolCall(e.id, updated, conversationId, resolution.matchScope, messageId);
        }
      } else {
        recordToolProjectionRejection(e.id, e, resolution);
      }
      addInspectorPayload("tool_call", e.id, {
        event: "tool_result",
        status: e.status,
        is_error: e.is_error,
        summary: e.summary,
        display_summary: e.display_summary,
        artifact_id: e.artifact_id,
        artifact_kind: e.artifact_kind,
        artifact_media_type: e.artifact_media_type,
        artifact_bytes: e.artifact_bytes,
        diff: e.diff,
        source_url: e.source_url,
        content_preview: e.content_preview,
        error_info: e.error_info,
        developer_detail: e.developer_detail,
        projection: e.projection,
        visibility: e.visibility,
        output_files: e.output_files,
        superseded_tool_call_ids: e.superseded_tool_call_ids,
        removed_file_paths: e.removed_file_paths,
        turn_id: e.turn_id,
        iteration_id: e.iteration_id,
        step_id: e.step_id,
        seq: e.seq,
        projected: Boolean(toolCall),
        ...(resolution.reason ? { projection_reason: resolution.reason } : {}),
      });
      return true;
    }
    case "permission.decision": {
      const ev = e as unknown as {
        tool_call_id?: string;
        tool_name?: string;
        decision?: string;
        source?: string;
        permission_level?: string;
        message?: string;
        capability?: { allowed?: boolean; reason?: string };
        approval_policy?: string;
        matched_rule?: { source?: string; rule?: string };
        risk?: string;
        scope?: Record<string, unknown>;
        expiry?: string;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const decision = ev.decision || "ask";
      const toolName = ev.tool_name || "工具";
      const ruleLabel = ev.matched_rule?.rule || ev.source || "策略";
      const summary = ev.message || (decision === "allow"
        ? "已自动允许"
        : decision === "ask"
          ? `${ruleLabel}要求审批`
          : `已被${ruleLabel}阻止`);
      if (ev.tool_call_id) {
        const resolution = resolveToolCall(ev.tool_call_id, conversationId, undefined, messageId);
        const existing = resolution.record;
        if (existing && !isTerminalToolCallStatus(existing.status)) {
          const patch = decision === "ask"
            ? {
                status: "pending" as const,
                transition: "waiting_approval",
                waitingOn: "approval",
                blockingReason: summary,
              }
            : decision === "deny"
              ? {
                  status: "blocked" as const,
                  transition: "blocked",
                  waitingOn: undefined,
                  blockingReason: summary,
                }
              : {
                  status: "running" as const,
                  transition: "running",
                  waitingOn: undefined,
                  blockingReason: undefined,
          };
          s.updateToolCall(ev.tool_call_id, patch, conversationId, undefined, messageId);
        } else if (!existing) {
          recordToolProjectionRejection(ev.tool_call_id, e, resolution);
        }
        addInspectorPayload("tool_call", ev.tool_call_id, {
          event: "permission.decision",
          tool_name: toolName,
          decision,
          source: ev.source,
          permission_level: ev.permission_level,
          message: ev.message,
          capability: ev.capability,
          approval_policy: ev.approval_policy,
          matched_rule: ev.matched_rule,
          risk: ev.risk,
          scope: ev.scope,
          expiry: ev.expiry,
          projected: Boolean(existing),
          ...(resolution.reason ? { projection_reason: resolution.reason } : {}),
        });
      }
      return true;
    }
    case "agent.item": {
      const ev = e as unknown as {
        id?: string;
        item_id?: string;
        kind?: string;
        content?: string;
        title?: string;
        summary?: string;
        status?: string;
        role?: string;
        source?: string;
        visibility?: string;
        loop_id?: string;
        iteration_id?: string;
        parent_id?: string;
        group_id?: string;
        step_id?: string;
        tool_call_ids?: string[];
        default_collapsed?: boolean;
        skill_name?: string;
        trigger_mode?: string;
        source_level?: string;
        reason?: string;
        token_estimate?: number;
        created_at?: number;
        order?: number;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      const content = ev.content || ev.summary || "";
      const itemId = ev.item_id || ev.id;
      if (
        itemId
        && (ev.status === "retracted" || ev.visibility === "debug")
      ) {
        flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
        addInspectorPayload("message", itemId, {
          event: "agent.item",
          conversation_id: conversationId,
          message_id: messageId,
          item_id: itemId,
          kind: ev.kind,
          status: ev.status,
          visibility: ev.visibility,
          title: ev.title,
          summary: ev.summary,
          content: ev.content,
          source: ev.source,
          reason: ev.reason,
          tool_call_ids: ev.tool_call_ids,
          retracted: ev.status === "retracted",
          replayed: isReplayedChatEvent(e),
        });
        s.removeProcessItem(itemId, conversationId, messageId);
        return true;
      }
      if (itemId && content.trim() && ev.visibility !== "debug") {
        flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
        s.appendProcessItem({
          id: itemId,
          itemKind: ev.kind || "process_text",
          content,
          title: ev.title,
          summary: ev.summary,
          status: ev.status,
          role: ev.role,
          source: ev.source,
          visibility: ev.visibility,
          loopId: ev.loop_id,
          iterationId: ev.iteration_id,
          parentId: ev.parent_id,
          groupId: ev.group_id,
          stepId: ev.step_id,
          toolCallIds: ev.tool_call_ids,
          defaultCollapsed: ev.default_collapsed,
          skillName: ev.skill_name,
          triggerMode: ev.trigger_mode,
          sourceLevel: ev.source_level,
          reason: ev.reason,
          tokenEstimate: ev.token_estimate,
          timestamp: ev.created_at,
          order: ev.order,
        }, conversationId, messageId);
        // File preparation is already represented by the file-change card and
        // diff stream. Do not mirror it into the composer/task progress area.
      }
      return true;
    }
    case "done": {
      const messageId = eventMessageId(e);
      if (!conversationId && !messageId) {
        console.warn("[ws] Ignoring unscoped done event", e);
        return true;
      }
      if (consumeKnownStaleTurnEvent(conversationId, messageId)) return true;
      const target = resolveTerminalEventTarget(conversationId, messageId, eventTurnId(e));
      if (target.stale) return true;
      const terminalMessageId = terminalMessageIdForEvent(conversationId, target.messageId);
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const usage = usageFromDoneEvent(e);
      const replayed = isReplayedChatEvent(e);
      if (!replayed && usage && (!conversationId || conversationId === useAppStore.getState().conversationId)) {
        s.setLastUsage(usage);
      }
      const providerRaw = providerRawFromDoneEvent(e);
      const providerTracePayload = providerTracePayloadFromDone(
        providerRaw,
        providerUsageFromDoneUsage(usage),
      );
      if (providerTracePayload) {
        const traceId = String(providerTracePayload.trace_id || `${terminalMessageId || messageId || conversationId || "provider"}:provider:done`);
        const hasAuthoritativeTrace = useAppStore.getState().inspectorEntries.some((entry) => (
          entry.targetKind === "provider" && entry.targetId === traceId
        ));
        if (!hasAuthoritativeTrace) {
          addInspectorPayload("provider", traceId, {
            ...providerTracePayload,
            conversationId,
            messageId: terminalMessageId || messageId,
          });
        }
      }
      const doneStatus = (e as unknown as { status?: string }).status;
      const terminalStatus =
        doneStatus === "cancelled" || doneStatus === "interrupted" ? "interrupted" :
        doneStatus === "partial" ? "partial" :
        doneStatus === "failed" ? "failed" :
        "completed";
      // Carry the sanitized text of any recoverable error the loop reported
      // earlier in this turn; it only becomes user-visible if the turn in fact
      // ended as failed.
      const recoverableFailure = takeRecoverableFailure(conversationId, terminalMessageId || messageId);
      const doneFailureRecoverable = (e as unknown as {
        failure_recoverable?: unknown;
        failureRecoverable?: unknown;
      }).failure_recoverable ?? (e as unknown as {
        failure_recoverable?: unknown;
        failureRecoverable?: unknown;
      }).failureRecoverable;
      const failureRecoverable = terminalStatus === "failed"
        ? typeof doneFailureRecoverable === "boolean"
          ? doneFailureRecoverable
          : recoverableFailure?.recoverable
        : undefined;
      const durationMs = Number((e as unknown as { duration_ms?: unknown; durationMs?: unknown }).duration_ms
        ?? (e as unknown as { duration_ms?: unknown; durationMs?: unknown }).durationMs);
      s.finishAgentProgress(
        conversationId,
        terminalStatus === "failed" || terminalStatus === "interrupted"
          ? "failed"
          : terminalStatus === "partial"
            ? "partial"
            : "completed",
      );
      s.finishStreaming(
        conversationId,
        usage,
        terminalStatus,
        terminalMessageId,
        recoverableFailure?.text,
        failureRecoverable,
        Number.isFinite(durationMs) ? durationMs : undefined,
        String((e as unknown as { reason?: unknown }).reason || "").trim() || undefined,
      );
      // approval.cancelled is authoritative, but DONE is the terminal fence
      // for the turn. Clear prompts owned by this conversation as a fallback
      // for cancellation races, reconnect gaps, or out-of-order delivery.
      if (!replayed) {
        const latest = useAppStore.getState();
        const promptTargetsConversation = (prompt: { conversationId?: string } | null | undefined) =>
          Boolean(prompt) && (
            prompt?.conversationId === conversationId ||
            (!prompt?.conversationId && latest.conversationId === conversationId)
          );
        const approvalIds = [latest.pendingApproval, ...latest.approvalQueue]
          .filter(promptTargetsConversation)
          .map((approval) => approval!.requestId);
        if (approvalIds.length > 0) {
          latest.clearApprovals(approvalIds);
        }
        const diffReviewIds = [latest.pendingDiffReview, ...latest.diffReviewQueue]
          .filter(promptTargetsConversation)
          .map((review) => review!.requestId);
        if (diffReviewIds.length > 0) {
          latest.clearDiffReviews(diffReviewIds);
        }
        const askUserIds = [latest.pendingAskUser, ...latest.askUserQueue]
          .filter(promptTargetsConversation)
          // A teammate plan review is answered by the teammate's own run, not
          // by this turn. Clearing it here would strand that teammate until its
          // deadline rejects the plan, so only turn-owned questions expire.
          .filter((prompt) => !prompt?.planReview)
          .map((prompt) => prompt!.requestId);
        if (askUserIds.length > 0) {
          latest.clearAskUsers(askUserIds);
        }
        useAppStore.setState((state) => ({
          diffReview: promptTargetsConversation(state.diffReview)
            ? null
            : state.diffReview,
        }));
      }
      if (!replayed && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        const conversation = s.conversations.find((item) => item.id === conversationId);
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: terminalStatus === "completed" ? "MiniCode 任务已完成" : "MiniCode 任务已停止",
          body: conversation?.title || "答复已就绪。",
          ...(conversationId ? { target: { kind: "conversation" as const, conversationId } } : {}),
        }));
      }
      // Refresh the authoritative context/budget snapshot so the usage ring
      // reflects the turn that just completed (done carries token counts but
      // not the budget breakdown). silent: indicator-only, no chat notice.
      if (!replayed) {
        sendClientCommand({ type: "session.usage.inspect", silent: true });
        resetSendDeduplication();
      }
      return true;
    }
    case "error": {
      const messageId = eventMessageId(e);
      const turnId = eventTurnId(e);
      const replayed = isReplayedChatEvent(e);
      if (!conversationId) {
        const globalError = e as unknown as {
          message?: string;
          error_type?: string;
          error_code?: string;
        };
        const rawGlobalMessage = globalError.message ?? "An unexpected error occurred.";
        if (!isStaleApprovalResponseError(rawGlobalMessage) && !replayed) {
          const globalMessage = normalizeAgentErrorMessage(rawGlobalMessage);
          const transient = isTransientCommandBacklogError(globalError, rawGlobalMessage);
          pushToast(globalMessage, transient ? "warning" : "error", transient ? 3000 : 6000);
        }
        return true;
      }
      if (replayed && !messageId && !turnId) {
        const replayError = e as unknown as {
          message?: string;
          error_type?: string;
          error_code?: string;
          recoverable?: boolean;
        };
        addInspectorPayload("message", `error:${conversationId}:${e.event_id || e.seq || "replay"}`, {
          event: "error",
          conversation_id: conversationId,
          error_type: replayError.error_type,
          error_code: replayError.error_code,
          message: normalizeAgentErrorMessage(replayError.message ?? "An unexpected error occurred.", { includeProviderDetails: false }),
          recoverable: replayError.recoverable,
          replayed: true,
          projected: false,
        });
        return true;
      }
      if (consumeKnownStaleTurnEvent(conversationId, messageId)) return true;
      const target = resolveTerminalEventTarget(conversationId, messageId, turnId);
      if (target.stale) return true;
      const terminalMessageId = terminalMessageIdForEvent(conversationId, target.messageId);
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const err = e as unknown as { conversation_id?: string; message_id?: string; message?: string; tool_call_id?: string; request_id?: string; error_type?: string; error_code?: string; provider_error_type?: string; recoverable?: boolean };
      const rawMessage = err.message ?? "发生了错误。";
      if (isStaleApprovalResponseError(rawMessage)) {
        return true;
      }
      if (err.error_code === "conversation.not_found") {
        if (!replayed) {
          recoverMissingConversation(conversationId);
          resetSendDeduplication();
        }
        return true;
      }
      const message = normalizeAgentErrorMessage(rawMessage);
      const transcriptMessage = normalizeAgentErrorMessage(rawMessage, { includeProviderDetails: false });
      if (isTransientCommandBacklogError(err, rawMessage)) {
        if (!replayed) pushToast(message, "warning", 3000);
        return true;
      }
      if (!replayed) pushToast(message, "error", 6000);
      if (err.error_code === "workspace_missing") {
        if (!replayed) clearMissingWorkspaceBinding(conversationId);
        s.finishAgentProgress(conversationId, "failed");
        s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage, err.recoverable === true);
        if (!replayed) resetSendDeduplication();
        return true;
      }
      if (err.error_code === "agent.busy") {
        s.removeEmptyStreamingAssistant(conversationId, target.messageId);
        if (target.messageId && hasStreamingAssistantForConversation(conversationId, target.messageId)) {
          s.finishAgentProgress(conversationId, "failed");
          s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage, err.recoverable === true);
        } else {
          clearStreamingFlagIfNoLiveAssistant(conversationId);
        }
        if (!replayed) resetSendDeduplication();
        return true;
      }
      const requestId = err.tool_call_id ?? err.request_id;
      if (requestId && !replayed) {
        s.clearDiffReview(requestId);
        s.clearAskUser(requestId);
        useAppStore.setState((state) => ({
          pendingApproval: state.pendingApproval?.requestId === requestId
            ? { ...state.pendingApproval, status: "error", error: message }
            : state.pendingApproval,
          approvalQueue: state.approvalQueue.filter((queued) => queued.requestId !== requestId),
          diffReview: state.diffReview?.requestId === requestId
            ? { ...state.diffReview, status: "error", error: message }
            : state.diffReview,
        }));
      }
      if (err.error_type === "blocked" || err.error_type === "billing") {
        useAppStore.setState((state) => ({
          isStreaming: false,
          // Clear conversationStreaming for the error-affected conversation
          // so background conversations don't remain stuck as "running"
          conversationStreaming: conversationId
            ? { ...state.conversationStreaming, [conversationId]: false }
            : state.conversationId
              ? { ...state.conversationStreaming, [state.conversationId]: false }
              : state.conversationStreaming,
        }));
      }
      // A recoverable error is evidence, not terminal authority: the loop keeps
      // running (retry ladder) or is about to commit a partial result, and the
      // `done` event that always follows carries the real terminal status.
      // Sealing the turn here would drop any answer text emitted after it.
      if (err.recoverable === true && err.error_type !== "blocked" && err.error_type !== "billing") {
        rememberRecoverableFailureText(conversationId, terminalMessageId || messageId, transcriptMessage);
        return true;
      }
      s.finishAgentProgress(conversationId, "failed");
      s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage, err.recoverable === true);
      if (!replayed) resetSendDeduplication();
      return true;
    }
    case "stream_resume": {
      flushLiveBuffers(handlers);
      const ev = e as unknown as {
        conversation_id?: string;
        turn_id?: string;
        phase?: string;
        stream_status?: string;
        event_seq?: number;
        last_event_type?: string;
        content_blocks?: Array<Record<string, unknown>>;
        tool_calls_pending?: PendingToolCallResume[];
        tool_states?: PendingToolCallResume[];
      };
      const messageId = eventMessageId(e);
      const resumeConversationId = ev.conversation_id || conversationId || "";
      const pendingToolCalls = ev.tool_calls_pending ?? [];
      const toolStates = ev.tool_states ?? pendingToolCalls;
      const normalizedSnapshotBlocks = Array.isArray(ev.content_blocks)
        ? normalizeContentBlocks(ev.content_blocks) ?? []
        : undefined;
      const snapshotBlocks = normalizedSnapshotBlocks;
      const rejectionReason = streamResumeRejectionReason(
        ev,
        resumeConversationId,
        messageId,
      );
      if (rejectionReason) {
        addInspectorPayload("message", messageId || `${resumeConversationId}:stream_resume`, {
          event: "stream_resume",
          conversation_id: resumeConversationId,
          message_id: messageId,
          turn_id: ev.turn_id,
          event_seq: ev.event_seq,
          stream_status: ev.stream_status,
          last_event_type: ev.last_event_type,
          ignored: true,
          reason: rejectionReason,
          replayed: isReplayedChatEvent(e),
        });
        return true;
      }
      if (
        (snapshotBlocks?.length ?? 0) === 0 &&
        toolStates.length === 0 &&
        !hasStreamingAssistantForConversation(resumeConversationId, messageId)
      ) {
        return true;
      }
      s.resumeStreaming(resumeConversationId, toolStates, messageId, ev.turn_id, snapshotBlocks);
      const eventSeq = Number(ev.event_seq);
      if (Number.isSafeInteger(eventSeq) && eventSeq >= 0) {
        rememberResumeEventSeq(
          `${turnEventKey(resumeConversationId, messageId)}:${String(ev.turn_id || "").trim()}`,
          eventSeq,
        );
      }
      return true;
    }
    default:
      return false;
  }
  } catch (err) {
    console.error("[chatStreamEvents] Unhandled error processing event:", err, e);
    if (!isReplayedChatEvent(e)) {
      if (conversationId) {
        useAppStore.getState().finishStreaming(conversationId, undefined, "failed", eventMessageId(e));
      }
      pushToast("处理流式响应时出错，请重试。", "error", 4000);
    }
    return false;
  }
};
