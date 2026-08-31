import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamBuffer } from "../lib/stream-buffer";
import type { ServerEvent } from "../protocol/events";
import { useAppStore } from "../stores";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { pushToast } from "../overlays/ToastContainer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { resetSendDeduplication } from "./sendChatMessage";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("./sendChatMessage", () => ({ resetSendDeduplication: vi.fn() }));

const immediateBuffer = (
  apply: (chunk: string, conversationId?: string, itemId?: string, metadata?: Record<string, unknown>, messageId?: string) => void,
): StreamBuffer => ({
  push: apply,
  flush: vi.fn(),
  destroy: vi.fn(),
});

const seedStreamingAssistant = () => {
  useAppStore.setState({
    conversationId: "conv-stream",
    messages: [{
      id: "assistant-stream",
      role: "assistant",
      content: "",
      blocks: [],
      artifacts: [],
      timestamp: 1,
      isStreaming: true,
    }],
    conversationMessages: {},
    conversationStreaming: { "conv-stream": true },
    sideChats: {},
    isStreaming: true,
    lastUsage: null,
    agentProgress: [],
    inspectorEntries: [],
    inspectorFocus: null,
    plan: null,
    todos: [],
    pendingApproval: null,
    approvalQueue: [],
    pendingDiffReview: null,
    diffReviewQueue: [],
    diffReview: null,
    pendingAskUser: null,
    askUserQueue: [],
  });
};

const handlers = {
  textStreamBuffer: immediateBuffer((chunk, conversationId, itemId, _metadata, messageId) =>
    useAppStore.getState().appendAgentMessageDelta(
      itemId || "agent-message",
      chunk,
      conversationId,
      messageId,
    ),
  ),
  thinkingStreamBuffer: immediateBuffer((chunk, conversationId, _source, metadata, messageId) =>
    useAppStore.getState().appendThinkingChunk(chunk, conversationId, metadata, messageId),
  ),
};

const handle = (event: ServerEvent) =>
  handleChatStreamEvent(event, "conv-stream", handlers);

describe("handleChatStreamEvent typed lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    seedStreamingAssistant();
  });

  it("projects started, delta and completed into one authoritative answer block", () => {
    expect(handle({
      type: "item.started",
      item: { id: "agent-message", type: "agent_message", text: "", status: "in_progress" },
      message_id: "assistant-stream",
    } as ServerEvent)).toBe(true);
    expect(handle({
      type: "agent_message.delta",
      item_id: "agent-message",
      delta: "Done",
      message_id: "assistant-stream",
    } as ServerEvent)).toBe(true);

    let message = useAppStore.getState().messages[0];
    expect(message.content).toBe("");
    expect(message.blocks).toEqual([{
      type: "text",
      itemId: "agent-message",
      content: "Done",
      status: "in_progress",
      isStreaming: true,
    }]);
    let turns = projectMessagesToTurns(useAppStore.getState().messages, true);
    expect(turns[0]?.activeCell).toEqual(expect.objectContaining({
      kind: "streaming_assistant_tail",
      partialMarkdown: "Done",
    }));
    expect(turns[0]?.committedCells).toEqual([]);
    expect(turns[0]?.finalAnswerCell).toBeNull();

    expect(handle({
      type: "item.completed",
      item: {
        id: "agent-message",
        type: "agent_message",
        text: "Done",
        source: "model_final",
        status: "completed",
      },
      finish_reason: "stop",
      message_id: "assistant-stream",
    } as ServerEvent)).toBe(true);

    message = useAppStore.getState().messages[0];
    expect(message.content).toBe("Done");
    expect(message.blocks).toEqual([{
      type: "text",
      itemId: "agent-message",
      content: "Done",
      source: "model_final",
      status: "completed",
      isStreaming: false,
      providerRaw: undefined,
      finishReason: "stop",
    }]);
    turns = projectMessagesToTurns(useAppStore.getState().messages, false);
    expect(turns[0]?.activeCell).toBeNull();
    expect(turns[0]?.committedCells).toEqual([]);
    expect(turns[0]?.finalAnswerCell?.markdownSource).toBe("Done");
  });

  it("streams provisional text as live narration and keeps it out of the answer surface", () => {
    handle({
      type: "item.started",
      item: {
        id: "pending-message",
        type: "agent_message",
        text: "",
        source: "pending",
        status: "in_progress",
      },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "agent_message.delta",
      item_id: "pending-message",
      delta: "我先从官方气象源确认一下。",
      source: "pending",
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]?.blocks).toEqual([
      expect.objectContaining({
        type: "text",
        itemId: "pending-message",
        content: "我先从官方气象源确认一下。",
        isStreaming: true,
      }),
    ]);
    expect(useAppStore.getState().messages[0]?.blocks[0]).toHaveProperty("source", "pending");

    let turns = projectMessagesToTurns(useAppStore.getState().messages, true);
    // Provisional text is process narration, not an answer. It has to reach the
    // timeline delta by delta, while the copyable answer surface stays empty
    // until the provider settles the item.
    expect(turns[0]?.activeCell).toBeNull();
    expect(turns[0]?.finalAnswerCell).toBeNull();
    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "thinking",
        source: "model_preamble",
        content: "我先从官方气象源确认一下。",
        isStreaming: true,
      }),
    ]);

    handle({
      type: "item.completed",
      item: {
        id: "pending-message",
        type: "agent_message",
        text: "我先从官方气象源确认一下。",
        source: "commentary",
        status: "completed",
      },
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]).toMatchObject({
      content: "",
      blocks: [expect.objectContaining({
        type: "text",
        itemId: "pending-message",
        source: "commentary",
        status: "completed",
      })],
    });
    turns = projectMessagesToTurns(useAppStore.getState().messages, true);
    expect(turns[0]?.activeCell).toBeNull();
    expect(turns[0]?.finalAnswerCell).toBeNull();
    expect(turns[0]?.committedCells).toEqual([
      expect.objectContaining({
        kind: "thinking",
        source: "commentary",
        content: "我先从官方气象源确认一下。",
      }),
    ]);

    useAppStore.setState((state) => ({
      messages: state.messages.map((message) => ({
        ...message,
        blocks: message.blocks?.map((block) => block.type === "text"
          ? { ...block, source: "commentary", status: "in_progress", isStreaming: true }
          : block),
      })),
    }));
    handle({
      type: "item.completed",
      item: {
        id: "pending-message",
        type: "agent_message",
        text: "Legacy completion without a source.",
        status: "completed",
      },
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]?.blocks[0]).toMatchObject({
      type: "text",
      source: "model_final",
      content: "Legacy completion without a source.",
      isStreaming: false,
    });
  });

  it("settles the pending reasoning batch before an agent message item starts", () => {
    let pendingThinking: (() => void) | undefined;
    const delayedThinkingBuffer: StreamBuffer = {
      push: (chunk, conversationId, _source, metadata, messageId) => {
        pendingThinking = () => useAppStore.getState().appendThinkingChunk(
          chunk,
          conversationId,
          metadata,
          messageId,
        );
      },
      flush: vi.fn(() => {
        pendingThinking?.();
        pendingThinking = undefined;
      }),
      destroy: vi.fn(),
    };
    const delayedHandlers = {
      textStreamBuffer: handlers.textStreamBuffer,
      thinkingStreamBuffer: delayedThinkingBuffer,
    };

    handleChatStreamEvent({
      type: "thinking_delta",
      content: "The date is 2026-08-02.",
      source: "provider",
      message_id: "assistant-stream",
    } as ServerEvent, "conv-stream", delayedHandlers);
    handleChatStreamEvent({
      type: "item.started",
      item: {
        id: "final-message",
        type: "agent_message",
        source: "model_final",
        status: "in_progress",
      },
      message_id: "assistant-stream",
    } as ServerEvent, "conv-stream", delayedHandlers);

    expect(delayedThinkingBuffer.flush).toHaveBeenCalledOnce();
    expect(useAppStore.getState().messages[0]?.blocks).toEqual([
      expect.objectContaining({
        type: "thinking",
        content: "The date is 2026-08-02.",
        source: "provider",
      }),
      expect.objectContaining({
        type: "text",
        itemId: "final-message",
      }),
    ]);
    expect(useAppStore.getState().messages[0]?.blocks[1]).toHaveProperty("source", "model_final");
  });

  it("routes interleaved reasoning deltas by provider item identity", () => {
    handle({
      type: "thinking_delta",
      content: "A",
      item_id: "reasoning-1",
      content_index: 0,
      lifecycle: "delta",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "item.started",
      item: { id: "answer-1", type: "agent_message" },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "thinking_delta",
      content: "B",
      item_id: "reasoning-1",
      content_index: 0,
      lifecycle: "delta",
      message_id: "assistant-stream",
    } as ServerEvent);

    const blocks = useAppStore.getState().messages[0]?.blocks ?? [];
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({
      type: "thinking",
      item_id: "reasoning-1",
      content: "AB",
    });
    expect(blocks[1]).toMatchObject({ type: "text", itemId: "answer-1" });
  });

  it("settles thinking when a typed tool lifecycle starts", () => {
    handle({
      type: "thinking_delta",
      content: "Inspecting the repository",
      source: "provider",
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]?.isThinkingStreaming).toBe(true);

    handle({
      type: "tool_call",
      id: "read-1",
      name: "read_file",
      args: { file_path: "README.md" },
      status: "running",
      activity_kind: "fileRead",
      result_kind: "file",
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]?.isThinkingStreaming).toBe(false);
  });

  it("does not add permission decisions as generic transcript progress", () => {
    handle({
      type: "tool_call",
      id: "write-1",
      name: "write_file",
      args: { file_path: "a.txt" },
      status: "running",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "permission.decision",
      tool_call_id: "write-1",
      tool_name: "write_file",
      decision: "ask",
      source: "policy",
      message_id: "assistant-stream",
    } as ServerEvent);

    const tool = getToolCallsFromMessage(useAppStore.getState().messages[0])[0];
    expect(tool).toMatchObject({
      id: "write-1",
      status: "pending",
      transition: "waiting_approval",
      waitingOn: "approval",
    });
  });

  it("projects a live legacy raster image as a stable artifact without fabricating markdown", () => {
    const event = {
      type: "image_chunk",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      media_type: "image/png",
      image_data: "iVBORw0KGgo=",
      event_id: "session-1:image-1",
    } as unknown as ServerEvent;

    expect(handle(event)).toBe(true);
    expect(handle(event)).toBe(true);

    const message = useAppStore.getState().messages[0];
    expect(message.content).toBe("");
    expect(message.blocks).toEqual([]);
    expect(message.artifacts).toEqual([
      expect.objectContaining({
        artifactId: expect.stringMatching(/^legacy-image-/),
        kind: "image",
        summary: "Generated PNG image (legacy stream)",
        bytes: 8,
        mediaType: "image/png",
        url: "data:image/png;base64,iVBORw0KGgo=",
      }),
    ]);
    expect(useAppStore.getState().inspectorEntries).toEqual([]);
  });

  it("keeps replay-omitted legacy image metadata in Inspector without inventing an artifact", () => {
    expect(handle({
      type: "image_chunk",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      media_type: "image/png",
      image_data_omitted: true,
      image_data_size: 12,
      event_id: "session-1:image-replay",
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().messages[0].artifacts).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "artifact",
        targetId: expect.stringMatching(/^legacy-image-/),
        payload: {
          event: "image_chunk",
          conversation_id: "conv-stream",
          message_id: "assistant-stream",
          media_type: "image/png",
          image_data_size: 12,
          image_data_omitted: true,
          replayed: true,
          projected: false,
        },
      }),
    ]);
  });

  it("routes command output by the backend tool_call_id", () => {
    for (const id of ["cmd-1", "cmd-2"]) {
      handle({
        type: "tool_call",
        id,
        name: "run_command",
        args: { command: id },
        status: "running",
        message_id: "assistant-stream",
      } as ServerEvent);
    }
    handle({
      type: "command_output_chunk",
      id: "cmd-1",
      tool_call_id: "cmd-1",
      content: "first-only",
      stream: "stdout",
      message_id: "assistant-stream",
    } as ServerEvent);

    const tools = getToolCallsFromMessage(useAppStore.getState().messages[0]);
    expect(tools.find((tool) => tool.id === "cmd-1")?.outputPreview).toContain("first-only");
    expect(tools.find((tool) => tool.id === "cmd-2")?.outputPreview ?? "").not.toContain("first-only");
  });

  it("never falls back to a different command when an explicit tool id is unknown", () => {
    handle({
      type: "tool_call",
      id: "cmd-running",
      name: "run_command",
      args: { command: "npm test" },
      status: "running",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "command_output_chunk",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      turn_id: "turn-1",
      id: "cmd-missing",
      tool_call_id: "cmd-missing",
      content: "must-not-leak",
      stream: "stderr",
    } as ServerEvent);

    const tool = getToolCallsFromMessage(useAppStore.getState().messages[0])[0];
    expect(tool.outputPreview ?? "").not.toContain("must-not-leak");
    expect(useAppStore.getState().inspectorEntries.at(-1)).toMatchObject({
      targetKind: "tool_call",
      targetId: "cmd-missing",
      payload: {
        event: "command_output_chunk",
        conversation_id: "conv-stream",
        message_id: "assistant-stream",
        turn_id: "turn-1",
        id: "cmd-missing",
        tool_call_id: "cmd-missing",
        stream: "stderr",
        output: "must-not-leak",
        projected: false,
      },
    });
  });

  it("falls back to the latest running command only when the backend omits tool identity", () => {
    for (const id of ["cmd-first", "cmd-latest"]) {
      handle({
        type: "tool_call",
        id,
        name: "run_command",
        args: { command: id },
        status: "running",
        message_id: "assistant-stream",
      } as ServerEvent);
    }
    handle({
      type: "command_output_chunk",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      turn_id: "turn-2",
      content: "latest-only",
      stream: "stdout",
    } as ServerEvent);

    const tools = getToolCallsFromMessage(useAppStore.getState().messages[0]);
    expect(tools.find((tool) => tool.id === "cmd-first")?.outputPreview ?? "").not.toContain("latest-only");
    expect(tools.find((tool) => tool.id === "cmd-latest")?.outputPreview).toContain("latest-only");
    expect(useAppStore.getState().inspectorEntries.at(-1)).toMatchObject({
      targetKind: "tool_call",
      targetId: "cmd-latest",
      payload: expect.objectContaining({
        turn_id: "turn-2",
        stream: "stdout",
        output: "latest-only",
        projected: true,
      }),
    });
  });

  it("records unprojected command output against the owning message", () => {
    handle({
      type: "command_output_chunk",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      content: "orphan output",
      stream: "stdout",
    } as ServerEvent);

    expect(useAppStore.getState().inspectorEntries.at(-1)).toMatchObject({
      targetKind: "message",
      targetId: "assistant-stream",
      payload: expect.objectContaining({
        conversation_id: "conv-stream",
        message_id: "assistant-stream",
        stream: "stdout",
        output: "orphan output",
        projected: false,
      }),
    });
  });

  it("keeps command output in a strict 64K head-tail buffer", () => {
    handle({
      type: "tool_call",
      id: "cmd-bounded",
      name: "run_command",
      args: { command: "long" },
      status: "running",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "command_output_chunk",
      id: "cmd-bounded",
      tool_call_id: "cmd-bounded",
      content: `HEAD${"x".repeat(80_000)}TAIL`,
      stream: "stdout",
      message_id: "assistant-stream",
    } as ServerEvent);

    const output = getToolCallsFromMessage(useAppStore.getState().messages[0])[0]?.outputPreview ?? "";
    expect(output.length).toBeLessThanOrEqual(64 * 1024);
    expect(output.startsWith("HEAD")).toBe(true);
    expect(output.endsWith("TAIL")).toBe(true);
    expect(output).toContain("characters omitted");
  });

  it("closes rejected output before starting a distinct item", () => {
    const firstId = "iter-1:agent-message:1";
    const secondId = "iter-1:agent-message:2";
    handle({
      type: "item.started",
      item: { id: firstId, type: "agent_message", text: "", status: "in_progress" },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "agent_message.delta", item_id: firstId, delta: "rejected", message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "item.completed",
      item: { id: firstId, type: "agent_message", text: "rejected", source: "cancelled", status: "cancelled" },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "item.started",
      item: { id: secondId, type: "agent_message", text: "", status: "in_progress" },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "agent_message.delta", item_id: secondId, delta: "accepted", message_id: "assistant-stream",
    } as ServerEvent);

    const blocks = useAppStore.getState().messages[0]?.blocks;
    expect(blocks).toHaveLength(2);
    expect(blocks?.[0]).toEqual(expect.objectContaining({ content: "rejected", status: "cancelled", isStreaming: false }));
    expect(blocks?.[1]).toEqual(expect.objectContaining({ content: "accepted", isStreaming: true }));
  });

  it("settles an unfinished item as partial when done arrives", () => {
    handle({
      type: "item.started",
      item: { id: "agent-message", type: "agent_message", text: "", status: "in_progress" },
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "agent_message.delta", item_id: "agent-message", delta: "partial", message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "done", status: "failed", conversation_id: "conv-stream", message_id: "assistant-stream",
    } as ServerEvent);

    const message = useAppStore.getState().messages[0];
    expect(message.content).toBe("partial");
    expect(message.terminalStatus).toBe("failed");
    expect(message.blocks?.[0]).toEqual(expect.objectContaining({
      content: "partial",
      status: "partial",
      isStreaming: false,
    }));
  });

  it("does not replace an authoritative deferred Provider trace with the reduced done fallback", () => {
    useAppStore.setState({
      inspectorEntries: [{
        targetKind: "provider",
        targetId: "iter:1:provider:1",
        timestamp: 1,
        payload: {
          kind: "provider_trace",
          diagnostics_deferred: true,
          finish_reason: "end_turn",
        },
      }],
    });

    handle({
      type: "done",
      status: "completed",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      usage: { input_tokens: 29, output_tokens: 14 },
      provider_raw: {
        provider: "custom",
        trace_id: "iter:1:provider:1",
        usage: { input_tokens: 29, output_tokens: 14 },
      },
    } as unknown as ServerEvent);

    expect(useAppStore.getState().inspectorEntries).toEqual([{
      targetKind: "provider",
      targetId: "iter:1:provider:1",
      timestamp: 1,
      payload: {
        kind: "provider_trace",
        diagnostics_deferred: true,
        finish_reason: "end_turn",
      },
    }]);
  });

  it("renders one terminal error after a recoverable error", () => {
    handle({
      type: "error",
      conversation_id: "conv-stream",
      message: "network unavailable",
      error_type: "network",
      recoverable: true,
    } as ServerEvent);
    expect(useAppStore.getState().messages.filter((message) => message.role === "system")).toHaveLength(0);

    handle({
      type: "done",
      status: "failed",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
    } as ServerEvent);

    const turns = projectMessagesToTurns(useAppStore.getState().messages, false);
    const errorCells = turns[0]?.committedCells.filter((cell) => cell.kind === "error") ?? [];
    expect(errorCells).toHaveLength(1);
    expect(errorCells[0]).toEqual(expect.objectContaining({
      message: "network unavailable",
      recoverable: true,
    }));
    expect(useAppStore.getState().messages[0]?.failureRecoverable).toBe(true);
  });

  it("keeps a fatal authentication failure on the assistant turn without appending a system notice", () => {
    handle({
      type: "error",
      conversation_id: "conv-stream",
      message: "401 Unauthorized: invalid api key",
      error_type: "auth",
      recoverable: false,
    } as ServerEvent);

    const messages = useAppStore.getState().messages;
    expect(messages.filter((message) => message.role === "system")).toHaveLength(0);
    expect(messages[0]?.role).toBe("assistant");
    expect(messages[0]?.terminalStatus).toBe("failed");
    expect(messages[0]?.failureMessage).toContain("API Key");
  });

  it("keeps recoverable errors scoped to the matching concurrent turn", () => {
    useAppStore.setState({
      messages: [
        {
          id: "assistant-first",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        },
        {
          id: "assistant-second",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 2,
          isStreaming: true,
        },
      ],
    });

    handle({
      type: "error",
      conversation_id: "conv-stream",
      message_id: "assistant-first",
      message: "first turn failed",
      error_type: "network",
      recoverable: true,
    } as ServerEvent);
    handle({
      type: "error",
      conversation_id: "conv-stream",
      message_id: "assistant-second",
      message: "second turn failed",
      error_type: "network",
      recoverable: true,
    } as ServerEvent);

    handle({
      type: "done",
      status: "failed",
      conversation_id: "conv-stream",
      message_id: "assistant-second",
    } as ServerEvent);
    handle({
      type: "done",
      status: "failed",
      conversation_id: "conv-stream",
      message_id: "assistant-first",
    } as ServerEvent);

    const [first, second] = useAppStore.getState().messages;
    expect(first.failureMessage).toBe("first turn failed");
    expect(second.failureMessage).toBe("second turn failed");
    expect(first.failureRecoverable).toBe(true);
    expect(second.failureRecoverable).toBe(true);
  });

  it("uses authoritative fatal recoverability from done", () => {
    handle({
      type: "error",
      conversation_id: "conv-stream",
      message: "provider failed",
      error_type: "network",
      recoverable: true,
    } as ServerEvent);

    handle({
      type: "done",
      status: "failed",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      failure_recoverable: false,
    } as unknown as ServerEvent);

    expect(useAppStore.getState().messages[0]).toMatchObject({
      failureMessage: "provider failed",
      failureRecoverable: false,
    });
  });

  it("shows a live global error without terminating the active conversation", () => {
    useAppStore.setState({
      workingDirectory: "C:\\workspace",
      conversations: [{
        id: "conv-stream",
        title: "Active conversation",
        updatedAt: "2026-08-16T00:00:00Z",
        workspaceRoot: "C:\\workspace",
      }],
    });

    expect(handle({
      type: "error",
      message: "Invalid JSON message",
      error_type: "protocol",
      recoverable: true,
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(pushToast).toHaveBeenCalledWith("Invalid JSON message", "error", 6000);
    expect(state.conversationId).toBe("conv-stream");
    expect(state.workingDirectory).toBe("C:\\workspace");
    expect(state.messages[0]).toMatchObject({ id: "assistant-stream", isStreaming: true });
    expect(state.isStreaming).toBe(true);
    expect(resetSendDeduplication).not.toHaveBeenCalled();
  });

  it("hydrates replayed owned errors without replaying destructive recovery or toasts", () => {
    useAppStore.setState({
      workingDirectory: "C:\\workspace",
      conversations: [{
        id: "conv-stream",
        title: "Active conversation",
        updatedAt: "2026-08-16T00:00:00Z",
        workspaceRoot: "C:\\workspace",
        worktreePath: "C:\\workspace\\.worktrees\\conv-stream",
        gitIsolated: true,
      }],
    });

    expect(handle({
      type: "error",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      message: "Workspace is unavailable",
      error_type: "tool",
      error_code: "workspace_missing",
      recoverable: false,
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(pushToast).not.toHaveBeenCalled();
    expect(resetSendDeduplication).not.toHaveBeenCalled();
    expect(state.workingDirectory).toBe("C:\\workspace");
    expect(state.conversations[0]).toMatchObject({
      id: "conv-stream",
      workspaceRoot: "C:\\workspace",
      worktreePath: "C:\\workspace\\.worktrees\\conv-stream",
      gitIsolated: true,
    });
    expect(state.messages[0]).toMatchObject({
      id: "assistant-stream",
      terminalStatus: "failed",
      failureMessage: "Workspace is unavailable",
    });
  });

  it("does not delete a conversation when conversation.not_found is replayed", () => {
    useAppStore.setState({
      conversations: [{
        id: "conv-stream",
        title: "Active conversation",
        updatedAt: "2026-08-16T00:00:00Z",
      }],
    });

    expect(handle({
      type: "error",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      message: "Conversation not found",
      error_type: "tool",
      error_code: "conversation.not_found",
      recoverable: true,
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().conversationId).toBe("conv-stream");
    expect(useAppStore.getState().conversations).toEqual([
      expect.objectContaining({ id: "conv-stream" }),
    ]);
    expect(pushToast).not.toHaveBeenCalled();
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(resetSendDeduplication).not.toHaveBeenCalled();
  });

  it("clears pending provider progress when a live conversation is missing", () => {
    useAppStore.setState({
      pendingProviderProgress: {
        "conv-stream\u0000assistant-stream": [{
          type: "progress",
          id: "provider:connection:missing:iteration-1",
          stage: "status",
          status: "running",
          message: "正在重连",
          timestamp: 1,
        }],
        "conv-other\u0000assistant-other": [{
          type: "progress",
          id: "provider:connection:other:iteration-1",
          stage: "status",
          status: "running",
          message: "正在重连",
          timestamp: 1,
        }],
      },
    });

    expect(handle({
      type: "error",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      message: "Conversation not found",
      error_type: "tool",
      error_code: "conversation.not_found",
      recoverable: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingProviderProgress).toEqual({
      "conv-other\u0000assistant-other": [expect.objectContaining({
        id: "provider:connection:other:iteration-1",
      })],
    });
  });

  it("ignores lifecycle events targeted at an old assistant message", () => {
    expect(handle({
      type: "agent_message.delta",
      item_id: "agent-message",
      delta: "stale",
      message_id: "assistant-old",
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().messages[0]?.blocks).toEqual([]);
  });

  it("projects process and tool events without treating their text as an answer", () => {
    handle({
      type: "agent.item",
      id: "process-1",
      item_id: "process-1",
      kind: "process_text",
      content: "Inspecting files",
      status: "completed",
      order: 3,
      seq: 42,
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "tool_call",
      id: "tool-1",
      name: "read_file",
      args: { path: "README.md" },
      status: "running",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "tool_result",
      id: "tool-1",
      summary: "Read README",
      status: "success",
      is_error: false,
      message_id: "assistant-stream",
    } as ServerEvent);

    const message = useAppStore.getState().messages[0];
    expect(message.content).toBe("");
    expect(message.blocks).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "process", id: "process-1", order: 3 }),
      expect.objectContaining({ type: "tool_call" }),
    ]));
    const process = message.blocks?.find((block) => block.type === "process");
    expect(process).not.toHaveProperty("seq");
    expect(getToolCallsFromMessage(message)[0]).toEqual(expect.objectContaining({
      id: "tool-1",
      status: "success",
    }));
  });

  it("keeps debug and retracted process evidence in Inspector without rendering it", () => {
    handle({
      type: "agent.item",
      id: "debug-1",
      item_id: "debug-1",
      kind: "status",
      content: "Provider retry ladder selected attempt 2.",
      status: "completed",
      visibility: "debug",
      source: "runtime",
      message_id: "assistant-stream",
    } as ServerEvent);

    expect(useAppStore.getState().messages[0]?.blocks ?? []).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "message",
        targetId: "debug-1",
        payload: expect.objectContaining({
          event: "agent.item",
          visibility: "debug",
          content: "Provider retry ladder selected attempt 2.",
        }),
      }),
    ]);

    handle({
      type: "agent.item",
      id: "process-2",
      item_id: "process-2",
      kind: "process_text",
      content: "Temporary process text",
      status: "completed",
      visibility: "timeline",
      message_id: "assistant-stream",
    } as ServerEvent);
    expect(useAppStore.getState().messages[0]?.blocks).toEqual([
      expect.objectContaining({ type: "process", id: "process-2" }),
    ]);

    handle({
      type: "agent.item",
      id: "process-2",
      item_id: "process-2",
      kind: "process_text",
      status: "retracted",
      visibility: "timeline",
      reason: "superseded by final answer",
      message_id: "assistant-stream",
    } as ServerEvent);
    expect(useAppStore.getState().messages[0]?.blocks ?? []).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toEqual(expect.arrayContaining([
      expect.objectContaining({
        targetKind: "message",
        targetId: "process-2",
        payload: expect.objectContaining({
          status: "retracted",
          reason: "superseded by final answer",
          retracted: true,
        }),
      }),
    ]));
  });

  it("updates one tool record when transport scope changes across its lifecycle", () => {
    handle({
      type: "tool_call",
      id: "write-1",
      name: "write_file",
      args: {},
      status: "pending",
      turn_id: "run-1",
      iteration_id: "iter:1",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "tool_call",
      id: "write-1",
      name: "write_file",
      args: { file_path: "src/app.ts" },
      status: "pending",
      turn_id: "run-1",
      iteration_id: "iter:1",
      message_id: "assistant-stream",
    } as ServerEvent);

    let records = getToolCallsFromMessage(useAppStore.getState().messages[0]);
    expect(records).toHaveLength(1);
    expect(records[0]).toEqual(expect.objectContaining({
      args: { file_path: "src/app.ts" },
      status: "pending",
    }));

    handle({
      type: "tool_call",
      id: "write-1",
      name: "write_file",
      args: { file_path: "src/app.ts" },
      status: "running",
      turn_id: "assistant-stream",
      iteration_id: "iter:1",
      activity_kind: "fileChange",
      result_kind: "edit",
      display_hint: "Write",
      message_id: "assistant-stream",
    } as ServerEvent);
    handle({
      type: "tool_result",
      id: "write-1",
      summary: "Wrote src/app.ts",
      status: "success",
      is_error: false,
      turn_id: "assistant-stream",
      iteration_id: "iter:1",
      diff: { plus: 3, minus: 1, patch: "@@\n-old\n+new" },
      message_id: "assistant-stream",
    } as ServerEvent);

    records = getToolCallsFromMessage(useAppStore.getState().messages[0]);
    expect(records).toHaveLength(1);
    expect(records[0]).toEqual(expect.objectContaining({
      id: "write-1",
      args: { file_path: "src/app.ts" },
      status: "success",
      diff: expect.objectContaining({ plus: 3, minus: 1 }),
    }));
  });

  it("hydrates a reconnect snapshot instead of reconstructing state from prose", () => {
    handle({
      type: "stream_resume",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      content_blocks: [{
        type: "text",
        itemId: "agent-message",
        content: "Recovered",
        source: "partial",
        status: "partial",
        isStreaming: false,
      }],
      tool_calls_pending: [],
    } as ServerEvent);

    const message = useAppStore.getState().messages[0];
    expect(message.content).toBe("Recovered");
    expect(message.blocks).toEqual([expect.objectContaining({
      type: "text",
      itemId: "agent-message",
      status: "partial",
    })]);
  });

  it("does not revive a message after the terminal fence", () => {
    handle({
      type: "done",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      status: "completed",
      usage: {},
    } as ServerEvent);

    handle({
      type: "stream_resume",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      turn_id: "turn-old",
      event_seq: 12,
      content_blocks: [{ type: "text", content: "stale recovered text" }],
      tool_calls_pending: [],
    } as unknown as ServerEvent);

    const message = useAppStore.getState().messages[0];
    expect(message.isStreaming).toBe(false);
    expect(message.terminalStatus).toBe("completed");
    expect(message.content).not.toBe("stale recovered text");
    expect(useAppStore.getState().inspectorEntries.at(-1)?.payload).toMatchObject({
      event: "stream_resume",
      ignored: true,
      reason: "target_already_terminal",
    });
  });

  it("rejects a resume snapshot owned by an older turn", () => {
    useAppStore.setState({
      messages: [{
        ...useAppStore.getState().messages[0],
        turnId: "turn-new",
      }],
    });

    handle({
      type: "stream_resume",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      turn_id: "turn-old",
      event_seq: 8,
      content_blocks: [{ type: "text", content: "old turn" }],
      tool_calls_pending: [],
    } as unknown as ServerEvent);

    expect(useAppStore.getState().messages[0]).toMatchObject({
      turnId: "turn-new",
      content: "",
      isStreaming: true,
    });
    expect(useAppStore.getState().inspectorEntries.at(-1)?.payload).toMatchObject({
      ignored: true,
      reason: "turn_mismatch",
    });
  });

  it("keeps the newest authoritative resume event sequence", () => {
    const snapshot = (eventSeq: number, content: string) => ({
      type: "stream_resume",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      turn_id: "turn-resume-seq",
      event_seq: eventSeq,
      stream_status: "running",
      content_blocks: [{
        type: "text",
        itemId: "agent-message",
        content,
        status: "in_progress",
        isStreaming: true,
      }],
      tool_calls_pending: [],
    } as unknown as ServerEvent);

    handle(snapshot(10, "new snapshot"));
    handle(snapshot(9, "old snapshot"));

    const resumedMessage = useAppStore.getState().messages[0];
    // While the authoritative snapshot is still running, answer text lives in
    // the structured streaming block. The legacy `content` field is reserved
    // for sealed completed/partial answer blocks.
    expect(resumedMessage.content).toBe("");
    expect(resumedMessage.blocks).toEqual([
      expect.objectContaining({
        type: "text",
        itemId: "agent-message",
        content: "new snapshot",
        status: "in_progress",
        isStreaming: true,
      }),
    ]);
    expect(useAppStore.getState().inspectorEntries.at(-1)?.payload).toMatchObject({
      ignored: true,
      reason: "stale_event_seq",
      event_seq: 9,
    });
  });

  it("clears prompts owned by the terminal conversation", () => {
    useAppStore.setState({
      pendingApproval: { requestId: "approval-done", conversationId: "conv-stream", toolName: "run_command", args: {} },
      approvalQueue: [
        { requestId: "approval-done-queued", conversationId: "conv-stream", toolName: "write_file", args: {} },
        { requestId: "approval-other", conversationId: "conv-other", toolName: "read_file", args: {} },
      ],
      pendingDiffReview: { requestId: "diff-done", conversationId: "conv-stream", diff: "patch" },
      diffReviewQueue: [
        { requestId: "diff-done-queued", conversationId: "conv-stream", diff: "queued patch" },
        { requestId: "diff-other", conversationId: "conv-other", diff: "other patch" },
      ],
      pendingAskUser: { requestId: "ask-done", conversationId: "conv-stream", question: "Continue?" },
      askUserQueue: [
        { requestId: "ask-done-queued", conversationId: "conv-stream", question: "Still continue?" },
        { requestId: "ask-other", conversationId: "conv-other", question: "Other?" },
      ],
    });

    handle({ type: "done", conversation_id: "conv-stream", message_id: "assistant-stream" } as ServerEvent);

    const state = useAppStore.getState();
    expect(state.pendingApproval?.requestId).toBe("approval-other");
    expect(state.approvalQueue).toEqual([]);
    expect(state.pendingDiffReview?.requestId).toBe("diff-other");
    expect(state.diffReviewQueue).toEqual([]);
    expect(state.pendingAskUser?.requestId).toBe("ask-other");
    expect(state.askUserQueue).toEqual([]);
  });

  it("does not add replayed done usage to cumulative totals", () => {
    useAppStore.setState({
      usageTotals: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, promptCacheTotal: 0, reasoning: 0, turns: 1 },
      pendingApproval: {
        requestId: "approval-current",
        conversationId: "conv-stream",
        toolName: "write_file",
        args: {},
      },
    });

    handle({
      type: "done",
      conversation_id: "conv-stream",
      message_id: "assistant-stream",
      status: "completed",
      usage: { input_tokens: 7, output_tokens: 3 },
      replayed: true,
    } as unknown as ServerEvent);

    expect(useAppStore.getState().usageTotals).toEqual({
      input: 10,
      output: 5,
      cacheRead: 0,
      cacheWrite: 0,
      promptCacheTotal: 0,
      reasoning: 0,
      turns: 1,
    });
    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-current");
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(resetSendDeduplication).not.toHaveBeenCalled();
  });
});
