// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ChatActionsButton } from "./ChatActionsButton";

const mocks = vi.hoisted(() => ({ send: vi.fn(), prompt: vi.fn(), confirm: vi.fn(), toast: vi.fn(), copy: vi.fn() }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommandAwaitResult: mocks.send }));
vi.mock("../overlays/DialogService", () => ({ showPrompt: mocks.prompt, showConfirm: mocks.confirm }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copy }));

const openSubmenu = (item: HTMLElement) => { item.focus(); fireEvent.keyDown(item, { key: "ArrowRight" }); };

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.send.mockResolvedValue({ type: "command.result", level: "success", data: {} });
  mocks.copy.mockResolvedValue(true);
  mocks.confirm.mockResolvedValue(true);
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, workingDirectory: "C:/A", sideChatOpen: false, activeSideChatId: null, sideChats: {}, appMode: "code",
    rightPanelOpen: false, commandPaletteOpen: false, isStreaming: true,
    conversations: [{ id: "A", title: "Chat A", workspaceRoot: "C:/A", updatedAt: "today" }, { id: "B", title: "Chat B", workspaceRoot: "C:/B", updatedAt: "today" }],
    messages: [{ id: "a-user", role: "user", content: "Inspect project A", timestamp: 1 }],
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const open = () => fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));

it("renames the captured canonical conversation even if selection changes while its prompt is open", async () => {
  let resolve!: (value: string) => void;
  mocks.prompt.mockReturnValue(new Promise<string>((done) => { resolve = done; }));
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" }));
  await act(async () => resolve("Renamed A"));
  expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.rename", conversation_id: "A", title: "Renamed A" }, "conversation.rename");
  expect(useAppStore.getState().conversations[0].title).toBe("Chat A");
});

it("copies the actual captured chat as source Markdown and exposes no export-tree action", async () => {
  render(<ChatActionsButton />); open();
  expect(screen.queryByRole("menuitem", { name: "导出会话树" })).toBeNull();
  openSubmenu(screen.getByRole("menuitem", { name: "复制" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "复制为 Markdown" }));
  await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("# Chat A\n\n> Inspect project A\n", "复制为 Markdown"));
  expect(mocks.send).not.toHaveBeenCalled();
  expect(useAppStore.getState().commandPaletteOpen).toBe(false);
});

it("surfaces archive rejection without stopping or optimistically archiving a running chat", async () => {
  mocks.send.mockResolvedValue({ type: "command.result", level: "error", message: "任务仍在运行，不能归档。" });
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "归档" }));
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("任务仍在运行，不能归档。", "error"));
  expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.archive", conversation_id: "A", archived: true, archive_navigation: "home" }, "conversation.archive");
  expect(useAppStore.getState().isStreaming).toBe(true);
  expect(useAppStore.getState().conversations[0].archived).not.toBe(true);
});

it("creates a new side fork owner on each source action and preserves an existing side draft", () => {
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "打开侧边聊天" }));
  const first = useAppStore.getState().activeSideChatId!;
  act(() => useAppStore.getState().setSideChatDraft(first, "Keep this side draft"));
  expect(useAppStore.getState()).toMatchObject({ rightStackTab: "sidechat", sideChatOpen: true, rightPanelOpen: true, conversationId: "A", isStreaming: true });
  open(); fireEvent.click(screen.getByRole("menuitem", { name: "打开侧边聊天" }));
  expect(useAppStore.getState().activeSideChatId).not.toBe(first);
  expect(useAppStore.getState().sideChats[first].draft).toBe("Keep this side draft");
  expect(useAppStore.getState().sideChats[first].parentConversationId).toBe("A");
  expect(mocks.send).not.toHaveBeenCalled();
});

it("submits blank rename values to the actual rename boundary rather than silently treating them as Cancel", async () => {
  mocks.prompt.mockResolvedValue("");
  mocks.send.mockResolvedValue({ type: "command.result", level: "error", message: "thread name must not be empty" });
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.rename", conversation_id: "A", title: "" }, "conversation.rename"));
  expect(mocks.prompt).toHaveBeenCalledWith(expect.objectContaining({ allowEmpty: true }));
  expect(mocks.toast).toHaveBeenCalledWith("thread name must not be empty", "error");
});

it("copies the source deeplink and working directory through the Copy submenu", () => {
  render(<ChatActionsButton />); open();
  openSubmenu(screen.getByRole("menuitem", { name: "复制" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "复制深度链接" }));
  expect(mocks.copy).toHaveBeenCalledWith("minicode://conversation?conversation_id=A", "复制深度链接");
  open(); openSubmenu(screen.getByRole("menuitem", { name: "复制" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "复制工作目录" }));
  expect(mocks.copy).toHaveBeenCalledWith("C:/A", "工作区路径");
});

it("uses the current full-history endpoint for Fork and lets the canonical switched event activate it", async () => {
  render(<ChatActionsButton />); open();
  openSubmenu(screen.getByRole("menuitem", { name: "分叉" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "创建聊天分支" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "context.fork", conversation_id: "A", workspace_root: "C:/A",
    message_id: "a-user", create_branch: false, activate: true }, "context.fork"));
  expect(useAppStore.getState().conversationId).toBe("A");
  expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.clone" }), expect.anything());
});

it("presents the Work continuation labels and only exposes new-worktree Fork for a known repository", async () => {
  useAppStore.setState({ appMode: "cowork", conversations: [{ id: "A", title: "Worktree A", updatedAt: "today", workspaceRoot: "C:/repo", executionCwd: "C:/worktree", worktreePath: "C:/worktree", gitIsolated: true }] });
  render(<ChatActionsButton />); open();
  openSubmenu(screen.getByRole("menuitem", { name: "继续" }));
  expect(screen.getByRole("menuitem", { name: "在同一工作树中继续" })).toBeTruthy();
  fireEvent.click(screen.getByRole("menuitem", { name: "在新工作树中继续" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ type: "context.fork", workspace_root: "C:/worktree", create_branch: true, activate: true }), "context.fork"));
});

it("cancels Stop and archive without cancelling a running owner or sending an archive request", async () => {
  mocks.confirm.mockResolvedValue(false);
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "归档" }));
  await waitFor(() => expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "停止并归档此聊天？", confirmLabel: "停止并归档", danger: true })));
  expect(mocks.send).not.toHaveBeenCalled();
  expect(useAppStore.getState().isStreaming).toBe(true);
});

it("cannot act on a pending title that has not become the canonical owner", () => {
  useAppStore.setState({ pendingConversationSwitchId: "B" });
  render(<ChatActionsButton />);
  expect(screen.getByRole("button", { name: "聊天操作" })).toHaveProperty("disabled", true);
  expect(mocks.send).not.toHaveBeenCalled();
});
