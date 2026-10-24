/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() => { Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }); });
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
import { useAppStore } from "./index";
import { createWorkspaceSlice } from "./workspace-slice";
import { ensureCodePanelSlots } from "./shared-helpers";
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
    expect(restored.workspaceFileTabs).toEqual([{ id, owner: home, path: "notes/a.ts", title: "a.ts", open: false, pinned: true, pane: "left" }]);
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
});
