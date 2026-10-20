/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import activeCapture from "../../test/fixtures/code-cell-main-active-actual-20261020.json";
import idleCapture from "../../test/fixtures/code-cell-main-idle-actual-20261020.json";
import activeCanonicalCapture from "../../test/fixtures/code-cell-main-active-canonical-actual-20261020.json";
import idleCanonicalCapture from "../../test/fixtures/code-cell-main-idle-canonical-actual-20261020.json";
import childCapture from "../../test/fixtures/code-cell-child-actual-20261020.json";
import { useAppStore } from "../stores";
import type { ChatMessage, SubagentState } from "../stores/types";
import type { CommandResultEvent, ServerEvent, ToolCallEvent, ToolResultEvent } from "../protocol/events";
import type { SubagentTranscriptEventsDelta } from "../protocol/streaming-types";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getPresentedContentBlocks, getToolCallsFromMessage } from "../lib/content-blocks";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { handleChatStreamEvent, resetStreamResumePages } from "./chatStreamEvents";
import { handleRuntimeEvent } from "./runtimeEvents";
import { applySubagentTranscriptEvents } from "./subagentTranscript";
import { projectMessagesToTurns } from "./chatSurfaceState";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true), rejectClientCommandResult: vi.fn(),
  sendClientCommandAwaitResult: vi.fn(() => new Promise(() => {})),
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

const activeWire = activeCapture.server_events as unknown as ServerEvent[];
const originalStart = activeWire.find(event => event.type === "agent.run.started")!;
const firstDoneIndex = activeWire.findIndex(event => event.type === "done");
const lateStart = activeWire.slice(firstDoneIndex + 1).find(event => event.type === "tool_call")! as ToolCallEvent & { message_id: string; conversation_id: string };
const lateResult = activeWire.find(event => event.type === "tool_result" && event.id === lateStart.id)! as ToolResultEvent & { message_id: string; conversation_id: string };
const owner = lateStart.conversation_id;
const originalMessageId = lateStart.message_id;
const buffers = {
  textStreamBuffer: {
    push: (text, conversationId, itemId, _metadata, messageId) =>
      useAppStore.getState().appendAgentMessageDelta(itemId || "answer", text, conversationId, messageId),
    flush: vi.fn(), destroy: vi.fn(),
  } as StreamBuffer,
  thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } as StreamBuffer,
};
const dispatch = (raw: ServerEvent) => {
  // This backend fixture enters _handle_user_message directly, so its
  // admission receipts have an empty client_command_id. Neither shared
  // handler under test consumes command.result; replay its tool/turn facts.
  if (raw.type === "command.result") return;
  const event = normalizeInboundServerEvent(raw);
  expect(event, raw.type).not.toBeNull();
  handleRuntimeEvent(event!);
  handleChatStreamEvent(event!, undefined, buffers);
};
const ownerMessages = (id = owner) => {
  const state = useAppStore.getState();
  return id === state.conversationId ? state.messages : state.sideChats[id]?.messages ?? state.conversationMessages[id] ?? [];
};
const original = (id = owner, messageId = originalMessageId) => ownerMessages(id).find(message => message.id === messageId)!;
const transcript = (conversation: { transcript: unknown }) => hydrateMessages(conversation.transcript as BackendTranscriptMessage[]);
const setLocation = (messages: ChatMessage[], location: string, id = owner) => {
  useAppStore.setState({ conversationId: location === "foreground" ? id : "reader", messages: location === "foreground" ? messages : [],
    conversationMessages: location === "background" ? { [id]: messages } : {},
    sideChats: location === "side-chat" ? { [id]: { id, messages, isStreaming: false, draft: "keep side draft" } } : {},
    isStreaming: false, conversationStreaming: { [id]: false }, draft: "keep visible draft" });
};

beforeEach(() => {
  vi.clearAllMocks(); resetStreamResumePages(); localStorage.clear();
  useAppStore.setState({ conversationId: owner, messages: [], conversationMessages: {}, conversationStreaming: {}, isStreaming: false,
    sideChats: {}, runtimeSession: null, conversationAgentStates: {}, conversationHistoryPages: {}, conversationHydration: {},
    pendingProviderProgress: {}, pendingConversationSwitchId: null, pendingConversationCreateId: null, pendingComposerConversationAdoption: null,
    inspectorEntries: [], plan: null, todos: [], subagents: [], agentProgress: [], backgroundTasks: [],
    draft: "keep visible draft", richDraft: undefined, attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [],
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {},
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
  });
});

describe("real WS code-cell facts after normal final", () => {
  it.each(["foreground", "background", "side-chat"])("replays final, next turn, original result, late leaf start and result (%s)", location => {
    setLocation(transcript(activeCapture.initial_conversation).filter(message => message.role === "user"), location);
    for (const event of activeWire.slice(0, firstDoneIndex + 1)) dispatch(event);
    const ended = original();
    expect(ended).toMatchObject({ terminalStatus: "completed", isStreaming: false });
    const firstLeaf = getToolCallsFromMessage(ended).find(record => record.callSource?.kind === "code_mode")!;
    expect(firstLeaf.status).toBe("running");
    expect(getPresentedContentBlocks(ended).find(block => block.type === "tool_call" && block.record.id === firstLeaf.id))
      .toMatchObject({ record: { status: "running" } });
    for (const event of activeWire.slice(firstDoneIndex + 1, 51)) dispatch(event);
    const next = ownerMessages().find(message => message.id === "assistant-next")!;
    expect(next.isStreaming).toBe(true);
    for (const event of activeWire.slice(51)) dispatch(event);
    expect(ownerMessages().find(message => message.id === next.id)).toEqual(next);
    const leaves = getToolCallsFromMessage(original()).filter(record => record.callSource?.kind === "code_mode");
    expect(leaves).toHaveLength(2);
    for (const leaf of leaves) expect(leaf).toMatchObject({ status: "success", summary: expect.stringContaining("process completed"), turnId: lateStart.turn_id });
    expect(original().content).toBe(ended.content);
    expect(original().completedAt).toBe(ended.completedAt);
    expect(useAppStore.getState().draft).toBe("keep visible draft");
    if (location === "side-chat") expect(useAppStore.getState().sideChats[owner]).toMatchObject({ isStreaming: true, draft: "keep side draft" });
    else expect(useAppStore.getState().conversationStreaming[owner]).toBe(true);
    expect(useAppStore.getState().isStreaming).toBe(location === "foreground");
    const projected = projectMessagesToTurns(ownerMessages(), true).find(turn => turn.id === originalMessageId)!;
    expect(projected.status).toBe("completed"); expect(projected.activeCell).toBeNull();
  });

  it.each(["active", "idle", "active-canonical", "idle-canonical"])("uses the persisted turn identity after cold hydration (%s)", mode => {
    const capture = mode === "active" ? activeCapture : mode === "idle" ? idleCapture
      : mode === "active-canonical" ? activeCanonicalCapture : idleCanonicalCapture;
    const wire = capture.server_events as unknown as ServerEvent[];
    const ended = wire.find(event => event.type === "done")! as ServerEvent & { conversation_id: string; message_id: string };
    const messages = transcript(capture.initial_conversation);
    setLocation(messages, "foreground", ended.conversation_id);
    // The captured older persisted message has no top-level turn_id. Its
    // actual parent tool_exec record still owns the original run identity.
    const hydrated = messages.find(message => message.id === ended.message_id)!;
    if (mode.endsWith("-canonical")) expect(hydrated.turnId).toBe(getToolCallsFromMessage(hydrated)[0].turnId);
    else expect(hydrated.turnId).toBeUndefined();
    for (const event of wire.slice(wire.indexOf(ended) + 1)) dispatch(event);
    const actual = original(ended.conversation_id, ended.message_id);
    const expected = transcript(capture.final_conversation).find(message => message.id === ended.message_id)!;
    const facts = (message: ChatMessage) => getToolCallsFromMessage(message).map(record => ({
      id: record.id, status: record.status, summary: record.summary, callSource: record.callSource, turnId: record.turnId,
    }));
    expect(facts(actual)).toEqual(facts(expected));
    expect(useAppStore.getState().isStreaming).toBe(mode.startsWith("active"));
    expect(useAppStore.getState().draft).toBe("keep visible draft");
  });

  it("keeps ordinary missing tool results failed while a real owned leaf stays running", () => {
    const messages = transcript(activeCapture.initial_conversation);
    const ended = messages.find(message => message.id === originalMessageId)!;
    useAppStore.setState({ messages: messages.map(message => message.id === ended.id ? { ...message, turnId: lateStart.turn_id,
      terminalStatus: undefined, completedAt: undefined, isStreaming: true, blocks: [...message.blocks!, {
        type: "tool_call", record: { id: "ordinary", name: "read_file", args: { path: "x" }, status: "running", startedAt: 1 },
      }] } : message), isStreaming: true });
    useAppStore.getState().finishStreaming(owner, undefined, "completed", ended.id);
    expect(getToolCallsFromMessage(original()).find(record => record.id === "ordinary")?.status).toBe("failed");
    expect(getToolCallsFromMessage(original()).find(record => record.callSource?.kind === "code_mode")?.status).toBe("running");
  });

  it.each(["cell_id", "parent_call_id", "runtime_call_id", "turn_id", "seq", "missing_call_source", "iteration_id"])("rejects an unrelated or stale late result (%s)", field => {
    setLocation(transcript(activeCapture.initial_conversation), "foreground");
    dispatch(lateStart);
    const before = original();
    const invalid = structuredClone(lateResult) as unknown as Record<string, unknown>;
    if (field === "missing_call_source") delete invalid.call_source;
    else if (["cell_id", "parent_call_id", "runtime_call_id"].includes(field)) (invalid.call_source as Record<string, unknown>)[field] = "different";
    else invalid[field] = field === "seq" ? lateStart.seq : "different";
    invalid.output_files = [{ path: "unrelated.txt", size: 1 }]; invalid.superseded_tool_call_ids = ["start-cell"];
    dispatch(invalid as unknown as ServerEvent);
    expect(original()).toBe(before);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it.each(["cell_id", "parent_call_id", "turn_id", "receipt"])("cannot create a post-final leaf without its original running receipt (%s)", field => {
    const messages = transcript(activeCapture.initial_conversation);
    if (field === "receipt") {
      const message = messages.find(message => message.id === originalMessageId)!;
      message.blocks = message.blocks!.map(block => block.type === "tool_call" && block.record.id === "start-cell"
        ? { ...block, record: { ...block.record, summary: "cell_id alone is not a receipt", outputPreview: undefined, contentPreview: undefined } } : block);
    }
    setLocation(messages, "foreground");
    const before = original();
    const invalid = structuredClone(lateStart) as unknown as Record<string, unknown>;
    if (field === "turn_id") invalid.turn_id = "different";
    else if (field !== "receipt") (invalid.call_source as Record<string, unknown>)[field] = "different";
    dispatch(invalid as unknown as ServerEvent);
    expect(original()).toBe(before);
  });

  it("accepts only exact leaf output and keeps stale text and lifecycle behind final", () => {
    setLocation(transcript(activeCapture.initial_conversation), "foreground");
    dispatch(lateStart);
    const base = { conversation_id: owner, message_id: originalMessageId, turn_id: lateStart.turn_id, id: lateStart.id,
      call_source: lateStart.call_source, seq: lateStart.seq! + 1 };
    dispatch({ ...base, type: "tool_output_delta", output: "actual nested output" } as unknown as ServerEvent);
    expect(getToolCallsFromMessage(original()).find(record => record.id === lateStart.id)?.outputPreview).toBe("actual nested output");
    const before = original();
    dispatch({ ...base, seq: base.seq + 1, call_source: { ...base.call_source!, runtime_call_id: "different" },
      type: "tool_output_delta", output: "unrelated" } as unknown as ServerEvent);
    dispatch({ ...base, type: "agent_message.delta", item_id: "late-text", delta: "unrelated" } as unknown as ServerEvent);
    dispatch(originalStart);
    expect(original()).toBe(before);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });
});

type BridgeFrame = { type: string; data: Record<string, unknown> };
const childFrames = childCapture as unknown as BridgeFrame[];
const childData = (index: number) => childFrames[index].data;
const childId = String(childData(20).subagent_id);
const childEvent = (index: number) => normalizeInboundServerEvent({ type: childFrames[index].type, ...childData(index), conversation_id: owner })!;
const childDelta = (index: number) => childData(index).transcript_delta as SubagentTranscriptEventsDelta;
const childSnapshot = (index: number) => childData(index).transcript_snapshot as { seq: number; messages: BackendTranscriptMessage[] };
const seedChild = (seq = 29) => {
  handleRuntimeEvent(childEvent(20));
  const snapshot = childSnapshot(seq === 29 ? 13 : 20);
  useAppStore.getState().updateSubagent(childId, { transcriptSeq: snapshot.seq, transcriptMessages: hydrateMessages(snapshot.messages) }, owner);
};
const child = () => useAppStore.getState().subagents.find(subagent => subagent.id === childId)!;
const childHeader = (state: SubagentState) => {
  const { transcriptMessages: _messages, transcriptSeq: _seq, ...header } = state;
  return header;
};

describe("real child journal facts owned by an earlier code cell", () => {
  it.each([1, 3])("keeps the current child lifecycle and applies mixed journals and the original result (current epoch %i)", epoch => {
    seedChild();
    useAppStore.getState().updateSubagent(childId, { mailboxEpoch: epoch, model: "current-model", summary: "Current turn work" }, owner);
    const before = childHeader(child());
    handleRuntimeEvent(childEvent(24));
    expect(child().transcriptSeq).toBe(childDelta(24).seq);
    handleRuntimeEvent(childEvent(25));
    expect(child().transcriptSeq).toBe(childDelta(25).seq);
    expect(childHeader(child())).toEqual(before);
    const record = childDelta(25).events[0].payload.record as { id: string; turnId: string; summary: string };
    const old = child().transcriptMessages!.find(message => message.turnId === record.turnId)!;
    expect(old).toMatchObject({ terminalStatus: "completed", isStreaming: false });
    expect(getToolCallsFromMessage(old).find(tool => tool.id === record.id)).toMatchObject({ status: "success", summary: record.summary });
    expect(child().transcriptMessages!.filter(message => message.isStreaming)).toHaveLength(1);
    expect(child().transcriptMessages!.at(-1)?.turnId).not.toBe(record.turnId);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("routes old results without moving the current text cursor inside the same journal batch", () => {
    const initial = applySubagentTranscriptEvents(hydrateMessages(childSnapshot(13).messages), childDelta(24))!;
    const nextDelta = childDelta(27);
    const combined = { kind: "events" as const, base_seq: childDelta(25).base_seq, seq: nextDelta.seq,
      events: [...childDelta(25).events, ...nextDelta.events.filter(event => event.seq > childDelta(25).seq)] };
    const updated = applySubagentTranscriptEvents(initial, combined)!;
    expect(updated.at(-1)?.content).toBe("The child turn finished.");
    expect(updated[1].content).toBe(initial[1].content);
    expect(updated.at(-1)?.isStreaming).toBe(true);
  });

  it.each(["cell_id", "parent_call_id", "runtime_call_id", "turnId"])("refuses an old result with wrong canonical ownership (%s)", field => {
    const initial = applySubagentTranscriptEvents(hydrateMessages(childSnapshot(13).messages), childDelta(24))!;
    const delta = structuredClone(childDelta(25));
    const record = delta.events[0].payload.record as Record<string, unknown>;
    if (field === "turnId") record.turnId = "different";
    else (record.callSource as Record<string, unknown>)[field] = "different";
    expect(applySubagentTranscriptEvents(initial, delta)).toBeNull();
  });

  it("recovers a journal gap from the current authoritative snapshot without applying old header metadata", async () => {
    seedChild(43);
    const before = childHeader(child());
    const response = { type: "command.result", command: "subagent.transcript", level: "info", data: {
      ...childSnapshot(30), agent_path: child().agentPath, mailbox_epoch: child().mailboxEpoch,
    } } as unknown as CommandResultEvent;
    expect(commandResultSucceeded(response)).toBe(true);
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce(response);
    handleRuntimeEvent(childEvent(24));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(child().transcriptSeq).toBe(childSnapshot(30).seq));
    expect(childHeader(child())).toEqual(before);
  });

  it("keeps duplicate code facts and old full snapshots from replacing the current child state", () => {
    seedChild(); handleRuntimeEvent(childEvent(24)); handleRuntimeEvent(childEvent(25));
    const before = child();
    handleRuntimeEvent(childEvent(25));
    handleRuntimeEvent({ ...childEvent(25), transcript_delta: undefined, transcript_snapshot: childSnapshot(13) } as unknown as ServerEvent);
    expect(child()).toBe(before);
  });
});
