/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { projectChatTurnToAgentLoop } from "../agent-loop/projection/project-turn";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";
import { releaseConversationReadingState } from "./transcriptReadingState";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true), rejectClientCommandResult: vi.fn(() => false) }));

const owner = "source-physical-a";
const user = (id: string): ChatMessage => ({ id, role: "user", content: id, timestamp: 1, artifacts: [] });
const assistant = (blocks: ContentBlock[] = [], patch: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "physical-answer", role: "assistant", content: "", turnId: "physical-turn", timestamp: 100_000,
  artifacts: [], blocks, isStreaming: true, ...patch,
});
const read = (id: string): ContentBlock => ({ type: "tool_call", record: {
  id, name: "read_file", args: { file_path: id + ".ts" }, startedAt: 100_001, status: "success",
} });
const answer: ContentBlock = { type: "text", itemId: "final-item", source: "model_final", status: "completed", content: "Same turn final", isStreaming: false };
const input = (id = "actual-steer") => ({ id, role: "user", content: "Actual model input", display_content: "Actual displayed steer",
  timestamp: 100_002, steered: true, steer_target_message_id: "physical-answer",
  context_refs: [{ kind: "file", name: "note.ts", path: "C:/a/note.ts" }],
  quoted_message: { id: "quoted", role: "assistant", content: "Actual quoted source" },
  attachments: [{ id: "original-upload-ref", kind: "document", file_name: "actual-steer.txt", media_type: "text/plain",
    artifact_id: "original-artifact-id", doc_id: "original-doc-id", size_bytes: 23 }],
});
const buffer: StreamBuffer = { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() };
const handlers = { textStreamBuffer: buffer, thinkingStreamBuffer: buffer };
const chat = (value: object) => handleChatStreamEvent({ ...value, conversation_id: owner } as ServerEvent, owner, handlers);
const rows = () => useAppStore.getState().conversationId === owner ? useAppStore.getState().messages
  : useAppStore.getState().conversationMessages[owner];
const liveItem = (type: "item.started" | "item.completed", id = "actual-steer") => ({ type, conversation_id: owner,
  message_id: "physical-answer", turn_id: "physical-turn", item: { id, type: "user_message", message: input(id) } });

beforeEach(() => {
  vi.clearAllMocks();
  releaseConversationReadingState(owner);
  useAppStore.setState({ conversationId: owner, conversations: [{ id: owner, title: "A", updatedAt: "", workspaceRoot: "C:/a" }],
    messages: [user("original-user"), assistant()], conversationMessages: {}, conversationStreaming: { [owner]: true },
    isStreaming: true, sideChats: {}, runtimeSession: null, pendingProviderProgress: {}, conversationAgentStates: {},
    conversationHydration: {}, conversationHistoryPages: {}, pendingConversationSwitchId: null, pendingConversationCreateId: null,
    draft: "original unsent draft", permissionMode: "confirm", workingDirectory: "C:/a", inspectorEntries: [],
    agentProgress: [], todos: [], plan: null, subagents: [], turnDiffs: {}, viewMode: "normal",
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); releaseConversationReadingState(owner); });

describe("Source execution time provenance", () => {
  it.each([0, 95_000.25])("binds the original main-run start even when the exact turn was already bound (%s)", (startedAt) => {
    handleRuntimeEvent({ type: "agent.run.started", conversation_id: owner, message_id: "physical-answer", turn_id: "physical-turn",
      run_id: "physical-run", role: "main", status: "running", started_at: startedAt,
      timestamp: "2026-10-15T12:00:00.000Z" } as ServerEvent, owner);
    expect(rows()[1]).toMatchObject({ runStartedAt: startedAt, timestamp: startedAt, turnId: "physical-turn" });
    expect(projectMessagesToTurns(rows(), true)[0].startedAt).toBe(startedAt);
  });

  it("keeps original start through every resume page instead of the follower's observation time", () => {
    vi.useFakeTimers(); vi.setSystemTime(120_000);
    const base = { type: "stream_resume", message_id: "physical-answer", turn_id: "physical-turn", started_at: 95_000.25,
      event_seq: 12, stream_status: "running", tool_calls_pending: [], tool_states: [], snapshot_id: "paged-clock" };
    chat({ ...base, snapshot_part: 0, snapshot_complete: false, content_blocks: [read("before-resume")] });
    expect(rows()[1].runStartedAt).toBeUndefined();
    chat({ ...base, snapshot_part: 1, snapshot_complete: true, content_blocks: [] });
    expect(rows()[1].runStartedAt).toBe(95_000.25);
    const view = render(<ChatTurn turn={projectMessagesToTurns(rows(), true)[0]} conversationId={owner} />);
    expect(screen.getByText("已处理 24秒")).toBeTruthy();
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByText("已处理 25秒")).toBeTruthy();
    view.unmount();
    chat({ ...base, snapshot_id: undefined, snapshot_part: undefined, snapshot_complete: undefined, content_blocks: [read("before-resume")] });
    expect(rows()[1].runStartedAt).toBe(95_000.25);
  });

  it("does not turn transport, creation or completion timestamps into a missing execution clock", () => {
    vi.useFakeTimers(); vi.setSystemTime(120_000);
    handleRuntimeEvent({ type: "agent.run.started", conversation_id: owner, message_id: "physical-answer", turn_id: "physical-turn",
      run_id: "physical-run", role: "main", status: "running", timestamp: "2026-10-15T12:00:00.000Z" } as ServerEvent, owner);
    chat({ type: "stream_resume", message_id: "physical-answer", turn_id: "physical-turn", event_seq: 13,
      timestamp: "2026-10-15T12:00:05.000Z", stream_status: "running", content_blocks: [read("unknown-clock")], tool_calls_pending: [] });
    const turn = projectMessagesToTurns(rows(), true)[0];
    expect(turn.startedAt).toBeUndefined();
    render(<ChatTurn turn={turn} conversationId={owner} />);
    expect(screen.getByText("处理中")).toBeTruthy();
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByText("处理中")).toBeTruthy();
    expect(projectChatTurnToAgentLoop({ ...turn, status: "completed", completedAt: 130_000 }).durationMs).toBeNull();
    expect(projectChatTurnToAgentLoop({ ...turn, status: "completed", completedAt: 130_000, durationMs: 13_719.625 }).durationMs).toBe(13_719.625);
  });

  it("retains the actual live origin across session sync and restores authoritative raw final duration", () => {
    useAppStore.setState({ messages: [user("original-user"), assistant([read("retained")], { runStartedAt: 95_000.25 })] });
    handleSessionEvent({ type: "session.synced", synced: true, session_id: "sync-clock", active_conversation_id: owner,
      session: { session_id: "sync-clock", active_conversation_id: owner, active_task_id: "physical-task", active_stream_conversation_ids: [owner] },
      active_conversation: { id: owner, title: "A", revision: 20, transcript: [
        { id: "original-user", role: "user", content: "original-user", timestamp: 1 },
        { id: "physical-answer", role: "assistant", content: "", timestamp: 120_000, blocks: [] },
      ] } } as ServerEvent, handlers);
    expect(rows()[1].runStartedAt).toBe(95_000.25);
    const final = hydrateMessages([{ id: "physical-answer", role: "assistant", content: "Same turn final", blocks: [answer],
      timestamp: 100_000, completed_at: 150_000, duration_ms: 13_719.625, terminal_status: "completed" }]);
    expect(final[0].durationMs).toBe(13_719.625);
    const turn = projectMessagesToTurns(final, false)[0];
    expect(turn.startedAt).toBeUndefined();
    expect(projectChatTurnToAgentLoop(turn).durationMs).toBe(13_719.625);
  });
});

describe("Source user items remain in their original physical turn", () => {
  it.each([undefined, "earlier-answer"])("does not make an earlier unpaired user look running while a later physical turn executes (%s)", (steeredIntoMessageId) => {
    const earlier = { ...user("earlier-unpaired-user"), steeredIntoMessageId };
    const messages = [earlier, user("current-user"), assistant([read("current-read")])];
    const live = projectMessagesToTurns(messages, true);
    const settled = projectMessagesToTurns(messages.map((message) => message.role === "assistant" ? { ...message, isStreaming: false } : message), false);
    expect(live[0]).toMatchObject({ id: earlier.id, status: "completed", committedCells: [], startedAt: undefined });
    expect(live[0]).toBe(settled[0]);
    render(<ChatTurn turn={live[0]} conversationId={owner} />);
    expect(screen.queryByText("处理中")).toBeNull();
    expect(screen.queryByLabelText("Agent 处理进度")).toBeNull();
  });

  it("keeps only the actual pending tail user running and ignores hidden queue entries for that boundary", () => {
    const pending = user("actual-pending-user");
    const queue = [{ ...user("queued-user"), queueState: "queued" as const }, assistant([], { id: "queued-answer", queueState: "queued", isStreaming: false })];
    expect(projectMessagesToTurns([pending, ...queue], true)[0]).toMatchObject({ id: pending.id, status: "streaming" });
    expect(projectMessagesToTurns([{ ...pending, steeredIntoMessageId: "original-answer" }], true)[0].status).toBe("completed");
    expect(projectMessagesToTurns([{ ...pending, queueState: "cancelled" }], true)[0].status).toBe("completed");
  });

  it("admits the real user item once, flushes its boundary and keeps canonical rows in place", () => {
    useAppStore.setState({ messages: [user("original-user"), assistant([read("before-input"),
      { type: "thinking", item_id: "reasoning-item", content: "Before input", lifecycle: "delta" }]) ] });
    const started = normalizeInboundServerEvent(liveItem("item.started"));
    expect(started).not.toBeNull();
    handleChatStreamEvent(started!, owner, handlers);
    expect(buffer.flush).toHaveBeenCalled();
    expect(rows().map((message) => message.id)).toEqual(["original-user", "physical-answer", "actual-steer"]);
    expect(rows()[2]).toMatchObject({ content: "Actual displayed steer", backendContent: "Actual model input",
      steeredIntoMessageId: "physical-answer", quotedMessage: { id: "quoted", content: "Actual quoted source" },
      attachmentRefs: [expect.objectContaining({ artifactId: "original-artifact-id", docId: "original-doc-id" })] });
    useAppStore.getState().appendThinkingChunk("After input", owner, { item_id: "reasoning-item" }, "physical-answer");
    handleChatStreamEvent(normalizeInboundServerEvent(liveItem("item.completed"))!, owner, handlers);
    const blocks = rows()[1].blocks!;
    expect(blocks.filter((block) => block.type === "user_input")).toEqual([{ type: "user_input", userMessageId: "actual-steer" }]);
    expect(blocks.filter((block) => block.type === "thinking").map((block) => block.content)).toEqual(["Before input", "After input"]);
    expect(rows()[1].isThinkingStreaming).toBe(true);
    expect(rows().filter((message) => message.id === "actual-steer")).toHaveLength(1);
    expect(projectMessagesToTurns(rows(), true).map((turn) => turn.id)).toEqual(["physical-answer"]);
  });

  it.each(["reader-b", null])("receives genuine user content and artifacts while the observer reads %s", (reader) => {
    const background = [user("original-user"), assistant([read("before-background-input")])];
    const readerRows = reader ? [user("reader-local-user")] : [];
    useAppStore.setState({ conversationId: reader, messages: readerRows, isStreaming: false, conversationMessages: { [owner]: background } });
    chat(liveItem("item.started")); chat(liveItem("item.completed"));
    expect(rows()[2]).toMatchObject({ content: "Actual displayed steer", attachmentRefs: [expect.objectContaining({ artifactId: "original-artifact-id" })] });
    expect(useAppStore.getState()).toMatchObject({ conversationId: reader, messages: readerRows,
      draft: "original unsent draft", permissionMode: "confirm", workingDirectory: "C:/a", isStreaming: false });
    expect(projectMessagesToTurns(rows(), true)).toHaveLength(1);
  });

  it("interleaves two real input boundaries between their exact activities before the same final answer", () => {
    const canonical = hydrateMessages([
      { id: "original-user", role: "user", content: "Original input", timestamp: 1 },
      { id: "physical-answer", role: "assistant", turn_id: "physical-turn", content: "Same turn final", timestamp: 2,
        terminal_status: "completed", blocks: [read("before-first"), { type: "user_input", userMessageId: "actual-steer" },
          read("between-inputs"), { type: "user_input", userMessageId: "second-steer" }, read("after-second"), answer] },
      input(), { ...input("second-steer"), display_content: "Second actual input", attachments: [] },
    ]);
    const ids = canonical.map((message) => message.id);
    const turns = projectMessagesToTurns(canonical, false);
    expect(turns).toHaveLength(1);
    expect(turns[0].id).toBe("physical-answer");
    expect(turns[0].workItems?.map((item) => item.kind)).toEqual(["process", "user", "process", "user", "process"]);
    const { container } = render(<ChatTurn turn={turns[0]} conversationId={owner} defaultProcessExpanded />);
    const ordered = [screen.getByText("Original input"), screen.getByTitle("before-first.ts"), screen.getByText("Actual displayed steer"),
      screen.getByTitle("between-inputs.ts"), screen.getByText("Second actual input"), screen.getByTitle("after-second.ts"), screen.getByText("Same turn final")];
    for (let index = 1; index < ordered.length; index += 1) {
      expect(ordered[index - 1].compareDocumentPosition(ordered[index]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(container.querySelectorAll(".chat-turn")).toHaveLength(1);
    expect(screen.getAllByText("actual-steer.txt")).toHaveLength(1);
    expect(canonical.map((message) => message.id)).toEqual(ids);
    expect(projectRecentMessagesToTurns(canonical, false, 1)).toMatchObject({ totalTurnCount: 1, hiddenTurnCount: 0 });
  });

  it("collapses completed work while keeping admitted input, attachment and final visible", () => {
    const actualUser = hydrateMessages([input()])[0];
    const live = [user("original-user"), assistant([read("before-collapse"), { type: "user_input", userMessageId: actualUser.id }, read("after-collapse")]), actualUser];
    const view = render(<ChatTurn turn={projectMessagesToTurns(live, true)[0]} conversationId={owner} />);
    expect(screen.getByTitle("after-collapse.ts")).toBeTruthy();
    const final = live.map((message) => message.id === "physical-answer" ? { ...message,
      isStreaming: false, terminalStatus: "completed" as const, blocks: [...message.blocks!, answer] } : message);
    view.rerender(<ChatTurn turn={projectMessagesToTurns(final, false)[0]} conversationId={owner} />);
    expect(screen.queryByTitle("before-collapse.ts")).toBeNull();
    expect(screen.queryByTitle("after-collapse.ts")).toBeNull();
    expect(screen.getByText("Actual displayed steer")).toBeTruthy();
    expect(screen.getByText("actual-steer.txt")).toBeTruthy();
    expect(screen.getByText("Same turn final")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByTitle("after-collapse.ts")).toBeTruthy();
    view.unmount();
    render(<ChatTurn turn={projectMessagesToTurns(final, false)[0]} conversationId={owner} />);
    expect(screen.getByTitle("after-collapse.ts")).toBeTruthy();
  });

  it("keeps user-only input visible and hides human bubbles in child work records", () => {
    const actualUser = hydrateMessages([input()])[0];
    const messages = [assistant([{ type: "user_input", userMessageId: actualUser.id }, answer], { isStreaming: false }), actualUser];
    const turn = projectMessagesToTurns(messages, false)[0];
    const view = render(<ChatTurn turn={turn} conversationId={owner} />);
    expect(screen.getByText("Actual displayed steer")).toBeTruthy();
    expect(view.container.textContent).not.toContain("0");
    view.rerender(<ChatTurn turn={turn} conversationId={owner} isTranscriptMode />);
    expect(screen.queryByText("Actual displayed steer")).toBeNull();
    expect(screen.getByText("Same turn final")).toBeTruthy();
  });

  it("updates segmented activity receipts while the actual referenced user row is outside the loaded history", () => {
    useAppStore.setState({ messages: [assistant([read("before-unloaded-input"),
      { type: "user_input", userMessageId: "not-yet-loaded-user" }, read("after-unloaded-input")])] });
    projectMessagesToTurns(rows(), true);
    useAppStore.getState().updateToolCall("after-unloaded-input", { outputPreview: "Actual updated receipt" }, owner, undefined, "physical-answer");
    const turn = projectMessagesToTurns(rows(), true)[0];
    const segment = turn.workItems!.at(-1)!;
    expect(segment.kind).toBe("process");
    if (segment.kind === "process") expect(segment.cells[0]).toMatchObject({ toolCallRecords: [expect.objectContaining({ outputPreview: "Actual updated receipt" })] });
    expect(turn.workItems?.some((item) => item.kind === "user")).toBe(false);
  });

  it("retains tool images, independent terminal evidence and one final diff across the input boundary", () => {
    const actualUser = hydrateMessages([input()])[0];
    const original = assistant([read("before-image"), { type: "user_input", userMessageId: actualUser.id },
      { type: "tool_call", record: { id: "edit-after-input", name: "apply_patch", args: {}, status: "success",
        startedAt: 100_005, activityKind: "fileChange", diff: { plus: 1, minus: 1,
          files: [{ path: "actual.ts", plus: 1, minus: 1, patch: "@@ -1 +1 @@\n-old\n+new" }] } } }],
    { isStreaming: false, terminalStatus: "failed", failureMessage: "Actual terminal model error", artifacts: [
      { artifactId: "owned-image", kind: "image", summary: "Actual tool image", source: "tool", toolCallId: "before-image" },
      { artifactId: "retained-unloaded-owner-image", kind: "image", summary: "Actual unowned tool image", source: "tool", toolCallId: "unloaded-owner" },
    ] });
    const turn = projectMessagesToTurns([user("original-user"), original, actualUser], false)[0];
    const workCells = turn.workItems!.flatMap((item) => item.kind === "process" ? item.cells : []);
    expect(workCells.map((cell) => cell.id)).toEqual(turn.committedCells.filter((cell) => cell.kind !== "diff").map((cell) => cell.id));
    expect(workCells.flatMap((cell) => cell.kind === "activity" || cell.kind === "exec" ? (cell.artifacts ?? []).map((artifact) => artifact.artifactId) : []))
      .toEqual(["owned-image", "retained-unloaded-owner-image"]);
    expect(workCells.filter((cell) => cell.kind === "error")).toHaveLength(1);
    expect(turn.committedCells.filter((cell) => cell.kind === "diff")).toHaveLength(1);
  });
});

describe("Source user-item and original-clock wire contracts", () => {
  it("requires real user identity and original turn ownership on both existing item events", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    for (const type of ["item.started", "item.completed"] as const) {
      const valid = liveItem(type);
      expect(normalizeInboundServerEvent(valid)).not.toBeNull();
      for (const invalid of [{ ...valid, turn_id: undefined }, { ...valid, message_id: undefined },
        { ...valid, item: { ...valid.item, id: "different-user" } },
        { ...valid, item: { ...valid.item, message: { ...input(), role: "assistant" } } }]) {
        expect(normalizeInboundServerEvent(invalid)).toBeNull();
      }
    }
    vi.restoreAllMocks();
  });

  it("accepts zero and fractional original start on resume and rejects malformed start or input markers", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const base = { type: "stream_resume", conversation_id: owner, message_id: "physical-answer", turn_id: "physical-turn",
      tool_calls_pending: [], content_blocks: [{ type: "user_input", userMessageId: "actual-steer" }] };
    for (const startedAt of [0, 95_000.25]) expect(normalizeInboundServerEvent({ ...base, started_at: startedAt })).toMatchObject({ started_at: startedAt });
    expect(normalizeInboundServerEvent(base)).not.toBeNull();
    for (const startedAt of [-1, NaN, Infinity, "95000", null]) expect(normalizeInboundServerEvent({ ...base, started_at: startedAt })).toBeNull();
    expect(normalizeInboundServerEvent({ ...base, content_blocks: [{ type: "user_input", userMessageId: 123 }] })).toBeNull();
    vi.restoreAllMocks();
  });
});
