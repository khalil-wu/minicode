/* @vitest-environment jsdom */
import { expect, it } from "vitest";
import { useAppStore } from ".";
import type { ChatMessage } from "./types";

it("rebuilds the session usage totals when an uncached conversation's history arrives", () => {
  useAppStore.setState({ conversationId: "other", conversationMessages: {}, messages: [],
    usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 } });
  const history: ChatMessage[] = [
    { id: "u", role: "user", content: "q", artifacts: [], timestamp: 1 },
    { id: "a", role: "assistant", content: "answer", artifacts: [], timestamp: 2,
      usage: { input: 1000, output: 50, cacheRead: 800, cacheWrite: 0, reasoning: 10 } } as ChatMessage,
  ];
  useAppStore.getState().hydrateConversationMessages("target", history, { activate: true });
  expect(useAppStore.getState().conversationId).toBe("target");
  expect(useAppStore.getState().usageTotals).toMatchObject({ input: 1000, output: 50, cacheRead: 800, reasoning: 10, turns: 1 });
});
