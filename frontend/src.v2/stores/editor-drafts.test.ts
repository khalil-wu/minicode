/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { cachedEditorDrafts, cachedEditorWorkspaceIndex, flushEditorDrafts, loadEditorDrafts, resetEditorDraftStorageForTests } from "./editor-drafts";
import { cacheEditorStateForWorkspace, clearEditorWorkspaceBufferCacheForTests, dirtyEditorFiles, editorStateForWorkspace, loadEditorViewState, loadPersistedEditorTabs, persistEditorTabs, persistEditorViewState, restoreWorkspaceEditorDrafts } from "./shared-helpers";

const root = "C:/draft-storage";
const key = root.toLowerCase();
beforeEach(async () => {
  window.__MINICODE_RUNTIME__ = { ...window.__MINICODE_RUNTIME__, windowOwnerId: "primary" };
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
  it("cold reopens two windows' different buffers and selections for the same workspace and path", async () => {
    persistEditorTabs([authoredTab("app.ts", "primary content")], root, { activeTabPath: "app.ts", activeEditorPath: null });
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1" };
    persistEditorTabs([authoredTab("app.ts", "secondary content"), authoredTab("other.ts", "secondary only")], root, { activeTabPath: "other.ts", activeEditorPath: "app.ts" });
    await restart();
    const secondary = await loadEditorDrafts(key, [], "unused-secondary-source");
    expect(secondary.get("app.ts")?.content).toBe("secondary content");
    expect(cachedEditorWorkspaceIndex(key)).toMatchObject({ paths: ["app.ts", "other.ts"], activeTabPath: "other.ts", activeEditorPath: "app.ts" });
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
    const primary = await loadEditorDrafts(key, [], "unused-primary-source");
    expect([...primary.keys()]).toEqual(["app.ts"]);
    expect(primary.get("app.ts")?.content).toBe("primary content");
    expect(cachedEditorWorkspaceIndex(key)).toMatchObject({ paths: ["app.ts"], activeTabPath: "app.ts", activeEditorPath: null });
  });

  it("scopes local tab indexes, view state and hidden editor activity to their actual window", () => {
    localStorage.setItem(`minicode.editor.tabs:${JSON.stringify(["primary", key])}`, JSON.stringify(["primary.ts"]));
    localStorage.setItem(`minicode.editor.tabs:${JSON.stringify(["secondary-1", key])}`, JSON.stringify(["secondary.ts"]));
    persistEditorViewState(root, "app.ts", { kind: "code", state: { cursor: 11 } });
    cacheEditorStateForWorkspace(root, [authoredTab("primary.ts", "primary hidden")], "primary.ts", null);
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1" };
    persistEditorViewState(root, "app.ts", { kind: "code", state: { cursor: 22 } });
    cacheEditorStateForWorkspace(root, [authoredTab("secondary.ts", "secondary hidden")], "secondary.ts", null);
    expect(loadPersistedEditorTabs(root).map(tab => tab.path)).toEqual(["secondary.ts"]);
    expect(loadEditorViewState(root, "app.ts")?.state).toEqual({ cursor: 22 });
    expect(dirtyEditorFiles({ workingDirectory: "", editorTabs: [] })).toEqual([key + "/secondary.ts"]);
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
    expect(loadPersistedEditorTabs(root).map(tab => tab.path)).toEqual(["primary.ts"]);
    expect(loadEditorViewState(root, "app.ts")?.state).toEqual({ cursor: 11 });
    expect(dirtyEditorFiles({ workingDirectory: "", editorTabs: [] })).toEqual([key + "/primary.ts"]);
  });

  it("only the native primary can recover an unscoped localStorage body, then preserves it at the old IDB key", async () => {
    const legacyKey = "minicode.editor.drafts:" + key;
    localStorage.setItem("minicode.editor.tabs:" + key, JSON.stringify(["legacy.ts"]));
    localStorage.setItem(legacyKey, JSON.stringify([authoredTab("legacy.ts", "legacy body")]));
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "secondary-1" };
    expect(loadPersistedEditorTabs(root)).toEqual([]);
    expect(await restoreWorkspaceEditorDrafts(root)).toEqual([]);
    expect(localStorage.getItem(legacyKey)).not.toBeNull();
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "primary" };
    expect((await restoreWorkspaceEditorDrafts(root))[0].content).toBe("legacy body");
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("minicode-editor-drafts", 2);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    const request = db.transaction("drafts").objectStore("drafts").get([key, "legacy.ts"]);
    expect(await new Promise(resolve => { request.onsuccess = () => resolve(request.result.tab.content); })).toBe("legacy body");
    db.close();
  });

  it("atomically assigns legacy browser recovery to one upgrading window and retains the original rows", async () => {
    const legacy = [authoredTab("legacy.ts", "browser legacy body")];
    const legacyKey = "minicode.editor.drafts:" + key;
    localStorage.setItem(legacyKey, JSON.stringify(legacy));
    const [first, second] = await Promise.all([
      loadEditorDrafts(key, legacy, legacyKey, undefined, "browser-first"),
      loadEditorDrafts(key, legacy, legacyKey, undefined, "browser-second"),
    ]);
    expect(first.get("legacy.ts")?.content).toBe("browser legacy body");
    expect(second.size).toBe(0);
    await restart();
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "browser-first" };
    expect((await loadEditorDrafts(key, [], legacyKey)).get("legacy.ts")?.content).toBe("browser legacy body");
    window.__MINICODE_RUNTIME__ = { windowOwnerId: "browser-second" };
    expect((await loadEditorDrafts(key, legacy, legacyKey)).size).toBe(0);
  });

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

  it("retains both the previous durable body and last authored buffer after an actual abort, then flush retries without another edit", async () => {
    useAppStore.setState({ editorTabs: [authoredTab("app.ts", "previous committed body")] });
    await flushEditorDrafts();
    const put = IDBObjectStore.prototype.put;
    const failing = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (...args) {
      const request = put.apply(this, args);
      if (this.name === "drafts" && this.transaction.db.name === "minicode-editor-drafts") this.add(args[0]);
      return request;
    });
    useAppStore.getState().updateTabContent("app.ts", "last authored body");
    await expect(flushEditorDrafts()).rejects.toMatchObject({ name: "ConstraintError" });
    expect(useAppStore.getState().editorTabs[0].content).toBe("last authored body");
    expect(cachedEditorDrafts(key)?.get("app.ts")?.content).toBe("previous committed body");
    failing.mockRestore();
    await flushEditorDrafts();
    await restart();
    expect((await loadEditorDrafts(key, [], "unused-legacy-source")).get("app.ts")?.content).toBe("last authored body");
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

  it.each(["close", "reload"] as const)("keeps the window open until the queued draft commits and resumes the actual %s intent without saving user files", async action => {
    const close = vi.fn(async () => true);
    const intent = { id: `editor-${action}`, action, ignoreCache: action === "reload" };
    const captureUnloadIntent = vi.fn(() => intent);
    const resumeUnload = vi.fn(async () => true);
    const previousRuntime = window.__MINICODE_RUNTIME__;
    window.__MINICODE_RUNTIME__ = { ...previousRuntime, desktop: { windowControls: { close, captureUnloadIntent, resumeUnload } } as never };
    useAppStore.setState({ editorTabs: [authoredTab()] });
    useAppStore.getState().updateTabContent("app.ts", "last authored change");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    expect(close).not.toHaveBeenCalled();
    expect(resumeUnload).not.toHaveBeenCalled();
    await flushEditorDrafts();
    await Promise.resolve();
    expect(cachedEditorDrafts(key)?.get("app.ts")?.content).toBe("last authored change");
    await vi.waitFor(() => expect(resumeUnload).toHaveBeenCalledExactlyOnceWith(intent.id));
    expect(captureUnloadIntent).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
    window.__MINICODE_RUNTIME__ = previousRuntime;
  });
});
