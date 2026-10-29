/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ send: vi.fn(), reconnect: vi.fn() }) }));
vi.mock("./SidebarLeft", () => ({ SidebarLeft: () => null }));
vi.mock("./NavigationRail", () => ({ NavigationRail: () => null }));
vi.mock("./BottomDock", () => ({ BottomDock: () => null }));
vi.mock("../chat/MessageList", () => ({ MessageList: () => <div>Retained transcript</div> }));
vi.mock("../composer/Composer", () => ({ Composer: () => {
  const draft = useAppStore(state => state.draft);
  return <textarea aria-label="Summary test composer" value={draft}
    onChange={event => useAppStore.getState().setDraft(event.target.value)} />;
} }));
vi.mock("./tabs/SubagentsTab", () => ({ SubagentsTab: () => <button>Agent work record</button> }));
vi.mock("../panels/PreviewPanel", () => ({ PreviewPanel: () => <button>Preview contents</button> }));

import { useAppStore } from "../stores";
import { WorkbenchShell } from "./WorkbenchShell";
import { closeAutomations } from "../lib/automations-navigation";

type Observed = { callback: ResizeObserverCallback; targets: Set<Element> };
let observers: Observed[];
let paneWidth: number;
const resize = (width: number) => {
  paneWidth = width;
  act(() => {
    for (const observer of observers) {
      const entries = [...observer.targets].map(target => ({ target,
        contentRect: new DOMRect(0, 0, target.classList.contains("workbench-stage") ? 1400 : width, 800),
      } as ResizeObserverEntry));
      if (entries.length) observer.callback(entries, {} as ResizeObserver);
    }
  });
};
const summary = () => screen.queryByLabelText("工作区上下文摘要");
const toggle = () => screen.getByRole("button", { name: "切换摘要" });

beforeEach(() => {
  closeAutomations();
  observers = [];
  paneWidth = 1200;
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1800 });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function () { return paneWidth; });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return new DOMRect(0, 0, this.classList.contains("workbench-stage") ? 1400 : paneWidth, 800);
  });
  vi.stubGlobal("ResizeObserver", class {
    observed: Observed;
    constructor(callback: ResizeObserverCallback) { this.observed = { callback, targets: new Set() }; observers.push(this.observed); }
    observe(target: Element) { this.observed.targets.add(target); }
    unobserve(target: Element) { this.observed.targets.delete(target); }
    disconnect() { this.observed.targets.clear(); }
  });
  useAppStore.setState({ appMode: "cowork", settingsOpen: false, skillsMarketplaceOpen: false, projectsOpen: false,
    conversationId: "summary-shell", pendingConversationSwitchId: null, conversationHydration: {},
    conversations: [{ id: "summary-shell", title: "Summary shell", updatedAt: "2026-10-24T00:00:00Z" }],
    workingDirectory: "C:/summary-workspace", messages: [], subagents: [], backgroundTasks: [],
    rightPanelOpen: false, rightPanelExpanded: false, rightStackTab: "subagents", sideChatOpen: false,
    rightSidebarWidth: 420, rightSidebarWidthRatio: null, leftSidebarWidth: 0, dockCollapsed: true,
    panelSlots: [{ id: "main-chat", kind: "chat", focused: true }], editorTabs: [], workspaceFileTabs: [],
    contextCardCollapsed: true, contextCardPinned: false, draft: "Retain the unsent draft", isConnected: false,
    compactPanel: null,
    activeEditorOpenRequestId: null, diffReview: null,
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe("thread summary through WorkbenchShell, MainSlots and ChatPane", () => {
  it("retains the pinned choice while the summary switches between inline and popover beside the right sidebar", async () => {
    const { container } = render(<WorkbenchShell />);
    const composer = screen.getByRole("textbox", { name: "Summary test composer" });
    fireEvent.click(toggle());
    expect(summary()?.getAttribute("data-display-mode")).toBe("shift");
    expect(toggle().getAttribute("aria-pressed")).toBe("true");
    act(() => useAppStore.getState().setRightStackTab("subagents"));
    expect(summary()).toBeNull();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
    expect(useAppStore.getState().contextCardPinned).toBe(true);
    resize(700);
    fireEvent.click(toggle());
    const card = summary();
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    expect(summary()?.getAttribute("data-display-mode")).toBe("overlay");
    const right = await screen.findByRole("button", { name: "Agent work record" });
    fireEvent.pointerDown(right);
    fireEvent.click(right);
    act(() => right.focus());
    expect(document.activeElement).toBe(right);
    expect(summary()).toBe(card);
    expect(useAppStore.getState()).toMatchObject({ contextCardPinned: true, contextCardCollapsed: false,
      rightPanelOpen: true, rightSidebarWidth: 420, rightSidebarWidthRatio: null });
    resize(1600);
    expect(summary()?.getAttribute("data-display-mode")).toBe("gutter");
    expect(summary()?.getAttribute("data-surface")).toBe("inline");
    resize(1096);
    expect(summary()?.getAttribute("data-display-mode")).toBe("shift");
    expect(summary()?.getAttribute("data-surface")).toBe("inline");
    resize(1095);
    expect(summary()?.getAttribute("data-display-mode")).toBe("overlay");
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    expect(toggle().getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".chat-pane-layout")?.contains(card)).toBe(true);
    expect(screen.getByRole("textbox", { name: "Summary test composer" })).toBe(composer);
    expect((composer as HTMLTextAreaElement).value).toBe("Retain the unsent draft");
    act(() => composer.focus());
    expect(document.activeElement).toBe(composer);
    fireEvent.click(toggle());
    expect(summary()).toBeNull();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
    expect(useAppStore.getState()).toMatchObject({ contextCardPinned: true, contextCardCollapsed: true,
      rightPanelOpen: true, rightStackTab: "subagents", rightSidebarWidth: 420 });
    expect(localStorage.getItem("minicode.summary-pinned")).toBe("1");
  });

  it("keeps a temporary popover beside another panel without changing the unpinned choice", async () => {
    paneWidth = 700;
    render(<WorkbenchShell />);
    fireEvent.click(toggle());
    expect(summary()?.getAttribute("role")).toBe("dialog");
    expect(useAppStore.getState().contextCardPinned).toBe(false);
    act(() => useAppStore.getState().setRightStackTab("subagents"));
    expect(summary()).toBeNull();
    fireEvent.click(toggle());
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    const right = await screen.findByRole("button", { name: "Agent work record" });
    fireEvent.pointerDown(right);
    act(() => right.focus());
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    expect(useAppStore.getState().contextCardPinned).toBe(false);
    act(() => useAppStore.getState().toggleRightPanel());
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    const composer = screen.getByRole("textbox", { name: "Summary test composer" });
    fireEvent.pointerDown(composer);
    expect(summary()).toBeNull();
    act(() => useAppStore.getState().setRightStackTab("subagents"));
    expect(summary()).toBeNull();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
  });

  it("hides the summary on each compact drawer reveal, including reopening the same selected tab", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    paneWidth = 700;
    useAppStore.setState({ subagents: [{ id: "summary-audit", status: "done", role: "reviewer", objective: "Layout audit" }] });
    render(<WorkbenchShell />);
    fireEvent.click(toggle());
    const openAgents = screen.getByRole("button", { name: "子智能体", exact: true });
    fireEvent.pointerDown(openAgents);
    fireEvent.click(openAgents);
    const right = await screen.findByRole("button", { name: "Agent work record" });
    fireEvent.pointerDown(right);
    act(() => right.focus());
    expect(document.activeElement).toBe(right);
    expect(summary()).toBeNull();
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(useAppStore.getState()).toMatchObject({ contextCardCollapsed: true, contextCardPinned: false, rightPanelOpen: true });
    fireEvent.click(screen.getByRole("button", { name: "关闭右侧面板" }));
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    fireEvent.click(toggle());
    expect(summary()?.getAttribute("data-surface")).toBe("popover");
    fireEvent.click(screen.getByRole("button", { name: "子智能体", exact: true }));
    expect(summary()).toBeNull();
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(useAppStore.getState()).toMatchObject({ compactPanel: "right", rightPanelOpen: true,
      rightStackTab: "subagents", contextCardCollapsed: true, contextCardPinned: false });
  });
});
