import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";

const PREVIEW_NAVIGATE_DEDUPE_MS = 750;
let lastPreviewNavigate: { url: string; at: number } | null = null;

function normalizePreviewUrl(value: string): string | null {
  const trimmed = value.trim();
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

    const hostname = parsed.hostname === "0.0.0.0" ? "localhost" : parsed.hostname;
    const port = parsed.port ? `:${parsed.port}` : "";
    const auth = parsed.username
      ? `${parsed.username}${parsed.password ? `:${parsed.password}` : ""}@`
      : "";
    const pathname = parsed.pathname === "/" && !parsed.search && !parsed.hash ? "" : parsed.pathname;
    return `${parsed.protocol}//${auth}${hostname}${port}${pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

export function isPreviewableHttpUrl(value: string): boolean {
  return normalizePreviewUrl(value) != null;
}

export function openWebInPreview(url: string): boolean {
  const normalizedUrl = normalizePreviewUrl(url);
  if (!normalizedUrl) return false;
  const now = Date.now();
  const isRecentDuplicate =
    lastPreviewNavigate?.url === normalizedUrl &&
    now - lastPreviewNavigate.at < PREVIEW_NAVIGATE_DEDUPE_MS;
  const state = useAppStore.getState();
  const alreadyShowing =
    state.livePreviewUrl === normalizedUrl &&
    state.rightStackTab === "preview" &&
    state.rightPanelOpen;
  if (isRecentDuplicate) {
    if (!alreadyShowing) {
      state.openLivePreview(normalizedUrl);
    }
    return true;
  }
  if (!alreadyShowing) {
    state.openLivePreview(normalizedUrl);
  }
  lastPreviewNavigate = { url: normalizedUrl, at: now };
  getWebSocket()?.send({ type: "preview.navigate", url: normalizedUrl });
  return true;
}

export function __resetOpenWebInPreviewDedupeForTests() {
  lastPreviewNavigate = null;
}
