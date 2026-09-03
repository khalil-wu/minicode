import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import {
  commandWithClientCommandId,
  coalescingKeyForClientCommand,
  conversationIdForSessionRestore,
  eventsFromSessionReplay,
  getLastReceivedServerSeqForTests,
  getPendingClientCommandAckIdsForTests,
  isQueueableWhenOffline,
  isTimeSensitiveCommand,
  isUndeliverableReplayEvent,
  rendererSessionId,
  markInboundEventFailed,
  acknowledgeClientCommand,
  assertInboundReplayCursorContinuity,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  shouldAdvanceReplayCursor,
  commitProcessedInboundEvent,
  shouldTrackClientCommandAck,
  shouldProcessInboundEvent,
  shouldReplayQueuedCommand,
  shouldDeferCommandUntilSessionRestore,
  trackPendingClientCommandAck,
  workspaceRootForConversationRestore,
  CLIENT_RESYNC_CLOSE_CODE,
  closeWebSocketForResync,
} from "./useWebSocket";
import { LS } from "../stores/shared-helpers";
import {
  createClientCommandId,
  registerWebSocketSender,
  resetPendingCommandResultsForTests,
  sendClientCommandAwaitResult,
} from "../protocol/ws-outbox";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

const storage = new Map<string, string>();

vi.stubGlobal("localStorage", {
  getItem: vi.fn((key: string) => storage.get(key) ?? null),
  setItem: vi.fn((key: string, value: string) => {
    storage.set(key, value);
  }),
  removeItem: vi.fn((key: string) => {
    storage.delete(key);
  }),
  clear: vi.fn(() => {
    storage.clear();
  }),
});

describe("useWebSocket queue policy", () => {
  const now = 1_000_000;

  it("uses a browser-valid private close code for client-side resync", () => {
    const close = vi.fn();
    closeWebSocketForResync({ close } as unknown as WebSocket, "event replay required");

    expect(CLIENT_RESYNC_CLOSE_CODE).toBeGreaterThanOrEqual(3000);
    expect(CLIENT_RESYNC_CLOSE_CODE).toBeLessThanOrEqual(4999);
    expect(close).toHaveBeenCalledWith(CLIENT_RESYNC_CLOSE_CODE, "event replay required");
  });

  it("queues short-lived prompt responses while the socket reconnects", () => {
    const commands: ClientCommand[] = [
      {
        type: "control_response",
        request_id: "tool-1",
        response: { subtype: "success", response: { action: "approve" } },
      },
      {
        type: "control_response",
        request_id: "ask-1",
        response: { subtype: "success", response: { answer: "yes" } },
      },
      {
        type: "control_cancel_request",
        request_id: "ctrl-1",
      },
      { type: "user_message", content: "continue" },
      { type: "commands.list" },
      { type: "skills.list" },
      { type: "conversation.switch", conversation_id: "conv-2" },
      { type: "interrupt", conversation_id: "conv-1", turn_id: "turn-1" },
    ];

    for (const command of commands) {
      expect(isQueueableWhenOffline(command)).toBe(true);
    }
  });

  it("does not queue incidental commands that can be retried by the UI", () => {
    expect(isQueueableWhenOffline({ type: "workspace.set", path: "C:/work" } as ClientCommand)).toBe(false);
    expect(isQueueableWhenOffline({ type: "terminal.input", session_id: "term-1", data: "x" })).toBe(false);
  });

  it("classifies interruption and prompt responses as time-sensitive commands", () => {
    expect(isTimeSensitiveCommand({ type: "interrupt" })).toBe(true);
    expect(isTimeSensitiveCommand({
      type: "control_response",
      request_id: "tool-1",
      response: { subtype: "success", response: { action: "approve" } },
    })).toBe(true);
    expect(isTimeSensitiveCommand({ type: "control_cancel_request", request_id: "tool-1" })).toBe(true);
    expect(isTimeSensitiveCommand({ type: "commands.list" })).toBe(false);
  });

  it("coalesces replaceable refresh commands without merging critical actions", () => {
    expect(coalescingKeyForClientCommand({ type: "commands.list" })).toBe("commands.list");
    expect(coalescingKeyForClientCommand({ type: "skills.list" })).toBe("skills.list");
    expect(coalescingKeyForClientCommand({ type: "runtime.capabilities.inspect", source: "settings" })).toBe("runtime.capabilities.inspect:settings");
    expect(coalescingKeyForClientCommand({ type: "terminal.resize", session_id: "term-1", cols: 80, rows: 24 })).toBe("terminal.resize:term-1");
    expect(coalescingKeyForClientCommand({ type: "approval", tool_call_id: "tool-1", action: "approve" })).toBe("");
    expect(coalescingKeyForClientCommand({ type: "user_message", content: "hello" })).toBe("");
  });

  it("drops stale prompt responses but never silently drops an explicit user message", () => {
    const approval = { type: "approval", tool_call_id: "tool-1", action: "approve" } as ClientCommand;
    const userMessage = { type: "user_message", content: "continue" } as ClientCommand;

    expect(shouldReplayQueuedCommand({ cmd: approval, queuedAt: now - 9_000 }, now)).toBe(true);
    expect(shouldReplayQueuedCommand({ cmd: approval, queuedAt: now - 11_000 }, now)).toBe(true);
    expect(shouldReplayQueuedCommand({ cmd: userMessage, queuedAt: now - 11_000 }, now)).toBe(true);
    expect(shouldReplayQueuedCommand({ cmd: userMessage, queuedAt: now - 61_000 }, now)).toBe(true);
  });
});

describe("useWebSocket session restore workspace", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("does not restore a workspace for blank global chat", () => {
    expect(workspaceRootForConversationRestore({
      conversationId: null,
      conversations: [],
    })).toBe("");
  });

  it("restores only the active conversation-bound workspace", () => {
    expect(workspaceRootForConversationRestore({
      conversationId: "conv-project",
      conversations: [
        { id: "conv-global" },
        { id: "conv-project", workspaceRoot: "C:/repo" },
      ],
    })).toBe("C:/repo");
  });

  it("prefers protected worktree paths over the base workspace", () => {
    expect(workspaceRootForConversationRestore({
      conversationId: "conv-worktree",
      conversations: [
        { id: "conv-worktree", workspaceRoot: "C:/repo", worktreePath: "C:/repo/.minicode/worktrees/conv-worktree" },
      ],
    })).toBe("C:/repo/.minicode/worktrees/conv-worktree");
  });

  it("restores the current active conversation id first", () => {
    localStorage.setItem(LS.conversation.activeId, "conv-persisted");

    expect(conversationIdForSessionRestore({
      conversationId: "conv-current",
      conversations: [{ id: "conv-current" }],
    })).toBe("conv-current");
  });

  it("falls back to the persisted active conversation id on cold start", () => {
    localStorage.setItem(LS.conversation.activeId, "conv-persisted");

    expect(conversationIdForSessionRestore({
      conversationId: null,
      conversations: [{ id: "conv-persisted" }],
    })).toBe("conv-persisted");
  });

  it("does not restore a persisted archived conversation id", () => {
    localStorage.setItem(LS.conversation.activeId, "conv-archived");

    expect(conversationIdForSessionRestore({
      conversationId: null,
      conversations: [{ id: "conv-archived", archived: true }],
    })).toBe("");
  });
});

describe("browser identity fallbacks", () => {
  it("does not read an absent global crypto object", () => {
    const originalCrypto = globalThis.crypto;
    vi.stubGlobal("crypto", undefined);
    try {
      expect(rendererSessionId({})).toMatch(/^session_[A-Za-z0-9_-]+$/);
      expect(createClientCommandId()).toMatch(/^cmd_[A-Za-z0-9_-]+$/);
    } finally {
      vi.stubGlobal("crypto", originalCrypto);
    }
  });
});

describe("useWebSocket renderer identity", () => {
  it("keeps one id across reloads in the same browser session", () => {
    const values = new Map<string, string>();
    const sessionStorage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    };
    const firstRenderer = { sessionStorage };
    const reloadedRenderer = { sessionStorage };

    const firstId = rendererSessionId(firstRenderer);

    expect(rendererSessionId(reloadedRenderer)).toBe(firstId);
    expect(firstId).toMatch(/^session_[A-Za-z0-9_-]+$/);
  });

  it("does not persist an unfenced interrupt that could cancel a later turn", () => {
    expect(isQueueableWhenOffline({ type: "interrupt", conversation_id: "conv-1" })).toBe(false);
    expect(isQueueableWhenOffline({
      type: "interrupt",
      conversation_id: "conv-1",
      message_id: "assistant-1",
    })).toBe(true);
  });

  it("defers every durable session action until restore completes", () => {
    expect(shouldDeferCommandUntilSessionRestore({
      type: "control_response",
      request_id: "tool-1",
      response: { subtype: "success", response: { action: "approve" } },
    })).toBe(true);
    expect(shouldDeferCommandUntilSessionRestore({
      type: "control_response",
      request_id: "ask-1",
      response: { subtype: "success", response: { answer: "yes" } },
    })).toBe(true);
    expect(shouldDeferCommandUntilSessionRestore({ type: "interrupt", conversation_id: "conv-1" })).toBe(true);
    expect(shouldDeferCommandUntilSessionRestore({ type: "conversation.switch", conversation_id: "conv-2" })).toBe(true);
    expect(shouldDeferCommandUntilSessionRestore({ type: "workspace.set", path: "C:/work" } as ClientCommand)).toBe(false);
  });
});

describe("useWebSocket client command ids", () => {
  beforeEach(() => {
    resetPendingClientCommandAcksForTests();
    resetPendingCommandResultsForTests();
    registerWebSocketSender(null);
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    resetPendingClientCommandAcksForTests();
    resetPendingCommandResultsForTests();
    registerWebSocketSender(null);
  });

  it("adds a stable client command id without mutating the caller command", () => {
    const command = { type: "conversation.list" } as ClientCommand;

    const withId = commandWithClientCommandId(command);
    const replay = commandWithClientCommandId(withId);

    expect(withId).not.toBe(command);
    expect(command).toEqual({ type: "conversation.list" });
    expect(withId.client_command_id).toMatch(/^cmd_/);
    expect(replay.client_command_id).toBe(withId.client_command_id);
  });

  it("tracks pending command ids until the backend acknowledges them", () => {
    const command = {
      type: "conversation.list",
      client_command_id: "cmd_pending",
    } as ClientCommand;

    trackPendingClientCommandAck(command);
    expect(getPendingClientCommandAckIdsForTests()).toEqual(["cmd_pending"]);

    expect(acknowledgeClientCommand({
      type: "client.command.ack",
      client_command_id: "cmd_pending",
      command_type: "conversation.list",
    } as ServerEvent)).toBe(true);
    expect(getPendingClientCommandAckIdsForTests()).toEqual([]);
  });

  it("rejects awaited command results when the durable ACK is rejected", async () => {
    let sentCommand: ClientCommand | null = null;
    registerWebSocketSender((command) => {
      sentCommand = command;
      return true;
    });
    const pending = sendClientCommandAwaitResult(
      { type: "permissions.content_rule.add", rule: "run_command(git status:*)", deny: false },
      "permissions.content_rule.add",
    );
    const clientCommandId = String(sentCommand?.client_command_id || "");

    expect(acknowledgeClientCommand({
      type: "client.command.ack",
      client_command_id: clientCommandId,
      command_type: "permissions.content_rule.add",
      accepted: false,
      reason: "command.persistence",
    } as ServerEvent)).toBe(true);

    await expect(pending).rejects.toThrow("command.persistence");
  });

  it("keeps a critical user command pending until the durable ACK arrives", () => {
    vi.useFakeTimers();
    const command = {
      type: "user_message",
      content: "hello",
      client_command_id: "cmd_missing_ack",
    } as ClientCommand;

    trackPendingClientCommandAck(command);
    vi.advanceTimersByTime(5000);

    expect(pushToast).not.toHaveBeenCalled();
    expect(getPendingClientCommandAckIdsForTests()).toEqual(["cmd_missing_ack"]);
  });

  it("does not warn for incidental background commands without acknowledgements", () => {
    vi.useFakeTimers();
    const command = {
      type: "commands.list",
      client_command_id: "cmd_catalog",
    } as ClientCommand;

    trackPendingClientCommandAck(command);
    vi.advanceTimersByTime(5000);

    expect(pushToast).not.toHaveBeenCalled();
    expect(getPendingClientCommandAckIdsForTests()).toEqual(["cmd_catalog"]);
  });

  it("does not track ping acknowledgements as pending work", () => {
    const command = {
      type: "ping",
      client_command_id: "cmd_ping",
    } as ClientCommand;

    expect(shouldTrackClientCommandAck(command)).toBe(false);
    trackPendingClientCommandAck(command);

    expect(getPendingClientCommandAckIdsForTests()).toEqual([]);
  });
});

describe("useWebSocket server sequence tracking", () => {
  beforeEach(() => {
    resetRecentInboundEventIdsForTests();
  });

  it("commits replay cursor and dedupe only after successful handling", () => {
    expect(getLastReceivedServerSeqForTests()).toBe(0);

    const pong = { type: "pong", event_id: "a", seq: 4 } as ServerEvent;
    expect(shouldProcessInboundEvent(pong)).toBe(true);
    expect(shouldProcessInboundEvent(pong)).toBe(true);
    commitProcessedInboundEvent(pong);
    expect(shouldProcessInboundEvent(pong)).toBe(false);
    expect(shouldProcessInboundEvent({ type: "runtime.capabilities", event_id: "b", seq: 9 } as ServerEvent)).toBe(true);
    const delta = { type: "agent_message.delta", conversation_id: "conv-1", event_id: "c", seq: 2 } as ServerEvent;
    expect(shouldProcessInboundEvent(delta)).toBe(true);
    commitProcessedInboundEvent(delta);

    expect(getLastReceivedServerSeqForTests()).toBe(2);
  });

  it("accepts live durable links across transient wire-sequence gaps", () => {
    const first = {
      type: "done",
      conversation_id: "conv-1",
      event_id: "durable-5",
      seq: 5,
      previous_replay_seq: 0,
    } as ServerEvent;
    const afterTransientTraffic = {
      type: "done",
      conversation_id: "conv-1",
      event_id: "durable-8",
      seq: 8,
      previous_replay_seq: 5,
    } as ServerEvent;

    assertInboundReplayCursorContinuity(first);
    commitProcessedInboundEvent(first);
    assertInboundReplayCursorContinuity(afterTransientTraffic);
    commitProcessedInboundEvent(afterTransientTraffic);

    expect(getLastReceivedServerSeqForTests()).toBe(8);
  });

  it.each(["artifact_content", "stream_event"])(
    "does not advance the durable cursor for transient %s events",
    (type) => {
      const first = {
        type: "done",
        conversation_id: "conv-1",
        event_id: "durable-5",
        seq: 5,
        previous_replay_seq: 0,
      } as ServerEvent;
      const transient = {
        type,
        conversation_id: "conv-1",
        event_id: `${type}-136`,
        seq: 136,
      } as ServerEvent;
      const next = {
        type: "command.result",
        conversation_id: "conv-1",
        event_id: "durable-137",
        seq: 137,
        previous_replay_seq: 5,
      } as ServerEvent;

      commitProcessedInboundEvent(first);
      commitProcessedInboundEvent(transient);
      expect(getLastReceivedServerSeqForTests()).toBe(5);
      expect(() => assertInboundReplayCursorContinuity(next)).not.toThrow();
    },
  );

  it("rejects a live durable event whose predecessor was not applied", () => {
    commitProcessedInboundEvent({
      type: "done",
      conversation_id: "conv-1",
      seq: 5,
      previous_replay_seq: 0,
    } as ServerEvent);

    expect(() => assertInboundReplayCursorContinuity({
      type: "done",
      conversation_id: "conv-1",
      seq: 8,
      previous_replay_seq: 7,
    } as ServerEvent)).toThrow("active replay chain");
    expect(getLastReceivedServerSeqForTests()).toBe(5);
  });

  it("commits a replay chain by durable predecessor instead of numeric adjacency", () => {
    commitProcessedInboundEvent({
      type: "done",
      conversation_id: "conv-1",
      seq: 5,
      previous_replay_seq: 0,
    } as ServerEvent);
    const replayed = eventsFromSessionReplay({
      type: "session.replay",
      last_seq: 5,
      current_seq: 11,
      replayed_events: 2,
      events: [
        {
          type: "done",
          conversation_id: "conv-1",
          message_id: "assistant-8",
          status: "completed",
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
            input_includes_cache_read: false,
          },
          seq: 8,
          previous_replay_seq: 5,
        },
        {
          type: "done",
          conversation_id: "conv-1",
          message_id: "assistant-11",
          status: "completed",
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
            input_includes_cache_read: false,
          },
          seq: 11,
          previous_replay_seq: 8,
        },
      ],
    } as ServerEvent);

    replayed.forEach(commitProcessedInboundEvent);

    expect(getLastReceivedServerSeqForTests()).toBe(11);
  });

  it("rejects replay envelopes that do not start from the active cursor", () => {
    commitProcessedInboundEvent({
      type: "done",
      conversation_id: "conv-1",
      seq: 5,
      previous_replay_seq: 0,
    } as ServerEvent);

    expect(() => eventsFromSessionReplay({
      type: "session.replay",
      last_seq: 4,
      current_seq: 8,
      replayed_events: 1,
      events: [{
        type: "done",
        conversation_id: "conv-1",
        message_id: "assistant-8",
        status: "completed",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
          input_includes_cache_read: false,
        },
        seq: 8,
        previous_replay_seq: 4,
      }],
    } as ServerEvent)).toThrow("active durable cursor");
  });

  it("rebases the cursor only from an authoritative no-replay snapshot", () => {
    commitProcessedInboundEvent({
      type: "session.restored",
      last_seq: 0,
      current_seq: 5,
      replayed_events: 0,
    } as ServerEvent);
    expect(getLastReceivedServerSeqForTests()).toBe(5);

    commitProcessedInboundEvent({
      type: "done",
      conversation_id: "conv-1",
      seq: 10,
      previous_replay_seq: 5,
    } as ServerEvent);
    commitProcessedInboundEvent({
      type: "session.restored",
      last_seq: 4,
      current_seq: 4,
      requested_last_seq: 10,
      replayed_events: 0,
      cursor_reset: true,
      snapshot_required: true,
    } as ServerEvent);

    expect(getLastReceivedServerSeqForTests()).toBe(4);
  });

  it("advances the replay cursor for newly-added conversation events without a manual allowlist update", () => {
    const event = {
      type: "agent.item",
      conversation_id: "conv-1",
      event_id: "agent-item-1",
      seq: 11,
    } as ServerEvent;
    expect(shouldProcessInboundEvent(event)).toBe(true);
    commitProcessedInboundEvent(event);

    expect(getLastReceivedServerSeqForTests()).toBe(11);
  });

  it("freezes ordered replay at a failed event until that event succeeds", () => {
    const failed = { type: "agent.item", conversation_id: "conv-1", event_id: "failed", seq: 5 } as ServerEvent;
    const later = { type: "done", conversation_id: "conv-1", event_id: "later", seq: 6 } as ServerEvent;

    markInboundEventFailed(failed);
    expect(shouldProcessInboundEvent(later)).toBe(false);
    expect(getLastReceivedServerSeqForTests()).toBe(0);

    expect(shouldProcessInboundEvent(failed)).toBe(true);
    commitProcessedInboundEvent(failed);
    expect(shouldProcessInboundEvent(later)).toBe(true);
    commitProcessedInboundEvent(later);
    expect(getLastReceivedServerSeqForTests()).toBe(6);
  });

  it("exposes replay cursor classification for restore contract tests", () => {
    expect(shouldAdvanceReplayCursor({ type: "pong", seq: 4 } as ServerEvent)).toBe(false);
    expect(shouldAdvanceReplayCursor({
      type: "session.replay",
      seq: 5,
      last_seq: 0,
      current_seq: 0,
      replayed_events: 0,
      events: [],
    } as ServerEvent)).toBe(false);
    expect(shouldAdvanceReplayCursor({ type: "agent_message.delta", seq: 6 } as ServerEvent)).toBe(false);
    expect(shouldAdvanceReplayCursor({
      type: "thinking_delta",
      conversation_id: "conv-1",
      seq: 7,
      source: "provider",
      provider_reasoning_type: "reasoning_content",
      visibility: "timeline",
    } as ServerEvent)).toBe(false);
    expect(shouldAdvanceReplayCursor({
      type: "thinking_delta",
      conversation_id: "conv-1",
      seq: 8,
      source: "provider",
      provider_reasoning_type: "reasoning_summary_text",
      visibility: "timeline",
    } as ServerEvent)).toBe(true);
    expect(shouldAdvanceReplayCursor({ type: "done", conversation_id: "conv-1", seq: 9 } as ServerEvent)).toBe(true);
    expect(shouldAdvanceReplayCursor({ type: "done", conversation_id: "conv-1" } as ServerEvent)).toBe(false);
  });

  it("carries undeliverable replay events through so the durable cursor advances", () => {
    // Throwing on one unrepresentable event stranded the whole envelope, so the
    // cursor stayed behind and the backend replayed it after every reconnect.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const replayed = eventsFromSessionReplay({
      type: "session.replay",
      last_seq: 0,
      current_seq: 4,
      replayed_events: 3,
      events: [
        { type: "agent_message.delta", conversation_id: "conv-1", item_id: "agent-message", delta: "hello", seq: 2, previous_replay_seq: 0, event_id: "event-2" },
        { seq: 3, previous_replay_seq: 2 },
        { type: "agent_message.delta", conversation_id: "conv-1", item_id: "agent-message", delta: " world", seq: 4, previous_replay_seq: 3, event_id: "event-4" },
      ],
    } as ServerEvent);

    expect(replayed.map((event) => event.seq)).toEqual([2, 3, 4]);
    expect(replayed.map(isUndeliverableReplayEvent)).toEqual([false, true, false]);
    warn.mockRestore();
  });

  it("rejects a replay envelope whose durable chain is discontinuous", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(() => eventsFromSessionReplay({
      type: "session.replay",
      last_seq: 0,
      current_seq: 4,
      replayed_events: 2,
      events: [
        { type: "agent_message.delta", conversation_id: "conv-1", item_id: "agent-message", delta: "hello", seq: 2, previous_replay_seq: 0, event_id: "event-2" },
        { type: "session.replay", events: [] },
      ],
    } as ServerEvent)).toThrow("discontinuous durable chain");
    warn.mockRestore();
  });
});

describe("useWebSocket inbound event dedupe", () => {
  it("allows events without event_id for backwards compatibility", () => {
    resetRecentInboundEventIdsForTests();

    expect(shouldProcessInboundEvent({ type: "pong" } as ServerEvent)).toBe(true);
    expect(shouldProcessInboundEvent({ type: "pong" } as ServerEvent)).toBe(true);
  });

  it("drops duplicate event_id values at the websocket ingress", () => {
    resetRecentInboundEventIdsForTests();
    const event = { type: "pong", event_id: "session:1" } as ServerEvent;

    expect(shouldProcessInboundEvent(event)).toBe(true);
    commitProcessedInboundEvent(event);
    expect(shouldProcessInboundEvent(event)).toBe(false);
    expect(shouldProcessInboundEvent({ type: "pong", event_id: "session:2" } as ServerEvent)).toBe(true);
  });

  it("retains a full backend replay window of event ids", () => {
    resetRecentInboundEventIdsForTests();
    const first = { type: "agent_message.delta", conversation_id: "conv-1", event_id: "event-0", seq: 1 } as ServerEvent;
    expect(shouldProcessInboundEvent(first)).toBe(true);
    commitProcessedInboundEvent(first);
    for (let index = 1; index < 1000; index += 1) {
      const event = {
        type: "agent_message.delta",
        conversation_id: "conv-1",
        event_id: `event-${index}`,
        seq: index + 1,
      } as ServerEvent;
      expect(shouldProcessInboundEvent(event)).toBe(true);
      commitProcessedInboundEvent(event);
    }

    expect(shouldProcessInboundEvent(first)).toBe(false);
  });
});
