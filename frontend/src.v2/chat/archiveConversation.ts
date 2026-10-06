import { isDesktop } from "../desktop/runtime";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";

/** Archive acknowledgement follows the host's scoped resource shutdown. */
export const setConversationArchived = (conversationId: string, archived: boolean) => {
  const command = archived ? "conversation.archive" : "conversation.unarchive";
  return sendClientCommandAwaitResult({
    type: command,
    conversation_id: conversationId,
    archived,
    ...(archived && isDesktop() ? { client_resource_cleanup: true } : {}),
  }, command);
};
