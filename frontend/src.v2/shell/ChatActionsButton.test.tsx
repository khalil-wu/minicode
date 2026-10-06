// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ChatActionsButton } from "./ChatActionsButton";

const mocks = vi.hoisted(() => ({ send: vi.fn(), prompt: vi.fn(), toast: vi.fn(), copy: vi.fn() }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommandAwaitResult: mocks.send }));
vi.mock("../overlays/DialogService", () => ({ showPrompt: mocks.prompt }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copy }));

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.send.mockResolvedValue({ type: "command.result", level: "success", data: {} });
  mocks.copy.mockResolvedValue(true);
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, workingDirectory: "C:/A", sideChatOpen: false,
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

it("exports and copies the actual captured chat without using the command palette", async () => {
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "复制对话文本" }));
  expect(mocks.copy).toHaveBeenCalledWith("用户\nInspect project A", "对话文本");
  open(); fireEvent.click(screen.getByRole("menuitem", { name: "导出会话树" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.export", conversation_id: "A", include_descendants: true }, "conversation.export"));
  expect(useAppStore.getState().commandPaletteOpen).toBe(false);
});

it("surfaces archive rejection without stopping or optimistically archiving a running chat", async () => {
  mocks.send.mockResolvedValue({ type: "command.result", level: "error", message: "任务仍在运行，不能归档。" });
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "归档" }));
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("任务仍在运行，不能归档。", "error"));
  expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.archive", conversation_id: "A", archived: true }, "conversation.archive");
  expect(useAppStore.getState().isStreaming).toBe(true);
  expect(useAppStore.getState().conversations[0].archived).not.toBe(true);
});

it("opens a real side-chat surface and preserves an existing side chat on subsequent use", () => {
  render(<ChatActionsButton />); open();
  fireEvent.click(screen.getByRole("menuitem", { name: "新建侧边聊天" }));
  expect(useAppStore.getState()).toMatchObject({ rightStackTab: "sidechat", sideChatOpen: true, rightPanelOpen: true, conversationId: "A", isStreaming: true });
  open(); expect(screen.getByRole("menuitem", { name: "打开侧边聊天" })).toBeTruthy();
  expect(mocks.send).not.toHaveBeenCalled();
});

it("cannot act on a pending title that has not become the canonical owner", () => {
  useAppStore.setState({ pendingConversationSwitchId: "B" });
  render(<ChatActionsButton />);
  expect(screen.getByRole("button", { name: "聊天操作" })).toHaveProperty("disabled", true);
  expect(mocks.send).not.toHaveBeenCalled();
});
