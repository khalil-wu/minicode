/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useDesktopEvents } from "./useDesktopEvents";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../chat/openWebInBrowser";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../desktop/runtime", async load => ({
  ...await load<Record<string, unknown>>(),
  isDesktop: () => true,
  desktop: () => null,
  onEmbeddedBrowserEvent: () => () => {},
}));

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(async () => null),
}));

const Harness = () => {
  useDesktopEvents();
  return null;
};

const originalActions = {
  createConversation: useAppStore.getState().createConversation,
  requestConversationSwitch: useAppStore.getState().requestConversationSwitch,
};

describe("useDesktopEvents open actions", () => {
  beforeEach(() => {
    __resetOpenWebInBrowserForTests();
    useAppStore.setState({
      ...originalActions,
      shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
      settingsOpen: false,
      projectsOpen: false,
      projectEditTarget: null,
      shortcutsHelpOpen: false,
      commandPaletteOpen: false,
      quickOpenVisible: false,
      editorExplorerOpen: true,
      dockCollapsed: true,
      activeBottomTab: "git",
      defaultTerminalLocation: "bottom",
      skillsMarketplaceOpen: false,
      skillsMarketplaceTab: "skills",
      leftSidebarWidth: 320,
      appMode: "cowork",
      workingDirectory: "",
      conversations: [],
      conversationId: "native-current",
      pendingConversationSwitchId: null,
      messages: [{ id: "native-message", role: "user", content: "Current saved message", timestamp: 1, artifacts: [] }],
      draft: "Current unsent 中文",
      attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
      conversationWorkbenchStates: {},
      isConnected: false, isStreaming: false,
      rightPanelOpen: false, rightPanelExpanded: false, rightStackTab: "tasks", rightStackTabLocked: false,
    });
  });

  afterEach(() => {
    cleanup();
    window.__MINICODE_RUNTIME__ = undefined;
    vi.clearAllMocks();
    useAppStore.setState(originalActions);
  });

  it("keeps open-settings as an open action instead of a toggle", () => {
    render(<Harness />);

    fireEvent(window, new Event("open-settings"));
    expect(useAppStore.getState().settingsOpen).toBe(true);

    fireEvent(window, new Event("open-settings"));
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("keeps open-extensions-marketplace as an open action instead of a toggle", () => {
    const original = useAppStore.getState();
    render(<Harness />);

    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");

    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");
    expect(useAppStore.getState().conversationId).toBe(original.conversationId);
    expect(useAppStore.getState().draft).toBe(original.draft);
    expect(useAppStore.getState().messages).toBe(original.messages);
  });

  it.each([
    { appMode: "code" as const, workingDirectory: "C:\\CurrentProject", bindWorkspace: true },
    { appMode: "code" as const, workingDirectory: "", bindWorkspace: false },
    { appMode: "cowork" as const, workingDirectory: "C:\\CurrentProject", bindWorkspace: false },
  ])("native new-conversation uses the shell policy for $appMode with workspace=$workingDirectory", ({ appMode, workingDirectory, bindWorkspace }) => {
    const create = vi.fn(async () => false);
    useAppStore.setState({ appMode, workingDirectory, createConversation: create });
    const original = useAppStore.getState();
    const focus = vi.fn();
    window.addEventListener("composer:focus", focus);
    try {
      render(<Harness />);
      fireEvent(window, new Event("new-conversation"));
      expect(create).toHaveBeenCalledExactlyOnceWith({ appMode: "cowork", bindWorkspace });
      expect(focus).not.toHaveBeenCalled();
      expect(useAppStore.getState().conversationId).toBe(original.conversationId);
      expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
      expect(useAppStore.getState().workingDirectory).toBe(workingDirectory);
      expect(useAppStore.getState().draft).toBe(original.draft);
      expect(useAppStore.getState().messages).toBe(original.messages);
    } finally {
      window.removeEventListener("composer:focus", focus);
    }
  });

  it("native quick-chat stays unbound to the open code project and focuses the composer once", () => {
    const create = vi.fn(async () => false);
    useAppStore.setState({ appMode: "code", workingDirectory: "C:\\CurrentProject", createConversation: create });
    const original = useAppStore.getState();
    const focus = vi.fn();
    window.addEventListener("composer:focus", focus);
    try {
      render(<Harness />);
      fireEvent(window, new Event("quick-chat"));
      expect(create).toHaveBeenCalledExactlyOnceWith({ appMode: "cowork", bindWorkspace: false });
      expect(focus).toHaveBeenCalledTimes(1);
      expect(useAppStore.getState().conversationId).toBe(original.conversationId);
      expect(useAppStore.getState().draft).toBe(original.draft);
      expect(useAppStore.getState().messages).toBe(original.messages);
      expect(useAppStore.getState().workingDirectory).toBe(original.workingDirectory);
    } finally {
      window.removeEventListener("composer:focus", focus);
    }
  });

  it("native context opens the shared tasks surface from another tab and then toggles it", () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser", appMode: "code", workingDirectory: "C:\\CurrentProject" });
    const original = useAppStore.getState();
    render(<Harness />);
    fireEvent(window, new Event("toggle-context"));
    expect(useAppStore.getState().rightStackTab).toBe("tasks");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    fireEvent(window, new Event("toggle-context"));
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    fireEvent(window, new Event("toggle-context"));
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(useAppStore.getState().rightStackTab).toBe("tasks");
    expect(useAppStore.getState().appMode).toBe(original.appMode);
    expect(useAppStore.getState().conversationId).toBe(original.conversationId);
    expect(useAppStore.getState().workingDirectory).toBe(original.workingDirectory);
    expect(useAppStore.getState().draft).toBe(original.draft);
    expect(useAppStore.getState().messages).toBe(original.messages);
  });

  it("native plugin entry selects plugins even when the skills marketplace was already open", () => {
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills" });
    render(<Harness />);
    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");
  });

  it("opens the actual new-project editor and folder picker without replacing the chat draft", () => {
    useAppStore.setState({ settingsOpen: true, workingDirectory: "C:/CurrentProject" });
    const original = useAppStore.getState();
    render(<Harness />);
    fireEvent(window, new Event("new-project"));
    expect(useAppStore.getState()).toMatchObject({ projectsOpen: false, projectEditTarget: "create", settingsOpen: false,
      conversationId: original.conversationId, draft: original.draft, workingDirectory: original.workingDirectory });
    fireEvent(window, new Event("open-import-modal"));
    expect(openWorkspaceFolder).toHaveBeenCalledOnce();
  });

  it.each([
    { appMode: "cowork", defaultTerminalLocation: "right" },
    { appMode: "code", defaultTerminalLocation: "bottom" },
  ] as const)("toggles the $defaultTerminalLocation terminal and $appMode file consumer", ({ appMode, defaultTerminalLocation }) => {
    useAppStore.setState({ appMode, defaultTerminalLocation });
    render(<Harness />);
    fireEvent(window, new Event("toggle-terminal"));
    expect(useAppStore.getState()).toMatchObject(defaultTerminalLocation === "bottom"
      ? { appMode, activeBottomTab: "terminal", dockCollapsed: false }
      : { appMode, rightStackTab: "terminal", rightPanelOpen: true, dockCollapsed: true });
    fireEvent(window, new Event("toggle-terminal"));
    expect(defaultTerminalLocation === "bottom" ? useAppStore.getState().dockCollapsed : !useAppStore.getState().rightPanelOpen).toBe(true);
    fireEvent(window, new Event("toggle-file-tree"));
    expect(useAppStore.getState()).toMatchObject(appMode === "code"
      ? { editorExplorerOpen: false } : { rightStackTab: "files", rightPanelOpen: true });
    expect(useAppStore.getState().draft).toBe("Current unsent 中文");
  });

  it.each(["chat", "cowork", "code"] as const)("native Changes opens the real diff in %s without replacing its chat or draft", appMode => {
    useAppStore.setState({ appMode, workingDirectory: "C:/CurrentProject", rightPanelOpen: true, rightStackTab: "browser" });
    const original = useAppStore.getState();
    render(<Harness />);
    fireEvent(window, new Event("toggle-side-panel"));
    expect(useAppStore.getState()).toMatchObject({ appMode, rightPanelOpen: true, rightStackTab: "diff", conversationId: original.conversationId, workingDirectory: original.workingDirectory, draft: original.draft });
    expect(useAppStore.getState().messages).toBe(original.messages);
    expect(useAppStore.getState().attachments).toBe(original.attachments);
    fireEvent(window, new Event("toggle-side-panel"));
    expect(useAppStore.getState()).toMatchObject({ appMode, rightPanelOpen: false, rightStackTab: "diff", draft: original.draft });
    fireEvent(window, new Event("toggle-side-panel"));
    expect(useAppStore.getState()).toMatchObject({ appMode, rightPanelOpen: true, rightStackTab: "diff", draft: original.draft });
  });

  it("reveals Changes from a maximized editor without closing its unsaved editor or changing the chat", () => {
    useAppStore.setState({ appMode: "code", panelSlots: [{ id: "editor", kind: "editor", focused: true, maximized: true, label: "Unsaved editor" }], rightPanelOpen: true, rightStackTab: "diff" });
    const original = useAppStore.getState();
    render(<Harness />);
    fireEvent(window, new Event("toggle-side-panel"));
    expect(useAppStore.getState()).toMatchObject({ appMode: "code", rightPanelOpen: true, rightStackTab: "diff", conversationId: original.conversationId, draft: original.draft });
    expect(useAppStore.getState().panelSlots).toEqual([expect.objectContaining({ id: "editor", kind: "editor", maximized: false, label: "Unsaved editor" })]);
  });

  it("toggles the pinned context summary and the bottom pane in both work modes through their real owners", () => {
    useAppStore.setState({ contextCardCollapsed: false });
    render(<Harness />);
    fireEvent(window, new Event("toggle-pinned-summary"));
    expect(useAppStore.getState().contextCardCollapsed).toBe(true);
    fireEvent(window, new Event("toggle-pinned-summary"));
    expect(useAppStore.getState().contextCardCollapsed).toBe(false);
    fireEvent(window, new Event("toggle-side-panel"));
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    fireEvent(window, new Event("toggle-pinned-summary"));
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: false, contextCardCollapsed: false });
    fireEvent(window, new Event("toggle-bottom-panel"));
    expect(useAppStore.getState()).toMatchObject({ appMode: "cowork", dockCollapsed: false, activeBottomTab: "terminal" });
    fireEvent(window, new Event("toggle-bottom-panel"));
    expect(useAppStore.getState().dockCollapsed).toBe(true);
    act(() => useAppStore.setState({ appMode: "code" }));
    fireEvent(window, new Event("toggle-bottom-panel"));
    expect(useAppStore.getState().dockCollapsed).toBe(false);
    fireEvent(window, new Event("toggle-bottom-panel"));
    expect(useAppStore.getState().dockCollapsed).toBe(true);
  });

  it("publishes actual browser new-tab and deferred address-focus requests for the same owner", () => {
    render(<Harness />);
    fireEvent(window, new Event("open-browser-tab"));
    fireEvent(window, new Event("focus-browser-address-bar"));
    const requests: unknown[] = [];
    const unsubscribe = subscribeBrowserRequests(request => requests.push(request));
    unsubscribe();
    expect(requests).toEqual([
      expect.objectContaining({ kind: "new", conversationId: "native-current" }),
      expect.objectContaining({ kind: "focus-address", conversationId: "native-current" }),
    ]);
    expect(useAppStore.getState()).toMatchObject({ rightStackTab: "browser", rightPanelOpen: true, draft: "Current unsent 中文" });
  });

  it.each(["cowork", "code"] as const)("routes native Find to only the visible %s consumer", appMode => {
    const chat = vi.fn(), editor = vi.fn(), chatNext = vi.fn(), editorNext = vi.fn();
    useAppStore.setState({ appMode, panelSlots: [{ id: "editor", kind: "editor", label: "Editor", focused: true }] });
    window.addEventListener("chat:request-search", chat); window.addEventListener("editor:find", editor);
    window.addEventListener("chat:find-next", chatNext); window.addEventListener("editor:find-next", editorNext);
    try {
      render(<Harness />);
      fireEvent(window, new Event("find-in-thread"));
      fireEvent(window, new Event("find-next-in-thread"));
      expect(chat).toHaveBeenCalledTimes(appMode === "cowork" ? 1 : 0);
      expect(chatNext).toHaveBeenCalledTimes(appMode === "cowork" ? 1 : 0);
      expect(editor).toHaveBeenCalledTimes(appMode === "code" ? 1 : 0);
      expect(editorNext).toHaveBeenCalledTimes(appMode === "code" ? 1 : 0);
    } finally {
      window.removeEventListener("chat:request-search", chat); window.removeEventListener("editor:find", editor);
      window.removeEventListener("chat:find-next", chatNext); window.removeEventListener("editor:find-next", editorNext);
    }
  });

  it("opens the existing command, file and shortcut surfaces and navigates real non-archived chats", () => {
    const switchChat = vi.fn(async () => {});
    useAppStore.setState({ requestConversationSwitch: switchChat, conversations: [
      { id: "native-current", title: "Current", updatedAt: "2026-10-19" },
      { id: "archived", title: "Hidden", archived: true, updatedAt: "2026-10-19" },
      { id: "next", title: "Next", updatedAt: "2026-10-18" },
    ] });
    render(<Harness />);
    fireEvent(window, new Event("open-command-menu"));
    fireEvent(window, new Event("search-chats"));
    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
    fireEvent(window, new Event("search-files"));
    expect(useAppStore.getState().quickOpenVisible).toBe(true);
    fireEvent(window, new Event("open-shortcuts"));
    expect(useAppStore.getState().shortcutsHelpOpen).toBe(true);
    fireEvent(window, new Event("next-thread"));
    expect(switchChat).toHaveBeenCalledExactlyOnceWith("next");
    act(() => useAppStore.setState({ conversationId: "next" }));
    fireEvent(window, new Event("previous-thread"));
    expect(switchChat).toHaveBeenLastCalledWith("native-current");
  });

  it("reports actual menu availability and editor focus to the native template", async () => {
    const setState = vi.fn(async () => {});
    window.__MINICODE_RUNTIME__ = { desktop: { menu: { setState },
      windowControls: { getState: async () => ({ isFocused: false }), onStateChanged: () => () => {} },
      onDeepLink: () => () => {}, hideNotifications: vi.fn(async () => 0),
    } } as never;
    render(<><Harness /><div className="monaco-editor"><textarea aria-label="native editor" /></div></>);
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ terminalEnabled: false, bottomPanelToggleEnabled: true, newProjectEnabled: true, editorFocused: false,
      shortcutRecording: false, accelerators: expect.objectContaining({ terminal: "CommandOrControl+`", zoomIn: "CommandOrControl+Shift+=" }),
      acceleratorBindings: expect.objectContaining({ zoomIn: ["CommandOrControl+Shift+=", "CommandOrControl+="] }) }));
    act(() => useAppStore.setState({ appMode: "code", isConnected: true }));
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ terminalEnabled: true, bottomPanelToggleEnabled: true }));
    fireEvent.focusIn(document.querySelector("textarea")!);
    (document.querySelector("textarea") as HTMLTextAreaElement).focus();
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ editorFocused: true }));
    act(() => useAppStore.setState({ pendingConversationSwitchId: "another" }));
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ terminalEnabled: false, newProjectEnabled: false }));
    await act(async () => { (document.querySelector("textarea") as HTMLTextAreaElement).blur(); });
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ editorFocused: false }));
    act(() => useAppStore.getState().setShortcutBinding("terminal", "Mod+Shift+Y"));
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ accelerators: expect.objectContaining({ terminal: "CommandOrControl+Shift+Y" }), shortcutOverrides: expect.arrayContaining(["terminal"]) }));
    const recorder = document.createElement("button");
    recorder.setAttribute("data-shortcut-recording", "true");
    await act(async () => { document.body.appendChild(recorder); });
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ shortcutRecording: true }));
    await act(async () => { recorder.remove(); });
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ shortcutRecording: false }));
    act(() => useAppStore.getState().setShortcutBinding("terminal", ""));
    expect(setState).toHaveBeenLastCalledWith(expect.objectContaining({ accelerators: expect.objectContaining({ terminal: null }) }));
  });

  it("removes native action listeners when the desktop harness unmounts", () => {
    const create = vi.fn(async () => false);
    useAppStore.setState({ createConversation: create });
    const { unmount } = render(<Harness />);
    unmount();
    fireEvent(window, new Event("new-conversation"));
    fireEvent(window, new Event("quick-chat"));
    fireEvent(window, new Event("toggle-context"));
    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(create).not.toHaveBeenCalled();
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
  });

  it("routes acknowledged conversation deep links", async () => {
    const ackDeepLink = vi.fn(async () => true);
    let onDeepLink: ((payload: { id: string; target: { kind: "conversation"; conversationId: string } }) => void) | undefined;
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        menu: { setState: vi.fn(async () => {}) },
        windowControls: { getState: async () => ({ isMaximized: false, isFocused: false }), onStateChanged: () => () => {} },
        hideNotifications: vi.fn(async () => 0),
        onDeepLink: (callback) => { onDeepLink = callback as typeof onDeepLink; return () => {}; },
        ackDeepLink,
      } as never,
    };
    useAppStore.setState({
      conversations: [{ id: "conv-target", title: "Target", updatedAt: new Date().toISOString() }],
      requestConversationSwitch: vi.fn(async () => {}),
    });
    render(<Harness />);
    await onDeepLink?.({ id: "link-1", target: { kind: "conversation", conversationId: "conv-target" } });
    expect(useAppStore.getState().requestConversationSwitch).toHaveBeenCalledWith("conv-target");
    expect(ackDeepLink).toHaveBeenCalledWith("link-1");
  });
});
