import { afterEach, describe, expect, it, vi } from "vitest";
import { compareWriteWorkspaceFile, createWorkspaceDirectory, deleteWorkspacePath, renameWorkspacePath, writeWorkspaceFile } from "./workspace";

afterEach(() => vi.unstubAllGlobals());

describe("workspace mutation error delivery", () => {
  it.each([
    ["create file", () => writeWorkspaceFile("same.txt", "", "C:/repo"), "File already exists."],
    ["create directory", () => createWorkspaceDirectory("same", "C:/repo"), "Path already exists: same"],
    ["delete", () => deleteWorkspacePath("protected", "C:/repo", true), "Refusing to modify protected path."],
  ] as const)("retains the server and transport errors for %s", async (_operation, run, message) => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: message }), { status: 409 }))
      .mockRejectedValueOnce(new Error("The network connection was lost"));
    vi.stubGlobal("fetch", fetch);
    await expect(run()).rejects.toThrow(message);
    await expect(run()).rejects.toThrow("The network connection was lost");
    expect(new URL(fetch.mock.calls[0][0]).searchParams.get("workspace_root")).toBe("C:/repo");
  });

  it("keeps a server save error visible", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Refusing to modify protected path." }), { status: 403 }));
    vi.stubGlobal("fetch", fetch);
    expect(await compareWriteWorkspaceFile("file.txt", "hash", "draft", "C:/repo")).toEqual({
      ok: false, conflict: false, message: "Refusing to modify protected path.",
    });
    const [url, request] = fetch.mock.calls[0];
    expect(new URL(url).searchParams.get("workspace_root")).toBe("C:/repo");
    expect(JSON.parse(request.body)).toEqual({ path: "file.txt", expected_hash: "hash", content: "draft" });
  });

  it("retains a transport error without calling it an offline connection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Request timed out after 15 seconds")));
    expect(await compareWriteWorkspaceFile("file.txt", "hash", "draft", "C:/repo")).toEqual({
      ok: false, conflict: false, message: "Request timed out after 15 seconds",
    });
  });

  it("delivers a rename conflict to the caller", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Target already exists: destination.txt" }), { status: 409 })));
    await expect(renameWorkspacePath("source.txt", "destination.txt", "C:/repo")).rejects.toThrow("Target already exists: destination.txt");
  });
});
