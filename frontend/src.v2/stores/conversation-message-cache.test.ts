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
import { cacheMessagesForConversation } from "./shared-helpers";

const message = (id: string): ChatMessage => ({
  id: `message-${id}`,
  role: "assistant",
  content: id,
  artifacts: [],
  timestamp: 1,
});

describe("conversation transcript renderer cache", () => {
  it("retains unchanged run flags when snapshotting messages and publishes actual start/stop transitions", () => {
    useAppStore.setState({ conversationId: "cache-owner", conversationStreaming: { "cache-owner": false }, messages: [], isStreaming: false });
    const state = useAppStore.getState();
    expect(cacheMessagesForConversation(state, "cache-owner", [message("one")], false).conversationStreaming).toBe(state.conversationStreaming);
    const started = cacheMessagesForConversation(state, "cache-owner", [message("two")], true);
    expect(started.conversationStreaming).toEqual({ "cache-owner": true });
    expect(cacheMessagesForConversation({ ...state, ...started }, "cache-owner", [], false).conversationStreaming).toEqual({ "cache-owner": false });
  });
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

  it("does not enumerate pinned transcript caches for text events and prunes at the actual stop boundary", () => {
    const settled = Array.from({ length: 8 }, (_, index) => `settled-${index}`);
    const main = [{ ...message("main"), isStreaming: true }];
    const background = [{ ...message("background"), isStreaming: true }];
    const cached = { ...Object.fromEntries(settled.map(id => [id, [message(id)]])), main, background };
    useAppStore.setState({ conversationId: "main", messages: main, isStreaming: true, conversationMessages: cached,
      conversationStreaming: { main: true, background: true }, sideChats: {},
      conversationHistoryPages: Object.fromEntries(Object.keys(cached).map(id => [id, { loading: false, hasMore: true, beforeMessageId: id }])),
      conversations: settled.map((id, index) => ({ id, title: id, updatedAt: new Date(2026, 0, index + 1).toISOString() })),
    });
    const entries = Object.entries, keys = Object.keys;
    let enumerations = 0;
    const entriesSpy = vi.spyOn(Object, "entries").mockImplementation(value => {
      if (value === useAppStore.getState().conversationMessages) enumerations += 1;
      return entries(value);
    });
    const keysSpy = vi.spyOn(Object, "keys").mockImplementation(value => {
      if (value === useAppStore.getState().conversationMessages) enumerations += 1;
      return keys(value);
    });
    try {
      for (let index = 0; index < 50; index += 1) {
        useAppStore.getState().appendAgentMessageDelta("reply-main", "word ", "main", "message-main", "reply");
        useAppStore.getState().appendAgentMessageDelta("reply-background", "word ", "background", "message-background", "reply");
      }
      expect(enumerations).toBe(0);
      useAppStore.getState().finishStreaming("background", undefined, "completed", "message-background");
      expect(enumerations).toBeGreaterThan(0);
    } finally { entriesSpy.mockRestore(); keysSpy.mockRestore(); }
    const current = useAppStore.getState();
    expect(current.messages[0].blocks?.some(block => block.type === "text" && block.content === "word ".repeat(50))).toBe(true);
    expect(current.conversationMessages.background[0]).toMatchObject({ content: "word ".repeat(50), isStreaming: false });
    expect(Object.keys(current.conversationMessages)).toHaveLength(9);
    expect(current.conversationMessages["settled-0"]).toBeUndefined();
    expect(current.conversationHistoryPages["settled-0"]).toBeUndefined();
    expect(current.conversationHistoryPages.background).toBeDefined();
  });

  it("counts a newly cached running owner, then preserves settled-history recency and owner switches", () => {
    let time = 1_800_000_000_000;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => time);
    try {
      const ids = Array.from({ length: 8 }, (_, index) => `recent-${index}`);
      const main = [{ ...message("recency-main"), isStreaming: true }];
      useAppStore.setState({ conversationId: "recency-main", messages: main, isStreaming: true, sideChats: {},
        conversationMessages: { ...Object.fromEntries(ids.map(id => [id, [message(id)]])), "recency-main": main },
        conversationStreaming: { "recency-main": true, "new-runner": true },
        conversations: ids.map((id, index) => ({ id, title: id, updatedAt: new Date(2026, 0, index + 1).toISOString() })),
      });
      time += 1000;
      for (let index = 0; index < 3; index += 1) useAppStore.getState().appendAgentMessageDelta("recency-reply", "word ", "recency-main", "message-recency-main", "reply");
      useAppStore.setState(state => cacheMessagesForConversation(state, "recent-0", [message("loaded-history-page")], false));
      time += 1000;
      useAppStore.setState(state => cacheMessagesForConversation(state, "new-runner", [{ ...message("new-runner"), isStreaming: true }], true));
      expect(Object.keys(useAppStore.getState().conversationMessages)).toHaveLength(10);
      useAppStore.getState().appendAgentMessageDelta("runner-reply", "actual result", "new-runner", "message-new-runner", "reply");
      time += 1000;
      useAppStore.getState().finishStreaming("new-runner", undefined, "completed", "message-new-runner");
      expect(useAppStore.getState().conversationMessages["recent-0"]?.[0].content).toBe("loaded-history-page");
      expect(useAppStore.getState().conversationMessages["recent-1"]).toBeUndefined();
      expect(useAppStore.getState().conversationMessages["new-runner"]?.[0].content).toBe("actual result");
      time += 1000;
      useAppStore.setState({ conversationId: "recent-0", messages: useAppStore.getState().conversationMessages["recent-0"], isStreaming: false });
      useAppStore.getState().finishStreaming("recency-main", undefined, "completed", "message-recency-main");
      expect(useAppStore.getState().conversationMessages["recent-0"]).toBeDefined();
      expect(useAppStore.getState().conversationMessages["recency-main"]).toBeDefined();
      time += 1000;
      useAppStore.setState(state => cacheMessagesForConversation(state, "new-settled", [message("new-settled")], false));
      expect(useAppStore.getState().conversationMessages["recent-2"]).toBeUndefined();
    } finally { clock.mockRestore(); }
  });
});
