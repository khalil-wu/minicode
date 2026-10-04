// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { resetSendDeduplication, sendChatMessage } from "./sendChatMessage";

const { send } = vi.hoisted(() => ({ send: vi.fn(() => true) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ send }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => {
  send.mockClear();
  resetSendDeduplication();
  useAppStore.setState({
    conversationId: "chat-b", workingDirectory: "C:/B", agentMode: "build",
    activeTabPath: "b.ts", isStreaming: false, messages: [], sideChats: {},
    conversationMessages: {}, conversationStreaming: {}, runtimeSession: null,
    conversations: [
      { id: "chat-a", title: "A", updatedAt: "2026-10-02", workspaceRoot: "C:/A" },
      { id: "chat-b", title: "B", updatedAt: "2026-10-02", workspaceRoot: "C:/B" },
    ],
  });
});

describe("source audit input and projection contracts", () => {
  it("sends a prepared message using the original mode and file after switching chats", () => {
    expect(sendChatMessage({
      backendContent: "explain this file", conversationId: "chat-a", agentMode: "explore",
      primaryFile: "a.ts", skipLocalAppend: true,
    })).toBe(true);
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      conversation_id: "chat-a", workspace_root: "C:/A", agent_mode: "explore", primary_file: "a.ts",
    }));
    expect(useAppStore.getState().conversationId).toBe("chat-b");
    expect(useAppStore.getState().agentMode).toBe("build");
  });

  it("keeps a missing command exit code unknown instead of projecting exit 0", () => {
    const record = {
      id: "command", name: "run_command", args: { command: "build" }, status: "failed",
      startedAt: 1, finishedAt: 2, errorInfo: { exit_code: null },
    } as unknown as ToolCallRecord;
    const messages: ChatMessage[] = [
      { id: "user", role: "user", content: "build", timestamp: 1, artifacts: [] },
      { id: "assistant", role: "assistant", content: "", timestamp: 2, artifacts: [],
        terminalStatus: "failed", blocks: [{ type: "tool_call", record }] },
    ];
    const exec = projectMessagesToTurns(messages)[0].committedCells.find((cell) => cell.kind === "exec");
    expect(exec?.kind).toBe("exec");
    if (exec?.kind === "exec") {
      expect(exec.status).toBe("failed");
      expect(exec.exitCode).toBeUndefined();
    }
  });
});
