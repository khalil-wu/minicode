// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { MainSlots } from "./MainSlots";
import { SidebarRight } from "./SidebarRight";
import { BottomDock } from "./BottomDock";
import { FooterRow } from "../composer/FooterRow";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "shell-fixture", send: vi.fn(() => true) }) }));
vi.mock("../panels/EditorPanel", () => ({ EditorPanel: () => <textarea aria-label="Editor input" /> }));
vi.mock("../panels/GitPanel", () => ({ GitPanel: () => <div>Git fixture</div> }));
vi.mock("./tabs/ActivityTab", () => ({ ActivityTab: () => <div>Activity fixture</div> }));
vi.mock("../chat/ChatPane", () => ({ ChatPane: () => <><textarea aria-label="Actual composer" data-composer-input />
  <FooterRow sendState="idle" onSend={() => {}} /></> }));
const initial = useAppStore.getState();
let frames: FrameRequestCallback[] = [];
const flushFrames = () => act(() => { for (const frame of frames.splice(0)) frame(0); });
const Harness = () => { useKeyboardShortcuts(); const mode = useAppStore((state) => state.appMode); return <MainSlots mode="tabs" forceChat={mode !== "code"} />; };
const key = (key: string, shiftKey = false) => fireEvent.keyDown(window, { key, ctrlKey: true, shiftKey });
beforeEach(() => {
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A", appMode: "code",
    panelSlots: [{ id: "chat", kind: "chat", focused: false, size: 1 }, { id: "editor", kind: "editor", focused: true, size: 1 }],
    editorTabs: [{ id: "file", path: "main.ts", content: "dirty", original: "base", loading: false }],
    activeTabPath: "main.ts", messages: [], conversations: [], runtimeCapabilities: null,
    currentModel: "model-A", availableModels: ["model-A"], permissionMode: "confirm", shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
    settingsOpen: false, commandPaletteOpen: false, quickOpenVisible: false, shortcutsHelpOpen: false,
    skillsMarketplaceOpen: false, liveArtifactsOpen: false, agentEditorOpen: false, automationsOpen: false,
    rightPanelOpen: true, rightStackTab: "tasks", dockCollapsed: false, activeBottomTab: "budget",
    rightSidebarWidth: 420, dockHeight: 240, sideChatOpen: false }, true);
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 1200, 800));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  frames = [];
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frames.push(callback); return frames.length; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("PointerEvent", class extends MouseEvent {
    pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; }
  });
  const captured = new WeakMap<HTMLElement, number>();
  Object.defineProperties(HTMLElement.prototype, {
    setPointerCapture: { configurable: true, value: function (this: HTMLElement, id: number) { captured.set(this, id); } },
    hasPointerCapture: { configurable: true, value: function (this: HTMLElement, id: number) { return captured.get(this) === id; } },
    releasePointerCapture: { configurable: true, value: function (this: HTMLElement) { captured.delete(this); } },
    scrollIntoView: { configurable: true, value: vi.fn() },
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.style.cursor = ""; document.body.style.userSelect = ""; document.body.classList.remove("layout-dragging"); });

describe("shell resource ownership", () => {
  it.each(["diff", "preview", "tasks", "subagents", "artifacts", "inspector"] as const)("routes legacy %s opens without destroying main files or the dock", (kind) => {
    const before = useAppStore.getState();
    before.addPanel({ id: "legacy", kind });
    const after = useAppStore.getState();
    expect(after.panelSlots).toBe(before.panelSlots);
    expect(after.editorTabs).toBe(before.editorTabs);
    expect(after.dockCollapsed).toBe(false);
    expect(after.rightStackTab).toBe(kind);
    expect(after.rightStackTabLocked).toBe(true);
  });

  it.each(["main", "sidebar", "dock"])("cleans the %s pointer drag on component unmount", (kind) => {
    document.body.style.cursor = "crosshair";
    document.body.style.userSelect = "text";
    const view = render(kind === "main" ? <MainSlots /> : kind === "sidebar" ? <SidebarRight /> : <BottomDock />);
    const label = kind === "main" ? "调整主面板宽度" : kind === "sidebar" ? "调整右侧栏宽度" : "调整底部工具高度";
    const handle = screen.getByRole("separator", { name: label });
    fireEvent.pointerDown(handle, { pointerId: 7, clientX: 100, clientY: 100 });
    expect(document.body.style.userSelect).toBe("none");
    view.unmount();
    expect(document.body.style.cursor).toBe("crosshair");
    expect(document.body.style.userSelect).toBe("text");
    const before = useAppStore.getState();
    fireEvent.pointerMove(window, { pointerId: 7, clientX: 180, clientY: 160 });
    const after = useAppStore.getState();
    expect(after.panelSlots).toBe(before.panelSlots);
    expect(after.rightSidebarWidth).toBe(before.rightSidebarWidth);
    expect(after.dockHeight).toBe(before.dockHeight);
  });

  it("cleans the main divider when pointer capture is lost", () => {
    const view = render(<MainSlots />);
    const handle = view.getByRole("separator", { name: "调整主面板宽度" });
    fireEvent.pointerDown(handle, { pointerId: 8, clientX: 100 });
    handle.dispatchEvent(new Event("lostpointercapture"));
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});

describe("actual code-mode keyboard routes", () => {
  it.each([{ key: "I", role: "listbox", name: "选择模型" }, { key: "M", role: "listbox", name: "权限：询问" }])("opens $name from the hidden chat behind a maximized file", ({ key: shortcut, role, name }) => {
    useAppStore.setState({ panelSlots: [{ id: "chat", kind: "chat", focused: false }, { id: "editor", kind: "editor", focused: true, maximized: true }] });
    render(<Harness />);
    key(shortcut, true);
    flushFrames();
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("chat");
    expect(screen.getByRole(role, { name })).toBeTruthy();
  });

  it("clears and focuses the actual composer while retaining file layout", () => {
    useAppStore.setState({ draft: "clear this" });
    render(<><textarea aria-label="Unrelated field" /><Harness /></>);
    key("l");
    flushFrames();
    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Actual composer" }));
    expect(useAppStore.getState().editorTabs[0].content).toBe("dirty");
  });

  it("toggles the canonical diff surface without adding or removing main slots", () => {
    render(<Harness />);
    key("D", true);
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "diff" });
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
    key("D", true);
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
  });

  it("blocks a workspace mutation while a modal is loading without a dialog DOM", () => {
    const createConversation = vi.fn();
    useAppStore.setState({ settingsOpen: true, createConversation });
    render(<Harness />);
    key("n");
    expect(createConversation).not.toHaveBeenCalled();
  });

  it("does not replay a pending composer menu after the conversation changes", () => {
    render(<Harness />);
    key("I", true);
    act(() => useAppStore.setState({ conversationId: "B" }));
    flushFrames();
    expect(screen.queryByRole("listbox", { name: "选择模型" })).toBeNull();
  });

  it("leaves the marketplace when the user explicitly switches to Code mode", () => {
    useAppStore.setState({ skillsMarketplaceOpen: true, appMode: "cowork", skillsMarketplaceReturnTarget: "settings" });
    render(<Harness />);
    fireEvent.keyDown(window, { key: "2", altKey: true });
    expect(useAppStore.getState()).toMatchObject({ appMode: "code", skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
  });
});
