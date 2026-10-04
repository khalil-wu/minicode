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
});
