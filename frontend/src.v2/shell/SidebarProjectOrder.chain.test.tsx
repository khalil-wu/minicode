/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleSessionEvent } from "../chat/sessionEvents";
import type { ServerEvent, SidebarOrderPayload } from "../protocol/events";
import { ConversationsTab } from "./ConversationsTab";
import { pushToast } from "../overlays/ToastContainer";

const mocks = vi.hoisted(() => ({ send: vi.fn(), switch: vi.fn(async () => true) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: mocks.send,
  commandResultSucceeded: (event: { level: string }) => !["error", "failed"].includes(event.level) }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, revealPath: vi.fn() }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const records = ["alpha", "hidden", "bravo", "charlie", "other", "pinned"].map((id, index) => ({
  id, title: id, workspace_root: id === "other" ? "C:/Other" : "C:/Alpha", updated_at: `2026-10-15T00:00:0${index}Z`,
  pinned: id === "pinned", archived: id === "hidden", has_unread_turn: id === "alpha",
}));
const initialOrder = (mode: "manual" | "updated_at" = "manual"): SidebarOrderPayload => ({ revision: 1, sort_mode: mode,
  project_order: ["c:/alpha", "c:/other"], project_thread_orders: { "c:/alpha": ["alpha", "hidden", "bravo", "charlie", "pinned"], "c:/other": ["other"] },
  recent_thread_ids: ["charlie", "alpha", "bravo", "other", "hidden", "pinned"],
});
let revision = 0;
const publish = (sidebarOrder = initialOrder()) => act(() => {
  handleSessionEvent({ type: "conversation.list", inventory_instance_id: "c".repeat(32), inventory_revision: ++revision,
    active_conversation_id: "alpha", conversation_id: "alpha", pinned_conversation_ids: ["pinned"], pinned_sort_mode: "manual",
    sidebar_order: sidebarOrder, conversations: records,
  } as ServerEvent, { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() }, thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } });
});
const project = () => screen.getByRole("region", { name: "工作区 Alpha" });
const recent = () => screen.getByRole("region", { name: "最近" });
const rowTitles = (region: HTMLElement) => [...region.querySelectorAll("[data-session-row]")].map(row => row.querySelector("button")!.textContent!.trim());
const primary = (region: HTMLElement, title: string) => within(region).getByRole("button", { name: title, exact: true });
const openSort = (section: "项目" | "聊天") => {
  fireEvent.click(screen.getByRole("button", { name: `${section}侧栏选项` }));
  fireEvent.click(screen.getByRole("menuitem", { name: "排序方式" }));
};
beforeEach(() => {
  revision = 0; mocks.send.mockReset().mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "success", message: "", data: {} }));
  mocks.switch.mockClear(); vi.mocked(pushToast).mockClear(); localStorage.removeItem("minicode.sidebar.conversations.state");
  useAppStore.setState({ conversationId: "alpha", pendingConversationSwitchId: null, pendingConversationCreateId: null, appMode: "cowork",
    requestConversationSwitch: mocks.switch, workingDirectory: "C:/Alpha", isConnected: false, isStreaming: false, conversationStreaming: {},
    conversations: [], conversationInventoryInstanceId: null, conversationInventoryRevision: 0, pinnedConversationIds: [], pinnedConversationSortMode: "manual", sidebarOrder: null,
    draft: "Original unsent reader draft", messages: [], conversationMessages: {}, conversationWorkbenchStates: {}, attachments: [],
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], runtimeSession: null,
    recentWorkspaces: [{ path: "C:/Alpha", name: "Alpha", projectType: "unknown", lastOpened: 1 }, { path: "C:/Other", name: "Other", projectType: "unknown", lastOpened: 2 }],
  }); publish();
});
afterEach(cleanup);

it("Source manual project Alt move addresses only that occurrence and canonical inventory preserves the reader", async () => {
  const original = useAppStore.getState();
  render(<ConversationsTab conversationId="alpha" onSetConfirmDialog={vi.fn()} />);
  expect(rowTitles(project())).toEqual(["alpha", "bravo", "charlie"]);
  const button = primary(project(), "bravo"); button.focus(); fireEvent.keyDown(button, { key: "ArrowUp", altKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "sidebar.project.thread.reorder", project_id: "C:/Alpha",
    moved_conversation_id: "bravo", before_conversation_id: "alpha", visible_conversation_ids: ["alpha", "bravo", "charlie"] }, "sidebar.project.thread.reorder", expect.anything()));
  publish({ ...initialOrder(), revision: 2, project_thread_orders: { "c:/alpha": ["bravo", "hidden", "alpha", "charlie", "pinned"], "c:/other": ["other"] } });
  expect(rowTitles(project())).toEqual(["bravo", "alpha", "charlie"]);
  expect(rowTitles(recent())).toEqual(["charlie", "alpha", "bravo", "other"]);
  expect(useAppStore.getState().pinnedConversationIds).toEqual(["pinned"]);
  expect(useAppStore.getState().attachments).toBe(original.attachments);
  expect(useAppStore.getState().conversations.find(item => item.id === "alpha")?.hasUnreadTurn).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "alpha", workingDirectory: "C:/Alpha", draft: original.draft });
  expect(mocks.switch).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(button);
});

it("Source Recents uses its own manual order and does not write project order", async () => {
  render(<ConversationsTab conversationId="alpha" onSetConfirmDialog={vi.fn()} />);
  const button = primary(recent(), "charlie"); button.focus(); fireEvent.keyDown(button, { key: "ArrowDown", altKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "sidebar.recent.reorder", moved_conversation_id: "charlie",
    before_conversation_id: "bravo", visible_conversation_ids: ["charlie", "alpha", "bravo", "other"] }, "sidebar.recent.reorder", expect.anything()));
  publish({ ...initialOrder(), revision: 2, recent_thread_ids: ["alpha", "charlie", "bravo", "other", "hidden", "pinned"] });
  expect(rowTitles(recent())).toEqual(["alpha", "charlie", "bravo", "other"]);
  expect(rowTitles(project())).toEqual(["alpha", "bravo", "charlie"]);
});

it("project titles reorder independently while recent activity blocks only chat occurrence reordering", async () => {
  publish(initialOrder("updated_at"));
  render(<ConversationsTab conversationId="alpha" onSetConfirmDialog={vi.fn()} />);
  const chat = primary(project(), "bravo"); chat.focus(); fireEvent.keyDown(chat, { key: "ArrowUp", altKey: true });
  expect(mocks.send).not.toHaveBeenCalled();
  const heading = screen.getByRole("button", { name: "Other", exact: true }); heading.focus(); fireEvent.keyDown(heading, { key: "ArrowUp", altKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "sidebar.project.reorder", moved_project_id: "C:/Other",
    before_project_id: "C:/Alpha", visible_project_ids: ["C:/Alpha", "C:/Other"] }, "sidebar.project.reorder", expect.anything()));
  publish({ ...initialOrder("updated_at"), revision: 2, project_order: ["c:/other", "c:/alpha"] });
  expect([...screen.getByRole("list", { name: "项目顺序" }).children].map(slot => slot.querySelector("section")!.getAttribute("aria-label")))
    .toEqual(["工作区 Other", "工作区 Alpha"]);
  expect(useAppStore.getState().sidebarOrder?.sort_mode).toBe("updated_at");
  expect(useAppStore.getState().recentWorkspaces.map(item => item.path)).toEqual(["C:/Alpha", "C:/Other"]);
});

it("both actual Source options menus share the same sort action and expose real radio semantics", async () => {
  publish(initialOrder("updated_at"));
  mocks.send.mockImplementation(async (command: { type: string }) => {
    if (command.type === "sidebar.sort.set") publish({ ...initialOrder("manual"), revision: 2 });
    return { type: "command.result", command: command.type, level: "success", message: "", data: {} };
  });
  render(<ConversationsTab conversationId="alpha" onSetConfirmDialog={vi.fn()} />);
  openSort("项目");
  expect(screen.getByRole("menuitemradio", { name: "最近活动" }).getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("menuitemradio", { name: "手动" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "sidebar.sort.set", mode: "manual", project_thread_seeds: {
    "C:/Alpha": ["charlie", "bravo", "alpha"], "C:/Other": ["other"],
  } }, "sidebar.sort.set", expect.anything()));
  openSort("聊天");
  const manual = screen.getByRole("menuitemradio", { name: "手动" });
  expect(manual.getAttribute("aria-checked")).toBe("true");
  manual.focus();
  fireEvent.keyDown(manual, { key: "ArrowUp" });
  expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "最近活动" }));
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "聊天侧栏选项" }));
});

it("a rejected reorder restores canonical order without changing drafts, unread or another occurrence", async () => {
  mocks.send.mockResolvedValue({ type: "command.result", command: "sidebar.project.thread.reorder", level: "error", message: "Project membership changed", data: {} });
  render(<ConversationsTab conversationId="alpha" onSetConfirmDialog={vi.fn()} />);
  fireEvent.keyDown(primary(project(), "bravo"), { key: "ArrowUp", altKey: true });
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Project membership changed", "error", 6000));
  expect(rowTitles(project())).toEqual(["alpha", "bravo", "charlie"]);
  expect(rowTitles(recent())).toEqual(["charlie", "alpha", "bravo", "other"]);
  expect(useAppStore.getState().draft).toBe("Original unsent reader draft");
});

it("an older sidebar receipt cannot roll back newer sort state even inside a newer conversation inventory", () => {
  publish({ ...initialOrder(), revision: 9, project_order: ["c:/other", "c:/alpha"] });
  publish({ ...initialOrder("updated_at"), revision: 8 });
  expect(useAppStore.getState().sidebarOrder).toMatchObject({ revision: 9, sort_mode: "manual", project_order: ["c:/other", "c:/alpha"] });
});
