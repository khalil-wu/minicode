/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ConversationsTab } from "./ConversationsTab";
import { handleSessionEvent } from "../chat/sessionEvents";
import type { ServerEvent } from "../protocol/events";

const { send } = vi.hoisted(() => ({ send: vi.fn(async (command: { type: string }) => ({
  type: "command.result", command: command.type, level: "success", message: "", data: {},
})) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: send,
  commandResultSucceeded: (event: { level: string }) => event.level !== "error" }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => false, revealPath: vi.fn() }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: vi.fn() }));

const record = (extra = {}) => ({ id: "task", title: "Actual task", updated_at: "2026-10-14T01:00:00Z", revision: 1,
  workspace_root: "C:/A", execution_cwd: "C:/A", pinned: false, has_unread_turn: false, project_id: null, ...extra });
let revision = 0;
const publish = (extra = {}) => act(() => {
  handleSessionEvent({ type: "conversation.list", conversation_id: "reader", active_conversation_id: "reader",
    inventory_instance_id: "a".repeat(32), inventory_revision: ++revision, conversations: [record(extra), {
      id: "reader", title: "Current reader", updated_at: "2026-10-14T00:00:00Z", workspace_root: "C:/B", execution_cwd: "C:/B",
    }] } as ServerEvent, { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() },
      thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } });
});
beforeEach(() => {
  revision = 0;
  send.mockClear();
  localStorage.removeItem("minicode.sidebar.conversations.state");
  useAppStore.setState({ conversationId: "reader", workingDirectory: "C:/B", appMode: "cowork", draft: "Original reader draft",
    messages: [], conversations: [], conversationMessages: {}, isConnected: false, isStreaming: false, conversationStreaming: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null, conversationInventoryInstanceId: null, conversationInventoryRevision: 0,
    pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [], runtimeSession: null,
    recentWorkspaces: [{ path: "C:/A", name: "A", projectType: "unknown", lastOpened: 1 }, { path: "C:/B", name: "B", projectType: "unknown", lastOpened: 2 }] });
  publish();
});
afterEach(cleanup);
const show = () => render(<ConversationsTab conversationId="reader" onSetConfirmDialog={vi.fn()} />);
const menuFor = (region: HTMLElement) => {
  const row = within(region).getByText("Actual task").closest('[data-session-row="true"]')!;
  fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "会话操作", exact: true }));
};

it("pins the captured background task only after authoritative inventory and unpins back to its project", async () => {
  show();
  menuFor(screen.getByRole("region", { name: "工作区 A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "固定", exact: true }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: "conversation.pin.set", conversation_id: "task", pinned: true }, "conversation.pin.set", expect.anything()));
  expect(screen.queryByRole("region", { name: "已固定" })).toBeNull();
  publish({ revision: 2, pinned: true, pinned_at: "2026-10-14T02:00:00Z" });
  const pinned = screen.getByRole("region", { name: "已固定" });
  expect(within(pinned).getByText("Actual task")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "最近" })).queryByText("Actual task")).toBeNull();
  expect(within(screen.getByRole("region", { name: "工作区 A" })).queryByText("Actual task")).toBeNull();
  fireEvent.click(within(pinned).getByRole("button", { name: "取消固定", exact: true }));
  publish({ revision: 3, pinned: false });
  expect(screen.queryByRole("region", { name: "已固定" })).toBeNull();
  expect(within(screen.getByRole("region", { name: "工作区 A" })).getByText("Actual task")).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("Original reader draft");
  expect(useAppStore.getState().conversationId).toBe("reader");
});

it("projects real unread metadata and clears it when the user opens that row", async () => {
  show();
  menuFor(screen.getByRole("region", { name: "工作区 A" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "标记为未读", exact: true }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: "conversation.read_state.set", conversation_id: "task", has_unread_turn: true }, "conversation.read_state.set", expect.anything()));
  publish({ revision: 2, has_unread_turn: true });
  const project = screen.getByRole("region", { name: "工作区 A" });
  expect(within(project).getByLabelText("未读")).toBeTruthy();
  menuFor(project);
  expect(screen.getByRole("menuitem", { name: "标记为已读", exact: true })).toBeTruthy();
  fireEvent.click(screen.getByRole("menuitem", { name: "标记为已读", exact: true }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: "conversation.read_state.set", conversation_id: "task", has_unread_turn: false }, "conversation.read_state.set", expect.anything()));
  publish({ revision: 3, has_unread_turn: true });
  fireEvent.click(within(project).getByRole("button", { name: "Actual task", exact: true }));
  expect(send).toHaveBeenLastCalledWith({ type: "conversation.read_state.set", conversation_id: "task", has_unread_turn: false }, "conversation.read_state.set", expect.anything());
});

it("moves the pinned task to a real project then removes membership while preserving execution and the reader", async () => {
  publish({ revision: 2, pinned: true, pinned_at: "2026-10-14T02:00:00Z" });
  show();
  menuFor(screen.getByRole("region", { name: "已固定" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "项目", exact: true }));
  expect(screen.queryByRole("menuitem", { name: "A", exact: true })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "B", exact: true }));
  await waitFor(() => expect(send).toHaveBeenCalledWith({ type: "conversation.project.set", conversation_id: "task", project_id: "c:/b" }, "conversation.project.set", expect.anything()));
  publish({ revision: 3, pinned: false, project_id: "C:/B" });
  const target = screen.getByRole("region", { name: "工作区 B" });
  expect(within(target).getByText("Actual task")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "工作区 A" })).queryByText("Actual task")).toBeNull();
  expect(useAppStore.getState().conversations.find(item => item.id === "task")?.executionCwd).toBe("C:/A");
  menuFor(target);
  fireEvent.click(screen.getByRole("menuitem", { name: "项目", exact: true }));
  fireEvent.click(screen.getByRole("menuitem", { name: "移出项目", exact: true }));
  publish({ revision: 4, project_id: "" });
  expect(within(target).queryByText("Actual task")).toBeNull();
  expect(within(screen.getByRole("region", { name: "最近" })).getByText("Actual task")).toBeTruthy();
  expect(useAppStore.getState().workingDirectory).toBe("C:/B");
  expect(useAppStore.getState().conversationId).toBe("reader");
  expect(useAppStore.getState().draft).toBe("Original reader draft");
});
