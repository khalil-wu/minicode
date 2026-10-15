/* @vitest-environment jsdom */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "./index";
import { deleteConversationComposerDrafts, emptyComposerDraft, flushComposerDrafts, loadComposerDrafts, resetComposerDraftStorageForTests } from "./composer-drafts";
import { flushEditorDrafts } from "./editor-drafts";
import { buildUpdateActivitySnapshot } from "../desktop/updateActivityMirror";

const close = vi.fn(async () => true);
let intent: { id: string; action: "close" | "reload"; ignoreCache: boolean };
const captureUnloadIntent = vi.fn(() => intent);
const resumeUnload = vi.fn(async (id: string) => id === intent.id);
const attemptUnload = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
};
beforeEach(async () => {
  await waitComposerDraftRecovery();
  await resetComposerDraftStorageForTests();
  close.mockClear();
  captureUnloadIntent.mockClear(); resumeUnload.mockClear();
  intent = { id: "fixture-close", action: "close", ignoreCache: false };
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary", desktop: { windowControls: { close, captureUnloadIntent, resumeUnload } } as never };
  useAppStore.setState({ ...emptyComposerDraft(), workingDirectory: "", conversationId: null, conversations: [],
    workspaceComposerDrafts: {}, conversationWorkbenchStates: {}, sideChats: {},
    composerDraftsReady: true, editorTabs: [], activeTabPath: null, activeEditorPath: null });
  await Promise.all([flushComposerDrafts(), flushEditorDrafts()]);
});
afterEach(async () => {
  vi.restoreAllMocks();
  await resetComposerDraftStorageForTests();
  delete window.__MINICODE_RUNTIME__;
});

describe("composer recovery close and update boundary", () => {
  it.each(["close", "reload"] as const)("retains authored input and blocks %s/update after an actual abort, then resumes the captured native intent after commit", async action => {
    intent = { id: `fixture-${action}`, action, ignoreCache: action === "reload" };
    vi.spyOn(console, "error").mockImplementation(() => {});
    const put = IDBObjectStore.prototype.put;
    const failing = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
      const request = put.apply(this, args);
      if (this.name === "drafts" && this.transaction.db.name === "minicode-composer-drafts") this.add(args[0]);
      return request;
    });
    useAppStore.getState().setDraft("最后一段未发送内容");
    await vi.waitFor(() => expect(useAppStore.getState().composerDraftStorageError).toBeTruthy());
    const activity = buildUpdateActivitySnapshot(useAppStore.getState());
    expect(activity.pendingDrafts).toEqual(["primary:commit", "primary:storage-error"]);
    expect(activity.draftStates).toContain(JSON.stringify(["primary", "workspace", ""]));
    attemptUnload();
    await vi.waitFor(() => expect(console.error).toHaveBeenCalledWith("Window remains open because authored drafts did not commit", expect.anything()));
    expect(close).not.toHaveBeenCalled();
    expect(resumeUnload).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("最后一段未发送内容");
    failing.mockRestore();
    attemptUnload();
    await vi.waitFor(() => expect(resumeUnload).toHaveBeenCalledExactlyOnceWith(intent.id));
    expect(captureUnloadIntent).toHaveLastReturnedWith(intent);
    expect(close).not.toHaveBeenCalled();
    expect(buildUpdateActivitySnapshot(useAppStore.getState()).pendingDrafts).toEqual([]);
    expect((await loadComposerDrafts())[0].draft.draft).toBe("最后一段未发送内容");
  });

  it.each(["close", "reload"] as const)("keeps a confirmed deletion pending on disk-full and lets the next %s commit the same authorised tombstone", async action => {
    intent = { id: `fixture-${action}`, action, ignoreCache: false };
    vi.spyOn(console, "error").mockImplementation(() => {});
    useAppStore.setState({ conversationId: "backend-deleted", draft: "old deleted input" });
    await flushComposerDrafts();
    const put = IDBObjectStore.prototype.put;
    const failing = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
      if (this.name === "deletedConversations") throw new DOMException("Disk is full", "QuotaExceededError");
      return put.apply(this, args);
    });
    await expect(deleteConversationComposerDrafts("backend-deleted")).rejects.toMatchObject({ name: "QuotaExceededError" });
    useAppStore.setState({ conversationId: null, ...emptyComposerDraft(), conversationWorkbenchStates: {} });
    attemptUnload();
    await vi.waitFor(() => expect(console.error).toHaveBeenCalledWith("Window remains open because authored drafts did not commit", expect.anything()));
    expect(close).not.toHaveBeenCalled();
    expect(resumeUnload).not.toHaveBeenCalled();
    expect(buildUpdateActivitySnapshot(useAppStore.getState()).pendingDrafts).toEqual(["primary:commit", "primary:storage-error"]);
    failing.mockRestore();
    attemptUnload();
    await vi.waitFor(() => expect(resumeUnload).toHaveBeenCalledExactlyOnceWith(intent.id));
    expect(close).not.toHaveBeenCalled();
    expect(await loadComposerDrafts()).toEqual([]);
    expect(useAppStore.getState().composerDraftStorageError).toBeNull();
  });
});
