/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../stores/types";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", {
  configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));

const mocks = vi.hoisted(() => ({
  awaitResult: vi.fn(),
  deleteConversation: vi.fn(async () => true),
  sendCommand: vi.fn((_command: unknown) => true),
  sendChatMessage: vi.fn(() => true),
  promptResponse: vi.fn(async () => ({ type: "command.result", level: "success", message: "", data: {} })),
}));

vi.mock("../protocol/ws-outbox", () => ({
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  sendClientCommand: mocks.sendCommand,
  sendClientCommandAwaitResult: mocks.awaitResult,
  sendConversationDeleteCommand: mocks.deleteConversation,
  sendPromptResponseCommand: mocks.promptResponse,
}));

vi.mock("../chat/sendChatMessage", async (importOriginal) => ({
  ...await importOriginal<typeof import("../chat/sendChatMessage")>(),
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
  it("submits side-owned attachments and a precise selection as a follow-up without touching the main composer", async () => {
    useAppStore.setState({ isConnected: true, draft: "main draft" });
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalled());
    const range = { startLineNumber: 4, startColumn: 2, endLineNumber: 6, endColumn: 5 };
    act(() => useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [sideId]: {
      ...state.sideChats[sideId], draft: "follow-up", isStreaming: true,
      selectedContext: { text: "selected", source: "source.ts", range, workspaceRoot: "C:/workspace/primary" },
      attachments: [{ id: "side-att", name: "note.txt", type: "text/plain", size: 4, status: "ready", conversationId: sideId, artifactId: "side-file", attachment: { id: "side-file", file_name: "note.txt", artifact_id: "side-file", media_type: "text/plain", kind: "document" } }],
    } } })));
    fireEvent.click(screen.getByRole("button", { name: "将消息加入队列" }));
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ conversationId: sideId, allowWhileStreaming: true,
      contextRefs: [expect.objectContaining({ range, path: "source.ts" })], attachments: [expect.objectContaining({ artifact_id: "side-file" })],
    })));
    expect(useAppStore.getState().draft).toBe("main draft");
    expect(useAppStore.getState().attachments).toEqual([]);
    expect(useAppStore.getState().sideChats[sideId].attachments).toEqual([]);
  });
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.awaitResult.mockImplementation(async (command) => ({ ...successfulCreate, data: { conversation_id: command.conversation_id } }));
    useAppStore.setState({
      sideChats: {},
      sideChatPendingContext: null,
      isConnected: false,
      permissionMode: "confirm",
      workingDirectory: "C:/workspace/primary",
      sendShortcut: "enter",
      conversationId: "main-conversation",
      messages: [],
      attachments: [],
      isStreaming: false,
      viewMode: "normal",
      pendingApproval: null,
      approvalQueue: [],
      pendingAskUser: null,
      askUserQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      turnDiffs: {},
      runtimeSession: null,
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

    await act(async () => resolveCreate({ ...successfulCreate, data: { conversation_id: mocks.awaitResult.mock.calls[0][0].conversation_id } }));
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
    await act(async () => resolveCreate({ ...successfulCreate, data: { conversation_id: mocks.awaitResult.mock.calls[0][0].conversation_id } }));
    await waitFor(() => expect(mocks.deleteConversation).toHaveBeenCalledTimes(1));
  });

  it("keeps each temporary thread bound to the workspace and permission mode in which it was opened", async () => {
    render(<SideChatPanel />);
    act(() => useAppStore.setState({ workingDirectory: "C:/workspace/other", permissionMode: "bypass", isConnected: true }));
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(2));
    expect(mocks.awaitResult.mock.calls.map(([command]) => command)).toEqual(expect.arrayContaining([
      expect.objectContaining({ workspace_root: "C:/workspace/primary", permission_mode: "confirm" }),
      expect.objectContaining({ workspace_root: "C:/workspace/other", permission_mode: "bypass" }),
    ]));
  });

  it("keeps the old workspace draft while the same relative file in another workspace gets its own side thread", async () => {
    const range = { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 15 };
    useAppStore.setState({ isConnected: true, workingDirectory: "C:/project-a" });
    useAppStore.getState().openSideChatWithSelection("project A code", "src/shared.ts", { range, workspaceRoot: "C:/project-a" });
    const view = render(<SideChatPanel />);
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(1));
    const firstId = Object.values(useAppStore.getState().sideChats).find((thread) => thread.workspaceRoot === "C:/project-a")!.id;
    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), { target: { value: "A 的未发送草稿" } });
    act(() => {
      useAppStore.getState().setWorkingDirectory("C:/project-b");
      useAppStore.getState().openSideChatWithSelection("project B code", "src/shared.ts", { range, workspaceRoot: "C:/project-b" });
    });
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledTimes(2));
    const second = Object.values(useAppStore.getState().sideChats).find((thread) => thread.workspaceRoot === "C:/project-b")!;
    expect(second.id).not.toBe(firstId);
    expect(second.selectedContext).toEqual({ text: "project B code", source: "src/shared.ts", range, workspaceRoot: "C:/project-b" });
    expect(useAppStore.getState().sideChats[firstId]).toMatchObject({ draft: "A 的未发送草稿", selectedContext: { text: "project A code", source: "src/shared.ts", workspaceRoot: "C:/project-a" } });
    expect((screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), { target: { value: "B 的草稿" } });
    act(() => useAppStore.getState().setWorkingDirectory("C:/project-a"));
    expect((screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement).value).toBe("A 的未发送草稿");
    expect(useAppStore.getState().sideChats[second.id].draft).toBe("B 的草稿");
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    expect(mocks.deleteConversation).not.toHaveBeenCalled();
    view.unmount();
    expect(mocks.deleteConversation).toHaveBeenCalledTimes(2);
  });

  it("respects IME composition and the shared send shortcut", async () => {
    useAppStore.setState({ isConnected: true, sendShortcut: "mod-enter" });
    render(<SideChatPanel />);
    const input = screen.getByRole("textbox", { name: "侧边对话消息" });
    fireEvent.change(input, { target: { value: "测试输入" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true });
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledTimes(1));
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ conversationId: sideId }));
    expect(useAppStore.getState().sideChats[sideId].draft).toBe("");
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("uses the main turn UI for side messages, process notes, command disclosure and failures", () => {
    const view = render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    const messages: ChatMessage[] = [
      { id: "side-user", role: "user", content: "检查所选内容", timestamp: 1, artifacts: [] },
      { id: "side-answer", role: "assistant", content: "", timestamp: 2, artifacts: [], isStreaming: true,
        blocks: [
          { type: "text", itemId: "note", source: "commentary", content: "我先检查现有实现。", status: "completed" },
          { type: "tool_call", record: { id: "side-command", name: "run_command", args: { command: "npm test" },
            status: "success", startedAt: 2, finishedAt: 3, stdoutPreview: "18 tests passed" } },
        ],
      },
    ];
    act(() => useAppStore.setState((state) => ({
      sideChats: { ...state.sideChats, [sideId]: { ...state.sideChats[sideId], messages, isStreaming: true } },
      messages: [{ id: "main-only", role: "user", content: "主对话独有内容", timestamp: 1, artifacts: [] }],
    })));
    expect(view.container.querySelector(".user-cell-bubble")?.textContent).toContain("检查所选内容");
    expect(screen.getByText("我先检查现有实现。")).toBeTruthy();
    expect(screen.queryByText("主对话独有内容")).toBeNull();
    expect(screen.queryByText("18 tests passed")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /展开命令详情.*npm test/ }));
    expect(screen.getByText("18 tests passed")).toBeTruthy();

    act(() => useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [sideId]: {
      ...state.sideChats[sideId], isStreaming: false,
      messages: [messages[0], { ...messages[1], isStreaming: false, terminalStatus: "failed", failureMessage: "侧边请求被拒绝" }],
    } } })));
    expect(screen.getByText("侧边请求被拒绝")).toBeTruthy();
  });

  it("shows and answers the side owner's queued question while the main conversation also waits", async () => {
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    act(() => useAppStore.setState({
      pendingAskUser: { requestId: "main-question", conversationId: "main-conversation", question: "主对话问题" },
      askUserQueue: [{ requestId: "side-question", conversationId: sideId, turnId: "side-turn", messageId: "side-answer",
        question: "侧边对话需要哪种方案？", options: [
          { label: "保留当前方案", value: "保留当前方案" }, { label: "换一种方案", value: "换一种方案" },
        ], allowCustom: true }],
    }));
    expect(screen.queryByText("主对话问题")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: /保留当前方案/ }));
    expect(mocks.promptResponse).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => expect(mocks.promptResponse).toHaveBeenCalledWith(expect.objectContaining({
      conversation_id: sideId, turn_id: "side-turn", message_id: "side-answer",
      request_id: "side-question", response: expect.objectContaining({ response: { answer: "保留当前方案" } }),
    })));
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("main-question");
  });

  it("submits the side owner's approval without clearing the main approval", async () => {
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    act(() => useAppStore.setState({
      pendingApproval: { requestId: "main-approval", conversationId: "main-conversation", toolName: "write_file", args: {} },
      approvalQueue: [{ requestId: "side-approval", conversationId: sideId, turnId: "side-turn", messageId: "side-answer",
        toolName: "read_file", args: { path: "src/app.ts" } }],
    }));
    fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
    await waitFor(() => expect(mocks.promptResponse).toHaveBeenCalledWith(expect.objectContaining({
      conversation_id: sideId, turn_id: "side-turn", message_id: "side-answer", request_id: "side-approval",
    })));
    expect(useAppStore.getState().pendingApproval?.requestId).toBe("main-approval");
  });

  it("interrupts only the side turn through the command, composer and Escape actions", () => {
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    act(() => useAppStore.setState((state) => ({
      isStreaming: true,
      messages: [{ id: "main-answer", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true, turnId: "main-turn" }],
      sideChats: { ...state.sideChats, [sideId]: { ...state.sideChats[sideId], isStreaming: true, messages: [
        { id: "side-answer", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true, turnId: "side-turn",
          blocks: [{ type: "tool_call", record: { id: "side-command", name: "run_command", args: { command: "npm test" }, status: "running", startedAt: 1 } }] },
      ] } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "停止命令" }));
    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "侧边对话消息" }), { key: "Escape" });
    expect(mocks.sendCommand).toHaveBeenCalledTimes(3);
    for (const [command] of mocks.sendCommand.mock.calls) expect(command).toEqual({
      type: "interrupt", conversation_id: sideId, turn_id: "side-turn", message_id: "side-answer",
    });
    expect(useAppStore.getState().isStreaming).toBe(true);
  });

  it("opens the side turn's authoritative multi-file review and retains independent drafts across tab visibility", () => {
    const view = render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    const patch = "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new\n";
    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), { target: { value: "侧边草稿" } });
    act(() => useAppStore.setState((state) => ({
      draft: "主对话草稿",
      sideChats: { ...state.sideChats, [sideId]: { ...state.sideChats[sideId], messages: [
        { id: "side-answer", role: "assistant", content: "更新完成", timestamp: 1, artifacts: [], turnId: "side-turn", terminalStatus: "completed" },
      ] } },
      turnDiffs: { [sideId]: { threadId: sideId, turnId: "side-turn", messageId: "side-answer", diff: patch, updatedAt: 1 } },
    })));
    expect(screen.getByRole("button", { name: "复制回复" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "引用回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "审核" }));
    expect(useAppStore.getState().diffReview).toMatchObject({ conversationId: sideId, selectedPath: "src/app.ts" });
    view.rerender(<SideChatPanel active={false} />);
    view.rerender(<SideChatPanel active />);
    expect((screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement).value).toBe("侧边草稿");
    expect(useAppStore.getState().draft).toBe("主对话草稿");
    expect(Object.keys(useAppStore.getState().sideChats)).toEqual([sideId]);
  });

  it("preserves a reading position while streaming and offers an explicit return to the latest message", () => {
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    const list = screen.getByRole("log", { name: "侧边对话历史" });
    Object.defineProperties(list, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 400 },
      scrollTo: { configurable: true, value: vi.fn() },
    });
    list.scrollTop = 100;
    fireEvent.scroll(list);
    act(() => useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [sideId]: {
      ...state.sideChats[sideId], isStreaming: true,
      messages: [{ id: "side-answer", role: "assistant", content: "继续输出", timestamp: 1, artifacts: [], isStreaming: true }],
    } } })));
    expect(list.scrollTop).toBe(100);
    fireEvent.click(screen.getByRole("button", { name: "回到侧边对话最新消息" }));
    expect(list.scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
  });

  it("sends a newly selected code context after earlier side turns without changing the main draft", async () => {
    useAppStore.setState({ isConnected: true, draft: "主对话草稿" });
    render(<SideChatPanel />);
    const sideId = Object.keys(useAppStore.getState().sideChats)[0];
    act(() => useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [sideId]: {
      ...state.sideChats[sideId], selectedContext: { text: "const selected = true", source: "src/selected.ts" },
      messages: [{ id: "earlier", role: "assistant", content: "先前回答", timestamp: 1, artifacts: [] }],
    } } })));
    expect(screen.getByText("const selected = true")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), { target: { value: "解释新的选区" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: sideId, primaryFile: "src/selected.ts", displayContent: "解释新的选区",
      backendContent: "File reference: src/selected.ts\n\nSelected code:\nconst selected = true\n\n解释新的选区",
      contextRefs: [expect.objectContaining({ kind: "file", path: "src/selected.ts", text: "const selected = true" })],
    })));
    expect(screen.queryByText("const selected = true")).toBeNull();
    expect(useAppStore.getState().draft).toBe("主对话草稿");
  });
});
