import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { buildInterruptCommand } from "../lib/interrupt-command";
import { resetSendDeduplication, sendChatMessage } from "./sendChatMessage";
import { handleRuntimeEvent } from "./runtimeEvents";
import type { ServerEvent } from "../protocol/events";

const transport = vi.hoisted(() => ({ send: vi.fn(() => true) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => transport }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const initialState = useAppStore.getState();

beforeEach(() => {
  useAppStore.setState({ ...initialState, conversationId: null, messages: [], conversations: [], isStreaming: false, runtimeSession: null, workingDirectory: "C:/workspace", activeTabPath: "src/main.py", pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [] }, true);
  resetSendDeduplication();
  transport.send.mockClear();
});
afterEach(() => useAppStore.setState(initialState, true));

describe("release input ownership", () => {
  it("admits a checkpoint-resume assistant before its output arrives", () => {
    useAppStore.setState({ conversationId: "resumed", messages: [{ id: "old", role: "assistant", content: "", artifacts: [], timestamp: 1, isStreaming: false, terminalStatus: "interrupted" }] });
    handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "resumed-run", conversation_id: "resumed", message_id: "server-assistant" } as ServerEvent);
    useAppStore.getState().startAgentMessage("answer", "resumed", "server-assistant", "model_final");
    useAppStore.getState().completeAgentMessage({ id: "answer", text: "Resumed result", source: "model_final", status: "completed" }, "resumed", undefined, "server-assistant");
    expect(useAppStore.getState().messages.at(-1)?.blocks).toContainEqual(expect.objectContaining({ type: "text", content: "Resumed result" }));
    expect(useAppStore.getState().messages[0].terminalStatus).toBe("interrupted");
  });

  it("sends the conversation-owned workspace and editor file on the first turn", () => {
    useAppStore.setState({ conversationId: "first", conversations: [{ id: "first", workspaceRoot: "C:/workspace" }] });
    expect(sendChatMessage({ displayContent: "Fix this file" })).toBe(true);
    expect(transport.send).toHaveBeenCalledWith(expect.objectContaining({ workspace_root: "C:/workspace", primary_file: "src/main.py", active_tab_path: "src/main.py" }));
  });

  it("fences Stop using the optimistic message before a conversation ID arrives", () => {
    expect(sendChatMessage({ displayContent: "Start work" })).toBe(true);
    const sent = transport.send.mock.calls[0][0];
    expect(buildInterruptCommand(useAppStore.getState()).message_id).toBe(sent.assistant_message_id);
  });

  it("keeps side-chat file references separate from the main editor", () => {
    useAppStore.setState({ conversationId: "main", conversations: [{ id: "side", title: "Side", workspaceRoot: "C:/side", gitBranch: "", archived: false, updatedAt: 0, createdAt: 0, messageCount: 0 }], sideChats: {} });
    expect(sendChatMessage({ displayContent: "Explain selection", conversationId: "side", primaryFile: "src/side.py" })).toBe(true);
    expect(transport.send).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: "side", workspace_root: "C:/side", primary_file: "src/side.py" }));
    expect(transport.send.mock.calls[0][0]).not.toHaveProperty("active_tab_path");
  });
});
