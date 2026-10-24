import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ sessionId: "session-a" as string | null, fetch: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => mocks.sessionId ? { sessionId: mocks.sessionId } : null }));
vi.mock("./api", () => ({ apiBase: () => "http://127.0.0.1:8000", authHeaders: () => ({ "X-MiniCode-Token": "secret" }), fetchWithTimeout: mocks.fetch,
  ApiError: class extends Error { constructor(public status: number, message: string) { super(message); } }, errorMessageFromResponseText: (body: string) => JSON.parse(body).detail }));
import { readFileWorkspaceApplyPatch, readFileWorkspaceBranches, readFileWorkspaceChanges, readFileWorkspaceOriginal, readFileWorkspaceTree, searchFileWorkspace } from "./file-workspace";
import type { WorkspaceFileOwner } from "../stores/types";

const home: WorkspaceFileOwner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
const chat: WorkspaceFileOwner = { scopeId: "conversation:a", conversationId: "a", executionCwd: "C:/tasks/a", browserRoot: "C:/Users/a/Documents", environmentId: "local-a" };
beforeEach(() => { mocks.fetch.mockReset().mockImplementation(async () => new Response(JSON.stringify({ entries: [], results: [], files: [], diff: "", is_git_repo: true }))); mocks.sessionId = "session-a"; });
const urlAt = (index: number) => new URL(String(mocks.fetch.mock.calls[index][0]));

describe("captured file workspace HTTP ownership", () => {
  it("reads Home without creating a conversation and preserves literal paths", async () => {
    mocks.sessionId = null;
    await readFileWorkspaceTree(home);
    await readFileWorkspaceOriginal(home, "notes/a#b & c.txt");
    await searchFileWorkspace(home, "deep child");
    expect(urlAt(0).pathname).toBe("/api/workspace/tree");
    expect(urlAt(1).searchParams.get("path")).toBe("notes/a#b & c.txt");
    for (const call of mocks.fetch.mock.calls) {
      const url = new URL(String(call[0]));
      expect(url.searchParams.get("workspace_root")).toBe("C:/home");
      expect(url.searchParams.has("session_id")).toBe(false);
      expect(url.searchParams.has("conversation_id")).toBe(false);
    }
    expect(urlAt(2).searchParams.get("query")).toBe("deep child");
  });

  it("keeps browser reads and comparisons with the captured executor", async () => {
    await readFileWorkspaceTree(chat);
    await readFileWorkspaceBranches(chat, undefined, "origin/main");
    await readFileWorkspaceChanges(chat, { source: "commit", commitSha: "abc" }, true);
    await readFileWorkspaceApplyPatch(chat, { source: "branch", baseBranch: "origin/main" });
    expect(urlAt(0).pathname).toBe("/api/workspace/browser-tree");
    expect(urlAt(0).searchParams.get("execution_cwd")).toBe(chat.executionCwd);
    for (const call of mocks.fetch.mock.calls) {
      const params = new URL(String(call[0])).searchParams;
      expect(params.get("conversation_id")).toBe("a"); expect(params.get("session_id")).toBe("session-a");
      expect(params.get("environment_id")).toBe("local-a");
    }
    expect(urlAt(1).searchParams.get("base_branch")).toBe("origin/main");
    expect(Object.fromEntries(urlAt(2).searchParams)).toMatchObject({ workspace_root: chat.executionCwd, source: "commit", commit_sha: "abc", hide_whitespace: "true" });
    expect(Object.fromEntries(urlAt(3).searchParams)).toMatchObject({ source: "branch", base_branch: "origin/main", for_apply: "true" });
  });

  it("selects a child repository directory without replacing its executor owner", async () => {
    await readFileWorkspaceBranches(chat, undefined, "origin/main", "C:/tasks/a/nested");
    await readFileWorkspaceChanges(chat, { source: "staged" }, false, undefined, "C:/tasks/a/nested");
    expect(urlAt(0).searchParams.get("path")).toBe("C:/tasks/a/nested");
    expect(Object.fromEntries(urlAt(1).searchParams)).toMatchObject({ workspace_root: chat.executionCwd, conversation_id: "a", path: "C:/tasks/a/nested", source: "staged" });
  });
});
