/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { cachedEditorDrafts, cachedEditorWorkspaceIndex, flushEditorDrafts, loadEditorDrafts, resetEditorDraftStorageForTests } from "./editor-drafts";
import { clearEditorWorkspaceBufferCacheForTests, editorStateForWorkspace, restoreWorkspaceEditorDrafts } from "./shared-helpers";

const root = "C:/draft-storage";
const key = root.toLowerCase();
beforeEach(async () => {
  localStorage.clear();
  clearEditorWorkspaceBufferCacheForTests();
  useAppStore.setState({ workingDirectory: root, editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [] });
  await flushEditorDrafts();
  await resetEditorDraftStorageForTests();
});
const authoredTab = (path = "app.ts", content = "authored") => ({ id: path, path, content, original: "disk", loading: false });
const restart = async () => {
  await flushEditorDrafts();
  await resetEditorDraftStorageForTests(true);
  clearEditorWorkspaceBufferCacheForTests();
};

describe("durable editor drafts", () => {
  it("commits large drafts by file and never sends their bodies through localStorage", async () => {
    const writes = vi.spyOn(Storage.prototype, "setItem");
    const body = "A".repeat(600000);
    useAppStore.setState({ editorTabs: Array.from({ length: 20 }, (_, i) => ({ ...authoredTab(`file-${i}.ts`, body + i), original: body })) });
    await flushEditorDrafts();
    const put = vi.spyOn(IDBObjectStore.prototype, "put");
    for (let i = 0; i < 15; i++) useAppStore.getState().updateTabContent("file-0.ts", body + "-latest-" + i);
    await flushEditorDrafts();
    const bodies = put.mock.calls.filter((_, index) => put.mock.contexts[index].name === "drafts");
    expect(bodies.length).toBeLessThanOrEqual(2);
    expect(bodies.every(([record]) => record.path === "file-0.ts")).toBe(true);
    for (const store of put.mock.contexts) {
      if (store.name === "drafts") expect(put.mock.contexts.some(indexStore => indexStore.name === "workspaces" && indexStore.transaction === store.transaction)).toBe(true);
    }
    expect(writes.mock.calls.every(([, value]) => value.length < 10000)).toBe(true);
    await restart();
    const restored = await loadEditorDrafts(key, [], "minicode.editor.drafts:" + key);
    expect(restored.size).toBe(20);
    expect(restored.get("file-0.ts")?.content).toBe(body + "-latest-14");
    writes.mockRestore(); put.mockRestore();
  });

  it.each([{ paths: [] }, { paths: ["older-clean.ts"] }])("recovers version-1 committed bodies when a valid old LS index is stale: $paths", async ({ paths: stalePaths }) => {
    await resetEditorDraftStorageForTests();
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("minicode-editor-drafts", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("drafts", { keyPath: ["workspace", "path"] }).createIndex("workspace", "workspace");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const transaction = db.transaction("drafts", "readwrite");
    transaction.objectStore("drafts").put({ workspace: key, path: "app.ts", tab: authoredTab() });
    await new Promise<void>(resolve => { transaction.oncomplete = () => resolve(); });
    db.close();
    localStorage.setItem("minicode.editor.tabs:" + key, JSON.stringify(stalePaths));
    const restored = await restoreWorkspaceEditorDrafts(root);
    expect(restored.find(tab => tab.path === "app.ts")?.content).toBe("authored");
    useAppStore.setState(editorStateForWorkspace(root));
    await flushEditorDrafts();
    await restart();
    expect((await loadEditorDrafts(key, [], "minicode.editor.drafts:" + key)).get("app.ts")?.content).toBe("authored");
  });

  it("opening an existing file while its cold index is being read cannot clear the saved dirty body", async () => {
    useAppStore.setState({ editorTabs: [authoredTab()] });
    await restart();
    // A new renderer has no live root buffer to cache while moving away.
    useAppStore.setState({ workingDirectory: "C:/other-before-cold-open", editorTabs: [], activeTabPath: null, activeEditorPath: null });
    await flushEditorDrafts();
    let resume!: () => void;
    let observed!: () => void;
    const released = new Promise<void>(resolve => { resume = resolve; });
    const reading = new Promise<void>(resolve => { observed = resolve; });
    const getAll = IDBIndex.prototype.getAll;
    const read = vi.spyOn(IDBIndex.prototype, "getAll").mockImplementationOnce(function (...args) {
      const request = getAll.apply(this, args);
      Object.defineProperty(request, "onsuccess", { set(callback) {
        request.addEventListener("success", event => { observed(); void released.then(() => callback.call(request, event)); });
      } });
      return request;
    });
    try {
      useAppStore.getState().setWorkingDirectory(root);
      await reading;
      expect(useAppStore.getState().editorTabs).toEqual([]);
      useAppStore.getState().openEditorTab("app.ts");
      expect(useAppStore.getState().editorTabs[0].recoveryPending).toBe(true);
      resume();
      await restoreWorkspaceEditorDrafts(root);
      await flushEditorDrafts();
      expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "authored", original: "disk", draftRestorePending: true });
      await restart();
      expect((await loadEditorDrafts(key, [], "minicode.editor.drafts:" + key)).get("app.ts")?.content).toBe("authored");
    } finally {
      resume();
      read.mockRestore();
      await flushEditorDrafts();
    }
  });

  it("keeps the saved selection when a cold workspace is hidden before its index finishes loading", async () => {
    useAppStore.setState({ editorTabs: [authoredTab("a.ts"), authoredTab("b.ts")], activeTabPath: "b.ts", activeEditorPath: "b.ts" });
    await restart();
    useAppStore.setState({ workingDirectory: "C:/cold-away", editorTabs: [], activeTabPath: null, activeEditorPath: null });
    useAppStore.getState().setWorkingDirectory(root);
    useAppStore.getState().setWorkingDirectory("C:/cold-away");
    await restoreWorkspaceEditorDrafts(root);
    await flushEditorDrafts();
    useAppStore.getState().setWorkingDirectory(root);
    expect(useAppStore.getState().editorTabs.map(tab => tab.path)).toEqual(["a.ts", "b.ts"]);
    expect(useAppStore.getState()).toMatchObject({ activeTabPath: "b.ts", activeEditorPath: "b.ts" });
  });

  it("persists the renamed selected file in a hidden workspace, including its editor location", async () => {
    useAppStore.setState({ editorTabs: [authoredTab("a.ts"), authoredTab("b.ts")], activeTabPath: "b.ts", activeEditorPath: "b.ts" });
    await flushEditorDrafts();
    useAppStore.getState().setWorkingDirectory("C:/visible-other-workspace");
    useAppStore.getState().renameEditorPath("b.ts", "z.ts", root);
    await restart();
    await loadEditorDrafts(key, [], "minicode.editor.drafts:" + key);
    const restored = editorStateForWorkspace(root);
    expect(restored.editorTabs.map(tab => tab.path)).toEqual(["a.ts", "z.ts"]);
    expect(restored).toMatchObject({ activeTabPath: "z.ts", activeEditorPath: "z.ts" });
  });

  it("restores the complete index, metadata and selection with localStorage fully unavailable", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("localStorage full", "QuotaExceededError"); });
    useAppStore.getState().openEditorTab("app.ts");
    useAppStore.getState().markTabLoaded("app.ts", "disk", null);
    useAppStore.getState().updateTabContent("app.ts", "authored under full localStorage");
    useAppStore.getState().pinEditorTab("app.ts", true);
    useAppStore.getState().setEditorTabLanguage("app.ts", "cpp");
    useAppStore.setState({ activeEditorPath: "app.ts" });
    await restart();
    useAppStore.getState().setWorkingDirectory("C:/new-cold-workspace");
    useAppStore.getState().setWorkingDirectory(root);
    await restoreWorkspaceEditorDrafts(root);
    await flushEditorDrafts();
    expect(useAppStore.getState().editorTabs).toHaveLength(1);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "authored under full localStorage", original: "disk", pinned: true, language: "cpp" });
    expect(useAppStore.getState()).toMatchObject({ activeTabPath: "app.ts", activeEditorPath: "app.ts" });
    expect(cachedEditorWorkspaceIndex(key)?.paths).toEqual(["app.ts"]);
    expect(localStorage.getItem("minicode.editor.tabs:" + key)).toBeNull();
    storage.mockRestore();
  });

  it("allows the next authored update to commit after an actual open error and then a load error", async () => {
    const open = vi.spyOn(indexedDB, "open").mockImplementationOnce(() => {
      const request = { error: new DOMException("open denied", "UnknownError"), onerror: null as ((event: Event) => void) | null };
      queueMicrotask(() => request.onerror?.(new Event("error")));
      return request as unknown as IDBOpenDBRequest;
    });
    useAppStore.setState({ editorTabs: [authoredTab()] });
    await expect(flushEditorDrafts()).rejects.toThrow("open denied");
    open.mockRestore();
    const read = vi.spyOn(IDBIndex.prototype, "getAll").mockImplementationOnce(() => {
      const request = { error: new DOMException("read failed", "UnknownError"), onerror: null as ((event: Event) => void) | null };
      queueMicrotask(() => request.onerror?.(new Event("error")));
      return request as unknown as IDBRequest;
    });
    useAppStore.getState().updateTabContent("app.ts", "newer after open failure");
    await expect(flushEditorDrafts()).rejects.toThrow("read failed");
    read.mockRestore();
    useAppStore.getState().updateTabContent("app.ts", "final after failures");
    await flushEditorDrafts();
    await restart();
    expect((await loadEditorDrafts(key, [], "minicode.editor.drafts:" + key)).get("app.ts")?.content).toBe("final after failures");
  });

  it("migrates existing recovery bodies only after the IndexedDB commit succeeds", async () => {
    localStorage.setItem("minicode.editor.tabs:" + key, JSON.stringify(["app.ts"]));
    const legacyKey = "minicode.editor.drafts:" + key;
    localStorage.setItem(legacyKey, JSON.stringify([authoredTab()]));
    useAppStore.setState(editorStateForWorkspace(root));
    const restored = await restoreWorkspaceEditorDrafts(root);
    expect(restored[0]).toMatchObject({ content: "authored", original: "disk", draftRestorePending: true });
    expect(localStorage.getItem(legacyKey)).toBeNull();
    await flushEditorDrafts();
    await resetEditorDraftStorageForTests(true);
    expect((await loadEditorDrafts(key, [], legacyKey)).get("app.ts")?.content).toBe("authored");
  });

  it.each(["edit", "close"])("keeps the actual cold-start tab index authoritative during legacy migration and immediate %s", async action => {
    const legacyKey = "minicode.editor.drafts:" + key;
    localStorage.setItem("minicode.editor.tabs:" + key, JSON.stringify(["app.ts"]));
    localStorage.setItem(legacyKey, JSON.stringify([authoredTab()]));
    useAppStore.setState(editorStateForWorkspace(root));
    expect(useAppStore.getState().editorTabs[0].recoveryPending).toBe(true);
    if (action === "edit") useAppStore.getState().updateTabContent("app.ts", "new input during migration");
    else useAppStore.getState().closeEditorTab("app.ts");
    await restoreWorkspaceEditorDrafts(root);
    await flushEditorDrafts();
    expect(localStorage.getItem(legacyKey)).toBeNull();
    if (action === "edit") expect(useAppStore.getState().editorTabs[0].content).toBe("new input during migration");
    else expect(useAppStore.getState().editorTabs).toEqual([]);
    await resetEditorDraftStorageForTests(true);
    const durable = await loadEditorDrafts(key, [], legacyKey);
    expect(durable.get("app.ts")?.content).toBe(action === "edit" ? "new input during migration" : undefined);
  });

  it("keeps the old recovery body and exposes a failed migration commit", async () => {
    const legacyKey = "minicode.editor.drafts:" + key;
    const legacy = [authoredTab()];
    localStorage.setItem(legacyKey, JSON.stringify(legacy));
    const put = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementationOnce(() => { throw new DOMException("out of space", "QuotaExceededError"); });
    await expect(loadEditorDrafts(key, legacy, legacyKey)).rejects.toThrow("out of space");
    expect(JSON.parse(localStorage.getItem(legacyKey)!)).toEqual(legacy);
    put.mockRestore();
  });

  it("does not overwrite a newer authored buffer when a cold restore finishes after switching away", async () => {
    useAppStore.setState({ editorTabs: [authoredTab()] });
    await restart();
    useAppStore.setState({ ...editorStateForWorkspace(root), workingDirectory: root });
    useAppStore.getState().openEditorTab("app.ts");
    useAppStore.getState().updateTabContent("app.ts", "newer input");
    useAppStore.getState().setWorkingDirectory("C:/other-draft-workspace");
    await restoreWorkspaceEditorDrafts(root);
    useAppStore.getState().setWorkingDirectory(root);
    expect(useAppStore.getState().editorTabs[0].content).toBe("newer input");
    await flushEditorDrafts();
    expect(cachedEditorDrafts(key)?.get("app.ts")?.content).toBe("newer input");
  });

  it("does not resurrect a closed tab when restore finishes late, and moves a pending renamed draft", async () => {
    useAppStore.setState({ editorTabs: [authoredTab()] });
    await restart();
    useAppStore.setState(editorStateForWorkspace(root));
    useAppStore.getState().openEditorTab("app.ts");
    useAppStore.getState().renameEditorPath("app.ts", "renamed.ts", root);
    await restoreWorkspaceEditorDrafts(root);
    await flushEditorDrafts();
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ path: "renamed.ts", content: "authored" });
    await restart();
    useAppStore.setState(editorStateForWorkspace(root));
    useAppStore.getState().openEditorTab("renamed.ts");
    useAppStore.getState().closeEditorTab("renamed.ts");
    await restoreWorkspaceEditorDrafts(root);
    await flushEditorDrafts();
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(cachedEditorDrafts(key)?.size).toBe(0);
  });

  it("keeps the window open until the final queued draft commits, without saving user files", async () => {
    const close = vi.fn(async () => true);
    const previousRuntime = window.__MINICODE_RUNTIME__;
    window.__MINICODE_RUNTIME__ = { desktop: { windowControls: { close } } as never };
    useAppStore.setState({ editorTabs: [authoredTab()] });
    useAppStore.getState().updateTabContent("app.ts", "last authored change");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    expect(close).not.toHaveBeenCalled();
    await flushEditorDrafts();
    await Promise.resolve();
    expect(cachedEditorDrafts(key)?.get("app.ts")?.content).toBe("last authored change");
    expect(close).toHaveBeenCalledOnce();
    window.__MINICODE_RUNTIME__ = previousRuntime;
  });
});
