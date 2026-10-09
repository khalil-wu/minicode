/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatTurn } from "./components/ChatTurn";
import { MessageList } from "./MessageList";
import { useAppStore } from "../stores";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { conversationReadingState, releaseConversationReadingState } from "./transcriptReadingState";
import type { ChatMessage } from "../stores/types";
import type { ChatTurnState } from "./cells/cellTypes";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

const owner = "reading-owner";
const messages: ChatMessage[] = [
  { id: "user", role: "user", content: "检查记录", artifacts: [], timestamp: 1 },
  { id: "assistant", role: "assistant", content: "已完成", artifacts: [], timestamp: 2,
    terminalStatus: "completed", blocks: [
      { type: "tool_call", record: { id: "read", name: "read_file", args: { file_path: "README.md" },
        outputPreview: "真实文件内容", status: "success", startedAt: 3, finishedAt: 4 } },
      { type: "text", itemId: "answer", content: "已完成", source: "model_final", status: "completed", isStreaming: false },
    ] },
];
beforeEach(() => {
  releaseConversationReadingState(owner);
  releaseConversationReadingState("reading-other");
  useAppStore.setState({ conversationId: owner, conversations: [{ id: owner, title: "Reading", updatedAt: "2026-10-09" }],
    messages, isStreaming: false, viewMode: "normal", workingDirectory: "", turnDiffs: {}, pendingConversationSwitchId: null,
    messageRevealTarget: null, conversationHydration: {}, conversationHistoryPages: {} });
});
afterEach(() => { cleanup(); releaseConversationReadingState(owner); releaseConversationReadingState("reading-other"); });

it("does not re-render or rescan a thousand completed tool rows when only the live answer grows", () => {
  let kindReads = 0;
  const committedCells: ChatTurnState["committedCells"] = Array.from({ length: 1000 }, (_, index) => ({
    get kind() { kindReads++; return "activity" as const; }, id: `tool-${index}`, activityKind: "fileRead" as const,
    title: `Read ${index}`, status: "done" as const, startedAt: 1, collapsed: true, segment: 1, segmentClosed: true,
  }));
  const turn: ChatTurnState = { id: "long-turn", userCell: null, committedCells, finalAnswerCell: null, startedAt: 1,
    status: "streaming", activeCell: { kind: "streaming_assistant_tail", id: "live", partialMarkdown: "开始回答", updatedAt: 1 } };
  const view = render(<ChatTurn turn={turn} conversationId={owner} />);
  kindReads = 0;
  view.rerender(<ChatTurn turn={{ ...turn, activeCell: { ...turn.activeCell!, partialMarkdown: "开始回答，继续输出", updatedAt: 2 } }} conversationId={owner} />);
  expect(screen.getByText("开始回答，继续输出")).toBeTruthy();
  // The answer projector locates a possible error once; no process grouping,
  // glyph, attention, or historical row rendering runs for this text delta.
  expect(kindReads).toBe(1000);
});

it("retains the reader's process and tool disclosure through unmounts without borrowing another conversation's choices", () => {
  const turn = projectMessagesToTurns(messages, false)[0];
  let view = render(<ChatTurn turn={turn} conversationId={owner} />);
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
  expect(screen.getByText("真实文件内容")).toBeTruthy();
  view.unmount();
  view = render(<ChatTurn turn={turn} conversationId="reading-other" />);
  expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
  expect(screen.queryByText("真实文件内容")).toBeNull();
  view.unmount();
  view = render(<ChatTurn turn={turn} conversationId={owner} />);
  expect(screen.getByRole("button", { name: "收起处理步骤" })).toBeTruthy();
  expect(screen.getByText("真实文件内容")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  expect(screen.getByText("真实文件内容")).toBeTruthy();
  view.unmount();
  releaseConversationReadingState(owner);
  render(<ChatTurn turn={turn} conversationId={owner} />);
  expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
});

it("keeps explicitly read live work expanded when the final item completes after virtualization remounts it", () => {
  const live: ChatMessage[] = messages.map((message) => message.role === "assistant" ? { ...message,
    terminalStatus: undefined, isStreaming: true, blocks: message.blocks?.slice(0, 1) } : message);
  let view = render(<ChatTurn turn={projectMessagesToTurns(live, true)[0]} conversationId={owner} />);
  fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
  view.unmount();
  view = render(<ChatTurn turn={projectMessagesToTurns(live, true)[0]} conversationId={owner} />);
  expect(screen.getByText("真实文件内容")).toBeTruthy();
  view.rerender(<ChatTurn turn={projectMessagesToTurns(messages, false)[0]} conversationId={owner} />);
  expect(screen.getByRole("button", { name: "收起处理步骤" })).toBeTruthy();
  expect(screen.getByText("真实文件内容")).toBeTruthy();
});

it("restores scroll and paused follow after a pending switch unmounts the transcript", async () => {
  let view = render(<MessageList />);
  let scroll = screen.getByTestId("message-list-scroll");
  Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1600 });
  Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
  await waitFor(() => expect(scroll.scrollTop).toBe(1600));
  fireEvent.wheel(scroll, { deltaY: -120 });
  scroll.scrollTop = 420;
  view.unmount();
  expect(conversationReadingState(owner).viewport).toMatchObject({ scrollTop: 420, isFollowing: false });
  view = render(<MessageList />);
  scroll = screen.getByTestId("message-list-scroll");
  Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 1800 });
  Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 400 });
  await waitFor(() => expect(scroll.scrollTop).toBe(420));
  act(() => useAppStore.setState({ isStreaming: true }));
  expect(scroll.scrollTop).toBe(420);
});
