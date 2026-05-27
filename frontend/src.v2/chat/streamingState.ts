import { useAppStore } from "../stores";
import type { StreamBuffer } from "../lib/stream-buffer";

export const clearStreamingState = (buffers: {
  textStreamBuffer: StreamBuffer;
  thinkingStreamBuffer: StreamBuffer;
}) => {
  buffers.textStreamBuffer.destroy();
  buffers.thinkingStreamBuffer.destroy();
  useAppStore.setState((state) => ({
    isStreaming: false,
    conversationStreaming: Object.fromEntries(
      Object.keys(state.conversationStreaming).map((id) => [id, false]),
    ),
    messages: state.messages.map((message) => (
      message.isStreaming || message.isThinkingStreaming
        ? { ...message, isStreaming: false, isThinkingStreaming: false }
        : message
    )),
    conversationMessages: Object.fromEntries(
      Object.entries(state.conversationMessages).map(([id, messages]) => [
        id,
        messages.map((message) => (
          message.isStreaming || message.isThinkingStreaming
            ? { ...message, isStreaming: false, isThinkingStreaming: false }
            : message
        )),
      ]),
    ),
  }));
};
