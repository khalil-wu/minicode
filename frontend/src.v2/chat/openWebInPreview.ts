import { assessNetworkTargetUrl } from "../lib/network-target";
import { useAppStore } from "../stores";
import { openWebInBrowser } from "./openWebInBrowser";

const PREVIEW_NAVIGATE_DEDUPE_MS = 750;
let lastPreviewNavigate: { url: string; at: number } | null = null;
const pendingNetworkReviews = new Map<string, Promise<void>>();

function normalizePreviewUrl(value: string): string | null {
  const trimmed = value.trim();
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password) return null;

    const hostname = parsed.hostname === "0.0.0.0" ? "localhost" : parsed.hostname;
    const port = parsed.port ? `:${parsed.port}` : "";
    const pathname = parsed.pathname === "/" && !parsed.search && !parsed.hash ? "" : parsed.pathname;
    return `${parsed.protocol}//${hostname}${port}${pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

export function isPreviewableHttpUrl(value: string): boolean {
  return normalizePreviewUrl(value) != null;
}

function commitPreviewNavigation(normalizedUrl: string): void {
  const now = Date.now();
  const isRecentDuplicate =
    lastPreviewNavigate?.url === normalizedUrl &&
    now - lastPreviewNavigate.at < PREVIEW_NAVIGATE_DEDUPE_MS;
  if (isRecentDuplicate) return;
  lastPreviewNavigate = { url: normalizedUrl, at: now };
  openWebInBrowser(normalizedUrl);
}

export function openWebInPreview(url: string): boolean {
  const target = assessNetworkTargetUrl(url);
  const normalizedUrl = normalizePreviewUrl(target.normalizedUrl);
  if (!normalizedUrl || target.risk === "invalid") return false;
  const state = useAppStore.getState();
  if (state.permissionMode !== "bypass" && target.requiresReview) {
    if (!pendingNetworkReviews.has(normalizedUrl)) {
      const review = import("../overlays/DialogService")
        .then(({ showConfirm }) => showConfirm({
          title: target.risk === "local" ? "打开本地地址？" : "打开局域网地址？",
          message: `${target.host} 可能访问这台电脑或局域网中的服务。确认继续吗？`,
          confirmLabel: "继续打开",
        }))
        .then((confirmed) => {
          if (confirmed) commitPreviewNavigation(normalizedUrl);
        })
        .catch(() => undefined)
        .finally(() => pendingNetworkReviews.delete(normalizedUrl));
      pendingNetworkReviews.set(normalizedUrl, review);
    }
    return true;
  }
  commitPreviewNavigation(normalizedUrl);
  return true;
}

export function __resetOpenWebInPreviewDedupeForTests() {
  lastPreviewNavigate = null;
  pendingNetworkReviews.clear();
}
