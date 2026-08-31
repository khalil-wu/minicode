/* @vitest-environment jsdom */

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { useAppStore } from "../stores";
import type { ClientCommand } from "../protocol/events";
import {
  getWebSocket,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  useWebSocketConnection,
} from "./useWebSocket";

type SocketListener = (event: Event | MessageEvent) => void;

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];

  readonly url: string;
  readyState = MockWebSocket.CONNECTING;
  private listeners = new Map<string, Set<SocketListener>>();

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const callback: SocketListener = typeof listener === "function"
      ? listener as SocketListener
      : (event) => listener.handleEvent(event);
    const listeners = this.listeners.get(type) ?? new Set<SocketListener>();
    listeners.add(callback);
    this.listeners.set(type, listeners);
  }

  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
  });

  emit(type: "open" | "close", code = 1000) {
    this.readyState = type === "open" ? MockWebSocket.OPEN : MockWebSocket.CLOSED;
    const event = Object.assign(new Event(type), { code });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  emitMessage(data: unknown) {
    for (const listener of this.listeners.get("message") ?? []) {
      listener(new MessageEvent("message", { data: JSON.stringify(data) }));
    }
  }
}

const sentCommandTypes = (socket: MockWebSocket): string[] => socket.send.mock.calls
  .map(([payload]) => {
    try {
      return JSON.parse(String(payload))?.type as string | undefined;
    } catch {
      return undefined;
    }
  })
  .filter((type): type is string => Boolean(type));

const sentCommandCount = (socket: MockWebSocket, type: string): number =>
  sentCommandTypes(socket).filter((candidate) => candidate === type).length;

const sentCommands = (socket: MockWebSocket): ClientCommand[] => socket.send.mock.calls
  .map(([payload]) => {
    try {
      return JSON.parse(String(payload)) as ClientCommand;
    } catch {
      return null;
    }
  })
  .filter((command): command is ClientCommand => Boolean(command));

const flushQueuedCommands = async () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

const Harness = () => {
  useWebSocketConnection();
  return null;
};

describe("useWebSocketConnection socket ownership", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    localStorage.clear();
    resetPendingClientCommandAcksForTests();
    resetRecentInboundEventIdsForTests();
    useAppStore.setState({
      isConnected: false,
      conversationId: null,
      conversations: [],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      connectionPhase: "connecting",
      reconnectAttempt: 0,
      reconnectMaxAttempts: null,
      connectionError: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("ignores a close event from a socket owned by an old effect", () => {
    const firstRender = render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const firstSocket = MockWebSocket.instances[0];
    act(() => firstSocket.emit("open"));
    expect(useAppStore.getState().isConnected).toBe(true);

    firstRender.unmount();
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const currentSocket = MockWebSocket.instances[1];
    act(() => currentSocket.emit("open"));

    act(() => firstSocket.emit("close"));
    expect(useAppStore.getState().isConnected).toBe(true);

    act(() => currentSocket.emit("close"));
    expect(useAppStore.getState().isConnected).toBe(false);
  });

  it("closes a half-open socket when ping receives no inbound traffic", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    act(() => vi.advanceTimersByTime(69_999));
    expect(socket.close).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(socket.close).toHaveBeenCalledTimes(1);
  });

  it("keeps a healthy quiet stream alive when any inbound event answers the probe", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    act(() => vi.advanceTimersByTime(30_000));
    act(() => socket.emitMessage({ type: "pong" }));
    act(() => vi.advanceTimersByTime(30_000));
    act(() => socket.emitMessage({ type: "pong" }));
    act(() => vi.advanceTimersByTime(59_999));

    expect(socket.close).not.toHaveBeenCalled();
  });

  it("does not reconnect after a policy rejection", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    act(() => socket.emit("close", 1008));
    act(() => vi.advanceTimersByTime(600_001));

    expect(useAppStore.getState().isConnected).toBe(false);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("reconnects after an ordinary transport close", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    act(() => socket.emit("close", 1006));
    act(() => vi.advanceTimersByTime(1_500));

    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("keeps the reconnect attempt visible until session restore completes", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const firstSocket = MockWebSocket.instances[0];
    act(() => firstSocket.emit("open"));
    expect(useAppStore.getState().connectionPhase).toBe("connected");

    act(() => firstSocket.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(1);

    act(() => vi.advanceTimersByTime(1_500));
    const secondSocket = MockWebSocket.instances[1];
    act(() => secondSocket.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(2);

    act(() => vi.advanceTimersByTime(3_000));
    const thirdSocket = MockWebSocket.instances[2];
    act(() => thirdSocket.emit("open"));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(2);
    expect(useAppStore.getState().isConnected).toBe(false);
    expect(sentCommandCount(thirdSocket, "session.restore")).toBe(1);

    act(() => thirdSocket.emitMessage({
      type: "session.restored",
      active_conversation_id: null,
      conversation_switched_follows: false,
      replayed_events: 0,
      session: { active_conversation_id: null },
    }));
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(useAppStore.getState().reconnectAttempt).toBe(0);
    expect(useAppStore.getState().connectionError).toBeNull();
  });

  it("waits for restore, switch, and replay before declaring a reconnect successful", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const firstSocket = MockWebSocket.instances[0];
    act(() => firstSocket.emit("open"));
    expect(useAppStore.getState().connectionPhase).toBe("connected");

    act(() => firstSocket.emit("close", 1006));
    expect(useAppStore.getState().reconnectAttempt).toBe(1);
    act(() => vi.advanceTimersByTime(1_500));

    const reconnectSocket = MockWebSocket.instances[1];
    act(() => reconnectSocket.emit("open"));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(1);

    act(() => reconnectSocket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-recovered",
      conversation_switched_follows: true,
      last_seq: 0,
      current_seq: 1,
      replayed_events: 1,
      requested_last_seq: 0,
      session: { active_conversation_id: "conv-recovered" },
    }));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(1);

    act(() => reconnectSocket.emitMessage({
      type: "conversation.switched",
      conversation_id: "conv-recovered",
      conversation: {
        id: "conv-recovered",
        title: "Recovered",
        updated_at: "2026-08-30T00:00:00Z",
        messages: [],
      },
    }));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    expect(useAppStore.getState().reconnectAttempt).toBe(1);

    act(() => reconnectSocket.emitMessage({
      type: "session.replay",
      last_seq: 0,
      current_seq: 1,
      replayed_events: 1,
      events: [{
        type: "done",
        conversation_id: "conv-recovered",
        message_id: "assistant-recovered",
        status: "completed",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
          input_includes_cache_read: false,
        },
        seq: 1,
        previous_replay_seq: 0,
        event_id: "recovered-done-1",
      }],
    }));

    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(useAppStore.getState().reconnectAttempt).toBe(0);
    expect(useAppStore.getState().reconnectMaxAttempts).toBeNull();
    expect(useAppStore.getState().isConnected).toBe(true);
  });

  it("runs exactly five reconnect attempts before entering the failed phase", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    let socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    for (let attempt = 1; attempt <= 5; attempt += 1) {
      act(() => socket.emit("close", 1006));
      expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
      expect(useAppStore.getState().reconnectAttempt).toBe(attempt);
      expect(useAppStore.getState().reconnectMaxAttempts).toBe(5);

      act(() => vi.advanceTimersByTime(40_000));
      expect(MockWebSocket.instances).toHaveLength(attempt + 1);
      socket = MockWebSocket.instances[attempt];
      act(() => socket.emit("open"));
      expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
      expect(useAppStore.getState().reconnectAttempt).toBe(attempt);
    }

    act(() => socket.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    expect(useAppStore.getState().reconnectAttempt).toBe(5);
    expect(useAppStore.getState().reconnectMaxAttempts).toBe(5);
    expect(useAppStore.getState().connectionError).toContain("5/5");
    expect(MockWebSocket.instances).toHaveLength(6);

    act(() => vi.advanceTimersByTime(600_001));
    expect(MockWebSocket.instances).toHaveLength(6);
  });

  it("projects permanent close as a terminal connection failure", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));

    act(() => socket.emit("close", 1008));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    expect(useAppStore.getState().connectionError).toContain("服务拒绝");
    act(() => vi.advanceTimersByTime(600_001));
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("refreshes the command catalog after session.synced applies the canonical owner", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const before = sentCommandCount(socket, "commands.list");

    act(() => socket.emitMessage({
      type: "session.synced",
      active_conversation_id: "conv-synced",
      session: { active_conversation_id: "conv-synced" },
    }));
    await flushQueuedCommands();

    expect(useAppStore.getState().conversationId).toBe("conv-synced");
    expect(sentCommandCount(socket, "commands.list")).toBe(before + 1);
  });

  it("refreshes once after session.restored when no conversation switch follows", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const before = sentCommandCount(socket, "commands.list");

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-restored",
      conversation_switched_follows: false,
      session: { active_conversation_id: "conv-restored" },
    }));
    await flushQueuedCommands();

    expect(useAppStore.getState().conversationId).toBe("conv-restored");
    expect(sentCommandCount(socket, "commands.list")).toBe(before + 1);
  });

  it("does not replay a pending restore after the semantic restore already completed", async () => {
    useAppStore.setState({ conversationId: "conv-pending-restore" });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();

    expect(sentCommandCount(socket, "session.restore")).toBe(1);
    expect(useAppStore.getState().connectionPhase).toBe("connecting");
    expect(useAppStore.getState().isConnected).toBe(false);

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-pending-restore",
      conversation_switched_follows: false,
      session: { active_conversation_id: "conv-pending-restore" },
    }));
    await flushQueuedCommands();

    expect(useAppStore.getState().conversationId).toBe("conv-pending-restore");
    expect(sentCommandCount(socket, "session.restore")).toBe(1);
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(useAppStore.getState().isConnected).toBe(true);
  });

  it("waits for the canonical conversation switch before refreshing a restored catalog", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const before = sentCommandCount(socket, "commands.list");

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-followup",
      conversation_switched_follows: true,
      session: { active_conversation_id: "conv-followup" },
    }));
    await flushQueuedCommands();
    expect(sentCommandCount(socket, "commands.list")).toBe(before);

    act(() => socket.emitMessage({
      type: "conversation.switched",
      conversation_id: "conv-followup",
      conversation: {
        id: "conv-followup",
        title: "Follow-up",
        updated_at: "2026-08-15T00:00:00Z",
        workspace_root: "C:/repo",
        messages: [],
      },
    }));
    await flushQueuedCommands();

    expect(useAppStore.getState().conversationId).toBe("conv-followup");
    expect(sentCommandCount(socket, "commands.list")).toBe(before + 1);
  });

  it("serializes inventory refresh and manual switching behind session restore", async () => {
    useAppStore.setState({
      conversationId: "conv-before-reconnect",
      conversations: [{
        id: "conv-before-reconnect",
        title: "Before reconnect",
        updatedAt: "2026-08-15T00:00:00Z",
      }],
    });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();

    expect(sentCommandCount(socket, "session.restore")).toBe(1);
    expect(sentCommandCount(socket, "conversation.list")).toBe(0);

    act(() => {
      expect(getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-user-selected",
        client_command_id: "cmd_switch_deferred",
      })).toBe(true);
    });
    await flushQueuedCommands();
    expect(sentCommandCount(socket, "conversation.switch")).toBe(0);

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-before-reconnect",
      conversation_switched_follows: false,
      session: { active_conversation_id: "conv-before-reconnect" },
    }));
    await flushQueuedCommands();

    expect(sentCommandCount(socket, "conversation.switch")).toBe(1);
    expect(sentCommandCount(socket, "conversation.list")).toBe(1);
    expect(sentCommands(socket)).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: "conversation.switch",
        conversation_id: "conv-user-selected",
        client_command_id: "cmd_switch_deferred",
      }),
    ]));
  });

  it("buffers live durable events until the advertised replay chain completes", async () => {
    useAppStore.setState({
      conversationId: "conv-recovering",
      conversations: [{
        id: "conv-recovering",
        title: "Recovering",
        updatedAt: "2026-08-16T00:00:00Z",
      }],
    });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();

    const observedTypes: string[] = [];
    const unsubscribe = getWebSocket()?.subscribe((event) => {
      observedTypes.push(String((event as { type?: unknown }).type || ""));
    });
    act(() => {
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-after-recovery",
        client_command_id: "cmd_after_recovery",
      });
    });
    await flushQueuedCommands();
    expect(sentCommandCount(socket, "conversation.switch")).toBe(0);

    const durableDone = {
      type: "done",
      conversation_id: "conv-recovering",
      message_id: "assistant-recovering",
      status: "completed",
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
        input_includes_cache_read: false,
      },
      seq: 5,
      previous_replay_seq: 0,
      event_id: "durable-5",
    };
    act(() => socket.emitMessage(durableDone));
    expect(observedTypes).not.toContain("done");

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-recovering",
      conversation_switched_follows: false,
      last_seq: 0,
      current_seq: 5,
      replayed_events: 1,
      requested_last_seq: 0,
      session: { active_conversation_id: "conv-recovering" },
    }));
    await flushQueuedCommands();
    expect(sentCommandCount(socket, "conversation.switch")).toBe(0);

    act(() => socket.emitMessage({
      type: "session.replay",
      last_seq: 0,
      current_seq: 5,
      replayed_events: 1,
      events: [durableDone],
    }));
    await flushQueuedCommands();

    expect(observedTypes.filter((type) => type === "done")).toHaveLength(1);
    expect(observedTypes).toContain("session.replay");
    expect(sentCommandCount(socket, "conversation.switch")).toBe(1);
    expect(socket.close).not.toHaveBeenCalledWith(1012, expect.any(String));
    unsubscribe?.();
  });

  it("rejects an older projection from both the same command type and a competing restore", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();

    act(() => {
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-old",
        client_command_id: "cmd_switch_old",
      });
    });
    await flushQueuedCommands();
    act(() => {
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-new",
        client_command_id: "cmd_switch_new",
      });
    });
    await flushQueuedCommands();

    act(() => socket.emitMessage({
      type: "conversation.switched",
      client_command_id: "cmd_switch_old",
      client_command_type: "conversation.switch",
      conversation_id: "conv-old",
      conversation: {
        id: "conv-old",
        title: "Old",
        updated_at: "2026-08-15T00:00:00Z",
        messages: [],
      },
    }));
    expect(useAppStore.getState().conversationId).toBeNull();

    act(() => socket.emitMessage({
      type: "conversation.switched",
      client_command_id: "cmd_switch_new",
      client_command_type: "conversation.switch",
      conversation_id: "conv-new",
      conversation: {
        id: "conv-new",
        title: "New",
        updated_at: "2026-08-16T00:00:00Z",
        messages: [],
      },
    }));
    expect(useAppStore.getState().conversationId).toBe("conv-new");

    act(() => {
      getWebSocket()?.send({
        type: "session.restore",
        client_command_id: "cmd_restore_old",
      });
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-latest",
        client_command_id: "cmd_switch_latest",
      });
    });
    await flushQueuedCommands();

    act(() => socket.emitMessage({
      type: "session.restored",
      client_command_id: "cmd_restore_old",
      client_command_type: "session.restore",
      active_conversation_id: "conv-old",
      conversation: {
        id: "conv-old",
        title: "Restored old",
        updated_at: "2026-08-15T00:00:00Z",
      },
      session: { active_conversation_id: "conv-old" },
    }));
    expect(useAppStore.getState().conversationId).toBe("conv-new");

    act(() => socket.emitMessage({
      type: "conversation.switched",
      client_command_id: "cmd_switch_latest",
      client_command_type: "conversation.switch",
      conversation_id: "conv-latest",
      conversation: {
        id: "conv-latest",
        title: "Latest",
        updated_at: "2026-08-16T01:00:00Z",
        messages: [],
      },
    }));
    expect(useAppStore.getState().conversationId).toBe("conv-latest");
  });

  it("drops stale restore replay and resume projections but keeps unrelated correlated events", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const observedTypes: string[] = [];
    const unsubscribe = getWebSocket()?.subscribe((event) => {
      observedTypes.push(String((event as { type?: unknown }).type || ""));
    });

    act(() => {
      getWebSocket()?.send({ type: "session.restore", client_command_id: "cmd_restore_first" });
      getWebSocket()?.send({ type: "session.restore", client_command_id: "cmd_restore_latest" });
    });

    act(() => {
      socket.emitMessage({
        type: "session.replay",
        client_command_id: "cmd_restore_first",
        client_command_type: "session.restore",
        last_seq: 0,
        current_seq: 0,
        replayed_events: 0,
        events: [],
      });
      socket.emitMessage({
        type: "stream_resume",
        client_command_id: "cmd_restore_first",
        client_command_type: "session.restore",
        conversation_id: "conv-resume",
        message_id: "assistant-resume",
        tool_calls_pending: [],
      });
      socket.emitMessage({
        type: "pong",
        client_command_id: "cmd_restore_first",
        client_command_type: "session.restore",
      });
    });
    expect(observedTypes).toEqual(["pong"]);

    act(() => {
      socket.emitMessage({
        type: "session.replay",
        client_command_id: "cmd_restore_latest",
        client_command_type: "session.restore",
        last_seq: 0,
        current_seq: 0,
        replayed_events: 0,
        events: [],
      });
      socket.emitMessage({
        type: "stream_resume",
        client_command_id: "cmd_restore_latest",
        client_command_type: "session.restore",
        conversation_id: "conv-resume",
        message_id: "assistant-resume",
        content_blocks: [{
          type: "text",
          itemId: "agent-message",
          content: "latest recovery",
          status: "partial",
          isStreaming: false,
        }],
        tool_calls_pending: [],
      });
    });
    expect(observedTypes).toEqual(["pong", "session.replay", "stream_resume"]);
    unsubscribe?.();
  });

  it("does not replay an older unacknowledged switch after a newer switch was accepted", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const firstSocket = MockWebSocket.instances[0];
    act(() => firstSocket.emit("open"));
    await flushQueuedCommands();

    act(() => {
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-old",
        client_command_id: "cmd_switch_unacked_old",
      });
    });
    await flushQueuedCommands();
    act(() => {
      getWebSocket()?.send({
        type: "conversation.switch",
        conversation_id: "conv-new",
        client_command_id: "cmd_switch_acked_new",
      });
    });
    await flushQueuedCommands();
    act(() => firstSocket.emitMessage({
      type: "client.command.ack",
      client_command_id: "cmd_switch_acked_new",
      command_type: "conversation.switch",
      accepted: true,
    }));

    act(() => firstSocket.emit("close", 1006));
    act(() => vi.advanceTimersByTime(2_000));
    const reconnectSocket = MockWebSocket.instances[1];
    act(() => reconnectSocket.emit("open"));
    await flushQueuedCommands();
    expect(sentCommandCount(reconnectSocket, "session.restore")).toBe(1);

    act(() => reconnectSocket.emitMessage({
      type: "session.restored",
      active_conversation_id: null,
      conversation_switched_follows: false,
      session: { active_conversation_id: null },
    }));
    await flushQueuedCommands();

    expect(sentCommands(reconnectSocket).filter((command) => (
      command.type === "conversation.switch"
    ))).toEqual([]);
  });
});
