/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import { MessageList } from "./MessageList";
import { releaseConversationReadingState } from "./transcriptReadingState";

vi.mock("./components/ChatTurn", () => ({
  ChatTurn: ({ turn }: { turn: { id: string } }) => {
    if (JSON.stringify(turn).includes("BROKEN")) throw new Error("cell exploded");
    return <div data-testid={`turn-${turn.id}`}>rendered {turn.id}</div>;
  },
}));

const CONVERSATION = "conv-turn-boundary";
const messages: ChatMessage[] = [
  { id: "user-ok", role: "user", content: "first question", artifacts: [], timestamp: 1 },
  { id: "answer-ok", role: "assistant", content: "healthy answer", artifacts: [], timestamp: 2 },
  { id: "user-broken", role: "user", content: "second question", artifacts: [], timestamp: 3 },
  { id: "answer-broken", role: "assistant", content: "BROKEN answer", artifacts: [], timestamp: 4 },
  { id: "user-after", role: "user", content: "third question", artifacts: [], timestamp: 5 },
  { id: "answer-after", role: "assistant", content: "later answer", artifacts: [], timestamp: 6 },
];

beforeEach(() => {
  releaseConversationReadingState(CONVERSATION);
  useAppStore.setState({
    conversationId: CONVERSATION,
    conversations: [{ id: CONVERSATION, title: "Turn boundary", updatedAt: "2026-01-01T00:00:00.000Z" }],
    messages,
    isStreaming: false,
    appMode: "chat",
    turnDiffs: {},
  });
});

afterEach(() => {
  cleanup();
  releaseConversationReadingState(CONVERSATION);
  useAppStore.setState({ conversationId: null, conversations: [], messages: [], isStreaming: false, turnDiffs: {} });
});

it("keeps the other turns rendered when one turn throws while rendering", () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  const swallow = (event: Event) => event.preventDefault();
  window.addEventListener("error", swallow);
  try {
    render(<MessageList />);
    expect(screen.getAllByText(/^rendered /)).toHaveLength(2);
    const alerts = screen.getAllByRole("alert").map((element) => element.textContent ?? "");
    expect(alerts.some((text) => text.includes("此回复渲染失败"))).toBe(true);
    expect(screen.getByRole("button", { name: "重新加载" })).toBeTruthy();
  } finally {
    window.removeEventListener("error", swallow);
    consoleError.mockRestore();
  }
});
