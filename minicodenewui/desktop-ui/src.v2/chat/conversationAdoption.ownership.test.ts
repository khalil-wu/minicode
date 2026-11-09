// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import type { ServerEvent } from "../protocol/events";
import { adoptGeneratedConversation } from "./conversationAdoption";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { handleRuntimeEvent } from "./runtimeEvents";

const initial = useAppStore.getState();
const assistant = (id: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id, role: "assistant", content: "", artifacts: [], timestamp: 1,
  isStreaming: true, ...extra,
});
const terminalOld = assistant("old-assistant", {
  content: "durable old answer", isStreaming: false, terminalStatus: "completed", completedAt: 2,
});
const event = (messageId: string, owner = "A"): ServerEvent => ({
  type: "thinking_delta", conversation_id: owner, message_id: messageId,
  turn_id: "incoming-turn", content: "new reasoning",
}) as ServerEvent;
const buffers = () => {
  const thinking = { push: vi.fn(), flush: vi.fn() };
  return {
    thinking,
    handlers: { textStreamBuffer: { flush: vi.fn() }, thinkingStreamBuffer: thinking } as unknown as Parameters<typeof handleChatStreamEvent>[2],
  };
};

beforeEach(() => {
  useAppStore.setState({
    ...initial, conversationId: null, messages: [assistant("new-assistant")],
    conversations: [{ id: "A", title: "old owner", updatedAt: "2026-09-30" }],
    conversationMessages: { A: [terminalOld] }, conversationStreaming: { A: false },
    sideChats: {}, isStreaming: true,
  }, true);
});

describe("first-send owner admission", () => {
  it("does not replace a terminal old owner's cache with the blank new intent", () => {
    const { handlers, thinking } = buffers();
    expect(handleChatStreamEvent(event("old-assistant"), "A", handlers)).toBe(true);
    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.messages.map((m) => m.id)).toEqual(["new-assistant"]);
    expect(state.conversationMessages.A).toEqual([terminalOld]);
    expect(thinking.push).not.toHaveBeenCalled();
  });

  it("keeps a live old event routed to its own cache without adopting the foreground", () => {
    const old = assistant("old-live");
    useAppStore.setState({ conversationMessages: { A: [old] }, conversationStreaming: { A: true } });
    const { handlers, thinking } = buffers();
    handleChatStreamEvent(event("old-live"), "A", handlers);
    expect(useAppStore.getState().conversationId).toBeNull();
    expect(useAppStore.getState().messages[0].id).toBe("new-assistant");
    expect(useAppStore.getState().conversationMessages.A[0].id).toBe("old-live");
    expect(thinking.push.mock.calls[0][1]).toBe("A");
    expect(thinking.push.mock.calls[0][4]).toBe("old-live");
  });

  it("adopts a matching first stream and binds its turn", () => {
    const { handlers, thinking } = buffers();
    handleChatStreamEvent(event("new-assistant", "generated"), "generated", handlers);
    const state = useAppStore.getState();
    expect(state.conversationId).toBe("generated");
    expect(state.conversationMessages.generated[0].id).toBe("new-assistant");
    expect(state.messages[0].turnId).toBe("incoming-turn");
    expect(state.conversationMessages.A).toEqual([terminalOld]);
    expect(thinking.push).toHaveBeenCalled();
  });

  it("does not lose a real main run-start after restore cleared the optimistic streaming flag", () => {
    useAppStore.setState({ messages: [assistant("new-assistant", { isStreaming: false })], isStreaming: false });
    handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "first-run",
      conversation_id: "generated", message_id: "new-assistant" } as ServerEvent, "generated");
    const state = useAppStore.getState();
    expect(state.conversationId).toBe("generated");
    expect(state.messages[0].isStreaming).toBe(true);
    expect(state.messages[0].turnId).toBe("first-run");
  });

  it("does not adopt a stale main run-start for an old message", () => {
    handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "stale-run",
      conversation_id: "A", message_id: "old-assistant" } as ServerEvent, "A");
    expect(useAppStore.getState().conversationId).toBeNull();
    expect(useAppStore.getState().conversationMessages.A).toEqual([terminalOld]);
  });

  it("does not let a child run-start claim the main first-send intent", () => {
    handleRuntimeEvent({ type: "agent.run.started", role: "subagent", run_id: "child-run",
      conversation_id: "generated", message_id: "new-assistant" } as ServerEvent, "generated");
    expect(useAppStore.getState().conversationId).toBeNull();
  });

  it.each([undefined, "", "unrelated"])("requires the exact message intent, not %s", (id) => {
    adoptGeneratedConversation("A", id);
    expect(useAppStore.getState().conversationId).toBeNull();
    expect(useAppStore.getState().conversationMessages.A).toEqual([terminalOld]);
  });

  it.each([
    { terminalStatus: "completed" as const }, { completedAt: 2 }, { queueState: "queued" as const },
  ])("does not adopt a settled or queued foreground message: %j", (extra) => {
    useAppStore.setState({ messages: [assistant("new-assistant", extra)] });
    adoptGeneratedConversation("A", "new-assistant");
    expect(useAppStore.getState().conversationId).toBeNull();
  });

  it("does not replace a different active owner", () => {
    useAppStore.setState({ conversationId: "B" });
    adoptGeneratedConversation("A", "new-assistant");
    expect(useAppStore.getState().conversationId).toBe("B");
    expect(useAppStore.getState().conversationMessages.A).toEqual([terminalOld]);
  });

  it("does not promote a side-chat owner to main foreground", () => {
    useAppStore.setState({ sideChats: { A: { conversationId: "A", messages: [], isStreaming: true } } } as never);
    adoptGeneratedConversation("A", "new-assistant");
    expect(useAppStore.getState().conversationId).toBeNull();
  });
});
