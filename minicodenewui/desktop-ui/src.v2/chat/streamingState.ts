import { useAppStore } from "../stores";
import type { StreamBuffer } from "../lib/stream-buffer";

type ClearStreamingStateOptions = {
  conversationId?: string | null;
  clearAllConversations?: boolean;
  /**
   * Terminal status stamped on the sealed turn. Callers know why the turn can
   * no longer progress; the store must never invent "completed" for a turn that
   * simply stopped receiving events.
   */
  terminalStatus?: "completed" | "partial" | "failed" | "interrupted";
  failureMessage?: string;
  failureRecoverable?: boolean;
};

type StoreState = ReturnType<typeof useAppStore.getState>;

const hasLiveAssistant = (messages: StoreState["messages"]): boolean =>
  messages.some((message) => message.isStreaming || message.isThinkingStreaming);

const isLiveTarget = (state: StoreState, conversationId: string | undefined): boolean => {
  if (!conversationId || conversationId === state.conversationId) {
    if (state.isStreaming || hasLiveAssistant(state.messages)) return true;
    const activeId = conversationId || state.conversationId;
    if (!activeId) return false;
    return Boolean(state.conversationStreaming[activeId])
      || hasLiveAssistant(state.conversationMessages[activeId] ?? []);
  }
  const sideChat = state.sideChats[conversationId];
  if (sideChat) return sideChat.isStreaming || hasLiveAssistant(sideChat.messages);
  return Boolean(state.conversationStreaming[conversationId])
    || hasLiveAssistant(state.conversationMessages[conversationId] ?? []);
};

/**
 * Every conversation that still believes it is streaming. `finishStreaming`
 * seals one conversation at a time, so the fan-out is resolved here. Targets
 * with nothing live are skipped: sealing is not idempotent (it also blocks
 * in-progress todos), so it must only run where a turn is actually open.
 */
const streamingTargets = (
  state: StoreState,
  options: ClearStreamingStateOptions,
): (string | undefined)[] => {
  if (!options.clearAllConversations) {
    const target = options.conversationId || undefined;
    return isLiveTarget(state, target) ? [target] : [];
  }
  const ids = new Set<string | undefined>();
  if (isLiveTarget(state, undefined)) ids.add(state.conversationId || undefined);
  for (const [id, streaming] of Object.entries(state.conversationStreaming)) {
    if (streaming) ids.add(id);
  }
  for (const [id, messages] of Object.entries(state.conversationMessages)) {
    if (hasLiveAssistant(messages)) ids.add(id);
  }
  for (const [id, thread] of Object.entries(state.sideChats)) {
    if (thread.isStreaming || hasLiveAssistant(thread.messages)) ids.add(id);
  }
  return Array.from(ids);
};

/**
 * Seal locally-live streaming state when no further terminal event can arrive.
 *
 * This delegates to `finishStreaming` rather than flipping the two
 * message-level flags itself: only `finishStreaming` also terminalizes
 * `blocks[].isStreaming`, running/pending tool records, running progress and
 * process blocks, and records `terminalStatus`/`completedAt`. Clearing just the
 * message flags left an `ExecCell` spinning with a live Stop button on a turn
 * the transcript already rendered as finished.
 */
export const clearStreamingState = (buffers: {
  textStreamBuffer: StreamBuffer;
  thinkingStreamBuffer: StreamBuffer;
}, options: ClearStreamingStateOptions = {}) => {
  buffers.textStreamBuffer.destroy();
  buffers.thinkingStreamBuffer.destroy();
  const state = useAppStore.getState();
  const terminalStatus = options.terminalStatus ?? "partial";
  for (const conversationId of streamingTargets(state, options)) {
    state.finishStreaming(
      conversationId,
      undefined,
      terminalStatus,
      undefined,
      options.failureMessage,
      options.failureRecoverable,
    );
  }
};
