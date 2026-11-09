/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const runtime = (event: Record<string, unknown>) => handleRuntimeEvent(event as ServerEvent);
const childDone = (owner: string, id: string, epoch: number, content: string, path = `/root/${id}`) => runtime({ type: "subagent.done", conversation_id: owner,
  message_id: "original-parent-already-completed", subagent_id: id, agent_path: path, mailbox_epoch: epoch, status: "completed", summary: content,
  started_at: 10, result: { content, status: "completed" } });
const parentStart = (owner: string, run: string) => runtime({ type: "agent.run.started", role: "main", conversation_id: owner, message_id: `message-${run}`, run_id: run, turn_id: run, status: "running" });
const parentDone = (owner: string, run: string) => runtime({ type: "agent.run.completed", role: "main", conversation_id: owner, message_id: `message-${run}`, run_id: run, turn_id: run, status: "completed" });
const buffer = (): StreamBuffer => ({ push: vi.fn(), flush: vi.fn(), destroy: vi.fn() });

beforeEach(() => useAppStore.setState({ conversationId: "owner-A", workingDirectory: "C:/A", pendingConversationSwitchId: null, pendingConversationCreateId: null,
  conversations: [{ id: "owner-A", title: "A", workspaceRoot: "C:/A", updatedAt: "now" }, { id: "owner-B", title: "B", workspaceRoot: "C:/B", updatedAt: "now" }],
  messages: [], conversationMessages: {}, conversationStreaming: {}, conversationAgentStates: {}, conversationWorkbenchStates: {}, sideChats: {},
  isStreaming: false, subagents: [], plan: null, todos: [], agentProgress: [], turnDiffs: {}, runtimeSession: null, inspectorEntries: [] }));

describe("conversation-lifetime child facts across main turns", () => {
  it("keeps completed children through each real new parent run while clearing only turn plan/progress", () => {
    childDone("owner-A", "child-A", 1, "Initial A result");
    childDone("owner-A", "child-B", 1, "Initial B result");
    useAppStore.setState({ todos: [{ id: "old-todo", content: "Old turn", activeForm: "Working", status: "completed" }] });
    for (const run of ["initial-followup", "mailbox-followup", "next-user-turn"]) {
      parentStart("owner-A", run);
      expect(useAppStore.getState().subagents.map((child) => child.id)).toEqual(["child-A", "child-B"]);
      expect(useAppStore.getState().conversationAgentStates["owner-A"].subagents).toHaveLength(2);
      expect(useAppStore.getState().todos).toEqual([]);
      expect(useAppStore.getState().agentProgress).toEqual([]);
      parentDone("owner-A", run);
    }
    expect(useAppStore.getState().subagents.every((child) => child.status === "done")).toBe(true);
  });

  it("updates a cached owner without clearing or importing the visible conversation's children", () => {
    childDone("owner-A", "child-A", 1, "A result");
    useAppStore.getState().applyConversationSwitched({ conversationId: "owner-B" });
    childDone("owner-B", "child-B", 1, "B result");
    parentStart("owner-A", "inactive-parent");
    childDone("owner-A", "child-A", 2, "A followup result");
    expect(useAppStore.getState().subagents.map((child) => child.id)).toEqual(["child-B"]);
    expect(useAppStore.getState().conversationAgentStates["owner-A"].subagents).toEqual([expect.objectContaining({ id: "child-A", mailboxEpoch: 2, resultContent: "A followup result" })]);
  });

  it("recovers a newer child epoch without its missed start and rejects old-epoch terminal deliveries", () => {
    childDone("owner-A", "child-A", 1, "Retained previous result");
    useAppStore.getState().updateSubagent("child-A", { completedAt: 50, durationMs: 40, toolCallCount: 3, terminationReason: "success",
      transcriptMessages: [{ id: "previous-child-turn", role: "assistant", content: "Retained previous result", timestamp: 50, terminalStatus: "completed" }] }, "owner-A");
    parentStart("owner-A", "new-parent");
    runtime({ type: "subagent.progress", conversation_id: "owner-A", message_id: "original-parent-already-completed", subagent_id: "child-A",
      agent_path: "/root/child-A", mailbox_epoch: 2, status: "running", user_visible: true, current_activity: "Reading followup", started_at: 20 });
    expect(useAppStore.getState().subagents[0]).toMatchObject({ mailboxEpoch: 2, status: "running", currentActivity: "Reading followup" });
    expect(useAppStore.getState().subagents[0]).toMatchObject({ resultAvailable: false, resultContent: undefined, completedAt: undefined,
      durationMs: undefined, toolCallCount: undefined, terminationReason: undefined });
    expect(useAppStore.getState().subagents[0].transcriptMessages?.[0].content).toBe("Retained previous result");
    childDone("owner-A", "child-A", 1, "Delayed stale result");
    childDone("owner-A", "child-A", 2, "Wrong identity result", "/root/foreign-path");
    childDone("owner-A", "child-A", 3, "Foreign identity with higher epoch", "/root/foreign-path");
    expect(useAppStore.getState().subagents[0]).toMatchObject({ mailboxEpoch: 2, status: "running" });
    childDone("owner-A", "child-A", 2, "Real new result");
    expect(useAppStore.getState().subagents[0]).toMatchObject({ mailboxEpoch: 2, status: "done", resultContent: "Real new result" });
  });

  it("does not let stale transcript/status receipts overwrite a newer incarnation through the store", () => {
    childDone("owner-A", "child-A", 2, "Current result");
    const store = useAppStore.getState();
    store.updateSubagent("child-A", { agentPath: "/root/child-A", mailboxEpoch: 1, status: "error", resultContent: "Old failure" }, "owner-A");
    store.updateSubagent("child-A", { agentPath: "/root/other-child", mailboxEpoch: 2, resultContent: "Other child result" }, "owner-A");
    store.updateSubagent("child-A", { agentPath: "/root/other-child", mailboxEpoch: 3, resultContent: "Higher epoch from another child" }, "owner-A");
    expect(useAppStore.getState().subagents[0]).toMatchObject({ agentPath: "/root/child-A", mailboxEpoch: 2, status: "done", resultContent: "Current result" });
  });

  it("hydrates retained child history after a new parent turn and a conversation switch", () => {
    childDone("owner-A", "child-A", 2, "Persisted followup result");
    parentStart("owner-A", "new-parent");
    parentDone("owner-A", "new-parent");
    const saved = useAppStore.getState().conversationAgentStates["owner-A"];
    useAppStore.getState().applyConversationSwitched({ conversationId: "owner-B" });
    handleSessionEvent({ type: "conversation.switched", conversation_id: "owner-A", conversation: {
      id: "owner-A", title: "A", workspace_root: "C:/A", updated_at: "now", transcript: [], context_snapshot: { ui_agent_state: saved },
    } } as ServerEvent, { textStreamBuffer: buffer(), thinkingStreamBuffer: buffer() });
    expect(useAppStore.getState().subagents).toEqual([expect.objectContaining({ id: "child-A", mailboxEpoch: 2, status: "done", resultContent: "Persisted followup result" })]);
    expect(useAppStore.getState().conversationAgentStates["owner-A"].subagents).toHaveLength(1);
  });
});
