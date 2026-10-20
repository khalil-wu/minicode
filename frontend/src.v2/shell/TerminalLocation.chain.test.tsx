/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import type { AppMode } from "../stores/types";
import type { DesktopPreferences } from "../desktop/runtime";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { useDesktopBehaviorPreferences } from "../hooks/useDesktopBehaviorPreferences";
import { useDesktopEvents } from "../hooks/useDesktopEvents";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<Record<string, unknown>>(), sendClientCommand: vi.fn(() => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const PreferencesHarness = () => { useDesktopBehaviorPreferences(); return null; };
const NativeEventsHarness = () => { useDesktopEvents(); return null; };
const NativeShortcutHarness = () => { useDesktopBehaviorPreferences(); useDesktopEvents(); useKeyboardShortcuts(); return null; };
type BehaviorPreferences = Pick<DesktopPreferences, "defaultTerminalLocation" | "composerPlainTextMode">;

function bindNative(initial: BehaviorPreferences) {
  let value = initial;
  const changes = new Set<(preferences: DesktopPreferences) => void>();
  const get = vi.fn(async () => value);
  const set = vi.fn(async (patch: Partial<BehaviorPreferences>) => {
    value = { ...value, ...patch }; for (const listener of changes) listener(value as DesktopPreferences); return value;
  });
  const execute = vi.fn(async (action: string) => {
    expect(window.__MINICODE_RUNTIME__?.windowInstanceId).toBe("fixture-terminal-window");
    if (action === "terminal") window.dispatchEvent(new Event("toggle-terminal"));
    return true;
  });
  const setMenuState = vi.fn(async () => {});
  const pty = { list: vi.fn(async () => []), snapshot: vi.fn(async () => ({ id: "kept-terminal", conversationId: "fixture-chat", shell: "fixture-shell", cwd: "C:/Fixture", output: "PS> unsent command", isAlive: true })),
    spawn: vi.fn(), kill: vi.fn(), onData: vi.fn(() => () => {}), onExit: vi.fn(() => () => {}) };
  window.__MINICODE_RUNTIME__ = { windowOwnerId: "fixture-terminal-owner", windowInstanceId: "fixture-terminal-window", windowBindingToken: "fixture-binding",
    desktop: { platformInfo: { isDesktop: true, platform: "win32", arch: "x64" },
      preferences: { get, set, onChanged: (listener: (preferences: DesktopPreferences) => void) => { changes.add(listener); return () => changes.delete(listener); } },
      menu: { execute, setState: setMenuState }, pty, embeddedBrowser: { onEvent: () => () => {} },
      windowControls: { getState: async () => ({ isFocused: false, isMaximized: false }), onStateChanged: () => () => {} },
      hideNotifications: vi.fn(async () => 0), onDeepLink: () => () => {}, ackDeepLink: vi.fn(async () => true),
    },
  } as never;
  return { get, set, execute, setMenuState, pty, changes,
    push(next: BehaviorPreferences) { value = next; for (const listener of changes) listener(value as DesktopPreferences); } };
}

function seed(appMode: AppMode, location: "bottom" | "right") {
  useAppStore.setState({ appMode, defaultTerminalLocation: location, composerPlainTextMode: false,
    conversationId: "fixture-chat", workingDirectory: "C:/Fixture", pendingConversationSwitchId: null,
    conversations: [{ id: "fixture-chat", title: "Fixture task", updatedAt: "2026-10-20" }],
    draft: "Keep this unsent 中文 draft", sideChats: {}, messages: [],
    terminalSessions: [{ id: "kept-terminal", conversationId: "fixture-chat", shell: "fixture-shell", cwd: "C:/Fixture", status: "running", terminalMode: "pty" }],
    activeTerminalSessionId: "kept-terminal", terminalSnapshots: { "kept-terminal": { id: "kept-terminal", conversationId: "fixture-chat", shell: "fixture-shell", cwd: "C:/Fixture", output: "PS> unsent command", capturedAt: 1 } },
    panelSlots: [{ id: "main-chat", kind: "chat", focused: true, maximized: false }, { id: "main-editor", kind: "editor", maximized: false }],
    rightPanelOpen: location === "bottom", rightPanelExpanded: false, rightStackTab: location === "bottom" ? "terminal" : "files", rightStackTabLocked: false,
    activeBottomTab: "terminal", dockCollapsed: location === "bottom", dockHeight: 240,
    settingsOpen: false, projectsOpen: false, projectEditTarget: null, shortcutsHelpOpen: false, commandPaletteOpen: false, quickOpenVisible: false,
    skillsMarketplaceOpen: false, liveArtifactsOpen: false, agentEditorOpen: false, isConnected: true, isStreaming: false,
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
  });
  useAppStore.getState().ensureSideChat("hidden-chat", { workspaceRoot: "C:/HiddenFixture", permissionMode: "confirm", parentConversationId: "fixture-chat" });
  useAppStore.getState().setSideChatDraft("hidden-chat", "Keep the hidden workspace draft");
  const state = useAppStore.getState();
  return { sessions: state.terminalSessions, snapshots: state.terminalSnapshots, sideChats: state.sideChats, messages: state.messages };
}

function expectPreserved(original: ReturnType<typeof seed>, appMode: AppMode) {
  const state = useAppStore.getState();
  expect(state).toMatchObject({ appMode, conversationId: "fixture-chat", workingDirectory: "C:/Fixture", draft: "Keep this unsent 中文 draft", activeTerminalSessionId: "kept-terminal" });
  expect(state.terminalSessions).toBe(original.sessions); expect(state.terminalSnapshots).toBe(original.snapshots);
  expect(state.sideChats).toBe(original.sideChats); expect(state.messages).toBe(original.messages);
  expect(state.sideChats["hidden-chat"].draft).toBe("Keep the hidden workspace draft");
}

beforeEach(async () => { await waitComposerDraftRecovery(); window.__MINICODE_RUNTIME__ = undefined; localStorage.removeItem("minicode.default-terminal-location"); localStorage.removeItem("minicode.composer-plain-text"); });
afterEach(() => { cleanup(); window.__MINICODE_RUNTIME__ = undefined; vi.clearAllMocks(); });

for (const appMode of ["chat", "cowork", "code"] as const) for (const location of ["bottom", "right"] as const) {
  it(`opens and genuinely toggles the ${location} terminal in ${appMode} without replacing terminal or composer state`, () => {
    const original = seed(appMode, location);
    useAppStore.getState().openTerminal();
    expect(useAppStore.getState()).toMatchObject(location === "bottom" ? { activeBottomTab: "terminal", dockCollapsed: false, rightPanelOpen: false }
      : { rightStackTab: "terminal", rightPanelOpen: true, dockCollapsed: true });
    expectPreserved(original, appMode);
    useAppStore.getState().toggleTerminal();
    expect(location === "bottom" ? useAppStore.getState().dockCollapsed : !useAppStore.getState().rightPanelOpen).toBe(true);
    useAppStore.getState().toggleTerminal();
    expect(location === "bottom" ? !useAppStore.getState().dockCollapsed : useAppStore.getState().rightPanelOpen).toBe(true);
    expectPreserved(original, appMode);
  });
}

it("reveals a hidden Code terminal before treating the next toggle as close", () => {
  const original = seed("code", "bottom");
  useAppStore.setState({ rightPanelExpanded: true, dockCollapsed: false,
    panelSlots: [{ id: "main-chat", kind: "chat" }, { id: "main-editor", kind: "editor", focused: true, maximized: true }] });
  useAppStore.getState().toggleTerminal();
  expect(useAppStore.getState()).toMatchObject({ rightPanelExpanded: false, dockCollapsed: false, activeBottomTab: "terminal" });
  expect(useAppStore.getState().panelSlots.every(slot => !slot.maximized)).toBe(true);
  useAppStore.getState().toggleTerminal(); expect(useAppStore.getState().dockCollapsed).toBe(true);
  expectPreserved(original, "code");
});

it.each(["chat", "cowork", "code"] as const)("the owned native terminal event uses the same preference in %s and permits the bottom-panel menu", appMode => {
  const original = seed(appMode, "bottom");
  const native = bindNative({ defaultTerminalLocation: "bottom", composerPlainTextMode: false });
  render(<NativeEventsHarness />);
  expect(native.setMenuState).toHaveBeenCalledWith(expect.objectContaining({ terminalEnabled: true, bottomPanelToggleEnabled: true }));
  fireEvent(window, new Event("toggle-terminal")); expect(useAppStore.getState().dockCollapsed).toBe(false);
  fireEvent(window, new Event("toggle-terminal")); expect(useAppStore.getState().dockCollapsed).toBe(true);
  expectPreserved(original, appMode);
  expect(native.pty.spawn).not.toHaveBeenCalled(); expect(native.pty.kill).not.toHaveBeenCalled();
});

it("Ctrl+Backquote passes through the owned native command once and restores the maximized Code view's right terminal", async () => {
  const original = seed("code", "right");
  useAppStore.setState({ rightPanelOpen: true, rightStackTab: "terminal", rightPanelExpanded: true,
    panelSlots: [{ id: "main-chat", kind: "chat" }, { id: "main-editor", kind: "editor", focused: true, maximized: true }] });
  const native = bindNative({ defaultTerminalLocation: "right", composerPlainTextMode: false });
  render(<NativeShortcutHarness />);
  await waitFor(() => expect(native.get).toHaveBeenCalledOnce());
  const press = () => { const event = new KeyboardEvent("keydown", { key: "`", code: "Backquote", ctrlKey: true, bubbles: true, cancelable: true }); fireEvent(window, event); return event; };
  expect(press().defaultPrevented).toBe(true);
  expect(native.execute).toHaveBeenCalledExactlyOnceWith("terminal");
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "terminal", rightPanelExpanded: false, dockCollapsed: true });
  expect(useAppStore.getState().panelSlots.every(slot => !slot.maximized)).toBe(true);
  press(); expect(native.execute).toHaveBeenCalledTimes(2); expect(useAppStore.getState().rightPanelOpen).toBe(false);
  expectPreserved(original, "code");
  expect(native.pty.spawn).not.toHaveBeenCalled(); expect(native.pty.kill).not.toHaveBeenCalled();
});

it("native preference reads and committed pushes project into the actual terminal and plain-text consumers", async () => {
  const original = seed("cowork", "bottom");
  const native = bindNative({ defaultTerminalLocation: "bottom", composerPlainTextMode: false });
  const view = render(<PreferencesHarness />);
  await waitFor(() => expect(native.get).toHaveBeenCalledOnce());
  act(() => native.push({ defaultTerminalLocation: "right", composerPlainTextMode: true }));
  expect(useAppStore.getState()).toMatchObject({ defaultTerminalLocation: "right", composerPlainTextMode: true });
  useAppStore.getState().openTerminal(); expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "terminal" });
  await act(async () => { await useAppStore.getState().setDefaultTerminalLocation("bottom"); });
  expect(native.set).toHaveBeenCalledWith({ defaultTerminalLocation: "bottom" });
  useAppStore.getState().toggleTerminal(); expect(useAppStore.getState()).toMatchObject({ dockCollapsed: false, rightPanelOpen: false });
  expectPreserved(original, "cowork");
  view.unmount(); expect(native.changes.size).toBe(0);
});

it("a late initial native read cannot roll back a newer committed terminal preference", async () => {
  seed("chat", "bottom");
  const native = bindNative({ defaultTerminalLocation: "bottom", composerPlainTextMode: false });
  let finish!: (preferences: BehaviorPreferences) => void;
  native.get.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<PreferencesHarness />);
  act(() => native.push({ defaultTerminalLocation: "right", composerPlainTextMode: true }));
  expect(useAppStore.getState()).toMatchObject({ defaultTerminalLocation: "right", composerPlainTextMode: true });
  await act(async () => { finish({ defaultTerminalLocation: "bottom", composerPlainTextMode: false }); });
  expect(useAppStore.getState()).toMatchObject({ defaultTerminalLocation: "right", composerPlainTextMode: true });
});

it("web storage changes and local preference writes project without any native or OS access", async () => {
  const original = seed("chat", "bottom");
  render(<PreferencesHarness />);
  fireEvent(window, new StorageEvent("storage", { key: "minicode.default-terminal-location", newValue: "right", storageArea: localStorage }));
  fireEvent(window, new StorageEvent("storage", { key: "minicode.composer-plain-text", newValue: "true", storageArea: localStorage }));
  expect(useAppStore.getState()).toMatchObject({ defaultTerminalLocation: "right", composerPlainTextMode: true });
  useAppStore.getState().openTerminal(); expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "terminal" });
  await useAppStore.getState().setDefaultTerminalLocation("bottom");
  expect(localStorage.getItem("minicode.default-terminal-location")).toBe("bottom");
  useAppStore.getState().toggleTerminal(); expect(useAppStore.getState()).toMatchObject({ dockCollapsed: false, rightPanelOpen: false });
  expectPreserved(original, "chat");
  expect(window.__MINICODE_RUNTIME__).toBeUndefined();
});
