/**
 * A renderer reload during a running turn restores the in-flight assistant from
 * the backend's periodic projection (termination_reason "run_in_progress").
 * That projection is not a terminal state: the backend stream slot, announced
 * by the active-stream snapshot and stream_resume, owns the message until done.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false, media: "", onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    }),
  });
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { useAppStore } from "../stores";
import { handleSessionEvent } from "./sessionEvents";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

// Module-level stream fences outlive a test; each case gets its own turn identity.
let caseNumber = 0;
let C = "";
let A = "";
let TURN = "";
const nextTurnIdentity = () => {
  caseNumber += 1;
  C = `conv_reload_${caseNumber}`;
  A = `a_reload_assistant_${caseNumber}`;
  TURN = `run_reload_${caseNumber}`;
};
const now = new Date().toISOString();

const immediateBuffer = (kind: "text" | "thinking") => ({
  push: (chunk: string, conversationId?: string, source?: string, metadata?: Record<string, unknown>, messageId?: string) => {
    if (kind === "text") {
      useAppStore.getState().appendAgentMessageDelta(source || "agent-message", chunk, conversationId, messageId,
        typeof metadata?.source === "string" ? metadata.source : undefined);
    } else {
      useAppStore.getState().appendThinkingChunk(chunk, conversationId, metadata as never, messageId);
    }
  },
  flush: () => {},
  destroy: () => {},
});
const buffers = { textStreamBuffer: immediateBuffer("text"), thinkingStreamBuffer: immediateBuffer("thinking") };

const conversationPayload = (withPartialAssistant: boolean) => ({
  id: C,
  title: "reload",
  conversation_type: "main",
  archived: false,
  updated_at: now,
  created_at: now,
  revision: 4,
  workspace_root: "",
  transcript: [
    { id: "user_prev", role: "user", content: "earlier", timestamp: now },
    {
      id: "a_prev", role: "assistant", content: "earlier answer", timestamp: now,
      terminal_status: "completed", completed_at: Date.now() - 60_000,
      blocks: [{ type: "text", itemId: "p1", content: "earlier answer", source: "model_final", status: "completed", isStreaming: false }],
    },
    { id: "user_now", role: "user", content: "run ls then answer", timestamp: now },
    ...(withPartialAssistant ? [{
      id: A, role: "assistant", content: "", timestamp: now,
      terminal_status: "partial", termination_reason: "run_in_progress", duration_ms: 1500,
      blocks: [
        { type: "text", itemId: "item_1", content: "Let me look.", source: "commentary", status: "completed", isStreaming: false },
        { type: "tool_call", record: { id: "call_1", name: "run_command", args: { command: "ls" }, status: "partial", startedAt: Date.now() - 1000 } },
      ],
    }] : []),
  ],
});

const runtimeSnapshot = (withPartialAssistant: boolean) => ({
  session_id: "session_reload",
  parent_session_id: null,
  active_conversation_id: C,
  active_conversation: conversationPayload(withPartialAssistant),
  active_task_id: "task_1",
  active_stream_conversation_ids: [C],
  permission_mode: "confirm",
  pending_approval_count: 0,
  pending_approvals: [],
  queued_user_messages: [],
  pending_turn_inputs: [],
  running_tasks: [],
  forks: [],
  invoked_skill_names: [],
  task_summary: { total: 1, pending: 0, running: 1, completed: 0, failed: 0, cancelled: 0 },
});

const wire = (withPartialAssistant: boolean) => [
  {
    type: "session.restored", seq: 101, event_id: "s:1:101", timestamp: now,
    session_id: "session_reload", restored: true, active_conversation_id: C,
    conversation_switched_follows: true,
    conversation: conversationPayload(withPartialAssistant),
    active_conversation: conversationPayload(withPartialAssistant),
    workspace: null, working_directory: "", model: "m", current_model: "m", provider: "p",
    provider_id: "", base_url: "", wire_api: "", available_models: ["m"], models_source: "",
    session: runtimeSnapshot(withPartialAssistant), messages: [], error: null,
    missed_events: false, event_log_gap: false, snapshot_required: false, cursor_reset: false,
    requested_last_seq: 0, last_seq: 0, current_seq: 100, replayed_events: 0, snapshot_at: now,
  },
  {
    type: "conversation.switched", seq: 102, event_id: "s:1:102", timestamp: now,
    conversation_id: C, conversation: conversationPayload(withPartialAssistant), is_hydrating: false,
    session: runtimeSnapshot(withPartialAssistant), snapshot_at: now,
  },
  {
    type: "stream_resume", seq: 103, event_id: "s:1:103", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN,
    tool_calls_pending: [{ id: "call_1", name: "run_command", args: { command: "ls" }, status: "running", transition: "streaming_output" }],
    content_blocks: [
      { type: "text", itemId: "item_1", content: "Let me look.", source: "commentary", status: "completed", isStreaming: false },
      { type: "tool_call", record: { id: "call_1", name: "run_command", args: { command: "ls" }, status: "running", transition: "streaming_output" } },
    ],
    phase: "tool", stream_status: "running", event_seq: 12, last_event_type: "tool_output_delta",
    tool_states: [{ id: "call_1", name: "run_command", args: { command: "ls" }, status: "running", transition: "streaming_output" }],
  },
  {
    type: "tool_result", seq: 104, previous_replay_seq: 100, event_id: "s:1:104", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN, id: "call_1", summary: "ok", status: "success",
  },
  {
    type: "item.started", seq: 105, previous_replay_seq: 104, event_id: "s:1:105", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN,
    item: { id: "item_2", type: "agent_message", text: "", source: "pending", status: "in_progress" },
  },
  {
    type: "agent_message.delta", seq: 106, event_id: "s:1:106", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN, item_id: "item_2", delta: "FINAL ANSWER",
  },
  {
    type: "item.completed", seq: 107, previous_replay_seq: 105, event_id: "s:1:107", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN,
    item: { id: "item_2", type: "agent_message", text: "FINAL ANSWER", source: "model_final", status: "completed" },
  },
  {
    type: "done", seq: 108, previous_replay_seq: 107, event_id: "s:1:108", timestamp: now,
    conversation_id: C, message_id: A, turn_id: TURN, status: "completed",
    usage: {
      input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      input_includes_cache_read: true, input_includes_cache_write: true,
      ordinary_input_tokens: 0, prompt_cache_total_tokens: 0,
    },
  },
];

const SESSION_TYPES = new Set(["session.restored", "session.synced", "conversation.switched"]);

const deliver = (raw: Record<string, unknown>) => {
  const event = normalizeInboundServerEvent(raw);
  if (!event) throw new Error(`validator dropped ${String(raw.type)}`);
  const cid = (event as { conversation_id?: string }).conversation_id;
  const handled = SESSION_TYPES.has(event.type)
    ? handleSessionEvent(event, buffers as never)
    : handleChatStreamEvent(event, cid, buffers as never);
  if (!handled) throw new Error(`unhandled ${event.type}`);
};

const freshStore = () => {
  useAppStore.setState({
    isConnected: true, conversationId: null, conversations: [], messages: [], conversationMessages: {},
    conversationStreaming: {}, isStreaming: false, inspectorEntries: [],
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [],
    pendingDiffReview: null, diffReviewQueue: [],
  });
};

const summarize = () => {
  const state = useAppStore.getState();
  const assistant = state.messages.find((m) => m.id === A);
  const rejected = state.inspectorEntries
    .filter((entry) => (entry.payload as Record<string, unknown>)?.event === "stream_resume")
    .map((entry) => (entry.payload as Record<string, unknown>).reason);
  return {
    activeConversation: state.conversationId,
    conversationIsStreaming: state.isStreaming,
    assistantFound: Boolean(assistant),
    assistantContent: assistant?.content,
    assistantTerminalStatus: assistant?.terminalStatus,
    assistantIsStreaming: assistant?.isStreaming,
    assistantTextBlocks: (assistant?.blocks ?? []).filter((b) => b.type === "text").map((b) => (b as { content: string }).content),
    toolStatuses: (assistant?.blocks ?? []).filter((b) => b.type === "tool_call").map((b) => (b as { record: { status: string } }).record.status),
    streamResumeRejections: rejected,
  };
};

describe("renderer reload while a turn is running", () => {
  beforeEach(() => {
    nextTurnIdentity();
    freshStore();
  });

  it.each([true, false])("finishes the turn from the live stream (in-flight projection persisted: %s)", (withPartial) => {
    for (const raw of wire(withPartial)) deliver(raw);
    const result = summarize();
    expect(result.streamResumeRejections).toEqual([]);
    expect(result.assistantTextBlocks).toContain("FINAL ANSWER");
    expect(result.toolStatuses).toEqual(["success"]);
    expect(result.assistantTerminalStatus).toBe("completed");
    expect(result.assistantIsStreaming).toBeFalsy();
  });

  it("keeps an in-flight projection partial when no stream owns it", () => {
    for (const raw of wire(true).slice(0, 2)) {
      const idle = JSON.parse(JSON.stringify(raw));
      idle.session.active_stream_conversation_ids = [];
      idle.session.active_task_id = "";
      deliver(idle);
    }
    const assistant = useAppStore.getState().messages.find((message) => message.id === A);
    expect(assistant?.terminalStatus).toBe("partial");
    expect(assistant?.isStreaming).toBeFalsy();
  });
  it("keeps the newest live item and the recent-page cursor through deferred restore and session sync", () => {
    const events = wire(true);
    const snapshot = conversationPayload(true);
    const page = { ...snapshot, transcript: [
      ...Array.from({ length: 80 - snapshot.transcript.length }, (_, index) => ({ id: `history-${index}`, role: index % 2 ? "assistant" : "user", content: `History ${index}`, timestamp: now })),
      ...snapshot.transcript,
    ], transcript_page: { before_message_id: "history-0", has_more: true, total_messages: 240 } };
    const withPage = (raw: Record<string, unknown>) => ({ ...raw, conversation: page, active_conversation: page,
      session: { ...runtimeSnapshot(true), active_conversation: page } });
    deliver(withPage(events[0]));
    deliver({ ...withPage(events[1]), is_hydrating: true });
    expect(useAppStore.getState().messages).toHaveLength(80);
    expect(useAppStore.getState().conversationHistoryPages[C]).toMatchObject({ hasMore: true, beforeMessageId: "history-0" });
    for (const event of events.slice(2, 6)) deliver(event);
    expect(summarize().assistantTextBlocks).toContain("FINAL ANSWER");
    deliver({ ...withPage(events[1]), is_hydrating: false });
    expect(useAppStore.getState().conversationHydration[C]?.isHydrating).toBe(false);
    expect(summarize().assistantTextBlocks).toContain("FINAL ANSWER");
    deliver({ ...withPage(events[0]), type: "session.synced", synced: true, protocol_version: "1.0.0" });
    expect(summarize().assistantTextBlocks).toContain("FINAL ANSWER");
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(useAppStore.getState().conversationHistoryPages[C]).toMatchObject({ hasMore: true, beforeMessageId: "history-0" });
    for (const event of events.slice(6)) deliver(event);
    expect(summarize().assistantTerminalStatus).toBe("completed");
  });
});
