/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ArchivedTab } from "./ArchivedTab";

const { readArchive, command, confirm } = vi.hoisted(() => ({ readArchive: vi.fn(), command: vi.fn(), confirm: vi.fn() }));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://archive.test", authHeaders: () => ({}), errorMessageFromResponseText: (text: string) => text, fetchWithTimeout: readArchive }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "read-session" }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: command, sendConversationDeleteCommand: vi.fn(), commandResultSucceeded: (result: { level: string }) => result.level !== "error" }));
vi.mock("./DialogService", () => ({ showConfirm: confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../chat/messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "active", workingDirectory: "C:/active", conversations: [
    { id: "archive-A", title: "检查项目", archived: true, workspaceRoot: "C:/alpha", updatedAt: "2026-10-01T08:00:00Z" },
    { id: "archive-B", title: "检查项目", archived: true, workspaceRoot: "C:/beta", updatedAt: "2026-10-04T08:00:00Z" },
  ] });
  readArchive.mockResolvedValue({ ok: true, json: async () => ({ transcript: [{ id: "old-question", role: "user", content: "以前的项目问题" }, { id: "old-answer", role: "assistant", content: "以前的项目结果" }], transcript_page: { has_more: false, before_message_id: "", total_messages: 2 } }) });
  command.mockResolvedValue({ level: "success" });
});
afterEach(cleanup);

it("finds duplicate archive titles by project and last-activity date", () => {
  render(<ArchivedTab />);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "alpha" } });
  expect(screen.getAllByRole("button", { name: "检查项目" })).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("搜索已归档任务"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("归档最后活动开始日期"), { target: { value: "2026-10-03" } });
  expect(screen.getAllByRole("button", { name: "检查项目" })).toHaveLength(1);
  expect(screen.getByText("beta")).toBeTruthy();
});

it("reads an archived transcript without switching or restoring the active conversation", async () => {
  render(<ArchivedTab />);
  fireEvent.click(screen.getAllByRole("button", { name: "检查项目" })[0]);
  expect(await screen.findByText("以前的项目问题")).toBeTruthy();
  expect(screen.getByText("以前的项目结果")).toBeTruthy();
  expect(String(readArchive.mock.calls[0][0])).toContain("archive-B/messages?session_id=read-session");
  expect(useAppStore.getState().conversationId).toBe("active");
  expect(command).not.toHaveBeenCalled();
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
  expect(screen.getByText("没有匹配的任务")).toBeTruthy();
});
