/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { notifyClientCommandReceived, registerWebSocketSender, resetPendingCommandResultsForTests } from "../../protocol/ws-outbox";
import { handleCommandResultEvent } from "../commandResultEvents";
import type { ClientCommand } from "../../protocol/events";
import { AssistantMarkdownCell } from "./AssistantMarkdownCell";

const mocks = vi.hoisted(() => ({ clipboard: vi.fn(async () => {}), toast: vi.fn() }));
vi.mock("../messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
const initial = useAppStore.getState();
const commands: ClientCommand[] = [];
beforeEach(() => {
  commands.length = 0; mocks.clipboard.mockClear(); mocks.toast.mockClear();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: mocks.clipboard } });
  useAppStore.setState({ ...initial, appMode: "cowork", conversationId: "owner", workingDirectory: "C:/project", isConnected: true,
    messages: [{ id: "user", role: "user", content: "Actual request", timestamp: 1 }, { id: "answer", role: "assistant", content: "Literal answer", timestamp: 2 }],
  }, true);
  registerWebSocketSender(command => { commands.push(command); return true; });
});
afterEach(async () => { cleanup(); await act(async () => resetPendingCommandResultsForTests()); registerWebSocketSender(null); vi.useRealTimers(); });
const view = () => render(<AssistantMarkdownCell conversationId="owner" workspaceRoot="C:/project" cell={{ kind: "assistant_markdown",
  id: "answer-cell", messageId: "answer", markdownSource: "Literal answer", phase: "final", copyable: true, createdAt: 2 }} />);
const settle = async (command: ClientCommand, level = "success") => act(async () => {
  handleCommandResultEvent({ type: "command.result", command: "context.fork", client_command_id: command.client_command_id,
    conversation_id: "owner", level, message: level === "error" ? "Actual fork failure" : "", data: { parent_conversation_id: "owner", branch_conversation_id: "real-branch" } });
});

describe("actual Codex Ax reply actions", () => {
  it("offers Copy and Fork on a settled work reply and retains literal clipboard contents", async () => {
    const { container } = view();
    expect(container.querySelectorAll(".assistant-cell-action-buttons button")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "引用回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.clipboard).toHaveBeenCalledWith("Literal answer"));
    expect(await screen.findByRole("button", { name: "已复制" })).toBeTruthy();
    expect(container.querySelector('.assistant-cell-action-buttons button [data-reply-action-icon="copied"]')).toBeTruthy();
  });

  it("keeps the fork disabled until the actual semantic result, independent of transport receipt", async () => {
    const { container } = view(); const fork = screen.getByRole("button", { name: "从此处分支" });
    fireEvent.click(fork); fireEvent.click(fork);
    expect(commands).toHaveLength(1);
    expect(commands[0]).toMatchObject({ type: "context.fork", conversation_id: "owner", workspace_root: "C:/project", message_id: "answer", create_branch: true, activate: true });
    expect((fork as HTMLButtonElement).disabled).toBe(true); expect(fork.getAttribute("aria-busy")).toBe("true");
    expect(container.querySelector(".lucide-loader-circle")).toBeTruthy();
    act(() => notifyClientCommandReceived(commands[0].client_command_id!));
    expect((fork as HTMLButtonElement).disabled).toBe(true);
    await settle(commands[0]);
    expect((fork as HTMLButtonElement).disabled).toBe(false); expect(fork.getAttribute("aria-busy")).toBeNull();
  });

  it("reports the real failed fork and allows a new request without claiming a branch was created", async () => {
    view(); const fork = screen.getByRole("button", { name: "从此处分支" }); fireEvent.click(fork);
    await settle(commands[0], "error");
    expect(mocks.toast).toHaveBeenCalledWith("Actual fork failure", "error");
    expect(useAppStore.getState().conversationId).toBe("owner");
    expect((fork as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(fork); expect(commands).toHaveLength(2);
    expect(commands[1].client_command_id).not.toBe(commands[0].client_command_id);
    await settle(commands[1]);
  });

  it("retains the separate legacy chat actions while the Codex work row remains concise", () => {
    useAppStore.setState({ appMode: "chat" }); view();
    expect(screen.getByRole("button", { name: "引用回复" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeTruthy();
  });
});
