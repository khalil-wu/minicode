import { useAppStore } from "../stores";
import type { ServerEvent, ToolOutputDeltaEvent, ToolResultEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { applyUserMessageQueueUpdate } from "./sessionEvents";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getContentBlocks, getToolCallsFromMessage } from "../lib/content-blocks";
import {
  reduceToolCallResult,
  reduceToolCallStart,
  type ToolCallRecord,
} from "../lib/tool-call-reducer";
import { pushToast } from "../overlays/ToastContainer";
import type { ChatMessage, ChatSlice, PendingToolCallResume } from "../stores/types";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { resetSendDeduplication } from "./sendChatMessage";
import { addInspectorPayload, maybeAutoRoutePanel } from "./displayRouting";
import { providerTracePayloadFromDone, type ProviderUsageSummary } from "./providerTrace";

interface ChatStreamHandlers {
  textStreamBuffer: StreamBuffer;
  /** @deprecated thinking goes directly to appendThinkingChunk. Remove in v0.4.0 */
  thinkingStreamBuffer?: StreamBuffer;
}

const flushLiveBuffers = ({ textStreamBuffer, thinkingStreamBuffer }: ChatStreamHandlers) => {
  textStreamBuffer.flush();
  thinkingStreamBuffer?.flush();
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
      : [{ id: targetId, title: "New chat", updatedAt: new Date().toISOString() }, ...state.conversations];
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
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId;
    return {
      appMode: "cowork",
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

const isWriteLikeTool = (toolName: string): boolean =>
  /(?:write|edit|patch|replace|delete|rename|create|move|save)/i.test(toolName);

const requestPreviewValidationForTool = (toolName: string) => {
  if (!isWriteLikeTool(toolName)) return;
  const state = useAppStore.getState();
  if (!state.livePreviewUrl) return;
  window.dispatchEvent(new Event("preview:auto-refresh"));
  sendClientCommand({ type: "preview.verify", url: state.livePreviewUrl });
};

const isRawProviderErrorText = (content: string): boolean => {
  // Suppress only FULL provider-error messages (the kind emitted when a model
  // call fails and its raw error text leaks as the chunk), never substrings.
  // A legitimate draft answer that mentions "a 429 rate limit response" must
  // not be dropped — a filtered non-finalize chunk is permanently lost, because
  // the later in-place finalize seals only the accumulated buffer text.
  const trimmed = content.trim().replace(/^Error:\s*/i, "");
  return /^(?:Claude API 调用失败|LLM API 调用失败|LLM API request failed|Concurrency limit exceeded)/i.test(trimmed);
};

const isStaleApprovalResponseError = (content: string): boolean =>
  /^(?:Approval|Question) request '.+' is no longer pending$/i.test(content.trim());

const isTransientCommandBacklogError = (err: { error_type?: string; error_code?: string }, message: string): boolean =>
  err.error_code === "command.backlog" ||
  (err.error_type === "rate_limit" && /too many pending commands/i.test(message));

const TOOL_ONLY_NO_REPLY_MESSAGE = "Tool calls failed before the assistant produced a reply.";

const hasVisibleAssistantReply = (conversationId?: string, messageId?: string): boolean =>
  messagesForConversation(conversationId, messageId).some((message) => {
    if (message.role !== "assistant") return false;
    if (String(message.content || "").trim()) return true;
    return getContentBlocks(message).some((block) =>
      block.type === "text" &&
      String(block.content || "").trim() &&
      block.visibility !== "timeline" &&
      block.visibility !== "debug",
    );
  });

const isToolOnlyNoReplyFallback = (message: string): boolean =>
  message.replace(/^Error:\s*/i, "").trim() === TOOL_ONLY_NO_REPLY_MESSAGE;

const recoverMissingConversation = (conversationId?: string) => {
  const missingId = conversationId?.trim();
  if (!missingId) return;
  useAppStore.getState().finishAgentProgress(missingId, "failed");
  useAppStore.getState().finishStreaming(missingId, undefined, "failed");
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

const isActiveConversationEvent = (conversationId?: string): boolean => {
  if (!conversationId) return true;
  return useAppStore.getState().conversationId === conversationId;
};

type ToolCallScope = { turnId?: string; iterationId?: string; stepId?: string };

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
  if (scope.turnId && candidate.turnId && candidate.turnId !== scope.turnId) return false;
  if (scope.iterationId && candidate.iterationId && candidate.iterationId !== scope.iterationId) return false;
  if (scope.stepId && candidate.stepId && candidate.stepId !== scope.stepId) return false;
  return true;
};

const eventMessageId = (event: unknown): string | undefined => {
  const value = (event as { message_id?: unknown; messageId?: unknown }).message_id
    ?? (event as { message_id?: unknown; messageId?: unknown }).messageId;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const eventTurnId = (event: unknown): string | undefined => {
  const value = (event as { turn_id?: unknown; turnId?: unknown }).turn_id
    ?? (event as { turn_id?: unknown; turnId?: unknown }).turnId;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const messagesForConversation = (conversationId?: string, messageId?: string): ChatMessage[] => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  const messages = !targetId
    ? state.messages
    : targetId === state.conversationId
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
  if (hasStreamingAssistantForConversation(conversationId)) return;
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId || undefined;
    return {
      ...(!targetId || targetId === state.conversationId ? { isStreaming: false } : {}),
      conversationStreaming: targetId
        ? { ...state.conversationStreaming, [targetId]: false }
        : state.conversationStreaming,
    };
  });
};

const findToolCall = (id: string, conversationId?: string, scope?: ToolCallScope, messageId?: string) => {
  const state = useAppStore.getState();
  const allMessages = conversationId
    ? conversationId === state.conversationId
      ? state.messages
      : state.sideChats[conversationId]?.messages ?? state.conversationMessages[conversationId] ?? []
    : state.messages;
  const messages = messageId ? allMessages.filter((message) => message.id === messageId) : allMessages;
  for (const message of messages) {
    const toolCall = getToolCallsFromMessage(message).find((candidate) =>
      candidate.id === id && toolCallMatchesScope(candidate, scope),
    );
    if (toolCall) return toolCall;
  }
  return undefined;
};

const COMMAND_OUTPUT_PREVIEW_LIMIT = 60_000;
const isCommandLikeTool = (record: ReturnType<typeof getToolCallsFromMessage>[number]): boolean =>
  String(record.resultKind || "").toLowerCase() === "command" ||
  record.activityKind === "commandExecution" ||
  /(?:run_command|terminal|shell|bash|powershell|cmd)/i.test(record.name);

const latestRunningCommandTool = (conversationId?: string, messageId?: string) => {
  const records = messagesForConversation(conversationId, messageId).flatMap(getToolCallsFromMessage);
  return records
    .filter((record) =>
      isCommandLikeTool(record) &&
      (record.status === "running" || record.status === "pending"),
    )
    .at(-1);
};

const appendBoundedOutput = (current: string | undefined, chunk: string): string => {
  const next = `${current ?? ""}${chunk}`;
  if (next.length <= COMMAND_OUTPUT_PREVIEW_LIMIT) return next;
  return `[output truncated: showing latest ${COMMAND_OUTPUT_PREVIEW_LIMIT} chars]\n${next.slice(-COMMAND_OUTPUT_PREVIEW_LIMIT)}`;
};

const streamingKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__active__"}:${messageId?.trim() || "__latest__"}`;

const staleTurnEventKeys = new Set<string>();

const turnEventKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__active__"}:${messageId?.trim() || ""}`;

const markStaleTurnEventIfMissing = (conversationId?: string, messageId?: string): boolean => {
  if (!messageId) return false;
  if (hasStreamingAssistantForConversation(conversationId, messageId)) return false;
  if (!hasStreamingAssistantForConversation(conversationId)) return false;
  staleTurnEventKeys.add(turnEventKey(conversationId, messageId));
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

const progressStatusFromLoop = (
  eventType: string,
  status?: string,
): "running" | "completed" | "failed" | "info" => {
  if (eventType === "agent.loop.started") return "running";
  if (status === "failed" || status === "interrupted" || status === "cancelled") return "failed";
  if (status === "running") return "running";
  if (status === "info") return "info";
  return "completed";
};

const appendSystemMessage = (message: ChatMessage, conversationId?: string) => {
  const state = useAppStore.getState();
  const targetId = conversationId || state.conversationId;
  if (!targetId) {
    useAppStore.setState({ messages: [...state.messages, message] });
    return;
  }
  if (state.sideChats[targetId]) {
    const thread = state.sideChats[targetId];
    useAppStore.setState({
      sideChats: {
        ...state.sideChats,
        [targetId]: { ...thread, messages: [...thread.messages, message] },
      },
    });
    return;
  }
  if (targetId === state.conversationId) {
    state.hydrateConversationMessages(targetId, [...state.messages, message], { activate: true });
    return;
  }
  useAppStore.setState({
    conversationMessages: {
      ...state.conversationMessages,
      [targetId]: [...(state.conversationMessages[targetId] ?? []), message],
    },
  });
};

const usageFromDoneEvent = (e: ServerEvent): NonNullable<ChatSlice["lastUsage"]> | undefined => {
  const usage = (e as unknown as { usage?: { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number; prompt_cache_total_tokens?: number; prompt_cache_hit_rate?: number; reasoning_output_tokens?: number } }).usage;
  if (!usage) return undefined;
  const result: NonNullable<ChatSlice["lastUsage"]> = {
    input: usage.input_tokens ?? 0,
    output: usage.output_tokens ?? 0,
    cacheRead: usage.cache_read_input_tokens ?? 0,
    cacheWrite: usage.cache_creation_input_tokens ?? 0,
    reasoning: usage.reasoning_output_tokens ?? 0,
  };
  if (Number.isFinite(Number(usage.prompt_cache_total_tokens))) {
    result.promptCacheTotal = Number(usage.prompt_cache_total_tokens);
  }
  if (Number.isFinite(Number(usage.prompt_cache_hit_rate))) {
    result.promptCacheHitRate = Number(usage.prompt_cache_hit_rate);
  }
  return result;
};

const providerRawFromDoneEvent = (e: ServerEvent): Record<string, unknown> | undefined => {
  const value = (e as unknown as { providerRaw?: unknown; provider_raw?: unknown }).providerRaw
    ?? (e as unknown as { providerRaw?: unknown; provider_raw?: unknown }).provider_raw;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
};

const providerUsageFromDoneUsage = (
  usage: NonNullable<ChatSlice["lastUsage"]> | undefined,
): ProviderUsageSummary | undefined =>
  usage ? {
    input: usage.input,
    output: usage.output,
    cacheRead: usage.cacheRead,
    cacheWrite: usage.cacheWrite,
    promptCacheTotal: usage.promptCacheTotal,
    promptCacheHitRate: usage.promptCacheHitRate,
    reasoning: usage.reasoning ?? 0,
  } : undefined;

const normalizeTextPhase = (phase: string | undefined): string | undefined =>
  phase === "final_answer" ? "final" : phase;

const textEventMetadata = (ev: {
  visibility?: string;
  role?: string;
  phase?: string;
  metadata?: {
    visibility?: string;
    role?: string;
    phase?: string;
    segmentId?: string;
    segment_id?: string;
    iterationIndex?: number;
    iteration_index?: number;
    streamAttempt?: number;
    stream_attempt?: number;
    sealReason?: string;
    seal_reason?: string;
    sealed?: boolean;
    promoteAllUnsealedNarration?: boolean;
    promote_all_unsealed_narration?: boolean;
    providerRaw?: Record<string, unknown>;
    finishReason?: string;
  };
  segmentId?: string;
  segment_id?: string;
  iterationIndex?: number;
  iteration_index?: number;
  streamAttempt?: number;
  stream_attempt?: number;
    sealReason?: string;
    seal_reason?: string;
    promoteAllUnsealedNarration?: boolean;
    promote_all_unsealed_narration?: boolean;
    providerRaw?: Record<string, unknown>;
    finishReason?: string;
}) => {
  const incoming = ev.metadata ?? {};
  const metadata: {
    visibility?: string;
    role?: string;
    phase?: string;
    segmentId?: string;
    iterationIndex?: number;
    streamAttempt?: number;
    sealReason?: string;
    sealed?: boolean;
    promoteAllUnsealedNarration?: boolean;
    providerRaw?: Record<string, unknown>;
    finishReason?: string;
  } = {};
  const visibility = ev.visibility ?? incoming.visibility;
  const role = ev.role ?? incoming.role;
  const phase = normalizeTextPhase(ev.phase ?? incoming.phase);
  const segmentId = ev.segmentId ?? ev.segment_id ?? incoming.segmentId ?? incoming.segment_id;
  const iterationIndex = ev.iterationIndex ?? ev.iteration_index ?? incoming.iterationIndex ?? incoming.iteration_index;
  const streamAttempt = ev.streamAttempt ?? ev.stream_attempt ?? incoming.streamAttempt ?? incoming.stream_attempt;
  const sealReason = ev.sealReason ?? ev.seal_reason ?? incoming.sealReason ?? incoming.seal_reason;
  const promoteAllUnsealedNarration =
    ev.promoteAllUnsealedNarration ??
    ev.promote_all_unsealed_narration ??
    incoming.promoteAllUnsealedNarration ??
    incoming.promote_all_unsealed_narration;
  const providerRaw = ev.providerRaw ?? incoming.providerRaw;
  const finishReason = ev.finishReason ?? incoming.finishReason;
  if (visibility !== undefined) metadata.visibility = visibility;
  if (role !== undefined) metadata.role = role;
  if (phase !== undefined) metadata.phase = phase;
  if (segmentId !== undefined) metadata.segmentId = segmentId;
  if (iterationIndex !== undefined) metadata.iterationIndex = iterationIndex;
  if (streamAttempt !== undefined) metadata.streamAttempt = streamAttempt;
  if (sealReason !== undefined) metadata.sealReason = sealReason;
  if (incoming.sealed !== undefined) metadata.sealed = incoming.sealed;
  if (promoteAllUnsealedNarration !== undefined) metadata.promoteAllUnsealedNarration = promoteAllUnsealedNarration;
  if (providerRaw !== undefined) metadata.providerRaw = providerRaw;
  if (finishReason !== undefined) metadata.finishReason = finishReason;
  return Object.keys(metadata).length ? metadata : undefined;
};

const isUnscopedLiveTextChunk = (ev: {
  source?: string;
  visibility?: string;
  role?: string;
  phase?: string;
  finalize?: boolean;
  metadata?: unknown;
}): boolean =>
  !ev.finalize &&
  !ev.source &&
  !ev.visibility &&
  !ev.role &&
  !ev.phase &&
  !ev.metadata;

const unsealedTextMetadata = (metadata: ReturnType<typeof textEventMetadata>): ReturnType<typeof textEventMetadata> => ({
  ...(metadata ?? {}),
  visibility: "unsealed",
  phase: metadata?.phase ?? "model",
});

export const handleChatStreamEvent = (
  e: ServerEvent,
  conversationId: string | undefined,
  handlers: ChatStreamHandlers,
): boolean => {
  try {
  const { textStreamBuffer, thinkingStreamBuffer } = handlers;
  adoptGeneratedConversation(conversationId);
  activateQueuedTurnFromFirstStreamEvent(conversationId, eventMessageId(e));
  const s = useAppStore.getState();
  s.bindStreamingTurn(conversationId, eventMessageId(e), eventTurnId(e));
  switch (e.type) {
    case "thinking_delta":
    case "thinking": {
      const ev = e as unknown as {
        content?: string;
        source?: string;
        visibility?: string;
        is_raw_provider_reasoning?: boolean;
        provider_reasoning_type?: string;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.content) {
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
          is_raw_provider_reasoning: ev.is_raw_provider_reasoning,
          provider_reasoning_type: ev.provider_reasoning_type,
        };
        if (thinkingStreamBuffer) {
          thinkingStreamBuffer.push(ev.content, conversationId, undefined, thinkingMeta, messageId);
        } else {
          useAppStore.getState().appendThinkingChunk(ev.content, conversationId, thinkingMeta, messageId);
        }
      }
      return true;
    }
    case "text_chunk": {
      const ev = e as unknown as {
        content?: string;
        source?: string;
        visibility?: string;
        role?: string;
        phase?: string;
        finalize?: boolean;
        metadata?: Parameters<typeof textEventMetadata>[0]["metadata"];
        segmentId?: string;
        segment_id?: string;
        iterationIndex?: number;
        iteration_index?: number;
        streamAttempt?: number;
        stream_attempt?: number;
        sealReason?: string;
        seal_reason?: string;
        providerRaw?: Record<string, unknown>;
        finishReason?: string;
        attachments?: Array<{ path: string; size: number; is_image: boolean }>;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.finalize) {
        // Contentless seal: the answer was streamed live token-by-token; re-tag
        // the last streamed text block as the final answer without re-emitting.
        textStreamBuffer.flush();
        s.finalizeStreamingText(conversationId || "", ev.source, textEventMetadata(ev), messageId);
      } else if (ev.content != null && !isRawProviderErrorText(ev.content)) {
        const metadata = textEventMetadata(ev);
        textStreamBuffer.push(
          ev.content,
          conversationId,
          ev.source,
          isUnscopedLiveTextChunk(ev) ? unsealedTextMetadata(metadata) : metadata,
          messageId,
        );
      }
      if (Array.isArray(ev.attachments) && ev.attachments.length > 0) {
        s.setFinalAnswerAttachments(
          conversationId,
          ev.attachments.map((a) => ({ path: a.path, size: a.size, isImage: a.is_image })),
          messageId,
        );
      }
      return true;
    }
    case "text_replace": {
      const ev = e as unknown as { content?: string; source?: string; visibility?: string; role?: string; phase?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      textStreamBuffer.flush();
      s.replaceStreamingText(conversationId || "", ev.content ?? "", ev.source, textEventMetadata(ev), messageId);
      return true;
    }
    case "image_chunk": {
      const img = e as unknown as { image_data?: string; media_type?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (img.image_data) {
        const mediaType = img.media_type || "image/png";
        textStreamBuffer.push(`\n![image](data:${mediaType};base64,${img.image_data})\n`, conversationId, undefined, undefined, messageId);
      }
      return true;
    }
    case "tool_call": {
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const scope = toolCallScopeFromEvent(e);
      const existing = findToolCall(e.id, conversationId, scope, messageId);
      if (existing) {
        s.updateToolCall(e.id, {
          args: e.args ?? {},
          status: "running",
          displayHint: e.display_hint,
          inputSummary: e.input_summary,
          resultKind: e.result_kind,
          activityKind: e.activity_kind,
          groupId: e.group_id,
          stepId: e.step_id,
          turnId: e.turn_id,
          iterationId: e.iteration_id,
          phase: e.phase,
          displayScope: e.display_scope,
          panelHint: e.panel_hint,
          requiresAttention: e.requires_attention,
        }, conversationId, scope);
      } else {
        const record = reduceToolCallStart(new Map(), e).get(e.id);
        if (record) s.appendToolCallBlock(record, conversationId, messageId);
      }
      addInspectorPayload("tool_call", e.id, {
        event: "tool_call",
        name: e.name,
        args: e.args ?? {},
        result_kind: e.result_kind,
        activity_kind: e.activity_kind,
        display_hint: e.display_hint,
        input_summary: e.input_summary,
        turn_id: e.turn_id,
        iteration_id: e.iteration_id,
        phase: e.phase,
      });
      if (isActiveConversationEvent(conversationId)) {
        maybeAutoRoutePanel(e, e.name === "task" ? "subagents" : undefined);
      }
      return true;
    }
    case "tool_output_delta": {
      const delta = e as ToolOutputDeltaEvent;
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (delta.id && delta.output) {
        const scope = toolCallScopeFromEvent(delta);
        const existing = findToolCall(delta.id, conversationId, scope, messageId);
        s.updateToolCall(delta.id, outputPreviewUpdates(existing, delta.output, delta.stream), conversationId, scope);
        addInspectorPayload("tool_call", delta.id, {
          event: "tool_output_delta",
          stream: delta.stream ?? "stdout",
          output: delta.output,
          turn_id: delta.turn_id,
          iteration_id: delta.iteration_id,
        });
      }
      return true;
    }
    case "command_output_chunk": {
      const ev = e as unknown as { content?: string; stream?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (ev.content) {
        const commandTool = latestRunningCommandTool(conversationId, messageId);
        if (commandTool) {
          s.updateToolCall(commandTool.id, outputPreviewUpdates(commandTool, ev.content, ev.stream), conversationId);
          addInspectorPayload("tool_call", commandTool.id, {
            event: "command_output_chunk",
            stream: ev.stream ?? "stdout",
            output: ev.content,
          });
        }
      }
      return true;
    }
    case "tool_result": {
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const scope = toolCallScopeFromEvent(e);
      const toolCall = findToolCall(e.id, conversationId, scope, messageId);
      if (toolCall) {
        const updated = reduceToolCallResult(new Map([[e.id, toolCall]]), e).get(e.id);
        if (updated) s.updateToolCall(e.id, updated, conversationId, scope);
        if (!(e as unknown as { is_error?: boolean }).is_error) {
          requestPreviewValidationForTool(toolCall.name);
        }
      } else {
        // The matching tool_call block is missing (its event was dropped, scope
        // conflicts, or it arrived post-hydration). Synthesize a minimal block
        // from the result so the result/exec/diff card still renders instead of
        // being silently dropped from the chat (only the inspector would see it).
        const fallbackName = (() => {
          const kind = String((e as ToolResultEvent).result_kind || "").trim();
          return kind && kind !== "generic" ? kind : "tool";
        })();
        const synthesized: ToolCallRecord = {
          id: e.id,
          name: fallbackName,
          args: {},
          status: "running",
          startedAt: Date.now(),
          resultKind: (e as ToolResultEvent).result_kind,
          activityKind: (e as ToolResultEvent).activity_kind,
          groupId: (e as ToolResultEvent).group_id,
          stepId: (e as ToolResultEvent).step_id,
          turnId: (e as ToolResultEvent).turn_id,
          iterationId: (e as ToolResultEvent).iteration_id,
        };
        const withResult = reduceToolCallResult(new Map([[e.id, synthesized]]), e).get(e.id);
        if (withResult) s.appendToolCallBlock(withResult, conversationId, messageId);
      }
      addInspectorPayload("tool_call", e.id, {
        event: "tool_result",
        status: e.status,
        is_error: e.is_error,
        summary: e.summary,
        display_summary: e.display_summary,
        artifact_id: e.artifact_id,
        diff: e.diff,
        source_url: e.source_url,
        content_preview: e.content_preview,
        error_info: e.error_info,
        developer_detail: e.developer_detail,
        projection: e.projection,
        turn_id: e.turn_id,
        iteration_id: e.iteration_id,
      });
      if (isActiveConversationEvent(conversationId)) {
        maybeAutoRoutePanel(e, e.diff ? "diff" : undefined);
      }
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
      const toolName = ev.tool_name || "tool";
      const status = decision === "allow" ? "completed" : decision === "deny" ? "failed" : "running";
      const ruleLabel = ev.matched_rule?.rule || ev.source || "policy";
      const summary = ev.message || (decision === "allow"
        ? "Allowed automatically"
        : decision === "ask"
          ? `Approval required by ${ruleLabel}`
          : `Blocked by ${ruleLabel}`);
      if (decision !== "allow") {
        s.appendProgress({
          id: `permission:${ev.tool_call_id || toolName}:${decision}`,
          stage: decision === "ask" ? "approval" : "tool",
          phase: decision === "ask" ? "approval" : "tool",
          status,
          message: summary,
          label: decision === "ask" ? "Approval required" : "Permission denied",
          summary,
          visibility: "timeline",
          toolCallId: ev.tool_call_id,
          toolName,
          requiresAttention: decision === "deny",
        }, conversationId, messageId);
      }
      if (ev.tool_call_id) {
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
        });
      }
      return true;
    }
    case "agent.loop.started":
    case "agent.loop.completed": {
      const ev = e as unknown as {
        loop_id?: string;
        iteration_id?: string;
        status?: string;
        title?: string;
        summary?: string;
        tool_call_count?: number;
      };
      const messageId = eventMessageId(e);
      const loopId = ev.loop_id || ev.iteration_id;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (loopId) {
        s.appendProgress({
          id: `loop:${loopId}`,
          stage: "status",
          phase: "iteration",
          status: progressStatusFromLoop(e.type, ev.status),
          message: ev.summary || ev.title || (e.type === "agent.loop.started" ? "Agent is working" : "Agent step completed"),
          label: ev.title || (e.type === "agent.loop.started" ? "Thinking" : "Processed"),
          summary: ev.summary || ev.title,
          visibility: "debug",
          count: ev.tool_call_count,
          iterationId: ev.iteration_id || loopId,
        }, conversationId, messageId);
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
        display_scope?: string;
        panel_hint?: string;
        requires_attention?: boolean;
        skill_name?: string;
        trigger_mode?: string;
        source_level?: string;
        reason?: string;
        token_estimate?: number;
        created_at?: number;
        order?: number;
        seq?: number;
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      const content = ev.content || ev.summary || "";
      const itemId = ev.item_id || ev.id;
      if (itemId && content.trim() && ev.visibility !== "debug") {
        textStreamBuffer.flush();
        thinkingStreamBuffer?.flush();
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
          displayScope: ev.display_scope,
          panelHint: ev.panel_hint,
          requiresAttention: ev.requires_attention,
          skillName: ev.skill_name,
          triggerMode: ev.trigger_mode,
          sourceLevel: ev.source_level,
          reason: ev.reason,
          tokenEstimate: ev.token_estimate,
          timestamp: ev.created_at,
          order: ev.order,
          seq: ev.seq,
        }, conversationId, messageId);
        // File preparation is already represented by the file-change card and
        // diff stream. Do not mirror it into the composer/task progress area.
        if (isActiveConversationEvent(conversationId)) {
          maybeAutoRoutePanel(ev);
        }
      }
      return true;
    }
    case "done": {
      const messageId = eventMessageId(e);
      if (consumeKnownStaleTurnEvent(conversationId, messageId)) return true;
      const target = resolveTerminalEventTarget(conversationId, messageId, eventTurnId(e));
      if (target.stale) return true;
      const terminalMessageId = terminalMessageIdForEvent(conversationId, target.messageId);
      textStreamBuffer.flush();
      thinkingStreamBuffer?.flush();
      const usage = usageFromDoneEvent(e);
      if (usage && (!conversationId || conversationId === useAppStore.getState().conversationId)) {
        s.setLastUsage(usage);
      }
      const providerRaw = providerRawFromDoneEvent(e);
      const providerTracePayload = providerTracePayloadFromDone(
        providerRaw,
        providerUsageFromDoneUsage(usage),
      );
      if (providerTracePayload) {
        const traceId = String(providerTracePayload.trace_id || `${terminalMessageId || messageId || conversationId || "provider"}:provider:done`);
        addInspectorPayload("provider", traceId, {
          ...providerTracePayload,
          conversationId,
          messageId: terminalMessageId || messageId,
        });
      }
      const doneStatus = (e as unknown as { status?: string }).status;
      const terminalStatus =
        doneStatus === "cancelled" ? "interrupted" :
        doneStatus === "partial" ? "partial" :
        doneStatus === "failed" || doneStatus === "interrupted" ? "failed" :
        "completed";
      s.finishAgentProgress(
        conversationId,
        terminalStatus === "failed" || terminalStatus === "interrupted" ? "failed" : "completed",
      );
      s.finishStreaming(conversationId, usage, terminalStatus, terminalMessageId);
      const replayed = Boolean((e as unknown as { __replayed?: boolean }).__replayed);
      if (!replayed && typeof document !== "undefined" && (document.hidden || !document.hasFocus())) {
        const conversation = s.conversations.find((item) => item.id === conversationId);
        void import("../desktop/runtime").then(({ desktop }) => desktop()?.notify({
          title: terminalStatus === "completed" ? "MiniCode task completed" : "MiniCode task stopped",
          body: conversation?.title || "Your response is ready.",
          ...(conversationId ? { target: { kind: "conversation" as const, conversationId } } : {}),
        }));
      }
      // Refresh the authoritative context/budget snapshot so the usage ring
      // reflects the turn that just completed (done carries token counts but
      // not the budget breakdown). silent: indicator-only, no chat notice.
      sendClientCommand({ type: "session.usage.inspect", silent: true });
      resetSendDeduplication();
      return true;
    }
    case "error": {
      const messageId = eventMessageId(e);
      if (consumeKnownStaleTurnEvent(conversationId, messageId)) return true;
      const target = resolveTerminalEventTarget(conversationId, messageId, eventTurnId(e));
      if (target.stale) return true;
      const terminalMessageId = terminalMessageIdForEvent(conversationId, target.messageId);
      textStreamBuffer.flush();
      thinkingStreamBuffer?.flush();
      const err = e as unknown as { conversation_id?: string; message_id?: string; message?: string; tool_call_id?: string; request_id?: string; error_type?: string; error_code?: string; provider_error_type?: string };
      const rawMessage = err.message ?? "An error occurred.";
      if (isStaleApprovalResponseError(rawMessage)) {
        return true;
      }
      if (isToolOnlyNoReplyFallback(rawMessage) && hasVisibleAssistantReply(conversationId, target.messageId)) {
        s.finishAgentProgress(conversationId, "completed");
        s.finishStreaming(conversationId, undefined, "completed", terminalMessageId);
        resetSendDeduplication();
        return true;
      }
      if (err.error_code === "conversation.not_found") {
        recoverMissingConversation(conversationId);
        resetSendDeduplication();
        return true;
      }
      const message = normalizeAgentErrorMessage(rawMessage);
      const transcriptMessage = normalizeAgentErrorMessage(rawMessage, { includeProviderDetails: false });
      if (isTransientCommandBacklogError(err, rawMessage)) {
        pushToast(message, "warning", 3000);
        return true;
      }
      if (
        !err.conversation_id && !err.message_id && !err.tool_call_id && !err.request_id &&
        !err.error_type && !err.error_code && useAppStore.getState().isStreaming
      ) {
        pushToast(message, "error", 6000);
        return true;
      }
      pushToast(message, "error", 6000);
      if (err.error_code === "workspace_missing") {
        clearMissingWorkspaceBinding(conversationId);
        s.finishAgentProgress(conversationId, "failed");
        s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage);
        resetSendDeduplication();
        return true;
      }
      if (err.error_code === "agent.busy") {
        s.removeEmptyStreamingAssistant(conversationId, target.messageId);
        if (target.messageId && hasStreamingAssistantForConversation(conversationId, target.messageId)) {
          s.finishAgentProgress(conversationId, "failed");
          s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage);
        } else {
          clearStreamingFlagIfNoLiveAssistant(conversationId);
        }
        resetSendDeduplication();
        return true;
      }
      const isProviderApiError = err.error_type === "api" && !err.tool_call_id && !err.request_id;
      if (err.error_type !== "blocked" && err.error_type !== "billing" && !isProviderApiError) {
        appendSystemMessage({
          id: `m-${Date.now().toString(36)}-err`,
          role: "system",
          content: `Error: ${message}`,
          artifacts: [],
          timestamp: Date.now(),
        }, conversationId);
      }
      const requestId = err.tool_call_id ?? err.request_id;
      if (requestId) {
        useAppStore.setState((state) => ({
          pendingApproval: state.pendingApproval?.requestId === requestId
            ? { ...state.pendingApproval, status: "error", error: message }
            : state.pendingApproval,
          approvalQueue: state.approvalQueue.filter((queued) => queued.requestId !== requestId),
          diffReview: state.diffReview?.requestId === requestId
            ? { ...state.diffReview, status: "error", error: message }
            : state.diffReview,
          pendingDiffReview: state.pendingDiffReview?.requestId === requestId ? null : state.pendingDiffReview,
          pendingAskUser: state.pendingAskUser?.requestId === requestId ? null : state.pendingAskUser,
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
      s.finishAgentProgress(conversationId, "failed");
      s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage);
      resetSendDeduplication();
      return true;
    }
    case "stream_resume": {
      const ev = e as unknown as { conversation_id?: string; turn_id?: string; accumulated_text?: string; tool_calls_pending?: PendingToolCallResume[] };
      const messageId = eventMessageId(e);
      const resumeConversationId = ev.conversation_id || conversationId || "";
      const accumulatedText = typeof ev.accumulated_text === "string" ? ev.accumulated_text : "";
      const pendingToolCalls = ev.tool_calls_pending ?? [];
      if (hasTerminalAssistantForConversation(resumeConversationId, messageId)) {
        return true;
      }
      if (
        accumulatedText.length === 0 &&
        pendingToolCalls.length === 0 &&
        !hasStreamingAssistantForConversation(resumeConversationId, messageId)
      ) {
        return true;
      }
      s.resumeStreaming(resumeConversationId, ev.tool_calls_pending, messageId, ev.turn_id);
      if (typeof ev.accumulated_text === "string") {
        s.replaceStreamingText(
          resumeConversationId,
          ev.accumulated_text,
          undefined,
          { visibility: "unsealed", phase: "model" },
          messageId,
        );
      }
      return true;
    }
    default:
      return false;
  }
  } catch (err) {
    console.error("[chatStreamEvents] Unhandled error processing event:", err, e);
    const convId = conversationId || useAppStore.getState().conversationId || undefined;
    useAppStore.getState().finishStreaming(convId, undefined, "failed", eventMessageId(e));
    pushToast("Stream processing error — please retry", "error", 4000);
    return false;
  }
};
