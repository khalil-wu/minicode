import { useAppStore } from "../stores";

interface BrowserOpenRequest {
  id: number;
  url: string;
}

type BrowserOpenListener = (request: BrowserOpenRequest) => void;

const BROWSER_NAVIGATE_DEDUPE_MS = 750;
const listeners = new Set<BrowserOpenListener>();
let requestSequence = 0;
let pendingRequest: BrowserOpenRequest | null = null;
let lastNavigate: { url: string; at: number } | null = null;

function normalizeBrowserUrl(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function openWebInBrowser(url: string): boolean {
  const normalizedUrl = normalizeBrowserUrl(url);
  if (!normalizedUrl) return false;

  useAppStore.getState().setRightStackTab("browser");

  const now = Date.now();
  if (lastNavigate?.url === normalizedUrl && now - lastNavigate.at < BROWSER_NAVIGATE_DEDUPE_MS) {
    return true;
  }

  const request = { id: ++requestSequence, url: normalizedUrl };
  pendingRequest = request;
  lastNavigate = { url: normalizedUrl, at: now };
  listeners.forEach((listener) => listener(request));
  return true;
}

export function subscribeBrowserOpenRequests(listener: BrowserOpenListener): () => void {
  listeners.add(listener);
  if (pendingRequest) listener(pendingRequest);
  return () => listeners.delete(listener);
}

export function acknowledgeBrowserOpenRequest(id: number): void {
  if (pendingRequest?.id === id) pendingRequest = null;
}

export function __resetOpenWebInBrowserForTests(): void {
  pendingRequest = null;
  lastNavigate = null;
  requestSequence = 0;
  listeners.clear();
}
