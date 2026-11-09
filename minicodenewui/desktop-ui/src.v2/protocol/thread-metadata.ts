import { useCallback, useEffect, useState } from "react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";

export type SidebarSection = { id: string; name: string; appearance: { icon?: string | null; color?: string | null } | null; item_keys: string[] };
export type ThreadResourceReference = { id: string; attachment_type: string; identity_key: string; payload: unknown; created_at: number };
export const SIDEBAR_SECTIONS_EVENT = "minicode-sidebar-sections";

export async function threadMetadataRequest<T>(path: string, init?: RequestInit, sessionId = getWebSocket()?.sessionId): Promise<T> {
  if (!sessionId) throw new Error("The original session is not connected");
  const url = new URL(`/api/thread-metadata${path}`, apiBase());
  url.searchParams.set("session_id", sessionId);
  const response = await fetchWithTimeout(url, { ...init, headers: authHeaders({ "Content-Type": "application/json", ...init?.headers }) });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<T>;
}

export function useSidebarSections() {
  const connected = useAppStore(state => state.isConnected);
  const [sections, setSections] = useState<SidebarSection[]>([]);
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    let receivedProjection = false;
    const apply = (event: Event) => { receivedProjection = true; setSections((event as CustomEvent<SidebarSection[]>).detail); };
    window.addEventListener(SIDEBAR_SECTIONS_EVENT, apply);
    void threadMetadataRequest<{ sections: SidebarSection[] }>("/sections", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted && !receivedProjection) setSections(result.sections); })
      .catch(error => { if (!controller.signal.aborted) pushToast(String(error), "error"); });
    return () => { controller.abort(); window.removeEventListener(SIDEBAR_SECTIONS_EVENT, apply); };
  }, [connected]);
  const mutate = useCallback(async <T,>(path: string, body?: unknown, method = "POST") => {
    return threadMetadataRequest<T>(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  }, []);
  return { sections, mutate };
}
