/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true, writable: true,
    value: () => ({ matches: false, media: "", onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return false; } }),
  });
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { useAppStore } from "../stores";
import { buildUpdateActivitySnapshot } from "../desktop/updateActivityMirror";
import { sendChatMessage } from "../chat/sendChatMessage";
import { sendClientCommand } from "../protocol/ws-outbox";
import type { ClientCommand } from "../protocol/events";
import { resetPendingClientCommandAcksForTests, resetRecentInboundEventIdsForTests, useWebSocketConnection } from "./useWebSocket";

type Wire = Record<string, any>;
type Listener = (e: Event | MessageEvent) => void;
class WireSocket {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSING = 2; static readonly CLOSED = 3;
  static instances: WireSocket[] = [];
  readyState = 0;
  sent: Wire[] = [];
  listeners = new Map<string, Set<Listener>>();
  constructor(public url: string) { WireSocket.instances.push(this); }
  addEventListener(type: string, listener: Listener) {
    const entries = this.listeners.get(type) ?? new Set(); entries.add(listener); this.listeners.set(type, entries);
  }
  send = vi.fn((raw: string) => { this.sent.push(JSON.parse(raw)); });
  close = vi.fn(() => { this.readyState = 3; });
  open() { this.readyState = 1; for (const listener of this.listeners.get("open") ?? []) listener(new Event("open")); }
  disconnect() { this.readyState = 3; for (const listener of this.listeners.get("close") ?? []) listener(Object.assign(new Event("close"), { code: 1006 })); }
  emit(event: Wire) { for (const listener of this.listeners.get("message") ?? []) listener(new MessageEvent("message", { data: JSON.stringify(event) })); }
}
const Harness = () => { useWebSocketConnection(); return null; };

beforeEach(() => {
  vi.useFakeTimers(); WireSocket.instances = []; vi.stubGlobal("WebSocket", WireSocket); localStorage.clear();
  resetPendingClientCommandAcksForTests(); resetRecentInboundEventIdsForTests();
  useAppStore.setState({ isConnected: false, conversationId: null, conversations: [], messages: [], conversationMessages: {}, conversationStreaming: {}, isStreaming: false, connectionPhase: "connecting", reconnectAttempt: 0, reconnectMaxAttempts: null, connectionError: null, permissionMode: "bypass", runtimeSession: null, runtimeCapabilities: null });
});
afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(["connected", "reconnect"])("replays the real backend tool stress session: %s", async (mode) => {
  const fixturePath = process.env.MINICODE_AUDIT_WIRE || path.join(__dirname, "__fixtures__harness_audit.json.gz");
  const raw = fs.readFileSync(fixturePath);
  const fixture = JSON.parse((fixturePath.endsWith(".gz") ? gunzipSync(raw) : raw).toString("utf8"));
  const errors: string[] = [];
  vi.spyOn(console, "error").mockImplementation((...args) => { errors.push(args.map((arg) => typeof arg === "object" ? JSON.stringify(arg) : String(arg)).join(" ")); });
  render(<Harness />); act(() => vi.advanceTimersByTime(0));
  let socket = WireSocket.instances[0]; act(() => socket.open());
  const lostAfter = fixture.connections[1].commands[0].command.last_seq;
  const connections = mode === "connected" ? fixture.connections.slice(0, 1) : fixture.connections;
  for (const [connectionIndex, connection] of connections.entries()) {
    if (connectionIndex > 0) {
      act(() => socket.disconnect());
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      socket = WireSocket.instances.at(-1)!;
      act(() => socket.open());
    }
    const sentIds = new Map<string, string>();
    for (let index = 0; index < connection.events.length; index++) {
      if (mode === "reconnect" && connectionIndex === 0 && connection.events[index].seq > lostAfter) break;
      for (const entry of connection.commands.filter((c: Wire) => c.sent_after_events === index)) {
        const command = entry.command;
        await act(async () => {
          if (command.type === "user_message") {
            expect(sendChatMessage({ displayContent: command.content, conversationId: command.conversation_id,
              assistantMessageId: command.assistant_message_id, userMessageId: command.user_message_id })).toBe(true);
          } else if (command.type !== "session.restore" || connectionIndex === 0) {
            sendClientCommand(command as ClientCommand);
          }
          await vi.advanceTimersByTimeAsync(60);
        });
        const sent = socket.sent.filter((c) => c.type === command.type).at(-1);
        sentIds.set(command.client_command_id, sent!.client_command_id);
      }
      const original = connection.events[index];
      const event = { ...original };
      if (sentIds.has(event.client_command_id)) event.client_command_id = sentIds.get(event.client_command_id);
      const previous = useAppStore.getState().runtimeSession;
      act(() => socket.emit(event));
      if (event.type === "task.update" && event.partial) {
        expect(useAppStore.getState().runtimeSession).toEqual({ ...previous, ...event.session });
      }
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(60); });
    if (mode === "connected" || connectionIndex > 0) {
      const firstAnswer = useAppStore.getState().messages.find((message) => message.id === "a_audit_0");
      expect(firstAnswer?.content.length).toBe(fixture.expected_answer.length);
      expect(firstAnswer?.content).toBe(fixture.expected_answer);
    }
  }
  expect(errors).toEqual([]);
  expect(socket.close).not.toHaveBeenCalled();
  expect(useAppStore.getState().isStreaming).toBe(false);
  expect(buildUpdateActivitySnapshot(useAppStore.getState()).activeTurns).toEqual([]);
});
