/* @vitest-environment jsdom */

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true, writable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn(), dismissToast: vi.fn() }));

import { useAppStore } from "../stores";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import {
  getLastReceivedServerSeqForTests,
  getPendingClientCommandCountForTests,
  getWebSocket,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  useWebSocketConnection,
} from "./useWebSocket";

type Wire = Record<string, unknown>;
type Listener = (event: Event | MessageEvent) => void;
class MockWebSocket {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  readyState = 0;
  sent: Wire[] = [];
  private listeners = new Map<string, Set<Listener>>();
  constructor(public url: string) { MockWebSocket.instances.push(this); }
  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const callback: Listener = typeof listener === "function" ? listener as Listener : event => listener.handleEvent(event);
    const callbacks = this.listeners.get(type) ?? new Set<Listener>();
    callbacks.add(callback); this.listeners.set(type, callbacks);
  }
  send = vi.fn((raw: string) => { this.sent.push(JSON.parse(raw) as Wire); });
  close = vi.fn(() => { this.readyState = 3; });
  emit(type: "open" | "close") {
    this.readyState = type === "open" ? 1 : 3;
    const event = Object.assign(new Event(type), { code: 1000 });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  message(event: Wire) {
    for (const listener of this.listeners.get("message") ?? []) {
      listener(new MessageEvent("message", { data: JSON.stringify(event) }));
    }
  }
}
const Harness = () => { useWebSocketConnection(); return null; };
const frames = (socket: MockWebSocket) => socket.sent.filter(command => command.type === "scheduler.heartbeat.renderer_state");
const last = (socket: MockWebSocket) => frames(socket).at(-1);
const settle = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
const request = (socket: MockWebSocket, conversationId = "a") => socket.message({
  type: "scheduler.heartbeat.renderer_state.request", conversation_id: conversationId,
});
const switchSnapshot = (socket: MockWebSocket) => socket.message({
  type: "conversation.switched", conversation_id: "a",
  conversation: { id: "a", title: "A", messages: [], updated_at: "2026-10-15T00:00:00Z" },
  session: { active_conversation_id: "a", active_stream_conversation_ids: [] },
});
const restore = (socket: MockWebSocket, replayedEvents = 0) => socket.message({
  type: "session.restored", active_conversation_id: "a", conversation_switched_follows: true,
  replayed_events: replayedEvents, session: { active_conversation_id: "a", active_stream_conversation_ids: [] },
});
const open = async () => {
  render(<Harness />);
  act(() => vi.advanceTimersByTime(0));
  const socket = MockWebSocket.instances.at(-1)!;
  act(() => socket.emit("open"));
  await settle();
  return socket;
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  localStorage.clear();
  resetPendingClientCommandAcksForTests(); resetRecentInboundEventIdsForTests();
  useAppStore.setState({
    conversationId: "a", conversations: [{ id: "a", title: "A", updatedAt: "2026-10-15T00:00:00Z" }],
    messages: [], conversationMessages: {}, conversationHistoryPages: {}, conversationHydration: {},
    conversationStreaming: {}, isStreaming: false, runtimeSession: null, sideChats: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [],
    pendingDiffReview: null, diffReviewQueue: [],
    isConnected: false, connectionPhase: "connecting", reconnectAttempt: 0, reconnectMaxAttempts: null, connectionError: null,
    draft: "用户尚未发送的内容", attachments: [], scheduledTasks: [],
  });
});
afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("heartbeat renderer observer through production WS and reducers", () => {
  it("observes a loaded empty conversation from the initial session sync", async () => {
    useAppStore.setState({ conversationId: null, conversations: [] });
    const socket = await open();
    expect(frames(socket)).toHaveLength(0);
    act(() => socket.message({ type: "session.synced", active_conversation_id: "a",
      active_conversation: { id: "a", title: "A", messages: [], updated_at: "2026-10-15T00:00:00Z" },
      session: { active_conversation_id: "a", active_stream_conversation_ids: [] },
    }));
    await settle();
    expect(last(socket)).toEqual({ type: "scheduler.heartbeat.renderer_state", conversation_id: "a", is_eligible: true, reason: null });
  });

  it("answers during restoration and waits for snapshot, switch and replay before reporting resumed", async () => {
    const socket = await open();
    expect(last(socket)).toEqual({ type: "scheduler.heartbeat.renderer_state", conversation_id: "a", is_eligible: false, reason: "resuming" });
    const beforeRequest = getPendingClientCommandCountForTests();
    const beforeFrames = frames(socket).length;
    act(() => request(socket));
    expect(frames(socket)).toHaveLength(beforeFrames + 1);
    expect(getPendingClientCommandCountForTests()).toBe(beforeRequest);
    act(() => restore(socket, 1));
    act(() => switchSnapshot(socket));
    await settle();
    expect(last(socket)?.reason).toBe("resuming");
    expect(useAppStore.getState().isConnected).toBe(false);
    act(() => socket.message({ type: "session.replay", last_seq: 0, current_seq: 1, replayed_events: 1, events: [
      { type: "conversation.hydration.updated", conversation_id: "a", is_hydrating: false, seq: 1, previous_replay_seq: 0 },
    ] }));
    await settle();
    expect(last(socket)).toEqual({ type: "scheduler.heartbeat.renderer_state", conversation_id: "a", is_eligible: true, reason: null });
    expect(useAppStore.getState().isConnected).toBe(true);
    expect(frames(socket).every(frame => !Object.hasOwn(frame, "client_command_id") && !Object.hasOwn(frame, "updatedAt"))).toBe(true);
  });

  it("keeps an actual turn busy after its answer item and settles only on its terminal receipt", async () => {
    const socket = await open();
    act(() => { restore(socket); switchSnapshot(socket); });
    await settle();
    act(() => useAppStore.setState({
      messages: [{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, isStreaming: true }],
      isStreaming: true,
    }));
    await settle();
    expect(last(socket)?.is_eligible).toBe(true); // optimistic input is not an admitted turn
    act(() => socket.message({
      type: "agent.run.started", conversation_id: "a", message_id: "answer", turn_id: "real-turn",
      run_id: "real-run", role: "main", status: "running", started_at: 1,
    }));
    await settle();
    expect(last(socket)?.reason).toBe("turn_in_progress");
    act(() => socket.message({
      type: "item.completed", conversation_id: "a", message_id: "answer", turn_id: "real-turn",
      item: { id: "answer-item", type: "agent_message", text: "Answer", source: "model_final", status: "completed" },
    }));
    await settle();
    expect(last(socket)?.reason).toBe("turn_in_progress");
    act(() => socket.message({
      type: "done", conversation_id: "a", message_id: "answer", turn_id: "real-turn", status: "completed", usage: {
        input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, input_includes_cache_read: false,
      },
    }));
    await settle();
    expect(last(socket)?.is_eligible).toBe(true);
  });

  it("does not show requests, hydrate unknown targets, or move the replay cursor for the live observer", async () => {
    const socket = await open();
    act(() => { restore(socket); switchSnapshot(socket); });
    await settle();
    const messages = useAppStore.getState().messages;
    const before = socket.sent.length;
    const cursor = getLastReceivedServerSeqForTests();
    act(() => socket.message({ type: "scheduler.heartbeat.renderer_state.request", conversation_id: "unloaded", seq: 500 }));
    await settle();
    expect(socket.sent).toHaveLength(before);
    expect(useAppStore.getState().messages).toBe(messages);
    expect(getLastReceivedServerSeqForTests()).toBe(cursor);
    expect(socket.sent.some(command => command.type === "conversation.switch" && command.conversation_id === "unloaded")).toBe(false);
    expect(normalizeInboundServerEvent({ type: "scheduler.heartbeat.renderer_state.request", conversation_id: "a" })).not.toBeNull();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(normalizeInboundServerEvent({ type: "scheduler.heartbeat.renderer_state.request" })).toBeNull();
    expect(normalizeInboundServerEvent({ type: "scheduler.heartbeat.renderer_state.request", conversation_id: " " })).toBeNull();
    warn.mockRestore();
  });

  it("projects real request-user-input and MCP elicitation with their exact owners", async () => {
    const socket = await open();
    act(() => { restore(socket); switchSnapshot(socket); });
    await settle();
    const before = frames(socket).length;
    act(() => socket.message({ type: "control_request", request_id: "other", conversation_id: "b",
      request: { subtype: "can_use_tool", tool_use_id: "other", tool_name: "run_command", input: { command: "pwd" } } }));
    await settle();
    expect(frames(socket)).toHaveLength(before);
    act(() => socket.message({ type: "control_request", request_id: "mcp", conversation_id: "a", request: {
      subtype: "elicitation", tool_use_id: "mcp", prompt: "The server needs consent", question: "Continue?",
      mode: "url", url: "https://example.com/consent", elicitation_id: "consent", mcp_server_name: "example",
    } }));
    await settle();
    expect(last(socket)?.reason).toBe("waiting_on_approval");
    act(() => socket.message({ type: "approval.cancelled", conversation_id: "a", request_ids: ["mcp"] }));
    await settle();
    expect(last(socket)?.is_eligible).toBe(true);
    act(() => socket.message({ type: "control_request", request_id: "question", conversation_id: "a", request: {
      subtype: "request_user_input", tool_use_id: "question", isBlocking: true,
      questions: [{ id: "choice", header: "Choice", question: "Choose one", options: [{ label: "One", description: "First" }] }],
    } }));
    await settle();
    expect(last(socket)?.reason).toBe("waiting_on_user_input");
  });

  it("keeps submitted approval pending until the actual host clears its request", async () => {
    const socket = await open();
    act(() => { restore(socket); switchSnapshot(socket); });
    await settle();
    act(() => socket.message({ type: "control_request", request_id: "tool", conversation_id: "a",
      request: { subtype: "can_use_tool", tool_use_id: "tool", tool_name: "run_command", input: { command: "pwd" } } }));
    await settle();
    expect(last(socket)?.reason).toBe("waiting_on_approval");
    const before = frames(socket).length;
    act(() => useAppStore.getState().markApprovalSubmitted("tool"));
    await settle();
    expect(frames(socket)).toHaveLength(before);
    expect(last(socket)?.reason).toBe("waiting_on_approval");
    act(() => socket.message({ type: "approval.cancelled", conversation_id: "a", request_ids: ["tool"] }));
    await settle();
    expect(last(socket)?.is_eligible).toBe(true);
  });

  it("fences stale sockets and recomputes state on reconnect without observer replay", async () => {
    const first = await open();
    act(() => { restore(first); switchSnapshot(first); });
    await settle();
    act(() => useAppStore.getState().setAskUser({ requestId: "q", conversationId: "a", question: "Question" }));
    await settle();
    expect(last(first)?.reason).toBe("waiting_on_user_input");
    act(() => getWebSocket()?.reconnect());
    const second = MockWebSocket.instances.at(-1)!;
    act(() => second.emit("open"));
    await settle();
    const firstCount = first.sent.length;
    const secondCount = frames(second).length;
    act(() => request(first));
    await settle();
    expect(first.sent).toHaveLength(firstCount);
    expect(frames(second)).toHaveLength(secondCount);
    expect(frames(second).map(frame => frame.reason)).toEqual(["resuming"]);
    act(() => request(second));
    expect(frames(second).map(frame => frame.reason)).toEqual(["resuming", "resuming"]);
    expect(useAppStore.getState().draft).toBe("用户尚未发送的内容");
  });
});
