/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../stores/types";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { useAppStore } from "../stores";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import { handleChatStreamEvent } from "./chatStreamEvents";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true), rejectClientCommandResult: vi.fn(() => false),
}));

const owner = "recovery-a";
const event = (value: object) => value as ServerEvent;
const checkpoint = (): ChatMessage => ({
  id: "old-checkpoint", role: "assistant", content: "", artifacts: [], timestamp: 2,
  terminalStatus: "partial", terminationReason: "run_in_progress", durationMs: 22520,
  blocks: [{ type: "tool_call", record: {
    id: "read-old", name: "read_file", args: { file_path: "before.ts" },
    status: "success", turnId: "old-turn", summary: "original file evidence",
  } }],
});
const user = (id: string): ChatMessage => ({ id, role: "user", content: id, artifacts: [], timestamp: 3 });
const messages = () => {
  const state = useAppStore.getState();
  return state.conversationId === owner ? state.messages : state.conversationMessages[owner];
};
const buffer: StreamBuffer = {
  push: (text, conversationId, itemId, _metadata, messageId) =>
    useAppStore.getState().appendAgentMessageDelta(itemId || "answer", text, conversationId, messageId),
  flush: vi.fn(), destroy: vi.fn(),
};
const handlers = { textStreamBuffer: buffer, thinkingStreamBuffer: buffer };
const chat = (value: object) => handleChatStreamEvent(event({ ...value, conversation_id: owner }), owner, handlers);
const started = (id: string) => event({
  type: "agent.run.started", role: "main", conversation_id: owner,
  message_id: id, turn_id: id + "-turn", run_id: id + "-run", task_id: id + "-task", status: "running",
});
const finishCurrent = (id: string) => {
  const scope = { message_id: id, turn_id: id + "-turn", task_id: id + "-task" };
  chat({ type: "tool_call", ...scope, id: "read-" + id, name: "read_file", args: { file_path: "note.ts" }, status: "running" });
  chat({ type: "tool_result", ...scope, id: "read-" + id, status: "success", summary: "actual current file" });
  chat({ type: "item.started", ...scope, item: { id: id + "-answer", type: "agent_message", text: "", status: "in_progress" } });
  chat({ type: "agent_message.delta", ...scope, item_id: id + "-answer", delta: "Current final" });
  chat({ type: "item.completed", ...scope, item: { id: id + "-answer", type: "agent_message", text: "Current final", source: "model_final", status: "completed" } });
  handleRuntimeEvent(event({ type: "agent.run.completed", role: "main", conversation_id: owner,
    ...scope, run_id: id + "-run", status: "completed" }), owner);
  chat({ type: "done", ...scope, status: "completed", usage: {
    input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0, input_includes_cache_read: true,
  } });
  const current = messages().find(message => message.id === id)!;
  expect(current).toMatchObject({ id, turnId: id + "-turn", isStreaming: false, terminalStatus: "completed", content: "Current final" });
  expect(getToolCallsFromMessage(current)).toEqual([expect.objectContaining({ id: "read-" + id, status: "success", summary: "actual current file" })]);
};

describe("real current turn recovery after a previous in-flight checkpoint", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({ conversationId: owner, messages: [], conversationMessages: {}, conversationStreaming: {},
      conversations: [{ id: owner, title: "A", updatedAt: "", workspaceRoot: "C:/a" }],
      conversationAgentStates: {}, conversationHydration: {}, conversationHistoryPages: {},
      sideChats: {}, runtimeSession: null, pendingProviderProgress: {}, pendingConversationSwitchId: null,
      pendingConversationCreateId: null, isStreaming: false, inspectorEntries: [],
      agentProgress: [], todos: [], plan: null, subagents: [], turnDiffs: {},
      draft: "original unsent input", workingDirectory: "C:/a", permissionMode: "confirm",
      pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [],
      pendingDiffReview: null, diffReviewQueue: [], usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 },
    });
  });

  it.each([false, true])("admits a live main run over the old checkpoint without changing its reader (%s)", (background) => {
    const old = { ...checkpoint(), isStreaming: true, isThinkingStreaming: true };
    const history = [user("old-user"), old];
    const reader = [user("reader-b")];
    useAppStore.setState({ conversationId: background ? "reader-b" : owner,
      messages: background ? reader : history, conversationMessages: { [owner]: history },
      isStreaming: !background, conversationStreaming: { [owner]: true } });
    const id = background ? "background-current" : "foreground-current";

    handleRuntimeEvent(started(id), owner);
    expect(messages().find(message => message.id === id)).toMatchObject({ isStreaming: true, turnId: id + "-turn" });
    expect(messages().find(message => message.id === old.id)).toEqual({ ...old, isStreaming: false, isThinkingStreaming: false });
    finishCurrent(id);
    expect(useAppStore.getState().draft).toBe("original unsent input");
    expect(useAppStore.getState().permissionMode).toBe("confirm");
    expect(useAppStore.getState().workingDirectory).toBe("C:/a");
    if (background) {
      expect(useAppStore.getState().conversationId).toBe("reader-b");
      expect(useAppStore.getState().messages).toBe(reader);
      expect(useAppStore.getState().isStreaming).toBe(false);
    }
  });

  it("hydrates the exact new user, then resumes its assistant without reviving the older partial", () => {
    const old = checkpoint();
    handleSessionEvent(event({ type: "session.synced", session_id: "follower-session", synced: true,
      active_conversation_id: owner, session: { session_id: "follower-session", active_conversation_id: owner,
        active_task_id: "resumed-current-task", active_stream_conversation_ids: [owner] },
      active_conversation: { id: owner, title: "A", updated_at: "", revision: 20,
        transcript: [{ id: "old-user", role: "user", content: "old request", timestamp: 1 },
          { id: old.id, role: "assistant", content: "", timestamp: 2, terminal_status: "partial",
            termination_reason: "run_in_progress", duration_ms: 22520, blocks: old.blocks },
          { id: "new-user", role: "user", content: "new real request", timestamp: 3 }] },
    }), handlers);
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(messages().find(message => message.id === old.id)).toMatchObject({ terminalStatus: "partial",
      terminationReason: "run_in_progress", durationMs: 22520 });
    expect(messages().find(message => message.id === old.id)?.isStreaming).not.toBe(true);

    chat({ type: "stream_resume", message_id: "resumed-current", turn_id: "resumed-current-turn",
      event_seq: 7, stream_status: "running", last_event_type: "runtime.span", content_blocks: [], tool_states: [], tool_calls_pending: [] });
    expect(messages().find(message => message.id === "resumed-current")).toMatchObject({ isStreaming: true, turnId: "resumed-current-turn" });
    finishCurrent("resumed-current");
    expect(messages().find(message => message.id === old.id)).toMatchObject({ terminalStatus: "partial", terminationReason: "run_in_progress", durationMs: 22520 });
    expect(getToolCallsFromMessage(messages().find(message => message.id === old.id)!)).toEqual([
      expect.objectContaining({ id: "read-old", status: "success", summary: "original file evidence" }),
    ]);
  });

  it("preserves the old checkpoint when the new task runtime arrives before its run-start", () => {
    const old = checkpoint();
    useAppStore.setState({ messages: [user("old-user"), old], runtimeSession: {
      active_conversation_id: owner, active_task_id: null, active_stream_conversation_ids: [],
    } });
    // This is the observed primary wire order: task.update seq56, then the
    // new agent.run.started seq58. The new raw user is not a local placeholder.
    handleRuntimeEvent(event({ type: "task.update", session: { active_conversation_id: owner,
      active_task_id: "runtime-first-current-task", active_stream_conversation_ids: [owner] } }), owner);
    expect(messages().find(message => message.id === old.id)).toEqual(old);
    expect(useAppStore.getState().isStreaming).toBe(true);
    handleRuntimeEvent(started("runtime-first-current"), owner);
    expect(messages().find(message => message.id === old.id)).toEqual(old);
    finishCurrent("runtime-first-current");
    expect(messages().find(message => message.id === old.id)).toEqual(old);
  });

  it("keeps an appended same-turn steer and its attachment through sync, resume and resampling", () => {
    const id = "steered-current";
    useAppStore.setState({ messages: [user("turn-user")] });
    handleRuntimeEvent(started(id), owner);
    chat({ type: "item.started", message_id: id, turn_id: id + "-turn",
      item: { id: "first-sample", type: "agent_message", text: "", status: "in_progress" } });
    chat({ type: "item.completed", message_id: id, turn_id: id + "-turn",
      item: { id: "first-sample", type: "agent_message", text: "withdrawn sample", source: "cancelled", status: "cancelled" } });
    handleSessionEvent(event({ type: "session.synced", session_id: "steer-follower", synced: true,
      active_conversation_id: owner, session: { session_id: "steer-follower", active_conversation_id: owner,
        active_task_id: id + "-task", active_stream_conversation_ids: [owner] },
      active_conversation: { id: owner, title: "A", updated_at: "", revision: 30, transcript: [
        { id: "turn-user", role: "user", content: "original request", timestamp: 1 },
        { id, role: "assistant", content: "", timestamp: 2, terminal_status: "partial",
          termination_reason: "run_in_progress", duration_ms: 7648, blocks: [
            { type: "text", itemId: "first-sample", content: "withdrawn sample", source: "cancelled", status: "cancelled", isStreaming: false },
          ] },
        { id: "steer-user", role: "user", content: "use this real input", display_content: "use this real input",
          timestamp: 3, steered: true, steer_target_message_id: id, attachments: [
            { id: "fixture-attachment", kind: "document", file_name: "steer.txt", media_type: "text/plain",
              artifact_id: "fixture-upload", doc_id: "fixture-doc", size_bytes: 20 },
          ] },
      ] },
    }), handlers);
    chat({ type: "stream_resume", message_id: id, turn_id: id + "-turn", event_seq: 47,
      stream_status: "running", content_blocks: messages().find(message => message.id === id)!.blocks,
      tool_states: [], tool_calls_pending: [] });
    expect(messages().find(message => message.id === id)).toMatchObject({ isStreaming: true, turnId: id + "-turn" });
    finishCurrent(id);
    expect(messages().filter(message => message.id === id)).toHaveLength(1);
    expect(messages().find(message => message.id === "steer-user")).toMatchObject({ content: "use this real input",
      steeredIntoMessageId: id, attachmentRefs: [expect.objectContaining({ artifactId: "fixture-upload", name: "steer.txt" })] });
    expect(useAppStore.getState().draft).toBe("original unsent input");
  });

  it.each([false, true])("rejects an older snapshot and late done while the newer assistant is live (%s)", (missingOldTarget) => {
    const id = missingOldTarget ? "newer-paged" : "newer-with-checkpoint";
    const old = checkpoint();
    useAppStore.setState({ messages: [...(missingOldTarget ? [] : [user("old-user"), old]), user("new-user"),
      { id, role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 4, isStreaming: true, turnId: id + "-turn" }],
      isStreaming: true, conversationStreaming: { [owner]: true } });
    chat({ type: "stream_resume", message_id: old.id, turn_id: "old-turn", event_seq: 90,
      stream_status: "running", content_blocks: [{ type: "text", content: "late old content" }], tool_calls_pending: [] });
    expect(useAppStore.getState().inspectorEntries.at(-1)?.payload).toMatchObject({ ignored: true,
      reason: missingOldTarget ? "newer_turn_is_streaming" : "newer_user_turn_exists" });
    chat({ type: "done", message_id: old.id, turn_id: "old-turn", status: "completed", usage: {} });
    expect(messages().find(message => message.id === id)).toMatchObject({ isStreaming: true, turnId: id + "-turn" });
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(messages().some(message => message.content === "late old content")).toBe(false);
    finishCurrent(id);
    if (!missingOldTarget) {
      chat({ type: "stream_resume", message_id: old.id, turn_id: "old-turn", event_seq: 91,
        stream_status: "running", content_blocks: [], tool_calls_pending: [] });
      expect(messages().find(message => message.id === id)).toMatchObject({ isStreaming: false, terminalStatus: "completed" });
      expect(useAppStore.getState().isStreaming).toBe(false);
      expect(useAppStore.getState().inspectorEntries.at(-1)?.payload).toMatchObject({ ignored: true, reason: "newer_user_turn_exists" });
    }
  });

  it.each(["queued", "cancelled"] as const)("keeps the active assistant when later user input is %s", (queueState) => {
    useAppStore.setState({ messages: [user("current-user"),
      { id: "current-assistant", role: "assistant", content: "", artifacts: [], timestamp: 4, isStreaming: true },
      { ...user("queued-user"), queueState },
      { id: "queued-assistant", role: "assistant", content: "", artifacts: [], timestamp: 5, queueState }],
      isStreaming: true, conversationStreaming: { [owner]: true } });
    handleRuntimeEvent(event({ type: "task.update", session: { active_stream_conversation_ids: [owner] } }), owner);
    expect(messages().find(message => message.id === "current-assistant")?.isStreaming).toBe(true);
    expect(messages().find(message => message.id === "queued-assistant")?.isStreaming).not.toBe(true);
  });
});
