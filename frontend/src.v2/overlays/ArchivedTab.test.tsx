/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ArchivedTab } from "./ArchivedTab";

const { readArchive, readSearch, command, confirm, deleteCommand, sent, toast } = vi.hoisted(() => ({
  readArchive: vi.fn(), readSearch: vi.fn(), command: vi.fn(), confirm: vi.fn(), deleteCommand: vi.fn(), sent: vi.fn(), toast: vi.fn(),
}));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://archive.test", authHeaders: () => ({}), errorMessageFromResponseText: (text: string) => text,
  fetchWithTimeout: (url: URL, init: RequestInit) => url.pathname === "/api/conversations/search" ? readSearch(url, init) : readArchive(url, init) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "read-session" }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: command, sendClientCommand: sent,
  sendConversationDeleteCommand: deleteCommand, commandResultSucceeded: (result: { level: string }) => result.level !== "error" }));
vi.mock("./DialogService", () => ({ showConfirm: confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: toast }));

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "active", pendingConversationSwitchId: null, activeConversationDraftId: null,
    appMode: "cowork", settingsOpen: true, draft: "unsent current owner text", workingDirectory: "C:/active",
    recentWorkspaces: [], runtimeCapabilities: null, isConnected: true, conversations: [
      { id: "archive-A", title: "检查项目", archived: true, workspaceRoot: "C:/alpha", createdAt: "2026-09-01T08:00:00Z", updatedAt: "2026-10-01T08:00:00Z" },
      { id: "archive-B", title: "检查项目", archived: true, workspaceRoot: "C:/beta", createdAt: "2026-09-04T08:00:00Z", updatedAt: "2026-10-04T08:00:00Z" },
    ] });
  readSearch.mockResolvedValue({ ok: true, json: async () => ({ data: [], next_cursor: null }) });
  command.mockResolvedValue({ level: "success" });
  confirm.mockResolvedValue(false);
  deleteCommand.mockResolvedValue(true);
  sent.mockReturnValue(true);
});

afterEach(cleanup);

it("finds archived body-only matches without reading or reprojecting their transcript", async () => {
  readSearch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: [{
    conversation: { id: "archive-A", title: "检查项目", archived: true, workspace_root: "C:/alpha", updated_at: "2026-10-01" },
    message_id: "old-body", snippet: "buried body-only phrase",
  }], next_cursor: null }) });
  const drafts = useAppStore.getState().conversationDrafts;
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "body-only phrase" } });
  const match = await screen.findByText("检查项目");
  expect(readSearch.mock.calls[0][0].searchParams.get("session_id")).toBe("read-session");
  expect(readSearch.mock.calls[0][0].searchParams.get("archived")).toBe("true");
  expect(readSearch.mock.calls[0][0].searchParams.get("query")).toBe("body-only phrase");
  fireEvent.click(match);
  expect(screen.queryByRole("button", { name: "检查项目" })).toBeNull();
  expect(readArchive).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "active", workingDirectory: "C:/active", draft: "unsent current owner text" });
  expect(useAppStore.getState().conversationDrafts).toBe(drafts);
  expect(command).not.toHaveBeenCalled();
  expect(sent).not.toHaveBeenCalled();
});

it("binds archived body requests to the selected project and sort without clearing the input", async () => {
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "needle" } });
  fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "alpha" }));
  fireEvent.click(screen.getByRole("button", { name: "归档类型和排序" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "创建时间" }));
  await waitFor(() => expect(readSearch).toHaveBeenCalled());
  const url = readSearch.mock.calls.at(-1)![0] as URL;
  expect(url.searchParams.get("project")).toBe("C:/alpha");
  expect(url.searchParams.get("sort")).toBe("created");
  expect(screen.getByLabelText("搜索已归档任务")).toHaveProperty("value", "needle");
});

it("disambiguates duplicate titles by project and omits the redundant selected-project header", () => {
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "alpha" } });
  expect(screen.getAllByText("检查项目")).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "beta" }));
  expect(screen.getAllByText("检查项目")).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "beta" })).toBeNull();
  expect(screen.getByText(/ • beta$/)).toBeTruthy();
  expect(screen.queryByLabelText("归档最后活动开始日期")).toBeNull();
});

it("leaves the active owner and draft intact when unarchiving, navigating only through View", async () => {
  const drafts = useAppStore.getState().conversationDrafts;
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "恢复 检查项目" }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({ type: "conversation.unarchive", conversation_id: "archive-A", archived: false }, "conversation.unarchive"));
  await waitFor(() => expect(toast).toHaveBeenCalledWith("已恢复任务：检查项目", "success", undefined, expect.objectContaining({ id: "unarchive-thread-archive-A", label: "查看" })));
  act(() => useAppStore.setState((state) => ({ conversations: state.conversations.map((conversation) => conversation.id === "archive-A" ? { ...conversation, archived: false } : conversation) })));
  expect(screen.getByLabelText("搜索已归档任务")).toHaveProperty("value", "alpha");
  expect(await screen.findByText("没有匹配的任务")).toBeTruthy();
  expect(sent).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "active", draft: "unsent current owner text", settingsOpen: true });
  act(() => toast.mock.calls[0][3].onClick());
  expect(sent).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "archive-A" }));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "active", pendingConversationSwitchId: "archive-A", draft: "unsent current owner text", settingsOpen: false });
  expect(useAppStore.getState().conversationDrafts).toBe(drafts);
});

it("keeps a rejected unarchive visible without offering navigation", async () => {
  command.mockResolvedValueOnce({ level: "error", message: "Workspace unavailable" });
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "恢复 检查项目" })[0]);
  await waitFor(() => expect(toast).toHaveBeenCalledWith("恢复任务失败：Workspace unavailable", "error"));
  expect(screen.getAllByText("检查项目")).toHaveLength(2);
  expect(sent).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "active", draft: "unsent current owner text" });
});

it("sorts by real creation time and groups isolated worktrees under their project", () => {
  useAppStore.setState({ conversations: [
    { id: "one", title: "Created later", archived: true, workspaceRoot: "C:/alpha", worktreePath: "C:/worktrees/one", createdAt: "2026-10-10", updatedAt: "2026-10-11" },
    { id: "two", title: "Updated later", archived: true, workspaceRoot: "C:/alpha", worktreePath: "C:/worktrees/two", createdAt: "2026-10-09", updatedAt: "2026-10-12" },
    { id: "legacy", title: "Unknown creation", archived: true, workspaceRoot: "C:/alpha", updatedAt: "2026-10-13" },
  ] });
  render(<ArchivedTab />);
  expect(screen.getAllByRole("heading", { name: "alpha" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "归档类型和排序" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "创建时间" }));
  const group = screen.getByRole("region", { name: "alpha" });
  const titles = within(group).getAllByText(/^(Created later|Updated later|Unknown creation)$/);
  expect(titles.map(title => title.textContent)).toEqual(["Created later", "Updated later", "Unknown creation"]);
});

it("keeps a detached chat projectless in its row and body-search request", async () => {
  useAppStore.setState({ conversations: [{ id: "none", title: "Standalone chat", archived: true, projectId: "", workspaceRoot: "C:/alpha", worktreePath: "C:/worktrees/one", updatedAt: "2026-10-23T03:38:00Z" }] });
  render(<ArchivedTab />);
  expect(screen.getByRole("heading", { name: "无项目" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "归档项目操作 无项目" })).toBeNull();
  const date = screen.getByText(/^2026年10月23日，/);
  expect(date.getAttribute("datetime")).toBe("2026-10-23T03:38:00Z");
  expect(screen.getByText("Standalone chat").tagName).toBe("SPAN");
  fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "无项目" }));
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "detached body" } });
  await waitFor(() => expect(readSearch).toHaveBeenCalled());
  expect(readSearch.mock.calls.at(-1)![0].searchParams.get("project")).toBe("");
});

it("requires confirmation for single, project and global archive deletion", async () => {
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "删除 检查项目" })[0]);
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "删除已归档聊天？" })));
  fireEvent.click(screen.getByRole("button", { name: "归档项目操作 alpha" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除项目中的全部聊天" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "删除项目内所有已归档聊天？" })));
  fireEvent.click(screen.getByRole("button", { name: "全部删除" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "删除所有已归档聊天？" })));
  expect(deleteCommand).not.toHaveBeenCalled();
});

it("deletes the full confirmed project independently of text filtering and stops on a rejected delete", async () => {
  useAppStore.setState(state => ({ conversations: [...state.conversations,
    { id: "archive-A2", title: "Other alpha chat", archived: true, workspaceRoot: "C:/alpha", updatedAt: "2026-10-03" },
    { id: "detached", title: "Detached chat", archived: true, projectId: "", workspaceRoot: "C:/alpha", updatedAt: "2026-10-03" },
  ] }));
  confirm.mockResolvedValueOnce(true);
  deleteCommand.mockResolvedValueOnce(false);
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "Other alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "归档项目操作 alpha" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除项目中的全部聊天" }));
  await waitFor(() => expect(deleteCommand).toHaveBeenCalledTimes(1));
  expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ message: "这会永久删除此项目中的 2 个本地已归档聊天。" }));
  expect(deleteCommand).toHaveBeenCalledWith({ type: "conversation.delete", conversation_id: "archive-A", cleanup_worktree: false });
  expect(useAppStore.getState().conversations).toHaveLength(4);
});
