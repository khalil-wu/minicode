import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { getWebSocket } from "../hooks/useWebSocket";
import { ApiError, apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import type { WorkspaceFilePreviewResponse } from "./workspace";

export type ConversationBrowserOwner = { conversationId: string; executionCwd: string };
export interface ConversationBrowserEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number | null;
  modified_at: string;
  has_children: boolean;
}
export interface ConversationBrowserTree {
  workspace_root: string;
  requested_path: string;
  entries: ConversationBrowserEntry[];
}

function browserUrl(owner: ConversationBrowserOwner, method: string, path: string) {
  const sessionId = getWebSocket()?.sessionId;
  if (!sessionId) throw new Error("The file browser needs a connected session.");
  const url = new URL(`${apiBase()}/api/workspace/${method}`);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("conversation_id", owner.conversationId);
  url.searchParams.set("execution_cwd", owner.executionCwd);
  url.searchParams.set("path", path);
  return url;
}

async function browserResponse(owner: ConversationBrowserOwner, method: string, path: string, signal?: AbortSignal) {
  const response = await fetchWithTimeout(browserUrl(owner, method, path), { headers: authHeaders(), signal });
  if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
  return response;
}

export async function fetchConversationBrowserTree(owner: ConversationBrowserOwner, path = ".", signal?: AbortSignal): Promise<ConversationBrowserTree> {
  return (await browserResponse(owner, "browser-tree", path, signal)).json();
}

export async function fetchConversationBrowserPreview(owner: ConversationBrowserOwner, path: string, signal?: AbortSignal): Promise<WorkspaceFilePreviewResponse> {
  return (await browserResponse(owner, "browser-preview", path, signal)).json();
}

export async function fetchConversationBrowserOriginal(owner: ConversationBrowserOwner, path: string, signal?: AbortSignal): Promise<Blob> {
  return (await browserResponse(owner, "browser-raw", path, signal)).blob();
}

export function conversationBrowserRawUrl(owner: ConversationBrowserOwner, path: string): string {
  const url = browserUrl(owner, "browser-raw", path);
  const secret = new Headers(authHeaders()).get("X-MiniCode-Token");
  if (secret) {
    const expiresAt = Math.floor(Date.now() / 1000) + 300;
    const payload = `browser_raw:v1:${url.searchParams.get("session_id")}:${owner.conversationId}:${owner.executionCwd}:${path}:${expiresAt}`;
    const encoder = new TextEncoder();
    const signature = hmac(sha256, encoder.encode(secret), encoder.encode(payload));
    const encoded = btoa(Array.from(signature, (byte) => String.fromCharCode(byte)).join("")).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
    url.searchParams.set("raw_token", `${expiresAt}.${encoded}`);
  }
  return url.toString();
}
