/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Profiler } from "react";

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

import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { QueuedMessageList } from "./QueuedMessageList";
import type { DiffCommentContextRef } from "../stores/types";
import { pushToast } from "../overlays/ToastContainer";
import type { ClientCommand } from "../protocol/events";
import { resetQueuedMessageHistoryForTests } from "./queued-message-undo";

vi.mock("../protocol/ws-outbox", async load => {
  const original = await load<typeof import("../protocol/ws-outbox")>();
  const send = vi.fn((_command: ClientCommand) => true);
  return { ...original, sendClientCommand: send, sendClientCommandAwaitResult: vi.fn(async (command: ClientCommand) => {
    send(command);
    return { type: "command.result", command: command.type, message: "", level: "success", data: { cancelled: true } };
  }) };
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

describe("QueuedMessageList", () => {
  beforeEach(() => {
    vi.mocked(sendClientCommand).mockClear();
    vi.mocked(pushToast).mockClear();
    useAppStore.setState({
      conversationId: "conv-queue",
      isStreaming: true,
      messages: [
        { id: "assistant-active", turnId: "turn-active", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true },
        { id: "user-three", role: "user", content: "third request", artifacts: [], timestamp: 3, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-three" },
        { id: "assistant-three", role: "assistant", content: "", artifacts: [], timestamp: 3, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-three" },
        { id: "user-two", role: "user", content: "second request", artifacts: [], timestamp: 2, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-two" },
        { id: "assistant-two", role: "assistant", content: "", artifacts: [], timestamp: 2, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-two" },
      ],
    });
  });

  afterEach(async () => { cleanup(); await Promise.resolve(); await Promise.resolve(); resetQueuedMessageHistoryForTests(); });

  it("keeps an unchanged queue out of 32 answer frame commits while retaining real queue edits", () => {
    let commits = 0;
    render(<Profiler id="queue-performance" onRender={() => { commits += 1; }}><QueuedMessageList /></Profiler>);
    commits = 0;
    for (let index = 0; index < 32; index++) act(() => {
      useAppStore.getState().appendAgentMessageDelta("perf-output", ` frame-${index}`, "conv-queue", "assistant-active");
    });
    console.info("PERF", JSON.stringify({ scenario: "queued-input-frames", frames: 32, queueCommits: commits }));
    expect(commits).toBe(0);
    act(() => useAppStore.setState(state => ({ messages: state.messages.map(message => message.id === "user-two"
      ? { ...message, content: "edited queued request" } : message) })));
    expect(screen.getByText("edited queued request")).toBeTruthy();
    expect(commits).toBe(1);
  });

  it.each(["", "original authored request"])("summarizes real comments for queued input %j without replacing its normal steer identity", content => {
    const refs: DiffCommentContextRef[] = ["src/one.ts", "src/two.ts"].map(path => ({
      kind: "diff_comment", name: path, path, comment: { type: "comment", position: { path, side: "right", line: 100 },
        content: [{ content_type: "text", text: "original scoped comment" }], localDiffHunk: "@@ -1 +100 @@\n-old\n+new" },
    }));
    useAppStore.setState(state => ({ messages: [state.messages[0], { id: "comment-user", role: "user", content, artifacts: [], timestamp: 1,
      contextRefs: refs, queueState: "queued", queuePosition: 1, queueMessageId: "comment-assistant" }] }));
    render(<QueuedMessageList />);
    expect(screen.getByText(content || "2 条评论")).toBeTruthy();
    expect(screen.queryByText("Attachment")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "引导" }));
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "user_message.queue.steer", conversation_id: "conv-queue",
      message_id: "comment-assistant", user_message_id: "comment-user", expected_turn_id: "turn-active" });
    expect(useAppStore.getState().messages.find(message => message.id === "comment-user")?.contextRefs).toEqual(refs);
  });

  it("keeps stopped inputs visible and offers explicit queue continuation", () => {
    useAppStore.setState((state) => ({
      isStreaming: false,
      messages: state.messages.map((message) => ({ ...message, queuePaused: message.queueState === "queued" })),
    }));
    render(<QueuedMessageList />);
    expect(screen.getAllByText("已暂停")).toHaveLength(2);
    expect(sendClientCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "发送" })[0]);
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "user_message.queue.start", conversation_id: "conv-queue",
      message_id: "assistant-two", user_message_id: "user-two",
    });
  });

  it("renders MiniCode numbered rows and exposes steer and delete actions", () => {
    render(<QueuedMessageList />);

    expect(screen.getByText("second request")).toBeTruthy();
    expect(screen.getByText("third request")).toBeTruthy();
    expect(screen.getAllByText(/request$/).map((node) => node.textContent)).toEqual([
      "second request",
      "third request",
    ]);
    expect(screen.getByLabelText("队列第 2 项")).toBeTruthy();
    expect(screen.getByLabelText("队列第 3 项")).toBeTruthy();

    fireEvent.click(screen.getAllByText("引导")[1]);
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "user_message.queue.steer",
      conversation_id: "conv-queue",
      message_id: "assistant-three",
      user_message_id: "user-three",
      expected_turn_id: "turn-active",
    });

    fireEvent.click(screen.getByRole("button", { name: "删除排队消息 2" }));
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "user_message.queue.cancel",
      conversation_id: "conv-queue",
      message_id: "assistant-two",
      user_message_id: "user-two",
    });
  });

  it("rejects steering before run-start supplied a real turn and preserves the queue", () => {
    useAppStore.setState(state => ({ draft: "keep draft", messages: state.messages.map(message => message.id === "assistant-active"
      ? { ...message, turnId: undefined } : message) }));
    const messages = useAppStore.getState().messages;
    render(<QueuedMessageList />);
    fireEvent.click(screen.getAllByRole("button", { name: "引导" })[0]);
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("回合标识"), "warning");
    expect(useAppStore.getState()).toMatchObject({ messages, draft: "keep draft" });
  });

  it("captures the actual current turn for a paused queue item without taking its message id as the turn", () => {
    useAppStore.setState(state => ({ messages: state.messages.map(message => ({ ...message,
      ...(message.id === "assistant-active" ? { turnId: "new-real-turn" } : {}), queuePaused: message.queueState === "queued" })) }));
    render(<QueuedMessageList />);
    fireEvent.click(screen.getAllByRole("button", { name: "引导" })[0]);
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "user_message.queue.steer", conversation_id: "conv-queue",
      message_id: "assistant-two", user_message_id: "user-two", expected_turn_id: "new-real-turn" });
    expect(useAppStore.getState().messages.find(message => message.id === "user-two")?.queueState).toBe("queued");
  });
});
