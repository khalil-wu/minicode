import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { getWebSocket } from "./useWebSocket";
import { fetchConversationDirectory, publishDirectoryPage, type DirectoryQuery } from "../protocol/conversation-directory";

export function useConversationDirectory(enabled: boolean, query: DirectoryQuery, loadEntireDirectory = false) {
  const connected = useAppStore(state => state.isConnected);
  const sessionId = getWebSocket()?.sessionId || "";
  const queryKey = JSON.stringify(query);
  const [state, setState] = useState({ cursor: null as string | null, loading: false, error: "" });
  const owner = useRef<{ signal: AbortSignal; busy: boolean } | null>(null);
  const load = useCallback(async (cursor?: string | null) => {
    const captured = owner.current;
    if (!captured || captured.busy || captured.signal.aborted || !sessionId) return;
    captured.busy = true;
    setState(previous => ({ ...previous, loading: true, error: "" }));
    try {
      do {
        const page = await fetchConversationDirectory({ ...JSON.parse(queryKey), cursor, limit: 100, use_state_db_only: true }, sessionId, captured.signal);
        if (captured.signal.aborted || !publishDirectoryPage(page, sessionId)) return;
        cursor = page.next_cursor;
      } while (loadEntireDirectory && cursor);
      setState({ cursor: cursor ?? null, loading: false, error: "" });
    } catch (error) {
      if (!captured.signal.aborted) setState(previous => ({ ...previous, loading: false, error: error instanceof Error ? error.message : String(error) }));
    } finally {
      captured.busy = false;
      if (!captured.signal.aborted) setState(previous => ({ ...previous, loading: false }));
    }
  }, [queryKey, sessionId, loadEntireDirectory]);
  useEffect(() => {
    const controller = new AbortController();
    owner.current = { signal: controller.signal, busy: false };
    setState({ cursor: null, loading: enabled && connected, error: "" });
    if (enabled && connected) void load();
    return () => controller.abort();
  }, [enabled, connected, load]);
  const loadMore = useCallback(() => load(state.cursor), [load, state.cursor]);
  return { ...state, loadMore, hasMore: state.cursor !== null };
}
