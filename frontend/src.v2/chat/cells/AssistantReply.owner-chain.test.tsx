// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AssistantMarkdownCell } from "./AssistantMarkdownCell";
import type { AssistantMarkdownCellState } from "./cellTypes";
import { useAppStore } from "../../stores";

const mocks = vi.hoisted(() => ({ send: vi.fn(() => true), chat: vi.fn(() => true), confirm: vi.fn() }));
vi.mock("../../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../protocol/ws-outbox")>(), sendClientCommand: mocks.send,
}));
vi.mock("../sendChatMessage", () => ({ sendChatMessage: mocks.chat }));
vi.mock("../../overlays/DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("../messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));
const initial = useAppStore.getState();
const cell: AssistantMarkdownCellState = { kind: "assistant_markdown", id: "assistant-final", messageId: "assistant",
  markdownSource: "owned answer", phase: "final", copyable: false, createdAt: 2 };
const renderOwned = (workspaceRoot = "C:/A", conversationId = "A") => render(
  <AssistantMarkdownCell cell={cell} conversationId={conversationId} workspaceRoot={workspaceRoot} />,
);
beforeEach(() => {
  vi.clearAllMocks();
  mocks.confirm.mockResolvedValue(true);
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A",
    messages: [
      { id: "user", role: "user", content: "owned question", artifacts: [], timestamp: 1 },
      { id: "assistant", role: "assistant", content: "owned answer", artifacts: [], timestamp: 2, terminalStatus: "completed" },
    ], quotedMessage: null, isStreaming: false }, true);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("assistant reply actions", () => {
  it.each(["C:/A", ""])("forks by stable id with explicit conversation and workspace %s", (root) => {
    useAppStore.setState({ workingDirectory: root });
    renderOwned(root);
    fireEvent.click(screen.getByRole("button", { name: "从此处分支" }));
    expect(mocks.send).toHaveBeenCalledWith({ type: "context.fork", conversation_id: "A", workspace_root: root,
      message_id: "assistant", create_branch: true, activate: true });
    expect(mocks.send.mock.calls[0][0]).not.toHaveProperty("message_index");
  });

  it.each(["foreign-owner", "old-workspace"])("does not act on a %s row with the same message id", (reason) => {
    useAppStore.setState(reason === "foreign-owner" ? { conversationId: "B" } : { workingDirectory: "C:/B" });
    renderOwned();
    expect(screen.queryByRole("button", { name: "引用回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "从此处分支" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
    expect(useAppStore.getState().quotedMessage).toBeNull();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.confirm).not.toHaveBeenCalled();
  });

  it("quotes the exact owned answer through the existing composer store", () => {
    renderOwned();
    fireEvent.click(screen.getByRole("button", { name: "引用回复" }));
    expect(useAppStore.getState().quotedMessage).toEqual({ id: "assistant", role: "assistant", content: "owned answer" });
  });

  it("does not focus another owner's composer after the quote's queued animation frame", () => {
    let frame!: FrameRequestCallback;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frame = callback; return 1; }));
    renderOwned();
    const composer = document.createElement("textarea");
    composer.dataset.composerInput = "";
    document.body.append(composer);
    fireEvent.click(screen.getByRole("button", { name: "引用回复" }));
    act(() => { useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" }); frame(0); });
    expect(document.activeElement).not.toBe(composer);
    composer.remove();
  });

  it("does not regenerate after its confirmed workspace changes under the same conversation", async () => {
    let finish!: (approved: boolean) => void;
    mocks.confirm.mockReturnValueOnce(new Promise<boolean>((resolve) => { finish = resolve; }));
    renderOwned();
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(mocks.confirm).toHaveBeenCalledOnce());
    await act(async () => { useAppStore.setState({ workingDirectory: "C:/B" }); finish(true); });
    expect(mocks.chat).not.toHaveBeenCalled();
  });

  it("regenerates the owning user input and exact retry boundary after confirmation", async () => {
    renderOwned();
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(mocks.chat).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: "A", displayContent: "owned question", backendContent: "owned question", retryFromMessageId: "user",
    })));
  });
});
