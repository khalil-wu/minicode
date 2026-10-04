/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import { ComposerTextarea } from "../composer/ComposerTextarea";

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

  it("leaves IME shortcuts and composition-confirm Escape with their input", () => {
    useAppStore.setState({ settingsOpen: false, shortcutsHelpOpen: false, skillsMarketplaceOpen: false, liveArtifactsOpen: false,
      agentEditorOpen: false, isStreaming: true, appMode: "code", commandPaletteOpen: false });
    render(<ShortcutHarness />);
    const composing = new KeyboardEvent("keydown", { key: "K", code: "KeyK", ctrlKey: true, isComposing: true, bubbles: true, cancelable: true });
    window.dispatchEvent(composing);
    expect(composing.defaultPrevented).toBe(false);
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
    const legacy = new KeyboardEvent("keydown", { key: "1", altKey: true, keyCode: 229, bubbles: true, cancelable: true });
    window.dispatchEvent(legacy);
    expect(useAppStore.getState().appMode).toBe("code");
    const confirm = new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true, cancelable: true });
    window.dispatchEvent(confirm);
    expect(confirm.defaultPrevented).toBe(false);
    expect(useAppStore.getState().isStreaming).toBe(true);
  });

  it("routes the configured file shortcut before editor key bindings and stops using the old binding", () => {
    useAppStore.setState({ settingsOpen: false, shortcutsHelpOpen: false, skillsMarketplaceOpen: false,
      liveArtifactsOpen: false, agentEditorOpen: false, runtimeCapabilities: null,
      quickOpenVisible: false, shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS } });
    const localEditorKey = vi.fn((event: React.KeyboardEvent) => { event.preventDefault(); event.stopPropagation(); });
    render(<><textarea aria-label="Monaco keyboard target" onKeyDown={localEditorKey} /><ShortcutHarness /></>);
    const input = screen.getByRole("textbox", { name: "Monaco keyboard target" });
    fireEvent.keyDown(input, { key: "p", ctrlKey: true });
    expect(useAppStore.getState().quickOpenVisible).toBe(true);
    expect(localEditorKey).not.toHaveBeenCalled();
    useAppStore.setState({ quickOpenVisible: false, shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS, globalSearch: "Mod+Shift+P" } });
    fireEvent.keyDown(input, { key: "p", ctrlKey: true });
    expect(useAppStore.getState().quickOpenVisible).toBe(false);
    expect(localEditorKey).toHaveBeenCalledOnce();
    fireEvent.keyDown(input, { key: "P", ctrlKey: true, shiftKey: true });
    expect(useAppStore.getState().quickOpenVisible).toBe(true);
  });

  it("lets the active shortcut recorder receive the current file-search binding", () => {
    useAppStore.setState({ settingsOpen: true, settingsTab: "shortcuts", quickOpenVisible: false,
      shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS } });
    render(<><button data-shortcut-recording="true">Record binding</button><ShortcutHarness /></>);
    const recorder = vi.fn((event: KeyboardEvent) => { event.preventDefault(); event.stopPropagation(); });
    window.addEventListener("keydown", recorder, true);
    fireEvent.keyDown(screen.getByRole("button", { name: "Record binding" }), { key: "p", ctrlKey: true });
    expect(recorder).toHaveBeenCalledOnce();
    expect(useAppStore.getState().quickOpenVisible).toBe(false);
    window.removeEventListener("keydown", recorder, true);
  });

  it("distinguishes Save All from single-file save while an editor input has focus", () => {
    useAppStore.setState({ settingsOpen: false, shortcutsHelpOpen: false, skillsMarketplaceOpen: false, liveArtifactsOpen: false, automationsOpen: false, agentEditorOpen: false });
    const single = vi.fn();
    const all = vi.fn();
    window.addEventListener("editor:save", single);
    window.addEventListener("editor:save-all", all);
    render(<><textarea aria-label="Editor input" /><ShortcutHarness /></>);
    const input = document.querySelector("textarea")!;
    input.focus();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "S", code: "KeyS", ctrlKey: true, shiftKey: true, bubbles: true, cancelable: true }));
    expect(all).toHaveBeenCalledOnce();
    expect(single).not.toHaveBeenCalled();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "s", code: "KeyS", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(single).toHaveBeenCalledOnce();
    expect(all).toHaveBeenCalledOnce();
    window.removeEventListener("editor:save", single);
    window.removeEventListener("editor:save-all", all);
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

  it.each(["r", "R"])("opens prompt history for Ctrl+%s", async (key) => {
    useAppStore.setState({ settingsOpen: false, shortcutsHelpOpen: false, skillsMarketplaceOpen: false,
      liveArtifactsOpen: false, automationsOpen: false, agentEditorOpen: false, commandPaletteOpen: false, quickOpenVisible: false });
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

    await waitFor(() => expect(onHistorySearch).toHaveBeenCalledOnce());
    expect(event.defaultPrevented).toBe(true);
    window.removeEventListener("composer:history-search", onHistorySearch);
  });

  it("routes Clear Composer to the real main input while preserving the side-chat draft", async () => {
    useAppStore.setState({
      settingsOpen: false, shortcutsHelpOpen: false, skillsMarketplaceOpen: false,
      liveArtifactsOpen: false, automationsOpen: false, agentEditorOpen: false,
      commandPaletteOpen: false, quickOpenVisible: false,
      appMode: "code", conversationId: "composer-focus", workingDirectory: "C:/workspace",
      draft: "main draft",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: false },
        { id: "editor", kind: "editor", label: "Editor", size: 1, focused: true },
      ],
    });
    function MainComposer() {
      const draft = useAppStore((state) => state.draft);
      const setDraft = useAppStore((state) => state.setDraft);
      return <ComposerTextarea value={draft} onChange={setDraft} onSubmit={vi.fn()} />;
    }
    render(<><textarea aria-label="侧边对话消息" defaultValue="side draft" /><MainComposer /><ShortcutHarness /></>);
    const sideInput = screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement;
    const mainInput = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    sideInput.focus();
    fireEvent.keyDown(sideInput, { key: "l", code: "KeyL", ctrlKey: true });

    await waitFor(() => expect(document.activeElement).toBe(mainInput));
    expect(mainInput.value).toBe("");
    expect(sideInput.value).toBe("side draft");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.id).toBe("main-chat");
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
