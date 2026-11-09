import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ sessionId: "session-a" as string | null, fetch: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => mocks.sessionId ? { sessionId: mocks.sessionId } : null }));
vi.mock("./api", () => ({ apiBase: () => "http://127.0.0.1:8000", authHeaders: () => ({ "X-MiniCode-Token": "secret" }), fetchWithTimeout: mocks.fetch,
  ApiError: class extends Error { constructor(public status: number, message: string) { super(message); } }, errorMessageFromResponseText: (body: string) => JSON.parse(body).detail }));
import { fileWorkspaceOwner, readFileWorkspaceApplyPatch, readFileWorkspaceBranches, readFileWorkspaceChanges, readFileWorkspaceDiff,
  readFileWorkspaceOriginal, readFileWorkspacePreview, readFileWorkspaceTree, reconcileWorkspaceFileTab, searchFileWorkspace, workspaceFileAbsolutePath } from "./file-workspace";
import type { WorkspaceFileOwner, WorkspaceFileTab } from "../stores/types";

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

  it("uses the projectless browser namespace for FS and the original cwd for the same file's Git diff", async () => {
    const state = { workingDirectory: "C:/tasks/2026-10-25/report", conversations: [{ id: "a", title: "Report", updatedAt: "",
      executionCwd: "C:/tasks/2026-10-25/report", projectlessBrowserRoot: "C:/tasks" }] };
    const owner = fileWorkspaceOwner(state, "a");
    const path = "2026-10-25/report/outputs/alignment-check.md";
    expect(owner).toMatchObject({ executionCwd: state.workingDirectory, browserRoot: "C:/tasks" });
    await readFileWorkspacePreview(owner, path);
    await readFileWorkspaceOriginal(owner, path);
    await readFileWorkspaceTree(owner, "2026-10-25/report");
    await searchFileWorkspace(owner, "alignment");
    await readFileWorkspaceBranches(owner, undefined, "origin/main");
    await readFileWorkspaceDiff(owner, path, "origin/main");
    expect(urlAt(0).pathname).toBe("/api/workspace/browser-preview");
    for (let index = 0; index < 4; index++) expect(urlAt(index).searchParams.get("execution_cwd")).toBe(state.workingDirectory);
    expect(urlAt(0).searchParams.get("path")).toBe(path);
    expect(urlAt(1).searchParams.get("path")).toBe(path);
    expect(urlAt(2).searchParams.get("path")).toBe("2026-10-25/report");
    expect(urlAt(3).searchParams.get("query")).toBe("alignment");
    expect(urlAt(4).searchParams.get("workspace_root")).toBe(state.workingDirectory);
    expect(Object.fromEntries(urlAt(5).searchParams)).toMatchObject({ workspace_root: state.workingDirectory,
      file: "outputs/alignment-check.md", base_ref: "origin/main" });
    expect(workspaceFileAbsolutePath(owner, path)).toBe(`${state.workingDirectory}/outputs/alignment-check.md`);
  });

  it("rebases a captured reference by absolute identity only after matching canonical owner metadata", () => {
    const cwd = "C:/tasks/2026-10-25/report";
    const tab: WorkspaceFileTab = { id: "kept", owner: { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd,
      browserRoot: cwd, environmentId: "local-a" }, path: "outputs/a # 100%.md", title: "a # 100%.md", open: false, pinned: true, pane: "left", line: 9 };
    const state = { workingDirectory: cwd, conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: "C:/tasks" }] };
    const rebased = reconcileWorkspaceFileTab(tab, state);
    expect(rebased).toMatchObject({ id: tab.id, title: tab.title, open: false, pinned: true, pane: "left", line: 9,
      path: "2026-10-25/report/outputs/a # 100%.md", owner: { executionCwd: cwd, browserRoot: "C:/tasks", environmentId: "local-a" } });
    expect(workspaceFileAbsolutePath(rebased.owner, rebased.path)).toBe(workspaceFileAbsolutePath(tab.owner, tab.path));
    expect(reconcileWorkspaceFileTab(rebased, state)).toBe(rebased);
    expect(reconcileWorkspaceFileTab(tab, { ...state, conversations: [] })).toBe(tab);
    expect(reconcileWorkspaceFileTab(tab, { ...state, conversations: [{ ...state.conversations[0], executionCwd: "C:/another-owner" }] })).toBe(tab);
  });

  it("preserves native aliases and literal file URI characters without changing the browser owner", () => {
    expect(workspaceFileAbsolutePath(home, "notes/../a#b & c.txt")).toBe("C:/home/a#b & c.txt");
    const remote = { ...chat, executionCwd: "file:///remote/work", browserRoot: "file:///remote/work" };
    expect(workspaceFileAbsolutePath(remote, "outputs/a#b %.md")).toBe("file:///remote/work/outputs/a%23b%20%25.md");
    const uri = "file:///remote/work/outputs/a%23b%20%25.md";
    expect(workspaceFileAbsolutePath(remote, uri)).toBe(uri);
  });
});
