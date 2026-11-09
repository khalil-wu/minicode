import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import type { ConversationSummaryPayload } from "./conversation-types";
import { getWebSocket } from "../hooks/useWebSocket";
import { toConversationMeta } from "../chat/sessionEvents";
import { incomingConversationMetaIsStale } from "../chat/activeConversation";
import { useAppStore } from "../stores";

export type DirectoryQuery = { cursor?: string | null; limit?: number; archived?: boolean;
  sort_key?: "created_at" | "updated_at"; project_id?: string | null; cwd?: string | string[];
  sidebar_scope?: "recent" | "project"; sidebar_project?: string; use_state_db_only?: boolean };
export interface DirectoryPage { data: ConversationSummaryPayload[]; next_cursor: string | null;
  backwards_cursor: string | null; inventory_instance_id: string; inventory_revision: number }

export async function fetchConversationDirectory(query: DirectoryQuery, sessionId: string, signal?: AbortSignal): Promise<DirectoryPage> {
  const url = new URL(`${apiBase()}/api/conversations/list`);
  url.searchParams.set("session_id", sessionId);
  const response = await fetchWithTimeout(url, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ conversation_type: "main", ...query }), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<DirectoryPage>;
}

export function publishDirectoryPage(page: DirectoryPage, sessionId: string, scope?: { projectKey?: string }) {
  if (getWebSocket()?.sessionId !== sessionId) return false;
  const state = useAppStore.getState();
  if (state.conversationInventoryInstanceId && (state.conversationInventoryInstanceId !== page.inventory_instance_id
    || page.inventory_revision < state.conversationInventoryRevision)) return false;
  const existing = new Map(state.conversations.map(item => [item.id, item]));
  for (const payload of page.data) {
    const incoming = toConversationMeta(payload);
    const current = existing.get(incoming.id);
    if (!current || !incomingConversationMetaIsStale(incoming, current)) existing.set(incoming.id, incoming);
  }
  useAppStore.setState({ conversations: [...existing.values()], conversationInventoryInstanceId: page.inventory_instance_id,
    ...(scope ? { conversationDirectory: {
      nextCursor: scope.projectKey === undefined ? page.next_cursor : state.conversationDirectory?.nextCursor ?? null,
      projectCursors: { ...state.conversationDirectory?.projectCursors,
        ...(scope.projectKey !== undefined ? { [scope.projectKey]: page.next_cursor } : {}) },
    } } : {}) });
  for (const item of page.data) if (item.agent_mode !== undefined)
    useAppStore.getState().syncConversationAgentMode(item.agent_mode, item.id, undefined, item.revision);
  return true;
}
