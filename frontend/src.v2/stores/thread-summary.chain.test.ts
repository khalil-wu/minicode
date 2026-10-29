/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it } from "vitest";
import { useAppStore } from ".";
import { emptyConversationWorkbenchState } from "./ui-slice";
import { __resetOpenWebInBrowserForTests, openNewBrowserTab } from "../chat/openWebInBrowser";
import { threadSummarySurface } from "../chat/thread-summary-layout";

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
  useAppStore.setState({ conversationId: "summary-open-owner", workingDirectory: "C:/summary-workspace",
    rightPanelOpen: false, rightPanelExpanded: false, compactPanel: null, rightStackTab: "tasks",
    draft: "Keep the unsent draft", attachments: [{ id: "keep-upload", name: "reference.png", type: "image/png", size: 12, status: "ready" }],
    workspaceFileTabs: [], panelSlots: [{ id: "main-chat", kind: "chat", focused: true }], conversationWorkbenchStates: {},
  });
  useAppStore.getState().setContextCardPinned(true);
});
afterEach(() => { __resetOpenWebInBrowserForTests(); localStorage.clear(); });

it.each(["toggle", "tab", "legacy panel", "browser", "file"])("temporarily hides the summary for a closed-to-open %s request without changing the pin or authored input", entry => {
  const before = useAppStore.getState();
  if (entry === "toggle") before.toggleRightPanel();
  else if (entry === "tab") before.setRightStackTab("subagents");
  else if (entry === "legacy panel") before.addPanel({ id: "preview-panel", kind: "preview" });
  else if (entry === "browser") openNewBrowserTab();
  else before.openWorkspaceFileTab({ scopeId: "conversation:summary-open-owner", conversationId: "summary-open-owner",
    executionCwd: "C:/summary-workspace", browserRoot: "C:/summary-workspace" }, "readme.md");
  const after = useAppStore.getState();
  expect(after).toMatchObject({ rightPanelOpen: true, contextCardCollapsed: true, contextCardPinned: true });
  expect(after.draft).toBe(before.draft);
  expect(after.attachments).toBe(before.attachments);
  expect(localStorage.getItem("minicode.summary-pinned")).toBe("1");
  expect(threadSummarySurface("gutter", after.contextCardPinned, !after.contextCardCollapsed)).toBeNull();
  after.toggleContextCard(1600);
  expect(useAppStore.getState()).toMatchObject({ contextCardCollapsed: false, contextCardPinned: true });
  for (const tab of ["files", "subagents", "browser"] as const) {
    useAppStore.getState().setRightStackTab(tab);
    expect(useAppStore.getState().contextCardCollapsed).toBe(false);
  }
});

it("hides a restored right panel by default and lets the user reopen the saved pinned summary", () => {
  const before = useAppStore.getState();
  useAppStore.setState({ conversationWorkbenchStates: { "summary-open-owner": { ...emptyConversationWorkbenchState(),
    rightPanelOpen: true, rightStackTab: "subagents", draft: before.draft, attachments: before.attachments } } });
  before.restoreWorkbenchState("summary-open-owner");
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, contextCardCollapsed: true, contextCardPinned: true,
    draft: before.draft, attachments: before.attachments });
  useAppStore.getState().toggleContextCard(1600);
  expect(useAppStore.getState()).toMatchObject({ contextCardCollapsed: false, contextCardPinned: true });
});

it("distinguishes a compact drawer reveal from switching tabs inside an already open drawer", () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
  useAppStore.getState().setRightStackTab("subagents");
  useAppStore.getState().toggleContextCard(700);
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, compactPanel: "right", contextCardCollapsed: false });
  useAppStore.getState().setRightStackTab("files");
  expect(useAppStore.getState().contextCardCollapsed).toBe(false);
  useAppStore.getState().setCompactPanel(null);
  expect(useAppStore.getState().rightPanelOpen).toBe(true);
  useAppStore.getState().setRightStackTab("files");
  expect(useAppStore.getState()).toMatchObject({ compactPanel: "right", contextCardCollapsed: true, contextCardPinned: true });
  useAppStore.getState().toggleContextCard(700);
  useAppStore.getState().setRightStackTab("files");
  expect(useAppStore.getState().contextCardCollapsed).toBe(false);
  useAppStore.getState().setCompactPanel(null);
  useAppStore.getState().toggleRightPanel();
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, compactPanel: "right", contextCardCollapsed: true });
});
