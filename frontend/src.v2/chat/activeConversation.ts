import type { ConversationMeta } from "../stores/types";

export const activeVisibleConversation = (
  conversationId: string | null | undefined,
  conversations: ConversationMeta[],
): ConversationMeta | null => {
  if (!conversationId) return null;
  const conversation = conversations.find((item) => item.id === conversationId);
  return conversation && !conversation.archived ? conversation : null;
};

export const hasVisibleActiveConversation = (
  conversationId: string | null | undefined,
  conversations: ConversationMeta[],
): boolean => activeVisibleConversation(conversationId, conversations) !== null;
