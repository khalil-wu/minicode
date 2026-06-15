import { useAppStore } from "../stores";
import type { StreamBuffer } from "../lib/stream-buffer";

type StreamingMessage = {
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
};

type ClearStreamingStateOptions = {
  conversationId?: string | null;
  clearAllConversations?: boolean;
};

const clearMessageStreaming = <T extends StreamingMessage>(message: T): T => (
  message.isStreaming || message.isThinkingStreaming
    ? { ...message, isStreaming: false, isThinkingStreaming: false }
    : message
);

const clearMessageListStreaming = <T extends StreamingMessage>(messages: T[]): T[] =>
  messages.map(clearMessageStreaming);

export const clearStreamingState = (buffers: {
  textStreamBuffer: StreamBuffer;
  thinkingStreamBuffer: StreamBuffer;
}, options: ClearStreamingStateOptions = {}) => {
  buffers.textStreamBuffer.destroy();
  buffers.thinkingStreamBuffer.destroy();
  useAppStore.setState((state) => ({
    ...clearStreamingStateForStore(state, options),
  }));
};

const clearStreamingStateForStore = (
  state: ReturnType<typeof useAppStore.getState>,
  options: ClearStreamingStateOptions,
) => {
  if (options.clearAllConversations) {
    return {
      isStreaming: false,
      conversationStreaming: Object.fromEntries(
        Object.keys(state.conversationStreaming).map((id) => [id, false]),
      ),
      messages: clearMessageListStreaming(state.messages),
      conversationMessages: Object.fromEntries(
        Object.entries(state.conversationMessages).map(([id, messages]) => [
          id,
          clearMessageListStreaming(messages),
        ]),
      ),
    };
  }

  const targetConversationId = options.conversationId ?? state.conversationId;
  const isActiveTarget = !targetConversationId || targetConversationId === state.conversationId;
  const cachedTargetMessages = targetConversationId
    ? state.conversationMessages[targetConversationId] ?? []
    : [];
  const targetMessages = isActiveTarget
    ? state.messages
    : cachedTargetMessages;

  return {
    ...(isActiveTarget
      ? {
          isStreaming: false,
          messages: clearMessageListStreaming(state.messages),
        }
      : {}),
    ...(targetConversationId
      ? {
          conversationStreaming: {
            ...state.conversationStreaming,
            [targetConversationId]: false,
          },
          conversationMessages: {
            ...state.conversationMessages,
            [targetConversationId]: clearMessageListStreaming(targetMessages),
          },
        }
      : {}),
  };
};
