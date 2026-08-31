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

vi.mock("../desktop/runtime", () => ({
  isDesktop: () => true,
}));

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(async () => null),
}));

const Harness = () => {
  useDesktopEvents();
  return null;
};

describe("useDesktopEvents open actions", () => {
  beforeEach(() => {
    useAppStore.setState({
      settingsOpen: false,
      skillsMarketplaceOpen: false,
      leftSidebarWidth: 320,
      appMode: "cowork",
      workingDirectory: "",
      conversations: [],
    });
  });

  afterEach(() => {
    cleanup();
    window.__MINICODE_RUNTIME__ = undefined;
    vi.clearAllMocks();
  });

  it("keeps open-settings as an open action instead of a toggle", () => {
    render(<Harness />);

    fireEvent(window, new Event("open-settings"));
    expect(useAppStore.getState().settingsOpen).toBe(true);

    fireEvent(window, new Event("open-settings"));
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("keeps open-extensions-marketplace as an open action instead of a toggle", () => {
    render(<Harness />);

    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);

    fireEvent(window, new Event("open-extensions-marketplace"));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
  });

  it("routes acknowledged conversation deep links", async () => {
    const ackDeepLink = vi.fn(async () => true);
    let onDeepLink: ((payload: { id: string; target: { kind: "conversation"; conversationId: string } }) => void) | undefined;
    window.__MINICODE_RUNTIME__ = {
      desktop: {
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
