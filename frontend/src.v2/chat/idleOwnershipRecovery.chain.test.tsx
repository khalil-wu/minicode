/* @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../stores/types";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { useAppStore } from "../stores";
import { latestProjectionCommandIds, rememberSentCommandProjection } from "../protocol/command-projection";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import { handleChatStreamEvent, resetStreamResumePages } from "./chatStreamEvents";
import { handlePeripheralEvent } from "./peripheralEvents";
import { buildActivitySidebarState } from "../shell/activitySidebarState";
import { parseComposerMarkdown, richDocumentState } from "../composer/rich-composer-document";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { ExecCell } from "./cells/ExecCell";
import { ActivityCell } from "./cells/ActivityCell";
import { ToolCallTimeline, runTimelineExportJsonl } from "./tool-calls/ToolCallTimeline";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), rejectClientCommandResult: vi.fn() }));

const owner = "idle-a";
const reader = "idle-b";
const scope = { conversation_id: owner, message_id: "answer-a", turn_id: "turn-a" };
const wire = (event: object) => event as ServerEvent;
const buffers: { textStreamBuffer: StreamBuffer; thinkingStreamBuffer: StreamBuffer } = {
  textStreamBuffer: {
    push: (text, conversationId, itemId, _metadata, messageId) =>
      useAppStore.getState().appendAgentMessageDelta(itemId || "answer", text, conversationId, messageId),
    flush: vi.fn(), destroy: vi.fn(),
  },
  thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() },
};
const chat = (event: object) => handleChatStreamEvent(wire({ ...scope, ...event }), owner, buffers);
const idle = () => handleRuntimeEvent(wire({ type: "task.update", partial: true,
  session: { active_conversation_id: useAppStore.getState().conversationId, active_task_id: null, active_stream_conversation_ids: [] } }));
const history = (): ChatMessage[] => [{ id: "user-a", role: "user", content: "Run the real task", artifacts: [], timestamp: 1 }, {
  id: scope.message_id, turnId: scope.turn_id, role: "assistant", content: "", artifacts: [], timestamp: 2,
  isStreaming: true, isThinkingStreaming: true,
  blocks: [
    { type: "tool_call", record: { id: "command-a", turnId: scope.turn_id, name: "run_command", args: { command: "node task.mjs" },
      status: "running", startedAt: 3, executionStartedAt: 4, stdoutPreview: "real output", seq: 10 } },
    { type: "tool_call", record: { id: "failed-read", turnId: scope.turn_id, name: "read_file", args: { path: "missing.ts" },
      status: "failed", errorKind: "file_not_found", summary: "ENOENT: missing.ts", startedAt: 3, finishedAt: 5 } },
    { type: "progress", id: "process-a", stage: "planning", status: "running", message: "Working", timestamp: 6 },
    { type: "process", id: "note-a", itemKind: "process_text", content: "Checking the result", status: "running", timestamp: 7 },
    { type: "text", itemId: "answer", source: "model_final", content: "Received ", status: "in_progress", isStreaming: true },
  ],
}];
const messages = () => {
  const state = useAppStore.getState();
  return state.conversationId === owner ? state.messages : state.sideChats[owner]?.messages ?? state.conversationMessages[owner];
};
const answer = () => messages().find(message => message.id === scope.message_id)!;
const lateEvidence = () => {
  chat({ type: "tool_result", id: "command-a", status: "success", summary: "real exit 0", seq: 11, exit_code: 0 });
  chat({ type: "agent_message.delta", item_id: "answer", delta: "the result" });
  chat({ type: "item.completed", item: { type: "agent_message", id: "answer", source: "model_final", text: "Received the result", status: "completed" } });
};
const done = (status = "completed") => chat({ type: "done", status, reason: "real-terminal", duration_ms: 2400 });

beforeEach(() => {
  vi.clearAllMocks(); latestProjectionCommandIds.clear(); resetStreamResumePages();
  localStorage.clear();
  useAppStore.setState({ conversationId: owner, messages: history(), conversationMessages: {}, conversationStreaming: { [owner]: true },
    isStreaming: true, runtimeSession: null, sideChats: {},
    conversations: [{ id: owner, title: "A", updatedAt: "2026-10-20", workspaceRoot: "C:/a" },
      { id: reader, title: "B", updatedAt: "2026-10-20", workspaceRoot: "C:/b" }],
    workingDirectory: "C:/a", draft: "keep A draft", richDraft: undefined, attachments: [], quotedMessage: null,
    selectedMentions: [], selectedSkills: [], conversationWorkbenchStates: {}, workspaceComposerDrafts: {},
    conversationAgentStates: {}, conversationHistoryPages: {}, conversationHydration: {}, pendingProviderProgress: {},
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    inspectorEntries: [], plan: null, todos: [], subagents: [], agentProgress: [], backgroundTasks: [],
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
  });
});
afterEach(cleanup);

describe("idle ownership, terminal evidence and conversation routing", () => {
  it("stops every visible process without changing raw outcomes or inventing duration", () => {
    const raw = answer().blocks;
    idle();
    expect(answer().blocks).toBe(raw);
    expect(answer().terminalStatus).toBeUndefined();
    expect(answer().completedAt).toBeUndefined();
    const turn = projectMessagesToTurns(messages(), false)[0];
    expect(turn).toMatchObject({ status: "partial", activeCell: null, completedAt: undefined });
    expect(turn.finalAnswerCell?.markdownSource).toBe("Received ");
    const command = turn.committedCells.find(cell => cell.id === "command-a")!;
    expect(command).toMatchObject({ kind: "exec", status: "partial", stdoutFull: "real output", durationMs: undefined });
    const view = render(<>{turn.committedCells.map(cell => cell.kind === "exec" ? <ExecCell key={cell.id} cell={cell} />
      : cell.kind === "activity" ? <ActivityCell key={cell.id} cell={cell} /> : null)}<ToolCallTimeline /></>);
    expect(view.container.querySelector('[data-processing="true"], [data-running="true"], .exec-cell-running')).toBeNull();
    expect(view.container.textContent).not.toContain("进行中");
    expect(getToolCallsFromMessage(answer())).toMatchObject([{ status: "running", seq: 10 }, { status: "failed", summary: "ENOENT: missing.ts" }]);
    expect(runTimelineExportJsonl(messages(), [])).toContain('"status":"running"');
  });

  it.each(["foreground", "background", "side-chat"])("keeps late real evidence after an idle snapshot (%s)", location => {
    const original = history();
    if (location === "background") useAppStore.setState({ conversationId: reader, messages: [], workingDirectory: "C:/b", draft: "keep B draft",
      isStreaming: false, conversationMessages: { [owner]: original } });
    if (location === "side-chat") useAppStore.setState({ conversationId: reader, messages: [], isStreaming: false, draft: "keep B draft",
      sideChats: { [owner]: { id: owner, messages: original, isStreaming: true, draft: "keep side draft" } } });
    const visibleDraft = useAppStore.getState().draft;
    idle(); lateEvidence(); done();
    expect(answer()).toMatchObject({ isStreaming: false, terminalStatus: "completed", content: "Received the result", durationMs: 2400,
      terminationReason: "real-terminal" });
    expect(getToolCallsFromMessage(answer())).toMatchObject([{ status: "success", summary: "real exit 0" }, { status: "failed", summary: "ENOENT: missing.ts" }]);
    expect(useAppStore.getState().draft).toBe(visibleDraft);
    expect(useAppStore.getState().isStreaming).toBe(false);
    if (location !== "foreground") expect(useAppStore.getState().conversationId).toBe(reader);
    if (location === "side-chat") expect(useAppStore.getState().sideChats[owner].draft).toBe("keep side draft");
  });

  it("receives the real runtime terminal after ownership already became idle", () => {
    idle(); lateEvidence();
    handleRuntimeEvent(wire({ type: "agent.run.completed", ...scope, role: "main", run_id: "run-a", status: "partial", terminal_reason: "max_output" }), owner);
    expect(answer()).toMatchObject({ isStreaming: false, terminalStatus: "partial", terminationReason: "max_output" });
    expect(getToolCallsFromMessage(answer())[0].status).toBe("success");
  });

  it("keeps wrong-turn completion and an older done behind a newer live turn", () => {
    idle();
    handleRuntimeEvent(wire({ type: "agent.run.completed", ...scope, turn_id: "other-turn", run_id: "other-run", status: "completed" }), owner);
    expect(answer().terminalStatus).toBeUndefined();
    handleRuntimeEvent(wire({ type: "agent.run.started", conversation_id: owner, message_id: "new-answer", turn_id: "new-turn", run_id: "new-run", role: "main", status: "running" }), owner);
    done();
    expect(answer().terminalStatus).toBeUndefined();
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(messages().find(message => message.id === "new-answer")?.isStreaming).toBe(true);
  });

  it("preserves draft, attachments and full same-turn evidence through repeated A/B snapshots", () => {
    const richDraft = richDocumentState(parseComposerMarkdown("keep A draft"));
    const attachments = [{ id: "unsent-file", name: "draft.txt", type: "file" as const, path: "C:/a/draft.txt" }];
    useAppStore.setState({ richDraft, attachments });
    const switchTo = (id: string, live: string[]) => handleSessionEvent(wire({ type: "conversation.switched", conversation_id: id,
      conversation: { id, title: id, workspace_root: id === owner ? "C:/a" : "C:/b", messages: id === owner ? [{ id: "user-a", role: "user", content: "Run the real task", timestamp: 1 }] : [] },
      session: { active_conversation_id: id, active_stream_conversation_ids: live } }), buffers);
    for (let index = 0; index < 4; index++) {
      switchTo(reader, [owner]); useAppStore.getState().setDraft("keep B draft");
      switchTo(owner, [owner]);
      expect(answer().blocks?.some(block => block.type === "tool_call" && block.record.id === "command-a")).toBe(true);
      expect(useAppStore.getState().draft).toBe("keep A draft");
      expect(useAppStore.getState().richDraft).toEqual(richDraft);
      expect(useAppStore.getState().attachments).toEqual(attachments);
    }
    switchTo(reader, [owner]); idle(); lateEvidence(); done();
    expect(useAppStore.getState().draft).toBe("keep B draft");
    useAppStore.getState().applyConversationSwitched({ conversationId: owner });
    expect(answer().content).toBe("Received the result");
    expect(useAppStore.getState().draft).toBe("keep A draft");
    expect(useAppStore.getState().attachments).toEqual(attachments);
  });

  it("keeps yielded background commands and actual child runs live after the parent becomes idle", () => {
    chat({ type: "tool_result", id: "command-a", status: "success", summary: "Background command bg-a (running)",
      content_preview: "Process still owns the real task", seq: 11 });
    handlePeripheralEvent(wire({ type: "background.started", conversation_id: owner,
      command_id: "bg-a", command: "node long-task.mjs", started_at: 10 }));
    const child = { id: "child-a", name: "Actual worker", task: "Continue independently", status: "running" as const, background: true };
    useAppStore.setState({ subagents: [child] });
    idle();
    const state = useAppStore.getState();
    expect(state.backgroundTasks).toMatchObject([{ id: "bg-a", status: "running" }]);
    expect(state.subagents).toEqual([child]);
    expect(getToolCallsFromMessage(answer())[0]).toMatchObject({ status: "success", summary: "Background command bg-a (running)" });
    const sidebar = buildActivitySidebarState({ conversationId: owner, messages: state.messages, isStreaming: state.isStreaming,
      todos: [], plan: null, agentProgress: [], livePreviewUrl: null, previewArtifact: null, previewVerification: null,
      previewServers: [], previewLaunchProcesses: [], backgroundTasks: state.backgroundTasks, subagents: state.subagents });
    expect(sidebar.runs.find(run => run.id === "background:bg-a")).toMatchObject({ status: "running" });
  });

  it("keeps newer navigation and its draft when an old switched snapshot arrives late", () => {
    rememberSentCommandProjection({ type: "conversation.switch", conversation_id: owner }, "older", owner);
    rememberSentCommandProjection({ type: "conversation.switch", conversation_id: reader }, "newer", owner);
    handleSessionEvent(wire({ type: "conversation.switched", conversation_id: reader, client_command_id: "newer", client_command_type: "conversation.switch",
      conversation: { id: reader, title: "B", messages: [] }, session: { active_conversation_id: reader, active_stream_conversation_ids: [owner] } }), buffers);
    useAppStore.getState().setDraft("keep chosen B");
    handleSessionEvent(wire({ type: "conversation.switched", conversation_id: owner, client_command_id: "older", client_command_type: "conversation.switch",
      conversation: { id: owner, title: "A", messages: [] }, session: { active_conversation_id: owner, active_stream_conversation_ids: [] } }), buffers);
    expect(useAppStore.getState()).toMatchObject({ conversationId: reader, draft: "keep chosen B" });
    expect(useAppStore.getState().conversationStreaming[owner]).toBe(true);
  });

  it("accepts evidence after idle restore and rejects resume after its real terminal", () => {
    handleSessionEvent(wire({ type: "session.restored", active_conversation_id: owner,
      session: { active_conversation_id: owner, active_stream_conversation_ids: [] } }), buffers);
    expect(answer().terminalStatus).toBeUndefined();
    lateEvidence(); done();
    const completed = answer();
    chat({ type: "stream_resume", event_seq: 30, stream_status: "running", tool_calls_pending: [], content_blocks: [] });
    expect(answer()).toBe(completed);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });
});
