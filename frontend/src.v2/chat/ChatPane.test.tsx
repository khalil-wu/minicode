/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPane } from "./ChatPane";
import { useAppStore } from "../stores";

vi.mock("./MessageList", () => ({ MessageList: () => <div>messages</div> }));
vi.mock("../composer/Composer", () => ({ Composer: () => <textarea aria-label="composer" /> }));
vi.mock("./ChatContextCard", () => ({ ChatContextCard: () => <aside>context</aside> }));

describe("ChatPane search shortcuts", () => {
  beforeEach(() => {
    useAppStore.setState({
      conversationId: "conv-chat-pane",
      pendingConversationSwitchId: null,
      conversationHydration: {},
      messages: [],
      appMode: "cowork",
      panelSlots: [{ id: "main-chat", kind: "chat", focused: true }],
    });
  });

  afterEach(() => cleanup());

  it("toggles Ctrl+F and closes the search with Escape", () => {
    const { container } = render(<ChatPane />);

    const pane = container.querySelector<HTMLElement>(".chat-pane");
    expect(pane?.style.display).toBe("grid");
    expect(pane?.classList.contains("flex")).toBe(false);
    expect(pane?.classList.contains("flex-col")).toBe(false);

    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    const search = screen.getByPlaceholderText("在对话中搜索…");
    const layout = container.querySelector(".chat-pane-layout");
    const main = container.querySelector(".chat-pane-main");
    const messages = container.querySelector(".chat-pane-message-transition");
    const composerRegion = container.querySelector(".chat-pane-composer-region");
    expect(search).toBeTruthy();
    expect(layout?.parentElement).toBe(pane);
    expect(main?.parentElement).toBe(layout);
    expect(main?.contains(search)).toBe(true);
    expect(messages?.parentElement).toBe(main);
    expect(composerRegion?.parentElement).toBe(main);
    expect(composerRegion?.querySelector('[aria-label="composer"]')).toBeTruthy();
    expect(main?.nextElementSibling?.tagName).toBe("ASIDE");

    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    expect(screen.queryByPlaceholderText("在对话中搜索…")).toBeNull();

    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    expect(screen.getByPlaceholderText("在对话中搜索…")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByPlaceholderText("在对话中搜索…")).toBeNull();
  });

  it("lets a focused Monaco editor handle Ctrl+F instead of opening chat search", () => {
    useAppStore.setState({
      appMode: "code",
      panelSlots: [
        { id: "main-chat", kind: "chat", focused: true },
        { id: "main-editor", kind: "editor", focused: false },
      ],
    });
    render(<><div role="textbox" aria-label="Monaco editor" tabIndex={0} /><ChatPane /></>);
    const editor = screen.getByRole("textbox", { name: "Monaco editor" });
    editor.focus();
    for (const modifier of [{ ctrlKey: true }, { metaKey: true }]) {
      const findEvent = new KeyboardEvent("keydown", { key: "f", ...modifier, bubbles: true, cancelable: true });
      editor.dispatchEvent(findEvent);
      expect(findEvent.defaultPrevented).toBe(false);
    }
    expect(screen.queryByPlaceholderText("在对话中搜索…")).toBeNull();

    useAppStore.getState().focusPanel("main-editor");
    const focusedEditorEvent = new KeyboardEvent("keydown", { key: "f", ctrlKey: true, bubbles: true, cancelable: true });
    editor.dispatchEvent(focusedEditorEvent);
    expect(focusedEditorEvent.defaultPrevented).toBe(false);

    useAppStore.getState().focusPanel("main-chat");
    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    expect(screen.getByPlaceholderText("在对话中搜索…")).toBeTruthy();
  });

  it("shows opening progress while a switch waits for its public page and preserves the source draft", () => {
    useAppStore.setState({ draft: "Unsent prompt" });
    const { container } = render(<ChatPane />);
    expect(screen.getByText("messages")).toBeTruthy();
    const composer = screen.getByRole("textbox", { name: "composer" });

    act(() => useAppStore.setState({ pendingConversationSwitchId: "conv-next" }));
    expect(container.querySelector(".chat-pane")?.getAttribute("data-switching")).toBe("true");
    expect(screen.queryByText("messages")).toBeNull();
    expect(screen.getByRole("textbox", { name: "composer" })).toBe(composer);
    expect(composer.closest<HTMLElement>(".chat-pane-composer-region")?.inert).toBe(true);
    const opening = screen.getByRole("status");
    expect(opening.textContent).toContain("正在打开会话");
    expect(opening.className).toBe("chat-pane-switch-status");
    expect(opening.parentElement?.classList.contains("chat-pane-message-transition")).toBe(true);
    expect(opening.querySelector("svg.animate-spin")?.getAttribute("aria-hidden")).toBe("true");
    expect(useAppStore.getState().draft).toBe("Unsent prompt");

    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    expect(composer.closest<HTMLElement>(".chat-pane-composer-region")?.inert).toBe(false);
    expect(screen.getByText("messages")).toBeTruthy();
    expect(screen.queryByText("正在打开会话…")).toBeNull();
    expect(useAppStore.getState().draft).toBe("Unsent prompt");
  });

  it("shows an informative hydration status while backend context is being restored", () => {
    useAppStore.setState({
      messages: [{ id: "cached", role: "user", content: "cached content", timestamp: 1, artifacts: [] }],
      conversationHydration: {
        "conv-chat-pane": { isHydrating: true, updatedAt: 1 },
      },
    });

    render(<ChatPane />);

    expect(screen.getByRole("status").textContent).toContain("正在恢复会话上下文、运行状态和工具记录");
    expect(screen.getByText("messages")).toBeTruthy();
    expect(screen.getByRole("status").querySelector(".animate-spin")).toBeNull();
  });

  it("centers cold-loading progress in the message area and retains the composer", () => {
    useAppStore.setState({ conversationHydration: { "conv-chat-pane": { isHydrating: true, updatedAt: 1 } } });
    render(<ChatPane />);
    expect(screen.queryByText("messages")).toBeNull();
    expect(screen.getByRole("status").querySelector(".animate-spin")).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "composer" })).toBeTruthy();
    act(() => useAppStore.setState({ conversationHydration: {} }));
    expect(screen.getByText("messages")).toBeTruthy();
    expect(screen.queryByText("正在打开会话…")).toBeNull();
  });
});
