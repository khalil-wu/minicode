import { useAppStore } from "../stores";
import { parseHttpUrl } from "../lib/network-target";
import type { EmbeddedBrowserState } from "../desktop/runtime";

interface BrowserRequestBase {
  id: number;
  url: string;
  conversationId: string;
}

type BrowserRequest = BrowserRequestBase & (
  | { kind: "open" }
  | { kind: "resume"; targetId: string }
  | { kind: "present"; target: EmbeddedBrowserState }
  | { kind: "refresh"; workspaceRoot: string }
);

type BrowserRequestListener = (request: BrowserRequest) => void;

const BROWSER_NAVIGATE_DEDUPE_MS = 750;
const listeners = new Set<BrowserRequestListener>();
let requestSequence = 0;
const pendingRequests = new Map<string, BrowserRequest>();
let lastNavigate: { url: string; conversationId: string; at: number } | null = null;

export function isPreviewableHttpUrl(value: string): boolean {
  return parseHttpUrl(value) !== null;
}

export function openWebInBrowser(url: string): boolean {
  const parsedUrl = parseHttpUrl(url);
  if (!parsedUrl) return false;
  const normalizedUrl = parsedUrl.toString();
  const conversationId = String(useAppStore.getState().conversationId || "").trim();
  if (!conversationId) return false;

  useAppStore.getState().setRightStackTab("browser");

  const now = Date.now();
  if (
    lastNavigate?.url === normalizedUrl
    && lastNavigate.conversationId === conversationId
    && now - lastNavigate.at < BROWSER_NAVIGATE_DEDUPE_MS
  ) {
    return true;
  }

  const request: BrowserRequest = { kind: "open", id: ++requestSequence, url: normalizedUrl, conversationId };
  pendingRequests.set("open", request);
  lastNavigate = { url: normalizedUrl, conversationId, at: now };
  listeners.forEach((listener) => listener(request));
  return true;
}

export function refreshWebInBrowser(url: string, conversationId: string, workspaceRoot: string): boolean {
  const parsedUrl = parseHttpUrl(url);
  if (!parsedUrl) return false;
  const request: BrowserRequest = {
    kind: "refresh", id: ++requestSequence, url: parsedUrl.toString(), conversationId, workspaceRoot,
  };
  pendingRequests.set(JSON.stringify([conversationId, workspaceRoot, parsedUrl.origin]), request);
  listeners.forEach((listener) => listener(request));
  return true;
}

export function returnToBrowserPage(target: { conversationId: string; targetId: string; url: string }): void {
  if (target.conversationId !== useAppStore.getState().conversationId) return;
  useAppStore.getState().setRightStackTab("browser");
  const request: BrowserRequest = { ...target, kind: "resume", id: ++requestSequence };
  pendingRequests.set("resume", request);
  listeners.forEach((listener) => listener(request));
}

/** Present the exact native target that an authorized browser tool navigated. */
export function presentExistingBrowserPage(target: EmbeddedBrowserState): void {
  const state = useAppStore.getState();
  if (target.conversationId !== state.conversationId || state.pendingConversationSwitchId
    || (state.rightStackTabLocked && (!state.rightPanelOpen || state.rightStackTab !== "browser"))) return;
  const request: BrowserRequest = { kind: "present", id: ++requestSequence,
    conversationId: target.conversationId, url: target.url, target };
  pendingRequests.set("present", request);
  state.setRightStackTab("browser", { automatic: true });
  listeners.forEach((listener) => listener(request));
}

export function discardInactiveBrowserPresentation(): void {
  const request = pendingRequests.get("present");
  if (!request) return;
  const state = useAppStore.getState();
  if (request.conversationId !== state.conversationId || state.pendingConversationSwitchId
    || !state.rightPanelOpen || state.rightStackTab !== "browser") pendingRequests.delete("present");
}

export function discardClosedBrowserPresentation(target: EmbeddedBrowserState): void {
  const request = pendingRequests.get("present");
  if (request?.kind === "present" && request.target.id === target.id
    && request.conversationId === target.conversationId) pendingRequests.delete("present");
}

export function subscribeBrowserRequests(listener: BrowserRequestListener): () => void {
  listeners.add(listener);
  pendingRequests.forEach((request) => listener(request));
  return () => listeners.delete(listener);
}

export function acknowledgeBrowserRequest(id: number): void {
  for (const [key, request] of pendingRequests) {
    if (request.id === id) {
      pendingRequests.delete(key);
      return;
    }
  }
}

export function __resetOpenWebInBrowserForTests(): void {
  pendingRequests.clear();
  lastNavigate = null;
  requestSequence = 0;
  listeners.clear();
}
