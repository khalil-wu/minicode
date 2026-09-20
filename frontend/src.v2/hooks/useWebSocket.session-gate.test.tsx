/* @vitest-environment jsdom */
/**
 * Session gate: replay a captured real-backend WebSocket session through the
 * renderer's transport hook and assert the end state.
 *
 * The fixture is produced by scripts/capture_session_gate.py against a live
 * backend + model (see docs/session-gate.md). It covers, on one session id:
 *
 *   connection 0  launch -> conversation.create (workspace bound)
 *                 -> user_message (six tool calls, one failing pytest, an edit)
 *                 -> second user_message; while it streams:
 *                    conversation.create (activates the new conversation)
 *                    -> conversation.switch back -> interrupt (turn fenced)
 *                 -> socket dropped without a close handshake
 *   connection 1  reconnect -> session.restore(last_seq, last_conversation_id)
 *                 -> user_message -> done
 *
 * The renderer sends its own commands here (store actions, not hand-written
 * payloads). Captured events that answer a client command are re-keyed from the
 * capture's command id to the id the renderer actually generated, matched by
 * command type and order on the same connection.
 */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

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
import type { ClientCommand } from "../protocol/events";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { sendChatMessage } from "../chat/sendChatMessage";
import { buildInterruptCommand } from "../lib/interrupt-command";
import {
  getLastReceivedServerSeqForTests,
  resetPendingClientCommandAcksForTests,
  resetRecentInboundEventIdsForTests,
  useWebSocketConnection,
} from "./useWebSocket";

type Listener = (e: Event | MessageEvent) => void;
type Wire = Record<string, unknown>;
type CapturedCommand = { sent_after_events: number; command: Wire };
type CapturedConnection = { index: number; commands: CapturedCommand[]; events: Wire[]; cursor_after: number };
type Fixture = {
  workspace_root: string;
  conversation_a: string;
  conversation_b: string;
  turn_id_second: string;
  assistant_message_ids: Record<string, string>;
  user_message_ids: Record<string, string>;
  prompts: Record<string, string>;
  connections: CapturedConnection[];
};

class MockWebSocket {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSING = 2; static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  readyState = 0;
  sent: Wire[] = [];
  private listeners = new Map<string, Set<Listener>>();
  constructor(public url: string) { MockWebSocket.instances.push(this); }
  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const cb: Listener = typeof listener === "function" ? (listener as Listener) : (e) => listener.handleEvent(e);
    const set = this.listeners.get(type) ?? new Set<Listener>();
    set.add(cb);
    this.listeners.set(type, set);
  }
  send = vi.fn((raw: string) => { this.sent.push(JSON.parse(raw) as Wire); });
  close = vi.fn(() => { this.readyState = 3; });
  emit(type: "open" | "close", code = 1000) {
    this.readyState = type === "open" ? 1 : 3;
    const event = Object.assign(new Event(type), { code });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  emitMessage(data: unknown) {
    for (const listener of this.listeners.get("message") ?? []) {
      listener(new MessageEvent("message", { data: JSON.stringify(data) }));
    }
  }
}

const Harness = () => { useWebSocketConnection(); return null; };

// Stream buffers flush on rAF or a 50ms fallback; advancing past the fallback
// inside act() makes every delta land before the next event is applied.
const FLUSH_MS = 60;

const loadFixture = (): Fixture =>
  JSON.parse(fs.readFileSync(path.join(__dirname, "__fixtures__session_gate.json"), "utf8")) as Fixture;

/** Re-key one captured event from the capture's command ids to the renderer's. */
const rekeyEvent = (
  event: Wire,
  captured: CapturedConnection,
  socket: MockWebSocket,
  unmapped: Set<string>,
): Wire => {
  const rekey = (id: unknown): unknown => {
    if (typeof id !== "string" || !id) return id;
    const commandIndex = captured.commands.findIndex((c) => c.command.client_command_id === id);
    if (commandIndex < 0) return id;
    const type = String(captured.commands[commandIndex].command.type);
    const ordinal = captured.commands
      .slice(0, commandIndex)
      .filter((c) => c.command.type === type).length;
    const rendererCommand = socket.sent.filter((c) => c.type === type)[ordinal];
    if (!rendererCommand) {
      unmapped.add(`${type}#${ordinal}`);
      return id;
    }
    return rendererCommand.client_command_id ?? id;
  };
  const next: Wire = { ...event, client_command_id: rekey(event.client_command_id) };
  if (next.data && typeof next.data === "object" && !Array.isArray(next.data)) {
    const data = next.data as Wire;
    if ("client_command_id" in data) next.data = { ...data, client_command_id: rekey(data.client_command_id) };
  }
  if (!("client_command_id" in event)) delete next.client_command_id;
  return next;
};

const messagesFor = (conversationId: string) => {
  const state = useAppStore.getState();
  return state.conversationId === conversationId ? state.messages : state.conversationMessages[conversationId] ?? [];
};

describe("session gate replay", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    localStorage.clear();
    resetPendingClientCommandAcksForTests();
    resetRecentInboundEventIdsForTests();
    useAppStore.setState({
      isConnected: false, conversationId: null, conversations: [], messages: [], conversationMessages: {},
      conversationStreaming: {}, isStreaming: false, connectionPhase: "connecting", reconnectAttempt: 0,
      reconnectMaxAttempts: null, connectionError: null,
    });
  });
  afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("reaches the captured terminal state across tool turn, create/switch during run, interrupt and reconnect", async () => {
    const fixture = loadFixture();
    const [c0, c1] = fixture.connections;
    const logs: string[] = [];
    const fmt = (args: unknown[]) => args.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(" ");
    vi.spyOn(console, "error").mockImplementation((...args) => { logs.push("ERR " + fmt(args)); });
    vi.spyOn(console, "warn").mockImplementation((...args) => { logs.push("WARN " + fmt(args)); });
    const unmapped = new Set<string>();
    const pendingResults: Promise<unknown>[] = [];
    let interruptCommand: ClientCommand | null = null;

    // The capture ran the conversations in bypass mode; the renderer reads the
    // mode from its store when it builds user_message.
    useAppStore.setState({ permissionMode: "bypass" });

    const drive = (captured: CapturedCommand): void => {
      const command = captured.command;
      switch (command.type) {
        case "conversation.list":
        case "commands.list":
        case "skills.list":
        case "session.restore":
          return; // the transport hook sends these itself
        case "conversation.create":
          pendingResults.push(
            sendClientCommandAwaitResult(command as unknown as ClientCommand, "conversation.create").catch(() => null),
          );
          return;
        case "user_message":
          expect(sendChatMessage({
            displayContent: String(command.content),
            conversationId: String(command.conversation_id),
            assistantMessageId: String(command.assistant_message_id),
            userMessageId: String(command.user_message_id),
          })).toBe(true);
          return;
        case "conversation.switch":
          useAppStore.getState().requestConversationSwitch(String(command.conversation_id));
          return;
        case "interrupt":
          interruptCommand = buildInterruptCommand(useAppStore.getState());
          expect(sendClientCommand(interruptCommand)).toBe(true);
          return;
        default:
          throw new Error(`capture drove an unexpected command ${String(command.type)}`);
      }
    };

    // Coalesced client commands are flushed on a microtask, so every step
    // awaits one: the renderer must have sent a command before the captured
    // answer to it can be re-keyed.
    const step = async (fn: () => void): Promise<void> => {
      await act(async () => { fn(); });
    };
    const replayConnection = async (captured: CapturedConnection, socket: MockWebSocket): Promise<void> => {
      for (const [index, event] of captured.events.entries()) {
        for (const command of captured.commands) {
          if (command.sent_after_events === index) await step(() => drive(command));
        }
        await step(() => socket.emitMessage(rekeyEvent(event, captured, socket, unmapped)));
        await step(() => vi.advanceTimersByTime(FLUSH_MS));
      }
      for (const command of captured.commands) {
        if (command.sent_after_events >= captured.events.length) await step(() => drive(command));
      }
    };

    render(<Harness />);
    await step(() => vi.advanceTimersByTime(0));
    const first = MockWebSocket.instances[0];
    await step(() => first.emit("open"));
    await step(() => vi.advanceTimersByTime(0));

    await replayConnection(c0, first);

    // ── assertions at the moment the transport drops ──
    const A = fixture.conversation_a;
    const B = fixture.conversation_b;
    expect(getLastReceivedServerSeqForTests()).toBe(c0.cursor_after);
    expect(useAppStore.getState().conversationId).toBe(A);
    expect(interruptCommand).toMatchObject({
      type: "interrupt",
      conversation_id: A,
      turn_id: fixture.turn_id_second,
      message_id: fixture.assistant_message_ids.second,
    });
    const rendererUserMessages = first.sent.filter((c) => c.type === "user_message");
    const capturedUserMessages = c0.commands.filter((c) => c.command.type === "user_message").map((c) => c.command);
    expect(rendererUserMessages.length).toBe(capturedUserMessages.length);
    rendererUserMessages.forEach((sent, i) => {
      const { client_command_id: _sentId, ...sentRest } = sent;
      const { client_command_id: _capturedId, ...capturedRest } = capturedUserMessages[i];
      expect(sentRest).toEqual(capturedRest);
    });

    // ── network cut: no close handshake, then the bounded reconnect ladder ──
    await step(() => first.emit("close", 1006));
    expect(useAppStore.getState().connectionPhase).toBe("reconnecting");
    await step(() => vi.advanceTimersByTime(2_000));
    const second = MockWebSocket.instances[1];
    expect(second).toBeDefined();
    await step(() => second.emit("open"));
    await step(() => vi.advanceTimersByTime(0));
    const restore = second.sent.find((c) => c.type === "session.restore");
    const capturedRestore = c1.commands.find((c) => c.command.type === "session.restore")?.command;
    expect(restore).toBeDefined();
    expect(restore?.last_seq).toBe(capturedRestore?.last_seq);
    expect(restore?.last_conversation_id).toBe(A);
    expect(restore?.last_workspace_root).toBe(fixture.workspace_root);

    await replayConnection(c1, second);
    await act(async () => { await Promise.all(pendingResults); });

    // ── terminal state ──
    const state = useAppStore.getState();
    expect(logs.filter((l) => l.startsWith("ERR"))).toEqual([]);
    expect(unmapped).toEqual(new Set());
    expect(getLastReceivedServerSeqForTests()).toBe(c1.cursor_after);
    expect(state.connectionPhase).toBe("connected");
    expect(state.conversationId).toBe(A);
    expect(state.conversations.map((c) => c.id)).toEqual(expect.arrayContaining([A, B]));
    expect(state.isStreaming).toBe(false);
    expect(Object.values(state.conversationStreaming).some(Boolean)).toBe(false);
    expect(second.close).not.toHaveBeenCalled();

    const assistants = messagesFor(A).filter((m) => m.role === "assistant");
    const byId = new Map(assistants.map((m) => [m.id, m]));
    const ids = fixture.assistant_message_ids;
    const firstTurn = byId.get(ids.first);
    const secondTurn = byId.get(ids.second);
    const thirdTurn = byId.get(ids.third);
    expect(firstTurn?.terminalStatus).toBe("completed");
    expect(firstTurn?.isStreaming).toBeFalsy();
    expect(firstTurn?.content.trim().length).toBeGreaterThan(0);
    const toolRecords = (firstTurn?.blocks ?? [])
      .filter((b): b is Extract<typeof b, { type: "tool_call" }> => b.type === "tool_call")
      .map((b) => b.record);
    const capturedToolIds = new Set(
      c0.events.filter((e) => e.type === "tool_result" && e.message_id === ids.first).map((e) => String(e.id)),
    );
    expect(new Set(toolRecords.map((r) => r.id))).toEqual(capturedToolIds);
    expect(toolRecords.every((r) => r.status !== "running" && r.status !== "pending")).toBe(true);
    expect(toolRecords.some((r) => r.name === "edit_file")).toBe(true);
    expect(secondTurn?.terminalStatus).toBe("interrupted");
    expect(secondTurn?.isStreaming).toBeFalsy();
    expect(thirdTurn?.terminalStatus).toBe("completed");
    expect(thirdTurn?.content).toMatch(/OK/);
    expect(messagesFor(A).filter((m) => m.role === "user").map((m) => m.id))
      .toEqual([fixture.user_message_ids.first, fixture.user_message_ids.second, fixture.user_message_ids.third]);
  });
});
