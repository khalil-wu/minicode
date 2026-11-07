/* @vitest-environment jsdom */
import { expect, it, vi } from "vitest";
import { fetchConversationDirectory } from "./conversation-directory";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("./api", () => ({ apiBase: () => "http://owned-backend", errorMessageFromResponseText: (text: string) => text,
  authHeaders: (initial: HeadersInit) => { const headers = new Headers(initial); headers.set("X-MiniCode-Token", "owned-runtime"); return headers; },
  fetchWithTimeout: request }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "owner" }) }));

it("preserves runtime authentication and JSON headers on the actual directory request", async () => {
  request.mockImplementation(async (url: URL, init: RequestInit) => {
    const headers = new Headers(init.headers);
    expect(headers.get("X-MiniCode-Token")).toBe("owned-runtime");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(url.searchParams.get("session_id")).toBe("original-session");
    expect(JSON.parse(String(init.body))).toMatchObject({ cursor: "page-cursor", sidebar_scope: "recent", conversation_type: "main" });
    return { ok: true, json: async () => ({ data: [], next_cursor: null, inventory_revision: 3, inventory_instance_id: "store" }) };
  });
  await fetchConversationDirectory({ cursor: "page-cursor", sidebar_scope: "recent" }, "original-session");
  expect(request).toHaveBeenCalledOnce();
});
