import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import {
  reduceToolCallResult,
  reduceToolCallStart,
} from "../lib/tool-call-reducer";
import { pushToast } from "../overlays/ToastContainer";
import type { ChatMessage, ChatSlice } from "../stores/types";

interface ChatStreamHandlers {
  textStreamBuffer: StreamBuffer;
  thinkingStreamBuffer: StreamBuffer;
}

const isWriteLikeTool = (toolName: string): boolean =>
  /(?:write|edit|patch|replace|delete|rename|create|move|save)/i.test(toolName);

const requestPreviewValidationForTool = (toolName: string) => {
  if (!isWriteLikeTool(toolName)) return;
  const state = useAppStore.getState();
  if (!state.livePreviewUrl) return;
  window.dispatchEvent(new Event("preview:auto-refresh"));
  sendClientCommand({ type: "preview.verify", url: state.livePreviewUrl });
};

const findToolCall = (id: string, conversationId?: string) => {
  const state = useAppStore.getState();
  const messages = conversationId
    ? conversationId === state.conversationId
      ? state.messages
      : state.sideChats[conversationId]?.messages ?? state.conversationMessages[conversationId] ?? []
    : state.messages;
  for (const message of messages) {
    const toolCall = getToolCallsFromMessage(message).find((candidate) => candidate.id === id);
    if (toolCall) return toolCall;
  }
  return undefined;
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

export const handleChatStreamEvent = (
  e: ServerEvent,
  conversationId: string | undefined,
  handlers: ChatStreamHandlers,
): boolean => {
  const s = useAppStore.getState();
  const { textStreamBuffer, thinkingStreamBuffer } = handlers;
  switch (e.type) {
    case "thinking_delta":
    case "thinking": {
      const ev = e as unknown as { content?: string };
      if (ev.content) thinkingStreamBuffer.push(ev.content, conversationId);
      return true;
    }
    case "text_chunk": {
      if (e.content != null) textStreamBuffer.push(e.content, conversationId);
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
      thinkingStreamBuffer.flush();
      const existing = findToolCall(e.id, conversationId);
      if (existing) {
        s.updateToolCall(e.id, { args: e.args ?? {}, status: "running" }, conversationId);
      } else {
        const record = reduceToolCallStart(new Map(), e).get(e.id);
        if (record) s.appendToolCallBlock(record, conversationId);
      }
      return true;
    }
    case "tool_result": {
      const toolCall = findToolCall(e.id, conversationId);
      if (toolCall) {
        const updated = reduceToolCallResult(new Map([[e.id, toolCall]]), e).get(e.id);
        if (updated) s.updateToolCall(e.id, updated, conversationId);
        if (!(e as unknown as { is_error?: boolean }).is_error) {
          requestPreviewValidationForTool(toolCall.name);
        }
      }
      return true;
    }
    case "done": {
      textStreamBuffer.flush();
      thinkingStreamBuffer.flush();
      const usage = usageFromDoneEvent(e);
      if (usage) s.setLastUsage(usage);
      s.finishAgentProgress(conversationId, "completed");
      s.finishStreaming(conversationId, usage);
      return true;
    }
    case "error": {
      textStreamBuffer.flush();
      thinkingStreamBuffer.flush();
      const err = e as unknown as { message?: string; tool_call_id?: string; request_id?: string; error_type?: string; error_code?: string };
      const message = err.message ?? "An error occurred.";
      pushToast(message, "error", 6000);
      s.removeEmptyStreamingAssistant(conversationId);
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
        return true;
      }
      if (err.error_type !== "blocked" && err.error_type !== "billing") {
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
        useAppStore.setState({ isStreaming: false });
      }
      s.finishAgentProgress(conversationId, "failed");
      s.finishStreaming(conversationId, undefined, "failed");
      return true;
    }
    case "stream_resume": {
      const ev = e as unknown as { conversation_id?: string; accumulated_text?: string; tool_calls_pending?: Array<{ id: string; name: string; args: Record<string, unknown> }> };
      const resumeConversationId = ev.conversation_id || conversationId || "";
      s.resumeStreaming(resumeConversationId, ev.tool_calls_pending);
      if (ev.accumulated_text) s.replaceStreamingText(resumeConversationId, ev.accumulated_text);
      return true;
    }
    default:
      return false;
  }
};
