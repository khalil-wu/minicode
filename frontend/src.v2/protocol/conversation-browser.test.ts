import { createHmac } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ sessionId: "session-a" as string | null, token: "browser-secret", fetch: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => mocks.sessionId ? { sessionId: mocks.sessionId } : null }));
vi.mock("./api", () => ({
  apiBase: () => "http://127.0.0.1:8000",
  authHeaders: () => mocks.token ? { "X-MiniCode-Token": mocks.token } : {},
  fetchWithTimeout: mocks.fetch,
  ApiError: class extends Error { constructor(public status: number, message: string) { super(message); } },
  errorMessageFromResponseText: (body: string) => JSON.parse(body).detail,
}));
import { conversationBrowserRawUrl, fetchConversationBrowserOriginal, fetchConversationBrowserPreview, fetchConversationBrowserTree } from "./conversation-browser";

const owner = { conversationId: "localmint-a", executionCwd: "C:/tasks/a" };
beforeEach(() => { mocks.fetch.mockReset(); mocks.sessionId = "session-a"; mocks.token = "browser-secret"; });
afterEach(() => vi.useRealTimers());

describe("conversation browser ownership", () => {
  it("keeps tree, preview and exact raw reads scoped to the connected execution owner", async () => {
    const page = { workspace_root: "C:/Users/ago/Documents", requested_path: ".", entries: [] };
    const preview = { path: "notes/a#b.txt", file_name: "a#b.txt", content: "preview only" };
    const bytes = new Uint8Array([0, 255, 10, 13, 65]);
    mocks.fetch.mockResolvedValueOnce(new Response(JSON.stringify(page)))
      .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
      .mockResolvedValueOnce(new Response(bytes, { headers: { "content-type": "application/octet-stream" } }));
    const signal = new AbortController().signal;
    expect(await fetchConversationBrowserTree(owner, ".", signal)).toEqual(page);
    expect(await fetchConversationBrowserPreview(owner, "notes/a#b.txt", signal)).toEqual(preview);
    expect(new Uint8Array(await (await fetchConversationBrowserOriginal(owner, "notes/a#b.txt", signal)).arrayBuffer())).toEqual(bytes);
    for (const [url, options] of mocks.fetch.mock.calls) {
      expect(Object.fromEntries(url.searchParams)).toMatchObject({ session_id: "session-a", conversation_id: "localmint-a", execution_cwd: "C:/tasks/a" });
      expect(url.searchParams.has("workspace_root")).toBe(false);
      expect(url.searchParams.has("base")).toBe(false);
      expect(options).toEqual({ headers: { "X-MiniCode-Token": "browser-secret" }, signal });
    }
    expect(mocks.fetch.mock.calls.map(([url]) => url.pathname)).toEqual(["/api/workspace/browser-tree", "/api/workspace/browser-preview", "/api/workspace/browser-raw"]);
    expect(mocks.fetch.mock.calls[2][0].searchParams.get("path")).toBe("notes/a#b.txt");
  });

  it("signs native raw URLs with the backend's exact owner/path contract and re-signs after reconnect", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-13T00:00:00Z"));
    const path = "images/报告 #1.png";
    const expires = Math.floor(Date.now() / 1000) + 300;
    const payload = `browser_raw:v1:session-a:localmint-a:C:/tasks/a:${path}:${expires}`;
    const signature = createHmac("sha256", "browser-secret").update(payload).digest("base64url");
    const url = new URL(conversationBrowserRawUrl(owner, path));
    expect(url.searchParams.get("raw_token")).toBe(`${expires}.${signature}`);
    expect(url.toString()).not.toContain("browser-secret");
    mocks.sessionId = "session-b";
    const next = new URL(conversationBrowserRawUrl(owner, path));
    expect(next.searchParams.get("session_id")).toBe("session-b");
    expect(next.searchParams.get("raw_token")).not.toBe(url.searchParams.get("raw_token"));
    expect(next.searchParams.get("execution_cwd")).toBe("C:/tasks/a");
  });

  it("surfaces stale owners and stops issuing reads without a connected session", async () => {
    mocks.fetch.mockResolvedValue(new Response(JSON.stringify({ detail: "File browser execution owner is stale." }), { status: 409 }));
    await expect(fetchConversationBrowserOriginal(owner, "old.txt")).rejects.toThrow("File browser execution owner is stale.");
    mocks.sessionId = null;
    await expect(fetchConversationBrowserTree(owner)).rejects.toThrow("connected session");
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });
});
