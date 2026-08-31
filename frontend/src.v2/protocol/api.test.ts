import { afterEach, describe, expect, it, vi } from "vitest";

const loadApi = async (env: Record<string, string | boolean | undefined> = {}) => {
  vi.resetModules();
  vi.stubGlobal("__MINICODE_RUNTIME__", undefined);
  vi.stubGlobal("location", {
    protocol: "http:",
    host: "127.0.0.1:5173",
  });
  vi.stubEnv("DEV", ("DEV" in env ? env.DEV : true) as never);
  vi.stubEnv("VITE_API_BASE_URL", env.VITE_API_BASE_URL as string | undefined);
  vi.stubEnv("VITE_WS_BASE_URL", env.VITE_WS_BASE_URL as string | undefined);
  vi.stubEnv("VITE_DEV_BACKEND_ORIGIN", env.VITE_DEV_BACKEND_ORIGIN as string | undefined);
  return import("./api");
};

describe("api base URLs", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("uses the current origin in dev so Vite proxy handles API and WS requests", async () => {
    const { apiBase, wsBase } = await loadApi({
      VITE_DEV_BACKEND_ORIGIN: "http://127.0.0.1:8000",
    });

    expect(apiBase()).toBe("http://127.0.0.1:5173");
    expect(wsBase()).toBe("ws://127.0.0.1:5173");
  });

  it("keeps using the Vite proxy in dev even when backend env overrides are present", async () => {
    const { apiBase, wsBase } = await loadApi({
      VITE_API_BASE_URL: "http://127.0.0.1:8000/",
      VITE_WS_BASE_URL: "ws://127.0.0.1:8000/",
    });

    expect(apiBase()).toBe("http://127.0.0.1:5173");
    expect(wsBase()).toBe("ws://127.0.0.1:5173");
  });

  it("uses the Vite proxy for runtime overrides that match the dev backend", async () => {
    vi.resetModules();
    vi.stubEnv("DEV", true as never);
    vi.stubEnv("VITE_DEV_BACKEND_ORIGIN", "http://127.0.0.1:8100");
    vi.stubEnv("VITE_WS_BASE_URL", "ws://127.0.0.1:8100");
    vi.stubGlobal("__MINICODE_RUNTIME__", {
      apiBaseUrl: "http://127.0.0.1:8100/",
      wsBaseUrl: "ws://127.0.0.1:8100/",
    });

    const api = await import("./api");

    expect(api.apiBase()).toBe("http://127.0.0.1:5173");
    expect(api.wsBase()).toBe("ws://127.0.0.1:5173");
  });

  it("honors runtime overrides in dev when they do not match the Vite proxy backend", async () => {
    vi.resetModules();
    vi.stubEnv("DEV", true as never);
    vi.stubEnv("VITE_DEV_BACKEND_ORIGIN", "http://127.0.0.1:8000");
    vi.stubGlobal("__MINICODE_RUNTIME__", {
      apiBaseUrl: "http://127.0.0.1:8100/",
      wsBaseUrl: "ws://127.0.0.1:8100/",
    });

    const api = await import("./api");

    expect(api.apiBase()).toBe("http://127.0.0.1:8100");
    expect(api.wsBase()).toBe("ws://127.0.0.1:8100");
  });

  it("keeps runtime tokens out of websocket URLs and exposes a subprotocol token", async () => {
    vi.resetModules();
    vi.stubEnv("DEV", true as never);
    vi.stubGlobal("__MINICODE_RUNTIME__", {
      wsBaseUrl: "ws://127.0.0.1:8100/",
      runtimeToken: "secret-token",
    });

    const api = await import("./api");
    const url = api.wsUrl("/ws", { session_id: "session-token-test" });

    expect(url).toBe("ws://127.0.0.1:8100/ws?session_id=session-token-test");
    expect(url).not.toContain("minicode_token");
    expect(api.wsProtocols()).toEqual(["minicode", "minicode-token.c2VjcmV0LXRva2Vu"]);
  });

  it("does not append runtime tokens to ordinary URLs and keeps query tokens scoped to raw resources", async () => {
    vi.resetModules();
    vi.stubEnv("DEV", false as never);
    vi.stubGlobal("__MINICODE_RUNTIME__", {
      apiBaseUrl: "http://127.0.0.1:8100/",
      runtimeToken: "secret-token",
    });
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);

    const api = await import("./api");
    const rawUrl = new URL(api.workspaceRawResourceUrlWithToken(
      "docs/report.pdf",
      "C:\\Desktop\\MiniCode",
    ));
    const artifactUrl = new URL(api.artifactRawResourceUrlWithToken(
      "artifact-image-1",
      "session-image-1",
      "conversation-image-1",
    ));

    expect(api.withRuntimeToken("http://127.0.0.1:8100/api/status"))
      .toBe("http://127.0.0.1:8100/api/status");
    expect(rawUrl.origin + rawUrl.pathname).toBe("http://127.0.0.1:8100/api/workspace/raw");
    expect(rawUrl.searchParams.get("path")).toBe("docs/report.pdf");
    expect(rawUrl.searchParams.get("workspace_root")).toBe("C:\\Desktop\\MiniCode");
    expect(rawUrl.searchParams.get("minicode_token")).toBeNull();
    expect(rawUrl.searchParams.get("raw_token")).toMatch(/^1700000300\.[A-Za-z0-9_-]+$/);
    expect(rawUrl.searchParams.get("raw_token"))
      .toBe("1700000300.7MsASRtFfalA2xuHddiZYwztaSIplIeIIyakubaheY8");
    expect(artifactUrl.origin + artifactUrl.pathname).toBe("http://127.0.0.1:8100/api/artifacts/raw");
    expect(artifactUrl.searchParams.get("artifact_id")).toBe("artifact-image-1");
    expect(artifactUrl.searchParams.get("session_id")).toBe("session-image-1");
    expect(artifactUrl.searchParams.get("conversation_id")).toBe("conversation-image-1");
    expect(artifactUrl.searchParams.get("asset_token")).toMatch(/^1700000300\.[A-Za-z0-9_-]+$/);
    expect(artifactUrl.searchParams.get("minicode_token")).toBeNull();
    expect(api.withRuntimeToken("https://example.com/download"))
      .toBe("https://example.com/download");

    nowSpy.mockRestore();
  });

  it("keeps runtime token query helper out of general frontend callers", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname, "..");
    const files = [
      path.join(root, "panels", "EditorPanel.tsx"),
      path.join(root, "shell", "fileTreeHelpers.tsx"),
    ];

    for (const file of files) {
      const source = fs.readFileSync(file, "utf8");
      expect(source).not.toContain("withRuntimeToken(");
    }
  });
});
