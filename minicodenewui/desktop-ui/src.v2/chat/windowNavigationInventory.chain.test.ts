// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleSessionEvent } from "./sessionEvents";
import { useAppStore } from "../stores";
import { LS, writeLS } from "../stores/shared-helpers";
import { sendClientCommand } from "../protocol/ws-outbox";
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
const inventory = { type: "conversation.list", active_conversation_id: null, active_conversation: null,
  conversations: [{ id: "existing-task", title: "Existing", updated_at: "2026-10-15", workspace_root: "C:/workspace" }],
  session: { session_id: "native-window-session", active_conversation_id: null, pending_approval_count: 0 } } as never;
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks();
  useAppStore.setState({ conversationId: null, pendingConversationSwitchId: null, pendingConversationCreateId: null,
    conversations: [], messages: [], conversationMessages: {}, conversationStreaming: {}, conversationWorkbenchStates: {},
    conversationInventoryInstanceId: null, conversationInventoryRevision: 0, isStreaming: false, workingDirectory: "C:/workspace",
    draft: "HOME_UNSENT", attachments: [], runtimeSession: null });
});
afterEach(() => { localStorage.clear(); });

it("keeps an explicit native Home route through initial and repeated inventory without auto-selecting the first thread", () => {
  writeLS(LS.conversation.activeId, "");
  handleSessionEvent(inventory, buffers); handleSessionEvent(inventory, buffers);
  expect(useAppStore.getState()).toMatchObject({ conversationId: null, draft: "HOME_UNSENT" });
  expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch" }));
});

it("retains the existing canonical fallback request when a previously active thread was actually removed", () => {
  writeLS(LS.conversation.activeId, "");
  useAppStore.setState({ conversationId: "removed-task", conversations: [{ id: "removed-task", title: "Removed", updatedAt: "2026-10-14" }] });
  handleSessionEvent(inventory, buffers);
  expect(useAppStore.getState().conversationId).toBeNull();
  expect(sendClientCommand).toHaveBeenCalledWith({ type: "conversation.switch", conversation_id: "existing-task" });
});
