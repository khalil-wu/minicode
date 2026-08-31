import { afterEach, describe, expect, it, vi } from "vitest";

const loadWorkspace = async () => {
  vi.resetModules();
  vi.stubEnv("DEV", true as never);
  vi.stubGlobal("__MINICODE_RUNTIME__", undefined);
  vi.stubGlobal("location", {
    protocol: "http:",
    host: "127.0.0.1:5173",
  });
  return import("./workspace");
};

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("listWorkspaceTree", () => {
  it("normalizes backend tree entries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      workspace_root: "C:\\Desktop\\MiniCode",
      requested_path: ".",
      entries: [{
        name: "backend",
        path: "backend",
        is_dir: true,
        size_bytes: null,
        modified_at: "2026-06-04T00:00:00Z",
        has_children: true,
      }],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const { listWorkspaceTree } = await loadWorkspace();
    const tree = await listWorkspaceTree("C:\\Desktop\\MiniCode", ".");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:5173/api/workspace/tree?workspace_root=C%3A%5CDesktop%5CMiniCode&path=.",
      expect.any(Object),
    );
    expect(tree?.children?.[0]).toMatchObject({
      name: "backend",
      path: "backend",
      is_dir: true,
      has_children: true,
    });
  });

  it("surfaces backend error details instead of returning null", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: "Workspace folder is missing: C:\\missing",
    }), { status: 404, statusText: "Not Found", headers: { "content-type": "application/json" } })));

    const { listWorkspaceTree } = await loadWorkspace();

    await expect(listWorkspaceTree("C:\\missing", ".")).rejects.toThrow("Workspace folder is missing: C:\\missing");
  });

  it("surfaces network failures with a diagnostic file tree message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const { listWorkspaceTree } = await loadWorkspace();

    await expect(listWorkspaceTree("C:\\Desktop\\MiniCode", ".")).rejects.toThrow(/Could not load file tree.*API request failed/i);
  });
});
