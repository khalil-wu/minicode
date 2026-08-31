/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  awaitResult: vi.fn(),
  deleteConversation: vi.fn(async () => true),
  sendCommand: vi.fn(() => true),
  sendChatMessage: vi.fn(() => true),
}));

vi.mock("../protocol/ws-outbox", () => ({
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  sendClientCommand: mocks.sendCommand,
  sendClientCommandAwaitResult: mocks.awaitResult,
  sendConversationDeleteCommand: mocks.deleteConversation,
}));

vi.mock("../chat/sendChatMessage", () => ({
  sendChatMessage: mocks.sendChatMessage,
}));

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { useAppStore } from "../stores";
import { SideChatPanel } from "./SideChatPanel";

const successfulCreate = {
  type: "command.result",
  command: "conversation.create",
  level: "info",
  message: "",
  data: {},
};

describe("SideChatPanel server lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.awaitResult.mockResolvedValue(successfulCreate);
    useAppStore.setState({
      sideChats: {},
      sideChatPendingContext: null,
      isConnected: false,
      permissionMode: "confirm",
    });
  });

  afterEach(() => cleanup());

  it("does not create while disconnected and creates exactly once after reconnect", async () => {
    render(<SideChatPanel />);
    expect(mocks.awaitResult).not.toHaveBeenCalled();

    act(() => useAppStore.setState({ isConnected: true }));
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(1));

    act(() => useAppStore.setState({ isConnected: false }));
    act(() => useAppStore.setState({ isConnected: true }));
    await act(async () => Promise.resolve());
    expect(mocks.awaitResult).toHaveBeenCalledTimes(1);
  });

  it("keeps Send disabled until the backend confirms creation", async () => {
    let resolveCreate: (value: typeof successfulCreate) => void = () => {};
    mocks.awaitResult.mockImplementationOnce(() => new Promise((resolve) => {
      resolveCreate = resolve;
    }));
    useAppStore.setState({ isConnected: true });
    render(<SideChatPanel />);

    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), {
      target: { value: "check this" },
    });
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();

    await act(async () => resolveCreate(successfulCreate));
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("never sends or deletes a conversation when creation fails", async () => {
    mocks.awaitResult.mockResolvedValueOnce({ ...successfulCreate, level: "error", message: "rejected" });
    useAppStore.setState({ isConnected: true });
    const view = render(<SideChatPanel />);

    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), {
      target: { value: "check this" },
    });
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(1));
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();

    view.unmount();
    expect(mocks.deleteConversation).not.toHaveBeenCalled();
  });

  it("deletes exactly once when a successful create resolves after unmount", async () => {
    let resolveCreate: (value: typeof successfulCreate) => void = () => {};
    mocks.awaitResult.mockImplementationOnce(() => new Promise((resolve) => {
      resolveCreate = resolve;
    }));
    useAppStore.setState({ isConnected: true });
    const view = render(<SideChatPanel />);
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(1));

    view.unmount();
    await act(async () => resolveCreate(successfulCreate));
    await waitFor(() => expect(mocks.deleteConversation).toHaveBeenCalledTimes(1));
  });
});
