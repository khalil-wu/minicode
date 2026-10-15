/* @vitest-environment jsdom */

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useDesktopEvents } from "./useDesktopEvents";

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
    useAppStore.setState({
      ...originalActions,
      settingsOpen: false,
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
