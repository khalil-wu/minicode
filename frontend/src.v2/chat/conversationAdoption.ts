import { useAppStore } from "../stores";

/** Bind the optimistic first turn to the conversation created by the backend. */
export const adoptGeneratedConversation = (conversationId?: string, messageId?: string) => {
  const targetId = conversationId?.trim();
  const intentId = messageId?.trim();
  if (!targetId || !intentId) return;
  useAppStore.setState((state) => {
    if (state.conversationId || state.sideChats[targetId]) return state;
    // A nil foreground owner is not authority to adopt an old owner's event.
    // The backend echoes the first send's optimistic assistant id. A restore
    // may have cleared its streaming flag before the admitted run starts.
    const intent = state.messages.find((message) => message.role === "assistant" && message.id === intentId);
    if (!intent || intent.terminalStatus || intent.completedAt != null || intent.queueState === "queued") return state;
    const messages = state.messages;
    const conversations = state.conversations.some((conversation) => conversation.id === targetId)
      ? state.conversations
      : [{ id: targetId, title: "新会话", updatedAt: new Date().toISOString() }, ...state.conversations];
    return {
      conversationId: targetId,
      conversations,
      conversationMessages: { ...state.conversationMessages, [targetId]: messages },
      conversationStreaming: { ...state.conversationStreaming, [targetId]: true },
      isStreaming: true,
    };
  });
};
