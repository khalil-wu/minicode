import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { clearEditorWorkspaceBufferCacheForTests } from "./shared-helpers";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
const storage = new Map<string, string>();
beforeEach(() => {
  clearEditorWorkspaceBufferCacheForTests();
  storage.clear();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  useAppStore.setState({ workingDirectory: "C:/project", editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [] });
});

describe("indexed model edit adoption", () => {
  it("promotes multiple changed sources atomically without changing the active buffer or its baseline", () => {
    useAppStore.setState({
      editorTabs: [{ id: "active", path: "src/active.ts", content: "draft", original: "disk", contentHash: "active-hash", loading: false }],
      activeTabPath: "src/active.ts", activeEditorPath: "src/active.ts",
    });
    const updates: Array<{ paths: string[]; loading: boolean[] }> = [];
    const unsubscribe = useAppStore.subscribe((state) => updates.push({ paths: state.editorTabs.map((tab) => tab.path), loading: state.editorTabs.map((tab) => tab.loading) }));
    useAppStore.getState().adoptEditorModelChanges([
      { path: "src/active.ts", content: "renamed active", original: "stale index disk", contentHash: "stale-index-hash" },
      { path: "C:/project/src/closed.ts", content: "renamed closed", original: "closed disk", contentHash: "closed-hash", sizeBytes: 20 },
    ], "C:/project");
    unsubscribe();
    expect(updates).toEqual([{ paths: ["src/active.ts", "src/closed.ts"], loading: [false, false] }]);
    const state = useAppStore.getState();
    expect(state.editorTabs[0]).toMatchObject({ id: "active", content: "renamed active", original: "disk", contentHash: "active-hash" });
    expect(state.editorTabs[1]).toMatchObject({ path: "src/closed.ts", content: "renamed closed", original: "closed disk", contentHash: "closed-hash", loading: false, error: null, sizeBytes: 20 });
    expect(state.activeTabPath).toBe("src/active.ts");
    expect(state.activeEditorPath).toBe("src/active.ts");
  });

  it("updates an adopted buffer on undo while avoiding tabs for unchanged closed sources", () => {
    const change = { path: "src/closed.ts", content: "edited", original: "original", contentHash: "hash" };
    useAppStore.getState().adoptEditorModelChanges([change], "C:/project");
    const id = useAppStore.getState().editorTabs[0].id;
    useAppStore.getState().adoptEditorModelChanges([
      { ...change, content: change.original },
      { path: "src/unchanged.ts", content: "same", original: "same", contentHash: "same-hash" },
    ], "C:/project");
    expect(useAppStore.getState().editorTabs).toHaveLength(1);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ id, content: "original", original: "original", contentHash: "hash" });
    expect(useAppStore.getState().activeTabPath).toBeNull();
  });

  it("keeps changes associated with their original workspace after the visible workspace switches", () => {
    useAppStore.getState().setWorkingDirectory("C:/other");
    useAppStore.getState().adoptEditorModelChanges([{ path: "src/closed.ts", content: "edited", original: "disk", contentHash: "hash" }], "C:/project");
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().workingDirectory).toBe("C:/other");
    useAppStore.getState().setWorkingDirectory("C:/project");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ path: "src/closed.ts", content: "edited", original: "disk", contentHash: "hash", loading: false });
  });
});
