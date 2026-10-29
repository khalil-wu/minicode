import { useAppStore } from "../stores";
import { parseHttpUrl } from "../lib/network-target";
import type { EmbeddedBrowserState } from "../desktop/runtime";
import { browserOwnerId } from "../stores/browser-workbench";

interface BrowserRequestBase {
  id: number;
  url: string;
  conversationId: string;
}

type BrowserRequest = BrowserRequestBase & (
  | { kind: "open" }
  | { kind: "new" }
  | { kind: "focus-address" }
  | { kind: "resume"; targetId: string }
  | { kind: "present"; target: EmbeddedBrowserState }
  | { kind: "refresh"; workspaceRoot: string }
);

type BrowserRequestListener = (request: BrowserRequest) => void;

const listeners = new Set<BrowserRequestListener>();
let requestSequence = 0;
const pendingRequests = new Map<number, BrowserRequest>();

export function isPreviewableHttpUrl(value: string): boolean {
  return parseHttpUrl(value) !== null;
}

export function openWebInBrowser(url: string): boolean {
  const parsedUrl = parseHttpUrl(url);
  if (!parsedUrl) return false;
  const normalizedUrl = parsedUrl.toString();
  const conversationId = browserOwnerId(useAppStore.getState());
  if (!conversationId) return false;

  useAppStore.getState().setRightStackTab("browser");

  const request: BrowserRequest = { kind: "open", id: ++requestSequence, url: normalizedUrl, conversationId };
  pendingRequests.set(request.id, request);
  listeners.forEach((listener) => listener(request));
  return true;
}

export function openNewBrowserTab(): void {
  const state = useAppStore.getState();
  state.setRightStackTab("browser");
  const request: BrowserRequest = { kind: "new", id: ++requestSequence, url: "", conversationId: browserOwnerId(state) };
  pendingRequests.set(request.id, request);
  listeners.forEach(listener => listener(request));
}

export function focusBrowserAddressBar(): void {
  const state = useAppStore.getState();
  state.setRightStackTab("browser");
  const request: BrowserRequest = { kind: "focus-address", id: ++requestSequence, url: "", conversationId: browserOwnerId(state) };
  pendingRequests.set(request.id, request);
  listeners.forEach(listener => listener(request));
}

export function refreshWebInBrowser(url: string, conversationId: string, workspaceRoot: string): boolean {
  const parsedUrl = parseHttpUrl(url);
  if (!parsedUrl) return false;
  const request: BrowserRequest = {
    kind: "refresh", id: ++requestSequence, url: parsedUrl.toString(), conversationId, workspaceRoot,
  };
  for (const [id, pending] of pendingRequests) {
    if (pending.kind === "refresh" && pending.conversationId === conversationId && pending.workspaceRoot === workspaceRoot
      && new URL(pending.url).origin === parsedUrl.origin) pendingRequests.delete(id);
  }
  pendingRequests.set(request.id, request);
  listeners.forEach((listener) => listener(request));
  return true;
}

export function returnToBrowserPage(target: { conversationId: string; targetId: string; url: string }): void {
  if (target.conversationId !== browserOwnerId(useAppStore.getState())) return;
  useAppStore.getState().setRightStackTab("browser");
  const request: BrowserRequest = { ...target, kind: "resume", id: ++requestSequence };
  pendingRequests.set(request.id, request);
  listeners.forEach((listener) => listener(request));
}

/** Present the exact native target that an authorized browser tool navigated. */
export function presentExistingBrowserPage(target: EmbeddedBrowserState): void {
  const state = useAppStore.getState();
  if (target.conversationId !== state.conversationId || state.pendingConversationSwitchId
    || (state.rightStackTabLocked && (!state.rightPanelOpen || state.rightStackTab !== "browser"))) return;
  const request: BrowserRequest = { kind: "present", id: ++requestSequence,
    conversationId: target.conversationId, url: target.url, target };
  pendingRequests.set(request.id, request);
  state.setRightStackTab("browser", { automatic: true });
  listeners.forEach((listener) => listener(request));
}

export function discardInactiveBrowserPresentation(): void {
  const state = useAppStore.getState();
  for (const [id, request] of pendingRequests) {
    if (request.kind === "present" && (request.conversationId !== state.conversationId || state.pendingConversationSwitchId
      || !state.rightPanelOpen || state.rightStackTab !== "browser")) pendingRequests.delete(id);
  }
}

export function discardClosedBrowserPresentation(target: EmbeddedBrowserState): void {
  for (const [id, request] of pendingRequests) {
    if (request.kind === "present" && request.target.id === target.id
      && request.conversationId === target.conversationId) pendingRequests.delete(id);
  }
}

export function transferDraftBrowserRequests(fromOwner: string, toOwner: string): void {
  for (const [id, request] of pendingRequests) {
    if (request.conversationId === fromOwner) pendingRequests.set(id, { ...request, conversationId: toOwner });
  }
}

export function subscribeBrowserRequests(listener: BrowserRequestListener): () => void {
  listeners.add(listener);
  pendingRequests.forEach((request) => listener(request));
  return () => listeners.delete(listener);
}

export function acknowledgeBrowserRequest(id: number): void {
  pendingRequests.delete(id);
}

export function __resetOpenWebInBrowserForTests(): void {
  pendingRequests.clear();
  requestSequence = 0;
  listeners.clear();
}
