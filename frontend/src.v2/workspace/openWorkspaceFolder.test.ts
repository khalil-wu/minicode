import { beforeEach, describe, expect, it, vi } from "vitest";
import { sendChatMessage, resetSendDeduplication } from "../chat/sendChatMessage";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { openWorkspaceFolder } from "./openWorkspaceFolder";
import { handlePeripheralEvent } from "../chat/peripheralEvents";

const sent: unknown[] = [];

const runtimeMocks = vi.hoisted(() => ({
  pickWorkspaceDirectory: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => ({
    pickWorkspaceDirectory: runtimeMocks.pickWorkspaceDirectory,
  }),
  pickWorkspaceDirectory: runtimeMocks.pickWorkspaceDirectory,
}));

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
}));

describe("openWorkspaceFolder", () => {
  beforeEach(() => {
    sent.length = 0;
    resetSendDeduplication();
    vi.clearAllMocks();
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

  it("binds the active conversation so the next agent turn can edit that workspace", async () => {
    const opened = await openWorkspaceFolder();

    expect(opened).toBe("C:\\Desktop\\MiniCode");
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "workspace.set",
      path: "C:\\Desktop\\MiniCode",
    });

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.workingDirectory).toBe("");

    handlePeripheralEvent({
      type: "workspace.imported",
      conversation_id: "conv-project",
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
    expect(confirmedState.conversations[0]).toMatchObject({
      id: "conv-project",
      workspaceRoot: "C:\\Desktop\\MiniCode",
      worktreePath: "",
    });

    expect(sendChatMessage({
      displayContent: "change the app title",
      backendContent: "change the app title",
    })).toBe(true);

    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "change the app title",
      workspace_root: "C:\\Desktop\\MiniCode",
      permission_mode: "confirm",
      conversation_id: "conv-project",
    });
  });
});
