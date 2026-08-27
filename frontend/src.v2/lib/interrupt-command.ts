import type { InterruptCommand } from "../protocol/conversation-types";
import type { ChatMessage } from "../stores/types";

type InterruptState = {
  conversationId?: string | null;
  messages: ChatMessage[];
  conversationMessages: Record<string, ChatMessage[]>;
  sideChats: Record<string, { messages: ChatMessage[] }>;
};

const streamingAssistantForConversation = (
  state: InterruptState,
  conversationId: string,
): ChatMessage | undefined => {
  const messages = state.sideChats[conversationId]?.messages
    ?? (conversationId === state.conversationId
      ? state.messages
      : state.conversationMessages[conversationId] ?? []);
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "assistant" && message.isStreaming) return message;
  }
  return undefined;
};

/** Build a turn-fenced interrupt, matching MiniCode's thread_id + turn_id contract. */
export const buildInterruptCommand = (
  state: InterruptState,
  requestedConversationId?: string | null,
): InterruptCommand => {
  const conversationId = (requestedConversationId ?? state.conversationId ?? "").trim();
  const message = conversationId
    ? streamingAssistantForConversation(state, conversationId)
    : undefined;
  return {
    type: "interrupt",
    ...(conversationId ? { conversation_id: conversationId } : {}),
    ...(message?.turnId ? { turn_id: message.turnId } : {}),
    ...(message?.id ? { message_id: message.id } : {}),
  };
};

export const hasInterruptFence = (command: InterruptCommand): boolean => Boolean(
  command.turn_id?.trim()
  || command.message_id?.trim()
  || command.task_id?.trim(),
);
