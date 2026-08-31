/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
      conversationHydration: {},
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

  it("shows an informative hydration status while backend context is being restored", () => {
    useAppStore.setState({
      conversationHydration: {
        "conv-chat-pane": { isHydrating: true, updatedAt: 1 },
      },
    });

    render(<ChatPane />);

    expect(screen.getByRole("status").textContent).toContain("正在恢复会话上下文、运行状态和工具记录");
  });
});
