/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import { MessageList } from "./MessageList";
import { handleDiffEvent } from "./diffEvents";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(),
}));

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

const conversationMessages: ChatMessage[] = [
  {
    id: "user-1",
    role: "user",
    content: "请检查聊天 UI 输出。",
    artifacts: [],
    timestamp: 1,
  },
  {
    id: "assistant-1",
    role: "assistant",
    content: "最终答案：已经整理成 cell UI。",
    blocks: [
      {
        type: "tool_call",
        record: {
          id: "read-1",
          name: "read_file",
          args: { file_path: "frontend/src.v2/chat/MessageList.tsx" },
          status: "success",
          startedAt: 2,
          finishedAt: 3,
        },
      },
      {
        type: "text",
        itemId: "agent-message",
        content: "最终答案：已经整理成 cell UI。",
        source: "model_final",
        status: "completed",
        isStreaming: false,
      },
    ],
    artifacts: [],
    timestamp: 4,
  },
];

describe("MessageList cell UI", () => {
  beforeEach(() => {
    localStorage.clear();
    useAppStore.setState({
      conversationId: "conv-message-list-test",
      conversations: [{
        id: "conv-message-list-test",
        title: "Message list test",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      messages: conversationMessages,
      isStreaming: false,
      appMode: "chat",
      turnDiffs: {},
    });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      messages: [],
      isStreaming: false,
      turnDiffs: {},
      diffReview: null,
      gitReviewRequest: null,
      workingDirectory: "",
    });
  });

  it("opens every persisted parent file change without a parent file-tool receipt or Git tab", () => {
    const patch = (path: string) => `diff --git a/${path} b/${path}\nnew file mode 100644\n--- /dev/null\n+++ b/${path}\n@@ -0,0 +1 @@\n+${path}\n`;
    const paths = ["ledger_check/parser.py", "tests/test_parser.py", "ledger_check/__main__.py"];
    const diff = paths.map(patch).join("");
    const messages = hydrateMessages([{ id: "parent-answer", role: "assistant", turn_id: "parent-turn", content: "Done", timestamp: 1,
      blocks: [{ type: "text", content: "Done", item_id: "final", source: "model_final", status: "completed" }],
      metadata: { turn_diff: { thread_id: "conv-message-list-test", conversation_id: "conv-message-list-test",
        turn_id: "parent-turn", message_id: "parent-answer", diff, revision: 5 } } }]);
    useAppStore.setState({ workingDirectory: "C:\\ledger", diffReview: null, gitReviewRequest: null });
    useAppStore.getState().hydrateConversationMessages("conv-message-list-test", messages, { activate: true });
    expect(useAppStore.getState().turnDiffs["conv-message-list-test"].diff).toBe(diff);
    const { container } = render(<MessageList />);
    expect(container.querySelectorAll(".diff-cell")).toHaveLength(1);
    for (const path of paths) expect(screen.getByText(path, { exact: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看变更", exact: true }));
    expect(useAppStore.getState().rightStackTab).toBe("diff");
    expect(useAppStore.getState().diffReview?.files.map((file) => file.path)).toEqual(paths);
    expect(useAppStore.getState().diffReview?.files.map((file) => file.patch)).toEqual(paths.map(patch));
  });

  it("shows a child journal's own change card without borrowing the concurrent parent or peer patch", () => {
    const own = "diff --git a/ledger_check/parser.py b/ledger_check/parser.py\n--- /dev/null\n+++ b/ledger_check/parser.py\n@@ -0,0 +1 @@\n+parser_ready = True\n";
    useAppStore.setState({ turnDiffs: { "conv-message-list-test": { threadId: "conv-message-list-test", turnId: "parent-turn", updatedAt: 1,
      diff: "diff --git a/tests/test_parser.py b/tests/test_parser.py\n--- /dev/null\n+++ b/tests/test_parser.py\n@@ -0,0 +1 @@\n+peer_ready = True\n" } } });
    const child = hydrateMessages([{ id: "child-answer", role: "assistant", turn_id: "child-turn", content: "Parser done", timestamp: 2,
      blocks: [{ type: "text", content: "Parser done", item_id: "child-final", source: "model_final", status: "completed" }],
      metadata: { turn_diff: { thread_id: "conv-message-list-test", conversation_id: "conv-message-list-test",
        turn_id: "child-turn", message_id: "child-answer", diff: own, revision: 3 } } }]);
    const { container } = render(<ChatTurn turn={projectMessagesToTurns(child, false)[0]} isTranscriptMode conversationId="conv-message-list-test" />);
    expect(container.querySelectorAll(".diff-cell")).toHaveLength(1);
    expect(screen.getByText("ledger_check/parser.py", { exact: true })).toBeTruthy();
    expect(screen.queryByText("tests/test_parser.py")).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销", exact: true })).toBeNull();
  });

  it("uses turn-based HistoryCell rendering by default", () => {
    const { container } = render(<MessageList />);

    expect(container.querySelector(".chat-turn")).toBeTruthy();
    expect(screen.getByText("请检查聊天 UI 输出。")).toBeTruthy();
    expect(screen.getByText("最终答案：已经整理成 cell UI。")).toBeTruthy();
  });

  it("keeps the transcript fully opaque while switching conversations", async () => {
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");

    useAppStore.setState({
      conversationId: "conv-message-list-other",
      conversations: [{
        id: "conv-message-list-other",
        title: "Other conversation",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      messages: conversationMessages,
    });

    await waitFor(() => expect(scroll.style.opacity).toBe("1"));
    expect(scroll.className).not.toContain("transition-opacity");
  });

  it.each(["exact", "unavailable", "empty"] as const)("shows committed edit evidence after the reply: %s", (aggregate) => {
    useAppStore.setState({
      messages: [
        { ...conversationMessages[0], turnId: "turn-file-summary" },
        {
          ...conversationMessages[1],
          turnId: "turn-file-summary",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "edit-app",
                name: "apply_patch",
                args: { file_path: "src/app.ts" },
                status: "success",
                resultKind: "edit",
                activityKind: "fileChange",
                diff: { plus: 1, minus: 1, patch: "@@ -1 +1 @@\n-old\n+new" },
                startedAt: 2,
                finishedAt: 3,
              },
            },
            ...(conversationMessages[1].blocks?.slice(-1) ?? []),
          ],
          terminalStatus: "completed",
          isStreaming: false,
        },
      ],
    });

    if (aggregate !== "exact") handleDiffEvent({
      type: "turn.diff.updated", thread_id: "conv-message-list-test",
      conversation_id: "conv-message-list-test", turn_id: "turn-file-summary",
      diff: aggregate === "unavailable" ? null : "", revision: 2,
    });
    render(<MessageList />);

    const work = screen.getByLabelText("Agent 处理进度");
    const reply = screen.getByLabelText("Agent 回复");
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    if (aggregate === "empty") {
      expect(screen.queryByLabelText("文件修改")).toBeNull();
      return;
    }
    const diff = screen.getByLabelText("文件修改");
    expect(work.querySelector(".diff-cell")).toBeNull();
    expect(diff.querySelector(".diff-cell")).toBeTruthy();
    expect(reply.compareDocumentPosition(diff) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText("src/app.ts").length).toBe(2);
    expect(work.querySelector(".activity-cell")).toBeTruthy();
    if (aggregate === "unavailable") {
      expect(screen.getByText("编辑记录 1 个文件")).toBeTruthy();
      expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
    }
  });

  it("marks Code mode for the wide conversation axis", () => {
    useAppStore.setState({ appMode: "code" });

    const { container } = render(<MessageList />);

    expect(container.querySelector(".message-list-content")?.getAttribute("data-layout-mode")).toBe("code");
  });

  it("uses the same code-layout conversation axis in Cowork mode", () => {
    useAppStore.setState({ appMode: "cowork" });

    const { container } = render(<MessageList />);

    expect(container.querySelector(".message-list-content")?.getAttribute("data-layout-mode")).toBe("code");
  });

  it("ignores the old localStorage legacy renderer escape hatch", () => {
    localStorage.setItem("minicode.legacyMessageUi", "1");

    const { container } = render(<MessageList />);

    expect(container.querySelector(".chat-turn")).toBeTruthy();
    expect(screen.getByText("最终答案：已经整理成 cell UI。")).toBeTruthy();
  });

  it("keeps an unbound empty chat quiet and prompt-first", () => {
    useAppStore.setState({
      messages: [],
      workingDirectory: "",
      currentModel: "",
      conversations: [{
        id: "conv-message-list-test",
        title: "Message list test",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
    });

    render(<MessageList />);

    // Minimal home: just the greeting, nothing clickable above the composer.
    expect(screen.getByText("今天想构建什么？")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not repeat workspace and model metadata in bound empty chats", () => {
    useAppStore.setState({
      messages: [],
      workingDirectory: "C:/repo/MiniCode",
      currentModel: "gpt-5",
      conversations: [{
        id: "conv-message-list-test",
        title: "Message list test",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
    });

    render(<MessageList />);

    expect(screen.getByText("今天想构建什么？")).toBeTruthy();
    expect(document.body.textContent).not.toContain("C:/repo/MiniCode");
    expect(document.body.textContent).not.toContain("gpt-5");
  });

  it("does not render stale messages when no visible active conversation exists", () => {
    useAppStore.setState({
      conversationId: null,
      conversations: [{
        id: "conv-archived",
        title: "Archived",
        updatedAt: "2026-01-01T00:00:00.000Z",
        archived: true,
      }],
      messages: conversationMessages,
      isStreaming: false,
      workingDirectory: "",
      currentModel: "",
    });

    render(<MessageList />);

    expect(screen.queryByText("请检查聊天 UI 输出。")).toBeNull();
    expect(screen.queryByText("最终答案：已经整理成 cell UI。")).toBeNull();
    expect(screen.getByText("今天想构建什么？")).toBeTruthy();
  });

  it("shows an optimistic first turn before conversation metadata arrives", () => {
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      messages: [
        {
          id: "u-local-first-turn",
          role: "user",
          content: "First query should appear immediately.",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "a-local-first-turn",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        },
      ],
      isStreaming: true,
    });

    render(<MessageList />);

    expect(screen.getByText("First query should appear immediately.")).toBeTruthy();
    expect(screen.queryByText("今天想构建什么？")).toBeNull();
  });

  it("keeps queued prompts in the composer queue instead of duplicating them in history", () => {
    useAppStore.setState({
      messages: [
        ...conversationMessages,
        { id: "user-queued", role: "user", content: "queued follow-up", artifacts: [], timestamp: 5, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-queued" },
        { id: "assistant-queued", role: "assistant", content: "", artifacts: [], timestamp: 5, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-queued" },
      ],
      isStreaming: true,
    });

    render(<MessageList />);

    expect(screen.queryByText("queued follow-up")).toBeNull();
    expect(screen.getByText("最终答案：已经整理成 cell UI。")).toBeTruthy();
  });

  it("renders only recent turns by default and expands older history on demand", () => {
    const longConversation: ChatMessage[] = Array.from({ length: 42 }, (_, index) => ([
      {
        id: `user-${index}`,
        role: "user" as const,
        content: `old user ${index}`,
        artifacts: [],
        timestamp: index * 2,
      },
      {
        id: `assistant-${index}`,
        role: "assistant" as const,
        content: `answer ${index}`,
        blocks: [{
          type: "text" as const,
          itemId: `agent-message-${index}`,
          content: `answer ${index}`,
          source: "model_final",
          status: "completed" as const,
          isStreaming: false,
        }],
        artifacts: [],
        timestamp: index * 2 + 1,
      },
    ])).flat();
    useAppStore.setState({
      messages: longConversation,
      isStreaming: false,
      conversations: [{
        id: "conv-message-list-test",
        title: "Message list test",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
    });

    render(<MessageList />);

    expect(screen.queryByText("old user 0")).toBeNull();
    expect(screen.getByText("answer 41")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /显示更早的消息/ }));

    expect(screen.queryByRole("button", { name: /显示更早的消息/ })).toBeNull();
    expect(screen.getByTestId("virtual-turn-list")).toBeTruthy();
  });

  it("virtualizes expanded history above the CC safety threshold", () => {
    const longConversation: ChatMessage[] = Array.from({ length: 205 }, (_, index) => ([
      { id: `virtual-user-${index}`, role: "user" as const, content: `virtual user ${index}`, artifacts: [], timestamp: index * 2 },
      {
        id: `virtual-assistant-${index}`,
        role: "assistant" as const,
        content: `virtual answer ${index}`,
        blocks: [{ type: "text" as const, itemId: `virtual-text-${index}`, content: `virtual answer ${index}`, status: "completed" as const, isStreaming: false }],
        artifacts: [],
        timestamp: index * 2 + 1,
      },
    ])).flat();
    useAppStore.setState({ messages: longConversation, isStreaming: false });

    render(<MessageList />);
    fireEvent.click(screen.getByRole("button", { name: /显示更早的消息/ }));

    expect(screen.getByTestId("virtual-turn-list")).toBeTruthy();
  });

  it("renders the actively streaming turn outside historical rows", () => {
    useAppStore.setState({
      messages: [
        ...conversationMessages,
        { id: "stream-user", role: "user", content: "continue", artifacts: [], timestamp: 5 },
        { id: "stream-assistant", role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 6, isStreaming: true },
      ],
      isStreaming: true,
    });

    render(<MessageList />);

    expect(screen.getByTestId("streaming-turn-tail")).toBeTruthy();
  });

  it("uses a larger visible hit target for scrolling back to the bottom", async () => {
    const { container } = render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    const scrollTo = vi.fn();
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1200 });
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 0 });
    Object.defineProperty(scroll, "scrollTo", { configurable: true, value: scrollTo });

    fireEvent.scroll(scroll);

    const button = await screen.findByRole("button", { name: "回到底部" });
    expect(button).toBe(screen.getByTestId("scroll-to-bottom-button"));
    expect(button.className).toContain("inline-flex");
    expect(button.style.width).toBe("44px");
    expect(button.style.minWidth).toBe("44px");
    expect(button.style.minHeight).toBe("44px");

    fireEvent.click(button);

    await waitFor(() => {
      expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
      expect(container.querySelector('[aria-label="回到底部"]')).toBeNull();
    });
  });

  it("follows streaming content when the user is already at the bottom", async () => {
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 600 });

    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1320 });
    useAppStore.setState((state) => ({
      messages: state.messages.map((message) =>
        message.id === "assistant-1"
          ? {
              ...message,
              isStreaming: true,
              content: `${message.content} 新增流式内容。`,
              blocks: [
                ...(message.blocks ?? []),
                {
                  type: "text" as const,
                  itemId: "agent-message-streaming",
                  content: "新增流式内容。",
                  source: "model_final",
                  status: "partial" as const,
                  isStreaming: false,
                },
              ],
            }
          : message,
      ),
      isStreaming: true,
    }));

    await waitFor(() => {
      expect(scroll.scrollTop).toBe(1320);
    });
  });

  it("does not steal scroll when the user has intentionally scrolled up", async () => {
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 600 });

    fireEvent.wheel(scroll, { deltaY: -120 });
    scroll.scrollTop = 250;
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1320 });
    useAppStore.setState((state) => ({
      messages: state.messages.map((message) =>
        message.id === "assistant-1"
          ? {
              ...message,
              isStreaming: true,
              content: `${message.content} 新增流式内容。`,
              blocks: [
                ...(message.blocks ?? []),
                {
                  type: "text" as const,
                  itemId: "agent-message-streaming",
                  content: "新增流式内容。",
                  source: "model_final",
                  status: "partial" as const,
                  isStreaming: false,
                },
              ],
            }
          : message,
      ),
      isStreaming: true,
    }));

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(scroll.scrollTop).toBe(250);
  });

  it("anchors a new user's turn at the viewport start after the user was reading history", async () => {
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 600 });

    fireEvent.wheel(scroll, { deltaY: -120 });
    scroll.scrollTop = 250;
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1480 });
    scroll.getBoundingClientRect = () => ({ top: 100 } as DOMRect);
    screen.getByTestId("latest-turn-tail").getBoundingClientRect = () => ({ top: 930 } as DOMRect);
    useAppStore.setState((state) => ({
      messages: [
        ...state.messages,
        {
          id: "user-new",
          role: "user",
          content: "继续",
          artifacts: [],
          timestamp: 10,
        },
        {
          id: "assistant-new",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 11,
          isStreaming: true,
        },
      ],
      isStreaming: true,
    }));

    await waitFor(() => {
      expect(scroll.scrollTop).toBe(1080);
    });
  });

  it("reserves only the newest turn from the actual viewport and tracks composer resize without stealing reading", async () => {
    const observers: Array<{ callback: ResizeObserverCallback; targets: Set<Element> }> = [];
    vi.stubGlobal("ResizeObserver", class {
      entry: { callback: ResizeObserverCallback; targets: Set<Element> };
      constructor(callback: ResizeObserverCallback) { this.entry = { callback, targets: new Set() }; observers.push(this.entry); }
      observe(target: Element) { this.entry.targets.add(target); }
      disconnect() { this.entry.targets.clear(); }
    });
    const { container } = render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    scroll.style.paddingTop = "16px";
    scroll.style.paddingBottom = "24px";
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 640 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1400 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 760 });
    const resize = () => act(() => { observers.filter(observer => observer.targets.has(scroll))
      .forEach(observer => observer.callback([], {} as ResizeObserver)); });
    resize();
    expect(scroll.style.getPropertyValue("--reply-viewport-height")).toBe("600px");
    expect(container.querySelectorAll('[data-reply-viewport="true"]')).toHaveLength(1);
    expect(screen.getByTestId("latest-turn-tail").style.minHeight).toBe("var(--reply-viewport-height)");
    await waitFor(() => expect(scroll.scrollTop).toBe(1400));

    fireEvent.wheel(scroll, { deltaY: -120 });
    scroll.scrollTop = 250;
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 440 });
    resize();
    expect(scroll.style.getPropertyValue("--reply-viewport-height")).toBe("400px");
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(scroll.scrollTop).toBe(250);
  });

  it("uses natural turn heights and keeps its position while transcript search is active", async () => {
    const { rerender } = render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 600 });
    rerender(<MessageList searchActive />);
    expect(screen.getByTestId("latest-turn-tail").getAttribute("data-reply-viewport")).toBe("false");
    expect(screen.getByTestId("latest-turn-tail").style.minHeight).toBe("");
    act(() => useAppStore.setState(state => ({ messages: [...state.messages,
      { id: "search-user", role: "user", content: "keep the search view", artifacts: [], timestamp: 10 }], isStreaming: true })));
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(scroll.scrollTop).toBe(600);
  });

  it("keeps the reading position when a new assistant reply starts after scrolling up", async () => {
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 600 });

    fireEvent.wheel(scroll, { deltaY: -120 });
    scroll.scrollTop = 250;
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1420 });
    useAppStore.setState((state) => ({
      messages: [
        ...state.messages,
        {
          id: "assistant-new",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 10,
          isStreaming: true,
        },
      ],
      isStreaming: true,
    }));

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(scroll.scrollTop).toBe(250);
  });
});
