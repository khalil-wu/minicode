/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
});
import { useAppStore } from "./index";
import type { DiffReviewState } from "./types";

describe("workbench review and expanded surface ownership", () => {
  beforeEach(() => {
    useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", conversationWorkbenchStates: {},
      gitReviewRequest: null, diffReview: null, rightPanelExpanded: false, rightPanelOpen: false,
      editorExplorerOpen: true, workspaceSearchOpen: false, fileTreeRevealRequests: [],
      rightStackTab: "tasks", panelSlots: [{ id: "chat", kind: "chat", focused: true }, { id: "editor", kind: "editor", focused: false }] });
  });

  it("restores each conversation's review selection and expanded layout", () => {
    useAppStore.getState().openGitReview({ path: "src/main.ts", section: "staged", workspaceRoot: "C:/A", conversationId: "A" });
    useAppStore.getState().setRightPanelExpanded(true);
    const selected = useAppStore.getState().gitReviewRequest;
    useAppStore.getState().snapshotWorkbenchState("A");
    useAppStore.setState({ conversationId: "B" });
    useAppStore.getState().restoreWorkbenchState("B");
    expect(useAppStore.getState().gitReviewRequest).toBeNull();
    expect(useAppStore.getState().rightPanelExpanded).toBe(false);
    useAppStore.setState({ conversationId: "A" });
    useAppStore.getState().restoreWorkbenchState("A");
    expect(useAppStore.getState().gitReviewRequest).toEqual(selected);
    expect(useAppStore.getState()).toMatchObject({ rightStackTab: "diff", rightPanelExpanded: true });
  });

  it("lets a new assistant review replace Git while keeping same-review file changes intact", () => {
    const review: DiffReviewState = { requestId: "turn-A", conversationId: "A", diff: "", files: [], fileDecisions: {}, status: "viewing", mode: "view" };
    useAppStore.getState().setDiffReviewState(review);
    useAppStore.getState().openGitReview({ path: "main.ts", section: "working", workspaceRoot: "C:/A", conversationId: "A" });
    const git = useAppStore.getState().gitReviewRequest;
    useAppStore.getState().setDiffReviewState({ ...review, selectedPath: "main.ts" });
    expect(useAppStore.getState().gitReviewRequest).toEqual(git);
    useAppStore.getState().setDiffReviewState({ ...review, requestId: "turn-B" });
    expect(useAppStore.getState().gitReviewRequest).toBeNull();
  });

  it("reveals a review opened from settings or a maximized editor", () => {
    useAppStore.setState({ appMode: "chat", settingsOpen: true, skillsMarketplaceOpen: false });
    useAppStore.getState().openGitReview({ path: "src/main.ts", section: "staged", workspaceRoot: "C:/A", conversationId: "A" });
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, appMode: "code", rightPanelOpen: true, rightStackTab: "diff" });
    expect(useAppStore.getState().gitReviewRequest).toMatchObject({ path: "src/main.ts", section: "staged", workspaceRoot: "C:/A", conversationId: "A" });

    useAppStore.setState({ appMode: "cowork", panelSlots: [{ id: "editor", kind: "editor", focused: true, maximized: true }] });
    useAppStore.getState().openGitReview({ path: "src/main.ts", section: "working", workspaceRoot: "C:/A", conversationId: "A" });
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(useAppStore.getState().panelSlots.some((slot) => slot.maximized)).toBe(false);
    expect(useAppStore.getState().gitReviewRequest?.section).toBe("working");
  });

  it("reveals source and conversation focus without losing editor buffers", () => {
    useAppStore.setState({ editorTabs: [{ id: "buffer", path: "main.ts", content: "draft", original: "disk", loading: false }] });
    useAppStore.getState().setRightPanelExpanded(true);
    useAppStore.getState().openEditorFile("main.ts", undefined, { line: 15, exact: true });
    expect(useAppStore.getState().rightPanelExpanded).toBe(false);
    expect(useAppStore.getState().editorOpenRequests.at(-1)?.line).toBe(15);
    useAppStore.getState().setRightPanelExpanded(true);
    useAppStore.getState().focusPanel("chat");
    expect(useAppStore.getState().rightPanelExpanded).toBe(false);
    expect(useAppStore.getState().editorTabs[0].content).toBe("draft");
  });

  it("reveals the terminal from an expanded side surface or maximized editor", () => {
    useAppStore.setState({ appMode: "code", rightPanelExpanded: true, dockCollapsed: false,
      panelSlots: [{ id: "chat", kind: "chat" }, { id: "editor", kind: "editor", focused: true, maximized: true }] });
    useAppStore.getState().openBottomTab("terminal");
    expect(useAppStore.getState()).toMatchObject({ rightPanelExpanded: false, dockCollapsed: false, activeBottomTab: "terminal" });
    expect(useAppStore.getState().panelSlots.some((slot) => slot.maximized)).toBe(false);
  });

  it("opens content search in the editor while leaving the global sidebar and buffers intact", () => {
    useAppStore.setState({ appMode: "cowork", leftSidebarWidth: 0, editorExplorerOpen: false,
      rightPanelExpanded: true, settingsOpen: true, skillsMarketplaceOpen: true,
      panelSlots: [{ id: "chat", kind: "chat", focused: true, maximized: true }],
      editorTabs: [{ id: "draft", path: "main.ts", content: "local draft", original: "disk", loading: false }] });

    useAppStore.getState().openWorkspaceSearch();

    expect(useAppStore.getState()).toMatchObject({ appMode: "code", leftSidebarWidth: 0,
      editorExplorerOpen: true, workspaceSearchOpen: true, rightPanelExpanded: false,
      settingsOpen: false, skillsMarketplaceOpen: false });
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("editor");
    expect(useAppStore.getState().panelSlots.some((slot) => slot.maximized)).toBe(false);
    expect(useAppStore.getState().editorTabs[0].content).toBe("local draft");
    useAppStore.getState().closeWorkspaceSearch();
    expect(useAppStore.getState()).toMatchObject({ editorExplorerOpen: true, workspaceSearchOpen: false });
  });

  it("routes folder links to the visible editor tree without replacing queued owner-scoped paths", () => {
    const previous = { id: "earlier", path: "docs", kind: "folder" as const, workspaceRoot: "C:/previous" };
    const path = "C:/A/下载/打开《项目内容概览.md》";
    useAppStore.setState({ appMode: "cowork", leftSidebarWidth: 280, editorExplorerOpen: false, workspaceSearchOpen: true,
      rightPanelExpanded: true, settingsOpen: true, skillsMarketplaceOpen: true,
      panelSlots: [{ id: "chat", kind: "chat", focused: true }, { id: "editor", kind: "editor", focused: false }],
      fileTreeRevealRequests: [previous] });

    useAppStore.getState().requestFileTreeReveal(path, "folder");

    expect(useAppStore.getState()).toMatchObject({ appMode: "code", leftSidebarWidth: 280,
      editorExplorerOpen: true, workspaceSearchOpen: false, rightPanelExpanded: false,
      settingsOpen: false, skillsMarketplaceOpen: false });
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.id).toBe("editor");
    expect(useAppStore.getState().fileTreeRevealRequests).toEqual([
      previous, { id: expect.any(String), path, kind: "folder", workspaceRoot: "C:/A" },
    ]);
  });

  it("retains the empty editor slot and explorer state across chat and code mode switches", () => {
    useAppStore.setState({ appMode: "code", editorTabs: [], editorExplorerOpen: false, workspaceSearchOpen: true });
    const slots = useAppStore.getState().panelSlots;
    useAppStore.getState().setAppMode("cowork");
    expect(useAppStore.getState().panelSlots).toBe(slots);
    useAppStore.getState().setAppMode("code");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.id).toBe("editor");
    expect(useAppStore.getState()).toMatchObject({ editorExplorerOpen: false, workspaceSearchOpen: true, editorTabs: [] });
  });
});
