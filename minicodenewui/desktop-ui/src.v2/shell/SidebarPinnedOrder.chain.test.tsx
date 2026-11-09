/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleSessionEvent } from "../chat/sessionEvents";
import type { ServerEvent } from "../protocol/events";
import { ConversationsTab } from "./ConversationsTab";
import { pushToast } from "../overlays/ToastContainer";

const mocks = vi.hoisted(() => ({ send: vi.fn(), switch: vi.fn(async () => true) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: mocks.send,
  commandResultSucceeded: (event: { level: string }) => !["error", "failed"].includes(event.level) }));
vi.mock("../desktop/runtime", () => ({ desktop: () => undefined, isDesktop: () => false, revealPath: vi.fn() }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const records = ["Alpha", "Bravo", "Charlie"].map((title, index) => ({ id: title.toLowerCase(), title, pinned: true,
  pinned_at: `2026-10-15T00:00:0${index}Z`, updated_at: `2026-10-15T00:00:0${index}Z`, workspace_root: "C:/Original", execution_cwd: "C:/Original",
  has_unread_turn: title === "Alpha" }));
let revision = 0;
const publish = (identifiers = ["alpha", "bravo", "charlie"], mode: "manual" | "updated_at" = "manual") => act(() => {
  handleSessionEvent({ type: "conversation.list", inventory_instance_id: "b".repeat(32), inventory_revision: ++revision,
    active_conversation_id: "reader", conversation_id: "reader", pinned_conversation_ids: identifiers, pinned_sort_mode: mode,
    conversations: [...records, { id: "reader", title: "Original reader", updated_at: "2026-10-15", workspace_root: "C:/Original" }],
  } as ServerEvent, { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() }, thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } });
});
const shownIds = () => [...screen.getByRole("region", { name: "已固定" }).querySelectorAll("[data-pinned-sortable-id]")].map(row => row.getAttribute("data-pinned-sortable-id"));
const primaryButton = (title: string) => within(screen.getByRole("region", { name: "已固定" })).getByText(title).closest("button")!;
beforeEach(() => {
  revision = 0; mocks.send.mockReset().mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "success", message: "", data: {} }));
  mocks.switch.mockClear(); vi.mocked(pushToast).mockClear(); localStorage.removeItem("minicode.sidebar.conversations.state");
  useAppStore.setState({ conversationId: "reader", pendingConversationSwitchId: null, pendingConversationCreateId: null, appMode: "cowork",
    requestConversationSwitch: mocks.switch, workingDirectory: "C:/Original", isConnected: false, isStreaming: false, conversationStreaming: {},
    conversations: [], conversationInventoryInstanceId: null, conversationInventoryRevision: 0, pinnedConversationIds: [], pinnedConversationSortMode: "manual",
    draft: "Original reader draft", messages: [], conversationMessages: {}, conversationWorkbenchStates: {}, attachments: [],
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], runtimeSession: null,
    recentWorkspaces: [{ path: "C:/Original", name: "Original", projectType: "unknown", lastOpened: 1 }],
  }); publish();
});
afterEach(cleanup);

it("actual Source Alt-Up gesture routes only global pinned order and authoritative inventory keeps unread and draft", async () => {
  const originalAttachments = useAppStore.getState().attachments;
  render(<ConversationsTab conversationId="reader" onSetConfirmDialog={vi.fn()} />);
  const primary = primaryButton("Bravo"); primary.focus(); fireEvent.keyDown(primary, { key: "ArrowUp", altKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.pin.reorder", moved_conversation_id: "bravo",
    before_conversation_id: "alpha", visible_conversation_ids: ["alpha", "bravo", "charlie"] }, "conversation.pin.reorder", expect.anything()));
  publish(["bravo", "alpha", "charlie"]);
  expect(shownIds()).toEqual(["bravo", "alpha", "charlie"]);
  expect(mocks.switch).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", workingDirectory: "C:/Original", draft: "Original reader draft" });
  expect(useAppStore.getState().conversations.find(record => record.id === "alpha")?.hasUnreadTurn).toBe(true);
  expect(useAppStore.getState().conversations.find(record => record.id === "alpha")?.pinnedAt).toBe(records[0].pinned_at);
  expect(useAppStore.getState().attachments).toBe(originalAttachments);
  expect(screen.getByRole("region", { name: "工作区 Original" }).querySelector("[data-pinned-sortable-id]")).toBeNull();
});

it("a Source move from recency order records the actual visible order and then becomes manual", async () => {
  publish(["alpha", "bravo", "charlie"], "updated_at");
  render(<ConversationsTab conversationId="reader" onSetConfirmDialog={vi.fn()} />);
  expect(shownIds()).toEqual(["charlie", "bravo", "alpha"]);
  const primary = primaryButton("Charlie"); primary.focus(); fireEvent.keyDown(primary, { key: "ArrowDown", altKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ moved_conversation_id: "charlie", before_conversation_id: "alpha",
    visible_conversation_ids: ["charlie", "bravo", "alpha"] }), "conversation.pin.reorder", expect.anything()));
  publish(["bravo", "charlie", "alpha"], "manual");
  expect(shownIds()).toEqual(["bravo", "charlie", "alpha"]);
  expect(useAppStore.getState().pinnedConversationSortMode).toBe("manual");
});

it("a rejected real reorder restores canonical order and never mutates the reader or unread flag", async () => {
  mocks.send.mockResolvedValue({ type: "command.result", command: "conversation.pin.reorder", level: "error", message: "Pinned membership changed", data: {} });
  render(<ConversationsTab conversationId="reader" onSetConfirmDialog={vi.fn()} />);
  const primary = primaryButton("Bravo"); primary.focus(); fireEvent.keyDown(primary, { key: "ArrowUp", altKey: true });
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Pinned membership changed", "error", 6000));
  expect(shownIds()).toEqual(["alpha", "bravo", "charlie"]);
  expect(useAppStore.getState().draft).toBe("Original reader draft");
  expect(useAppStore.getState().conversations.find(record => record.id === "alpha")?.hasUnreadTurn).toBe(true);
});

it("older order snapshots cannot replace the current canonical ordering", () => {
  publish(["charlie", "alpha", "bravo"]);
  const latest = useAppStore.getState().conversationInventoryRevision;
  revision = latest - 2; publish(["alpha", "bravo", "charlie"]);
  expect(useAppStore.getState().pinnedConversationIds).toEqual(["charlie", "alpha", "bravo"]);
});
