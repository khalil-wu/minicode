/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn(), dismissToast: vi.fn() }));

import { useAppStore } from "../stores";
import { sendChatMessage, resetPendingUserMessagesForTests } from "../chat/sendChatMessage";
import { sendClientCommand } from "../protocol/ws-outbox";
import { projectMessagesToTurns } from "../chat/chatSurfaceState";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { buildUpdateActivitySnapshot } from "../desktop/updateActivityMirror";
import type { ClientCommand } from "../protocol/events";
import { getLastReceivedServerSeqForTests, resetPendingClientCommandAcksForTests, resetRecentInboundEventIdsForTests, useWebSocketConnection } from "./useWebSocket";

type Wire = Record<string, any>;
type Listener = (event: Event | MessageEvent) => void;
class ReplaySocket {
  static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSED = 3;
  static instances: ReplaySocket[] = [];
  readyState = 0; sent: Wire[] = []; listeners = new Map<string, Set<Listener>>();
  constructor(public url: string) { ReplaySocket.instances.push(this); }
  addEventListener(type: string, listener: Listener) { const listeners = this.listeners.get(type) ?? new Set(); listeners.add(listener); this.listeners.set(type, listeners); }
  send = vi.fn((value: string) => this.sent.push(JSON.parse(value)));
  close = vi.fn(() => { this.readyState = 3; });
  open() { this.readyState = 1; this.listeners.get("open")?.forEach(listener => listener(new Event("open"))); }
  disconnect() { this.readyState = 3; this.listeners.get("close")?.forEach(listener => listener(Object.assign(new Event("close"), { code: 1006 }))); }
  message(payload: Wire) { this.listeners.get("message")?.forEach(listener => listener(new MessageEvent("message", { data: JSON.stringify(payload) }))); }
}
const Harness = () => { useWebSocketConnection(); return null; };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  ReplaySocket.instances = []; vi.stubGlobal("WebSocket", ReplaySocket); localStorage.clear();
  resetPendingClientCommandAcksForTests(); resetRecentInboundEventIdsForTests(); resetPendingUserMessagesForTests();
  useAppStore.setState({ conversationId: null, conversations: [], messages: [], conversationMessages: {}, conversationStreaming: {},
    isStreaming: false, runtimeSession: null, sideChats: {}, composerDraftsReady: true, draft: "", attachments: [], richDraft: undefined,
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, conversationAgentStates: {}, conversationHistoryPages: {},
    conversationHydration: {}, pendingProviderProgress: {}, inspectorEntries: [], agentProgress: [], plan: null, todos: [], subagents: [],
    pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingComposerConversationAdoption: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    connectionPhase: "connecting", reconnectAttempt: 0, reconnectMaxAttempts: null, connectionError: null, isConnected: false,
    permissionMode: "bypass", runtimeCapabilities: null, backgroundTasks: [], pendingPermissionModeChanges: {},
  });
});
afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("projects the actual long task, B snapshot completion, queue, child and 36-second reconnect without losing either draft", async () => {
  const fixture = JSON.parse(gunzipSync(fs.readFileSync(path.resolve(__dirname, "../../test/fixtures/long-task-session-20261020.json.gz"))).toString("utf8"));
  const rows: Array<{ direction: string; elapsed: number; payload: Wire }> = fixture.rows;
  const ids = new Map<string, string>(); const initializedDrafts = new Set<string>(); const deferred: Wire[] = []; const errors: string[] = [];
  vi.spyOn(console, "error").mockImplementation((...values) => errors.push(values.map(String).join(" ")));
  vi.setSystemTime(fixture.startedAtMs);
  render(<Harness />); act(() => vi.advanceTimersByTime(0));
  let socket = ReplaySocket.instances[0]; act(() => socket.open());
  const issue = async (command: Wire) => {
    if (command.type === "conversation.switch") useAppStore.getState().requestConversationSwitch(command.conversation_id);
    else if (command.type === "user_message") expect(sendChatMessage({ displayContent: command.content, backendContent: command.content,
      conversationId: command.conversation_id, assistantMessageId: command.assistant_message_id, userMessageId: command.user_message_id,
      allowWhileStreaming: command.queue_if_busy === true, busyBehavior: "queue" })).toBe(true);
    else expect(sendClientCommand(command as ClientCommand)).toBe(true);
    await vi.advanceTimersByTimeAsync(60);
    const sent = socket.sent.filter(value => value.type === command.type).at(-1)!;
    expect(sent, command.type).toBeTruthy(); ids.set(command.client_command_id, sent.client_command_id);
  };
  const rekey = (original: Wire): Wire => JSON.parse(JSON.stringify(original), (key, value) => key === "client_command_id" && ids.has(value) ? ids.get(value) : value);
  let bSnapshotProvedCompleted = false; let backgroundAObserved = false; let switchCount = 0; let disconnectAt = 0;
  for (const [index, row] of rows.entries()) {
    vi.setSystemTime(fixture.startedAtMs + row.elapsed * 1000);
    if (row.direction === "audit") {
      if (row.payload.type === "offline") { disconnectAt = row.elapsed; act(() => socket.disconnect()); }
      continue;
    }
    if (row.direction === "client") {
      if (row.payload.type === "session.restore") {
        expect(row.elapsed - disconnectAt).toBeGreaterThan(36);
        await act(async () => vi.advanceTimersByTimeAsync(1500)); socket = ReplaySocket.instances.at(-1)!; act(() => socket.open());
        const restore = socket.sent.find(command => command.type === "session.restore")!;
        expect(restore.last_seq).toBe(row.payload.last_seq); ids.set(row.payload.client_command_id, restore.client_command_id);
      } else if (row.payload.type === "conversation.switch" && useAppStore.getState().connectionPhase !== "connected") {
        // The capture client could send while recovery replay was pending.
        // The real renderer waits for the gate before issuing that selection.
        deferred.push(row.payload);
      } else await act(async () => issue(row.payload));
      continue;
    }
    act(() => socket.message(rekey(row.payload)));
    expect(socket.close, `frame ${index}: ${row.payload.type} seq=${row.payload.seq}`).not.toHaveBeenCalled();
    if (useAppStore.getState().connectionPhase === "connected" && deferred.length) {
      await act(async () => { for (const command of deferred.splice(0)) await issue(command); });
    }
    const state = useAppStore.getState();
    if (row.payload.type === "conversation.switched" && state.conversationId === row.payload.conversation_id) switchCount++;
    if (state.conversationId && [fixture.conversationA, fixture.conversationB].includes(state.conversationId) && !initializedDrafts.has(state.conversationId)) {
      initializedDrafts.add(state.conversationId);
      useAppStore.getState().setDraft(`unsent ${state.conversationId}`);
    }
    if (state.conversationId === fixture.conversationB && state.conversationStreaming[fixture.conversationA]) backgroundAObserved = true;
    if (row.payload.type === "conversation.switched" && row.payload.client_command_type === "session.restore") {
      const b = state.conversationId === fixture.conversationB ? state.messages.find(message => message.id === fixture.messageB) : undefined;
      if (b?.terminalStatus === "completed") { bSnapshotProvedCompleted = true; expect(b.content).toContain("B_SIDE_OK"); }
    }
    if (index % 50 === 0) await act(async () => vi.advanceTimersByTimeAsync(60));
  }
  await act(async () => vi.advanceTimersByTimeAsync(60));
  const state = useAppStore.getState();
  const ownerMessages = (id: string) => state.conversationId === id ? state.messages : state.conversationMessages[id];
  const expectedB = rows.find(row => row.payload.type === "session.restored")!.payload.messages.find((message: Wire) => message.id === fixture.messageB);
  const b = ownerMessages(fixture.conversationB).find(message => message.id === fixture.messageB)!;
  expect(bSnapshotProvedCompleted).toBe(true); expect(b).toMatchObject({ terminalStatus: "completed", content: expectedB.content });
  expect(rows.some(row => row.payload.type === "done" && row.payload.message_id === fixture.messageB)).toBe(false);
  for (const id of [fixture.messageA, fixture.messageQueue]) {
    const expected = rows.find(row => row.payload.type === "item.completed" && row.payload.message_id === id)!.payload.item.text;
    const answer = ownerMessages(fixture.conversationA).find(message => message.id === id)!;
    expect(answer).toMatchObject({ terminalStatus: "completed", content: expected });
    expect(answer.isStreaming).not.toBe(true); expect(answer.isThinkingStreaming).not.toBe(true);
    expect(getToolCallsFromMessage(answer).some(tool => tool.status === "running" || tool.status === "pending")).toBe(false);
  }
  for (const id of [fixture.conversationA, fixture.conversationB]) {
    expect(state.conversationStreaming[id]).toBe(false);
    expect(id === state.conversationId ? state.draft : state.conversationWorkbenchStates[id].draft).toBe(`unsent ${id}`);
    expect(projectMessagesToTurns(ownerMessages(id), false).some(turn => turn.activeCell || turn.status === "streaming")).toBe(false);
  }
  expect(backgroundAObserved).toBe(true); expect(switchCount).toBeGreaterThanOrEqual(10);
  expect(state.conversationAgentStates[fixture.conversationA].subagents).toEqual(expect.arrayContaining([expect.objectContaining({ status: "done" })]));
  expect(buildUpdateActivitySnapshot(state).activeTurns).toEqual([]);
  expect(getLastReceivedServerSeqForTests()).toBeGreaterThanOrEqual(Math.max(...rows.filter(row => row.payload.type === "done").map(row => row.payload.seq)));
  expect(errors).toEqual([]);
});
