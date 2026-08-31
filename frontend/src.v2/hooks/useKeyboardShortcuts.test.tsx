/* @vitest-environment jsdom */

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

import { useKeyboardShortcuts } from "./useKeyboardShortcuts";
import { useAppStore } from "../stores";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

const ShortcutHarness = () => {
  useKeyboardShortcuts();
  return null;
};

describe("useKeyboardShortcuts modal routing", () => {
  beforeEach(() => {
    useAppStore.setState({
      commandPaletteOpen: false,
      settingsOpen: true,
      shortcutsHelpOpen: true,
      quickOpenVisible: false,
      skillsMarketplaceOpen: true,
      liveArtifactsOpen: true,
      conversations: [],
      shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
    });
  });

  afterEach(() => {
    cleanup();
    document.body.innerHTML = "";
  });

  it("opens Quick Open through the shared modal toggle so other regular modals close", () => {
    render(<ShortcutHarness />);

    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "p",
      ctrlKey: true,
      bubbles: true,
    }));

    const state = useAppStore.getState();
    expect(state.quickOpenVisible).toBe(true);
    expect(state.commandPaletteOpen).toBe(false);
    expect(state.settingsOpen).toBe(false);
    expect(state.shortcutsHelpOpen).toBe(false);
    expect(state.skillsMarketplaceOpen).toBe(false);
    expect(state.liveArtifactsOpen).toBe(false);
  });

  it("opens General settings from Shift+E without closing an open settings dialog", async () => {
    useAppStore.setState({ settingsTab: "provider" });
    render(<ShortcutHarness />);

    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "E",
      ctrlKey: true,
      shiftKey: true,
      bubbles: true,
    }));

    expect(useAppStore.getState().settingsOpen).toBe(true);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("general"));
  });

  it("opens settings while the composer textarea has focus", () => {
    useAppStore.setState({ settingsOpen: false });
    render(
      <>
        <textarea aria-label="Composer" />
        <ShortcutHarness />
      </>,
    );
    const composer = document.querySelector("textarea");
    composer?.focus();

    composer?.dispatchEvent(new KeyboardEvent("keydown", {
      key: ",",
      ctrlKey: true,
      bubbles: true,
    }));

    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("opens the command palette for an uppercase browser key event", () => {
    useAppStore.setState({ commandPaletteOpen: false });
    render(<ShortcutHarness />);

    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "K",
      ctrlKey: true,
      bubbles: true,
    }));

    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
  });

  it("does not let application shortcuts pass through a modal input", () => {
    const createConversation = vi.fn();
    useAppStore.setState({ createConversation, appMode: "chat" });
    render(
      <>
        <div role="dialog"><input aria-label="API key" /></div>
        <ShortcutHarness />
      </>,
    );
    const input = document.querySelector("input");
    input?.focus();

    input?.dispatchEvent(new KeyboardEvent("keydown", {
      key: "n",
      ctrlKey: true,
      bubbles: true,
    }));

    expect(createConversation).not.toHaveBeenCalled();
  });

  it("allows top-level modal routing shortcuts through a modal input", () => {
    useAppStore.setState({ commandPaletteOpen: true, settingsOpen: false });
    render(
      <>
        <div role="dialog"><input aria-label="Command search" /></div>
        <ShortcutHarness />
      </>,
    );
    const input = document.querySelector("input");
    input?.focus();

    input?.dispatchEvent(new KeyboardEvent("keydown", {
      key: ",",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    }));

    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  });

  it("closes Agent Editor when a different modal opens", () => {
    useAppStore.setState({ settingsOpen: false, agentEditorOpen: true });
    render(<ShortcutHarness />);

    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: ",",
      ctrlKey: true,
      bubbles: true,
    }));

    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().agentEditorOpen).toBe(false);
  });

  it.each(["r", "R"])("opens prompt history for Ctrl+%s", (key) => {
    const onHistorySearch = vi.fn();
    window.addEventListener("composer:history-search", onHistorySearch);
    render(<ShortcutHarness />);
    const event = new KeyboardEvent("keydown", {
      key,
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });

    window.dispatchEvent(event);

    expect(onHistorySearch).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
    window.removeEventListener("composer:history-search", onHistorySearch);
  });

  it("honors an edited shortcut and stops using its old binding", () => {
    useAppStore.setState({
      commandPaletteOpen: false,
      shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS, commandPalette: "Mod+Shift+K" },
    });
    render(<ShortcutHarness />);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "K", ctrlKey: true, shiftKey: true, bubbles: true }));
    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
  });
});
