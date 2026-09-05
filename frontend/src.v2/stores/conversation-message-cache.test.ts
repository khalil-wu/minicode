/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});
import { useAppStore } from "./index";
import type { ChatMessage, ConversationMeta } from "./types";

const message = (id: string): ChatMessage => ({
  id: `message-${id}`,
  role: "assistant",
  content: id,
  artifacts: [],
  timestamp: 1,
});

describe("conversation transcript renderer cache", () => {
  it("keeps sidebar streaming state stable for text-only deltas", () => {
    useAppStore.setState({ conversationId: "live", messages: [{ ...message("live"), isStreaming: true }], isStreaming: true, conversationStreaming: { live: true } });
    const before = useAppStore.getState().conversationStreaming;
    useAppStore.getState().appendThinkingChunk("first", "live", { source: "provider" }, "message-live");
    useAppStore.getState().appendThinkingChunk("second", "live", { source: "provider" }, "message-live");
    expect(useAppStore.getState().conversationStreaming).toBe(before);
    expect(useAppStore.getState().messages[0].blocks).toEqual([expect.objectContaining({ content: "firstsecond" })]);
  });

  afterEach(() => {
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      conversationMessages: {},
      conversationStreaming: {},
      messages: [],
      isStreaming: false,
    });
  });

  it("bounds settled inactive transcripts while pinning active and streaming conversations", () => {
    const ids = Array.from({ length: 12 }, (_, index) => `cache-${index}`);
    const conversations: ConversationMeta[] = ids.map((id, index) => ({
      id,
      title: id,
      updatedAt: new Date(2026, 0, index + 1).toISOString(),
    }));
    useAppStore.setState({
      conversationId: "cache-0",
      conversations,
      conversationMessages: Object.fromEntries(ids.map((id) => [id, [message(id)]])),
      conversationStreaming: { "cache-1": true },
      messages: [message("cache-0")],
      isStreaming: false,
    });

    const cached = useAppStore.getState().conversationMessages;
    expect(cached["cache-0"]).toBeDefined();
    expect(cached["cache-1"]).toBeDefined();
    expect(Object.keys(cached)).toHaveLength(10);
    expect(cached["cache-2"]).toBeUndefined();
    expect(cached["cache-3"]).toBeUndefined();
  });

  it("does not scan transcript LRU state for unrelated UI updates", () => {
    useAppStore.setState({
      conversationId: "ui-cache",
      conversations: [{
        id: "ui-cache",
        title: "UI cache",
        updatedAt: new Date(2026, 0, 1).toISOString(),
      }],
      conversationMessages: { "ui-cache": [message("ui-cache")] },
      conversationStreaming: {},
    });
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_800_000_000_000);

    useAppStore.setState({ settingsOpen: !useAppStore.getState().settingsOpen });

    expect(nowSpy).not.toHaveBeenCalled();
    nowSpy.mockRestore();
  });
});
