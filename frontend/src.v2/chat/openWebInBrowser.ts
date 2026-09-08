import { useAppStore } from "../stores";

interface BrowserRequestBase {
  id: number;
  url: string;
  conversationId: string;
}

type BrowserRequest = BrowserRequestBase & (
  | { kind: "open" }
  | { kind: "refresh"; workspaceRoot: string }
);

type BrowserRequestListener = (request: BrowserRequest) => void;

const BROWSER_NAVIGATE_DEDUPE_MS = 750;
const listeners = new Set<BrowserRequestListener>();
let requestSequence = 0;
const pendingRequests = new Map<string, BrowserRequest>();
let lastNavigate: { url: string; conversationId: string; at: number } | null = null;

function parseBrowserUrl(value: string): URL | null {
  try {
    const parsed = new URL(value.trim());
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed;
  } catch {
    return null;
  }
}

export function openWebInBrowser(url: string): boolean {
  const parsedUrl = parseBrowserUrl(url);
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
  const parsedUrl = parseBrowserUrl(url);
  if (!parsedUrl) return false;
  const request: BrowserRequest = {
    kind: "refresh", id: ++requestSequence, url: parsedUrl.toString(), conversationId, workspaceRoot,
  };
  pendingRequests.set(JSON.stringify([conversationId, workspaceRoot, parsedUrl.origin]), request);
  listeners.forEach((listener) => listener(request));
  return true;
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
