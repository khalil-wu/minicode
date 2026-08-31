/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { QueuedMessageList } from "./QueuedMessageList";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));

describe("QueuedMessageList", () => {
  beforeEach(() => {
    vi.mocked(sendClientCommand).mockClear();
    useAppStore.setState({
      conversationId: "conv-queue",
      messages: [
        { id: "assistant-active", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true },
        { id: "user-three", role: "user", content: "third request", artifacts: [], timestamp: 3, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-three" },
        { id: "assistant-three", role: "assistant", content: "", artifacts: [], timestamp: 3, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-three" },
        { id: "user-two", role: "user", content: "second request", artifacts: [], timestamp: 2, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-two" },
        { id: "assistant-two", role: "assistant", content: "", artifacts: [], timestamp: 2, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-two" },
      ],
    });
  });

  afterEach(() => cleanup());

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
    });

    fireEvent.click(screen.getByRole("button", { name: "删除排队消息 2" }));
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "user_message.queue.cancel",
      conversation_id: "conv-queue",
      message_id: "assistant-two",
      user_message_id: "user-two",
    });
  });
});
