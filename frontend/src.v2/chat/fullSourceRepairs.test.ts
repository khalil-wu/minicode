/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleUserMessageAdmission, resetPendingUserMessagesForTests, sendChatMessage } from "./sendChatMessage";
import { handleRuntimeEvent } from "./runtimeEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { applySubagentTranscriptEvents } from "./subagentTranscript";
import { hydrateMessages } from "./transcriptHydration";
import { elicitationContent, elicitationDefaults, type ElicitationField } from "./elicitationForm";
import { mergeToolDelegations } from "../lib/tool-call-reducer";
import type { ChatMessage } from "../stores/types";
import type { SubagentTranscriptEventsDelta } from "../protocol/streaming-types";
import { replacementText } from "../protocol/workspace-search";

const wire = vi.hoisted(() => ({ sent: [] as Record<string, unknown>[], accepted: true }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ send: (command: Record<string, unknown>) => {
  if (!wire.accepted) return false; wire.sent.push(command); return true;
}, sessionId: "isolated-audit" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const original: ChatMessage[] = [
  { id: "old-user", role: "user", content: "task", artifacts: [], timestamp: 1 },
  { id: "old-answer", role: "assistant", content: "preserved answer", artifacts: [], timestamp: 2 },
];
beforeEach(() => {
  resetPendingUserMessagesForTests(); wire.sent = []; wire.accepted = true;
  useAppStore.setState({ composerDraftsReady: true, conversationId: "audit", workingDirectory: "C:/audit",
    pendingConversationSwitchId: null, pendingPermissionModeChanges: {}, messages: original, conversationMessages: { audit: original },
    isStreaming: false, sideChats: {}, subagents: [], conversationAgentStates: {}, pendingApproval: null, pendingAskUser: null,
    pendingDiffReview: null, approvalQueue: [], askUserQueue: [], diffReviewQueue: [], draft: "unsent", attachments: [] });
});

describe("durable retry admission", () => {
  it("preserves old tail and draft after send and semantic refusal", () => {
    expect(sendChatMessage({ conversationId: "audit", displayContent: "task", retryFromMessageId: "old-user" })).toBe(true);
    expect(useAppStore.getState().messages).toBe(original);
    const command = wire.sent[0];
    handleUserMessageAdmission({ type: "command.result", command: "user_message", level: "error", message: "another run owns it",
      client_command_id: command.client_command_id as string, data: { admitted: false, conversation_id: "audit" } });
    expect(useAppStore.getState().messages).toBe(original);
    expect(useAppStore.getState().draft).toBe("unsent");
  });
  it("commits exact input once before provider events, even after navigation", () => {
    sendChatMessage({ conversationId: "audit", displayContent: "task", retryFromMessageId: "old-user" });
    const command = wire.sent[0];
    useAppStore.setState({ conversationId: "new", messages: [], draft: "new draft" });
    handleRuntimeEvent({ type: "agent.run.started", role: "main", run_id: "actual-retry-run", conversation_id: "audit",
      message_id: command.assistant_message_id as string, turn_id: "actual-retry-run" });
    const receipt = { type: "command.result" as const, command: "user_message", level: "success", message: "admitted",
      client_command_id: command.client_command_id as string, data: { admitted: true, conversation_id: "audit" } };
    handleUserMessageAdmission(receipt); handleUserMessageAdmission(receipt);
    const state = useAppStore.getState();
    expect(state.messages).toEqual([]); expect(state.draft).toBe("new draft");
    expect(state.conversationMessages.audit.map(message => message.id)).toEqual([command.user_message_id, command.assistant_message_id]);
    expect(state.conversationStreaming.audit).toBe(true);
  });
  it("does not terminate an existing task when the socket rejects retry", () => {
    wire.accepted = false; useAppStore.setState({ isStreaming: true });
    expect(sendChatMessage({ conversationId: "audit", displayContent: "task", retryFromMessageId: "old-user" })).toBe(false);
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(useAppStore.getState().messages.slice(0, 2)).toEqual(original);
  });
});

describe("real child lifecycle", () => {
  it("updates the original parent tool without reopening its settled run", () => {
    const pending = { agent_id: "child", name: "Ada", status: "pending", started_at: 0, accepted_at: 100 };
    const parent = { ...original[1], blocks: [{ type: "tool_call" as const, record: { id: "spawn", name: "task", args: {}, status: "success" as const, delegations: [pending] } }] };
    useAppStore.setState({ messages: [original[0], parent], conversationMessages: { audit: [original[0], parent] } });
    const fact = { ...pending, status: "running", started_at: 200 };
    const event = normalizeInboundServerEvent({ type: "subagent.start", conversation_id: "audit", subagent_id: "child", parent_id: "parent",
      role: "Ada", parent_message_id: "old-answer", parent_tool_call_id: "spawn", delegation: fact });
    expect(event).toBeTruthy(); handleRuntimeEvent(event!);
    const block = useAppStore.getState().messages[1].blocks![0];
    expect(block.type === "tool_call" && block.record.delegations).toEqual([fact]);
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(mergeToolDelegations([fact], [pending])[0]).toMatchObject({ started_at: 200, status: "running" });
  });
  it("rejects malformed delegation facts at the public wire boundary", () => {
    expect(normalizeInboundServerEvent({ type: "subagent.start", conversation_id: "audit", subagent_id: "child",
      delegation: { agent_id: "child", name: "Ada", started_at: -1, status: "running" }, parent_message_id: "parent", parent_tool_call_id: "call" })).toBeNull();
  });
});

describe("incremental child transcript", () => {
  const base: ChatMessage[] = [{ id: "subagent-turn-run", role: "assistant", content: "", timestamp: 10, artifacts: [], isStreaming: true,
    turnId: "run", blocks: [{ type: "text", itemId: "text", content: "🙂", source: "pending", status: "running", isStreaming: true }] }];
  const event = (seq: number, event_type: string, payload: Record<string, unknown>) => ({ event_id: `e${seq}`, seq, ts_ms: seq * 10, event_type, payload });
  it("keeps args, exact output, code-point offsets and skipped private journal sequences", () => {
    const delta: SubagentTranscriptEventsDelta = { kind: "events", base_seq: 3, seq: 9, events: [
      event(5, "tool_use", { tool_call: { id: "read", name: "read_file", args: { path: "中文.txt" }, status: "running", startedAt: 50 } }),
      event(7, "tool_result", { tool_call_id: "read", record: { id: "read", name: "read_file", status: "success", outputPreview: "<error>literal</error>", finishedAt: 70 } }),
      event(8, "progress", { kind: "assistant_message", item_id: "text", content_delta: " tail", content_offset: 1, source: "pending", status: "running" }),
    ] };
    const wireEvent = normalizeInboundServerEvent({ type: "subagent.progress", conversation_id: "audit", subagent_id: "child", transcript_delta: delta });
    expect(wireEvent).toBeTruthy();
    const next = applySubagentTranscriptEvents(base, delta)!;
    const read = next[0].blocks![1];
    expect(read.type === "tool_call" && read.record).toMatchObject({ args: { path: "中文.txt" }, outputPreview: "<error>literal</error>", durationMs: 20 });
    expect(next[0].content).toBe("🙂 tail");
    expect(base[0].blocks![0]).toMatchObject({ content: "🙂" });
  });
  it("does not advance partially applied batches after missing tools or wrong offsets", () => {
    const delta: SubagentTranscriptEventsDelta = { kind: "events", base_seq: 3, seq: 5,
      events: [event(5, "progress", { kind: "assistant_message", item_id: "text", content_delta: "bad", content_offset: 2 })] };
    expect(applySubagentTranscriptEvents(base, delta)).toBeNull();
    expect(base[0].content).toBe("");
  });
  it("starts a new text item from its first journal delta without requesting a full transcript", () => {
    const empty: ChatMessage[] = [{ ...base[0], blocks: [] }];
    const delta: SubagentTranscriptEventsDelta = { kind: "events", base_seq: 3, seq: 5, events: [
      event(5, "progress", { kind: "assistant_message", item_id: "new-item", content_delta: "🙂 first", content_offset: 0,
        source: "pending", status: "running" }),
    ] };
    expect(normalizeInboundServerEvent({ type: "subagent.progress", conversation_id: "audit", subagent_id: "child", transcript_delta: delta })).toBeTruthy();
    const next = applySubagentTranscriptEvents(empty, delta)!;
    expect(next[0].content).toBe("🙂 first");
    expect(next[0].blocks).toMatchObject([{ type: "text", itemId: "new-item", content: "🙂 first", status: "running" }]);
    expect(empty[0].blocks).toEqual([]);
    expect(applySubagentTranscriptEvents(empty, { ...delta, events: [event(5, "progress", {
      ...delta.events[0].payload, content_offset: 1,
    })] })).toBeNull();
  });
  it("keeps multiple real text items when a terminal event publishes their aggregate", () => {
    const delta: SubagentTranscriptEventsDelta = { kind: "events", base_seq: 3, seq: 7, events: [
      event(5, "progress", { kind: "assistant_message", item_id: "second", content: "第二段", source: "pending", status: "running" }),
      event(7, "assistant", { content: "🙂\n第二段", message_only: true }),
    ] };
    expect(normalizeInboundServerEvent({ type: "subagent.progress", conversation_id: "audit", subagent_id: "child", transcript_delta: delta })).toBeTruthy();
    const next = applySubagentTranscriptEvents(base, delta)!;
    const snapshot = hydrateMessages([{ id: base[0].id, role: "assistant", content: "🙂\n第二段", blocks: [
      { type: "text", item_id: "text", content: "🙂", source: "pending", status: "running", is_streaming: true },
      { type: "text", item_id: "second", content: "第二段", source: "pending", status: "running", is_streaming: true },
    ] }]);
    const textItems = (messages: ChatMessage[]) => messages[0].blocks?.filter(block => block.type === "text")
      .map(block => ({ itemId: block.itemId, content: block.content, status: block.status }));
    expect(next[0].content).toBe(snapshot[0].content);
    expect(textItems(next)).toEqual(textItems(snapshot));
    expect(base[0].blocks).toHaveLength(1);
    expect(textItems(next)?.map(block => block.content).join("\n")).toBe("🙂\n第二段");
  });
  it("accepts published tool arguments within the existing transcript budget", () => {
    const delta: SubagentTranscriptEventsDelta = { kind: "events", base_seq: 3, seq: 5,
      events: [event(5, "tool_use", { tool_call: { id: "batch", name: "mcp_batch", args: { items: Array(5000).fill("value") }, status: "running" } })] };
    expect(normalizeInboundServerEvent({ type: "subagent.progress", conversation_id: "audit", subagent_id: "child", transcript_delta: delta })).toBeTruthy();
  });
});

describe("MCP field semantics and search captures", () => {
  it("accepts the backend's full Unicode goal text without losing its owner event", () => {
    const text = "🙂".repeat(4000);
    expect(normalizeInboundServerEvent({ type: "goal.updated", conversation_id: "audit", goal: { text, status: "active" }, source: "frontend.ui" }))
      .toMatchObject({ goal: { text } });
  });
  it("supports titled multiselect, presence-required empty strings and min/max constraints", () => {
    const fields: [string, ElicitationField][] = [["text", { type: "string" }], ["tags", { type: "array", minItems: 0, maxItems: 1,
      items: { anyOf: [{ const: "one", title: "第一项" }, { const: "two", title: "第二项" }] } }], ["count", { type: "integer", minimum: 1 }]];
    const values = elicitationDefaults(fields, ["text", "tags"]);
    expect(elicitationContent(fields, ["text", "tags"], values)).toEqual({ content: { text: "", tags: [] } });
    expect(elicitationContent(fields, [], { tags: ["one", "two"] }).error).toBeTruthy();
    expect(elicitationContent(fields, [], { count: "1.2" }).error).toBeTruthy();
    expect(elicitationContent(fields, [], { count: "NaN" }).error).toBeTruthy();
    expect(elicitationContent(fields, [], { count: "1" }).content).toEqual({ count: 1 });
    expect(elicitationContent([["emoji", { type: "string", maxLength: 1 }]], ["emoji"], { emoji: "🙂" })).toEqual({ content: { emoji: "🙂" } });
    expect(elicitationContent([["choice", { type: "string", enum: ["first"], default: "first" }]], [], { choice: undefined })).toEqual({ content: {} });
  });
  it("reconstructs replacements from UTF-16 capture spans without repeated match text", () => {
    const content = "前🙂foo后";
    const match = { id: "m", offset: 1, length: 5, line: 1, column: 2, end_line: 1, end_column: 7, snippet: "",
      groups: [{ offset: 1, length: 2 }, { offset: 3, length: 3 }, null], named_groups: { word: { offset: 3, length: 3 } } };
    expect(replacementText("$2/$1/$<word>/$&/$$/$3", match, true, content)).toBe("foo/🙂/foo/🙂foo/$/");
  });
});
