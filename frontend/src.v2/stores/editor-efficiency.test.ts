/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./index";
import { flushEditorDrafts } from "./editor-drafts";
import { clearEditorWorkspaceBufferCacheForTests, loadPersistedEditorTabs, persistEditorTabs } from "./shared-helpers";
import { applyWorkbenchPreferences, defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import { EditorNavigation } from "../panels/editorNavigation";

beforeEach(() => {
  localStorage.clear(); clearEditorWorkspaceBufferCacheForTests();
  useAppStore.setState({ workingDirectory: "/editor-test", editorTabs: [], activeTabPath: null, activeEditorPath: null,
    editorOpenRequests: [], workbenchPreferences: { ...defaultWorkbenchPreferences } });
});
describe("editor workspace efficiency", () => {
  it("replaces clean preview tabs, keeps modified buffers and persists pin metadata", async () => {
    const store = useAppStore.getState();
    store.openEditorTab("a.py", { preview: true }); store.markTabLoaded("a.py", "a = 1");
    store.openEditorTab("b.py", { preview: true }); store.markTabLoaded("b.py", "b = 1");
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["b.py"]);
    store.updateTabContent("b.py", "b = 2"); store.openEditorTab("c.py", { preview: true });
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["b.py", "c.py"]);
    store.pinEditorTab("c.py", true);
    persistEditorTabs(useAppStore.getState().editorTabs, "/editor-test");
    await flushEditorDrafts();
    expect(loadPersistedEditorTabs("/editor-test").find((tab) => tab.path === "c.py")).toMatchObject({ pinned: true, preview: false });
    expect(loadPersistedEditorTabs("/editor-test").find((tab) => tab.path === "b.py")).toMatchObject({ content: "b = 2", original: "b = 1" });
  });
  it("ignores legacy font choices while preserving sizes and unrelated editor preferences", () => {
    const legacy = { ...defaultWorkbenchPreferences, uiFont: "Georgia", proseFont: "Georgia", codeFont: "Fira Code", ligatures: true,
      proseSize: 17, tabSize: 2, formatOnSave: true, speechModel: "saved-speech-model", snippets: [{ id: "saved", language: "typescript", prefix: "log", body: "console.log($1)", description: "log" }] };
    const style = document.documentElement.style;
    const fontProperties = ["--font-ui", "--font-prose", "--font-mono", "--editor-font-family"];
    for (const property of fontProperties) style.setProperty(property, "LegacyFont");
    useAppStore.setState({ workbenchPreferences: legacy });

    applyWorkbenchPreferences(useAppStore.getState().workbenchPreferences);

    for (const property of fontProperties) expect(style.getPropertyValue(property)).toBe("");
    expect(style.getPropertyValue("--mc-font-reading")).toBe("17px");
    expect(style.getPropertyValue("--prose-font-size")).toBe("17px");
    style.setProperty("--font-ui", "OldInlineOverride");
    useAppStore.getState().setWorkbenchPreferences({ proseSize: 18 });
    expect(style.getPropertyValue("--font-ui")).toBe("");
    expect(style.getPropertyValue("--mc-font-reading")).toBe("18px");
    expect(JSON.parse(localStorage.getItem("minicode.workbench.preferences")!)).toMatchObject({ proseSize: 18, tabSize: 2, formatOnSave: true,
      speechModel: "saved-speech-model", snippets: legacy.snippets });
  });
  it("persists a file's chosen language with its unchanged draft and restores automatic mode", async () => {
    const store = useAppStore.getState();
    store.openEditorTab("example.txt");
    store.markTabLoaded("example.txt", "// saved comment\nint answer = 42;");
    store.updateTabContent("example.txt", "// unsaved comment\nint answer = 43;");
    store.setEditorTabLanguage("example.txt", "cpp");
    await flushEditorDrafts();
    const before = useAppStore.getState().editorTabs[0];
    clearEditorWorkspaceBufferCacheForTests();
    const restored = loadPersistedEditorTabs("/editor-test");
    expect(restored[0]).toMatchObject({ language: "cpp", content: before.content, original: before.original });
    useAppStore.setState({ editorTabs: restored });
    store.setEditorTabLanguage("example.txt", undefined);
    clearEditorWorkspaceBufferCacheForTests();
    expect(loadPersistedEditorTabs("/editor-test")[0]).toMatchObject({ content: before.content, original: before.original });
    expect(loadPersistedEditorTabs("/editor-test")[0].language).toBeUndefined();
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
