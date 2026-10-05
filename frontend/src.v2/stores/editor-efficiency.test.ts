/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./index";
import { clearEditorWorkspaceBufferCacheForTests, loadPersistedEditorTabs, persistEditorTabs } from "./shared-helpers";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import { EditorNavigation } from "../panels/editorNavigation";

beforeEach(() => {
  localStorage.clear(); clearEditorWorkspaceBufferCacheForTests();
  useAppStore.setState({ workingDirectory: "/editor-test", editorTabs: [], activeTabPath: null, activeEditorPath: null,
    editorOpenRequests: [], workbenchPreferences: { ...defaultWorkbenchPreferences } });
});
describe("editor workspace efficiency", () => {
  it("replaces clean preview tabs, keeps modified buffers and persists pin metadata", () => {
    const store = useAppStore.getState();
    store.openEditorTab("a.py", { preview: true }); store.markTabLoaded("a.py", "a = 1");
    store.openEditorTab("b.py", { preview: true }); store.markTabLoaded("b.py", "b = 1");
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["b.py"]);
    store.updateTabContent("b.py", "b = 2"); store.openEditorTab("c.py", { preview: true });
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["b.py", "c.py"]);
    store.pinEditorTab("c.py", true);
    persistEditorTabs(useAppStore.getState().editorTabs, "/editor-test");
    expect(loadPersistedEditorTabs("/editor-test").find((tab) => tab.path === "c.py")).toMatchObject({ pinned: true, preview: false });
    expect(loadPersistedEditorTabs("/editor-test").find((tab) => tab.path === "b.py")).toMatchObject({ content: "b = 2", original: "b = 1" });
  });
  it("stores the same typography and editor preferences that the DOM consumes", () => {
    useAppStore.getState().setWorkbenchPreferences({ codeFont: "Consolas", proseFont: "Georgia", proseSize: 18, tabSize: 2, formatOnSave: true });
    expect(document.documentElement.style.getPropertyValue("--editor-font-family")).toBe("Consolas");
    expect(document.documentElement.style.getPropertyValue("--font-prose")).toBe("Georgia");
    expect(JSON.parse(localStorage.getItem("minicode.workbench.preferences")!)).toMatchObject({ proseSize: 18, tabSize: 2, formatOnSave: true });
  });
  it("returns to the previous location, supports forward, and drops the old forward branch after a new jump", () => {
    const history = new EditorNavigation();
    history.record({ path: "a.ts", line: 1, column: 1 }); history.record({ path: "a.ts", line: 3, column: 2 });
    history.record({ path: "b.ts", line: 10, column: 4 });
    expect(history.go(-1)).toEqual({ path: "a.ts", line: 3, column: 2 });
    expect(history.go(1)?.path).toBe("b.ts");
    history.go(-1); history.record({ path: "c.ts", line: 30, column: 1 });
    expect(history.go(1)).toBeNull(); expect(history.entries).toHaveLength(2);
  });
  it("records explicit short jumps separately from normal cursor movement", () => {
    const history = new EditorNavigation();
    history.record({ path: "a.ts", line: 1, column: 1 });
    history.record({ path: "a.ts", line: 8, column: 7 }, true);
    expect(history.go(-1)).toEqual({ path: "a.ts", line: 1, column: 1 });
    expect(history.go(1)).toEqual({ path: "a.ts", line: 8, column: 7 });
  });
});
