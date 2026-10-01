import { beforeEach, describe, expect, it, vi } from "vitest";
import { sendChatMessage, resetSendDeduplication } from "../chat/sendChatMessage";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { openWorkspaceFolder } from "./openWorkspaceFolder";
import { pushToast } from "../overlays/ToastContainer";
import { handlePeripheralEvent } from "../chat/peripheralEvents";

const sent: unknown[] = [];

const runtimeMocks = vi.hoisted(() => ({
  pickWorkspaceDirectory: vi.fn(),
  isDesktop: vi.fn(() => true),
  showPrompt: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => ({
    pickWorkspaceDirectory: runtimeMocks.pickWorkspaceDirectory,
  }),
  pickWorkspaceDirectory: runtimeMocks.pickWorkspaceDirectory,
  isDesktop: runtimeMocks.isDesktop,
}));

vi.mock("../overlays/DialogService", () => ({ showPrompt: runtimeMocks.showPrompt }));

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({
    send: (command: unknown) => {
      sent.push(command);
      return true;
    },
    sessionId: "session-test",
  }),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: vi.fn(() => "test-client-command-id"),
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async () => ({ command: "workspace.set", level: "success", message: "Workspace activated.", data: {} })),
  commandResultSucceeded: vi.fn((event: { level?: string }) => event.level !== "error" && event.level !== "failed"),
}));

describe("openWorkspaceFolder", () => {
  beforeEach(() => {
    sent.length = 0;
    resetSendDeduplication();
    vi.clearAllMocks();
    runtimeMocks.isDesktop.mockReturnValue(true);
    runtimeMocks.pickWorkspaceDirectory.mockResolvedValue("C:\\Desktop\\MiniCode");
    useAppStore.setState({
      appMode: "chat",
      conversationId: "conv-project",
      conversations: [
        {
          id: "conv-project",
          title: "Project",
          updatedAt: "2026-06-15T00:00:00.000Z",
        },
      ],
      workingDirectory: "",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isConnected: true,
      isStreaming: false,
      pendingApproval: null,
      pendingDiffReview: null,
      pendingAskUser: null,
      runtimeSession: null,
      permissionMode: "confirm",
      activeTabPath: null,
      activeEditorPath: null,
    });
  });

  it("opens a new conversation and keeps the previous project's history", async () => {
    const original = { id: "old-message", role: "user" as const, content: "Keep this with Alpha", timestamp: 1, blocks: [], artifacts: [] };
    useAppStore.setState({ workingDirectory: "C:/Alpha", messages: [original], conversations: [{
      id: "conv-project", title: "Alpha", workspaceRoot: "C:/Alpha", updatedAt: "2026-06-15T00:00:00Z",
    }] });
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async () => {
      useAppStore.setState((state) => ({ conversations: [...state.conversations, {
        id: "conv-new-project", title: "New chat", workspaceRoot: "C:\\Desktop\\MiniCode", updatedAt: "2026-06-15T00:00:01Z",
      }] }));
      useAppStore.getState().applyConversationSwitched({ conversationId: "conv-new-project" });
      return { command: "workspace.set", level: "success", message: "Conversation created.", data: { conversation_id: "conv-new-project", workspace_root: "C:\\Desktop\\MiniCode" } };
    });
    const opened = await openWorkspaceFolder();

    expect(opened).toBe("C:\\Desktop\\MiniCode");
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "workspace.set",
      path: "C:\\Desktop\\MiniCode",
      permission_mode: "confirm",
    }, "workspace.set");

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");

    handlePeripheralEvent({
      type: "workspace.imported",
      conversation_id: "conv-new-project",
      workspace_root: "C:\\Desktop\\MiniCode",
      project: {
        root_path: "C:\\Desktop\\MiniCode",
        project_type: "typescript",
        name: "MiniCode",
        description: "Desktop coding agent",
        file_count: 420,
        total_size: 1_234_567,
        has_project_instructions: false,
        index_truncated: false,
      },
      summary: "TypeScript project",
      file_count: 420,
    } as never);
    const confirmedState = useAppStore.getState();
    expect(confirmedState.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(confirmedState.conversations[1]).toMatchObject({
      id: "conv-new-project",
      workspaceRoot: "C:\\Desktop\\MiniCode",
    });
    expect(confirmedState.messages).toEqual([]);
    expect(confirmedState.conversationMessages["conv-project"]).toEqual([original]);
    expect(confirmedState.conversations[0].workspaceRoot).toBe("C:/Alpha");

    expect(sendChatMessage({
      displayContent: "change the app title",
      backendContent: "change the app title",
    })).toBe(true);

    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "change the app title",
      workspace_root: "C:\\Desktop\\MiniCode",
      permission_mode: "confirm",
      conversation_id: "conv-new-project",
    });
  });

  it("does not overwrite a newer selection when an open-folder result arrives late", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async () => {
      useAppStore.getState().setWorkingDirectory("C:/ThirdProject");
      return { command: "workspace.set", level: "success", message: "created", data: { workspace_root: "C:/SecondProject" } };
    });
    await openWorkspaceFolder();
    expect(useAppStore.getState().workingDirectory).toBe("C:/ThirdProject");
  });

  it("leaves the current conversation untouched when the folder picker is cancelled", async () => {
    runtimeMocks.pickWorkspaceDirectory.mockResolvedValueOnce(null);
    expect(await openWorkspaceFolder()).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(useAppStore.getState().conversationId).toBe("conv-project");
  });
  it("lets browser users select a backend workspace explicitly", async () => {
    runtimeMocks.isDesktop.mockReturnValue(false);
    runtimeMocks.showPrompt.mockResolvedValue("  C:/browser-project  ");
    expect(await openWorkspaceFolder()).toBe("C:/browser-project");
    expect(runtimeMocks.pickWorkspaceDirectory).not.toHaveBeenCalled();
    expect(runtimeMocks.showPrompt).toHaveBeenCalledWith(expect.objectContaining({ title: "打开项目" }));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(
      expect.objectContaining({ type: "workspace.set", path: "C:/browser-project" }), "workspace.set",
    );
  });

  it("surfaces a native picker failure instead of treating it as cancellation", async () => {
    runtimeMocks.pickWorkspaceDirectory.mockRejectedValueOnce(new Error("Picker disconnected"));
    expect(await openWorkspaceFolder()).toBeNull();
    expect(pushToast).toHaveBeenCalledWith("Picker disconnected", "error", 5000);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("does not activate a cancelled or empty browser path", async () => {
    runtimeMocks.isDesktop.mockReturnValue(false);
    runtimeMocks.showPrompt.mockResolvedValueOnce(null).mockResolvedValueOnce("   ");
    expect(await openWorkspaceFolder()).toBeNull();
    expect(await openWorkspaceFolder()).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("explains the authoritative desktop trust boundary without bypassing it", async () => {
    runtimeMocks.isDesktop.mockReturnValue(false);
    runtimeMocks.showPrompt.mockResolvedValueOnce("C:/untrusted");
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ command: "workspace.set", level: "error",
      message: "Workspace is not trusted", data: { error_code: "workspace_untrusted" } } as never);
    expect(await openWorkspaceFolder()).toBeNull();
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("请先在桌面端打开并信任"), "error", 5000);
    expect(useAppStore.getState().workingDirectory).toBe("");
  });

});
