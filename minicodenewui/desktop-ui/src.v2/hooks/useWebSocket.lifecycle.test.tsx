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
import { initialSessionSyncSnapshot } from "../../test/initial-session-sync";
import { useSidebarSections } from "../protocol/thread-metadata";
import { resetEditorDraftStorageForTests } from "../stores/editor-drafts";
import { pushToast } from "../overlays/ToastContainer";
import type { ClientCommand } from "../protocol/events";
import { sendClientCommandAwaitResult, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { projectMessagesToTurns } from "../chat/chatSurfaceState";
import { sendChatMessage } from "../chat/sendChatMessage";
import { QueuedMessageList } from "../composer/QueuedMessageList";
import { latestProjectionCommandIds } from "../protocol/command-projection";
import {
  getWebSocket,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  commitProcessedInboundEvent,
  getLastReceivedServerSeqForTests,
  getPendingClientCommandAckIdsForTests,
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

const openWithInitialSync = (socket: MockWebSocket) => {
  socket.emit("open");
  const sync = sentCommands(socket).find(command => command.type === "session.sync");
  if (sync) socket.emitMessage(initialSessionSyncSnapshot(new URL(socket.url).searchParams.get("session_id")!, sync.client_command_id));
};

const flushQueuedCommands = async () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

const Harness = () => {
  useWebSocketConnection();
  return null;
};

const connectSkillOwner = async (owner = "skills-owner", root = "C:/skills-project") => {
  useAppStore.setState({ conversationId: null, activeConversationDraftId: null, pendingConversationSwitchId: null,
    workingDirectory: root, availableSkills: [], conversations: [{ id: owner, title: owner, updatedAt: "2026-10-26", workspaceRoot: root }] });
  render(<Harness />);
  act(() => vi.advanceTimersByTime(0));
  const socket = MockWebSocket.instances[0];
  act(() => {
    socket.emit("open");
    const sync = sentCommands(socket).find(command => command.type === "session.sync")!;
    socket.emitMessage({ ...initialSessionSyncSnapshot(new URL(socket.url).searchParams.get("session_id")!, sync.client_command_id,
      { active_conversation_id: owner, workspace_root: root, selected_model: "fixture-model" }), client_command_type: "session.sync" });
  });
  await flushQueuedCommands();
  expect(useAppStore.getState()).toMatchObject({ conversationId: owner, workingDirectory: root, connectionPhase: "connected" });
  return socket;
};

const skillCatalogReceipt = (commandId: string, owner: string, root: string, name: string, seq: number, previous: number) => ({
  type: "skills.list", client_command_id: commandId, client_command_type: "skills.list", conversation_id: owner, workspace_root: root,
  skills: [{ name, description: `Session-owned ${name}`, path: `${root}/.minicode/skills/${name}/SKILL.md`, source_level: "user" }],
  seq, previous_replay_seq: previous, event_id: `skills:${commandId}:${seq}`,
});

const acknowledgeSkillReads = (socket: MockWebSocket, ...ids: string[]) => {
  for (const id of ids) socket.emitMessage({ type: "client.command.ack", command_type: "skills.list", client_command_id: id, accepted: true });
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

  it("admits cold-start HTTP consumers only after the original session's model and sync snapshot are ready", async () => {
    let registered = false;
    const requests: { url: URL; registered: boolean }[] = [];
    const fetch = vi.fn(async (input: string | URL | Request) => {
      const url = new URL(String(input)); requests.push({ url, registered });
      return new Response(JSON.stringify(registered ? { sections: [{ id: "ready-section", name: "Original section", appearance: null, item_keys: [] }] }
        : { detail: "The original session is not connected" }), { status: registered ? 200 : 404 });
    });
    vi.stubGlobal("fetch", fetch);
    const SidebarHarness = () => {
      useWebSocketConnection();
      const { sections } = useSidebarSections();
      return <div>{sections.map(section => section.name).join(",")}</div>;
    };
    useAppStore.setState({ currentModel: "", draft: "Original unsent draft", runtimeSession: null });
    render(<SidebarHarness />);
    expect(getWebSocket()).not.toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => socket.emit("open"));
    expect(useAppStore.getState()).toMatchObject({ isConnected: false, connectionPhase: "connecting", currentModel: "", draft: "Original unsent draft" });
    expect(sentCommandTypes(socket)).toEqual(["session.sync"]);
    const sync = sentCommands(socket)[0];
    act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: sync.client_command_id, command_type: "session.sync" }));
    expect(useAppStore.getState().isConnected).toBe(false);
    expect(fetch).not.toHaveBeenCalled();

    registered = true;
    const sessionId = new URL(socket.url).searchParams.get("session_id")!;
    await act(async () => {
      socket.emitMessage(initialSessionSyncSnapshot(sessionId, sync.client_command_id, {
        active_conversation_id: null, active_stream_conversation_ids: [], selected_model: "initialized-model",
      }));
      await Promise.resolve(); await Promise.resolve();
    });
    expect(useAppStore.getState()).toMatchObject({ isConnected: true, connectionPhase: "connected", currentModel: "initialized-model", draft: "Original unsent draft" });
    expect(requests).toHaveLength(1);
    expect(requests[0].registered).toBe(true);
    expect(requests[0].url.pathname).toBe("/api/thread-metadata/sections");
    expect(requests[0].url.searchParams.get("session_id")).toBe(sessionId);
    expect(screen.getByText("Original section")).toBeTruthy();
    expect(sentCommandTypes(socket)).toEqual(["session.sync", "conversation.list", "commands.list", "skills.list"]);
    expect(pushToast).not.toHaveBeenCalledWith(expect.stringContaining("The original session is not connected"), "error");
  });

  it("keeps realtime audio and RPC results out of replay while preserving the actual speech commit chain", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const first = MockWebSocket.instances[0];
    act(() => openWithInitialSync(first));
    const owner = { threadId: "voice-A", conversation_id: "voice-A" };
    const audio = { type: "thread/realtime/appendAudio" as const, ...owner,
      audio: { data: "AAAA", sampleRate: 24000, numChannels: 1 } };
    expect(getWebSocket()!.send(audio)).toBe(true);
    expect(getWebSocket()!.send({ type: "thread/realtime/start", ...owner, outputModality: "audio" })).toBe(true);
    const liveAudio = sentCommands(first).find(command => command.type === audio.type)!;
    expect(liveAudio.client_command_id).toBeUndefined();
    const startId = sentCommands(first).find(command => command.type === "thread/realtime/start")!.client_command_id!;
    expect(getPendingClientCommandAckIdsForTests()).not.toContain(startId);
    act(() => first.emitMessage({ type: "command.result", ...owner, command: "conversation.rename", level: "success", message: "", data: {},
      seq: 1, previous_replay_seq: 0 }));
    act(() => first.emitMessage({ type: "thread/realtime/outputAudio/delta", ...owner, audio: audio.audio, seq: 2 }));
    act(() => first.emitMessage({ type: "command.result", ...owner, command: "thread/realtime/start", level: "success", message: "", data: {}, seq: 3 }));
    await flushQueuedCommands();
    expect(getLastReceivedServerSeqForTests()).toBe(1);
    act(() => first.emitMessage({ type: "thread/realtime/item/completed", ...owner, seq: 4, previous_replay_seq: 1,
      item: { id: "spoken", realtimeSessionId: "voice-session", type: "transcriptSegment", role: "assistant", text: "Spoken result" } }));
    await flushQueuedCommands();
    expect(getLastReceivedServerSeqForTests()).toBe(4);
    expect(first.close).not.toHaveBeenCalled();
    act(() => first.emit("close", 1006));
    expect(getWebSocket()!.send(audio)).toBe(false);
    act(() => vi.advanceTimersByTime(2000));
    const resumed = MockWebSocket.instances[1];
    act(() => openWithInitialSync(resumed));
    await flushQueuedCommands();
    expect(sentCommands(resumed).find(command => command.type === "session.restore")).toMatchObject({ last_seq: 4 });
    expect(getWebSocket()!.send(audio)).toBe(false);
    act(() => resumed.emitMessage({ type: "session.restored", active_conversation_id: null, conversation_switched_follows: false,
      current_seq: 4, last_seq: 4, replayed_events: 0, session: { active_conversation_id: null } }));
    await flushQueuedCommands();
    expect(sentCommandTypes(resumed).filter(type => type.startsWith("thread/realtime/"))).toEqual([]);
    expect(getWebSocket()!.send(audio)).toBe(true);
    expect(sentCommands(resumed).find(command => command.type === audio.type)!.client_command_id).toBeUndefined();
  });

  it("ignores a close event from a socket owned by an old effect", () => {
    const firstRender = render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const firstSocket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(firstSocket));
    expect(useAppStore.getState().isConnected).toBe(true);

    firstRender.unmount();
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const currentSocket = MockWebSocket.instances[1];
    act(() => openWithInitialSync(currentSocket));

    act(() => firstSocket.emit("close"));
    expect(useAppStore.getState().isConnected).toBe(true);

    act(() => currentSocket.emit("close"));
    expect(useAppStore.getState().isConnected).toBe(false);
  });

  it("sends every explicitly identified request instead of coalescing away its result", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));

    act(() => vi.advanceTimersByTime(69_999));
    expect(socket.close).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(socket.close).toHaveBeenCalledTimes(1);
  });

  it("keeps a healthy quiet stream alive when any inbound event answers the probe", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));

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
    act(() => openWithInitialSync(socket));

    act(() => socket.emit("close", 1008));
    act(() => vi.advanceTimersByTime(600_001));

    expect(useAppStore.getState().isConnected).toBe(false);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("reconnects after an ordinary transport close", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));

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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(firstSocket));
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
    act(() => openWithInitialSync(thirdSocket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(firstSocket));
    expect(useAppStore.getState().connectionPhase).toBe("connected");

    act(() => firstSocket.emit("close", 1006));
    expect(useAppStore.getState().reconnectAttempt).toBe(1);
    act(() => vi.advanceTimersByTime(1_500));

    const reconnectSocket = MockWebSocket.instances[1];
    act(() => openWithInitialSync(reconnectSocket));
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

  it("restores background A while reading B and commits replayed terminal evidence after an idle snapshot", () => {
    const owner = "long-a";
    const reader = "reader-b";
    const history = [{ id: "answer-a", turnId: "turn-a", role: "assistant" as const, content: "", artifacts: [], timestamp: 1, isStreaming: true,
      blocks: [{ type: "tool_call" as const, record: { id: "command-a", name: "run_command", args: { command: "node long-task.mjs" },
        turnId: "turn-a", status: "running" as const, startedAt: 1, stdoutPreview: "kept real output" } }] }];
    useAppStore.setState({ conversationId: reader, messages: [], draft: "keep B unsent draft", workingDirectory: "C:/b",
      conversations: [{ id: owner, title: "A", updatedAt: "2026-10-20" }, { id: reader, title: "B", updatedAt: "2026-10-20" }],
      conversationMessages: { [owner]: history }, conversationStreaming: { [owner]: true },
      conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, conversationAgentStates: {}, sideChats: {},
      pendingConversationSwitchId: null, pendingConversationCreateId: null });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const first = MockWebSocket.instances[0];
    act(() => openWithInitialSync(first));
    act(() => first.emitMessage({ type: "session.restored", active_conversation_id: reader, conversation_switched_follows: false,
      last_seq: 0, current_seq: 0, replayed_events: 0, requested_last_seq: 0,
      session: { active_conversation_id: reader, active_stream_conversation_ids: [owner] } }));
    act(() => first.emit("close", 1006));
    expect(useAppStore.getState().conversationMessages[owner][0].isStreaming).toBe(true);
    act(() => vi.advanceTimersByTime(1_500));
    const recovered = MockWebSocket.instances[1];
    act(() => openWithInitialSync(recovered));
    act(() => recovered.emitMessage({ type: "session.restored", active_conversation_id: reader, conversation_switched_follows: false,
      last_seq: 0, current_seq: 2, replayed_events: 2, requested_last_seq: 0,
      session: { active_conversation_id: reader, active_task_id: null, active_stream_conversation_ids: [] } }));
    const idleMessage = useAppStore.getState().conversationMessages[owner][0];
    expect(idleMessage.terminalStatus).toBeUndefined();
    expect(getToolCallsFromMessage(idleMessage)[0].status).toBe("running");
    expect(projectMessagesToTurns([idleMessage], false)[0].committedCells[0]).toMatchObject({ status: "partial" });
    act(() => recovered.emitMessage({ type: "session.replay", last_seq: 0, current_seq: 2, replayed_events: 2, events: [
      { type: "tool_result", conversation_id: owner, message_id: "answer-a", turn_id: "turn-a", id: "command-a", status: "success",
        summary: "Actual exit 0", seq: 1, previous_replay_seq: 0, event_id: "a-result" },
      { type: "done", conversation_id: owner, message_id: "answer-a", turn_id: "turn-a", status: "completed", reason: "real-terminal",
        usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, input_includes_cache_read: true },
        seq: 2, previous_replay_seq: 1, event_id: "a-done" },
    ] }));
    const state = useAppStore.getState();
    expect(state).toMatchObject({ conversationId: reader, draft: "keep B unsent draft", isStreaming: false, connectionPhase: "connected" });
    expect(state.conversationMessages[owner][0]).toMatchObject({ terminalStatus: "completed", terminationReason: "real-terminal", isStreaming: false });
    expect(getToolCallsFromMessage(state.conversationMessages[owner][0])[0]).toMatchObject({ status: "success", stdoutPreview: "kept real output" });
    expect(getLastReceivedServerSeqForTests()).toBe(2);
  });

  it("runs exactly five reconnect attempts before entering the failed phase", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    let socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));

    for (let attempt = 1; attempt <= 5; attempt += 1) {
      act(() => socket.emit("close", 1006));
      expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
      expect(useAppStore.getState().reconnectAttempt).toBe(attempt);
      expect(useAppStore.getState().reconnectMaxAttempts).toBe(5);

      act(() => vi.advanceTimersByTime(40_000));
      expect(MockWebSocket.instances).toHaveLength(attempt + 1);
      socket = MockWebSocket.instances[attempt];
      act(() => openWithInitialSync(socket));
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

  it("replays submitted steer commands with their original turn fences after a new run appears", async () => {
    const owner = "steer-replay-owner";
    useAppStore.setState({ conversationId: owner, draft: "keep authored draft", workingDirectory: "C:/steer-owner", composerDraftsReady: true,
      conversations: [{ id: owner, title: "Owner", updatedAt: "2026-10-20", workspaceRoot: "C:/steer-owner" }],
      isStreaming: true, sideChats: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, conversationAgentStates: {},
      pendingConversationSwitchId: null, pendingConversationCreateId: null,
      messages: [{ id: "old-answer", turnId: "original-turn", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true },
        { id: "queued-user", role: "user", content: "queued direction", artifacts: [], timestamp: 2, queueState: "queued", queueMessageId: "queued-answer" }] });
    render(<><Harness /><QueuedMessageList /></>);
    act(() => vi.advanceTimersByTime(0)); const first = MockWebSocket.instances[0]; act(() => openWithInitialSync(first));
    act(() => first.emitMessage({ type: "session.restored", active_conversation_id: owner, conversation_switched_follows: false,
      session: { active_conversation_id: owner, active_stream_conversation_ids: [owner] } }));
    fireEvent.click(screen.getByRole("button", { name: "引导" }));
    expect(sendChatMessage({ conversationId: owner, displayContent: "original direction", allowWhileStreaming: true, busyBehavior: "steer" })).toBe(true);
    await flushQueuedCommands();
    const submitted = sentCommands(first).filter(command => command.type === "user_message" || command.type === "user_message.queue.steer");
    expect(submitted).toHaveLength(2);
    for (const command of submitted) expect(command).toMatchObject({ expected_turn_id: "original-turn" });
    act(() => first.emit("close", 1006));
    // A new run updates both the reader and its conversation cache. Updating
    // only messages creates an impossible split that restore correctly reads
    // back from the previous cached turn.
    act(() => useAppStore.getState().hydrateConversationMessages(owner, [{ id: "new-answer", turnId: "new-current-turn", role: "assistant", content: "new work", artifacts: [], timestamp: 3, isStreaming: true }], { activate: true, isStreaming: true }));
    expect(useAppStore.getState().messages[0].turnId).toBe("new-current-turn");
    expect(useAppStore.getState().conversationMessages[owner][0].turnId).toBe("new-current-turn");
    act(() => vi.advanceTimersByTime(1500)); const recovered = MockWebSocket.instances[1]; act(() => openWithInitialSync(recovered));
    act(() => recovered.emitMessage({ type: "session.restored", active_conversation_id: owner, conversation_switched_follows: false,
      session: { active_conversation_id: owner, active_stream_conversation_ids: [owner] } }));
    await flushQueuedCommands();
    const replayed = sentCommands(recovered).filter(command => command.type === "user_message" || command.type === "user_message.queue.steer");
    expect(replayed).toEqual(submitted);
    expect(useAppStore.getState().messages[0].turnId).toBe("new-current-turn");
    expect(useAppStore.getState().draft).toBe("keep authored draft");
  });

  it("projects permanent close as a terminal connection failure", () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));

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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
    const result = sendPromptResponseCommand({ type: "control_cancel_request", request_id: "approval-unconfirmed", client_command_id: "withdraw-control" });
    const failure = expect(result).rejects.toThrow("尚未确认");
    if (admitted) act(() => socket.emitMessage({ type: "client.command.ack", client_command_id: "withdraw-control", command_type: "control_cancel_request", accepted: true }));
    act(() => socket.emit("close", 1006));
    await failure;
    act(() => vi.advanceTimersByTime(2_000));
    const replacement = MockWebSocket.instances[1];
    act(() => openWithInitialSync(replacement));
    act(() => replacement.emitMessage({ type: "session.restored", active_conversation_id: null, conversation_switched_follows: false, session: { active_conversation_id: null } }));
    await flushQueuedCommands();
    expect(sentCommands(replacement).filter((command) => command.client_command_id === "withdraw-control")).toEqual([]);
  });

  it("does not replay a control decision after its result waiter already timed out", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));
    const result = sendPromptResponseCommand({ type: "control_cancel_request", request_id: "timed-out", client_command_id: "control-timed-out" });
    const expired = expect(result).rejects.toThrow("操作超时");
    act(() => vi.advanceTimersByTime(60_000));
    await expired;
    act(() => socket.emit("close", 1006));
    act(() => vi.advanceTimersByTime(2_000));
    const replacement = MockWebSocket.instances[1];
    act(() => openWithInitialSync(replacement));
    act(() => replacement.emitMessage({ type: "session.restored", active_conversation_id: null,
      conversation_switched_follows: false, session: { active_conversation_id: null } }));
    await flushQueuedCommands();
    expect(sentCommands(replacement).filter((command) => command.client_command_id === "control-timed-out")).toEqual([]);
  });

  it("refreshes the command catalog after session.synced applies the canonical owner", async () => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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

  it("seals an unconfirmed local stream after restore failure without releasing queued work or losing evidence", async () => {
    const messages = [{ id: "unfinished", role: "assistant" as const, content: "partial answer",
      timestamp: 1, artifacts: [], isStreaming: true }];
    const attachments = [{ id: "draft-image", name: "draft.png", type: "image/png", size: 10 }];
    useAppStore.setState({ conversationId: "conv-rejected-restore", messages,
      isStreaming: true, conversationStreaming: { "conv-rejected-restore": true },
      draft: "unsent user instruction", attachments });
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));
    await flushQueuedCommands();
    const restore = sentCommands(socket).find((command) => command.type === "session.restore")!;

    act(() => {
      expect(getWebSocket()?.send({ type: "user_message", content: "queued instruction",
        conversation_id: "conv-rejected-restore", client_command_id: "queued-before-rejection" })).toBe(true);
      socket.emitMessage({ type: "agent.progress", conversation_id: "conv-rejected-restore",
        message_id: "unfinished", seq: 10, previous_replay_seq: 0, stage: "running", message: "buffered" });
      socket.emitMessage({ type: "command.result", command: "session.restore", level: "error",
        message: "session.capabilities contains a non-JSON value (tuple)",
        client_command_id: restore.client_command_id, client_command_type: "session.restore",
        conversation_id: "conv-rejected-restore", workspace_root: "", seq: 11, previous_replay_seq: 10 });
    });
    expect(useAppStore.getState()).toMatchObject({ connectionPhase: "failed", isConnected: false,
      draft: "unsent user instruction", attachments, isStreaming: false,
      conversationStreaming: { "conv-rejected-restore": false } });
    expect(useAppStore.getState().messages[0]).toMatchObject({
      id: "unfinished", content: "partial answer", isStreaming: false,
      terminalStatus: "partial", failureRecoverable: true,
      failureMessage: expect.stringContaining("non-JSON value"),
    });
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
    act(() => openWithInitialSync(resumed));
    await flushQueuedCommands();
    expect(sentCommandCount(resumed, "user_message")).toBe(0);
    act(() => {
      resumed.emitMessage({ type: "session.restored", active_conversation_id: "conv-rejected-restore",
        conversation_switched_follows: true, last_seq: 0, current_seq: 11, replayed_events: 0,
        session: { active_conversation_id: "conv-rejected-restore" } });
      resumed.emitMessage({ type: "conversation.switched", conversation_id: "conv-rejected-restore",
        conversation: { id: "conv-rejected-restore", title: "Recovered",
          transcript: [{ id: "unfinished", role: "assistant", content: "Authoritative completed answer",
            terminal_status: "completed" }] },
        session: { active_conversation_id: "conv-rejected-restore", active_stream_conversation_ids: [], active_task_id: null } });
    });
    await flushQueuedCommands();
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(sentCommands(resumed).filter((command) => command.client_command_id === "queued-before-rejection")).toHaveLength(1);
    expect(sentCommandCount(resumed, "session.restore")).toBe(1);
    expect(useAppStore.getState().messages[0]).toMatchObject({
      id: "unfinished", content: "Authoritative completed answer", terminalStatus: "completed",
    });
  });

  it.each(["session.restore", "session.sync"] as const)("reports a current %s failure to its caller and ignores late success", async (command) => {
    render(<Harness />);
    act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0];
    act(() => openWithInitialSync(socket));
    await flushQueuedCommands();
    useAppStore.setState({ conversationId: "conv-live", messages: [{
      id: "live-answer", role: "assistant", content: "Retained", artifacts: [], timestamp: 1, isStreaming: true,
    }], isStreaming: true, conversationStreaming: { "conv-live": true } });
    const pending = sendClientCommandAwaitResult({ type: command, client_command_id: "current-recovery" }, command);
    act(() => socket.emitMessage({ type: "command.result", command, level: "error", message: "projection failed",
      client_command_id: "current-recovery", client_command_type: command }));
    await expect(pending).resolves.toMatchObject({ level: "error", message: "projection failed" });
    expect(useAppStore.getState().connectionPhase).toBe("failed");
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(useAppStore.getState().messages[0]).toMatchObject({ content: "Retained", terminalStatus: "partial", failureRecoverable: true });
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    expect(sentCommandCount(socket, "commands.list")).toBe(before);
    // The canonical owner is applied first; catalog reads start on the next
    // task so they do not delay the restored conversation's initial paint.
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await flushQueuedCommands();
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(firstSocket));
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
    act(() => openWithInitialSync(reconnectSocket));
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
    act(() => openWithInitialSync(socket));
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
    act(() => openWithInitialSync(first));
    commitProcessedInboundEvent({ type: "agent.progress", conversation_id: "owner", seq: 1, previous_replay_seq: 0 } as never);
    useAppStore.setState({ conversationId: "owner", workingDirectory: "C:/owned", conversations: [{ id: "owner", title: "Owned", workspaceRoot: "C:/owned", updatedAt: "2026-10-03" }] });
    act(() => first.emit("close", 1006));
    act(() => vi.advanceTimersByTime(2_000));
    const resumed = MockWebSocket.instances[1];
    act(() => openWithInitialSync(resumed));
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

  it("keeps the latest same-owner Skill projection after both transport ACKs and consumes the obsolete durable receipt", async () => {
    const owner = "skills-owner", root = "C:/skills-project", socket = await connectSkillOwner(owner, root);
    const observed: string[] = [];
    const unsubscribe = getWebSocket()?.subscribe(value => {
      const event = value as { type?: string; client_command_id?: string };
      if (event.type === "skills.list") observed.push(event.client_command_id!);
    });
    act(() => {
      expect(getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "skills-old" })).toBe(true);
      expect(getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "skills-new" })).toBe(true);
      acknowledgeSkillReads(socket, "skills-old", "skills-new");
      socket.emitMessage(skillCatalogReceipt("skills-new", owner, root, "fresh", 1, 0));
      socket.emitMessage(skillCatalogReceipt("skills-old", owner, root, "obsolete", 2, 1));
      socket.emitMessage({ type: "command.result", command: "skills.list", level: "info", message: "", data: { count: 1 },
        conversation_id: owner, workspace_root: root, seq: 3, previous_replay_seq: 2 });
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["fresh"]);
    expect(observed).toEqual(["skills-new"]);
    expect(getPendingClientCommandAckIdsForTests()).not.toContain("skills-old");
    expect(getPendingClientCommandAckIdsForTests()).not.toContain("skills-new");
    expect(getLastReceivedServerSeqForTests()).toBe(3);
    expect(socket.close).not.toHaveBeenCalled();
    unsubscribe?.();
  });

  it("uses the canonical Skill fallback owner while a different conversation is pending and preserves that navigation", async () => {
    const owner = "skills-owner", root = "C:/skills-project", socket = await connectSkillOwner(owner, root);
    act(() => {
      getWebSocket()?.send({ type: "conversation.switch", conversation_id: "pending-owner", client_command_id: "pending-navigation" });
      useAppStore.setState({ pendingConversationSwitchId: "pending-owner" });
      getWebSocket()?.send({ type: "skills.list", client_command_id: "fallback-old" });
      getWebSocket()?.send({ type: "skills.list", client_command_id: "fallback-new" });
      socket.emitMessage(skillCatalogReceipt("fallback-new", owner, root, "fresh", 1, 0));
      socket.emitMessage(skillCatalogReceipt("fallback-old", owner, root, "obsolete", 2, 1));
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["fresh"]);
    expect(latestProjectionCommandIds.get("foreground-intent")).toBe("pending-navigation");
    expect(useAppStore.getState()).toMatchObject({ conversationId: owner, pendingConversationSwitchId: "pending-owner" });
    act(() => socket.emitMessage({ type: "conversation.switched", client_command_id: "pending-navigation", client_command_type: "conversation.switch",
      conversation_id: "pending-owner", is_hydrating: false, conversation: { id: "pending-owner", title: "Pending", updated_at: "2026-10-26", workspace_root: "C:/pending-project", messages: [] } }));
    expect(useAppStore.getState()).toMatchObject({ conversationId: "pending-owner", pendingConversationSwitchId: null });
  });

  it("does not supersede another Skill owner or another root while ordering repeated reads of the original scope", async () => {
    const socket = await connectSkillOwner("A", "C:/one");
    act(() => {
      getWebSocket()?.send({ type: "skills.list", conversation_id: "A", workspace_root: "C:/one", client_command_id: "a-old" });
      useAppStore.setState({ conversationId: "B", workingDirectory: "C:/one" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: "B", workspace_root: "C:/one", client_command_id: "b-original" });
      useAppStore.setState({ conversationId: "A", workingDirectory: "C:/two" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: "A", workspace_root: "C:/two", client_command_id: "a-other-root" });
      useAppStore.setState({ conversationId: "A", workingDirectory: "C:/one" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: "A", workspace_root: "C:/one", client_command_id: "a-new" });
      socket.emitMessage(skillCatalogReceipt("a-new", "A", "C:/one", "a-fresh", 1, 0));
      socket.emitMessage(skillCatalogReceipt("a-old", "A", "C:/one", "obsolete", 2, 1));
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["a-fresh"]);
    act(() => {
      useAppStore.setState({ conversationId: "B", workingDirectory: "C:/one" });
      socket.emitMessage(skillCatalogReceipt("b-original", "B", "C:/one", "b-fresh", 3, 2));
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["b-fresh"]);
    act(() => {
      useAppStore.setState({ conversationId: "A", workingDirectory: "C:/two" });
      socket.emitMessage(skillCatalogReceipt("a-other-root", "A", "C:/two", "other-root-fresh", 4, 3));
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["other-root-fresh"]);
    expect(getLastReceivedServerSeqForTests()).toBe(4);
    expect(socket.close).not.toHaveBeenCalled();
  });

  it.each([
    ["C:/Owned/Repo", "C:\\Owned\\Repo\\.\\sub\\..", "c:/owned/repo/"],
    ["//Server/Share/Repo", "\\\\Server\\Share\\Repo\\.", "//server/share/repo/"],
  ])("orders Skill receipts across aliases of the same Windows workspace (%s)", async (root, first, latest) => {
    const owner = "alias-owner", socket = await connectSkillOwner(owner, root);
    act(() => {
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: first, client_command_id: "alias-old" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: latest, client_command_id: "alias-new" });
      socket.emitMessage(skillCatalogReceipt("alias-new", owner, root, "fresh", 1, 0));
      socket.emitMessage(skillCatalogReceipt("alias-old", owner, root, "obsolete", 2, 1));
    });
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["fresh"]);
    expect(getLastReceivedServerSeqForTests()).toBe(2);
    expect(socket.close).not.toHaveBeenCalled();
  });

  it.each(["buffered", "replay"] as const)("consumes an obsolete durable Skill receipt during recovery without projecting or notifying it (%s)", async mode => {
    const owner = "recovery-owner", root = "C:/recovery-project", first = await connectSkillOwner(owner, root);
    const observed: string[] = [];
    const unsubscribe = getWebSocket()?.subscribe(value => {
      const event = value as { type?: string; client_command_id?: string };
      if (event.type === "skills.list") observed.push(event.client_command_id!);
    });
    act(() => {
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "recovery-old" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "recovery-new" });
      acknowledgeSkillReads(first, "recovery-old", "recovery-new");
      first.emitMessage(skillCatalogReceipt("recovery-new", owner, root, "fresh-before-reconnect", 1, 0));
      first.emit("close", 1006);
    });
    act(() => vi.advanceTimersByTime(2_000));
    const resumed = MockWebSocket.instances[1];
    act(() => resumed.emit("open"));
    await flushQueuedCommands();
    const restore = sentCommands(resumed).find(command => command.type === "session.restore")!;
    if (mode === "buffered") {
      act(() => resumed.emitMessage(skillCatalogReceipt("recovery-old", owner, root, "obsolete", 2, 1)));
      expect(getLastReceivedServerSeqForTests()).toBe(1);
    }
    act(() => resumed.emitMessage({ type: "session.restored", client_command_id: restore.client_command_id, client_command_type: "session.restore",
      active_conversation_id: owner, conversation_switched_follows: false, current_seq: mode === "replay" ? 3 : 1, last_seq: 1,
      requested_last_seq: 1, replayed_events: mode === "replay" ? 2 : 0, session: { active_conversation_id: owner, workspace_root: root, selected_model: "fixture-model" } }));
    if (mode === "replay") act(() => resumed.emitMessage({ type: "session.replay", last_seq: 1, current_seq: 3, replayed_events: 2, events: [
      skillCatalogReceipt("recovery-old", owner, root, "obsolete", 2, 1),
      { type: "command.result", command: "skills.list", level: "info", message: "", data: { count: 1 }, conversation_id: owner,
        workspace_root: root, seq: 3, previous_replay_seq: 2, event_id: "recovery-old-result" },
    ] }));
    await flushQueuedCommands();
    const cursor = mode === "replay" ? 3 : 2;
    expect(getLastReceivedServerSeqForTests()).toBe(cursor);
    expect(observed).not.toContain("recovery-old");
    expect(useAppStore.getState().availableSkills.some(skill => skill.name === "obsolete")).toBe(false);
    expect(useAppStore.getState().connectionPhase).toBe("connected");
    expect(resumed.close).not.toHaveBeenCalled();
    const refresh = sentCommands(resumed).filter(command => command.type === "skills.list").at(-1)!;
    act(() => resumed.emitMessage(skillCatalogReceipt(refresh.client_command_id!, owner, root, "fresh-after-reconnect", cursor + 1, cursor)));
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["fresh-after-reconnect"]);
    expect(getLastReceivedServerSeqForTests()).toBe(cursor + 1);
    unsubscribe?.();
  });

  it("does not consume a superseded durable Skill receipt across a real replay hole", async () => {
    const owner = "hole-owner", root = "C:/hole-project", socket = await connectSkillOwner(owner, root);
    act(() => {
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "hole-old" });
      getWebSocket()?.send({ type: "skills.list", conversation_id: owner, workspace_root: root, client_command_id: "hole-new" });
      socket.emitMessage(skillCatalogReceipt("hole-old", owner, root, "obsolete", 2, 1));
    });
    expect(getLastReceivedServerSeqForTests()).toBe(0);
    expect(useAppStore.getState().availableSkills).toEqual([]);
    expect(socket.close).toHaveBeenCalledWith(4002, "event replay required");
  });

});
