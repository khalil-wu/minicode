import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { getWebSocket } from "./useWebSocket";
import { searchConversations, type ConversationSearchPage, type ConversationSearchResult, type ConversationSearchSort } from "../protocol/conversation-search";

type SearchState = { key: string; results: ConversationSearchResult[]; cursor: string | null; loading: boolean; error: string };

export function useConversationSearch({ query, enabled, archived = false, project = null, sort = "updated" }: {
  query: string; enabled: boolean; archived?: boolean; project?: string | null; sort?: ConversationSearchSort;
}) {
  const { t: tr } = useI18n();
  const offlineError = tr("后端连接尚未就绪，恢复连接后将重新读取。");
  const isConnected = useAppStore(state => state.isConnected);
  const sessionId = getWebSocket()?.sessionId || "";
  const search = query.trim();
  const active = enabled && search.length > 0;
  const key = JSON.stringify([sessionId, isConnected, search, active, archived, project, sort]);
  const [state, setState] = useState<SearchState>({ key: "", results: [], cursor: null, loading: false, error: "" });
  const request = useRef<{ key: string; controller: AbortController; loading: boolean } | null>(null);
  const fetchPage = useCallback(async (cursor: string, append: boolean) => {
    const owner = request.current;
    if (!owner || owner.key !== key || owner.loading || owner.controller.signal.aborted) return;
    owner.loading = true;
    setState(current => ({ ...current, loading: true, error: "" }));
    try {
      const results: ConversationSearchResult[] = [];
      let nextCursor: string | null = cursor;
      do {
        const page: ConversationSearchPage = await searchConversations({ sessionId, query: search, archived, project, sort, cursor: nextCursor,
          limit: 50 - results.length, signal: owner.controller.signal });
        if (owner.controller.signal.aborted || getWebSocket()?.sessionId !== sessionId) return;
        results.push(...page.data);
        nextCursor = page.next_cursor;
      } while (results.length < 50 && nextCursor !== null);
      setState(current => ({ key, results: append ? [...new Map([...current.results, ...results].map(result => [result.conversation.id, result])).values()] : results,
        cursor: nextCursor, loading: false, error: "" }));
    } catch (reason) {
      if (!owner.controller.signal.aborted && getWebSocket()?.sessionId === sessionId) {
        setState(current => ({ ...current, loading: false, error: reason instanceof Error ? reason.message : String(reason) }));
      }
    } finally { owner.loading = false; }
  }, [key, sessionId, search, archived, project, sort]);

  useEffect(() => {
    const owner = { key, controller: new AbortController(), loading: false };
    request.current = owner;
    setState({ key, results: [], cursor: null, loading: active && Boolean(sessionId),
      error: active && !sessionId ? offlineError : "" });
    const timeout = active && sessionId ? window.setTimeout(() => { void fetchPage("", false); }, 200) : undefined;
    return () => { window.clearTimeout(timeout); owner.controller.abort(); };
  }, [key, active, sessionId, fetchPage, offlineError]);

  const current = state.key === key ? state : { key, results: [], cursor: null, loading: active && Boolean(sessionId), error: "" };
  const loadMore = useCallback(() => {
    if (state.key === key && state.cursor && !state.loading && !state.error) void fetchPage(state.cursor, true);
  }, [key, state.key, state.cursor, state.loading, state.error, fetchPage]);
  return { results: current.results, loading: current.loading, error: current.error, hasMore: Boolean(current.cursor), loadMore, sessionId };
}
