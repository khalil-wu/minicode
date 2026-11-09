import { useAppStore } from "../stores";
import type { ThreadReadState } from "../protocol/conversation-types";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";

// An explicit unread mark or a conflicting acknowledgement requires a new
// reader action before automatic acknowledgement can resume in this window.
export const automaticReadHolds = new Set<string>();

export function applyThreadReadState(conversationId: string, readState: ThreadReadState, expectedRevision?: string, metadataRevision?: number) {
  useAppStore.setState(state => {
    let changed = false;
    const conversations = state.conversations.map(conversation => {
      if (conversation.id !== conversationId || expectedRevision !== undefined
          && conversation.readState?.revision !== expectedRevision
          || metadataRevision !== undefined && conversation.revision !== undefined && metadataRevision < conversation.revision) return conversation;
      if (conversation.readState?.revision === readState.revision) return conversation;
      if (readState.firstUnread?.type === "threadStart") automaticReadHolds.add(conversationId);
      changed = true;
      return { ...conversation, readState, hasUnreadTurn: readState.firstUnread !== null,
        ...(metadataRevision !== undefined ? { revision: metadataRevision } : {}) };
    });
    return changed ? { conversations } : state;
  });
}

export async function setThreadReadState(conversationId: string, unread: boolean, expectedRevision: string) {
  if (unread) automaticReadHolds.add(conversationId);
  else automaticReadHolds.delete(conversationId);
  const result = await sendClientCommandAwaitResult({ type: "conversation.read_state.set", conversation_id: conversationId,
    has_unread_turn: unread, expected_revision: expectedRevision }, "conversation.read_state.set");
  const data = result.data as { reason?: string; readState?: ThreadReadState; revision?: number } | undefined;
  if (data?.reason === "readStateConflict") automaticReadHolds.add(conversationId);
  if (data?.readState) applyThreadReadState(conversationId, data.readState, expectedRevision, data.revision);
  return result;
}
