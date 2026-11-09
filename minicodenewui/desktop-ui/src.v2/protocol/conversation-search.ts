import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import type { ConversationSummaryPayload } from "./conversation-types";
import type { ConversationMeta } from "../stores/types";

export type ConversationSearchSummary = Pick<ConversationSummaryPayload,
  "id" | "title" | "created_at" | "updated_at" | "conversation_type" | "archived"
  | "project_id" | "workspace_root" | "execution_cwd" | "worktree_path" | "git_isolated">;

export interface ConversationSearchResult {
  conversation: ConversationSearchSummary;
  message_id: string;
  snippet: string;
}

export interface ConversationSearchPage {
  data: ConversationSearchResult[];
  next_cursor: string | null;
}

export type ConversationSearchSort = "updated" | "created" | "alphabetical";

export async function searchConversations({ sessionId, query, archived, project, sort, cursor, signal, limit = 50 }: {
  sessionId: string; query: string; archived: boolean; project: string | null;
  sort: ConversationSearchSort; cursor: string; signal: AbortSignal; limit?: number;
}): Promise<ConversationSearchPage> {
  const url = new URL(`${apiBase()}/api/conversations/search`);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("query", query);
  url.searchParams.set("archived", String(archived));
  url.searchParams.set("sort", sort);
  url.searchParams.set("limit", String(limit));
  if (project !== null) url.searchParams.set("project", project);
  if (cursor) url.searchParams.set("cursor", cursor);
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<ConversationSearchPage>;
}

export function searchResultConversation(result: ConversationSearchResult): ConversationMeta {
  const c = result.conversation;
  return { id: c.id, title: c.title || "", updatedAt: c.updated_at || "", createdAt: c.created_at || undefined,
    archived: c.archived, conversationType: c.conversation_type, projectId: c.project_id,
    workspaceRoot: c.workspace_root || undefined, executionCwd: c.execution_cwd || undefined,
    worktreePath: c.worktree_path || undefined, gitIsolated: c.git_isolated };
}
