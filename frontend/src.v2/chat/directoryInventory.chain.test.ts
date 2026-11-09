/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleSessionEvent, toConversationMeta } from "./sessionEvents";
import { publishDirectoryPage } from "../protocol/conversation-directory";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import type { StreamBuffer } from "../lib/stream-buffer";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "original-session" }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const buffers = { textStreamBuffer: { flush: vi.fn(), destroy: vi.fn(), push: vi.fn() } as unknown as StreamBuffer,
  thinkingStreamBuffer: { flush: vi.fn(), destroy: vi.fn(), push: vi.fn() } as unknown as StreamBuffer };

beforeEach(() => {
  useAppStore.setState({ conversationId: "visible", conversations: [
    { id: "visible", title: "Visible", updatedAt: "2026-11-07", revision: 2 },
    { id: "hidden", title: "Hidden", updatedAt: "2026-11-06", revision: 4 },
  ], conversationInventoryInstanceId: "original-store", conversationInventoryRevision: 5,
  conversationDirectory: { nextCursor: "already-paged", projectCursors: {} }, sidebarOrder: null,
  pendingConversationSwitchId: null, pendingConversationCreateId: null, activeConversationDraftId: null,
  conversationMessages: { hidden: [{ id: "saved", role: "user", content: "Hidden message", timestamp: 1 }] },
  conversationStreaming: { hidden: true }, conversationWorkbenchStates: {}, pendingApproval: null, approvalQueue: [],
  pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [],
  draft: "Unsent visible draft", runtimeSession: null, isStreaming: false,
  });
});

it("a partial refresh keeps unloaded messages and running work, and only explicit deletions retire them", () => {
  const messages = useAppStore.getState().conversationMessages.hidden;
  handleSessionEvent({ type: "conversation.list", partial_inventory: true, inventory_instance_id: "original-store", inventory_revision: 6,
    active_conversation_id: "visible", conversations: [{ id: "visible", title: "Visible", updated_at: "2026-11-07", revision: 2 }],
    directory_page: { next_cursor: "first-page", project_cursors: {} }, removed_conversation_ids: [] }, buffers);
  expect(useAppStore.getState().conversations.map(item => item.id)).toEqual(["visible", "hidden"]);
  expect(useAppStore.getState().conversationMessages.hidden).toBe(messages);
  expect(useAppStore.getState().conversationStreaming.hidden).toBe(true);
  expect(useAppStore.getState().draft).toBe("Unsent visible draft");
  expect(useAppStore.getState().conversationDirectory?.nextCursor).toBe("already-paged");
  handleSessionEvent({ type: "conversation.list", partial_inventory: true, inventory_instance_id: "original-store", inventory_revision: 7,
    active_conversation_id: "visible", conversations: [{ id: "visible", title: "Visible", updated_at: "2026-11-07", revision: 2 }],
    removed_conversation_ids: ["hidden"] }, buffers);
  expect(useAppStore.getState().conversations.map(item => item.id)).toEqual(["visible"]);
  expect(useAppStore.getState().conversationMessages.hidden).toBeUndefined();
  expect(useAppStore.getState().draft).toBe("Unsent visible draft");
});

it("an explicitly opened attachment keeps its composer and running state across ordinary inventory", () => {
  const attached = toConversationMeta({ id: "hidden", title: "Attached task", attached_to_thread_id: "visible", revision: 4 });
  useAppStore.setState(state => ({ conversationId: "hidden", conversations: [state.conversations[0], attached],
    draft: "Unsent attached draft", isStreaming: true }));
  const messages = useAppStore.getState().conversationMessages.hidden;
  const event = normalizeInboundServerEvent({ type: "conversation.list", partial_inventory: true,
    inventory_instance_id: "original-store", inventory_revision: 6,
    active_conversation_id: "hidden", conversations: [{ id: "visible", title: "Visible", revision: 2 }],
    active_conversation: { id: "hidden", title: "Renamed attached task", attached_to_thread_id: "visible", revision: 5 },
    removed_conversation_ids: [] });
  expect(event).not.toBeNull();
  handleSessionEvent(event!, buffers);
  const state = useAppStore.getState();
  expect(state.conversationId).toBe("hidden");
  expect(state.conversations.find(item => item.id === "hidden")?.attachedToThreadId).toBe("visible");
  expect(state.conversations.find(item => item.id === "hidden")?.title).toBe("Renamed attached task");
  expect(state.conversationMessages.hidden).toBe(messages);
  expect(state.conversationStreaming.hidden).toBe(true);
  expect(state.draft).toBe("Unsent attached draft");
});

it("an appended page keeps newer per-thread metadata and never changes the active composer", () => {
  const page = { data: [{ id: "hidden", title: "Stale row", revision: 3 }, { id: "older", title: "Older", revision: 1 }],
    next_cursor: "next-page", backwards_cursor: "previous-page", inventory_instance_id: "original-store", inventory_revision: 6 };
  expect(publishDirectoryPage(page, "original-session", {})).toBe(true);
  const state = useAppStore.getState();
  expect(state.conversations.find(item => item.id === "hidden")?.title).toBe("Hidden");
  expect(state.conversations.find(item => item.id === "older")?.title).toBe("Older");
  expect(state.conversationDirectory?.nextCursor).toBe("next-page");
  expect(state.conversationInventoryRevision).toBe(5);
  expect(state.conversationId).toBe("visible");
  expect(state.draft).toBe("Unsent visible draft");
  expect(state.conversationMessages.hidden[0].content).toBe("Hidden message");
});

it.each(["session", "store", "revision"])("rejects a page from an obsolete %s", change => {
  const before = useAppStore.getState().conversations;
  expect(publishDirectoryPage({ data: [{ id: "wrong", title: "Wrong owner" }], next_cursor: null, backwards_cursor: null,
    inventory_instance_id: change === "store" ? "another-store" : "original-store", inventory_revision: change === "revision" ? 4 : 6 },
  change === "session" ? "another-session" : "original-session", {})).toBe(false);
  expect(useAppStore.getState().conversations).toBe(before);
});
