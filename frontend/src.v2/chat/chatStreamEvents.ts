import { useAppStore } from "../stores";
import type { ServerEvent, ToolOutputDeltaEvent, ToolResultEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { applyUserMessageQueueUpdate } from "./sessionEvents";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import {
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
  const owner = conversationId?.trim();
  if (!owner) return;
  useAppStore.setState((state) => {
    const targetId = owner;
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

const isStaleApprovalResponseError = (content: string): boolean =>
  /^(?:Approval|Question) request '.+' is no longer pending$/i.test(content.trim());

const isTransientCommandBacklogError = (err: { error_type?: string; error_code?: string }, message: string): boolean =>
  err.error_code === "command.backlog" ||
  (err.error_type === "rate_limit" && /too many pending commands/i.test(message));

// A recoverable error does not seal the turn, so its sanitized text has to
// survive until the terminal `done` arrives and decides the real status.
// Keyed by the same conversation/message identity as the typed stream items so
// concurrent turns in one conversation cannot consume each other's error.
const pendingRecoverableFailureText = new Map<string, string>();

const recoverableFailureKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__unowned__"}:${messageId?.trim() || "__latest__"}`;

const rememberRecoverableFailureText = (
  conversationId: string | undefined,
  messageId: string | undefined,
  text: string,
) => {
  const key = recoverableFailureKey(conversationId, messageId);
  if (text.trim()) pendingRecoverableFailureText.set(key, text);
};

const takeRecoverableFailureText = (
  conversationId: string | undefined,
  messageId: string | undefined,
): string | undefined => {
  const key = recoverableFailureKey(conversationId, messageId);
  const fallbackKey = recoverableFailureKey(conversationId);
  const text = pendingRecoverableFailureText.get(key)
    ?? pendingRecoverableFailureText.get(fallbackKey);
  pendingRecoverableFailureText.delete(key);
  if (fallbackKey !== key) pendingRecoverableFailureText.delete(fallbackKey);
  return text;
};

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

const CHAT_SCOPED_EVENT_TYPES = new Set<string>([
  "thinking_delta",
  "thinking",
  "text_delta",
  "agent_message.delta",
  "item.started",
  "item.completed",
  "tool_call",
  "tool_output_delta",
  "command_output_chunk",
  "tool_result",
  "permission.decision",
  "agent.item",
  "done",
  "error",
  "stream_resume",
]);

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

const findToolCall = (id: string, conversationId?: string, scope?: ToolCallScope, messageId?: string) => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return undefined;
  const allMessages = targetId === state.conversationId
      ? state.messages
      : state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  const messages = messageId ? allMessages.filter((message) => message.id === messageId) : allMessages;
  for (const message of messages) {
    const toolCall = getToolCallsFromMessage(message).find((candidate) =>
      candidate.id === id && (messageId ? true : toolCallMatchesScope(candidate, scope)),
    );
    if (toolCall) return toolCall;
  }
  return undefined;
};

const isCommandLikeTool = (record: ReturnType<typeof getToolCallsFromMessage>[number]): boolean =>
  String(record.resultKind || "").toLowerCase() === "command" ||
  record.activityKind === "commandExecution";

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
  // The backend owns the Pi-compatible output budget and emits normalized
  // chunks/results. The renderer must not invent a second character budget or
  // silently replace the authoritative head/tail semantics with a local slice.
  return `${current ?? ""}${chunk}`;
};

const staleTurnEventKeys = new Set<string>();

const turnEventKey = (conversationId?: string, messageId?: string) =>
  `${conversationId?.trim() || "__unowned__"}:${messageId?.trim() || ""}`;

const markStaleTurnEventIfMissing = (conversationId?: string, messageId?: string): boolean => {
  // An event without an owner is inspector-only. Never compare it with the
  // active conversation's stream or add it to the stale-turn fence.
  if (!conversationId?.trim()) return false;
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

const appendSystemMessage = (message: ChatMessage, conversationId?: string) => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return;
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
  const usage = (e as unknown as { usage?: { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number; cache_deleted_input_tokens?: number; prompt_cache_total_tokens?: number; prompt_cache_hit_rate?: number; reasoning_output_tokens?: number } }).usage;
  if (!usage) return undefined;
  const result: NonNullable<ChatSlice["lastUsage"]> = {
    input: usage.input_tokens ?? 0,
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
  conversationId = typeof eventOwner === "string" && eventOwner.trim()
    ? eventOwner.trim()
    : conversationId?.trim() || undefined;
  if (CHAT_SCOPED_EVENT_TYPES.has(e.type) && !conversationId) {
    addInspectorPayload("message", `unowned:${e.type}:${eventMessageId(e) || "event"}`, {
      event: e.type,
      unowned: true,
      payload: e,
    });
    return true;
  }
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
        phase?: string;
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
          phase: ev.phase,
        };
        if (thinkingStreamBuffer) {
          thinkingStreamBuffer.push(ev.content, conversationId, undefined, thinkingMeta, messageId);
        } else {
          useAppStore.getState().appendThinkingChunk(ev.content, conversationId, thinkingMeta, messageId);
        }
      }
      return true;
    }
    case "item.started": {
      const ev = e as unknown as {
        item?: { id?: string; type?: string };
      };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.item?.type === "agent_message") {
        textStreamBuffer.flush();
        s.startAgentMessage(ev.item.id || "agent-message", conversationId, messageId);
      }
      return true;
    }
    case "agent_message.delta": {
      const ev = e as unknown as { item_id?: string; delta?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      if (ev.delta) {
        textStreamBuffer.push(
          ev.delta,
          conversationId,
          ev.item_id || "agent-message",
          undefined,
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
      textStreamBuffer.flush();
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
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      // Current backends convert provider image chunks to artifact.preview.
      // Ignore a legacy raw chunk rather than corrupting the agent-message
      // lifecycle with synthetic Markdown text.
      return true;
    }
    case "tool_call": {
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      const scope = toolCallScopeFromEvent(e);
      const existing = findToolCall(e.id, conversationId, scope, messageId);
      if (existing) {
        const terminalStatuses = new Set<string>([
          "success", "completed", "failed", "error", "cancelled", "blocked", "denied", "timeout",
        ]);
        const nextStatus = terminalStatuses.has(existing.status)
          ? existing.status
          : e.status === "pending"
            ? "pending"
            : "running";
        s.updateToolCall(e.id, {
          args: e.args ?? {},
          status: nextStatus,
          displayHint: e.display_hint ?? existing.displayHint,
          inputSummary: e.input_summary ?? existing.inputSummary,
          resultKind: e.result_kind ?? existing.resultKind,
          activityKind: e.activity_kind ?? existing.activityKind,
          groupId: e.group_id ?? existing.groupId,
          stepId: e.step_id ?? existing.stepId,
          turnId: e.turn_id ?? existing.turnId,
          iterationId: e.iteration_id ?? existing.iterationId,
          phase: e.phase ?? existing.phase,
        }, conversationId, scope, messageId);
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
        s.updateToolCall(delta.id, outputPreviewUpdates(existing, delta.output, delta.stream), conversationId, scope, messageId);
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
      const ev = e as unknown as { content?: string; stream?: string; tool_call_id?: string; id?: string };
      const messageId = eventMessageId(e);
      if (markStaleTurnEventIfMissing(conversationId, messageId)) return true;
      flushLiveBuffers({ textStreamBuffer, thinkingStreamBuffer });
      if (ev.content) {
        const toolCallId = String(ev.tool_call_id || ev.id || "").trim();
        const commandTool = toolCallId
          ? findToolCall(toolCallId, conversationId, undefined, messageId)
          : latestRunningCommandTool(conversationId, messageId);
        if (commandTool) {
          s.updateToolCall(commandTool.id, outputPreviewUpdates(commandTool, ev.content, ev.stream), conversationId, undefined, messageId);
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
      const toolCall = findToolCall(e.id, conversationId, scope, messageId);
      if (toolCall) {
        const updated = reduceToolCallResult(new Map([[e.id, toolCall]]), e).get(e.id);
        if (updated) s.updateToolCall(e.id, updated, conversationId, scope, messageId);
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
        output_files: e.output_files,
        superseded_tool_call_ids: e.superseded_tool_call_ids,
        removed_file_paths: e.removed_file_paths,
        turn_id: e.turn_id,
        iteration_id: e.iteration_id,
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
      const toolName = ev.tool_name || "tool";
      const ruleLabel = ev.matched_rule?.rule || ev.source || "policy";
      const summary = ev.message || (decision === "allow"
        ? "Allowed automatically"
        : decision === "ask"
          ? `Approval required by ${ruleLabel}`
          : `Blocked by ${ruleLabel}`);
      if (ev.tool_call_id) {
        const existing = findToolCall(ev.tool_call_id, conversationId, undefined, messageId);
        if (existing) {
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
        doneStatus === "cancelled" || doneStatus === "interrupted" ? "interrupted" :
        doneStatus === "partial" ? "partial" :
        doneStatus === "failed" ? "failed" :
        "completed";
      // Carry the sanitized text of any recoverable error the loop reported
      // earlier in this turn; it only becomes user-visible if the turn in fact
      // ended as failed.
      const recoverableFailureText = takeRecoverableFailureText(conversationId, terminalMessageId || messageId);
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
        recoverableFailureText,
        Number.isFinite(durationMs) ? durationMs : undefined,
      );
      // approval.cancelled is authoritative, but DONE is the terminal fence
      // for the turn. Clear prompts owned by this conversation as a fallback
      // for cancellation races, reconnect gaps, or out-of-order delivery.
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
        .map((prompt) => prompt!.requestId);
      if (askUserIds.length > 0) {
        latest.clearAskUsers(askUserIds);
      }
      useAppStore.setState((state) => ({
        diffReview: promptTargetsConversation(state.diffReview)
          ? null
          : state.diffReview,
      }));
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
      const err = e as unknown as { conversation_id?: string; message_id?: string; message?: string; tool_call_id?: string; request_id?: string; error_type?: string; error_code?: string; provider_error_type?: string; recoverable?: boolean };
      const rawMessage = err.message ?? "An error occurred.";
      if (isStaleApprovalResponseError(rawMessage)) {
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
      if (err.recoverable !== true && err.error_type !== "blocked" && err.error_type !== "billing" && !isProviderApiError) {
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
      s.finishStreaming(conversationId, undefined, "failed", terminalMessageId, transcriptMessage);
      resetSendDeduplication();
      return true;
    }
    case "stream_resume": {
      flushLiveBuffers(handlers);
      const ev = e as unknown as {
        conversation_id?: string;
        turn_id?: string;
        phase?: string;
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
      if (hasTerminalAssistantForConversation(resumeConversationId, messageId)) {
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
      return true;
    }
    default:
      return false;
  }
  } catch (err) {
    console.error("[chatStreamEvents] Unhandled error processing event:", err, e);
    if (conversationId) {
      useAppStore.getState().finishStreaming(conversationId, undefined, "failed", eventMessageId(e));
    }
    pushToast("Stream processing error — please retry", "error", 4000);
    return false;
  }
};
