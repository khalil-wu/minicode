/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true, writable: true,
    value: () => ({ matches: false, media: "", onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }),
  });
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { useAppStore } from "../stores";
import { getLastReceivedServerSeqForTests, resetPendingClientCommandAcksForTests, resetRecentInboundEventIdsForTests, useWebSocketConnection } from "./useWebSocket";

type L = (e: Event | MessageEvent) => void;
class MockWebSocket {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSING = 2; static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  readyState = 0; private listeners = new Map<string, Set<L>>();
  constructor(public url: string) { MockWebSocket.instances.push(this); }
  addEventListener(t: string, l: EventListenerOrEventListenerObject) {
    const cb: L = typeof l === "function" ? (l as L) : (e) => l.handleEvent(e);
    const s = this.listeners.get(t) ?? new Set<L>(); s.add(cb); this.listeners.set(t, s);
  }
  send = vi.fn(); close = vi.fn(() => { this.readyState = 3; });
  emit(t: "open" | "close", code = 1000) { this.readyState = t === "open" ? 1 : 3; const ev = Object.assign(new Event(t), { code }); for (const l of this.listeners.get(t) ?? []) l(ev); }
  emitMessage(d: unknown) { for (const l of this.listeners.get("message") ?? []) l(new MessageEvent("message", { data: JSON.stringify(d) })); }
}
const Harness = () => { useWebSocketConnection(); return null; };

describe("live launch replay", () => {
  beforeEach(() => {
    vi.useFakeTimers(); MockWebSocket.instances = []; vi.stubGlobal("WebSocket", MockWebSocket); localStorage.clear();
    resetPendingClientCommandAcksForTests(); resetRecentInboundEventIdsForTests();
    useAppStore.setState({ isConnected: false, conversationId: null, conversations: [], messages: [], conversationMessages: {}, conversationStreaming: {}, isStreaming: false, connectionPhase: "connecting", reconnectAttempt: 0, reconnectMaxAttempts: null, connectionError: null });
  });
  afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("applies the captured backend sequence without leaving a replay hole", () => {
    const logs: string[] = [];
    const fmt = (args: unknown[]) => args.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(" ");
    vi.spyOn(console, "error").mockImplementation((...args) => { logs.push("ERR " + fmt(args)); });
    vi.spyOn(console, "warn").mockImplementation((...args) => { logs.push("WARN " + fmt(args)); });
    const events = JSON.parse(fs.readFileSync(path.join(__dirname, "__fixtures__launch.json"), "utf8")) as Array<Record<string, unknown>>;
    render(<Harness />); act(() => vi.advanceTimersByTime(0));
    const socket = MockWebSocket.instances[0]; act(() => socket.emit("open"));
    for (const ev of events) {
      act(() => socket.emitMessage(ev));
      const s = useAppStore.getState();
      console.log(`after seq=${ev.seq} ${ev.type}: cursor=${getLastReceivedServerSeqForTests()} active=${s.conversationId} convs=${s.conversations.length} closed=${socket.close.mock.calls.length}`);
    }
    console.log("LOGS:\n" + logs.map((l) => l.slice(0, 900)).join("\n"));
    const s = useAppStore.getState();
    expect(logs.filter((l) => l.startsWith("ERR"))).toEqual([]);
    expect(s.conversationId).toBe(events.at(-1)!.conversation_id);
  });
});
