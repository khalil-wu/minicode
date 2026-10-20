import type { AppStore } from "../stores/types";
import { buildInterruptCommand } from "./interrupt-command";

type SteerState = Pick<AppStore, "conversationId" | "messages" | "conversationMessages" | "conversationStreaming" | "sideChats" | "isStreaming">;

/** Capture at the user action, before asynchronous context/permission work.
 * undefined means this was a normal send; null means the live turn has not
 * supplied its real identity yet. Neither may be retargeted to a later turn. */
export const captureSteerTurnId = (state: SteerState, requestedConversationId?: string): string | null | undefined => {
  const conversationId = requestedConversationId ?? state.conversationId ?? "";
  const isStreaming = state.sideChats[conversationId]?.isStreaming
    ?? (conversationId === (state.conversationId ?? "") ? state.isStreaming : state.conversationStreaming[conversationId]);
  if (!isStreaming) return undefined;
  return buildInterruptCommand(state, conversationId).turn_id || null;
};
