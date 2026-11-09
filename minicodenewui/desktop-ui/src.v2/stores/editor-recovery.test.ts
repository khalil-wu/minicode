/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./index";
import { flushEditorDrafts } from "./editor-drafts";
import { agentEditReviewScope, clearEditorWorkspaceBufferCacheForTests, editorStateForWorkspace, loadPersistedEditorTabs, loadEditorViewState, persistEditorViewState } from "./shared-helpers";

const root = "C:/projects/recovery";
beforeEach(() => {
  localStorage.clear();
  clearEditorWorkspaceBufferCacheForTests();
  useAppStore.setState({ workingDirectory: root, editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [], agentEditReviewKept: {} });
});
function dirty(path = "src/app.ts", content = "user draft") {
  const store = useAppStore.getState();
  store.openEditorTab(path);
  store.markTabLoaded(path, "disk baseline", null, "baseline-hash");
  store.updateTabContent(path, content);
}

describe("editor draft and reading-state recovery", () => {
  it("recovers unchanged-disk drafts and the selected file through a fresh workspace cache", async () => {
    dirty(); dirty("notes.md", "# Unsaved notes");
    useAppStore.getState().setActiveTab("src/app.ts");
    await flushEditorDrafts();
    clearEditorWorkspaceBufferCacheForTests();
    const recovered = editorStateForWorkspace(root);
    expect(recovered.activeTabPath).toBe("src/app.ts");
    expect(recovered.editorTabs.find((tab) => tab.path === "notes.md")).toMatchObject({ content: "# Unsaved notes", original: "disk baseline", draftRestorePending: true });
    useAppStore.setState(recovered);
    useAppStore.getState().markTabLoaded("src/app.ts", "disk baseline", null, "baseline-hash");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "user draft", original: "disk baseline", contentHash: "baseline-hash", externalChanged: false, loading: false, draftRestored: true });
  });
  it("retains the saved baseline when disk changed and retains the body even when the file is missing", async () => {
    dirty();
    await flushEditorDrafts();
    useAppStore.setState({ editorTabs: loadPersistedEditorTabs(root) });
    useAppStore.getState().markTabLoaded("src/app.ts", "external edit", null, "new-disk-hash");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "user draft", original: "disk baseline", contentHash: "baseline-hash", externalChanged: true });
    useAppStore.setState({ editorTabs: loadPersistedEditorTabs(root) });
    useAppStore.getState().markTabLoaded("src/app.ts", "", "File no longer exists");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "user draft", loading: false, error: null, externalChanged: true, loadWarning: "File no longer exists" });
  });
  it("removes saved or explicitly discarded drafts and keeps newer input during a save", async () => {
    dirty();
    useAppStore.getState().updateTabContent("src/app.ts", "newer draft");
    useAppStore.getState().markTabSaved("src/app.ts", "user draft", "saved-hash");
    await flushEditorDrafts();
    expect(loadPersistedEditorTabs(root)[0]).toMatchObject({ content: "newer draft", original: "user draft", contentHash: "saved-hash" });
    useAppStore.getState().updateTabContent("src/app.ts", "user draft");
    await flushEditorDrafts();
    const discarded = loadPersistedEditorTabs(root)[0];
    expect(discarded).toMatchObject({ content: "", original: "" });
    expect(discarded.draftRestorePending).toBeUndefined();
    dirty();
    useAppStore.getState().closeEditorTab("src/app.ts");
    expect(loadPersistedEditorTabs(root)).toEqual([]);
  });
  it("settles a saved hidden workspace and preserves reading state through a directory rename", async () => {
    dirty();
    const reading = { kind: "code" as const, state: { cursorState: [{ position: { lineNumber: 7, column: 3 } }], viewState: { scrollTop: 160 } } };
    persistEditorViewState(root, "src/app.ts", reading);
    useAppStore.getState().setWorkingDirectory("C:/projects/other");
    useAppStore.getState().markTabSaved("src/app.ts", "user draft", "saved-hash", 10, root);
    await flushEditorDrafts();
    clearEditorWorkspaceBufferCacheForTests();
    expect(loadPersistedEditorTabs(root)[0].content).toBe("");
    useAppStore.getState().setWorkingDirectory(root);
    useAppStore.getState().renameEditorPath("src", "code", root);
    expect(loadEditorViewState(root, "code/app.ts")).toEqual(reading);
    expect(loadEditorViewState(root, "src/app.ts")).toBeNull();
  });
  it("restores all open paths and does not serialize generated read-only results as drafts", async () => {
    for (let index = 0; index < 24; index++) useAppStore.getState().openEditorTab(`file-${index}.ts`);
    useAppStore.getState().markTabLoaded("file-23.ts", "generated", null, "hash", { readOnly: true });
    await flushEditorDrafts();
    expect(loadPersistedEditorTabs(root)).toHaveLength(24);
    expect(loadPersistedEditorTabs(root).some((tab) => tab.draftRestorePending)).toBe(false);
  });
  it("moves accepted review scope together with the renamed file", () => {
    dirty();
    useAppStore.getState().keepAgentEditBlocks(agentEditReviewScope(root, "conv", "turn", "src/app.ts"), ["block-key"]);
    useAppStore.getState().renameEditorPath("src", "code", root);
    expect(useAppStore.getState().agentEditReviewKept[agentEditReviewScope(root, "conv", "turn", "code/app.ts")]).toEqual(["block-key"]);
  });
});
