export const isConversationRunning = ({
  conversationId,
  activeConversationId,
  activeIsStreaming,
  conversationStreaming,
}: {
  conversationId: string;
  activeConversationId?: string | null;
  activeIsStreaming?: boolean;
  conversationStreaming: Record<string, boolean>;
}): boolean => {
  const mappedStreaming = Boolean(conversationStreaming[conversationId]);
  if (conversationId === activeConversationId) {
    return Boolean(activeIsStreaming || mappedStreaming);
  }
  return mappedStreaming;
};
