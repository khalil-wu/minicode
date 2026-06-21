import { useAppStore } from "../stores";
import type { ServerEvent, ToolOutputDeltaEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import {
  reduceToolCallResult,
  reduceToolCallStart,
} from "../lib/tool-call-reducer";
import { pushToast } from "../overlays/ToastContainer";
import type { ChatMessage, ChatSlice, PendingToolCallResume } from "../stores/types";
import { normalizeAgentErrorMessage } from "./errorMessages";
import { resetSendDeduplication } from "./sendChatMessage";
import { addInspectorPayload, maybeAutoRoutePanel } from "./displayRouting";

interface ChatStreamHandlers {
  textStreamBuffer: StreamBuffer;
  /** @deprecated thinking goes directly to appendThinkingChunk. Remove in v0.4.0 */
  thinkingStreamBuffer?: StreamBuffer;
}

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

const isRawProviderErrorText = (content: string): boolean =>
  /Claude API 调用失败|LLM API 调用失败|LLM API request failed|Concurrency limit exceeded|rate limit|too many requests|429/i.test(content);

const isStaleApprovalResponseError = (content: string): boolean =>
  /^(?:Approval|Question) request '.+' is no longer pending$/i.test(content.trim());

const isTransientCommandBacklogError = (err: { error_type?: string; error_code?: string }, message: string): boolean =>
  err.error_code === "command.backlog" ||
  (err.error_type === "rate_limit" && /too many pending commands/i.test(message));

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

type ToolCallScope = { iterationId?: string; stepId?: string };

const toolCallScopeFromEvent = (event: { iteration_id?: string; step_id?: string }): ToolCallScope | undefined => {
  const scope: ToolCallScope = {};
  if (event.iteration_id) scope.iterationId = event.iteration_id;
  if (event.step_id) scope.stepId = event.step_id;
  return scope.iterationId || scope.stepId ? scope : undefined;
};

const toolCallMatchesScope = (
  candidate: ReturnType<typeof getToolCallsFromMessage>[number],
  scope?: ToolCallScope,
): boolean => {
  if (!scope) return true;
  if (scope.iterationId && candidate.iterationId && candidate.iterationId !== scope.iterationId) return false;
  if (scope.stepId && candidate.stepId && candidate.stepId !== scope.stepId) return false;
  return true;
};

const messagesForConversation = (conversationId?: string): ChatMessage[] => {
  const state = useAppStore.getState();
  const targetId = conversationId?.trim();
  if (!targetId) return state.messages;
  if (targetId === state.conversationId) return state.messages;
  return state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
};

const hasStreamingAssistantForConversation = (conversationId?: string): boolean =>
  messagesForConversation(conversationId).some((message) =>
    message.role === "assistant" && (message.isStreaming || message.isThinkingStreaming),
  );

const findToolCall = (id: string, conversationId?: string, scope?: ToolCallScope) => {
  const state = useAppStore.getState();
  const messages = conversationId
    ? conversationId === state.conversationId
      ? state.messages
      : state.sideChats[conversationId]?.messages ?? state.conversationMessages[conversationId] ?? []
    : state.messages;
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

const latestRunningCommandTool = (conversationId?: string) => {
  const records = messagesForConversation(conversationId).flatMap(getToolCallsFromMessage);
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
  if (status === "failed" || status === "interrupted") return "failed";
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
  const usage = (e as unknown as { usage?: { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number } }).usage;
  return usage ? {
    input: usage.input_tokens ?? 0,
    output: usage.output_tokens ?? 0,
    cacheRead: usage.cache_read_input_tokens ?? 0,
    cacheWrite: usage.cache_creation_input_tokens ?? 0,
  } : undefined;
};

const textEventMetadata = (ev: {
  visibility?: string;
  role?: string;
  phase?: string;
}) => {
  const metadata: { visibility?: string; role?: string; phase?: string } = {};
  if (ev.visibility !== undefined) metadata.visibility = ev.visibility;
  if (ev.role !== undefined) metadata.role = ev.role;
  if (ev.phase !== undefined) metadata.phase = ev.phase;
  return Object.keys(metadata).length ? metadata : undefined;
};

export const handleChatStreamEvent = (
  e: ServerEvent,
  conversationId: string | undefined,
  handlers: ChatStreamHandlers,
): boolean => {
  try {
  const s = useAppStore.getState();
  const { textStreamBuffer, thinkingStreamBuffer } = handlers;
  adoptGeneratedConversation(conversationId);
  switch (e.type) {
    case "thinking_delta":
    case "thinking": {
      const ev = e as unknown as {
        content?: string;
        source?: string;
        visibility?: string;
        is_raw_provider_reasoning?: boolean;
      };
      if (ev.content) {
        textStreamBuffer.flush();
        useAppStore.getState().appendThinkingChunk(
          ev.content,
          conversationId,
          {
            source: ev.source,
            visibility: ev.visibility,
            is_raw_provider_reasoning: ev.is_raw_provider_reasoning,
          },
        );
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
        attachments?: Array<{ path: string; size: number; is_image: boolean }>;
      };
      if (ev.finalize) {
        // Contentless seal: the answer was streamed live token-by-token; re-tag
        // the last streamed text block as the final answer without re-emitting.
        textStreamBuffer.flush();
        s.finalizeStreamingText(conversationId || "", ev.source, textEventMetadata(ev));
      } else if (ev.content != null && !isRawProviderErrorText(ev.content)) {
        textStreamBuffer.push(ev.content, conversationId, ev.source, textEventMetadata(ev));
      }
      if (Array.isArray(ev.attachments) && ev.attachments.length > 0) {
        s.setFinalAnswerAttachments(
          conversationId,
          ev.attachments.map((a) => ({ path: a.path, size: a.size, isImage: a.is_image })),
        );
      }
      return true;
    }
    case "text_replace": {
      const ev = e as unknown as { content?: string; source?: string; visibility?: string; role?: string; phase?: string };
      textStreamBuffer.flush();
      s.replaceStreamingText(conversationId || "", ev.content ?? "", ev.source, textEventMetadata(ev));
      return true;
    }
    case "image_chunk": {
      const img = e as unknown as { image_data?: string; media_type?: string };
      if (img.image_data) {
        const mediaType = img.media_type || "image/png";
        textStreamBuffer.push(`\n![image](data:${mediaType};base64,${img.image_data})\n`, conversationId);
      }
      return true;
    }
    case "tool_call": {
      textStreamBuffer.flush();
      thinkingStreamBuffer?.flush();
      const scope = toolCallScopeFromEvent(e);
      const existing = findToolCall(e.id, conversationId, scope);
      if (existing) {
        s.updateToolCall(e.id, {
          args: e.args ?? {},
          status: "running",
          displayHint: e.display_hint,
          inputSummary: e.input_summary,
          resultKind: e.result_kind,
          activityKind: e.activity_kind,
          displayScope: e.display_scope,
          panelHint: e.panel_hint,
          requiresAttention: e.requires_attention,
        }, conversationId, scope);
      } else {
        const record = reduceToolCallStart(new Map(), e).get(e.id);
        if (record) s.appendToolCallBlock(record, conversationId);
      }
      addInspectorPayload("tool_call", e.id, {
        event: "tool_call",
        name: e.name,
        args: e.args ?? {},
        result_kind: e.result_kind,
        activity_kind: e.activity_kind,
        display_hint: e.display_hint,
        input_summary: e.input_summary,
        iteration_id: e.iteration_id,
        phase: e.phase,
      });
      maybeAutoRoutePanel(e, e.name === "task" ? "subagents" : undefined);
      return true;
    }
    case "tool_output_delta": {
      const delta = e as ToolOutputDeltaEvent;
      if (delta.id && delta.output) {
        const existing = findToolCall(delta.id, conversationId);
        s.updateToolCall(delta.id, outputPreviewUpdates(existing, delta.output, delta.stream), conversationId);
        addInspectorPayload("tool_call", delta.id, {
          event: "tool_output_delta",
          stream: delta.stream ?? "stdout",
          output: delta.output,
        });
      }
      return true;
    }
    case "command_output_chunk": {
      const ev = e as unknown as { content?: string; stream?: string };
      if (ev.content) {
        const commandTool = latestRunningCommandTool(conversationId);
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
      const scope = toolCallScopeFromEvent(e);
      const toolCall = findToolCall(e.id, conversationId, scope);
      if (toolCall) {
        const updated = reduceToolCallResult(new Map([[e.id, toolCall]]), e).get(e.id);
        if (updated) s.updateToolCall(e.id, updated, conversationId, scope);
        if (!(e as unknown as { is_error?: boolean }).is_error) {
          requestPreviewValidationForTool(toolCall.name);
        }
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
      });
      maybeAutoRoutePanel(e, e.diff ? "diff" : undefined);
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
      const loopId = ev.loop_id || ev.iteration_id;
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
        }, conversationId);
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
        created_at?: number;
        order?: number;
        seq?: number;
      };
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
          timestamp: ev.created_at,
          order: ev.order,
          seq: ev.seq,
        }, conversationId);
        maybeAutoRoutePanel(ev);
      }
      return true;
    }
    case "done": {
      textStreamBuffer.flush();
      thinkingStreamBuffer?.flush();
      const usage = usageFromDoneEvent(e);
      if (usage && (!conversationId || conversationId === useAppStore.getState().conversationId)) {
        s.setLastUsage(usage);
      }
      s.finishAgentProgress(conversationId, "completed");
      s.finishStreaming(conversationId, usage);
      // Refresh the authoritative context/budget snapshot so the usage ring
      // reflects the turn that just completed (done carries token counts but
      // not the budget breakdown). silent: indicator-only, no chat notice.
      sendClientCommand({ type: "session.usage.inspect", silent: true });
      resetSendDeduplication();
      return true;
    }
    case "error": {
      textStreamBuffer.flush();
      thinkingStreamBuffer?.flush();
      const err = e as unknown as { message?: string; tool_call_id?: string; request_id?: string; error_type?: string; error_code?: string; provider_error_type?: string };
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
      if (isTransientCommandBacklogError(err, rawMessage)) {
        pushToast(message, "warning", 3000);
        return true;
      }
      pushToast(message, "error", 6000);
      s.removeEmptyStreamingAssistant(conversationId);
      if (err.error_code === "workspace_missing") {
        clearMissingWorkspaceBinding(conversationId);
        s.finishAgentProgress(conversationId, "failed");
        s.finishStreaming(conversationId, undefined, "failed");
        resetSendDeduplication();
        return true;
      }
      if (err.error_code === "agent.busy") {
        s.finishStreaming(conversationId, undefined, "failed");
        useAppStore.setState((state) => ({
          isStreaming: conversationId && conversationId !== state.conversationId ? state.isStreaming : false,
          conversationStreaming: conversationId
            ? { ...state.conversationStreaming, [conversationId]: false }
            : state.conversationId
              ? { ...state.conversationStreaming, [state.conversationId]: false }
              : state.conversationStreaming,
        }));
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
      s.finishStreaming(conversationId, undefined, "failed");
      resetSendDeduplication();
      return true;
    }
    case "stream_resume": {
      const ev = e as unknown as { conversation_id?: string; accumulated_text?: string; tool_calls_pending?: PendingToolCallResume[] };
      const resumeConversationId = ev.conversation_id || conversationId || "";
      const accumulatedText = typeof ev.accumulated_text === "string" ? ev.accumulated_text : "";
      const pendingToolCalls = ev.tool_calls_pending ?? [];
      if (
        accumulatedText.length === 0 &&
        pendingToolCalls.length === 0 &&
        !hasStreamingAssistantForConversation(resumeConversationId)
      ) {
        return true;
      }
      s.resumeStreaming(resumeConversationId, ev.tool_calls_pending);
      if (typeof ev.accumulated_text === "string") s.replaceStreamingText(resumeConversationId, ev.accumulated_text);
      return true;
    }
    default:
      return false;
  }
  } catch (err) {
    console.error("[chatStreamEvents] Unhandled error processing event:", err, e);
    const convId = conversationId || useAppStore.getState().conversationId || undefined;
    useAppStore.getState().finishStreaming(convId, undefined, "failed");
    pushToast("Stream processing error — please retry", "error", 4000);
    return false;
  }
};
