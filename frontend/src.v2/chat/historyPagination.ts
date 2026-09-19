import { ApiError, apiBase, authHeaders, fetchWithTimeout, errorMessageFromResponseText } from "../protocol/api";
import type { ConversationRecordPayload } from "../protocol/conversation-types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, normalizeContentBlocks } from "./transcriptHydration";
import type { ToolHistoryPage } from "../stores/types";

export async function loadEarlierConversationMessages(conversationId: string): Promise<void> {
  const state = useAppStore.getState();
  const page = state.conversationHistoryPages[conversationId];
  if (!page?.hasMore || page.loading) return;
  const pending = { ...page, loading: true };
  useAppStore.setState({ conversationHistoryPages: { ...state.conversationHistoryPages, [conversationId]: pending } });
  const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(conversationId)}/messages`);
  url.searchParams.set("session_id", getWebSocket()?.sessionId ?? "");
  url.searchParams.set("before_message_id", page.beforeMessageId);
  let payload: Required<Pick<ConversationRecordPayload, "transcript" | "transcript_page">>;
  try {
    const response = await fetchWithTimeout(url, { headers: authHeaders() });
    if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
    payload = await response.json();
  } catch (error) {
    const current = useAppStore.getState();
    if (current.conversationHistoryPages[conversationId] !== pending) return;
    useAppStore.setState({ conversationHistoryPages: { ...current.conversationHistoryPages, [conversationId]: { ...page, loading: false } } });
    if (error instanceof ApiError && error.status === 409 && current.conversationId === conversationId) {
      sendClientCommand({ type: "conversation.switch", conversation_id: conversationId });
    } else {
      pushToast(error instanceof Error ? error.message : "无法加载更早的消息", "error");
    }
    return;
  }
  const current = useAppStore.getState();
  // A fresh snapshot or cache eviction owns a new cursor; an older fetch must
  // not prepend history from before that boundary.
  if (current.conversationHistoryPages[conversationId] !== pending) return;
  const messages = current.getVisibleMessages(conversationId);
  const loadedIds = new Set(messages.map((message) => message.id));
  const earlier = hydrateMessages(payload.transcript).filter((message) => !loadedIds.has(message.id));
  current.hydrateConversationMessages(conversationId, [...earlier, ...messages], {
    activate: current.conversationId === conversationId,
    isStreaming: current.conversationStreaming[conversationId],
    historyPage: { beforeMessageId: payload.transcript_page.before_message_id, hasMore: payload.transcript_page.has_more, loading: false },
  });
}

export async function loadEarlierToolItems(conversationId: string, messageId: string): Promise<void> {
  const state = useAppStore.getState();
  const original = state.getVisibleMessages(conversationId).find((message) => message.id === messageId);
  if (!original?.toolPage?.remaining) return;
  const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/tools`);
  url.searchParams.set("session_id", getWebSocket()?.sessionId ?? "");
  url.searchParams.set("before", String(original.toolPage.before));
  if (original.toolPage.revision) url.searchParams.set("revision", original.toolPage.revision);
  try {
    const response = await fetchWithTimeout(url, { headers: authHeaders() });
    if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
    const payload: { message_id: string; blocks: unknown[]; tool_page: ToolHistoryPage } = await response.json();
    const current = useAppStore.getState();
    const messages = current.getVisibleMessages(conversationId);
    if (messages.find((message) => message.id === messageId) !== original) return;
    if (payload.message_id !== messageId) throw new Error("工具历史与当前消息不匹配");
    const blocks = new Map((normalizeContentBlocks(payload.blocks) ?? []).map((block) => [block.transcriptIndex!, block]));
    for (const block of original.blocks ?? []) blocks.set(block.transcriptIndex!, block);
    const updated = { ...original, blocks: [...blocks].sort(([left], [right]) => left - right).map(([, block]) => block), toolPage: payload.tool_page };
    current.hydrateConversationMessages(conversationId, messages.map((message) => message === original ? updated : message), {
      activate: current.conversationId === conversationId, isStreaming: current.conversationStreaming[conversationId],
      historyPage: current.conversationHistoryPages[conversationId],
    });
  } catch (error) {
    const current = useAppStore.getState();
    if (current.getVisibleMessages(conversationId).find((message) => message.id === messageId) !== original) return;
    if (error instanceof ApiError && error.status === 409 && current.conversationId === conversationId) {
      sendClientCommand({ type: "conversation.switch", conversation_id: conversationId });
    } else {
      pushToast(error instanceof Error ? error.message : "无法加载更早的工具步骤", "error");
    }
  }
}
