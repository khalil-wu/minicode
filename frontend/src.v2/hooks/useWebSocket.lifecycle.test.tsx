/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn(), dismissToast: vi.fn() }));

import { useAppStore } from "../stores";
import { resetEditorDraftStorageForTests } from "../stores/editor-drafts";
import { pushToast } from "../overlays/ToastContainer";
import type { ClientCommand } from "../protocol/events";
import { sendClientCommandAwaitResult, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import {
  getWebSocket,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  commitProcessedInboundEvent,
  getLastReceivedServerSeqForTests,
  skipUndeliverableInboundEvent,
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
    // Own the transport clock; IndexedDB's setImmediate tasks must complete.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
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

  afterEach(async () => {
    cleanup();
    await resetEditorDraftStorageForTests();
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

  it("sends every explicitly identified request instead of coalescing away its result", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    getWebSocket()!.send({ type: "skills.list", client_command_id: "first-awaiting-request" });
    getWebSocket()!.send({ type: "skills.list", client_command_id: "second-awaiting-request" });
    await flushQueuedCommands();
    const ids = sentCommands(socket).map((command) => command.client_command_id);
    expect(ids).toContain("first-awaiting-request");
    expect(ids).toContain("second-awaiting-request");
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

  it.each([
    ["connection.llm_initialization_failed", 1008, null],
    ["connection.session_initialization_failed", 1011, null],
    ["connection.session_initialization_failed", 1011, "restore-conversation"],
  ])("keeps the initialization error for %s instead of retrying", async (errorCode, closeCode, conversationId) => {
    useAppStore.setState({ conversationId });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    vi.mocked(pushToast).mockClear();
    const message = "会话日志无法读取，请修复后重试。";

    act(() => socket.emitMessage({
      type: "error",
      message,
      recoverable: false,
      error_type: "api",
      error_code: errorCode,
    }));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    expect(useAppStore.getState().connectionError).toBe(message);
    expect(useAppStore.getState().isConnected).toBe(false);
    expect(socket.close).toHaveBeenCalledOnce();
    expect(pushToast).toHaveBeenCalledExactlyOnceWith(message, "error", 0);
    const sendCount = socket.send.mock.calls.length;

    act(() => socket.emit("close", closeCode));
    act(() => vi.advanceTimersByTime(600_001));
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(useAppStore.getState().connectionError).toBe(message);
    expect(socket.send).toHaveBeenCalledTimes(sendCount);
    expect(pushToast).toHaveBeenCalledOnce();
  });

  it.each([
    { recoverable: true },
    { conversation_id: "conversation" },
    { replayed: true },
    { error_code: "command.failed" },
  ])("does not turn unrelated errors into terminal connection failures: %j", (overrides) => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    act(() => socket.emitMessage({
      type: "error",
      message: "请求失败",
      recoverable: false,
      error_type: "api",
      error_code: "connection.session_initialization_failed",
      ...overrides,
    }));
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(socket.close).not.toHaveBeenCalled();
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

  it.each<ClientCommand>([
    { type: "subagent.cancel", subagent_id: "child-recovered", conversation_id: "conv-recovered", workspace_root: "C:/repo" },
    { type: "preview.launch.start", name: "dev", conversation_id: "conv-recovered", workspace_root: "C:/repo" },
    { type: "diff.git_stage_all", conversation_id: "conv-recovered", workspace_root: "C:/repo" },
  ])("refuses $type during restore without replaying it when the owner becomes ready", async (command) => {
    useAppStore.setState({ conversationId: "conv-recovered", workingDirectory: "C:/repo" });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    expect(useAppStore.getState().isConnected).toBe(false);

    await act(async () => {
      await expect(sendClientCommandAwaitResult(command, command.type, { silent: true }))
        .rejects.toThrow("会话正在恢复，请恢复完成后重试");
    });
    expect(sentCommandCount(socket, command.type)).toBe(0);

    act(() => socket.emitMessage({
      type: "session.restored",
      active_conversation_id: "conv-recovered",
      conversation_switched_follows: false,
      replayed_events: 0,
      session: { active_conversation_id: "conv-recovered", workspace_root: "C:/repo" },
    }));
    await flushQueuedCommands();
    expect(useAppStore.getState().isConnected).toBe(true);
    expect(sentCommandCount(socket, command.type)).toBe(0);

    act(() => { expect(getWebSocket()?.send(command)).toBe(true); });
    await flushQueuedCommands();
    expect(sentCommandCount(socket, command.type)).toBe(1);
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
    expect(getWebSocket()?.send({ type: "user_message", content: "not deliverable" })).toBe(false);
    expect(getWebSocket()?.send({ type: "control_cancel_request", request_id: "unresolved" })).toBe(false);
    useAppStore.getState().requestConversationSwitch("unreachable");
    expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
  });

  it("does not treat a positive admission ACK as control completion", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    const result = sendPromptResponseCommand({ type: "control_response", request_id: "approval-awaiting",
      client_command_id: "control-awaiting", conversation_id: "conv-inline", response: { subtype: "success", response: { action: "approve" } } });
    const observed = vi.fn();
    void result.then(observed);
    act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: "control-awaiting", command_type: "control_response", accepted: true }));
    await flushQueuedCommands();
    expect(observed).not.toHaveBeenCalled();
    act(() => socket.emitMessage({ type: "command.result", client_command_id: "control-awaiting", command: "control_response", level: "success", message: "" }));
    await expect(result).resolves.toMatchObject({ command: "control_response", level: "success" });
  });

  it("keeps the actual approval card retryable after a negative ACK, clearing only on semantic acceptance", async () => {
    const ApprovalHarness = () => { useWebSocketConnection(); return <InlineAgentPrompt />; };
    render(<ApprovalHarness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    act(() => {
      useAppStore.setState({ conversationId: "conv-inline", pendingApproval: null, approvalQueue: [],
        pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [] });
      useAppStore.getState().setApproval({ requestId: "real-unconfirmed", conversationId: "conv-inline", toolName: "write_file", args: {} });
    });
    fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
    const first = sentCommands(socket).find((command) => command.type === "control_response")!;
    act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: first.client_command_id,
      command_type: "control_response", accepted: false, reason: "command.persistence" }));
    await flushQueuedCommands();
    expect(useAppStore.getState().pendingApproval).toMatchObject({ requestId: "real-unconfirmed", status: "error", error: "command.persistence" });
    expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(false);
    expect(sentCommandCount(socket, "control_response")).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
    const retry = sentCommands(socket).filter((command) => command.type === "control_response").at(-1)!;
    expect(retry.client_command_id).not.toBe(first.client_command_id);
    act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: retry.client_command_id,
      command_type: "control_response", accepted: true }));
    await flushQueuedCommands();
    expect(useAppStore.getState().pendingApproval?.requestId).toBe("real-unconfirmed");
    act(() => socket.emitMessage({ type: "command.result", command: "control_response", level: "success", message: "",
      client_command_id: retry.client_command_id, data: { client_command_id: retry.client_command_id } }));
    await flushQueuedCommands();
    expect(useAppStore.getState().pendingApproval).toBeNull();
  });

  it.each([false, true])("withdraws an unconfirmed control decision on disconnect (ACK received: %s)", async (admitted) => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    const result = sendPromptResponseCommand({ type: "control_cancel_request", request_id: "approval-unconfirmed", client_command_id: "withdraw-control" });
    const failure = expect(result).rejects.toThrow("尚未确认");
    if (admitted) act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: "withdraw-control", command_type: "control_cancel_request", accepted: true }));
    act(() => socket.emit("close", 1006));
    await failure;
    act(() => vi.advanceTimersByTime(2_000));
    const replacement = MockWebSocket.instances[1];
    act(() => replacement.emit("open"));
    act(() => replacement.emitMessage({ type: "session.restored", active_conversation_id: null, conversation_switched_follows: false, session: { active_conversation_id: null } }));
    await flushQueuedCommands();
    expect(sentCommands(replacement).filter((command) => command.client_command_id === "withdraw-control")).toEqual([]);
  });

  it("does not replay a control decision after its result waiter already timed out", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    const result = sendPromptResponseCommand({ type: "control_cancel_request", request_id: "timed-out", client_command_id: "control-timed-out" });
    const expired = expect(result).rejects.toThrow("操作超时");
    act(() => vi.advanceTimersByTime(60_000));
    await expired;
    act(() => socket.emit("close", 1006));
    act(() => vi.advanceTimersByTime(2_000));
    const replacement = MockWebSocket.instances[1];
    act(() => replacement.emit("open"));
    act(() => replacement.emitMessage({ type: "session.restored", active_conversation_id: null,
      conversation_switched_follows: false, session: { active_conversation_id: null } }));
    await flushQueuedCommands();
    expect(sentCommands(replacement).filter((command) => command.client_command_id === "control-timed-out")).toEqual([]);
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

  it("ends a rejected restore without releasing queued work or losing drafts and the replay cursor", async () => {
    const messages = [{ id: "unfinished", role: "assistant" as const, content: "partial answer",
      timestamp: 1, artifacts: [], isStreaming: true }];
    const attachments = [{ id: "draft-image", name: "draft.png", type: "image/png", size: 10 }];
    useAppStore.setState({ conversationId: "conv-rejected-restore", messages,
      isStreaming: true, conversationStreaming: { "conv-rejected-restore": true },
      draft: "unsent user instruction", attachments });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const restore = sentCommands(socket).find((command) => command.type === "session.restore")!;

    act(() => {
      expect(getWebSocket()?.send({ type: "user_message", content: "queued instruction",
        conversation_id: "conv-rejected-restore", client_command_id: "queued-before-rejection" })).toBe(true);
      socket.emitMessage({ type: "agent.progress", conversation_id: "conv-rejected-restore",
        seq: 10, previous_replay_seq: 0, stage: "running", message: "buffered" });
      socket.emitMessage({ type: "command.result", command: "session.restore", level: "error",
        message: "session.capabilities contains a non-JSON value (tuple)",
        client_command_id: restore.client_command_id, client_command_type: "session.restore",
        conversation_id: "conv-rejected-restore", workspace_root: "", seq: 11, previous_replay_seq: 10 });
    });
    expect(useAppStore.getState()).toMatchObject({ connectionPhase: "failed", isConnected: false,
      draft: "unsent user instruction", attachments, messages, isStreaming: true });
    expect(useAppStore.getState().connectionError).toContain("non-JSON value");
    expect(getLastReceivedServerSeqForTests()).toBe(0);
    expect(sentCommandCount(socket, "user_message")).toBe(0);
    expect(getWebSocket()?.send({ type: "conversation.list" })).toBe(false);
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("会话恢复失败"), "error", 0);

    act(() => {
      socket.emit("close", 1000);
      vi.advanceTimersByTime(100_000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    act(() => getWebSocket()?.reconnect());
    const resumed = MockWebSocket.instances[1];
    act(() => resumed.emit("open"));
    await flushQueuedCommands();
    expect(sentCommandCount(resumed, "user_message")).toBe(0);
    act(() => resumed.emitMessage({ type: "session.restored", active_conversation_id: "conv-rejected-restore",
      conversation_switched_follows: false, last_seq: 0, current_seq: 11, replayed_events: 0,
      session: { active_conversation_id: "conv-rejected-restore" } }));
    await flushQueuedCommands();
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(sentCommands(resumed).filter((command) => command.client_command_id === "queued-before-rejection")).toHaveLength(1);
    expect(sentCommandCount(resumed, "session.restore")).toBe(1);
  });

  it.each(["session.restore", "session.sync"] as const)("reports a current %s failure to its caller and ignores late success", async (command) => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const pending = sendClientCommandAwaitResult({ type: command, client_command_id: "current-recovery" }, command);
    act(() => socket.emitMessage({ type: "command.result", command, level: "error", message: "projection failed",
      client_command_id: "current-recovery", client_command_type: command }));
    await expect(pending).resolves.toMatchObject({ level: "error", message: "projection failed" });
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    act(() => socket.emitMessage({ type: command === "session.restore" ? "session.restored" : "session.synced",
      active_conversation_id: null, conversation_switched_follows: false,
      session: { active_conversation_id: null }, client_command_id: "current-recovery", client_command_type: command }));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
  });

  it("keeps recovery pending when an older request or replayed restore reports failure", async () => {
    useAppStore.setState({ conversationId: "conv-current-restore" });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    await flushQueuedCommands();
    const restore = sentCommands(socket).find((command) => command.type === "session.restore")!;
    act(() => {
      socket.emitMessage({ type: "command.result", command: "session.restore", level: "error", message: "older failure",
        client_command_id: "obsolete-restore", client_command_type: "session.restore" });
      socket.emitMessage({ type: "command.result", command: "session.restore", level: "error", message: "replayed failure",
        client_command_id: restore.client_command_id, client_command_type: "session.restore", replayed: true });
    });
    expect(useAppStore.getState().connectionPhase).toBe("connecting");
    expect(socket.close).not.toHaveBeenCalled();
    act(() => socket.emitMessage({ type: "session.restored", active_conversation_id: "conv-current-restore",
      conversation_switched_follows: false, session: { active_conversation_id: "conv-current-restore" },
      client_command_id: restore.client_command_id, client_command_type: "session.restore" }));
    await flushQueuedCommands();
    expect(useAppStore.getState().connectionPhase).toBe("connected");
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
  it("allows an explicit retry after exhaustion without replacing the session", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const sessionId = getWebSocket()?.sessionId;
    let socket = MockWebSocket.instances[0];
    for (let attempt = 0; attempt < 5; attempt += 1) {
      act(() => socket.emit("close", 1006));
      act(() => vi.advanceTimersByTime(40_000));
      socket = MockWebSocket.instances[attempt + 1];
    }
    act(() => socket.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    const count = MockWebSocket.instances.length;
    act(() => getWebSocket()?.reconnect());
    expect(MockWebSocket.instances).toHaveLength(count + 1);
    expect(getWebSocket()?.sessionId).toBe(sessionId);
    expect(useAppStore.getState().reconnectAttempt).toBe(0);
    expect(useAppStore.getState().connectionPhase).toBe("connecting");
  });

  it("seals a disconnected live turn as unconfirmed partial rather than a backend failure", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    let socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    useAppStore.setState({
      conversationId: "live-owner", isStreaming: true,
      messages: [{ id: "live-message", role: "assistant", content: "partial output", timestamp: 1, artifacts: [], isStreaming: true }],
      conversationStreaming: { "live-owner": true },
    });
    for (let attempt = 0; attempt < 5; attempt++) {
      act(() => socket.emit("close", 1006));
      act(() => vi.advanceTimersByTime(40_000));
      socket = MockWebSocket.instances[attempt + 1];
    }
    act(() => socket.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    expect(useAppStore.getState().messages[0]).toMatchObject({ terminalStatus: "partial", failureRecoverable: true, isStreaming: false });
  });

  it("does not advance the durable cursor for an unknown transient wire frame", () => {
    commitProcessedInboundEvent({ type: "agent.progress", conversation_id: "owner", seq: 1, previous_replay_seq: 0 } as never);
    expect(skipUndeliverableInboundEvent({ type: "future.transient", seq: 2 }, 2)).toBe(true);
    expect(getLastReceivedServerSeqForTests()).toBe(1);
    commitProcessedInboundEvent({ type: "agent.progress", conversation_id: "owner", seq: 3, previous_replay_seq: 1 } as never);
    expect(getLastReceivedServerSeqForTests()).toBe(3);
  });

  it("defers an unknown durable frame until the requested recovery snapshot commits", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const first = MockWebSocket.instances[0];
    act(() => first.emit("open"));
    commitProcessedInboundEvent({ type: "agent.progress", conversation_id: "owner", seq: 1, previous_replay_seq: 0 } as never);
    useAppStore.setState({ conversationId: "owner", workingDirectory: "C:/owned", conversations: [{ id: "owner", title: "Owned", workspaceRoot: "C:/owned", updatedAt: "2026-10-03" }] });
    act(() => first.emit("close", 1006));
    act(() => vi.advanceTimersByTime(2_000));
    const resumed = MockWebSocket.instances[1];
    act(() => resumed.emit("open"));
    await flushQueuedCommands();
    act(() => resumed.emitMessage({ type: "future.durable", conversation_id: "owner", seq: 2, previous_replay_seq: 1 }));
    expect(getLastReceivedServerSeqForTests()).toBe(1);
    act(() => resumed.emitMessage({ type: "session.restored", active_conversation_id: "owner", current_seq: 1, last_seq: 1, replayed_events: 0,
      session: { active_conversation_id: "owner", workspace_root: "C:/owned" } }));
    await flushQueuedCommands();
    expect(getLastReceivedServerSeqForTests()).toBe(2);
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(resumed.close).not.toHaveBeenCalled();
  });

});
