import type { ConversationMeta } from "../stores/types";

export const isVisibleConversationMeta = (conversation: ConversationMeta | undefined | null): boolean =>
  Boolean(conversation && conversation.conversationType !== "side_chat" && !conversation.archived);

export const normalizedConversationRevision = (value: unknown): number | undefined =>
  Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : undefined;

export const incomingConversationMetaIsStale = (
  incoming: Pick<ConversationMeta, "revision" | "updatedAt">,
  existing: Pick<ConversationMeta, "revision" | "updatedAt">,
): boolean => {
  const incomingRevision = normalizedConversationRevision(incoming.revision);
  const existingRevision = normalizedConversationRevision(existing.revision);
  if (incomingRevision !== undefined && existingRevision !== undefined) return incomingRevision < existingRevision;
  if (existingRevision !== undefined && incomingRevision === undefined) return true;
  const incomingUpdatedAt = Date.parse(String(incoming.updatedAt || ""));
  const existingUpdatedAt = Date.parse(String(existing.updatedAt || ""));
  return Number.isFinite(incomingUpdatedAt) && Number.isFinite(existingUpdatedAt) && incomingUpdatedAt < existingUpdatedAt;
};

export const activeVisibleConversation = (
  conversationId: string | null | undefined,
  conversations: ConversationMeta[],
): ConversationMeta | null => {
  if (!conversationId) return null;
  const conversation = conversations.find((item) => item.id === conversationId);
  return conversation && isVisibleConversationMeta(conversation)
    ? conversation
    : null;
};

export const hasVisibleActiveConversation = (
  conversationId: string | null | undefined,
  conversations: ConversationMeta[],
): boolean => activeVisibleConversation(conversationId, conversations) !== null;
