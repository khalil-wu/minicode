/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPane } from "./ChatPane";
import { useAppStore } from "../stores";

const { messageRenders, summaryRenders } = vi.hoisted(() => ({ messageRenders: vi.fn(), summaryRenders: vi.fn() }));
vi.mock("./MessageList", () => ({ MessageList: () => { messageRenders(); return <div>messages</div>; } }));
vi.mock("../composer/Composer", () => ({ Composer: () => <textarea aria-label="composer" /> }));
vi.mock("./ChatContextCard", () => ({ ChatContextCard: ({ displayMode }: { displayMode: string }) => { summaryRenders(displayMode); return <aside>context</aside>; } }));

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

  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("updates only the summary at layout breakpoints while a sidebar animates", () => {
    let resized: ResizeObserverCallback;
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1200);
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { resized = callback; }
      observe() {} disconnect() {}
    });
    render(<ChatPane />);
    messageRenders.mockClear(); summaryRenders.mockClear();
    for (const width of [1210, 1240, 1280, 1320, 1400]) act(() => resized([{ contentRect: { width } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(messageRenders).not.toHaveBeenCalled();
    expect(summaryRenders).not.toHaveBeenCalled();
    act(() => resized([{ contentRect: { width: 1600 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(summaryRenders).toHaveBeenCalledWith("gutter");
    expect(messageRenders).not.toHaveBeenCalled();
  });

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
    fireEvent.change(composer, { target: { value: "Unsent prompt" } });

    act(() => useAppStore.setState({ pendingConversationSwitchId: "conv-next" }));
    expect(container.querySelector(".chat-pane")?.getAttribute("data-switching")).toBe("true");
    const messages = screen.getByText("messages");
    expect(messages.closest<HTMLElement>(".chat-pane-layout")?.style.visibility).toBe("hidden");
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull();
    expect(screen.getByRole("textbox", { hidden: true })).toBe(composer);
    expect(container.querySelector<HTMLElement>(".chat-pane-layout")?.style.visibility).toBe("hidden");
    expect(container.querySelector("aside")?.closest<HTMLElement>(".chat-pane-layout")?.style.visibility).toBe("hidden");
    const opening = screen.getByRole("status");
    expect(opening.textContent).toContain("正在打开会话");
    expect(opening.className).toBe("chat-pane-switch-status");
    expect(opening.parentElement?.classList.contains("chat-pane")).toBe(true);
    expect(opening.querySelector("svg.animate-spin")?.getAttribute("aria-hidden")).toBe("true");
    expect(useAppStore.getState().draft).toBe("Unsent prompt");

    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    expect(screen.getByRole("textbox", { name: "composer" })).toBe(composer);
    expect((composer as HTMLTextAreaElement).value).toBe("Unsent prompt");
    expect(screen.getByText("messages")).toBe(messages);
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

  it("centers cold-loading progress in the entire chat pane while its retained composer is hidden", () => {
    useAppStore.setState({ conversationHydration: { "conv-chat-pane": { isHydrating: true, updatedAt: 1 } } });
    render(<ChatPane />);
    expect(screen.getByText("messages").closest<HTMLElement>(".chat-pane-layout")?.style.visibility).toBe("hidden");
    expect(screen.getByRole("status").querySelector(".animate-spin")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull();
    expect(screen.getByRole("textbox", { hidden: true })).toBeTruthy();
    act(() => useAppStore.setState({ conversationHydration: {} }));
    expect(screen.getByText("messages")).toBeTruthy();
    expect(screen.queryByText("正在打开会话…")).toBeNull();
  });
});
