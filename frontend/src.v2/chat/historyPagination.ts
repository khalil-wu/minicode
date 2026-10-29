import { ApiError, apiBase, authHeaders, fetchWithTimeout, errorMessageFromResponseText } from "../protocol/api";
import type { ConversationRecordPayload } from "../protocol/conversation-types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, normalizeContentBlocks } from "./transcriptHydration";
import type { ToolHistoryPage } from "../stores/types";
import { hasValidHistoricalTurnDiffMetadata } from "../protocol/server-event-validation";

export async function loadEarlierConversationMessages(
  conversationId: string,
  beforePrepend?: () => void,
): Promise<void> {
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
    if (!payload.transcript.every((message) => hasValidHistoricalTurnDiffMetadata(message, conversationId))) throw new Error("历史消息的文件差异摘要无效");
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
  const canonical = hydrateMessages(payload.transcript);
  const canonicalUsers = new Map(canonical.filter(message => message.role === "user").map(message => [message.id, message]));
  const messages = current.getVisibleMessages(conversationId).map(message => canonicalUsers.get(message.id) ?? message);
  const loadedIds = new Set(messages.map((message) => message.id));
  const earlier = canonical.filter((message) => !loadedIds.has(message.id));
  // The list owns its reading position. Capture it at the commit boundary,
  // after the request has finished and before new history changes its layout.
  beforePrepend?.();
  current.hydrateConversationMessages(conversationId, [...earlier, ...messages], {
    activate: current.conversationId === conversationId,
    isStreaming: current.conversationStreaming[conversationId],
    confirmedUserMessageIds: [...canonicalUsers.keys()],
    historyPage: { beforeMessageId: payload.transcript_page.before_message_id, hasMore: payload.transcript_page.has_more, loading: false },
  });
}

const toolHistoryRequests = new Map<string, { page: ToolHistoryPage; promise: Promise<void> }>();

export function loadEarlierToolItems(conversationId: string, messageId: string): Promise<void> {
  const state = useAppStore.getState();
  const original = state.getVisibleMessages(conversationId).find((message) => message.id === messageId);
  const page = original?.toolPage;
  if (!page?.remaining) return Promise.resolve();
  const key = JSON.stringify([conversationId, messageId]);
  const running = toolHistoryRequests.get(key);
  if (running?.page === page) return running.promise;
  // Virtual rows and detail views can request the same page concurrently.
  // Share only that exact owner/cursor; a new authoritative cursor starts fresh.
  const promise = loadToolHistoryPage(conversationId, messageId, page).finally(() => {
    if (toolHistoryRequests.get(key)?.promise === promise) toolHistoryRequests.delete(key);
  });
  toolHistoryRequests.set(key, { page, promise });
  return promise;
}

async function loadToolHistoryPage(conversationId: string, messageId: string, page: ToolHistoryPage): Promise<void> {
  const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/tools`);
  url.searchParams.set("session_id", getWebSocket()?.sessionId ?? "");
  url.searchParams.set("before", String(page.before));
  if (page.revision) url.searchParams.set("revision", page.revision);
  try {
    const response = await fetchWithTimeout(url, { headers: authHeaders() });
    if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
    const payload: { message_id: string; blocks: unknown[]; tool_page: ToolHistoryPage } = await response.json();
    const current = useAppStore.getState();
    const messages = current.getVisibleMessages(conversationId);
    const latest = messages.find((message) => message.id === messageId);
    // Text/tool deltas replace the message but retain its paging snapshot.
    // A new authoritative page owns a new cursor object and retires this fetch.
    if (latest?.toolPage !== page) return;
    if (payload.message_id !== messageId) throw new Error("工具历史与当前消息不匹配");
    const blocks = new Map((normalizeContentBlocks(payload.blocks) ?? []).map((block) => [block.transcriptIndex!, block]));
    const appended = [];
    for (const block of latest.blocks ?? []) {
      if (block.transcriptIndex === undefined) appended.push(block);
      else blocks.set(block.transcriptIndex, block);
    }
    const updated = { ...latest, blocks: [...blocks].sort(([left], [right]) => left - right).map(([, block]) => block).concat(appended), toolPage: payload.tool_page };
    current.hydrateConversationMessages(conversationId, messages.map((message) => message === latest ? updated : message), {
      activate: current.conversationId === conversationId, isStreaming: current.conversationStreaming[conversationId],
      historyPage: current.conversationHistoryPages[conversationId],
      confirmedUserMessageIds: [],
    });
  } catch (error) {
    const current = useAppStore.getState();
    if (current.getVisibleMessages(conversationId).find((message) => message.id === messageId)?.toolPage !== page) return;
    if (error instanceof ApiError && error.status === 409 && current.conversationId === conversationId) {
      sendClientCommand({ type: "conversation.switch", conversation_id: conversationId });
    } else {
      pushToast(error instanceof Error ? error.message : "无法加载更早的工具步骤", "error");
    }
  }
}
