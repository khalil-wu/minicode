/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() => { Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }); });
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
import { useAppStore } from "./index";
import { createWorkspaceSlice } from "./workspace-slice";
import { ensureCodePanelSlots } from "./shared-helpers";
import { emptyConversationWorkbenchState } from "./ui-slice";
import type { WorkspaceFileOwner } from "./types";

const home: WorkspaceFileOwner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
beforeEach(() => {
  localStorage.clear();
  const dirty = { id: "dirty", path: "keep.ts", label: "keep.ts", content: "unsaved", original: "saved", loading: false };
  useAppStore.setState({ workspaceFileTabs: [], fileBrowserRoots: {}, conversationId: null, workingDirectory: "C:/home", conversations: [],
    draft: "home draft", workspaceComposerDrafts: {}, conversationWorkbenchStates: {}, previewArtifact: null, editorTabs: [dirty], activeTabPath: dirty.path,
    appMode: "code", panelSlots: [{ id: "chat", kind: "chat", focused: true }, { id: "editor", kind: "editor", focused: false }],
    pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingWorkspaceFileTabId: null, editorOpenRequests: [] });
});

describe("independent file tabs and persistent references", () => {
  it("opens two Home references without replacing previews, chat drafts or unsaved editor buffers", () => {
    const before = useAppStore.getState();
    const a = before.openWorkspaceFileTab(home, "a.ts");
    const b = useAppStore.getState().openWorkspaceFileTab(home, "b.ts");
    const state = useAppStore.getState();
    expect(state.workspaceFileTabs.map(tab => tab.id)).toEqual([a, b]);
    expect(state).toMatchObject({ conversationId: null, workingDirectory: "C:/home", draft: "home draft", previewArtifact: null, rightStackTab: `file:${b}` });
    expect(state.editorTabs).toBe(before.editorTabs);
    expect(state.conversationWorkbenchStates).toBe(before.conversationWorkbenchStates);
    state.moveWorkspaceFileTab(a, "left"); state.moveWorkspaceFileTab(b, "left");
    expect(ensureCodePanelSlots(useAppStore.getState().panelSlots).filter(slot => slot.kind === "file")).toHaveLength(2);
    useAppStore.getState().openEditorFile("next.ts");
    expect(useAppStore.getState().panelSlots.filter(slot => slot.kind === "file")).toHaveLength(2);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ path: "keep.ts", content: "unsaved", original: "saved" });
  });

  it("restores metadata after slice reconstruction and reopens a closed pin with its exact owner", () => {
    const id = useAppStore.getState().openWorkspaceFileTab(home, "notes/a.ts");
    useAppStore.getState().pinWorkspaceFileTab(id, true);
    useAppStore.getState().moveWorkspaceFileTab(id, "left");
    useAppStore.getState().closeWorkspaceFileTab(id);
    const restored = createWorkspaceSlice(useAppStore.setState, useAppStore.getState, useAppStore);
    expect(restored.workspaceFileTabs).toEqual([{ id, owner: home, path: "notes/a.ts", title: "a.ts", open: false, pinned: true, pane: "left", isPreview: false }]);
    const stored = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)!).find(key => key.startsWith("minicode.workspaceFiles."))!;
    expect(localStorage.getItem(stored)).not.toContain("unsaved"); expect(localStorage.getItem(stored)).not.toContain("home draft");
    useAppStore.setState({ workspaceFileTabs: restored.workspaceFileTabs });
    useAppStore.getState().activateWorkspaceFileTab(id);
    expect(useAppStore.getState().workspaceFileTabs[0]).toMatchObject({ owner: home, open: true, pinned: true, pane: "left" });
  });

  it("uses the existing switch request when a pinned reference belongs to another conversation", () => {
    const switchConversation = vi.spyOn(useAppStore.getState(), "requestConversationSwitch").mockImplementation(() => true);
    const owner = { ...home, scopeId: "conversation:owner-b", conversationId: "owner-b", executionCwd: "C:/b", browserRoot: "C:/b" };
    const id = useAppStore.getState().openWorkspaceFileTab(owner, "b.txt");
    expect(switchConversation).toHaveBeenCalledWith("owner-b");
    expect(useAppStore.getState()).toMatchObject({ conversationId: null, pendingWorkspaceFileTabId: id, workingDirectory: "C:/home", draft: "home draft" });
    switchConversation.mockRestore();
  });

  it("restores an older same-owner tab by its original absolute file without touching drafts, buffers or selection", () => {
    const cwd = "C:/tasks/2026-10-25/report";
    const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd, browserRoot: cwd };
    const legacy = { id: "persisted-file", owner, path: "outputs/alignment-check.md", title: "alignment-check.md",
      open: true, pinned: true, pane: "left" as const, line: 7 };
    useAppStore.setState({ conversationId: "a", workingDirectory: cwd,
      conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: "C:/tasks" }],
      workspaceFileTabs: [legacy], rightStackTab: `file:${legacy.id}`,
      panelSlots: [{ id: `workspace-file:${legacy.id}`, kind: "file", fileTabId: legacy.id, focused: true }],
      conversationWorkbenchStates: { a: { ...emptyConversationWorkbenchState(), draft: "hidden draft",
        attachments: [{ id: "hidden-file", name: "hidden.md", type: "text/markdown", size: 3, status: "ready" }] } } });
    const before = useAppStore.getState();
    before.reconcileWorkspaceFileTabs();
    const repaired = useAppStore.getState();
    expect(repaired.workspaceFileTabs).toEqual([{ ...legacy, owner: { ...owner, browserRoot: "C:/tasks" },
      path: "2026-10-25/report/outputs/alignment-check.md" }]);
    expect(repaired.editorTabs).toBe(before.editorTabs);
    expect(repaired.conversationWorkbenchStates).toBe(before.conversationWorkbenchStates);
    expect(repaired.panelSlots).toBe(before.panelSlots);
    expect(repaired).toMatchObject({ conversationId: "a", workingDirectory: cwd, draft: "home draft", rightStackTab: `file:${legacy.id}` });
    const restored = createWorkspaceSlice(useAppStore.setState, useAppStore.getState, useAppStore);
    expect(restored.workspaceFileTabs).toEqual(repaired.workspaceFileTabs);
    const reopened = useAppStore.getState().openWorkspaceFileTab(owner, "outputs/alignment-check.md");
    expect(reopened).toBe(legacy.id);
    expect(useAppStore.getState().workspaceFileTabs).toHaveLength(1);
  });

  it("waits for authoritative metadata and never retargets a genuine stale execution owner", () => {
    const cwd = "C:/tasks/2026-10-25/report";
    const legacy = { id: "late-owner", owner: { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd, browserRoot: cwd },
      path: "outputs/report.md", title: "report.md", open: true, pinned: false, pane: "right" as const };
    useAppStore.setState({ workingDirectory: cwd, workspaceFileTabs: [legacy], conversations: [] });
    useAppStore.getState().reconcileWorkspaceFileTabs();
    expect(useAppStore.getState().workspaceFileTabs[0]).toBe(legacy);
    useAppStore.setState({ conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: "C:/different", projectlessBrowserRoot: "C:/tasks" }] });
    useAppStore.getState().reconcileWorkspaceFileTabs();
    expect(useAppStore.getState().workspaceFileTabs[0]).toBe(legacy);
    useAppStore.setState({ conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: "C:/tasks" }] });
    useAppStore.getState().reconcileWorkspaceFileTabs();
    expect(useAppStore.getState().workspaceFileTabs[0]).toMatchObject({ id: legacy.id, path: "2026-10-25/report/outputs/report.md",
      owner: { executionCwd: cwd, browserRoot: "C:/tasks" } });
  });
});
