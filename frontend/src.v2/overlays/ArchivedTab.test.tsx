/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ArchivedTab } from "./ArchivedTab";

const { readArchive, readSearch, command, confirm, deleteCommand } = vi.hoisted(() => ({ readArchive: vi.fn(), readSearch: vi.fn(), command: vi.fn(), confirm: vi.fn(), deleteCommand: vi.fn() }));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://archive.test", authHeaders: () => ({}), errorMessageFromResponseText: (text: string) => text,
  fetchWithTimeout: (url: URL, init: RequestInit) => url.pathname === "/api/conversations/search" ? readSearch(url, init) : readArchive(url, init) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "read-session" }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: command, sendConversationDeleteCommand: deleteCommand, commandResultSucceeded: (result: { level: string }) => result.level !== "error" }));
vi.mock("./DialogService", () => ({ showConfirm: confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../chat/messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "active", workingDirectory: "C:/active", recentWorkspaces: [], runtimeCapabilities: null, isConnected: true, conversations: [
    { id: "archive-A", title: "检查项目", archived: true, workspaceRoot: "C:/alpha", createdAt: "2026-09-01T08:00:00Z", updatedAt: "2026-10-01T08:00:00Z" },
    { id: "archive-B", title: "检查项目", archived: true, workspaceRoot: "C:/beta", createdAt: "2026-09-04T08:00:00Z", updatedAt: "2026-10-04T08:00:00Z" },
  ] });
  readArchive.mockResolvedValue({ ok: true, json: async () => ({ transcript: [{ id: "old-question", role: "user", content: "以前的项目问题" }, { id: "old-answer", role: "assistant", content: "以前的项目结果" }], transcript_page: { has_more: false, before_message_id: "", total_messages: 2 } }) });
  readSearch.mockResolvedValue({ ok: true, json: async () => ({ data: [], next_cursor: null }) });
  command.mockResolvedValue({ level: "success" });
  confirm.mockResolvedValue(false);
  deleteCommand.mockResolvedValue(true);
});
afterEach(cleanup);

it("finds an archived body-only match through the real session API and opens its independent reader", async () => {
  readSearch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: [{
    conversation: { id: "archive-A", title: "检查项目", archived: true, workspace_root: "C:/alpha", updated_at: "2026-10-01" },
    message_id: "old-body", snippet: "buried body-only phrase",
  }], next_cursor: null }) });
  useAppStore.setState({ draft: "unsent current owner text" });
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "body-only phrase" } });
  const match = await screen.findByRole("button", { name: "检查项目" });
  expect(readSearch.mock.calls[0][0].searchParams.get("session_id")).toBe("read-session");
  expect(readSearch.mock.calls[0][0].searchParams.get("archived")).toBe("true");
  expect(readSearch.mock.calls[0][0].searchParams.get("query")).toBe("body-only phrase");
  fireEvent.click(match);
  expect(await screen.findByText("以前的项目问题")).toBeTruthy();
  expect(String(readArchive.mock.calls[0][0])).toContain("archive-A/messages?session_id=read-session");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "active", workingDirectory: "C:/active", draft: "unsent current owner text" });
  expect(command).not.toHaveBeenCalled();
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

it("finds duplicate archive titles by project and offers the observed filter layout", () => {
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "alpha" } });
  expect(screen.getAllByRole("button", { name: "检查项目" })).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "beta" }));
  expect(screen.getAllByRole("button", { name: "检查项目" })).toHaveLength(1);
  expect(screen.getByRole("heading", { name: "beta" })).toBeTruthy();
  expect(screen.queryByLabelText("归档最后活动开始日期")).toBeNull();
});

it("reads an archived transcript without switching or restoring the active conversation", async () => {
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "检查项目" })[0]);
  expect(await screen.findByText("以前的项目问题")).toBeTruthy();
  expect(screen.getByText("以前的项目结果")).toBeTruthy();
  expect(String(readArchive.mock.calls[0][0])).toContain("archive-B/messages?session_id=read-session");
  expect(useAppStore.getState().conversationId).toBe("active");
  expect(command).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox", { name: "搜索已归档任务" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回已归档聊天" }));
  expect(screen.getByRole("textbox", { name: "搜索已归档任务" })).toBeTruthy();
});

it("shows complete messages once without also rendering their summary copy", async () => {
  useAppStore.setState((state) => ({ conversations: state.conversations.map((conversation) => conversation.id === "archive-B"
    ? { ...conversation, summary: "User: 以前的项目问题\nAssistant: ## 以前的项目结果（摘要副本）" } : conversation) }));
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "检查项目" })[0]);
  expect(await screen.findByText("以前的项目问题")).toBeTruthy();
  expect(screen.getAllByText("以前的项目问题")).toHaveLength(1);
  expect(screen.getAllByText("以前的项目结果")).toHaveLength(1);
  expect(screen.queryByText(/摘要副本/)).toBeNull();
});

it("preserves search while restoring only the selected task", async () => {
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "恢复 检查项目" }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({ type: "conversation.unarchive", conversation_id: "archive-A", archived: false }, "conversation.unarchive"));
  act(() => useAppStore.setState((state) => ({ conversations: state.conversations.map((conversation) => conversation.id === "archive-A" ? { ...conversation, archived: false } : conversation) })));
  expect(screen.getByLabelText("搜索已归档任务")).toHaveProperty("value", "alpha");
  expect(await screen.findByText("没有匹配的任务")).toBeTruthy();
});

it("sorts by the real creation time and groups isolated worktrees under their project", () => {
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
  const titles = within(group).getAllByRole("button").filter(button => ["Created later", "Updated later", "Unknown creation"].includes(button.textContent || ""));
  expect(titles.map(button => button.textContent)).toEqual(["Created later", "Updated later", "Unknown creation"]);
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
  expect(useAppStore.getState().conversations).toHaveLength(3);
});

it("loads older archived messages in the independent reader and preserves the search on return", async () => {
  readArchive.mockResolvedValueOnce({ ok: true, json: async () => ({ transcript: [{ id: "recent", role: "assistant", content: "Recent result" }], transcript_page: { has_more: true, before_message_id: "recent" } }) });
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "beta" } });
  fireEvent.click(screen.getByRole("button", { name: "检查项目" }));
  await screen.findByText("Recent result");
  readArchive.mockResolvedValueOnce({ ok: true, json: async () => ({ transcript: [{ id: "old", role: "user", content: "Earlier question" }], transcript_page: { has_more: false, before_message_id: "" } }) });
  fireEvent.click(screen.getByRole("button", { name: "读取更早的消息" }));
  expect(await screen.findByText("Earlier question")).toBeTruthy();
  expect(screen.getByText("Recent result")).toBeTruthy();
  expect(String(readArchive.mock.calls[1][0])).toContain("before_message_id=recent");
  fireEvent.click(screen.getByRole("button", { name: "返回已归档聊天" }));
  expect(screen.getByRole("textbox", { name: "搜索已归档任务" })).toHaveProperty("value", "beta");
  expect(command).not.toHaveBeenCalled();
});

it("retains uploaded files and quoted text in the read-only message consumers", async () => {
  readArchive.mockResolvedValueOnce({ ok: true, json: async () => ({ transcript: [
    { id: "with-input", role: "user", content: "Read this document", attachmentRefs: [{ id: "upload", name: "reference.pdf", kind: "document", mediaType: "application/pdf", sizeBytes: 12, artifactId: "original-upload" }], quotedMessage: { messageId: "quote", content: "Original quotation", role: "assistant" } },
  ], transcript_page: { has_more: false } }) });
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "检查项目" })[0]);
  expect(await screen.findByRole("button", { name: "reference.pdf" })).toBeTruthy();
  expect(screen.getByText("Read this document")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "撤回到输入框" })).toBeNull();
  expect(command).not.toHaveBeenCalled();
});
