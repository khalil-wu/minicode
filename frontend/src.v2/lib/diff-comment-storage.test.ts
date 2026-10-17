/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cloneDiffComments, diffCommentKey, type DiffComment, type DiffCommentDraft } from "./diff-comments";

type Storage = typeof import("./diff-comment-storage");
let storage: Storage;
const disposers: Array<() => void> = [];
const saved = (text = "comment", line = 202): DiffComment => ({ type: "comment",
  content: [{ content_type: "text", text }], position: { path: "src/a.ts", side: "right", line },
  localWorkspaceRoot: "C:/work", localDiffHunk: "@@ -100 +202 @@\n-old\n+new",
});
const draft = (ownerId = "reader-a"): DiffCommentDraft => ({ key: `${ownerId}:right:202`, ownerId,
  position: { path: "src/a.ts", side: "right", line: 202 }, text: "unsaved private text" });

beforeEach(async () => {
  localStorage.clear(); sessionStorage.clear();
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
  vi.resetModules();
  storage = await import("./diff-comment-storage");
});
afterEach(() => {
  for (const dispose of disposers.splice(0)) dispose();
  delete window.__MINICODE_RUNTIME__;
  vi.restoreAllMocks();
});

describe("saved Diff comments and reader-owned in-memory drafts", () => {
  it("keeps stable snapshots between changes and notifies both local writes and draft changes", () => {
    const listener = vi.fn();
    disposers.push(storage.subscribeDiffComments(listener));
    const empty = storage.readDiffCommentState("thread-a");
    expect(storage.readDiffCommentState("thread-a")).toBe(empty);
    storage.saveDiffComment("thread-a", saved());
    const savedState = storage.readDiffCommentState("thread-a");
    expect(savedState).not.toBe(empty);
    expect(storage.readDiffCommentState("thread-a")).toBe(savedState);
    storage.setDiffCommentDraft("thread-a", draft());
    expect(storage.readDiffCommentState("thread-a").comments).toBe(savedState.comments);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("isolates real conversations and window-owned workspace composers", () => {
    const workspace = storage.getDiffCommentScope(null, "C:\\Work\\");
    expect(storage.getDiffCommentScope(null, "c:/work")).toBe(workspace);
    expect(storage.getDiffCommentScope("thread-a", "c:/other")).toBe("thread-a");
    storage.saveDiffComment(workspace, saved("workspace"));
    storage.saveDiffComment("thread-a", saved("a"));
    storage.saveDiffComment("thread-b", saved("b"));
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary" };
    const secondary = storage.getDiffCommentScope(null, "C:/work");
    expect(secondary).not.toBe(workspace);
    expect(storage.readDiffCommentState(secondary).comments).toEqual([]);
    expect(storage.readDiffCommentState(workspace).comments).toEqual([saved("workspace")]);
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([saved("a")]);
    expect(storage.readDiffCommentState("thread-b").comments).toEqual([saved("b")]);
  });

  it("durably reopens only saved comments and never writes unsaved line text", async () => {
    storage.saveDiffComment("thread-a", saved());
    const writes = vi.spyOn(Storage.prototype, "setItem");
    storage.setDiffCommentDraft("thread-a", draft());
    expect(writes).not.toHaveBeenCalled();
    expect(JSON.stringify({ ...localStorage })).not.toContain("unsaved private text");
    expect(storage.readDiffCommentState("thread-a").drafts[draft().key]).toEqual(draft());
    vi.resetModules();
    const reopened = await import("./diff-comment-storage");
    expect(reopened.readDiffCommentState("thread-a")).toMatchObject({ comments: [saved()], drafts: {} });
  });

  it("clones saved objects and edits only the prior real comment key", () => {
    const first = saved();
    storage.saveDiffComment("thread-a", first);
    first.content[0].text = "mutated caller";
    first.position.line = 999;
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([saved()]);
    storage.saveDiffComment("thread-a", saved("another", 300));
    storage.saveDiffComment("thread-a", saved("edited"), saved());
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([saved("edited"), saved("another", 300)]);
  });

  it("migrates client comments into a populated conversation with existing-first deduplication", () => {
    storage.saveDiffComment("client-a", saved());
    storage.saveDiffComment("client-a", saved("new", 400));
    const old = { ...saved(), localDiffHunk: "kept existing hunk" };
    storage.saveDiffComment("thread-a", old);
    storage.saveDiffComment("thread-b", saved("unrelated"));
    storage.transferDiffComments("client-a", "thread-a");
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([old, saved("new", 400)]);
    expect(storage.readDiffCommentState("client-a").comments).toEqual([]);
    expect(localStorage.getItem("minicode.diff_comments.client-a")).toBeNull();
    expect(storage.readDiffCommentState("thread-b").comments).toEqual([saved("unrelated")]);
    const before = storage.readDiffCommentState("thread-a");
    storage.transferDiffComments("thread-a", "thread-a");
    expect(storage.readDiffCommentState("thread-a")).toBe(before);
  });

  it("clears only a submitted snapshot and preserves comments added or edited during send", () => {
    storage.saveDiffComment("thread-a", saved());
    const submitted = cloneDiffComments(storage.readDiffCommentState("thread-a").comments);
    storage.saveDiffComment("thread-a", saved("new user edit"), saved());
    storage.saveDiffComment("thread-a", saved("new comment", 301));
    storage.clearSubmittedDiffComments("thread-a", submitted);
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([saved("new user edit"), saved("new comment", 301)]);
    storage.restoreSubmittedDiffComments("thread-a", submitted);
    storage.restoreSubmittedDiffComments("thread-a", submitted);
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([saved("new user edit"), saved("new comment", 301), saved()]);
    expect(submitted).toEqual([saved()]);
  });

  it("removes the empty conversation key without erasing another saved scope or reader draft", () => {
    storage.saveDiffComment("thread-a", saved());
    storage.saveDiffComment("thread-b", saved("other"));
    storage.setDiffCommentDraft("thread-a", draft());
    storage.removeDiffComment("thread-a", diffCommentKey(saved()));
    expect(localStorage.getItem("minicode.diff_comments.thread-a")).toBeNull();
    expect(storage.readDiffCommentState("thread-a").drafts[draft().key]).toEqual(draft());
    storage.clearDiffCommentScope("thread-a");
    expect(storage.readDiffCommentState("thread-a")).toMatchObject({ comments: [], drafts: {} });
    expect(storage.readDiffCommentState("thread-b").comments).toEqual([saved("other")]);
  });

  it("lets a reader release its own draft while preserving the other reader", () => {
    storage.setDiffCommentDraft("thread-a", draft());
    storage.setDiffCommentDraft("thread-a", draft("reader-b"));
    const input = draft();
    storage.setDiffCommentDraft("thread-a", input);
    input.position.line = 1000;
    expect(storage.readDiffCommentState("thread-a").drafts[draft().key].position.line).toBe(202);
    storage.removeDiffCommentDraft("thread-a", draft().key);
    expect(Object.values(storage.readDiffCommentState("thread-a").drafts)).toEqual([draft("reader-b")]);
  });

  it("updates subscribed windows on external storage changes while retaining their in-memory draft", () => {
    storage.saveDiffComment("thread-a", saved());
    storage.setDiffCommentDraft("thread-a", draft());
    const before = storage.readDiffCommentState("thread-a"), listener = vi.fn();
    disposers.push(storage.subscribeDiffComments(listener));
    localStorage.setItem("minicode.diff_comments.thread-a", JSON.stringify([saved("from another window")]));
    window.dispatchEvent(new StorageEvent("storage", { key: "minicode.diff_comments.thread-a" }));
    const after = storage.readDiffCommentState("thread-a");
    expect(after.comments).toEqual([saved("from another window")]);
    expect(after.drafts).toBe(before.drafts);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(storage.readDiffCommentState("thread-a")).toBe(after);
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated.preference" }));
    expect(listener).toHaveBeenCalledTimes(1);
    localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    expect(storage.readDiffCommentState("thread-a").comments).toEqual([]);
    expect(storage.readDiffCommentState("thread-a").drafts).toBe(before.drafts);
  });

  it("does not claim a comment was saved when persistence rejects the write", () => {
    const before = storage.readDiffCommentState("thread-a"), listener = vi.fn();
    disposers.push(storage.subscribeDiffComments(listener));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    expect(() => storage.saveDiffComment("thread-a", saved())).toThrow("full");
    expect(storage.readDiffCommentState("thread-a")).toBe(before);
    expect(listener).not.toHaveBeenCalled();
  });
});
