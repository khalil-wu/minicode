import { ApiError, apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";
import type { TurnDiffState } from "../stores/types";
import { isHistoricalTurnDiffPayload } from "../protocol/server-event-validation";

export interface DeferredTurnDiffOwner {
  conversationId: string;
  messageId: string;
  turnId: string;
  revision?: number;
}

/** Load the retained patch only when its owning historical message is opened. */
export async function loadMessageTurnDiff(owner: DeferredTurnDiffOwner, signal?: AbortSignal): Promise<TurnDiffState | undefined> {
  const state = useAppStore.getState();
  const originalMessage = state.getVisibleMessages(owner.conversationId).find((message) => message.id === owner.messageId);
  const original = originalMessage?.turnDiff;
  if (state.conversationId !== owner.conversationId || originalMessage?.role !== "assistant"
    || originalMessage.turnId !== owner.turnId || !original?.deferred || original.messageId !== owner.messageId
    || original.threadId !== owner.conversationId || original.turnId !== owner.turnId || original.revision !== owner.revision) return;
  const controller = new AbortController();
  const abort = () => controller.abort();
  const ownsRequest = () => {
    const current = useAppStore.getState();
    const message = current.getVisibleMessages(owner.conversationId).find((entry) => entry.id === owner.messageId);
    const live = current.turnDiffs[owner.conversationId];
    const newerLiveRevision = live?.turnId === owner.turnId && (!live.messageId || live.messageId === owner.messageId)
      && owner.revision !== undefined && live.revision !== undefined && live.revision > owner.revision;
    return current.conversationId === owner.conversationId && message?.role === "assistant"
      && message.turnId === owner.turnId && message.turnDiff === original && !newerLiveRevision;
  };
  const unsubscribe = useAppStore.subscribe(() => { if (!ownsRequest()) abort(); });
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(owner.conversationId)}/messages/${encodeURIComponent(owner.messageId)}/turn-diff`);
  url.searchParams.set("session_id", getWebSocket()?.sessionId ?? "");
  url.searchParams.set("turn_id", owner.turnId);
  if (owner.revision !== undefined) url.searchParams.set("revision", String(owner.revision));
  try {
    if (!ownsRequest()) return;
    const response = await fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal });
    if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
    const payload: { conversation_id: string; message_id: string; thread_id: string; turn_id: string;
      revision?: number; diff: string | null; source?: string; truncated?: boolean } = await response.json();
    if (controller.signal.aborted || !ownsRequest()) return;
    if (!isHistoricalTurnDiffPayload(payload) || payload.conversation_id !== owner.conversationId || payload.thread_id !== owner.conversationId
      || payload.message_id !== owner.messageId || payload.turn_id !== owner.turnId || payload.revision !== owner.revision) throw new Error("历史差异与当前轮次或版本不匹配");
    const loaded: TurnDiffState = { ...original, diff: payload.diff, deferred: false,
      source: payload.source ?? original.source, truncated: payload.truncated ?? original.truncated };
    const current = useAppStore.getState();
    const messages = current.getVisibleMessages(owner.conversationId).map((message) => message.id === owner.messageId
      ? { ...message, turnDiff: loaded } : message);
    unsubscribe();
    current.hydrateConversationMessages(owner.conversationId, messages, {
      activate: true, isStreaming: current.isStreaming, historyPage: current.conversationHistoryPages[owner.conversationId],
    });
    return loaded;
  } catch (error) {
    if (controller.signal.aborted) return;
    throw error;
  } finally {
    unsubscribe();
    signal?.removeEventListener("abort", abort);
  }
}
